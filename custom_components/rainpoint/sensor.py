from __future__ import annotations

import logging
import re
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any, ClassVar

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory, UnitOfVolume
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .api import _USAGE_GALLONS_PER_COUNT
from .const import (
    CONF_GENERIC_ENTITIES_ENABLED,
    DOMAIN,
    MODEL_CO2,
    MODEL_DISPLAY_HUB,
    MODEL_FLOWMETER,
    # New HCS sensor models
    MODEL_HCS005FRF,
    MODEL_HCS015ARF,
    MODEL_HCS024FRF_V1,
    MODEL_HTV210B,
    MODEL_MOISTURE_FULL,
    MODEL_MOISTURE_SIMPLE,
    MODEL_POOL,
    MODEL_POOL_PLUS,
    MODEL_RAIN,
    MODEL_TEMPHUM,
    MODEL_VALVE_213,
    MODEL_VALVE_245,
    MODEL_VALVE_345,
    MODEL_VALVE_405,
)
from .coordinator import (
    NO_STATUS_PAYLOAD_MARKER,
    SILENT_DATA_TYPE,
    RainPointCoordinator,
    _build_new_device_issue_url,
    is_hub_record,
)
from .diagnostic_sensors import (
    RainPointBatterySensor,
    RainPointFirmwareVersionSensor,
    RainPointLastUpdatedSensor,
    RainPointRSSISensor,
)
from .entity import RainPointSubDeviceEntity, sub_device_attributes
from .hub_entities import (
    RainPointHubDeviceIDSensor,
    RainPointHubFirmwareSensor,
    RainPointHubMACSensor,
    RainPointHubRSSISensor,
    RainPointPushLastMessageSensor,
    resolve_push_diagnostic_hubs,
)

_LOGGER = logging.getLogger(__name__)

# HCS device variants that share an entity layout with one of the canonical
# RainPoint sensor models. Resolving through this map lets the dispatch chain
# below stay flat: each variant is rebound to its base model before the if/elif
# runs, so we don't repeat identical entity-creation blocks per variant.
_SENSOR_MODEL_ALIASES: dict[str, str] = {
    MODEL_HCS015ARF: MODEL_POOL,
}


def _slugify(text: str) -> str:
    text = text.lower()
    text = re.sub(r"[^a-z0-9]+", "_", text)
    text = re.sub(r"_+", "_", text)
    return text.strip("_")


def _make_display_hub_entities(coordinator, key, info, base_slug):
    data = info.get("data", {})
    readings = data.get("readings", {}) if data else {}
    return [DisplayHubReadingSensor(coordinator, key, info, base_slug, reading_key) for reading_key in readings]


def _make_diagnostic_entities(coordinator, key, info, base_slug):
    """Generic RSSI / battery / firmware / last-updated diagnostic set."""
    return [
        RainPointRSSISensor(coordinator, key, info, base_slug),
        RainPointBatterySensor(coordinator, key, info, base_slug),
        RainPointFirmwareVersionSensor(coordinator, key, info, base_slug),
        RainPointLastUpdatedSensor(coordinator, key, info, base_slug),
    ]


def _make_moisture_simple_entities(coordinator, key, info, base_slug):
    return [
        RainPointMoisturePercentSensor(coordinator, key, info, base_slug, simple=True),
        *_make_diagnostic_entities(coordinator, key, info, base_slug),
    ]


def _make_moisture_full_entities(coordinator, key, info, base_slug):
    return [
        RainPointMoisturePercentSensor(coordinator, key, info, base_slug, simple=False),
        RainPointTemperatureSensor(coordinator, key, info, base_slug),
        RainPointIlluminanceSensor(coordinator, key, info, base_slug),
        *_make_diagnostic_entities(coordinator, key, info, base_slug),
    ]


def _make_rain_entities(coordinator, key, info, base_slug):
    rain_specs = (
        ("rain_last_hour_mm", "rain last hour"),
        ("rain_last_24h_mm", "rain last 24h"),
        ("rain_last_7d_mm", "rain last 7d"),
        ("rain_total_mm", "rain total"),
    )
    return [RainPointRainSensor(coordinator, key, info, base_slug, data_key, label) for data_key, label in rain_specs]


def _make_temphum_entities(coordinator, key, info, base_slug):
    return [
        RainPointTempHumCurrentSensor(coordinator, key, info, base_slug),
        RainPointTempHumHighSensor(coordinator, key, info, base_slug),
        RainPointTempHumLowSensor(coordinator, key, info, base_slug),
        RainPointTempHumHumidityCurrentSensor(coordinator, key, info, base_slug),
        RainPointTempHumHumidityHighSensor(coordinator, key, info, base_slug),
        RainPointTempHumHumidityLowSensor(coordinator, key, info, base_slug),
    ]


def _make_flowmeter_entities(coordinator, key, info, base_slug):
    return [
        RainPointFlowCurrentUsedSensor(coordinator, key, info, base_slug),
        RainPointFlowCurrentDurationSensor(coordinator, key, info, base_slug),
        RainPointFlowLastUsedSensor(coordinator, key, info, base_slug),
        RainPointFlowLastUsedDurationSensor(coordinator, key, info, base_slug),
        RainPointFlowTotalTodaySensor(coordinator, key, info, base_slug),
        RainPointFlowTotalSensor(coordinator, key, info, base_slug),
        RainPointFlowBatterySensor(coordinator, key, info, base_slug),
    ]


def _make_co2_entities(coordinator, key, info, base_slug):
    return [
        RainPointCO2Sensor(coordinator, key, info, base_slug),
        RainPointCO2LowSensor(coordinator, key, info, base_slug),
        RainPointCO2HighSensor(coordinator, key, info, base_slug),
        RainPointCO2TempSensor(coordinator, key, info, base_slug),
        RainPointCO2HumiditySensor(coordinator, key, info, base_slug),
        RainPointCO2BatterySensor(coordinator, key, info, base_slug),
    ]


def _make_pool_entities(coordinator, key, info, base_slug):
    return [
        RainPointPoolCurrentTempSensor(coordinator, key, info, base_slug),
        RainPointPoolHighTempSensor(coordinator, key, info, base_slug),
        RainPointPoolLowTempSensor(coordinator, key, info, base_slug),
        RainPointPoolBatterySensor(coordinator, key, info, base_slug),
    ]


def _make_pool_plus_entities(coordinator, key, info, base_slug):
    return [
        RainPointPoolPlusPoolCurrentTempSensor(coordinator, key, info, base_slug),
        RainPointPoolPlusPoolHighTempSensor(coordinator, key, info, base_slug),
        RainPointPoolPlusPoolLowTempSensor(coordinator, key, info, base_slug),
        RainPointPoolPlusAmbientCurrentTempSensor(coordinator, key, info, base_slug),
        RainPointPoolPlusAmbientHighTempSensor(coordinator, key, info, base_slug),
        RainPointPoolPlusAmbientLowTempSensor(coordinator, key, info, base_slug),
        RainPointPoolPlusHumidityCurrentSensor(coordinator, key, info, base_slug),
        RainPointPoolPlusHumidityHighSensor(coordinator, key, info, base_slug),
        RainPointPoolPlusHumidityLowSensor(coordinator, key, info, base_slug),
    ]


