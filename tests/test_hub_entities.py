"""Tests for hub entity classes (hub_entities.py)."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from custom_components.rainpoint.hub_entities import (
    RainPointHubBroadcastSwitch,
    RainPointHubChannelSelect,
    RainPointHubDeviceIDSensor,
    RainPointHubFirmwareSensor,
    RainPointHubMACSensor,
    RainPointHubRSSISensor,
)


def _make_coordinator():
    """Return a minimal mock coordinator."""
    coord = MagicMock()
    coord.data = {"hubs": [], "sensors": {}, "status": {}}
    return coord


def _make_hub_info(hid=100, name="Test Hub", soft_ver="2.0", mac="AA:BB:CC"):
    """Make hub info helper."""
    return {
        "hid": hid,
        "name": name,
        "softVer": soft_ver,
        "mac": mac,
        "model": "HTV0540FRF",
        "hardwareVersion": "1.0",
    }


class TestRainPointHubRSSISensor:
    """Tests for hub RSSI sensor."""

    def _make(self):
        """Make helper."""
        coord = _make_coordinator()
        hub_info = _make_hub_info()
        sensor = RainPointHubRSSISensor.__new__(RainPointHubRSSISensor)
        RainPointHubRSSISensor.__init__(sensor, coord, hub_info)
        return sensor

    def test_native_value_is_none(self):
        """Hub RSSI sensor always returns None (not yet available from API)."""
        sensor = self._make()
        assert sensor.native_value is None

    def test_available_is_true(self):
        """Hub sensors are always available."""
        sensor = self._make()
        assert sensor.available is True

    def test_unique_id_ends_with_rssi(self):
        """unique_id should end with '_rssi'."""
        sensor = self._make()
        assert "_rssi" in sensor._attr_unique_id

    def test_name_contains_signal_strength(self):
        """name should describe signal strength."""
        sensor = self._make()
        assert "Signal Strength" in sensor._attr_name


class TestRainPointHubDeviceIDSensor:
    """Tests for hub device ID sensor."""

    def _make(self, hid=100):
        """Make helper."""
        coord = _make_coordinator()
        hub_info = _make_hub_info(hid=hid)
        sensor = RainPointHubDeviceIDSensor.__new__(RainPointHubDeviceIDSensor)
        RainPointHubDeviceIDSensor.__init__(sensor, coord, hub_info)
        return sensor

    def test_native_value_returns_hid(self):
        """native_value should return the hub hid."""
        sensor = self._make(hid=100)
        assert sensor.native_value == 100

    def test_unique_id_contains_device_id(self):
        """unique_id should contain 'device_id'."""
        sensor = self._make()
        assert "device_id" in sensor._attr_unique_id


class TestRainPointHubFirmwareSensor:
    """Tests for hub firmware version sensor."""

    def _make(self, soft_ver="2.0"):
        """Make helper."""
        coord = _make_coordinator()
        hub_info = _make_hub_info(soft_ver=soft_ver)
        sensor = RainPointHubFirmwareSensor.__new__(RainPointHubFirmwareSensor)
        RainPointHubFirmwareSensor.__init__(sensor, coord, hub_info)
        return sensor

    def test_native_value_returns_soft_ver(self):
        """native_value should return softVer from hub_info."""
        sensor = self._make(soft_ver="2.5")
        assert sensor.native_value == "2.5"

    def test_native_value_none_when_missing(self):
        """native_value should be None if softVer is missing."""
        coord = _make_coordinator()
        hub_info = {"hid": 100, "name": "Hub"}  # no softVer
        sensor = RainPointHubFirmwareSensor.__new__(RainPointHubFirmwareSensor)
        RainPointHubFirmwareSensor.__init__(sensor, coord, hub_info)
        assert sensor.native_value is None

    def test_unique_id_contains_firmware(self):
        """unique_id should contain 'firmware'."""
        sensor = self._make()
        assert "firmware" in sensor._attr_unique_id


class TestRainPointHubMACSensor:
    """Tests for hub MAC address sensor."""

    def _make(self, mac="AA:BB:CC:DD:EE:FF"):
        """Make helper."""
        coord = _make_coordinator()
        hub_info = _make_hub_info(mac=mac)
        sensor = RainPointHubMACSensor.__new__(RainPointHubMACSensor)
        RainPointHubMACSensor.__init__(sensor, coord, hub_info)
        return sensor

    def test_native_value_returns_mac(self):
        """native_value should return the mac from hub_info."""
        sensor = self._make(mac="11:22:33:44:55:66")
        assert sensor.native_value == "11:22:33:44:55:66"

    def test_unique_id_contains_mac(self):
        """unique_id should contain 'mac'."""
        sensor = self._make()
        assert "mac" in sensor._attr_unique_id


class TestRainPointHubChannelSelect:
    """Tests for hub RF channel select entity."""

    def _make(self):
        """Make helper."""
        coord = _make_coordinator()
        hub_info = _make_hub_info()
        select = RainPointHubChannelSelect.__new__(RainPointHubChannelSelect)
        RainPointHubChannelSelect.__init__(select, coord, hub_info)
        return select

    def test_options_has_16_items(self):
        """Channel select should offer channels 1 through 16."""
        select = self._make()
        assert len(select._attr_options) == 16

    def test_options_include_all_channels(self):
        """Options should be '1' through '16' as strings."""
        select = self._make()
        for i in range(1, 17):
            assert str(i) in select._attr_options

    def test_current_option_initially_none(self):
        """Current option should be None initially (API doesn't read it)."""
        select = self._make()
        assert select.current_option is None

    def test_available_is_true(self):
        """Channel select should always be available."""
        select = self._make()
        assert select.available is True

    @pytest.mark.asyncio
    async def test_async_select_option_raises(self):
        """Selecting an option should raise an error (not yet supported)."""
        select = self._make()
        # HomeAssistantError is stubbed as a real Exception subclass in conftest
        from homeassistant.exceptions import HomeAssistantError
        with pytest.raises(HomeAssistantError):
            await select.async_select_option("5")


class TestRainPointHubBroadcastSwitch:
    """Tests for hub broadcast switch entity."""

    def _make(self):
        """Make helper."""
        coord = _make_coordinator()
        hub_info = _make_hub_info()
        switch = RainPointHubBroadcastSwitch.__new__(RainPointHubBroadcastSwitch)
        RainPointHubBroadcastSwitch.__init__(switch, coord, hub_info)
        return switch

    def test_is_on_initially_none(self):
        """is_on should be None initially."""
        switch = self._make()
        assert switch.is_on is None

    def test_available_is_true(self):
        """Broadcast switch should always be available."""
        switch = self._make()
        assert switch.available is True

    @pytest.mark.asyncio
    async def test_turn_on_raises(self):
        """async_turn_on should raise HomeAssistantError."""
        switch = self._make()
        from homeassistant.exceptions import HomeAssistantError
        with pytest.raises(HomeAssistantError):
            await switch.async_turn_on()

    @pytest.mark.asyncio
    async def test_turn_off_raises(self):
        """async_turn_off should raise HomeAssistantError."""
        switch = self._make()
        from homeassistant.exceptions import HomeAssistantError
        with pytest.raises(HomeAssistantError):
            await switch.async_turn_off()

    def test_unique_id_contains_broadcast(self):
        """unique_id should contain 'broadcast'."""
        switch = self._make()
        assert "broadcast" in switch._attr_unique_id
