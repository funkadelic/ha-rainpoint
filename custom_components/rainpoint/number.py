from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfTime
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    CONF_GENERIC_CONTROL_ENABLED,
    DOMAIN,
    GENERIC_CONTROL_DURATION_SUFFIX,
    GENERIC_CONTROL_MARKER_ICON,
    GENERIC_CONTROL_UNIQUE_ID_MARKER,
    UNIQUE_ID_PREFIX,
    VALVE_MODELS,
)
from .coordinator import SILENT_DATA_TYPE, RainPointCoordinator
from .device import build_sub_device_info
from .entity import LateEntityAdder, register_late_adder, sub_device_attributes

_LOGGER = logging.getLogger(__name__)

DURATION_MIN_MINUTES = 1
DURATION_MAX_MINUTES = 60
DURATION_STEP_MINUTES = 1
DURATION_DEFAULT_MINUTES = 10


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    data = hass.data[DOMAIN][entry.entry_id]
    coordinator: RainPointCoordinator = data["coordinator"]

    sensors_cfg = {key: info for key, info in coordinator.data.get("sensors", {}).items() if isinstance(info, dict)}
    generic_enabled = entry.options.get(CONF_GENERIC_CONTROL_ENABLED, False)

    def build(key: str, info: dict) -> list:
        """Return every duration entity one sensor key currently supports.

        Widened from list[RainPointZoneDurationNumber]: the opt-in
        generic-control branch extends this with
        RainPointGenericZoneDurationNumber instances, mirroring how valve.py
        and switch.py widen their own entity lists for the same reason.
        """
        built: list = []
        if info.get("model") in VALVE_MODELS:
            decoded = info.get("data") or {}
            # A Bluetooth-only unit is enumerated by the cloud and reaches the
            # sensors dict as a debounced silent entry whose model field is
            # filled from the sub-device record, so the model-set check above
            # alone would admit it. The silent type is the discriminator: a
            # device the integration cannot currently reach is never offered a
            # control. The absence of a zones key would also block creation
            # today, but this guard states the invariant rather than resting
            # on that data shape.
            if decoded.get("type") != SILENT_DATA_TYPE:
                zones: dict = decoded.get("zones", {})
                for zone_num in sorted(zones.keys()):
                    built.append(RainPointZoneDurationNumber(coordinator, key, info, zone_num))
                    _LOGGER.debug("Creating duration number entity: key=%s zone=%s", key, zone_num)

        if generic_enabled:
            base_slug = f"{info.get('hid', '')}_{info.get('mid', '')}_{info.get('addr', '')}"
            built.extend(build_generic_duration_entities(coordinator, key, info, base_slug))
        return built

    # The literal the PLATFORMS list and every entity_id prefix already use.
    adder = LateEntityAdder(coordinator, async_add_entities, build, "number")
    # Published before anything is emitted, so the removal sweep can ask this
    # adder what it created for a key that later vanishes.
    register_late_adder(data, adder)

    entities: list = []
    for key, info in sensors_cfg.items():
        entities.extend(adder.collect(key, info))

    if entities:
        async_add_entities(entities)

    # Registered unconditionally, for the same reason valve.py does: a valve
    # that reports no zones at setup produces nothing here, and its duration
    # companions have to appear alongside the valve entities they configure.
    entry.async_on_unload(coordinator.async_add_listener(adder.async_on_coordinator_update))