def _make_htv_valve_diagnostic_entities(coordinator, key, info, base_slug):
    """Battery, signal, and per-zone water usage for the HTV213/245/345/405 valve family.

    All four models share decode_htv213frf_valve and declare the same catalog
    identities, differing only in port count, so they share this factory too.

    Zone control lives on the valve/number platforms; the sensor platform
    surfaces the battery status word and RSSI these hubs carry in their status
    frame, plus one water-usage entity per zone the frame actually reports.
    The decoder leaves battery_percent/rssi_dbm absent when the frame lacks
    them, so the entities read unknown rather than a false value.

    Zones come from the decoded payload rather than the model name, mirroring
    valve.py and number.py, so a hub that reports fewer zones than its model
    implies grows no phantom entities. A zone the frame reports but carries no
    usage record for still gets its entity, reading unknown: the only captured
    HTV405FRF frame omits the usage records entirely, and an entity that reads
    unknown states that plainly, where a missing entity would look like the
    zone itself was not reported.
    """
    entities = [
        RainPointBatterySensor(coordinator, key, info, base_slug),
        RainPointRSSISensor(coordinator, key, info, base_slug),
    ]
    zones = (info.get("data") or {}).get("zones")
    if isinstance(zones, dict):
        entities.extend(RainPointZoneWaterUsageSensor(coordinator, key, info, base_slug, zone_num) for zone_num in sorted(zones))
    return entities


def _make_htv210b_entities(coordinator, key, info, base_slug):
    """Battery, signal, and per-zone state for the HTV210B Bluetooth valve.

    Hub-paired, this valve reports the same status-frame family as the HTV213
    group, but it gets no valve or number entities yet: its cloud control path
    is unproven, so the open/closed reading surfaces as a read-only sensor
    instead of a valve that could not honestly offer open/close. No per-zone
    water-usage entities either; the device has no flow meter, and its usage
    records read zero on every capture.

    Zones come from the decoded payload rather than the model name, mirroring
    the HTV213 factory, so only zones the frame reports grow entities.
    """
    entities = [
        RainPointBatterySensor(coordinator, key, info, base_slug),
        RainPointRSSISensor(coordinator, key, info, base_slug),
    ]
    zones = (info.get("data") or {}).get("zones")
    if isinstance(zones, dict):
        entities.extend(RainPointZoneStateSensor(coordinator, key, info, base_slug, zone_num) for zone_num in sorted(zones))
    return entities


def _make_hcs_moisture_only_entities(coordinator, key, info, base_slug):
    return [RainPointMoisturePercentSensor(coordinator, key, info, base_slug, simple=True)]


def _make_hcs_multisensor_entities(coordinator, key, info, base_slug):
    """Multi-sensor (moisture + temperature + illuminance).

    Distinct from MODEL_MOISTURE_FULL: this group does not emit the generic
    RSSI, battery, firmware, and last-updated diagnostic entities.
    """
    return [
        RainPointMoisturePercentSensor(coordinator, key, info, base_slug, simple=False),
        RainPointTemperatureSensor(coordinator, key, info, base_slug),
        RainPointIlluminanceSensor(coordinator, key, info, base_slug),
    ]


def _make_unknown_entities(coordinator, key, info, base_slug):
    """Fallback: only emit a diagnostic entity when the decoder flagged the model unknown."""
    data = info.get("data", {})
    if data and data.get("type") == "unknown":
        return [RainPointUnknownSensor(coordinator, key, info, base_slug)]
    return []


# Maps canonical sensor model to a factory that yields its entity list.
# Aliased models (see _SENSOR_MODEL_ALIASES) are resolved to their canonical
# model before lookup, so they share factories with their base model.
_MODEL_FACTORIES: dict[str, Callable[..., list]] = {
    MODEL_DISPLAY_HUB: _make_display_hub_entities,
    MODEL_MOISTURE_SIMPLE: _make_moisture_simple_entities,
    MODEL_MOISTURE_FULL: _make_moisture_full_entities,
    MODEL_RAIN: _make_rain_entities,
    MODEL_TEMPHUM: _make_temphum_entities,
    MODEL_FLOWMETER: _make_flowmeter_entities,
    MODEL_CO2: _make_co2_entities,
    MODEL_POOL: _make_pool_entities,
    MODEL_POOL_PLUS: _make_pool_plus_entities,
    MODEL_HCS005FRF: _make_hcs_moisture_only_entities,
    MODEL_HCS024FRF_V1: _make_hcs_multisensor_entities,
    MODEL_VALVE_213: _make_htv_valve_diagnostic_entities,
    MODEL_VALVE_245: _make_htv_valve_diagnostic_entities,
    MODEL_VALVE_345: _make_htv_valve_diagnostic_entities,
    MODEL_VALVE_405: _make_htv_valve_diagnostic_entities,
    MODEL_HTV210B: _make_htv210b_entities,
}


def _create_hub_entities(coordinator, hubs_cfg):
    """Create the per-hub diagnostic entities for every real hub returned by the API.

    Keyed by mid, not hid: a home has one hid but can hold several top-level
    records, so keying by hid collapsed them and let the last record win over
    the real hub. Records that carry no hub identity are skipped outright.
    """
    hubs_dict = {str(hub.get("mid", i)): hub for i, hub in enumerate(hubs_cfg)} if isinstance(hubs_cfg, list) else hubs_cfg
    entities: list = []
    for hub_info in hubs_dict.values():
        if not is_hub_record(hub_info):
            continue
        entities.append(RainPointHubDeviceIDSensor(coordinator, hub_info))
        entities.append(RainPointHubFirmwareSensor(coordinator, hub_info))
        entities.append(RainPointHubMACSensor(coordinator, hub_info))
        entities.append(RainPointHubRSSISensor(coordinator, hub_info))
    return entities


def _create_sensor_entities(coordinator, key, info, generic_enabled: bool = False):
    """Resolve a sub-device's canonical model and produce its entity list.

    A model with a hand-written factory always wins by lookup order; a model
    with none falls back to the always-on Unsupported diagnostic and, only
    when generic_enabled is true, is additionally offered to the opt-in
    generic sensor factory. Always appends a per-device raw-payload
    diagnostic entity at the end.
    """
    raw_model = info.get("model")
    model = _SENSOR_MODEL_ALIASES.get(raw_model, raw_model)
    sub_name = info.get("sub_name") or f"Sensor {info['addr']}"
    hid = info.get("hid", "")
    mid = info.get("mid", "")
    addr = info.get("addr", "")
    base_slug = f"{hid}_{mid}_{addr}"
    _LOGGER.debug(
        "Creating sensor entity: key=%s, model=%s, sub_name=%s, base_slug=%s",
        key,
        model,
        sub_name,
        base_slug,
    )

    if (info.get("data") or {}).get("type") == SILENT_DATA_TYPE:
        # Must run before the factory lookup: a silent entry has no payload of
        # any kind, but MODEL_HTV210B -- the device this guard was written for --
        # HAS a factory (_make_htv210b_entities), and reaching it here would
        # emit a battery/RSSI pair that reads available with a native_value of
        # None, exactly the "looks wired up while reading nothing" outcome this
        # guard exists to prevent. No generic entities and no Raw Payload
        # sensor either: there
        # is nothing for either to hold.
        return [RainPointNotReportingSensor(coordinator, key, info, base_slug)]

    factory = _MODEL_FACTORIES.get(model)
    if factory is not None:
        entities = list(factory(coordinator, key, info, base_slug))
    else:
        entities = list(_make_unknown_entities(coordinator, key, info, base_slug))
        if generic_enabled:
            # Imported locally to avoid a circular import: generic_entities
            # subclasses RainPointSensorBase, which is defined later in this
            # module than this function.
            from .generic_entities import build_generic_entities

            entities.extend(build_generic_entities(coordinator, key, info, base_slug))
    entities.append(RainPointRawPayloadSensor(coordinator, key, info, base_slug))
    return entities


