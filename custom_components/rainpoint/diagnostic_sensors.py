"""Diagnostic sensors for RainPoint devices."""

from __future__ import annotations

import logging
import re
from datetime import datetime
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.const import PERCENTAGE, SIGNAL_STRENGTH_DECIBELS_MILLIWATT, EntityCategory
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .coordinator import RainPointCoordinator

_LOGGER = logging.getLogger(__name__)

# Candidate field names that may carry the RainPoint device ID, in priority order.
_DEVICE_ID_FIELDS: tuple[str, ...] = (
    "device_id",
    "deviceId",
    "id",
    "deviceID",
    "sub_device_id",
    "subDeviceId",
    "subDeviceID",
    "device_sn",
    "deviceSN",
    "serial_number",
    "serialNumber",
    "addr",
    "address",
    "mac",
    "mac_address",
)

# RainPoint device IDs in this family are 9+ digit numeric values.
_DEVICE_ID_RE = re.compile(r"\b\d{9,}\b")


def _looks_like_device_id(value: Any) -> bool:
    """Return True when value is an int or string of 9+ digits."""
    if not isinstance(value, (int, str)):
        return False
    text = str(value)
    return text.isdigit() and len(text) >= 9


def _find_device_id_in_dict(source: dict, sensor_key: str, source_label: str) -> str | int | None:
    """Scan source for any candidate device-id field that looks valid."""
    for field in _DEVICE_ID_FIELDS:
        device_id = source.get(field)
        if not device_id:
            continue
        _LOGGER.debug("Found device ID %s in %s: %s", field, source_label, device_id)
        if _looks_like_device_id(device_id):
            return device_id
    _LOGGER.debug("No matching device ID field in %s for %s", source_label, sensor_key)
    return None


def _find_device_id_in_raw_payload(raw_payload: Any) -> int | None:
    """Find the first 9+ digit run starting with '1' in a raw payload string.

    RainPoint device IDs for this family are 10-11 digits and always start
    with "1"; the startswith("1") filter relies on that convention.
    """
    if not isinstance(raw_payload, str):
        return None
    matches = _DEVICE_ID_RE.findall(raw_payload)
    if not matches:
        return None
    _LOGGER.debug("Found potential device IDs in raw payload: %s", matches)
    for match in matches:
        if match.startswith("1"):
            return int(match)
    return None


class RainPointDiagnosticSensorBase(CoordinatorEntity, SensorEntity):
    """Base class for RainPoint diagnostic sensors."""

    _attr_should_poll = False
    _attr_entity_category = EntityCategory.DIAGNOSTIC

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
        """Represent each subDevice as its own HA device."""
        from .const import DOMAIN

        hid = self._sensor_info["hid"]
        mid = self._sensor_info["mid"]
        addr = self._sensor_info["addr"]
        sub_name = self._sensor_info.get("sub_name") or f"Sensor {addr}"
        model = self._sensor_info.get("model") or "Unknown"
        return {
            "identifiers": {(DOMAIN, f"{hid}_{mid}_{addr}")},
            "name": sub_name,
            "manufacturer": "RainPoint",
            "model": model,
        }


class RainPointDeviceIDSensor(RainPointDiagnosticSensorBase):
    """Device ID diagnostic sensor."""

    _attr_icon = "mdi:identifier"

    def __init__(self, coordinator, sensor_key, sensor_info, base_slug):
        super().__init__(coordinator, sensor_key, sensor_info, base_slug)
        sub_name = sensor_info.get("sub_name") or "Sensor"
        self._attr_unique_id = f"rainpoint_{base_slug}_device_id"
        self._attr_name = f"{sub_name} Device ID"

    @property
    def native_value(self) -> str | int | None:
        sensors = self.coordinator.data.get("sensors", {})
        info = sensors.get(self._sensor_key)
        if not info:
            return None

        _LOGGER.debug("Available fields for %s: %s", self._sensor_key, list(info.keys()))

        device_id = _find_device_id_in_dict(info, self._sensor_key, "sensor info")
        if device_id is not None:
            return device_id

        decoded_data = info.get("data") or {}
        if decoded_data:
            _LOGGER.debug("Checking decoded data: %s", list(decoded_data.keys()))
            device_id = _find_device_id_in_dict(decoded_data, self._sensor_key, "decoded data")
            if device_id is not None:
                return device_id

            raw_match = _find_device_id_in_raw_payload(decoded_data.get("raw_value"))
            if raw_match is not None:
                return raw_match

        _LOGGER.debug("No device ID found for %s, available info: %s", self._sensor_key, info)
        return None


class RainPointRSSISensor(RainPointDiagnosticSensorBase):
    """RSSI diagnostic sensor."""

    _attr_device_class = SensorDeviceClass.SIGNAL_STRENGTH
    _attr_native_unit_of_measurement = SIGNAL_STRENGTH_DECIBELS_MILLIWATT
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:wifi"

    def __init__(self, coordinator, sensor_key, sensor_info, base_slug):
        super().__init__(coordinator, sensor_key, sensor_info, base_slug)
        sub_name = sensor_info.get("sub_name") or "Sensor"
        self._attr_unique_id = f"rainpoint_{base_slug}_rssi"
        self._attr_name = f"{sub_name} Signal Strength"

    @property
    def native_value(self) -> int | None:
        data = self._sensor_data
        if data:
            return data.get("rssi_dbm")
        return None


class RainPointBatterySensor(RainPointDiagnosticSensorBase):
    """Battery diagnostic sensor."""

    _attr_device_class = SensorDeviceClass.BATTERY
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:battery"

    def __init__(self, coordinator, sensor_key, sensor_info, base_slug):
        super().__init__(coordinator, sensor_key, sensor_info, base_slug)
        sub_name = sensor_info.get("sub_name") or "Sensor"
        self._attr_unique_id = f"rainpoint_{base_slug}_battery"
        self._attr_name = f"{sub_name} Battery"

    @property
    def native_value(self) -> int | None:
        data = self._sensor_data
        if data:
            return data.get("battery_percent")
        return None


class RainPointFirmwareVersionSensor(RainPointDiagnosticSensorBase):
    """Firmware version diagnostic sensor."""

    _attr_icon = "mdi:chip"

    def __init__(self, coordinator, sensor_key, sensor_info, base_slug):
        super().__init__(coordinator, sensor_key, sensor_info, base_slug)
        sub_name = sensor_info.get("sub_name") or "Sensor"
        self._attr_unique_id = f"rainpoint_{base_slug}_firmware_version"
        self._attr_name = f"{sub_name} Firmware Version"

    @property
    def native_value(self) -> str | None:
        sensors = self.coordinator.data.get("sensors", {})
        info = sensors.get(self._sensor_key)
        if info:
            return info.get("firmware_version")
        return None


class RainPointLastUpdatedSensor(RainPointDiagnosticSensorBase):
    """Last updated timestamp diagnostic sensor."""

    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_icon = "mdi:clock-outline"

    def __init__(self, coordinator, sensor_key, sensor_info, base_slug):
        super().__init__(coordinator, sensor_key, sensor_info, base_slug)
        sub_name = sensor_info.get("sub_name") or "Sensor"
        self._attr_unique_id = f"rainpoint_{base_slug}_last_updated"
        self._attr_name = f"{sub_name} Last Updated"

    @property
    def native_value(self) -> datetime | None:
        data = self._sensor_data
        if data and "device_timestamp" in data:
            try:
                # Parse ISO format timestamp
                return datetime.fromisoformat(data["device_timestamp"].replace("Z", "+00:00"))
            except (ValueError, AttributeError):
                pass
        return None
