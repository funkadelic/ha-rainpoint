"""Device representation for RainPoint hubs and sub-devices."""
from __future__ import annotations

from typing import Any

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import Entity
from .const import DOMAIN


class RainPointHubDevice(Entity):
    """Base class for RainPoint hub devices."""

    def __init__(
        self,
        hub_info: dict,
    ) -> None:
        self._hub_info = hub_info
        self._attr_unique_id = f"{DOMAIN}_hub_{hub_info['hid']}"
        self._attr_name = hub_info.get("name", "RainPoint Hub")
        self._attr_should_poll = False

    @property
    def device_info(self) -> DeviceInfo:
        """Return device registry information for this hub."""
        return DeviceInfo(
            identifiers={(DOMAIN, f"hub_{self._hub_info['hid']}")},
            name=self._hub_info.get("name", "RainPoint Hub"),
            manufacturer="RainPoint",  # RainPoint is the actual device manufacturer
            model=self._hub_info.get("model", "Unknown"),
            sw_version=self._hub_info.get("softVer"),
            hw_version=self._hub_info.get("hardwareVersion"),
            serial_number=self._hub_info.get("mac"),
        )

    @property
    def available(self) -> bool:
        return True  # Hub is always available if config exists


class RainPointSubDevice(Entity):
    """Base class for RainPoint sub-devices (sensors, valves, etc.)."""

    def __init__(
        self,
        hub_info: dict,
        sub_device_info: dict,
    ) -> None:
        self._hub_info = hub_info
        self._sub_device_info = sub_device_info

    @property
    def device_info(self) -> DeviceInfo:
        """Return device registry information for this sub-device."""
        return DeviceInfo(
            identifiers={(DOMAIN, f"{self._hub_info['hid']}_{self._sub_device_info['mid']}_{self._sub_device_info['addr']}")},
            name=self._sub_device_info.get("sub_name") or f"Device {self._sub_device_info['addr']}",
            manufacturer="RainPoint",  # RainPoint is the actual device manufacturer
            model=self._sub_device_info.get("model", "Unknown"),
            sw_version=self._sub_device_info.get("softVer"),
            via_device=(DOMAIN, f"hub_{self._hub_info['hid']}"),  # Link to parent hub
        )