class _LateSensorEntityAdder:
    """Add a sub-device's entities the first time its key is worth entities.

    Entity construction used to happen exactly once, from the single snapshot
    taken right after the first refresh. A device the hub lists but the cloud
    never reports on only turns into a "silent" entry after three consecutive
    polls, so its diagnostic entity could never be built inside a running
    session. Registering this as a coordinator listener closes that gap.

    Two add-once sets rather than one, and that split is load bearing. A device
    that was reporting when the platform was set up and later goes quiet must
    still gain its Not Reporting entity, and a device that was silent and later
    starts reporting must still gain its real model entities. Neither may be
    handed what it already has, because a repeated unique_id is an error in
    Home Assistant.
    """

    def __init__(self, coordinator, async_add_entities, generic_enabled: bool):
        """Init helper."""
        self._coordinator = coordinator
        self._async_add_entities = async_add_entities
        self._generic_enabled = generic_enabled
        # Both sets are deliberately never pruned, unlike the coordinator's
        # _silent_poll_counts, and the asymmetry is the point. That counter is
        # per-poll state a returning device must restart from zero. These are a
        # record of what has already been handed to Home Assistant, and a key
        # leaving coordinator.data does not remove the entities registered for
        # it, so forgetting the key would let the same unique_id be offered a
        # second time if the key came back. Bounded by the number of distinct
        # sensor keys the installation produces in one session.
        self._keys_with_model_entities: set[str] = set()
        self._keys_with_silent_entity: set[str] = set()

    def collect(self, key: str, info: dict) -> list:
        """Return the entities to add for one sensor key, recording that they were added.

        The single place the add-once bookkeeping is written, so the setup path
        and the listener path cannot disagree about what already exists.
        """
        if (info.get("data") or {}).get("type") == SILENT_DATA_TYPE:
            if key in self._keys_with_silent_entity:
                return []
            self._keys_with_silent_entity.add(key)
        else:
            if key in self._keys_with_model_entities:
                return []
            self._keys_with_model_entities.add(key)
        return list(_create_sensor_entities(self._coordinator, key, info, self._generic_enabled))

    @callback
    def async_on_coordinator_update(self) -> None:
        """Add entities for any sensor key that has become eligible since the last update."""
        sensors_cfg = (self._coordinator.data or {}).get("sensors", {})
        new: list = []
        for key, info in sensors_cfg.items():
            # Matches the defensive filter valve.py and switch.py already apply
            # at setup. It matters more here: this runs on every coordinator
            # update, and raising inside the listener would break the update
            # for every other key rather than just skipping one bad record.
            if not isinstance(info, dict):
                continue
            new.extend(self.collect(key, info))
        if new:
            self._async_add_entities(new)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    data = hass.data[DOMAIN][entry.entry_id]
    coordinator: RainPointCoordinator = data["coordinator"]

    sensors_cfg = {key: info for key, info in coordinator.data.get("sensors", {}).items() if isinstance(info, dict)}
    hubs_cfg = coordinator.data.get("hubs", [])
    generic_enabled = entry.options.get(CONF_GENERIC_ENTITIES_ENABLED, False)

    adder = _LateSensorEntityAdder(coordinator, async_add_entities, generic_enabled)

    entities: list[RainPointSensorBase] = []
    entities.extend(_create_hub_entities(coordinator, hubs_cfg))
    for key, info in sensors_cfg.items():
        # Routed through the adder so the setup snapshot seeds the same
        # bookkeeping the listener reads, which is what keeps the two paths
        # from ever offering the same unique_id twice.
        entities.extend(adder.collect(key, info))

    # The push last-message age entity only exists when push is enabled (it reads
    # the MQTT client's liveness clock, not coordinator.data).
    mqtt_client = data.get("mqtt_client")
    if mqtt_client is not None:
        for hub_info in resolve_push_diagnostic_hubs(coordinator, mqtt_client):
            entities.append(RainPointPushLastMessageSensor(mqtt_client, hub_info))

    if entities:
        async_add_entities(entities)

    # Registered unconditionally: a hub whose every child is silent from the
    # first poll produces zero entities here, and that is precisely the install
    # the late-add path exists for.
    entry.async_on_unload(coordinator.async_add_listener(adder.async_on_coordinator_update))


class RainPointSensorBase(RainPointSubDeviceEntity, SensorEntity):
    """Base class for RainPoint sensors."""

    _device_name_prefix = "Sensor"

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        data = self._sensor_data or {}
        attrs: dict[str, Any] = {}
        if "rssi_dbm" in data:
            attrs["rssi_dbm"] = data["rssi_dbm"]
        if data.get("battery_percent") is not None:
            attrs["battery_percent"] = data["battery_percent"]
        if data.get("battery_flag") is not None:
            # Surfaced alongside the percentage rather than only as a fallback:
            # it is the raw reading, and it is the only thing to look at when a
            # flag we have no charge level for leaves battery_percent unset.
            attrs["battery_flag"] = data["battery_flag"]
        if data.get("report_time") is not None:
            attrs["report_time"] = data["report_time"]

        attrs.update(sub_device_attributes(self.coordinator, self._sensor_key))
        if "device_timestamp" not in attrs:
            _LOGGER.debug("No timestamp found in sensor data: %s", data)

        # Legacy timestamp from raw_status (fallback)
        info = (self.coordinator.data or {}).get("sensors", {}).get(self._sensor_key) or {}
        raw_status = info.get("raw_status") or {}
        ts = raw_status.get("time")
        if ts:
            try:
                dt = datetime.fromtimestamp(ts / 1000, tz=UTC)
                attrs["last_updated"] = dt.isoformat()
            except Exception:
                # If anything goes wrong, we simply omit last_updated
                pass

        _LOGGER.debug("Sensor %s attributes: %s", self._sensor_key, attrs)
        return attrs


class RainPointMoisturePercentSensor(RainPointSensorBase):
    """Moisture % sensor."""

    _attr_device_class = SensorDeviceClass.MOISTURE
    _attr_native_unit_of_measurement = "%"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:water-percent"

    def __init__(
        self,
        coordinator: RainPointCoordinator,
        sensor_key: str,
        sensor_info: dict,
        base_slug: str,
        simple: bool,
    ) -> None:
        super().__init__(coordinator, sensor_key, sensor_info, base_slug)
        self._simple = simple
        sub_name = sensor_info.get("sub_name") or "Sensor"
        self._attr_unique_id = f"rainpoint_{base_slug}_moisture_percent"
        self._attr_name = f"{sub_name} Moisture Percent"

    @property
    def native_value(self) -> float | None:
        data = self._sensor_data
        value = data.get("moisture_percent") if data else None
        _LOGGER.debug("native_value for %s (moisture_percent): %s", self._sensor_key, value)
        return value


