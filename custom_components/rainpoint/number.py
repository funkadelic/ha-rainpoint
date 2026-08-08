from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfTime
from homeassistant.core import HomeAssistant
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
    _device_name_prefix = "Valve Hub"

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

    async def async_set_native_value(self, value: float) -> None:
        self._current_value = value
        self.async_write_ha_state()

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return sub_device_attributes(self.coordinator, self._sensor_key)

    @property
    def device_info(self) -> DeviceInfo:
        return build_sub_device_info(
            self._sensor_info,
            name_fallback=f"{self._device_name_prefix} {self._sensor_info['addr']}",
        )


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

    _device_name_prefix = "Device"

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
