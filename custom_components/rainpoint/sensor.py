from __future__ import annotations

import logging
import re
from typing import Any

from datetime import datetime, timezone

from homeassistant.components.sensor import (
    SensorEntity,
    SensorDeviceClass,
    SensorStateClass,
)
from homeassistant.const import EntityCategory
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    DOMAIN,
    MODEL_MOISTURE_SIMPLE,
    MODEL_MOISTURE_FULL,
    MODEL_RAIN,
    MODEL_TEMPHUM,
    MODEL_FLOWMETER,
    MODEL_CO2,
    MODEL_POOL,
    MODEL_POOL_PLUS,
    MODEL_DISPLAY_HUB,
    # New HCS sensor models
    MODEL_HCS005FRF,
    MODEL_HCS003FRF,
    MODEL_HCS024FRF_V1,
    MODEL_HCS015ARF,
    MODEL_HCS0528ARF,
    MODEL_HCS027ARF,
    MODEL_HCS016ARF,
    MODEL_HCS044FRF,
    MODEL_HCS666FRF,
    MODEL_HCS666RFR_P,
    MODEL_HCS999FRF,
    MODEL_HCS999FRF_P,
    MODEL_HCS666FRF_X,
    MODEL_HCS701B,
    MODEL_HCS596WB,
    MODEL_HCS596WB_V4,
    MODEL_HCS706ARF,
    MODEL_HCS802ARF,
    MODEL_HCS048B,
    MODEL_HCS888ARF_V1,
    MODEL_HCS0600ARF,
)
from .coordinator import RainPointCoordinator
from .diagnostic_sensors import (
    RainPointRSSISensor,
    RainPointBatterySensor,
    RainPointFirmwareVersionSensor,
    RainPointLastUpdatedSensor,
)
from .device import RainPointSubDevice
from .hub_entities import (
    RainPointHubDeviceIDSensor,
    RainPointHubFirmwareSensor,
    RainPointHubMACSensor,
)

_LOGGER = logging.getLogger(__name__)