class RainPointTemperatureSensor(RainPointSensorBase):
    """Temperature sensor for HCS021FRF."""

    _attr_device_class = SensorDeviceClass.TEMPERATURE
    _attr_native_unit_of_measurement = "°C"
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(
        self,
        coordinator: RainPointCoordinator,
        sensor_key: str,
        sensor_info: dict,
        base_slug: str,
    ) -> None:
        super().__init__(coordinator, sensor_key, sensor_info, base_slug)
        sub_name = sensor_info.get("sub_name") or "Sensor"
        self._attr_unique_id = f"rainpoint_{base_slug}_temperature"
        self._attr_name = f"{sub_name} Temperature"

    @property
    def native_value(self) -> float | None:
        data = self._sensor_data
        value = round(data.get("temperature_c"), 1) if data and data.get("temperature_c") is not None else None
        _LOGGER.debug("native_value for %s (temperature_c): %s", self._sensor_key, value)
        return value


class RainPointIlluminanceSensor(RainPointSensorBase):
    """Illuminance sensor for HCS021FRF."""

    _attr_device_class = SensorDeviceClass.ILLUMINANCE
    _attr_native_unit_of_measurement = "lx"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:brightness-5"

    def __init__(
        self,
        coordinator: RainPointCoordinator,
        sensor_key: str,
        sensor_info: dict,
        base_slug: str,
    ) -> None:
        super().__init__(coordinator, sensor_key, sensor_info, base_slug)
        sub_name = sensor_info.get("sub_name") or "Sensor"
        self._attr_unique_id = f"rainpoint_{base_slug}_illuminance"
        self._attr_name = f"{sub_name} Illuminance"

    @property
    def native_value(self) -> float | None:
        data = self._sensor_data
        value = data.get("illuminance_lux") if data else None
        _LOGGER.debug("native_value for %s (illuminance_lux): %s", self._sensor_key, value)
        return value


class RainPointRainSensor(RainPointSensorBase):
    """Rain sensor (various windows)."""

    _attr_device_class = SensorDeviceClass.PRECIPITATION
    _attr_native_unit_of_measurement = "mm"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:weather-rainy"

    def __init__(
        self,
        coordinator: RainPointCoordinator,
        sensor_key: str,
        sensor_info: dict,
        base_slug: str,
        data_key: str,
        label: str,
    ) -> None:
        super().__init__(coordinator, sensor_key, sensor_info, base_slug)
        self._data_key = data_key
        sub_name = sensor_info.get("sub_name") or "Rain Sensor"
        slug_suffix = data_key
        self._attr_unique_id = f"rainpoint_{base_slug}_{slug_suffix}"
        # Format rain labels: convert to "Rain (Last X)" style
        window = label.replace("rain", "").strip()
        window_map = {
            "last hour": "Last Hour",
            "last 24h": "Last 24 Hours",
            "last 7d": "Last 7 Days",
            "total": "Total",
        }
        window_fmt = window_map.get(window, window.title())
        self._attr_name = f"{sub_name} Rain ({window_fmt})"

    @property
    def native_value(self) -> float | None:
        data = self._sensor_data
        if not data:
            return None
        val = data.get(self._data_key)
        if val is None:
            return None
        return round(val, 1)


# HWS019WRF-V2 (Display Hub)
class DisplayHubReadingSensor(RainPointSensorBase):
    """Sensor for each Display Hub reading."""

    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coordinator, sensor_key, sensor_info, base_slug, reading_key):
        super().__init__(coordinator, sensor_key, sensor_info, base_slug)
        self._reading_key = reading_key
        self._attr_unique_id = f"rainpoint_{base_slug}_displayhub_{reading_key}"
        sub_name = sensor_info.get("sub_name") or "Display Hub"
        self._attr_name = f"{sub_name} {reading_key}"

    @property
    def native_value(self):
        data = self._sensor_data
        if not data:
            return None
        readings = data.get("readings", {})
        value = readings.get(self._reading_key)
        try:
            return float(value)
        except (TypeError, ValueError):
            return value


# HCS014ARF (Temperature/Humidity)
class RainPointTempHumCurrentSensor(RainPointSensorBase):
    _attr_device_class = SensorDeviceClass.TEMPERATURE
    _attr_native_unit_of_measurement = "°C"
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coordinator, sensor_key, sensor_info, base_slug):
        super().__init__(coordinator, sensor_key, sensor_info, base_slug)
        self._attr_unique_id = f"rainpoint_{base_slug}_temphum_current"
        self._attr_name = f"{sensor_info.get('sub_name', 'Sensor')} Current Temperature"

    @property
    def native_value(self):
        data = self._sensor_data
        return data.get("tempcurrent") if data else None


class RainPointTempHumHighSensor(RainPointSensorBase):
    _attr_device_class = SensorDeviceClass.TEMPERATURE
    _attr_native_unit_of_measurement = "°C"
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coordinator, sensor_key, sensor_info, base_slug):
        super().__init__(coordinator, sensor_key, sensor_info, base_slug)
        self._attr_unique_id = f"rainpoint_{base_slug}_temphum_high"
        self._attr_name = f"{sensor_info.get('sub_name', 'Sensor')} High Temperature"

    @property
    def native_value(self):
        data = self._sensor_data
        return data.get("temphigh") if data else None


class RainPointTempHumLowSensor(RainPointSensorBase):
    _attr_device_class = SensorDeviceClass.TEMPERATURE
    _attr_native_unit_of_measurement = "°C"
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coordinator, sensor_key, sensor_info, base_slug):
        super().__init__(coordinator, sensor_key, sensor_info, base_slug)
        self._attr_unique_id = f"rainpoint_{base_slug}_temphum_low"
        self._attr_name = f"{sensor_info.get('sub_name', 'Sensor')} Low Temperature"

    @property
    def native_value(self):
        data = self._sensor_data
        return data.get("templow") if data else None


class RainPointTempHumHumidityCurrentSensor(RainPointSensorBase):
    _attr_device_class = SensorDeviceClass.HUMIDITY
    _attr_native_unit_of_measurement = "%"
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coordinator, sensor_key, sensor_info, base_slug):
        super().__init__(coordinator, sensor_key, sensor_info, base_slug)
        self._attr_unique_id = f"rainpoint_{base_slug}_temphum_humidity_current"
        self._attr_name = f"{sensor_info.get('sub_name', 'Sensor')} Current Humidity"

    @property
    def native_value(self):
        data = self._sensor_data
        return data.get("humiditycurrent") if data else None


class RainPointTempHumHumidityHighSensor(RainPointSensorBase):
    _attr_device_class = SensorDeviceClass.HUMIDITY
    _attr_native_unit_of_measurement = "%"
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coordinator, sensor_key, sensor_info, base_slug):
        super().__init__(coordinator, sensor_key, sensor_info, base_slug)
        self._attr_unique_id = f"rainpoint_{base_slug}_temphum_humidity_high"
        self._attr_name = f"{sensor_info.get('sub_name', 'Sensor')} High Humidity"

    @property
    def native_value(self):
        data = self._sensor_data
        return data.get("humidityhigh") if data else None