class _RainPointDurationNumberBase(CoordinatorEntity[RainPointCoordinator], NumberEntity, RestoreEntity):
    """Shared restore, value, attribute, and device-page behaviour for a duration entity.

    The two concrete classes below differ only in their constructors and class
    attributes: one is built from a zone number, the other from a catalog
    datapoint. Everything here was duplicated between them verbatim.
    """

    _attr_has_entity_name = True
    _attr_native_min_value = DURATION_MIN_MINUTES
    _attr_native_max_value = DURATION_MAX_MINUTES
    _attr_native_step = DURATION_STEP_MINUTES
    _attr_native_unit_of_measurement = UnitOfTime.MINUTES
    _attr_mode = NumberMode.BOX

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last_state = await self.async_get_last_state()
        if last_state is not None:
            try:
                restored = float(last_state.state)
                if DURATION_MIN_MINUTES <= restored <= DURATION_MAX_MINUTES:
                    self._current_value = restored
                    _LOGGER.debug(
                        "Restored duration for %s: %s min",
                        self._attr_unique_id,
                        restored,
                    )
            except (ValueError, TypeError):
                pass

    @property
    def native_value(self) -> float:
        return self._current_value

    @property
    def _run_state_open(self) -> bool | None:
        """Report whether this entity's own zone is confirmed explicitly open.

        None means the family cannot currently confirm its own run state --
        no data yet, a silent device, or a reading whose meaning is not
        settled. That is deliberately the fail-open reading: a setpoint edit
        never starts water, so there is no safety argument for refusing on an
        unconfirmed state, and a family that cannot answer must accept writes
        rather than have its duration permanently locked. A concrete class
        overrides this with its own confirmed reading; this base default is
        what a future duration family inherits until it does.
        """
        return None

    @property
    def _zone_label(self) -> str:
        """Name the subject of the refusal message.

        Supplies the noun phrase async_set_native_value builds its message
        around. The base default names no number; a family carrying a zone
        or port number overrides it with one.
        """
        return "The zone"

    @property
    def _open_run_attributes(self) -> dict[str, Any]:
        """Return the running run's own numbers, contributed only while open.

        The base's extra_state_attributes evaluates the open gate once and
        merges this only when it reads True, so this hook never re-checks
        openness itself. A family whose data carries no such numbers
        contributes nothing rather than inventing a substitute -- the base
        default here is that empty contribution.
        """
        return {}

    async def async_set_native_value(self, value: float) -> None:
        """Apply a new duration, refusing while the entity's own zone is open.

        Reads the zone-open hook exactly once and compares it with ``is
        True``, never with truthiness: None, a missing zone record, a silent
        device and a stale reading must all accept the write, mirroring
        valve.py's own availability gate. On an explicit open the method
        raises before touching the stored value or writing state, so the
        displayed value visibly does not move. A reading that is not an
        explicit open accepts the write exactly as before this guard
        existed. The accepted cost is that editing the setpoint for the next
        run while the current one waters is blocked too; the user must wait
        for the run to end.

        The decision behind this guard, recorded here in full rather than
        only in a working file, because the deliverable this guard exists
        to satisfy is the record itself and not only the behaviour a test
        can prove on its own.

        This entity is a setpoint for the next run and has never been a
        live control of the run already in progress, so refusing a mid-run
        edit with an explanation is the honest answer rather than an
        obstructive one. The failure this guard replaces was silent in both
        directions: the old code accepted the new number, wrote it to
        state, and did nothing to the running zone, leaving the person who
        made the change with no way to tell that it had no effect. Raising
        before any state mutation fixes both halves at once, because the
        displayed value never moves and the raise itself is Home
        Assistant's own signal that the write did not happen.

        Three other answers were weighed and rejected. Re-commanding the
        running zone with the new value, so the entity becomes a live
        control instead of a setpoint, was rejected because it cannot be
        built honestly today: nobody has probed what an open command does
        to a zone that is already open, so whether it restarts the run
        from zero or merely adjusts the time remaining is unknown; the
        hub-paired endpoint addresses a run by an absolute end time rather
        than a duration, so a mid-run value would be ambiguous between
        ending the run one minute from now and declaring that the run
        should have been one minute long and stopping it now; and the box
        input mode this entity uses commits on every keystroke, so editing
        a setpoint by typing would turn into an unbounded stream of valve
        writes instead of one command. Accepting the write and marking the
        entity stale, so the setpoint could still be edited for a later run
        while the current one keeps going, was rejected because Home
        Assistant has no warning-on-success affordance for a number write:
        a person would learn the change did not take only by opening an
        attribute panel, which answers less than the toast this guard
        raises instead. Marking the entity unavailable for the length of
        the run was rejected because unavailable is Home Assistant's own
        word for a device that is broken, and using it here would report a
        working entity as faulty for as long as it waters.

        The cost knowingly accepted is that this guard also blocks the
        legitimate case of setting up the next run's duration while the
        current one is still watering; there is no way today to tell that
        intent apart from an attempt to change the run in progress, so the
        user must wait for the run to end before the setpoint can move
        again. Re-commanding the running zone would revive as an option if
        a hardware probe settles what an open command does to an
        already-open zone and a meaning is decided for a mid-run duration
        against an absolute end time; until both of those are answered,
        refusing remains the only answer this integration can build
        honestly.
        """
        if self._run_state_open is True:
            raise HomeAssistantError(
                f"{self._zone_label} is watering. The run duration can only be changed while the zone is closed. "
                "The new value would apply to the next run."
            )
        self._current_value = value
        self.async_write_ha_state()

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Merge the running run's own numbers in, only while the zone is open.

        The open gate lives here rather than in _open_run_attributes itself,
        evaluated once against the same _run_state_open hook
        async_set_native_value reads, so the refusal and its explanation
        read one signal: an entity can never refuse a write without also
        carrying the numbers that explain the refusal, and an idle zone's
        stale last-run values never accumulate on a setpoint entity. Merge
        order mirrors valve.py's own extra_state_attributes: entity-specific
        keys first, sub_device_attributes layered on top last.
        """
        attrs: dict[str, Any] = {}
        if self._run_state_open is True:
            attrs.update(self._open_run_attributes)
        attrs.update(sub_device_attributes(self.coordinator, self._sensor_key))
        return attrs

    @property
    def device_info(self) -> DeviceInfo:
        return build_sub_device_info(self._sensor_info)


class RainPointZoneDurationNumber(_RainPointDurationNumberBase):
    """Configurable run duration (in minutes) for a single irrigation zone.

    The value is restored on HA restart via RestoreEntity.  When a valve is
    opened without an explicit duration override in the service call data,
    valve.py reads this entity's current value and converts it to seconds.
    """

    _attr_icon = "mdi:timer-outline"

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
        self._current_value: float = DURATION_DEFAULT_MINUTES

        hid = sensor_info["hid"]
        mid = sensor_info["mid"]
        addr = sensor_info["addr"]

        self._attr_unique_id = f"rainpoint_{hid}_{mid}_{addr}_zone{zone_num}_duration"
        self._attr_name = f"Zone {zone_num} Duration"

    @property
    def _zone_data(self) -> dict | None:
        """Return this entity's own zone dict, or None.

        An identically-shaped copy of RainPointValveEntity._zone_data in
        valve.py, kept as a copy rather than a shared call so this change
        never touches that live control path.
        """
        sensors = self.coordinator.data.get("sensors", {})
        info = sensors.get(self._sensor_key)
        if not info:
            return None
        decoded = info.get("data")
        if not decoded:
            return None
        return decoded.get("zones", {}).get(self._zone_num)

    @property
    def _run_state_open(self) -> bool | None:
        """Read this zone's own explicit open/closed/unknown reading.

        Returns the raw tri-state ``open`` value rather than valve.py's
        ``is_closed``, whose inverted truthiness would collapse None into a
        closed reading. A zone whose record is entirely absent reads as
        unknown, the same as one whose ``open`` flag is None.
        """
        zone = self._zone_data
        if zone is None:
            return None
        return zone.get("open")

    @property
    def _zone_label(self) -> str:
        """Name this entity's own zone number in the refusal message."""
        return f"Zone {self._zone_num}"

    @property
    def _open_run_attributes(self) -> dict[str, Any]:
        """Carry the running run's own duration and event time, only while open.

        The base has already established the zone is open before this is
        read, so this hook never re-checks that itself. Each value is
        guarded with ``is not None`` so a missing reading omits its key
        rather than rendering a null, mirroring valve.py's own guards. The
        raw status byte the sibling valve entity also carries is
        deliberately not repeated here: the decision names only these two
        values. ``event_time`` is a naive local wall-clock string, absent on
        some frames, which is why it lives here as an attribute rather than
        in the refusal message.
        """
        attrs: dict[str, Any] = {}
        zone = self._zone_data or {}
        duration = zone.get("duration_seconds")
        if duration is not None:
            attrs["duration_seconds"] = duration
        event_time = zone.get("event_time")
        if event_time is not None:
            attrs["event_time"] = event_time
        return attrs


