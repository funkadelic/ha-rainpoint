"""Device representation for RainPoint hubs and sub-devices."""

from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import Entity

from .const import DOMAIN


def build_sub_device_info(sensor_info: dict, *, name_fallback: str) -> DeviceInfo:
    """Return the device registry entry for one sub-device.

    Every module that owns sub-device entities routes through here, so the
    device page carries the same identity, firmware and hub link no matter
    which platform registered the device first. Five modules used to build
    this dict inline and had drifted: none carried the firmware the
    coordinator already had, and only the sensor platform linked the device to
    its hub. There are four direct call sites today (entity.py, valve.py,
    number.py, generic_control.py); sensor.py and diagnostic_sensors.py were
    later collapsed onto entity.py's shared RainPointSubDeviceEntity base.

    serial_number is the mid/addr pair rather than a manufacturer serial, which the
    status payloads do not carry. It is the same identity the entity unique IDs
    are built from, so it is stable across restarts and re-pairings that keep
    the device in place.

    name_fallback stays per-platform: it only applies to a device the cloud
    gave no name, and changing it would rename those devices in place.

    A function rather than a base class: the platform entities all descend
    from CoordinatorEntity with their own __init__ chains. A sub-device base
    class shipped alongside RainPointHubDevice originally and no platform ever
    inherited from it, which is how the inline copies drifted in the first
    place.

    The hub link is conditional. via_device is included only when
    sensor_info's stamped "hub_paired" field is truthy: the link is to the
    top-level record that actually carries this sub-device, and a record that
    is not a real hub yields a parentless top-level device, because the
    Bluetooth wrapper record carries no identity fields to build a device page
    from. The verdict is read off the sensor entry rather than re-derived
    here, so there is one predicate for the question rather than a third
    spelling of it. A sensor_info with no "hub_paired" key defaults to
    hub-linked, so a caller that omits the key gets today's behaviour rather
    than a silently orphaned device.

    via_device is omitted rather than passed as None. Both spellings resolve
    identically inside Home Assistant's device registry, so this is not a
    correctness distinction; the reason to prefer omission is readability at
    the call site, a key that is never constructed states "this device has no
    parent" more plainly than a key carrying None, and it is what lets a test
    assert by key membership rather than by equality against None, which would
    pass for either construction. via_device is the first and only
    conditionally-constructed key in this function; every other key is always
    present.
    """
    hid = sensor_info["hid"]
    mid = sensor_info["mid"]
    addr = sensor_info["addr"]
    optional: dict = {}
    if sensor_info.get("hub_paired", True):
        optional["via_device"] = (DOMAIN, f"hub_{hid}")
    return DeviceInfo(
        identifiers={(DOMAIN, f"{hid}_{mid}_{addr}")},
        name=sensor_info.get("sub_name") or name_fallback,
        manufacturer="RainPoint",  # RainPoint is the actual device manufacturer
        model=sensor_info.get("model") or "Unknown",
        sw_version=sensor_info.get("firmware_version"),
        serial_number=f"{mid}_{addr}",
        **optional,
    )


class RainPointHubDevice(Entity):
    """Base class for RainPoint hub devices."""

    def __init__(
        self,
        hub_info: dict,
    ) -> None:
        """Bind this entity to one hub record.

        hub_info is the raw top-level device record the coordinator collected,
        with hid and brand injected. Held by reference so a later poll's field
        changes are picked up without rebuilding the entity.
        """
        self._hub_info = hub_info
        self._attr_unique_id = f"{DOMAIN}_hub_{hub_info['hid']}"
        self._attr_name = hub_info.get("name") or "RainPoint Hub"
        self._attr_should_poll = False

    @property
    def device_info(self) -> DeviceInfo:
        """Return device registry information for this hub."""
        return DeviceInfo(
            identifiers={(DOMAIN, f"hub_{self._hub_info['hid']}")},
            name=self._hub_info.get("name") or "RainPoint Hub",
            manufacturer="RainPoint",  # RainPoint is the actual device manufacturer
            model=self._hub_info.get("model") or "Unknown",
            sw_version=self._hub_info.get("softVer"),
            hw_version=self._hub_info.get("hardwareVersion"),
            serial_number=self._hub_info.get("mac"),
        )

    @property
    def available(self) -> bool:
        return True  # Hub is always available if config exists