def _slugify(text: str) -> str:
    text = text.lower()
    text = re.sub(r"[^a-z0-9]+", "_", text)
    text = re.sub(r"_+", "_", text)
    return text.strip("_")


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    data = hass.data[DOMAIN][entry.entry_id]
    coordinator: RainPointCoordinator = data["coordinator"]

    sensors_cfg = coordinator.data.get("sensors", {})
    hubs_cfg = coordinator.data.get("hubs", [])

    entities: list[RainPointSensorBase] = []

    # Create hub entities first
    if isinstance(hubs_cfg, list):
        # Convert list to dict for easier processing
        hubs_dict = {str(hub.get("hid", i)): hub for i, hub in enumerate(hubs_cfg)}
    else:
        hubs_dict = hubs_cfg
    
    for _hub_key, hub_info in hubs_dict.items():
        # Add hub sensors
        entities.append(RainPointHubDeviceIDSensor(coordinator, hub_info))
        entities.append(RainPointHubFirmwareSensor(coordinator, hub_info))
        entities.append(RainPointHubMACSensor(coordinator, hub_info))

    # Create sensor entities for sub-devices
    for key, info in sensors_cfg.items():
        model = info.get("model")
        sub_name = info.get("sub_name") or f"Sensor {info['addr']}"
        # Use stable hardware identifiers for unique IDs to survive renames
        hid = info.get("hid", "")
        mid = info.get("mid", "")
        addr = info.get("addr", "")
        base_slug = f"{hid}_{mid}_{addr}"
        _LOGGER.debug("Creating sensor entity: key=%s, model=%s, sub_name=%s, base_slug=%s", key, model, sub_name, base_slug)

        if model == MODEL_DISPLAY_HUB:
            data = info.get("data", {})
            readings = data.get("readings", {}) if data else {}
            for reading_key, reading_val in readings.items():
                entities.append(DisplayHubReadingSensor(coordinator, key, info, base_slug, reading_key))
        elif model == MODEL_MOISTURE_SIMPLE:
            entities.append(RainPointMoisturePercentSensor(coordinator, key, info, base_slug, simple=True))
            # Add diagnostic sensors
            entities.append(RainPointRSSISensor(coordinator, key, info, base_slug))
            entities.append(RainPointBatterySensor(coordinator, key, info, base_slug))
            entities.append(RainPointFirmwareVersionSensor(coordinator, key, info, base_slug))
            entities.append(RainPointLastUpdatedSensor(coordinator, key, info, base_slug))
        elif model == MODEL_MOISTURE_FULL:
            entities.append(RainPointMoisturePercentSensor(coordinator, key, info, base_slug, simple=False))
            entities.append(RainPointTemperatureSensor(coordinator, key, info, base_slug))
            entities.append(RainPointIlluminanceSensor(coordinator, key, info, base_slug))
            # Add diagnostic sensors
            entities.append(RainPointRSSISensor(coordinator, key, info, base_slug))
            entities.append(RainPointBatterySensor(coordinator, key, info, base_slug))
            entities.append(RainPointFirmwareVersionSensor(coordinator, key, info, base_slug))
            entities.append(RainPointLastUpdatedSensor(coordinator, key, info, base_slug))
        elif model == MODEL_RAIN:
            entities.append(RainPointRainSensor(coordinator, key, info, base_slug, "rain_last_hour_mm", "rain last hour"))
            entities.append(RainPointRainSensor(coordinator, key, info, base_slug, "rain_last_24h_mm", "rain last 24h"))
            entities.append(RainPointRainSensor(coordinator, key, info, base_slug, "rain_last_7d_mm", "rain last 7d"))
            entities.append(RainPointRainSensor(coordinator, key, info, base_slug, "rain_total_mm", "rain total"))
        elif model == MODEL_TEMPHUM:
            entities.append(RainPointTempHumCurrentSensor(coordinator, key, info, base_slug))
            entities.append(RainPointTempHumHighSensor(coordinator, key, info, base_slug))
            entities.append(RainPointTempHumLowSensor(coordinator, key, info, base_slug))
            entities.append(RainPointTempHumHumidityCurrentSensor(coordinator, key, info, base_slug))
            entities.append(RainPointTempHumHumidityHighSensor(coordinator, key, info, base_slug))
            entities.append(RainPointTempHumHumidityLowSensor(coordinator, key, info, base_slug))
        elif model == MODEL_FLOWMETER:
            entities.append(RainPointFlowCurrentUsedSensor(coordinator, key, info, base_slug))
            entities.append(RainPointFlowCurrentDurationSensor(coordinator, key, info, base_slug))
            entities.append(RainPointFlowLastUsedSensor(coordinator, key, info, base_slug))
            entities.append(RainPointFlowLastUsedDurationSensor(coordinator, key, info, base_slug))
            entities.append(RainPointFlowTotalTodaySensor(coordinator, key, info, base_slug))
            entities.append(RainPointFlowTotalSensor(coordinator, key, info, base_slug))
            entities.append(RainPointFlowBatterySensor(coordinator, key, info, base_slug))
        elif model == MODEL_CO2:
            entities.append(RainPointCO2Sensor(coordinator, key, info, base_slug))
            entities.append(RainPointCO2LowSensor(coordinator, key, info, base_slug))
            entities.append(RainPointCO2HighSensor(coordinator, key, info, base_slug))
            entities.append(RainPointCO2TempSensor(coordinator, key, info, base_slug))
            entities.append(RainPointCO2HumiditySensor(coordinator, key, info, base_slug))
            entities.append(RainPointCO2BatterySensor(coordinator, key, info, base_slug))
        elif model == MODEL_POOL:
            entities.append(RainPointPoolCurrentTempSensor(coordinator, key, info, base_slug))
            entities.append(RainPointPoolHighTempSensor(coordinator, key, info, base_slug))
            entities.append(RainPointPoolLowTempSensor(coordinator, key, info, base_slug))
            entities.append(RainPointPoolBatterySensor(coordinator, key, info, base_slug))
        elif model == MODEL_POOL_PLUS:
            entities.append(RainPointPoolPlusPoolCurrentTempSensor(coordinator, key, info, base_slug))
            entities.append(RainPointPoolPlusPoolHighTempSensor(coordinator, key, info, base_slug))
            entities.append(RainPointPoolPlusPoolLowTempSensor(coordinator, key, info, base_slug))
            entities.append(RainPointPoolPlusAmbientCurrentTempSensor(coordinator, key, info, base_slug))
            entities.append(RainPointPoolPlusAmbientHighTempSensor(coordinator, key, info, base_slug))
            entities.append(RainPointPoolPlusAmbientLowTempSensor(coordinator, key, info, base_slug))
            entities.append(RainPointPoolPlusHumidityCurrentSensor(coordinator, key, info, base_slug))
            entities.append(RainPointPoolPlusHumidityHighSensor(coordinator, key, info, base_slug))
            entities.append(RainPointPoolPlusHumidityLowSensor(coordinator, key, info, base_slug))
        
        # New HCS sensor models
        elif model == MODEL_HCS005FRF:
            # Moisture-only sensor - same as MODEL_MOISTURE_SIMPLE
            entities.append(RainPointMoisturePercentSensor(coordinator, key, info, base_slug, simple=True))
        elif model == MODEL_HCS003FRF:
            # Moisture-only sensor - same as MODEL_MOISTURE_SIMPLE
            entities.append(RainPointMoisturePercentSensor(coordinator, key, info, base_slug, simple=True))
        elif model == MODEL_HCS024FRF_V1:
            # Multi-sensor (temp+moisture+lux) - same as MODEL_MOISTURE_FULL
            entities.append(RainPointMoisturePercentSensor(coordinator, key, info, base_slug, simple=False))
            entities.append(RainPointTemperatureSensor(coordinator, key, info, base_slug))
            entities.append(RainPointIlluminanceSensor(coordinator, key, info, base_slug))
        elif model == MODEL_HCS015ARF:
            # Pool temperature sensor - same as MODEL_POOL
            entities.append(RainPointPoolCurrentTempSensor(coordinator, key, info, base_slug))
            entities.append(RainPointPoolHighTempSensor(coordinator, key, info, base_slug))
            entities.append(RainPointPoolLowTempSensor(coordinator, key, info, base_slug))
            entities.append(RainPointPoolBatterySensor(coordinator, key, info, base_slug))
        elif model == MODEL_HCS0528ARF:
            # Pool temperature sensor - same as MODEL_POOL
            entities.append(RainPointPoolCurrentTempSensor(coordinator, key, info, base_slug))
            entities.append(RainPointPoolHighTempSensor(coordinator, key, info, base_slug))
            entities.append(RainPointPoolLowTempSensor(coordinator, key, info, base_slug))
            entities.append(RainPointPoolBatterySensor(coordinator, key, info, base_slug))
        elif model == MODEL_HCS027ARF:
            # Temperature/humidity sensor - same as MODEL_TEMPHUM
            entities.append(RainPointTempHumCurrentSensor(coordinator, key, info, base_slug))
            entities.append(RainPointTempHumHighSensor(coordinator, key, info, base_slug))
            entities.append(RainPointTempHumLowSensor(coordinator, key, info, base_slug))
            entities.append(RainPointTempHumHumidityCurrentSensor(coordinator, key, info, base_slug))
            entities.append(RainPointTempHumHumidityHighSensor(coordinator, key, info, base_slug))
            entities.append(RainPointTempHumHumidityLowSensor(coordinator, key, info, base_slug))
        elif model == MODEL_HCS016ARF:
            # Temperature/humidity sensor - same as MODEL_TEMPHUM
            entities.append(RainPointTempHumCurrentSensor(coordinator, key, info, base_slug))
            entities.append(RainPointTempHumHighSensor(coordinator, key, info, base_slug))
            entities.append(RainPointTempHumLowSensor(coordinator, key, info, base_slug))
            entities.append(RainPointTempHumHumidityCurrentSensor(coordinator, key, info, base_slug))
            entities.append(RainPointTempHumHumidityHighSensor(coordinator, key, info, base_slug))
            entities.append(RainPointTempHumHumidityLowSensor(coordinator, key, info, base_slug))
        elif model == MODEL_HCS044FRF:
            # Multi-sensor device - same as MODEL_MOISTURE_FULL
            entities.append(RainPointMoisturePercentSensor(coordinator, key, info, base_slug, simple=False))
            entities.append(RainPointTemperatureSensor(coordinator, key, info, base_slug))
            entities.append(RainPointIlluminanceSensor(coordinator, key, info, base_slug))
        elif model == MODEL_HCS666FRF:
            # Sensor variant (similar to HCS021FRF) - same as MODEL_MOISTURE_FULL
            entities.append(RainPointMoisturePercentSensor(coordinator, key, info, base_slug, simple=False))
            entities.append(RainPointTemperatureSensor(coordinator, key, info, base_slug))
            entities.append(RainPointIlluminanceSensor(coordinator, key, info, base_slug))
        elif model == MODEL_HCS666RFR_P:
            # Sensor variant with plus features - same as MODEL_MOISTURE_FULL
            entities.append(RainPointMoisturePercentSensor(coordinator, key, info, base_slug, simple=False))
            entities.append(RainPointTemperatureSensor(coordinator, key, info, base_slug))
            entities.append(RainPointIlluminanceSensor(coordinator, key, info, base_slug))
        elif model == MODEL_HCS999FRF:
            # Advanced sensor variant - same as MODEL_MOISTURE_FULL
            entities.append(RainPointMoisturePercentSensor(coordinator, key, info, base_slug, simple=False))
            entities.append(RainPointTemperatureSensor(coordinator, key, info, base_slug))
            entities.append(RainPointIlluminanceSensor(coordinator, key, info, base_slug))
        elif model == MODEL_HCS999FRF_P:
            # Advanced sensor variant with plus features - same as MODEL_MOISTURE_FULL
            entities.append(RainPointMoisturePercentSensor(coordinator, key, info, base_slug, simple=False))
            entities.append(RainPointTemperatureSensor(coordinator, key, info, base_slug))
            entities.append(RainPointIlluminanceSensor(coordinator, key, info, base_slug))
        elif model == MODEL_HCS666FRF_X:
            # Extended sensor variant - same as MODEL_MOISTURE_FULL
            entities.append(RainPointMoisturePercentSensor(coordinator, key, info, base_slug, simple=False))
            entities.append(RainPointTemperatureSensor(coordinator, key, info, base_slug))
            entities.append(RainPointIlluminanceSensor(coordinator, key, info, base_slug))
        elif model == MODEL_HCS701B:
            # Wall-mounted sensor - same as MODEL_TEMPHUM
            entities.append(RainPointTempHumCurrentSensor(coordinator, key, info, base_slug))
            entities.append(RainPointTempHumHighSensor(coordinator, key, info, base_slug))
            entities.append(RainPointTempHumLowSensor(coordinator, key, info, base_slug))
            entities.append(RainPointTempHumHumidityCurrentSensor(coordinator, key, info, base_slug))
            entities.append(RainPointTempHumHumidityHighSensor(coordinator, key, info, base_slug))
            entities.append(RainPointTempHumHumidityLowSensor(coordinator, key, info, base_slug))
        elif model == MODEL_HCS596WB:
            # Weather station base - same as MODEL_TEMPHUM
            entities.append(RainPointTempHumCurrentSensor(coordinator, key, info, base_slug))
            entities.append(RainPointTempHumHighSensor(coordinator, key, info, base_slug))
            entities.append(RainPointTempHumLowSensor(coordinator, key, info, base_slug))
            entities.append(RainPointTempHumHumidityCurrentSensor(coordinator, key, info, base_slug))
            entities.append(RainPointTempHumHumidityHighSensor(coordinator, key, info, base_slug))
            entities.append(RainPointTempHumHumidityLowSensor(coordinator, key, info, base_slug))
        elif model == MODEL_HCS596WB_V4:
            # Weather station base v4 - same as MODEL_TEMPHUM
            entities.append(RainPointTempHumCurrentSensor(coordinator, key, info, base_slug))
            entities.append(RainPointTempHumHighSensor(coordinator, key, info, base_slug))
            entities.append(RainPointTempHumLowSensor(coordinator, key, info, base_slug))
            entities.append(RainPointTempHumHumidityCurrentSensor(coordinator, key, info, base_slug))
            entities.append(RainPointTempHumHumidityHighSensor(coordinator, key, info, base_slug))
            entities.append(RainPointTempHumHumidityLowSensor(coordinator, key, info, base_slug))
        elif model == MODEL_HCS706ARF:
            # Environmental sensor - same as MODEL_TEMPHUM
            entities.append(RainPointTempHumCurrentSensor(coordinator, key, info, base_slug))
            entities.append(RainPointTempHumHighSensor(coordinator, key, info, base_slug))
            entities.append(RainPointTempHumLowSensor(coordinator, key, info, base_slug))
            entities.append(RainPointTempHumHumidityCurrentSensor(coordinator, key, info, base_slug))
            entities.append(RainPointTempHumHumidityHighSensor(coordinator, key, info, base_slug))
            entities.append(RainPointTempHumHumidityLowSensor(coordinator, key, info, base_slug))
        elif model == MODEL_HCS802ARF:
            # Environmental sensor - same as MODEL_TEMPHUM
            entities.append(RainPointTempHumCurrentSensor(coordinator, key, info, base_slug))
            entities.append(RainPointTempHumHighSensor(coordinator, key, info, base_slug))
            entities.append(RainPointTempHumLowSensor(coordinator, key, info, base_slug))
            entities.append(RainPointTempHumHumidityCurrentSensor(coordinator, key, info, base_slug))
            entities.append(RainPointTempHumHumidityHighSensor(coordinator, key, info, base_slug))
            entities.append(RainPointTempHumHumidityLowSensor(coordinator, key, info, base_slug))
        elif model == MODEL_HCS048B:
            # Compact sensor device - same as MODEL_TEMPHUM
            entities.append(RainPointTempHumCurrentSensor(coordinator, key, info, base_slug))
            entities.append(RainPointTempHumHighSensor(coordinator, key, info, base_slug))
            entities.append(RainPointTempHumLowSensor(coordinator, key, info, base_slug))
            entities.append(RainPointTempHumHumidityCurrentSensor(coordinator, key, info, base_slug))
            entities.append(RainPointTempHumHumidityHighSensor(coordinator, key, info, base_slug))
            entities.append(RainPointTempHumHumidityLowSensor(coordinator, key, info, base_slug))
        elif model == MODEL_HCS888ARF_V1:
            # Multi-function sensor v1 - same as MODEL_TEMPHUM
            entities.append(RainPointTempHumCurrentSensor(coordinator, key, info, base_slug))
            entities.append(RainPointTempHumHighSensor(coordinator, key, info, base_slug))
            entities.append(RainPointTempHumLowSensor(coordinator, key, info, base_slug))
            entities.append(RainPointTempHumHumidityCurrentSensor(coordinator, key, info, base_slug))
            entities.append(RainPointTempHumHumidityHighSensor(coordinator, key, info, base_slug))
            entities.append(RainPointTempHumHumidityLowSensor(coordinator, key, info, base_slug))
        elif model == MODEL_HCS0600ARF:
            # Advanced environmental sensor - same as MODEL_TEMPHUM
            entities.append(RainPointTempHumCurrentSensor(coordinator, key, info, base_slug))
            entities.append(RainPointTempHumHighSensor(coordinator, key, info, base_slug))
            entities.append(RainPointTempHumLowSensor(coordinator, key, info, base_slug))
            entities.append(RainPointTempHumHumidityCurrentSensor(coordinator, key, info, base_slug))
            entities.append(RainPointTempHumHumidityHighSensor(coordinator, key, info, base_slug))
            entities.append(RainPointTempHumHumidityLowSensor(coordinator, key, info, base_slug))
        else:
            # Unknown/unsupported model - create diagnostic entity
            data = info.get("data", {})
            if data and data.get("type") == "unknown":
                entities.append(RainPointUnknownSensor(coordinator, key, info, base_slug))
        
        # Add raw payload sensor for all devices (disabled by default)
        entities.append(RainPointRawPayloadSensor(coordinator, key, info, base_slug))

    if entities:
        async_add_entities(entities)


