"""Tests for device base classes (device.py)."""

from __future__ import annotations

from custom_components.rainpoint.const import DOMAIN
from custom_components.rainpoint.device import RainPointHubDevice, build_sub_device_info


class TestRainPointHubDevice:
    """Tests for RainPointHubDevice."""

    def _make_hub(self, hid=100, name="My Hub", model="HTV0540FRF"):
        """Create a RainPointHubDevice with a mock coordinator via __new__."""
        hub_info = {
            "hid": hid,
            "name": name,
            "model": model,
            "softVer": "2.0",
            "hardwareVersion": "1.0",
            "mac": "AA:BB:CC:DD:EE:FF",
        }
        # RainPointHubDevice inherits from Entity stub, so use __new__ to bypass
        # any super().__init__ that might call into MagicMock internals.
        hub = RainPointHubDevice.__new__(RainPointHubDevice)
        RainPointHubDevice.__init__(hub, hub_info)
        return hub

    def test_hub_device_info_identifiers(self):
        """device_info should contain the expected identifier tuple."""
        hub = self._make_hub(hid=100)
        info = hub.device_info
        assert (DOMAIN, "hub_100") in info["identifiers"]

    def test_hub_device_info_name(self):
        """device_info name should match hub_info name."""
        hub = self._make_hub(name="My Hub")
        assert hub.device_info["name"] == "My Hub"

    def test_hub_device_info_manufacturer(self):
        """device_info manufacturer should be 'RainPoint'."""
        hub = self._make_hub()
        assert hub.device_info["manufacturer"] == "RainPoint"

    def test_hub_device_info_model(self):
        """device_info model should match hub_info model."""
        hub = self._make_hub(model="HTV0540FRF")
        assert hub.device_info["model"] == "HTV0540FRF"

    def test_hub_available_always_true(self):
        """Hub is always available if config exists."""
        hub = self._make_hub()
        assert hub.available is True

    def test_hub_unique_id_format(self):
        """unique_id should be domain_hub_{hid}."""
        hub = self._make_hub(hid=42)
        assert hub._attr_unique_id == f"{DOMAIN}_hub_42"

    def test_hub_name_attribute(self):
        """_attr_name should match the hub name."""
        hub = self._make_hub(name="Test Hub")
        assert hub._attr_name == "Test Hub"


class TestBuildSubDeviceInfoIdentity:
    """Identity fields the builder must keep stable for every platform."""

    def test_manufacturer_is_rainpoint(self):
        """All supported hardware is RainPoint, hub and sub-device alike."""
        info = build_sub_device_info(
            {"hid": 100, "mid": 200, "addr": 1, "sub_name": "Soil Sensor", "model": "HCS026FRF"},
            name_fallback="Sensor 1",
        )
        assert info["manufacturer"] == "RainPoint"

    def test_model_is_passed_through(self):
        """The cloud's model string reaches the device page verbatim."""
        info = build_sub_device_info(
            {"hid": 100, "mid": 200, "addr": 1, "sub_name": "Soil Sensor", "model": "HCS026FRF"},
            name_fallback="Sensor 1",
        )
        assert info["model"] == "HCS026FRF"


class TestBuildSubDeviceInfo:
    """Tests for the shared sub-device DeviceInfo builder."""

    def _info(self, **overrides):
        base = {
            "hid": 100,
            "mid": 200,
            "addr": 1,
            "sub_name": "Soil Sensor",
            "model": "HCS026FRF",
            "firmware_version": "1.4",
        }
        base.update(overrides)
        return base

    def test_firmware_becomes_sw_version(self):
        """The firmware the coordinator already carries reaches the device page."""
        info = build_sub_device_info(self._info(), name_fallback="Sensor 1")
        assert info["sw_version"] == "1.4"

    def test_serial_number_is_the_mid_addr_pair(self):
        """Sub-devices have no manufacturer serial, so the stable pair stands in for one."""
        info = build_sub_device_info(self._info(mid=200, addr=7), name_fallback="Sensor 7")
        assert info["serial_number"] == "200_7"

    def test_links_to_the_parent_hub(self):
        """Every sub-device is a child of its hub, not a top-level device."""
        info = build_sub_device_info(self._info(hid=100), name_fallback="Sensor 1")
        assert info["via_device"] == (DOMAIN, "hub_100")

    def test_identifiers_are_unchanged(self):
        """The registry key keeps its existing shape so no migration is needed."""
        info = build_sub_device_info(self._info(), name_fallback="Sensor 1")
        assert (DOMAIN, "100_200_1") in info["identifiers"]

    def test_missing_firmware_leaves_sw_version_unset(self):
        """A device the cloud reports no firmware for gets no fabricated version."""
        info = build_sub_device_info(self._info(firmware_version=None), name_fallback="Sensor 1")
        assert info["sw_version"] is None

    def test_name_falls_back_only_when_unnamed(self):
        """The per-platform fallback applies only to a device the cloud did not name."""
        named = build_sub_device_info(self._info(), name_fallback="Sensor 1")
        unnamed = build_sub_device_info(self._info(sub_name=None), name_fallback="Valve Hub 1")
        assert named["name"] == "Soil Sensor"
        assert unnamed["name"] == "Valve Hub 1"

    def test_missing_model_reads_unknown(self):
        """An absent model is reported as Unknown rather than left blank."""
        info = build_sub_device_info(self._info(model=None), name_fallback="Sensor 1")
        assert info["model"] == "Unknown"
