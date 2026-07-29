"""Tests for hub entity classes (hub_entities.py)."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from custom_components.rainpoint.const import PUSH_LAST_MESSAGE_UNIQUE_ID_SUFFIX
from custom_components.rainpoint.hub_entities import (
    RainPointHubBroadcastSwitch,
    RainPointHubChannelSelect,
    RainPointHubDeviceIDSensor,
    RainPointHubFirmwareSensor,
    RainPointHubMACSensor,
    RainPointHubRSSISensor,
    RainPointPushLastMessageSensor,
    resolve_push_diagnostic_hubs,
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


class TestResolvePushDiagnosticHubs:
    """resolve_push_diagnostic_hubs binds the diagnostics to the client's one hub."""

    @staticmethod
    def _coord(hubs):
        coord = MagicMock()
        coord.data = {"hubs": hubs}
        return coord

    def test_no_hubs_returns_empty(self):
        assert resolve_push_diagnostic_hubs(self._coord([]), MagicMock()) == []

    def test_none_data_returns_empty(self):
        coord = MagicMock()
        coord.data = None
        assert resolve_push_diagnostic_hubs(coord, MagicMock()) == []

    def test_returns_only_the_hub_matching_the_client_mid(self):
        hubs = [{"mid": 111}, {"mid": 222}, {"mid": 333}]
        client = MagicMock()
        client.hub_mid = 222
        assert resolve_push_diagnostic_hubs(self._coord(hubs), client) == [{"mid": 222}]

    def test_falls_back_to_first_hub_when_mid_is_none(self):
        hubs = [{"mid": 111, "did": "d111"}, {"mid": 222, "did": "d222"}]
        client = MagicMock()
        client.hub_mid = None
        assert resolve_push_diagnostic_hubs(self._coord(hubs), client) == [{"mid": 111, "did": "d111"}]

    def test_falls_back_to_first_hub_when_no_hub_matches(self):
        hubs = [{"mid": 111, "did": "d111"}, {"mid": 222, "did": "d222"}]
        client = MagicMock()
        client.hub_mid = 999  # no such hub
        assert resolve_push_diagnostic_hubs(self._coord(hubs), client) == [{"mid": 111, "did": "d111"}]

    def test_fallback_skips_a_record_with_no_hub_identity(self):
        """A Bluetooth wrapper in slot 0 must not be mistaken for the bound hub.

        Pairing a Bluetooth valve adds a parent record whose identity fields are
        all empty strings. Taking hubs[0] blindly returned that record.
        """
        hubs = [{"mid": 346965, "did": "", "mac": "", "productKey": "", "model": ""}, {"mid": 236547, "did": "17053410"}]
        client = MagicMock()
        client.hub_mid = None
        assert resolve_push_diagnostic_hubs(self._coord(hubs), client) == [{"mid": 236547, "did": "17053410"}]

    def test_fallback_returns_nothing_when_no_record_is_a_hub(self):
        """All-wrapper hub list yields no push diagnostics rather than a phantom one."""
        hubs = [{"mid": 346965, "did": "", "mac": "", "productKey": "", "model": ""}]
        client = MagicMock()
        client.hub_mid = None
        assert resolve_push_diagnostic_hubs(self._coord(hubs), client) == []

    def test_accepts_dict_shaped_hubs(self):
        hubs = {"a": {"mid": 111}, "b": {"mid": 222}}
        client = MagicMock()
        client.hub_mid = 222
        assert resolve_push_diagnostic_hubs(self._coord(hubs), client) == [{"mid": 222}]