class RainPointTempHumHumidityLowSensor(RainPointSensorBase):
    _attr_device_class = SensorDeviceClass.HUMIDITY
    _attr_native_unit_of_measurement = "%"
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coordinator, sensor_key, sensor_info, base_slug):
        super().__init__(coordinator, sensor_key, sensor_info, base_slug)
        self._attr_unique_id = f"rainpoint_{base_slug}_temphum_humidity_low"
        self._attr_name = f"{sensor_info.get('sub_name', 'Sensor')} Low Humidity"

    @property
    def native_value(self):
        data = self._sensor_data
        return data.get("humiditylow") if data else None


# HCS008FRF (Flowmeter)
class RainPointFlowCurrentUsedSensor(RainPointSensorBase):
    _attr_native_unit_of_measurement = "L"
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coordinator, sensor_key, sensor_info, base_slug):
        super().__init__(coordinator, sensor_key, sensor_info, base_slug)
        self._attr_unique_id = f"rainpoint_{base_slug}_flow_current_used"
        self._attr_name = f"{sensor_info.get('sub_name', 'Sensor')} Flow Current Used"

    @property
    def native_value(self):
        data = self._sensor_data
        return data.get("flowcurrentused") if data else None


class RainPointFlowCurrentDurationSensor(RainPointSensorBase):
    _attr_native_unit_of_measurement = "s"
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coordinator, sensor_key, sensor_info, base_slug):
        super().__init__(coordinator, sensor_key, sensor_info, base_slug)
        self._attr_unique_id = f"rainpoint_{base_slug}_flow_current_duration"
        self._attr_name = f"{sensor_info.get('sub_name', 'Sensor')} Flow Current Duration"

    @property
    def native_value(self):
        data = self._sensor_data
        return data.get("flowcurrenduration") if data else None


class RainPointFlowLastUsedSensor(RainPointSensorBase):
    _attr_native_unit_of_measurement = "L"
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coordinator, sensor_key, sensor_info, base_slug):
        super().__init__(coordinator, sensor_key, sensor_info, base_slug)
        self._attr_unique_id = f"rainpoint_{base_slug}_flow_last_used"
        self._attr_name = f"{sensor_info.get('sub_name', 'Sensor')} Flow Last Used"

    @property
    def native_value(self):
        data = self._sensor_data
        return data.get("flowlastused") if data else None


class RainPointFlowLastUsedDurationSensor(RainPointSensorBase):
    _attr_native_unit_of_measurement = "s"
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coordinator, sensor_key, sensor_info, base_slug):
        super().__init__(coordinator, sensor_key, sensor_info, base_slug)
        self._attr_unique_id = f"rainpoint_{base_slug}_flow_last_used_duration"
        self._attr_name = f"{sensor_info.get('sub_name', 'Sensor')} Flow Last Used Duration"

    @property
    def native_value(self):
        data = self._sensor_data
        return data.get("flowlastusedduration") if data else None


class RainPointFlowTotalTodaySensor(RainPointSensorBase):
    _attr_native_unit_of_measurement = "L"
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coordinator, sensor_key, sensor_info, base_slug):
        super().__init__(coordinator, sensor_key, sensor_info, base_slug)
        self._attr_unique_id = f"rainpoint_{base_slug}_flow_total_today"
        self._attr_name = f"{sensor_info.get('sub_name', 'Sensor')} Flow Total Today"

    @property
    def native_value(self):
        data = self._sensor_data
        return data.get("flowtotaltoday") if data else None


class RainPointFlowTotalSensor(RainPointSensorBase):
    _attr_native_unit_of_measurement = "L"
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coordinator, sensor_key, sensor_info, base_slug):
        super().__init__(coordinator, sensor_key, sensor_info, base_slug)
        self._attr_unique_id = f"rainpoint_{base_slug}_flow_total"
        self._attr_name = f"{sensor_info.get('sub_name', 'Sensor')} Flow Total"

    @property
    def native_value(self):
        data = self._sensor_data
        return data.get("flowtotal") if data else None


class RainPointFlowBatterySensor(RainPointSensorBase):
    _attr_device_class = SensorDeviceClass.BATTERY
    _attr_native_unit_of_measurement = "%"
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coordinator, sensor_key, sensor_info, base_slug):
        super().__init__(coordinator, sensor_key, sensor_info, base_slug)
        self._attr_unique_id = f"rainpoint_{base_slug}_flow_battery"
        self._attr_name = f"{sensor_info.get('sub_name', 'Sensor')} Flow Battery"

    @property
    def native_value(self):
        data = self._sensor_data
        return data.get("flowbatt") if data else None


# HCS0530THO (CO2/Temp/Humidity)
class RainPointCO2Sensor(RainPointSensorBase):
    _attr_device_class = SensorDeviceClass.CO2
    _attr_native_unit_of_measurement = "ppm"
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coordinator, sensor_key, sensor_info, base_slug):
        super().__init__(coordinator, sensor_key, sensor_info, base_slug)
        self._attr_unique_id = f"rainpoint_{base_slug}_co2"
        self._attr_name = f"{sensor_info.get('sub_name', 'Sensor')} CO2"

    @property
    def native_value(self):
        data = self._sensor_data
        return data.get("co2") if data else None


class RainPointCO2LowSensor(RainPointSensorBase):
    _attr_device_class = SensorDeviceClass.CO2
    _attr_native_unit_of_measurement = "ppm"
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coordinator, sensor_key, sensor_info, base_slug):
        super().__init__(coordinator, sensor_key, sensor_info, base_slug)
        self._attr_unique_id = f"rainpoint_{base_slug}_co2_low"
        self._attr_name = f"{sensor_info.get('sub_name', 'Sensor')} CO2 Low"

    @property
    def native_value(self):
        data = self._sensor_data
        return data.get("co2low") if data else None


class RainPointCO2HighSensor(RainPointSensorBase):
    _attr_device_class = SensorDeviceClass.CO2
    _attr_native_unit_of_measurement = "ppm"
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coordinator, sensor_key, sensor_info, base_slug):
        super().__init__(coordinator, sensor_key, sensor_info, base_slug)
        self._attr_unique_id = f"rainpoint_{base_slug}_co2_high"
        self._attr_name = f"{sensor_info.get('sub_name', 'Sensor')} CO2 High"

    @property
    def native_value(self):
        data = self._sensor_data
        return data.get("co2high") if data else None


class RainPointCO2TempSensor(RainPointSensorBase):
    _attr_device_class = SensorDeviceClass.TEMPERATURE
    _attr_native_unit_of_measurement = "°C"
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coordinator, sensor_key, sensor_info, base_slug):
        super().__init__(coordinator, sensor_key, sensor_info, base_slug)
        self._attr_unique_id = f"rainpoint_{base_slug}_co2_temp"
        self._attr_name = f"{sensor_info.get('sub_name', 'Sensor')} CO2 Temperature"

    @property
    def native_value(self):
        data = self._sensor_data
        return data.get("co2temp") if data else None


class RainPointCO2HumiditySensor(RainPointSensorBase):
    _attr_device_class = SensorDeviceClass.HUMIDITY
    _attr_native_unit_of_measurement = "%"
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coordinator, sensor_key, sensor_info, base_slug):
        super().__init__(coordinator, sensor_key, sensor_info, base_slug)
        self._attr_unique_id = f"rainpoint_{base_slug}_co2_humidity"
        self._attr_name = f"{sensor_info.get('sub_name', 'Sensor')} CO2 Humidity"

    @property
    def native_value(self):
        data = self._sensor_data
        return data.get("co2humidity") if data else None


