from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.valve import (
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
    decode_htv145frf,
    decode_htv210b_dp_state,
    decode_htv213frf_valve,
    decode_valve_hub,
    has_bluetooth_control_identity,
)
from .const import (
    CONF_GENERIC_CONTROL_ENABLED,
    DOMAIN,
    MODEL_VALVE_113,
    MODEL_VALVE_145,
    MODEL_VALVE_213,
    MODEL_VALVE_245,
    MODEL_VALVE_345,
    MODEL_VALVE_405,
    VALVE_MODELS,
)
from .coordinator import RainPointCoordinator, hub_connected_flag, hub_connectivity_record
from .device import build_sub_device_info
from .entity import LateEntityAdder, register_late_adder, sub_device_attributes

_LOGGER = logging.getLogger(__name__)

# Default run duration used when HA opens a valve without an explicit duration.
# Users can override by calling the valve.open_valve service with a duration attr.
DEFAULT_DURATION_SECONDS = 600  # 10 minutes


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
        built: list = []
        if info.get("model") in VALVE_MODELS:
            zones: dict = (info.get("data") or {}).get("zones", {})
            # Endpoint selection is a function of the committed catalog's
            # datapoint identity, never the model itself: a variant declaring
            # the Bluetooth-backed control identity commands through
            # RainPointDpValveEntity, every other admitted model through the
            # RF RainPointValveEntity.
            entity_cls = (
                RainPointDpValveEntity
                if has_bluetooth_control_identity(info.get("model"), info.get("model_code"))
                else RainPointValveEntity
            )
            # One entity per zone that reported in the payload. Zones absent
            # from the payload are not created, which avoids phantom entities
            # when a device reports fewer zones than its model name implies.
            for zone_num in sorted(zones.keys()):
                built.append(entity_cls(coordinator, key, info, zone_num))
                _LOGGER.debug(
                    "Creating valve entity: key=%s zone=%s model=%s class=%s",
                    key,
                    zone_num,
                    info.get("model"),
                    entity_cls.__name__,
                )

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
        sub_name = sensor_info.get("sub_name") or f"Valve Hub {addr}"

        self._attr_unique_id = f"rainpoint_{hid}_{mid}_{addr}_zone{zone_num}"
        self._attr_name = f"{sub_name} Zone {zone_num}"

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
        return build_sub_device_info(self._sensor_info, name_fallback=f"Valve Hub {self._sensor_info['addr']}")

    # ------------------------------------------------------------------
    # Control
    # ------------------------------------------------------------------

    def _get_configured_duration_seconds(self) -> int:
        """Look up the companion duration number entity for this zone and convert
        its value (minutes) to seconds.  Falls back to DEFAULT_DURATION_SECONDS
        if the entity is not yet available.

        Uses the entity registry to resolve unique_id -> entity_id so the lookup
        is not sensitive to HA auto-generated entity_id naming."""
        from homeassistant.helpers import entity_registry as er

        hid = self._sensor_info["hid"]
        mid = self._sensor_info["mid"]
        addr = self._sensor_info["addr"]
        unique_id = f"rainpoint_{hid}_{mid}_{addr}_zone{self._zone_num}_duration"
        registry = er.async_get(self.hass)
        entity_id = registry.async_get_entity_id("number", "rainpoint", unique_id)
        if entity_id:
            state = self.hass.states.get(entity_id)
            if state is not None:
                try:
                    minutes = float(state.state)
                    return max(1, int(minutes * 60))
                except (ValueError, TypeError):
                    pass
        _LOGGER.debug(
            "Duration entity for unique_id=%s not found, falling back to default %ss",
            unique_id,
            DEFAULT_DURATION_SECONDS,
        )
        return DEFAULT_DURATION_SECONDS

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