class TestRainPointHubRSSISensor:
    """Tests for hub RSSI sensor."""

    def _make(self, coord=None, hub_info=None):
        """Make helper. Defaults to an empty-status coordinator and a mid-less hub."""
        coord = _make_coordinator() if coord is None else coord
        hub_info = _make_hub_info() if hub_info is None else hub_info
        sensor = RainPointHubRSSISensor.__new__(RainPointHubRSSISensor)
        RainPointHubRSSISensor.__init__(sensor, coord, hub_info)
        return sensor

    def _make_with_state(self, mid, state_value):
        """Build a sensor whose coordinator carries a `state` status entry for mid."""
        coord = _make_coordinator()
        coord.data["status"] = {mid: {"subDeviceStatus": [{"id": "state", "value": state_value}]}}
        hub_info = _make_hub_info()
        hub_info["mid"] = mid
        return self._make(coord=coord, hub_info=hub_info)

    def test_native_value_none_when_status_missing(self):
        """With no matching status entry, the hub RSSI reads None rather than erroring."""
        sensor = self._make()
        assert sensor.native_value is None

    def test_native_value_reads_hub_rssi_from_state(self):
        """The second field of the `state` value (e.g. '0,-52') is the hub RSSI."""
        sensor = self._make_with_state(236547, "0,-52")
        assert sensor.native_value == -52

    def test_native_value_none_for_malformed_state(self):
        """A `state` value without a parseable RSSI field yields None."""
        sensor = self._make_with_state(236547, "0,notanumber")
        assert sensor.native_value is None

    def test_native_value_none_when_state_value_is_not_a_string(self):
        """A `state` entry whose value is not a string (e.g. None) yields None."""
        sensor = self._make_with_state(236547, None)
        assert sensor.native_value is None

    def test_native_value_none_when_state_has_no_rssi_field(self):
        """A `state` value with no comma-separated RSSI field yields None."""
        sensor = self._make_with_state(236547, "1")
        assert sensor.native_value is None

    def test_native_value_none_when_status_entry_is_explicitly_none(self):
        """An explicit None for the mid's status (not just a missing key) yields None, not a crash."""
        coord = _make_coordinator()
        coord.data["status"] = {236547: None}
        hub_info = _make_hub_info()
        hub_info["mid"] = 236547
        sensor = self._make(coord=coord, hub_info=hub_info)
        assert sensor.native_value is None

    def test_native_value_none_when_subdevicestatus_is_explicitly_none(self):
        """An explicit None subDeviceStatus yields None rather than iterating None."""
        coord = _make_coordinator()
        coord.data["status"] = {236547: {"subDeviceStatus": None}}
        hub_info = _make_hub_info()
        hub_info["mid"] = 236547
        sensor = self._make(coord=coord, hub_info=hub_info)
        assert sensor.native_value is None

    def test_native_value_skips_non_state_entries(self):
        """Non-`state` status entries are skipped before the `state` entry is read."""
        coord = _make_coordinator()
        coord.data["status"] = {
            236547: {"subDeviceStatus": [{"id": "connected", "value": "1"}, {"id": "state", "value": "0,-40"}]}
        }
        hub_info = _make_hub_info()
        hub_info["mid"] = 236547
        sensor = self._make(coord=coord, hub_info=hub_info)
        assert sensor.native_value == -40

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

    def _make(self, hid=100, did=None):
        """Make helper."""
        coord = _make_coordinator()
        hub_info = _make_hub_info(hid=hid)
        if did is not None:
            hub_info["did"] = did
        sensor = RainPointHubDeviceIDSensor.__new__(RainPointHubDeviceIDSensor)
        RainPointHubDeviceIDSensor.__init__(sensor, coord, hub_info)
        return sensor

    def test_native_value_returns_did(self):
        """native_value should return the device id (did), matching the vendor app."""
        sensor = self._make(hid=100, did="17053410")
        assert sensor.native_value == "17053410"

    def test_native_value_falls_back_to_hid_without_did(self):
        """When the hub record omits did, native_value falls back to the home id."""
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

    def _make(self, hub_info=None):
        """Make helper. Defaults to a hub record without the RF fields."""
        coord = _make_coordinator()
        hub_info = _make_hub_info() if hub_info is None else hub_info
        select = RainPointHubChannelSelect.__new__(RainPointHubChannelSelect)
        RainPointHubChannelSelect.__init__(select, coord, hub_info)
        return select

    def test_options_fallback_has_16_items(self):
        """With no function.RF field, options fall back to channels 1 through 16."""
        select = self._make()
        assert len(select._attr_options) == 16

    def test_options_fallback_include_all_channels(self):
        """Fallback options should be '1' through '16' as strings."""
        select = self._make()
        for i in range(1, 17):
            assert str(i) in select._attr_options

    def test_current_option_none_when_recich_absent(self):
        """Current option is None when the hub record has no recich field."""
        select = self._make()
        assert select.current_option is None

    def test_current_and_options_from_hub_record(self):
        """recich sets the current channel and function.RF (a bitmask) sets the options."""
        hub_info = _make_hub_info()
        hub_info["recich"] = 1
        hub_info["function"] = '{"model":"HWG023WBRF-V2","childMax":40,"RF":7,"SM":7,"rst":3,"SW":1}'
        select = self._make(hub_info)
        assert select.current_option == "1"
        assert select._attr_options == ["1", "2", "3"]

    def test_options_from_non_contiguous_rf_bitmask(self):
        """A non-contiguous RF bitmask maps each set bit to its channel number."""
        hub_info = _make_hub_info()
        hub_info["function"] = '{"RF":13}'  # 0b1101 -> channels 1, 3, 4
        select = self._make(hub_info)
        assert select._attr_options == ["1", "3", "4"]

    def test_current_channel_outside_mask_is_still_offered(self):
        """A current channel not present in the RF mask is added to the options."""
        hub_info = _make_hub_info()
        hub_info["recich"] = 9
        hub_info["function"] = '{"RF":7}'  # channels 1, 2, 3
        select = self._make(hub_info)
        assert select.current_option == "9"
        assert select._attr_options == ["1", "2", "3", "9"]

    def test_malformed_function_blob_falls_back_to_16(self):
        """An unparseable function blob degrades to the 1-16 fallback, not an error."""
        hub_info = _make_hub_info()
        hub_info["function"] = "not-json"
        select = self._make(hub_info)
        assert len(select._attr_options) == 16

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