class RainPointCO2BatterySensor(RainPointSensorBase):
    _attr_device_class = SensorDeviceClass.BATTERY
    _attr_native_unit_of_measurement = "%"
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coordinator, sensor_key, sensor_info, base_slug):
        super().__init__(coordinator, sensor_key, sensor_info, base_slug)
        self._attr_unique_id = f"rainpoint_{base_slug}_co2_battery"
        self._attr_name = f"{sensor_info.get('sub_name', 'Sensor')} CO2 Battery"

    @property
    def native_value(self):
        data = self._sensor_data
        return data.get("co2batt") if data else None


# HCS0528ARF (Pool/Temperature)
class RainPointPoolCurrentTempSensor(RainPointSensorBase):
    _attr_device_class = SensorDeviceClass.TEMPERATURE
    _attr_native_unit_of_measurement = "°C"
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coordinator, sensor_key, sensor_info, base_slug):
        super().__init__(coordinator, sensor_key, sensor_info, base_slug)
        self._attr_unique_id = f"rainpoint_{base_slug}_pool_current_temp"
        self._attr_name = f"{sensor_info.get('sub_name', 'Sensor')} Pool Current Temperature"

    @property
    def native_value(self):
        data = self._sensor_data
        return data.get("tempcurrent") if data else None


class RainPointPoolHighTempSensor(RainPointSensorBase):
    _attr_device_class = SensorDeviceClass.TEMPERATURE
    _attr_native_unit_of_measurement = "°C"
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coordinator, sensor_key, sensor_info, base_slug):
        super().__init__(coordinator, sensor_key, sensor_info, base_slug)
        self._attr_unique_id = f"rainpoint_{base_slug}_pool_high_temp"
        self._attr_name = f"{sensor_info.get('sub_name', 'Sensor')} Pool High Temperature"

    @property
    def native_value(self):
        data = self._sensor_data
        return data.get("temphigh") if data else None


class RainPointPoolLowTempSensor(RainPointSensorBase):
    _attr_device_class = SensorDeviceClass.TEMPERATURE
    _attr_native_unit_of_measurement = "°C"
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coordinator, sensor_key, sensor_info, base_slug):
        super().__init__(coordinator, sensor_key, sensor_info, base_slug)
        self._attr_unique_id = f"rainpoint_{base_slug}_pool_low_temp"
        self._attr_name = f"{sensor_info.get('sub_name', 'Sensor')} Pool Low Temperature"

    @property
    def native_value(self):
        data = self._sensor_data
        return data.get("templow") if data else None


class RainPointPoolBatterySensor(RainPointSensorBase):
    _attr_device_class = SensorDeviceClass.BATTERY
    _attr_native_unit_of_measurement = "%"
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coordinator, sensor_key, sensor_info, base_slug):
        super().__init__(coordinator, sensor_key, sensor_info, base_slug)
        self._attr_unique_id = f"rainpoint_{base_slug}_pool_battery"
        self._attr_name = f"{sensor_info.get('sub_name', 'Sensor')} Pool Battery"

    @property
    def native_value(self):
        data = self._sensor_data
        return data.get("tempbatt") if data else None


# HCS015ARF+ (Pool + Ambient temp/humidity)
class RainPointPoolPlusPoolCurrentTempSensor(RainPointSensorBase):
    _attr_device_class = SensorDeviceClass.TEMPERATURE
    _attr_native_unit_of_measurement = "°C"
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coordinator, sensor_key, sensor_info, base_slug):
        super().__init__(coordinator, sensor_key, sensor_info, base_slug)
        self._attr_unique_id = f"rainpoint_{base_slug}_pool_plus_pool_current_temp"
        self._attr_name = f"{sensor_info.get('sub_name', 'Sensor')} Pool Temperature"

    @property
    def native_value(self):
        data = self._sensor_data
        return data.get("pool_tempcurrent") if data else None


class RainPointPoolPlusPoolHighTempSensor(RainPointSensorBase):
    _attr_device_class = SensorDeviceClass.TEMPERATURE
    _attr_native_unit_of_measurement = "°C"
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coordinator, sensor_key, sensor_info, base_slug):
        super().__init__(coordinator, sensor_key, sensor_info, base_slug)
        self._attr_unique_id = f"rainpoint_{base_slug}_pool_plus_pool_high_temp"
        self._attr_name = f"{sensor_info.get('sub_name', 'Sensor')} Pool High Temperature"

    @property
    def native_value(self):
        data = self._sensor_data
        return data.get("pool_temphigh") if data else None


class RainPointPoolPlusPoolLowTempSensor(RainPointSensorBase):
    _attr_device_class = SensorDeviceClass.TEMPERATURE
    _attr_native_unit_of_measurement = "°C"
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coordinator, sensor_key, sensor_info, base_slug):
        super().__init__(coordinator, sensor_key, sensor_info, base_slug)
        self._attr_unique_id = f"rainpoint_{base_slug}_pool_plus_pool_low_temp"
        self._attr_name = f"{sensor_info.get('sub_name', 'Sensor')} Pool Low Temperature"

    @property
    def native_value(self):
        data = self._sensor_data
        return data.get("pool_templow") if data else None


class RainPointPoolPlusAmbientCurrentTempSensor(RainPointSensorBase):
    _attr_device_class = SensorDeviceClass.TEMPERATURE
    _attr_native_unit_of_measurement = "°C"
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coordinator, sensor_key, sensor_info, base_slug):
        super().__init__(coordinator, sensor_key, sensor_info, base_slug)
        self._attr_unique_id = f"rainpoint_{base_slug}_pool_plus_ambient_current_temp"
        self._attr_name = f"{sensor_info.get('sub_name', 'Sensor')} Ambient Temperature"

    @property
    def native_value(self):
        data = self._sensor_data
        return data.get("ambient_tempcurrent") if data else None


class RainPointPoolPlusAmbientHighTempSensor(RainPointSensorBase):
    _attr_device_class = SensorDeviceClass.TEMPERATURE
    _attr_native_unit_of_measurement = "°C"
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coordinator, sensor_key, sensor_info, base_slug):
        super().__init__(coordinator, sensor_key, sensor_info, base_slug)
        self._attr_unique_id = f"rainpoint_{base_slug}_pool_plus_ambient_high_temp"
        self._attr_name = f"{sensor_info.get('sub_name', 'Sensor')} Ambient High Temperature"

    @property
    def native_value(self):
        data = self._sensor_data
        return data.get("ambient_temphigh") if data else None


class RainPointPoolPlusAmbientLowTempSensor(RainPointSensorBase):
    _attr_device_class = SensorDeviceClass.TEMPERATURE
    _attr_native_unit_of_measurement = "°C"
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coordinator, sensor_key, sensor_info, base_slug):
        super().__init__(coordinator, sensor_key, sensor_info, base_slug)
        self._attr_unique_id = f"rainpoint_{base_slug}_pool_plus_ambient_low_temp"
        self._attr_name = f"{sensor_info.get('sub_name', 'Sensor')} Ambient Low Temperature"

    @property
    def native_value(self):
        data = self._sensor_data
        return data.get("ambient_templow") if data else None