class RainPointSensorBase(CoordinatorEntity, SensorEntity):
    """Base class for RainPoint sensors."""

    _attr_should_poll = False

    def __init__(
        self,
        coordinator: RainPointCoordinator,
        sensor_key: str,
        sensor_info: dict,
        base_slug: str,
    ) -> None:
        super().__init__(coordinator)
        self._sensor_key = sensor_key
        self._sensor_info = sensor_info
        self._base_slug = base_slug

    @property
    def _sensor_data(self) -> dict | None:
        sensors = self.coordinator.data.get("sensors", {})
        info = sensors.get(self._sensor_key)
        if not info:
            return None
        return info.get("data")

    @property
    def available(self) -> bool:
        return self._sensor_data is not None

    @property
    def device_info(self) -> dict[str, Any]:
        """Represent each subDevice as its own HA device, child of hub."""
        from .const import DOMAIN
        hid = self._sensor_info["hid"]
        mid = self._sensor_info["mid"]
        addr = self._sensor_info["addr"]
        sub_name = self._sensor_info.get("sub_name") or f"Sensor {addr}"
        model = self._sensor_info.get("model") or "Unknown"

        return {
            # Unique per subdevice
            "identifiers": {(DOMAIN, f"{hid}_{mid}_{addr}")},
            "name": f"{sub_name}",
            "manufacturer": "RainPoint",  # RainPoint is the actual device manufacturer
            "model": model,
            "via_device": (DOMAIN, f"hub_{hid}"),  # Link to parent hub
        }

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        data = self._sensor_data or {}
        attrs: dict[str, Any] = {}
        if "rssi_dbm" in data:
            attrs["rssi_dbm"] = data["rssi_dbm"]
        if "battery_percent" in data:
            attrs["battery_percent"] = data["battery_percent"]
        elif "battery_status_code" in data:
            attrs["battery_status_code"] = data["battery_status_code"]

        # Add firmware version from sensor info
        sensors = self.coordinator.data.get("sensors", {})
        info = sensors.get(self._sensor_key) or {}
        firmware_version = info.get("firmware_version")
        if firmware_version:
            attrs["firmware_version"] = firmware_version

        # Add device timestamp from decoded data
        if "device_timestamp" in data:
            attrs["device_timestamp"] = data["device_timestamp"]
            attrs["timestamp_method"] = data.get("timestamp_method")
            attrs["timestamp_source"] = data.get("timestamp_source", "server")
        elif "server_timestamp" in data:
            attrs["device_timestamp"] = data["server_timestamp"]
            attrs["timestamp_source"] = data.get("timestamp_source", "server")
        else:
            _LOGGER.debug("No timestamp found in sensor data: %s", data)
        
        # Legacy timestamp from raw_status (fallback)
        raw_status = info.get("raw_status") or {}
        ts = raw_status.get("time")
        if ts:
            try:
                dt = datetime.fromtimestamp(ts / 1000, tz=timezone.utc)
                attrs["last_updated"] = dt.isoformat()
            except Exception:  # noqa: BLE001
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
        model = sensor_info.get("model", "")
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
        attrs["report_url"] = "https://github.com/funkadelic/ha-rainpoint/issues"
        attrs["instructions"] = (
            "This sensor model is not yet supported. "
            "Please open a GitHub issue with the model and raw_payload values above."
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
