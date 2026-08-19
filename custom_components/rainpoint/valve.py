from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.valve import (
    ValveDeviceClass,
    ValveEntity,
    ValveEntityFeature,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .api import (
    _encode_dp_duration_param,
    decode_hic801w,
    decode_htv145frf,
    decode_htv210b_dp_state,
    decode_htv213frf_valve,
    decode_valve_hub,
    has_bluetooth_control_identity,
)
from .const import (
    CONF_GENERIC_CONTROL_ENABLED,
    DOMAIN,
    HIC801W_STATION_COUNT,
    MODEL_HIC801W,
    MODEL_VALVE_113,
    MODEL_VALVE_145,
    MODEL_VALVE_213,
    MODEL_VALVE_245,
    MODEL_VALVE_345,
    MODEL_VALVE_405,
    VALVE_MODELS,
)
from .coordinator import (
    SILENT_DATA_TYPE,
    RainPointCoordinator,
    hub_connected_flag,
    hub_connectivity_record,
)
from .device import build_sub_device_info
from .entity import LateEntityAdder, register_late_adder, sub_device_attributes

_LOGGER = logging.getLogger(__name__)

# Default run duration used when HA opens a valve without an explicit duration.
# Users can override by calling the valve.open_valve service with a duration attr.
DEFAULT_DURATION_SECONDS = 600  # 10 minutes

# The same default expressed in the unit the HIC801W wire format reads. Kept as
# its own constant rather than DEFAULT_DURATION_SECONDS // 60 so neither family
# can silently move the other's default when it changes.
DEFAULT_HIC_DURATION_MINUTES = 10


def _duration_number_value(hass, unique_id: str) -> float | None:
    """Return a companion duration number's current value, or None.

    Resolves unique_id -> entity_id through the entity registry so the lookup
    is not sensitive to Home Assistant's auto-generated entity_id naming. The
    unit is whatever the number entity itself carries (minutes, for every
    duration entity this integration builds); converting is the caller's job,
    because the two valve families send different units on the wire.

    None covers every way the lookup can come up empty -- no registry row, no
    state, or a state that will not parse as a float -- because each leaves the
    caller with the same decision to make, which is to fall back to its own
    default.
    """
    from homeassistant.helpers import entity_registry as er

    registry = er.async_get(hass)
    entity_id = registry.async_get_entity_id("number", "rainpoint", unique_id)
    if not entity_id:
        return None
    state = hass.states.get(entity_id)
    if state is None:
        return None
    try:
        return float(state.state)
    except (ValueError, TypeError):
        return None


def _build_trusted_valve_entities(coordinator: RainPointCoordinator, key: str, info: dict) -> list:
    """Return the hand-written-decoder valve entities one sensor key supports.

    Split out of the setup closure so the trusted path's own guards do not
    nest inside it; the opt-in generic branch stays there, since it reads the
    closure's options flag.
    """
    if info.get("model") not in VALVE_MODELS:
        return []
    data = info.get("data") or {}
    # A Bluetooth-only unit is enumerated by the cloud and reaches the
    # sensors dict as a debounced silent entry whose model field is filled
    # from the sub-device record, so the model-set check above alone would
    # admit it. The silent type is the discriminator: a device the
    # integration cannot currently reach is never offered a control. The
    # absence of a zones key would also block creation today, but this guard
    # states the invariant rather than resting on that data shape.
    if data.get("type") == SILENT_DATA_TYPE:
        return []

    # Endpoint selection is a function of the committed catalog's datapoint
    # identity, never the model itself: a variant declaring the
    # Bluetooth-backed control identity commands through
    # RainPointDpValveEntity, every other admitted model through the RF
    # RainPointValveEntity.
    entity_cls = (
        RainPointDpValveEntity
        if has_bluetooth_control_identity(info.get("model"), info.get("model_code"))
        else RainPointValveEntity
    )

    built: list = []
    # One entity per zone that reported in the payload. Zones absent from the
    # payload are not created, which avoids phantom entities when a device
    # reports fewer zones than its model name implies.
    for zone_num in sorted((data.get("zones") or {}).keys()):
        built.append(entity_cls(coordinator, key, info, zone_num))
        _LOGGER.debug(
            "Creating valve entity: key=%s zone=%s model=%s class=%s",
            key,
            zone_num,
            info.get("model"),
            entity_cls.__name__,
        )
    return built


def _build_hic801w_station_valves(coordinator: RainPointCoordinator, key: str, info: dict) -> list:
    """Return the eight station valves for one HIC801W sensor key, or [].

    Kept out of _build_trusted_valve_entities rather than folded into it: that
    function is the zone-shaped path, gated on VALVE_MODELS and driven by the
    decoded zones mapping, and the HIC801W has neither. Dispatching on the
    model here mirrors binary_sensor.py's own per-station factory, which fans
    the same single running-station reading out over the same station numbers.

    Returns [] for a silent entry, for the reason the zone-shaped path gives:
    a device the integration cannot currently reach is never offered a
    control, and eight valves on a controller that has never reported would
    read wired-up while commanding nothing. Returning [] is also what makes
    the late-add path do the right thing, since the adder records nothing for
    a poll that built nothing and offers all eight on the poll the controller
    starts reporting.
    """
    if info.get("model") != MODEL_HIC801W:
        return []
    if (info.get("data") or {}).get("type") == SILENT_DATA_TYPE:
        return []

    built: list = []
    for station_num in range(1, HIC801W_STATION_COUNT + 1):
        built.append(RainPointHicStationValveEntity(coordinator, key, info, station_num))
        _LOGGER.debug("Creating HIC801W station valve entity: key=%s station=%s", key, station_num)
    return built


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    data = hass.data[DOMAIN][entry.entry_id]
    coordinator: RainPointCoordinator = data["coordinator"]

    # Skip any record that is not a dict so one malformed sub-device entry
    # cannot raise out of a builder loop and drop already-accumulated trusted
    # entities.
    sensors_cfg = {key: info for key, info in coordinator.data.get("sensors", {}).items() if isinstance(info, dict)}
    generic_enabled = entry.options.get(CONF_GENERIC_CONTROL_ENABLED, False)

    def build(key: str, info: dict) -> list:
        """Return every valve entity one sensor key currently supports.

        Widened from list[RainPointValveEntity]: the opt-in generic-control
        branch extends this with RainPointGenericValve instances, which are
        ValveEntity subclasses but not RainPointValveEntity subclasses.
        """
        built: list = _build_trusted_valve_entities(coordinator, key, info)
        built.extend(_build_hic801w_station_valves(coordinator, key, info))

        if generic_enabled:
            # Deferred import: generic_control reaches sensor.py's
            # RainPointSensorBase transitively through generic_entities, so a
            # top-level import here would pull the whole sensor platform into
            # this module's import graph.
            from .generic_control import build_generic_valve_entities

            base_slug = f"{info.get('hid', '')}_{info.get('mid', '')}_{info.get('addr', '')}"
            built.extend(build_generic_valve_entities(coordinator, key, info, base_slug))
        return built

    # The literal the PLATFORMS list and every entity_id prefix already use.
    adder = LateEntityAdder(coordinator, async_add_entities, build, "valve")
    # Published before anything is emitted, so the removal sweep can ask this
    # adder what it created for a key that later vanishes.
    register_late_adder(data, adder)

    entities: list = []
    for key, info in sensors_cfg.items():
        entities.extend(adder.collect(key, info))

    if entities:
        async_add_entities(entities)

    # Registered unconditionally. A valve that is silent at setup reports no
    # zones, so it produces nothing here, and that is exactly the install this
    # path exists for: its entities appear when it starts reporting, with no
    # reload.
    entry.async_on_unload(coordinator.async_add_listener(adder.async_on_coordinator_update))


class RainPointValveEntity(CoordinatorEntity[RainPointCoordinator], ValveEntity):
    """Represents a single irrigation zone on a RainPoint valve hub."""

    _attr_should_poll = False
    _attr_has_entity_name = True
    _attr_reports_position = False
    _attr_supported_features = ValveEntityFeature.OPEN | ValveEntityFeature.CLOSE

    def __init__(
        self,
        coordinator: RainPointCoordinator,
        sensor_key: str,
        sensor_info: dict,
        zone_num: int,
    ) -> None:
        super().__init__(coordinator)
        self._sensor_key = sensor_key
        self._sensor_info = sensor_info
        self._zone_num = zone_num

        hid = sensor_info["hid"]
        mid = sensor_info["mid"]
        addr = sensor_info["addr"]

        self._attr_unique_id = f"rainpoint_{hid}_{mid}_{addr}_zone{zone_num}"
        self._attr_name = f"Zone {zone_num}"

    # ------------------------------------------------------------------
    # Coordinator data helpers
    # ------------------------------------------------------------------

    @property
    def _zone_data(self) -> dict | None:
        sensors = self.coordinator.data.get("sensors", {})
        info = sensors.get(self._sensor_key)
        if not info:
            return None
        decoded = info.get("data")
        if not decoded:
            return None
        return decoded.get("zones", {}).get(self._zone_num)

    # ------------------------------------------------------------------
    # Entity properties
    # ------------------------------------------------------------------

    @property
    def available(self) -> bool:
        """Return whether this zone can currently accept an open/close command.

        Availability is the conjunction of two independent signals, and both
        are read fresh every poll with no debounce: a valve that claims to be
        controllable against a hub the cloud has already reported as gone is
        the specific lie this check exists to stop.
        """
        sensors = self.coordinator.data.get("sensors", {})
        info = sensors.get(self._sensor_key)
        if not info:
            return False
        decoded = info.get("data")
        if not decoded:
            return False
        # hub_online is payload-derived RF link state and is not replaced: it
        # carries meaning for the sub-device that cloud reachability does not.
        # But it is exactly as stale as the payload it rides on, so during a
        # cloud outage the frozen payload keeps reporting a healthy link. The
        # cloud's own per-poll connectivity report is required in addition.
        # The gate is "is not False", not truthiness: None means the cloud's
        # connectivity is unknown (absent, or no hub_connectivity record at
        # all) and must leave availability alone; only an explicit False
        # means the cloud reported this hub as disconnected.
        cloud_connected = hub_connected_flag(hub_connectivity_record(self.coordinator, self._sensor_info.get("mid")))
        return bool(decoded.get("hub_online", False)) and cloud_connected is not False

    @property
    def is_closed(self) -> bool | None:
        zone = self._zone_data
        if zone is None or zone.get("open") is None:
            return None
        return not zone["open"]

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        attrs: dict[str, Any] = {}
        zone = self._zone_data
        if zone:
            dur = zone.get("duration_seconds")
            if dur is not None:
                attrs["duration_seconds"] = dur
            attrs["state_raw"] = zone.get("state_raw")
            # Naive local wall time, and only present on frames that carry it:
            # on every capture so far it is the moment this run ends, and it is
            # absent for an idle zone. Water usage is a sensor entity of its
            # own rather than an attribute here; only the timestamp, which has
            # no natural entity of its own, rides along on the valve.
            event_time = zone.get("event_time")
            if event_time is not None:
                attrs["event_time"] = event_time

        attrs.update(sub_device_attributes(self.coordinator, self._sensor_key))
        return attrs

    @property
    def device_info(self) -> DeviceInfo:
        return build_sub_device_info(self._sensor_info)

    # ------------------------------------------------------------------
    # Control
    # ------------------------------------------------------------------

    def _get_configured_duration_seconds(self) -> int:
        """Look up the companion duration number entity for this zone and convert
        its value (minutes) to seconds.  Falls back to DEFAULT_DURATION_SECONDS
        if the entity is not yet available."""
        hid = self._sensor_info["hid"]
        mid = self._sensor_info["mid"]
        addr = self._sensor_info["addr"]
        unique_id = f"rainpoint_{hid}_{mid}_{addr}_zone{self._zone_num}_duration"
        minutes = _duration_number_value(self.hass, unique_id)
        if minutes is None:
            _LOGGER.debug(
                "Duration entity for unique_id=%s not found, falling back to default %ss",
                unique_id,
                DEFAULT_DURATION_SECONDS,
            )
            return DEFAULT_DURATION_SECONDS
        return max(1, int(minutes * 60))

    def _apply_response_state(self, raw_state: str | None) -> None:
        """Decode the state string returned by controlWorkMode and inject it
        into the coordinator data immediately, bypassing the poll cycle.
        The API returns the post-command hub state, so this reflects the actual
        device state without waiting for the next poll."""
        if not raw_state:
            return
        model = self._sensor_info.get("model", "")
        if model in (MODEL_VALVE_113, MODEL_VALVE_145):
            decoded = decode_htv145frf(raw_state)
        elif model in (MODEL_VALVE_213, MODEL_VALVE_245, MODEL_VALVE_345, MODEL_VALVE_405):
            decoded = decode_htv213frf_valve(raw_state)
        else:
            decoded = decode_valve_hub(raw_state)
        if not decoded:
            return
        current = dict(self.coordinator.data)
        sensors = dict(current.get("sensors", {}))
        if self._sensor_key not in sensors:
            return
        entry = dict(sensors[self._sensor_key])
        entry["data"] = decoded
        sensors[self._sensor_key] = entry
        current["sensors"] = sensors
        self.coordinator.async_set_updated_data(current)

    def _record_successful_command(self) -> None:
        """Tell the coordinator this zone has command-fresh state."""
        self.coordinator.record_valve_command(self._sensor_key, self._zone_num)

    # ------------------------------------------------------------------
    async def async_open_valve(self, **kwargs: Any) -> None:
        duration = int(kwargs["duration"]) if "duration" in kwargs else self._get_configured_duration_seconds()
        mid = self._sensor_info["mid"]
        addr = self._sensor_info["addr"]
        device_name = self._sensor_info.get("device_name") or ""
        product_key = self._sensor_info.get("product_key") or ""

        _LOGGER.debug(
            "Opening valve mid=%s addr=%s zone=%s duration=%ss",
            mid,
            addr,
            self._zone_num,
            duration,
        )

        client = self.coordinator._client
        response_state = await client.control_work_mode(
            mid=mid,
            addr=addr,
            device_name=device_name,
            product_key=product_key,
            port=self._zone_num,
            mode=1,
            duration=duration,
        )
        self._record_successful_command()
        self._apply_response_state(response_state)

    async def async_close_valve(self, **kwargs: Any) -> None:
        mid = self._sensor_info["mid"]
        addr = self._sensor_info["addr"]
        device_name = self._sensor_info.get("device_name") or ""
        product_key = self._sensor_info.get("product_key") or ""

        _LOGGER.debug(
            "Closing valve mid=%s addr=%s zone=%s",
            mid,
            addr,
            self._zone_num,
        )

        client = self.coordinator._client
        response_state = await client.control_work_mode(
            mid=mid,
            addr=addr,
            device_name=device_name,
            product_key=product_key,
            port=self._zone_num,
            mode=0,
            duration=0,
        )
        self._record_successful_command()
        self._apply_response_state(response_state)


class RainPointDpValveEntity(RainPointValveEntity):
    """A valve zone commanded through the datapoint control endpoint.

    A Bluetooth-backed model such as the HTV210B commands through
    ``controlWorkModeDP`` rather than ``controlWorkMode``: a different URL, a
    request shape that carries the run duration as a little-endian hex
    ``param`` instead of a ``duration`` field, and a response blob that
    describes exactly one zone rather than the whole hub. Kept as a subclass
    rather than a branch inside the parent's command methods so the two
    framings can never leak into each other, mirroring how
    ``generic_control.py`` keeps its own write path separate rather than
    branching an existing one.

    ``__init__``, the unique_id shape, ``is_closed``,
    ``extra_state_attributes``, availability, and
    ``_get_configured_duration_seconds`` are all inherited unchanged, so the
    companion duration number entity keeps resolving this zone.
    """

    async def async_open_valve(self, **kwargs: Any) -> None:
        duration = int(kwargs["duration"]) if "duration" in kwargs else self._get_configured_duration_seconds()
        mid = self._sensor_info["mid"]
        addr = self._sensor_info["addr"]
        device_name = self._sensor_info.get("device_name") or ""
        product_key = self._sensor_info.get("product_key") or ""
        param = _encode_dp_duration_param(duration)

        _LOGGER.debug(
            "Opening DP valve mid=%s addr=%s zone=%s duration=%ss",
            mid,
            addr,
            self._zone_num,
            duration,
        )

        client = self.coordinator._client
        response_state = await client.control_work_mode_dp(
            mid=mid,
            addr=addr,
            device_name=device_name,
            product_key=product_key,
            port=self._zone_num,
            mode=1,
            param=param,
        )
        self._record_successful_command()
        self._apply_response_state(response_state)

    async def async_close_valve(self, **kwargs: Any) -> None:
        mid = self._sensor_info["mid"]
        addr = self._sensor_info["addr"]
        device_name = self._sensor_info.get("device_name") or ""
        product_key = self._sensor_info.get("product_key") or ""
        # Called through the encoder rather than hardcoded, so the open and
        # close paths cannot drift from each other's zero-duration encoding.
        param = _encode_dp_duration_param(0)

        _LOGGER.debug(
            "Closing DP valve mid=%s addr=%s zone=%s",
            mid,
            addr,
            self._zone_num,
        )

        client = self.coordinator._client
        response_state = await client.control_work_mode_dp(
            mid=mid,
            addr=addr,
            device_name=device_name,
            product_key=product_key,
            port=self._zone_num,
            mode=0,
            param=param,
        )
        self._record_successful_command()
        self._apply_response_state(response_state)

    def _apply_response_state(self, raw_state: str | None) -> None:
        """Decode the DP response blob and merge it into this zone only.

        Unlike the parent's wholesale replace, the DP response describes only
        the commanded zone: no battery, no signal, and -- on a portNumber-2
        device such as the HTV210B -- no sibling zone. Replacing
        ``entry["data"]`` wholesale would blank all three on every command,
        so this copies the existing decoded data and overwrites only
        ``zones[self._zone_num]``.
        """
        if not raw_state:
            return
        decoded_zone = decode_htv210b_dp_state(raw_state)
        if not decoded_zone:
            return
        current = dict(self.coordinator.data)
        sensors = dict(current.get("sensors", {}))
        if self._sensor_key not in sensors:
            return
        entry = dict(sensors[self._sensor_key])
        decoded_data = dict(entry.get("data") or {})
        zones = dict(decoded_data.get("zones") or {})
        zones[self._zone_num] = decoded_zone
        decoded_data["zones"] = zones
        entry["data"] = decoded_data
        sensors[self._sensor_key] = entry
        current["sensors"] = sensors
        self.coordinator.async_set_updated_data(current)


class RainPointHicStationValveEntity(CoordinatorEntity[RainPointCoordinator], ValveEntity):
    """One station on an HIC801W 8-station irrigation controller.

    A sibling of RainPointValveEntity rather than a subclass of it, and the
    reason is the data shape rather than taste. Every inherited member of that
    class reads ``decoded["zones"][zone]``: its state, its availability, its
    attributes and its optimistic write-back. The HIC801W carries no zones
    mapping at all. Its 279 accessory sends one aggregate record with a single
    ``current_station`` number, so a subclass would have to override all of
    them and inherit nothing but the constructor. generic_control.py keeps its
    own write path separate for the same reason, and RainPointDpValveEntity is
    a subclass precisely because it does share the zones shape.

    The eight entities are a fan-out this integration invents, not one the
    wire hands it -- the same fan-out binary_sensor.py already performs over
    the same reading. Those per-station binary sensors stay exactly where they
    are: they were built for a model whose control half was unproven, their
    unique IDs are persisted in users' registries, and deleting them would
    strand those rows rather than remove them. The HTV210B keeps its read-only
    per-zone state sensor beside its valve for the same reason.

    Only one station runs at a time, which is a property of the controller and
    not something this entity enforces: opening station 2 while station 1 runs
    sends the command and lets the controller answer, and the frame it answers
    with is what all eight entities then report.
    """

    _attr_should_poll = False
    _attr_has_entity_name = True
    _attr_reports_position = False
    _attr_supported_features = ValveEntityFeature.OPEN | ValveEntityFeature.CLOSE
    _attr_device_class = ValveDeviceClass.WATER

    def __init__(
        self,
        coordinator: RainPointCoordinator,
        sensor_key: str,
        sensor_info: dict,
        station_num: int,
    ) -> None:
        super().__init__(coordinator)
        self._sensor_key = sensor_key
        self._sensor_info = sensor_info
        self._station_num = station_num

        hid = sensor_info["hid"]
        mid = sensor_info["mid"]
        addr = sensor_info["addr"]

        # "station" rather than the "zone" the HTV valves use, matching the
        # vocabulary the rest of this model's entities already carry and the
        # word printed on the hardware. The shape is otherwise the
        # integration's own {hid}_{mid}_{addr} plus a suffix, and it is
        # persisted in Home Assistant's entity registry, so changing it later
        # needs a migration.
        self._attr_unique_id = f"rainpoint_{hid}_{mid}_{addr}_station{station_num}"
        # The bare label, never prefixed with a device name: Home Assistant's
        # device page strips an exact device-name prefix, and a device rename
        # breaks that match.
        self._attr_name = f"Station {station_num}"

    # ------------------------------------------------------------------
    # Coordinator data helpers
    # ------------------------------------------------------------------

    @property
    def _hic_data(self) -> dict | None:
        """Return this controller's decoded aggregate record, or None."""
        sensors = (self.coordinator.data or {}).get("sensors", {})
        info = sensors.get(self._sensor_key)
        if not info:
            return None
        return info.get("data") or None

    @property
    def _is_running_station(self) -> bool | None:
        """Return whether this station is the one currently running.

        The same reading, and the same three-way answer, as
        RainPointHicStationWateringBinarySensor.is_on. None means the frame is
        missing or did not parse, and it must not collapse to False: a valve
        reporting a confident closed on a frame that never parsed would be an
        invented state, and an automation cannot tell the two apart afterwards.

        A ``current_station`` outside the declared station range is treated the
        same way. The decoder's shape check rejects only on a non-zero b3 and
        does not itself exclude an out-of-range b0, so without this guard one
        corrupt byte would make all eight stations report a confident closed.
        """
        data = self._hic_data
        if not data:
            return None
        current_station = data.get("current_station")
        if not isinstance(current_station, int) or not (0 <= current_station <= HIC801W_STATION_COUNT):
            return None
        return current_station == self._station_num

    # ------------------------------------------------------------------
    # Entity properties
    # ------------------------------------------------------------------

    @property
    def available(self) -> bool:
        """Return whether this station can currently accept a command.

        Deliberately does not read ``hub_online`` the way the zone-shaped
        valves do. That flag is RF link state decoded out of a valve hub's own
        payload, and the HIC801W frame carries no counterpart, so requiring it
        would leave every station permanently unavailable.

        What is left is the cloud's own per-poll verdict on the hub, read with
        the same "is not False" gate the zone valves use: None means the
        cloud's connectivity is unknown and must leave availability alone, and
        only an explicit False means the cloud reported this hub as
        disconnected.

        A frame that failed its shape check leaves this True on purpose. The
        controller is reachable and still polling and it is the payload that
        did not parse, so unavailable would misreport the cause; the state
        reads unknown instead, through _is_running_station.
        """
        sensors = (self.coordinator.data or {}).get("sensors", {})
        info = sensors.get(self._sensor_key)
        if not info:
            return False
        decoded = info.get("data")
        if not decoded:
            return False
        if decoded.get("type") == SILENT_DATA_TYPE:
            return False
        cloud_connected = hub_connected_flag(hub_connectivity_record(self.coordinator, self._sensor_info.get("mid")))
        return cloud_connected is not False

    @property
    def is_closed(self) -> bool | None:
        running = self._is_running_station
        if running is None:
            return None
        return not running

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Carry the running run's own numbers, and only on the running station.

        The controller reports one run duration and one end time for the
        controller as a whole, not per station. Attaching them to all eight
        entities would report seven stations as having a run they are not
        having, so they ride only on the station the frame names as running.
        ``run_ends_at`` is a naive local wall-clock string and is absent from
        an idle frame. Merge order mirrors RainPointValveEntity: entity keys
        first, sub_device_attributes layered on top last.
        """
        attrs: dict[str, Any] = {}
        if self._is_running_station is True:
            data = self._hic_data or {}
            run_duration = data.get("run_duration_seconds")
            if run_duration is not None:
                attrs["duration_seconds"] = run_duration
            run_ends_at = data.get("run_ends_at")
            if run_ends_at is not None:
                attrs["run_ends_at"] = run_ends_at

        attrs.update(sub_device_attributes(self.coordinator, self._sensor_key))
        return attrs

    @property
    def device_info(self) -> DeviceInfo:
        return build_sub_device_info(self._sensor_info)

    # ------------------------------------------------------------------
    # Control
    # ------------------------------------------------------------------

    def _get_configured_duration_minutes(self) -> int:
        """Return this station's companion duration setpoint, in minutes.

        No conversion: the companion number entity is already in minutes and
        so is the field this controller reads, so this is the one valve family
        where the setpoint reaches the wire unscaled.
        """
        hid = self._sensor_info["hid"]
        mid = self._sensor_info["mid"]
        addr = self._sensor_info["addr"]
        unique_id = f"rainpoint_{hid}_{mid}_{addr}_station{self._station_num}_duration"
        minutes = _duration_number_value(self.hass, unique_id)
        if minutes is None:
            _LOGGER.debug(
                "Duration entity for unique_id=%s not found, falling back to default %s minutes",
                unique_id,
                DEFAULT_HIC_DURATION_MINUTES,
            )
            return DEFAULT_HIC_DURATION_MINUTES
        return max(1, int(minutes))

    def _wire_duration_minutes(self, kwargs: dict) -> int:
        """Resolve the run length to send, in the minutes this controller reads.

        The ``duration`` a service call may carry stays in seconds, because
        that is what it means on every other valve this integration exposes
        and one service should not change units with the model behind it. Only
        the wire differs, so the conversion happens here, at the boundary, and
        nowhere else.

        Rounded to the nearest minute rather than truncated, so a 90-second
        request runs for two minutes instead of one, and floored at one minute
        because this controller has no way to express a shorter run and a
        request for 20 seconds must not silently become a request for none.
        """
        if "duration" in kwargs:
            return max(1, round(int(kwargs["duration"]) / 60))
        return self._get_configured_duration_minutes()

    def _apply_response_state(self, raw_state: str | None) -> None:
        """Decode the command's own response frame straight into coordinator data.

        controlWorkMode answers with the controller's post-command status
        frame, so this reflects what the controller actually did without
        waiting up to a poll interval for it. Replacing ``entry["data"]``
        wholesale is right here, unlike the DP path's per-zone merge: this
        frame is the whole aggregate record and describes every station at
        once.

        A frame that did not parse is dropped rather than written. The decoder
        answers a fully-populated error envelope rather than None on that
        path, so the guard is the envelope's own error key: writing it would
        replace a good reading with an empty one and blank all eight stations
        on a command that may well have worked.
        """
        if not raw_state:
            return
        decoded = decode_hic801w(raw_state)
        if not decoded or decoded.get("error") is not None:
            return
        current = dict(self.coordinator.data)
        sensors = dict(current.get("sensors", {}))
        if self._sensor_key not in sensors:
            return
        entry = dict(sensors[self._sensor_key])
        entry["data"] = decoded
        sensors[self._sensor_key] = entry
        current["sensors"] = sensors
        self.coordinator.async_set_updated_data(current)

    def _record_successful_command(self) -> None:
        """Tell the coordinator this controller has command-fresh state.

        Recorded against this station's own number, the same way a zone valve
        records against its zone, even though the guard it feeds preserves the
        whole aggregate record: what the coordinator needs is the moment of the
        most recent command against this key, and keying per station keeps that
        moment attributable to the station that caused it.
        """
        self.coordinator.record_valve_command(self._sensor_key, self._station_num)

    # ------------------------------------------------------------------
    async def async_open_valve(self, **kwargs: Any) -> None:
        minutes = self._wire_duration_minutes(kwargs)
        mid = self._sensor_info["mid"]
        addr = self._sensor_info["addr"]
        device_name = self._sensor_info.get("device_name") or ""
        product_key = self._sensor_info.get("product_key") or ""

        _LOGGER.debug(
            "Starting HIC801W station mid=%s addr=%s station=%s duration=%s minutes",
            mid,
            addr,
            self._station_num,
            minutes,
        )

        client = self.coordinator._client
        response_state = await client.control_work_mode(
            mid=mid,
            addr=addr,
            device_name=device_name,
            product_key=product_key,
            port=self._station_num,
            mode=1,
            duration=minutes,
        )
        self._record_successful_command()
        self._apply_response_state(response_state)

    async def async_close_valve(self, **kwargs: Any) -> None:
        mid = self._sensor_info["mid"]
        addr = self._sensor_info["addr"]
        device_name = self._sensor_info.get("device_name") or ""
        product_key = self._sensor_info.get("product_key") or ""

        _LOGGER.debug(
            "Stopping HIC801W station mid=%s addr=%s station=%s",
            mid,
            addr,
            self._station_num,
        )

        client = self.coordinator._client
        response_state = await client.control_work_mode(
            mid=mid,
            addr=addr,
            device_name=device_name,
            product_key=product_key,
            port=self._station_num,
            mode=0,
            duration=0,
        )
        self._record_successful_command()
        self._apply_response_state(response_state)