class RainPointPoolPlusHumidityCurrentSensor(RainPointSensorBase):
    _attr_device_class = SensorDeviceClass.HUMIDITY
    _attr_native_unit_of_measurement = "%"
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coordinator, sensor_key, sensor_info, base_slug):
        super().__init__(coordinator, sensor_key, sensor_info, base_slug)
        self._attr_unique_id = f"rainpoint_{base_slug}_pool_plus_humidity_current"
        self._attr_name = f"{sensor_info.get('sub_name', 'Sensor')} Ambient Humidity"

    @property
    def native_value(self):
        data = self._sensor_data
        return data.get("humidity_current") if data else None


class RainPointPoolPlusHumidityHighSensor(RainPointSensorBase):
    _attr_device_class = SensorDeviceClass.HUMIDITY
    _attr_native_unit_of_measurement = "%"
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coordinator, sensor_key, sensor_info, base_slug):
        super().__init__(coordinator, sensor_key, sensor_info, base_slug)
        self._attr_unique_id = f"rainpoint_{base_slug}_pool_plus_humidity_high"
        self._attr_name = f"{sensor_info.get('sub_name', 'Sensor')} Ambient High Humidity"

    @property
    def native_value(self):
        data = self._sensor_data
        return data.get("humidity_high") if data else None


class RainPointPoolPlusHumidityLowSensor(RainPointSensorBase):
    _attr_device_class = SensorDeviceClass.HUMIDITY
    _attr_native_unit_of_measurement = "%"
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coordinator, sensor_key, sensor_info, base_slug):
        super().__init__(coordinator, sensor_key, sensor_info, base_slug)
        self._attr_unique_id = f"rainpoint_{base_slug}_pool_plus_humidity_low"
        self._attr_name = f"{sensor_info.get('sub_name', 'Sensor')} Ambient Low Humidity"

    @property
    def native_value(self):
        data = self._sensor_data
        return data.get("humidity_low") if data else None


class RainPointUnknownSensor(RainPointSensorBase):
    """Diagnostic sensor for unknown/unsupported models.

    This sensor surfaces raw payload data in Home Assistant so users can
    easily copy it when reporting issues for new sensor support.
    """

    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_icon = "mdi:help-circle-outline"

    # extra_state_attributes is read on every state write and every template
    # render, and two of the values it returns cost a full catalog gate
    # evaluation (plus, for the link, a structural decode of the payload).
    # The gate answer depends only on this entity's fixed model and modelCode,
    # and the link only on those and the current payload, so both are computed
    # once and reused until their inputs change. Declared on the class so an
    # instance built without __init__ still starts from an empty memo.
    _gate_description: dict | None = None
    _report_url_for_payload: tuple[str | None, str] | None = None

    def __init__(self, coordinator, sensor_key, sensor_info, base_slug):
        super().__init__(coordinator, sensor_key, sensor_info, base_slug)
        model = sensor_info.get("model", "unknown")
        self._attr_unique_id = f"rainpoint_{base_slug}_unknown_{model}"
        sub_name = sensor_info.get("sub_name") or "Sensor"
        self._attr_name = f"{sub_name} Unsupported ({model})"

    @property
    def native_value(self) -> str:
        """Return the model name as the state."""
        data = self._sensor_data
        if data:
            return f"Unsupported: {data.get('model', 'unknown')}"
        return "No data"

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Include raw payload and instructions for reporting."""
        attrs = super().extra_state_attributes
        data = self._sensor_data or {}

        attrs["model"] = data.get("model")
        attrs["raw_payload"] = data.get("raw_value")

        # Best-effort structural decode of the unsupported payload. These field
        # names/values are unverified (no per-model decoder exists yet); they
        # exist to speed up adding support, not to be relied on.
        generic = data.get("generic") or {}
        field_names = generic.get("field_names")
        if field_names:
            attrs["decoded_fields"] = field_names
            attrs["decoded_values"] = generic.get("fields")

        model = self._sensor_info.get("model")
        model_code = self._sensor_info.get("model_code")
        if self._gate_description is None:
            # Both imported locally to avoid a circular import: generic_entities
            # subclasses RainPointSensorBase, which is defined later in this
            # module than this class, and generic_control reaches
            # generic_entities the same way (it imports _IDENTITY_SPECS and
            # friends from it). Both are genuinely cycle-breaking for that
            # reason, unlike the deferral-of-convenience import in the options
            # flow.
            from .generic_control import describe_control_gate
            from .generic_entities import describe_generic_gate

            self._gate_description = {**describe_generic_gate(model, model_code), **describe_control_gate(model, model_code)}
        # Always present, regardless of either the generic entities or the
        # generic control options toggle: computed from the catalog and the
        # curated table alone, involves no entity creation, and is most
        # valuable to a user who has not opted in to either. Copied out so a
        # consumer editing the attributes cannot reach back into the cached
        # lists.
        attrs.update({key: list(value) for key, value in self._gate_description.items()})

        # The same pre-filled report link the unsupported-model notification
        # uses. This attribute is the durable surface: the notification fires
        # once per variant and can be dismissed, so a user who comes back to
        # the device later finds only this. Pointing it at the bare issue list
        # would make the lasting path the worse one.
        raw_value = data.get("raw_value")
        if self._report_url_for_payload is None or self._report_url_for_payload[0] != raw_value:
            self._report_url_for_payload = (raw_value, _build_new_device_issue_url(model or "unknown", raw_value, model_code))
        attrs["report_url"] = self._report_url_for_payload[1]
        attrs["instructions"] = (
            "This sensor model is not yet supported. Open report_url to file a pre-filled support request, "
            "then add what the RainPoint app shows for this device."
        )

        return attrs


class RainPointNotReportingSensor(RainPointSensorBase):
    """Diagnostic sensor for a sub-device the hub lists but no status response ever mentions.

    "never_reported" means this integration has observed no reading from this
    device since it started; "stopped_reporting" means it observed one at
    last_seen and has since stopped. That distinction stays true across a
    Home Assistant restart, which a bare "never seen" state would not report
    honestly. No state class is set, matching RainPointUnknownSensor:
    an entity with no readable state must never enter long-term statistics.
    """

    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options: ClassVar[list[str]] = ["never_reported", "stopped_reporting"]
    _attr_icon = "mdi:message-off-outline"

    # The report link's inputs (model, modelCode) are fixed once this entity is
    # constructed, so it is computed once and reused rather than rebuilt on
    # every attribute read. Declared on the class so an instance built without
    # __init__ still starts from an empty memo, matching RainPointUnknownSensor.
    _report_url: str | None = None

    def __init__(self, coordinator, sensor_key, sensor_info, base_slug):
        """Name the entity after the sub-device the hub lists, not the model.

        A silent device has no reading to identify it by, so the hub's own
        name for it is the only label a user will recognise.
        """
        super().__init__(coordinator, sensor_key, sensor_info, base_slug)
        self._attr_unique_id = f"rainpoint_{base_slug}_not_reporting"
        sub_name = sensor_info.get("sub_name") or "Device"
        self._attr_name = f"{sub_name} Not Reporting"

    @property
    def available(self) -> bool:
        """Always available: reporting the absence of a reading is this entity's job."""
        return True

    @property
    def native_value(self) -> str | None:
        """Report which kind of silence this is, or nothing once it recovers.

        A recovered device keeps this entity (nothing removes it) but its
        entry no longer carries silent_state, so the state goes empty rather
        than reporting a stale "not reporting".
        """
        data = self._sensor_data or {}
        return data.get("silent_state")

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Carry the report link and the evidence a maintainer needs with it."""
        attrs = super().extra_state_attributes
        data = self._sensor_data or {}
        attrs["model"] = data.get("model")
        attrs["last_seen"] = data.get("last_seen")
        attrs["missed_polls"] = data.get("missed_polls")

        # The same one-click report path an unsupported-payload device gets,
        # except the payload field states plainly that there is no payload
        # at all: the absence of one is itself the finding a maintainer needs.
        if self._report_url is None:
            model = self._sensor_info.get("model")
            model_code = self._sensor_info.get("model_code")
            self._report_url = _build_new_device_issue_url(
                model or "unknown", None, model_code, payload_note=NO_STATUS_PAYLOAD_MARKER
            )
        attrs["report_url"] = self._report_url
        attrs["instructions"] = (
            "This device is listed by RainPoint but returns no readings. Opening report_url files a "
            "pre-filled support request that already states the device reports no status."
        )
        return attrs


class RainPointRawPayloadSensor(RainPointSensorBase):
    """Raw hex payload sensor (diagnostic, disabled by default)."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_icon = "mdi:code-braces"
    _attr_entity_registry_enabled_default = False  # Disabled by default

    def __init__(
        self,
        coordinator: RainPointCoordinator,
        sensor_key: str,
        sensor_info: dict,
        base_slug: str,
    ) -> None:
        super().__init__(coordinator, sensor_key, sensor_info, base_slug)
        sub_name = sensor_info.get("sub_name") or "Sensor"
        self._attr_unique_id = f"rainpoint_{base_slug}_raw_payload"
        self._attr_name = f"{sub_name} Raw Payload"

    @property
    def native_value(self) -> str | None:
        """Return the raw hex payload string."""
        sensors = self.coordinator.data.get("sensors", {})
        info = sensors.get(self._sensor_key) or {}
        raw_status = info.get("raw_status") or {}
        value = raw_status.get("value")
        _LOGGER.debug("native_value for %s (raw_payload): %s", self._sensor_key, value)
        return value