def build_generic_duration_entities(coordinator, sensor_key: str, sensor_info: dict, base_slug: str) -> list:
    """Return the companion duration entities for one sub-device's generic valve zones, or [].

    Deferred import: generic_control reaches sensor.py's RainPointSensorBase
    transitively through generic_entities, so a top-level import here would
    pull the whole sensor platform into this module's import graph.

    Reuses generic_control's own shared gate-evaluation body
    (_build_generic_entities) rather than re-deriving eligibility, so this
    companion set can never disagree with the generic valve entity set it
    companions -- one gate evaluation, projected through this module's own
    entity class instead of the valve's.
    """
    from .generic_control import VALVE_CONTROL_IDENTITIES, _build_generic_entities

    return _build_generic_entities(
        coordinator, sensor_key, sensor_info, base_slug, VALVE_CONTROL_IDENTITIES, RainPointGenericZoneDurationNumber
    )


class RainPointGenericZoneDurationNumber(_RainPointDurationNumberBase):
    """Companion run-duration entity for one generic control valve zone.

    Shares the base class above with RainPointZoneDurationNumber: same bounds,
    same default, same restore-on-add behaviour, same extra attributes, same
    device_info construction. The differences are confined to identity and
    presentation -- constructed from the sensor key, sensor info, base slug
    and the resolved control datapoint rather than a bare zone number; its
    unique_id is the control entity's own unique_id shape plus the locked
    duration suffix; its display name carries the identity label and the
    same provisional marker every other generic entity carries; and its icon
    is the generic marker icon rather than a plain timer icon, so the
    provisional marking stays consistent across all four generic namespaces.

    generic_control.RainPointGenericValve._get_configured_duration_seconds
    resolves this entity by unique_id through the entity registry, exactly
    the way the trusted valve resolves its own duration companion.
    """

    def __init__(
        self,
        coordinator: RainPointCoordinator,
        sensor_key: str,
        sensor_info: dict,
        base_slug: str,
        datapoint: Any,
        port_number: int | None,
    ) -> None:
        super().__init__(coordinator)
        self._sensor_key = sensor_key
        self._sensor_info = sensor_info
        self._base_slug = base_slug
        self._datapoint = datapoint
        self._current_value: float = DURATION_DEFAULT_MINUTES

        identity = datapoint.identity
        self._attr_unique_id = (
            f"{UNIQUE_ID_PREFIX}{base_slug}{GENERIC_CONTROL_UNIQUE_ID_MARKER}"
            f"{identity.lower()}_p{datapoint.dp_port}{GENERIC_CONTROL_DURATION_SUFFIX}"
        )

        zone = ""
        if port_number is not None and port_number > 1 and datapoint.dp_port >= 1:
            zone = f"Zone {datapoint.dp_port} "
        self._attr_name = f"{zone}{identity} Duration (unverified)"

        # Assigned last so the marker always wins over any domain default icon.
        self._attr_icon = GENERIC_CONTROL_MARKER_ICON

    @property
    def _run_state_open(self) -> bool | None:
        """Read this port's own explicit open/closed/unknown reading.

        Calls generic_control.generic_run_state_open, the same body the
        companion generic valve's own run-state property now calls, so this
        entity and its valve can never disagree about whether a port is
        open. Imported inside the property body rather than at module level:
        generic_control reaches sensor.py's RainPointSensorBase transitively
        through generic_entities, so a top-level import here would pull the
        whole sensor platform into this module's import graph -- the same
        reason and the same shape build_generic_duration_entities already
        uses for its own deferred import.
        """
        from .generic_control import generic_run_state_open

        return generic_run_state_open(self.coordinator, self._sensor_key, self._datapoint.dp_port)

    @property
    def _zone_label(self) -> str:
        """Name this entity's own datapoint port in the refusal message."""
        return f"Zone {self._datapoint.dp_port}"