class TestRainPointPushLastMessageSensor:
    """Tests for the push last-message-age timestamp entity."""

    def _make(self, last_message_at=None, now=1000.0):
        """Build the entity with an injected monotonic clock for deterministic age math."""
        mqtt_client = MagicMock()
        mqtt_client.last_message_at = last_message_at
        entity = RainPointPushLastMessageSensor(
            mqtt_client,
            _make_hub_info(),
            time_source=lambda: now,
        )
        return entity, mqtt_client

    def test_native_value_none_before_first_message(self):
        """No message yet -> native_value is None."""
        entity, _ = self._make(last_message_at=None)
        assert entity.native_value is None

    def test_native_value_is_message_wall_clock_time(self):
        """A monotonic last-message value converts to an absolute UTC datetime in the past."""
        from datetime import UTC, datetime

        # Message arrived 30s ago on the monotonic clock.
        entity, _ = self._make(last_message_at=970.0, now=1000.0)
        value = entity.native_value
        assert value is not None
        assert value.tzinfo is not None
        age = (datetime.now(UTC) - value).total_seconds()
        # Rendered timestamp is ~30s in the past (allow scheduling slack).
        assert 29.0 <= age <= 31.0

    def test_native_value_clamps_negative_age_to_now(self):
        """A last-message value slightly ahead of the clock never renders a future time."""
        from datetime import UTC, datetime

        entity, _ = self._make(last_message_at=1005.0, now=1000.0)
        value = entity.native_value
        age = (datetime.now(UTC) - value).total_seconds()
        assert -0.5 <= age <= 0.5

    def test_unique_id_and_category(self):
        entity, _ = self._make()
        assert entity._attr_unique_id.endswith(f"_{PUSH_LAST_MESSAGE_UNIQUE_ID_SUFFIX}")
        assert entity._attr_entity_category == "diagnostic"
        assert getattr(entity, "_attr_entity_registry_enabled_default", True) is True

    def test_available_true_when_client_present(self):
        entity, _ = self._make()
        assert entity.available is True

    @pytest.mark.asyncio
    async def test_registers_and_unregisters_state_listener(self):
        entity, mqtt_client = self._make()
        await entity.async_added_to_hass()
        mqtt_client.add_state_listener.assert_called_once_with(entity._handle_client_state)
        await entity.async_will_remove_from_hass()
        mqtt_client.remove_state_listener.assert_called_once_with(entity._handle_client_state)