class RainPointZoneSensorBase(RainPointSensorBase):
    """Shared per-zone plumbing: the zone number and the guarded record lookup.

    Every per-zone sensor reads its record the same way, so the zones-shape
    guard lives here once rather than once per class.
    """

    def __init__(
        self,
        coordinator: RainPointCoordinator,
        sensor_key: str,
        sensor_info: dict,
        base_slug: str,
        zone_num: int,
    ) -> None:
        """Bind the sensor to its sub-device and remember which zone it reads."""
        super().__init__(coordinator, sensor_key, sensor_info, base_slug)
        self._zone_num = zone_num

    @property
    def _zone_data(self) -> dict | None:
        """Return this zone's decoded record, or None when the frame omits it."""
        data = self._sensor_data or {}
        zones = data.get("zones")
        if not isinstance(zones, dict):
            return None
        return zones.get(self._zone_num)


class RainPointZoneWaterUsageSensor(RainPointZoneSensorBase):
    """Water used by one valve zone during its last completed run.

    The value is a converted flow count, not a metered volume: the device
    reports a raw count and the gallons-per-count factor is calibrated from a
    single RainPoint-app reading (see _USAGE_GALLONS_PER_COUNT in api/decoders.py).
    Two consequences follow, and both are deliberate.

    There is no device_class. SensorDeviceClass.WATER accepts only the TOTAL
    and TOTAL_INCREASING state classes, and this reading is neither: it is a
    per-run value that returns to zero while the zone is running, so it would
    corrupt any meter built on it.

    _attr_state_class is None, so the reading stays out of long-term
    statistics. A finer calibration from a longer run would rescale every past
    value, which recorded statistics cannot retroactively absorb; recent-state
    history and graphs are unaffected. The raw count is exposed as an
    attribute so the conversion stays auditable against the app.
    """

    _attr_native_unit_of_measurement = UnitOfVolume.GALLONS
    _attr_state_class = None
    _attr_suggested_display_precision = 2
    _attr_icon = "mdi:water"

    def __init__(
        self,
        coordinator: RainPointCoordinator,
        sensor_key: str,
        sensor_info: dict,
        base_slug: str,
        zone_num: int,
    ) -> None:
        """Name and key the water-usage sensor for one zone."""
        super().__init__(coordinator, sensor_key, sensor_info, base_slug, zone_num)
        sub_name = sensor_info.get("sub_name") or "Valve Hub"
        self._attr_unique_id = f"rainpoint_{base_slug}_zone{zone_num}_water_used"
        self._attr_name = f"{sub_name} Zone {zone_num} Water Used"

    @property
    def native_value(self) -> float | None:
        zone = self._zone_data
        if not zone:
            return None
        return zone.get("last_usage_gallons")

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Expose the zone number, raw flow count, and conversion factor for auditing."""
        attrs = dict(super().extra_state_attributes)
        zone = self._zone_data or {}
        attrs["zone"] = self._zone_num
        attrs["last_usage_counts"] = zone.get("last_usage_counts")
        attrs["gallons_per_count"] = _USAGE_GALLONS_PER_COUNT
        return attrs


class RainPointZoneStateSensor(RainPointZoneSensorBase):
    """Open/closed state of one valve zone, read-only.

    Exists for valve models whose decode is trusted but whose control path is
    not: a valve entity would advertise open/close it cannot deliver, so the
    state ships as an enum sensor until the control half is proven on real
    hardware. The commanded run length and the packed end-of-run time ride
    along as attributes rather than separate entities, since both describe
    the same run the state refers to; the raw state word stays visible so a
    latched high bit is auditable against future captures.
    """

    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options: ClassVar[list[str]] = ["closed", "open"]
    _attr_icon = "mdi:valve"

    def __init__(
        self,
        coordinator: RainPointCoordinator,
        sensor_key: str,
        sensor_info: dict,
        base_slug: str,
        zone_num: int,
    ) -> None:
        """Name and key the state sensor for one zone."""
        super().__init__(coordinator, sensor_key, sensor_info, base_slug, zone_num)
        sub_name = sensor_info.get("sub_name") or "Valve"
        self._attr_unique_id = f"rainpoint_{base_slug}_zone{zone_num}_state"
        self._attr_name = f"{sub_name} Zone {zone_num} State"

    @property
    def native_value(self) -> str | None:
        """Return "open" or "closed", or None when the frame omits the zone."""
        zone = self._zone_data
        if not zone:
            return None
        return "open" if zone.get("open") else "closed"

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Expose the zone number plus the run's duration, end time, and raw state word."""
        attrs = dict(super().extra_state_attributes)
        zone = self._zone_data or {}
        attrs["zone"] = self._zone_num
        attrs["duration_seconds"] = zone.get("duration_seconds")
        attrs["event_time"] = zone.get("event_time")
        attrs["state_raw"] = zone.get("state_raw")
        return attrs
