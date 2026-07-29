"""Shared plumbing for entities that represent one RainPoint sub-device.

Every platform binds its entities to a coordinator plus a sensor key, then
reports the same firmware and timestamp attributes and resolves the same device
page. Each platform used to carry its own copy of that plumbing, and the copies
drifted: firmware never reached the device page, only the sensor platform linked
a device to its hub, and two of the copies would raise on a sub-device that has
no reading yet.

The two pieces here are split by what a platform can actually reuse.
``RainPointSubDeviceEntity`` suits the platforms whose entities are constructed
from (coordinator, sensor_key, sensor_info, base_slug); the valve, number, and
generic control entities take a zone or datapoint instead, so they keep their
own constructors and call ``sub_device_attributes`` directly.
"""

from __future__ import annotations

from typing import Any

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .coordinator import RainPointCoordinator
from .device import build_sub_device_info


def sub_device_attributes(coordinator: RainPointCoordinator, sensor_key: str) -> dict[str, Any]:
    """Return the firmware and timestamp attributes shared by every platform.

    Reads through the coordinator entry rather than a cached copy so a firmware
    change after a reload is picked up on the next state write.

    A sub-device with no reading yet has a ``data`` of None, which the previous
    per-platform copies in valve.py and number.py fed straight into a membership
    test and raised on. An absent or None reading yields the firmware attribute
    alone here.
    """
    attrs: dict[str, Any] = {}
    info = (coordinator.data or {}).get("sensors", {}).get(sensor_key) or {}

    firmware_version = info.get("firmware_version")
    if firmware_version:
        attrs["firmware_version"] = firmware_version

    data = info.get("data") or {}
    if "device_timestamp" in data:
        attrs["device_timestamp"] = data["device_timestamp"]
        attrs["timestamp_method"] = data.get("timestamp_method")
        attrs["timestamp_source"] = data.get("timestamp_source", "server")
    elif "server_timestamp" in data:
        attrs["device_timestamp"] = data["server_timestamp"]
        attrs["timestamp_source"] = data.get("timestamp_source", "server")

    return attrs


class RainPointSubDeviceEntity(CoordinatorEntity):
    """Coordinator-backed entity bound to a single sub-device.

    ``_device_name_prefix`` only ever reaches a user for a sub-device the cloud
    gave no name, so a subclass changing it would rename those devices in place.
    """

    _attr_should_poll = False
    _device_name_prefix = "Device"

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
    def device_info(self) -> DeviceInfo:
        """Represent each subDevice as its own HA device, child of hub."""
        return build_sub_device_info(
            self._sensor_info,
            name_fallback=f"{self._device_name_prefix} {self._sensor_info['addr']}",
        )
