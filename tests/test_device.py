"""Tests for device base classes (device.py)."""

from __future__ import annotations

from custom_components.rainpoint.const import DOMAIN
from custom_components.rainpoint.device import RainPointHubDevice, RainPointSubDevice


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
        # RainPointHubDevice inherits from Entity stub — use __new__ to bypass
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


class TestRainPointSubDevice:
    """Tests for RainPointSubDevice."""

    def _make_sub(self, hid=100, mid=200, addr=1, sub_name="Sensor 1", model="HCS026FRF"):
        """Create a RainPointSubDevice."""
        hub_info = {"hid": hid}
        sub_device_info = {
            "mid": mid,
            "addr": addr,
            "sub_name": sub_name,
            "model": model,
            "softVer": "1.0",
        }
        sub = RainPointSubDevice.__new__(RainPointSubDevice)
        RainPointSubDevice.__init__(sub, hub_info=hub_info, sub_device_info=sub_device_info)
        return sub

    def test_sub_device_info_identifiers(self):
        """device_info should contain {hid}_{mid}_{addr} identifier."""
        sub = self._make_sub(hid=100, mid=200, addr=1)
        info = sub.device_info
        assert (DOMAIN, "100_200_1") in info["identifiers"]

    def test_sub_device_info_name(self):
        """device_info name should match sub_name."""
        sub = self._make_sub(sub_name="Sensor 1")
        assert sub.device_info["name"] == "Sensor 1"

    def test_sub_device_info_manufacturer(self):
        """device_info manufacturer should be 'RainPoint'."""
        sub = self._make_sub()
        assert sub.device_info["manufacturer"] == "RainPoint"

    def test_sub_device_info_model(self):
        """device_info model should match sub_device_info model."""
        sub = self._make_sub(model="HCS026FRF")
        assert sub.device_info["model"] == "HCS026FRF"

    def test_sub_device_info_via_device(self):
        """device_info should link to parent hub via via_device."""
        sub = self._make_sub(hid=100)
        assert sub.device_info["via_device"] == (DOMAIN, "hub_100")

    def test_sub_device_info_name_fallback(self):
        """If sub_name is absent, device_info name should fall back to 'Device {addr}'."""
        hub_info = {"hid": 100}
        sub_device_info = {"mid": 200, "addr": 7, "model": "Unknown", "softVer": "1.0"}
        sub = RainPointSubDevice.__new__(RainPointSubDevice)
        RainPointSubDevice.__init__(sub, hub_info=hub_info, sub_device_info=sub_device_info)
        assert sub.device_info["name"] == "Device 7"
