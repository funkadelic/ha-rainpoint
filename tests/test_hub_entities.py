"""Tests for hub entity classes (hub_entities.py)."""

from __future__ import annotations

import logging
from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.rainpoint.api import RainPointApiError
from custom_components.rainpoint.const import (
    PUSH_CONNECTED_UNIQUE_ID_SUFFIX,
    PUSH_LAST_MESSAGE_UNIQUE_ID_SUFFIX,
)
from custom_components.rainpoint.coordinator import HUB_CONNECTED
from custom_components.rainpoint.hub_entities import (
    RainPointHubBroadcastButton,
    RainPointHubBroadcastSwitch,
    RainPointHubChannelSelect,
    RainPointHubConnectivityBinarySensor,
    RainPointHubDeviceIDSensor,
    RainPointHubFirmwareSensor,
    RainPointHubMACSensor,
    RainPointHubRSSISensor,
    RainPointPushConnectedBinarySensor,
    RainPointPushLastMessageSensor,
    hub_record_for_mid,
    resolve_connectivity_hubs,
    resolve_push_diagnostic_hubs,
)


def _make_coordinator():
    """Return a minimal mock coordinator."""
    coord = MagicMock()
    coord.data = {"hubs": [], "sensors": {}, "status": {}}
    return coord


def _make_hub_info(hid=100, name="Test Hub", soft_ver="2.0", mac="AA:BB:CC", mid=1001):
    """Make hub info helper."""
    return {
        "hid": hid,
        "mid": mid,
        "name": name,
        "softVer": soft_ver,
        "mac": mac,
        "model": "HTV0540FRF",
        "hardwareVersion": "1.0",
    }


class TestHubRecordForMid:
    """hub_record_for_mid resolves a hub's own live record, or {} when none matches."""

    def test_returns_the_matching_hub(self):
        """The hub whose mid matches is returned."""
        coord = _make_coordinator()
        coord.data["hubs"] = [{"mid": 1001, "param": "0|1||"}, {"mid": 1002, "param": "0|0||"}]
        assert hub_record_for_mid(coord, 1002) == {"mid": 1002, "param": "0|0||"}

    def test_no_matching_mid_returns_empty_dict(self):
        """No matching mid returns {} rather than None or raising."""
        coord = _make_coordinator()
        coord.data["hubs"] = [{"mid": 1001}]
        assert hub_record_for_mid(coord, 9999) == {}


class TestResolvePushDiagnosticHubs:
    """resolve_push_diagnostic_hubs covers every real hub on the account.

    It used to return the single hub the MQTT client was built for, because
    push reached that hub alone. The session is account-scoped and frames are
    routed by the mid they name, so every hub is covered and each gets its own
    pair of diagnostics. The client is no longer a parameter: there is nothing
    left for the resolver to ask it.
    """

    @staticmethod
    def _coord(hubs):
        coord = MagicMock()
        coord.data = {"hubs": hubs}
        return coord

    def test_no_hubs_returns_empty(self):
        """An empty hub list yields no diagnostics rather than a phantom pair."""
        assert resolve_push_diagnostic_hubs(self._coord([])) == []

    def test_none_data_returns_empty(self):
        """Before the first poll there is no hub list to build from."""
        coord = MagicMock()
        coord.data = None
        assert resolve_push_diagnostic_hubs(coord) == []

    def test_every_real_hub_gets_diagnostics(self):
        """Both hubs on a two-hub account get their own pair."""
        hubs = [{"mid": 236547, "did": "17053410"}, {"mid": 361277, "did": "17051777"}]
        assert resolve_push_diagnostic_hubs(self._coord(hubs)) == hubs

    def test_skips_a_record_with_no_hub_identity(self):
        """A Bluetooth wrapper carries no identity fields and is not a hub.

        Pairing a Bluetooth valve adds a parent record whose identity fields
        are all empty strings. It must not collect push diagnostics of its own.
        """
        wrapper = {"mid": 346965, "did": "", "mac": "", "productKey": "", "model": ""}
        real = {"mid": 236547, "did": "17053410"}
        assert resolve_push_diagnostic_hubs(self._coord([wrapper, real])) == [real]

    def test_returns_nothing_when_no_record_is_a_hub(self):
        """An all-wrapper list yields nothing rather than a phantom hub."""
        hubs = [{"mid": 346965, "did": "", "mac": "", "productKey": "", "model": ""}]
        assert resolve_push_diagnostic_hubs(self._coord(hubs)) == []

    def test_accepts_dict_shaped_hubs(self):
        """The hub collection is read through the shared shape helper, so a dict
        of records works the same as a list."""
        hubs = {"a": {"mid": 111, "did": "d111"}, "b": {"mid": 222, "did": "d222"}}
        assert resolve_push_diagnostic_hubs(self._coord(hubs)) == [{"mid": 111, "did": "d111"}, {"mid": 222, "did": "d222"}]


class TestResolveConnectivityHubs:
    """resolve_connectivity_hubs returns every real hub (unlike the push-diagnostic resolver)."""

    @staticmethod
    def _coord(hubs):
        coord = MagicMock()
        coord.data = {"hubs": hubs}
        return coord

    def test_no_hubs_returns_empty(self):
        assert resolve_connectivity_hubs(self._coord([])) == []

    def test_none_data_returns_empty(self):
        coord = MagicMock()
        coord.data = None
        assert resolve_connectivity_hubs(coord) == []

    def test_returns_every_real_hub(self):
        hubs = [{"mid": 111, "did": "d111"}, {"mid": 222, "did": "d222"}]
        assert resolve_connectivity_hubs(self._coord(hubs)) == hubs

    def test_excludes_the_bluetooth_wrapper_record(self):
        """A record whose every identity field is an empty string yields no entity."""
        hubs = [{"mid": 346965, "did": "", "mac": "", "productKey": "", "model": ""}, {"mid": 236547, "did": "17053410"}]
        assert resolve_connectivity_hubs(self._coord(hubs)) == [{"mid": 236547, "did": "17053410"}]

    def test_accepts_dict_shaped_hubs(self):
        hubs = {"a": {"mid": 111, "did": "d1"}, "b": {"mid": 222, "did": "d2"}}
        assert resolve_connectivity_hubs(self._coord(hubs)) == [{"mid": 111, "did": "d1"}, {"mid": 222, "did": "d2"}]


class TestRainPointHubConnectivityBinarySensor:
    """Tests for the hub-level cloud connectivity binary sensor entity."""

    def _make(self, coord=None, hub_info=None):
        """Make helper."""
        coord = _make_coordinator() if coord is None else coord
        hub_info = _make_hub_info() if hub_info is None else hub_info
        entity = RainPointHubConnectivityBinarySensor.__new__(RainPointHubConnectivityBinarySensor)
        RainPointHubConnectivityBinarySensor.__init__(entity, coord, hub_info)
        return entity

    def _make_with_record(self, mid, record):
        """Build an entity whose coordinator carries a hub_connectivity record for mid."""
        coord = _make_coordinator()
        coord.data["hub_connectivity"] = {mid: record}
        hub_info = _make_hub_info()
        hub_info["mid"] = mid
        return self._make(coord=coord, hub_info=hub_info)

    def test_is_on_true_when_connected(self):
        entity = self._make_with_record(200, {"state": "connected", "changed_at": None, "state_raw": None})
        assert entity.is_on is True

    def test_is_on_false_when_disconnected(self):
        entity = self._make_with_record(200, {"state": "disconnected", "changed_at": None, "state_raw": None})
        assert entity.is_on is False

    def test_is_on_none_when_unknown(self):
        entity = self._make_with_record(200, {"state": "unknown", "changed_at": None, "state_raw": None})
        assert entity.is_on is None

    def test_icon_tracks_connected_state(self):
        """A connected hub must not show a cloud-offline glyph.

        Pins the reason this is an icon property rather than a class-level
        _attr_icon: a fixed attribute overrides the CONNECTIVITY device
        class's own on/off pair, so the one entity whose job is an
        at-a-glance health check would read as a false alarm while healthy.
        """
        entity = self._make_with_record(200, {"state": "connected", "changed_at": None, "state_raw": None})
        assert entity.icon == "mdi:cloud-check-variant"

    def test_icon_tracks_disconnected_state(self):
        """A disconnected hub shows the offline glyph."""
        entity = self._make_with_record(200, {"state": "disconnected", "changed_at": None, "state_raw": None})
        assert entity.icon == "mdi:cloud-off-outline"

    def test_icon_falls_back_to_offline_glyph_when_unknown(self):
        """Unknown shares the offline glyph rather than claiming a healthy cloud."""
        entity = self._make_with_record(200, {"state": "unknown", "changed_at": None, "state_raw": None})
        assert entity.icon == "mdi:cloud-off-outline"

    def test_is_on_none_when_no_record_for_mid(self):
        """A coordinator snapshot with a hub_connectivity key that omits this mid."""
        coord = _make_coordinator()
        coord.data["hub_connectivity"] = {}
        hub_info = _make_hub_info()
        hub_info["mid"] = 200
        entity = self._make(coord=coord, hub_info=hub_info)
        assert entity.is_on is None

    def test_is_on_none_when_coordinator_data_is_none(self):
        coord = MagicMock()
        coord.data = None
        hub_info = _make_hub_info()
        hub_info["mid"] = 200
        entity = self._make(coord=coord, hub_info=hub_info)
        assert entity.is_on is None

    def test_is_on_none_when_coordinator_data_is_empty_dict(self):
        coord = MagicMock()
        coord.data = {}
        hub_info = _make_hub_info()
        hub_info["mid"] = 200
        entity = self._make(coord=coord, hub_info=hub_info)
        assert entity.is_on is None

    def test_is_on_none_and_attributes_present_when_hub_connectivity_key_absent(self):
        """A coordinator snapshot carrying hubs and sensors but no hub_connectivity
        key at all -- the shape every pre-existing test fake in this suite produces,
        so it is the shape most likely to break in a live reload after a partial
        upgrade."""
        coord = MagicMock()
        coord.data = {"hubs": [], "sensors": {}}
        hub_info = _make_hub_info()
        hub_info["mid"] = 200
        entity = self._make(coord=coord, hub_info=hub_info)
        assert entity.is_on is None
        assert entity.extra_state_attributes == {"changed_at": None, "state_raw": None}

    def test_extra_state_attributes_carries_both_keys_even_when_absent(self):
        """Both keys are present with None values, not simply missing."""
        hub_info = _make_hub_info()
        hub_info["mid"] = 200
        entity = self._make(hub_info=hub_info)
        assert entity.extra_state_attributes == {"changed_at": None, "state_raw": None}

    def test_extra_state_attributes_reads_changed_at_and_state_raw(self):
        entity = self._make_with_record(
            200, {"state": "connected", "changed_at": "2026-07-30T19:22:44+00:00", "state_raw": "0,-52"}
        )
        assert entity.extra_state_attributes == {"changed_at": "2026-07-30T19:22:44+00:00", "state_raw": "0,-52"}

    def test_available_is_true(self):
        entity = self._make()
        assert entity.available is True

    def test_unique_id_carries_both_hid_and_mid(self):
        """The spelling every hub sibling now shares, and the one that did not move.

        This entity has carried both segments since it shipped. The re-key
        brought the other seven onto it rather than the other way round, so
        this assertion is byte-identical to what it was before: a change here
        would mean the migration moved a row it had no business moving.
        """
        hub_info = _make_hub_info(hid=100)
        hub_info["mid"] = 200
        entity = self._make(hub_info=hub_info)
        assert entity._attr_unique_id == "rainpoint_hub_100_200_connectivity"

    def test_name_ends_with_cloud_connection(self):
        entity = self._make()
        assert entity._attr_name == "Cloud Connection"

    def test_entity_category_is_diagnostic(self):
        entity = self._make()
        assert entity._attr_entity_category == "diagnostic"


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
        assert sensor._attr_name == "Signal Strength"


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
        """native_value should return the device id (did), matching the RainPoint app."""
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
        hub_info = {"hid": 100, "mid": 1001, "name": "Hub"}  # no softVer
        sensor = RainPointHubFirmwareSensor.__new__(RainPointHubFirmwareSensor)
        RainPointHubFirmwareSensor.__init__(sensor, coord, hub_info)
        assert sensor.native_value is None

    def test_native_value_follows_an_upgrade(self):
        """A version that moved after the entity was built is the one reported.

        The regression this sensor shipped with: self._hub_info is the
        build-time snapshot and nothing reassigns it, so an upgrade landing in
        a later poll left the sensor reporting the pre-upgrade version while
        the update entity beside it reported the new one.
        """
        coord = _make_coordinator()
        hub_info = _make_hub_info(soft_ver="1.1.1032")
        sensor = RainPointHubFirmwareSensor.__new__(RainPointHubFirmwareSensor)
        RainPointHubFirmwareSensor.__init__(sensor, coord, hub_info)
        assert sensor.native_value == "1.1.1032"

        coord.data["hubs"] = [dict(hub_info, softVer="1.1.1041")]
        assert sensor.native_value == "1.1.1041"

    def test_native_value_reads_only_its_own_hub(self):
        """Another hub's record in the same poll does not feed this sensor."""
        coord = _make_coordinator()
        hub_info = _make_hub_info(mid=1001, soft_ver="2.0")
        sensor = RainPointHubFirmwareSensor.__new__(RainPointHubFirmwareSensor)
        RainPointHubFirmwareSensor.__init__(sensor, coord, hub_info)
        coord.data["hubs"] = [{"mid": 1002, "softVer": "9.9"}]
        assert sensor.native_value == "2.0"

    def test_native_value_holds_the_upgraded_version_across_a_missed_poll(self):
        """An upgrade then an absence keeps the upgraded version, not the snapshot.

        Drive the real sequence rather than asserting the end state: build at
        the pre-upgrade version, upgrade, then take the hub out of the device
        list. Seeding the snapshot at the upgraded version instead would let
        the fallback and the live value coincide, and the sensor could regress
        to the pre-upgrade version with the test still green.

        Holding rather than blanking is deliberate, and unlike
        RainPointHubRSSISensor, which reads unknown in the same case. A
        firmware version does not change while the hub is unreachable, so
        flickering it across the outage that HUB_ABSENT_DEBOUNCE_POLLS absorbs
        would be noise.
        """
        coord = _make_coordinator()
        hub_info = _make_hub_info(mid=1001, soft_ver="1.1.1032")
        sensor = RainPointHubFirmwareSensor.__new__(RainPointHubFirmwareSensor)
        RainPointHubFirmwareSensor.__init__(sensor, coord, hub_info)

        coord.data["hubs"] = [dict(hub_info, softVer="1.1.1041")]
        assert sensor.native_value == "1.1.1041"

        coord.data["hubs"] = []
        assert sensor.native_value == "1.1.1041"

    def test_native_value_holds_the_snapshot_when_absent_from_the_first_poll(self):
        """A hub never seen live still reports what the build-time record held."""
        coord = _make_coordinator()
        hub_info = _make_hub_info(mid=1001, soft_ver="1.1.1032")
        sensor = RainPointHubFirmwareSensor.__new__(RainPointHubFirmwareSensor)
        RainPointHubFirmwareSensor.__init__(sensor, coord, hub_info)
        assert sensor.native_value == "1.1.1032"

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
        """Make helper. Defaults to a hub record without the RF fields.

        Seeds coordinator.data["hubs"] with the same record, since the entity
        reads both its options and its current channel from the live poll.
        """
        coord = _make_coordinator()
        hub_info = _make_hub_info() if hub_info is None else hub_info
        coord.data["hubs"] = [dict(hub_info)]
        select = RainPointHubChannelSelect.__new__(RainPointHubChannelSelect)
        RainPointHubChannelSelect.__init__(select, coord, hub_info)
        return select

    def test_options_fallback_has_16_items(self):
        """With no function.RF field, options fall back to channels 1 through 16."""
        select = self._make()
        assert len(select.options) == 16

    def test_options_fallback_include_all_channels(self):
        """Fallback options should be '1' through '16' as strings."""
        select = self._make()
        for i in range(1, 17):
            assert str(i) in select.options

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
        assert select.options == ["1", "2", "3"]

    def test_options_from_non_contiguous_rf_bitmask(self):
        """A non-contiguous RF bitmask maps each set bit to its channel number."""
        hub_info = _make_hub_info()
        hub_info["function"] = '{"RF":13}'  # 0b1101 -> channels 1, 3, 4
        select = self._make(hub_info)
        assert select.options == ["1", "3", "4"]

    def test_current_channel_outside_mask_is_still_offered(self):
        """A current channel not present in the RF mask is added to the options."""
        hub_info = _make_hub_info()
        hub_info["recich"] = 9
        hub_info["function"] = '{"RF":7}'  # channels 1, 2, 3
        select = self._make(hub_info)
        assert select.current_option == "9"
        assert select.options == ["1", "2", "3", "9"]

    def test_current_option_follows_a_channel_change(self):
        """A channel changed in the RainPoint app after setup is the one reported.

        The regression this entity shipped with: both the current channel and
        the options were captured at construction, so a channel changed
        anywhere else never reached the entity until the config entry was
        reloaded.
        """
        hub_info = _make_hub_info()
        hub_info["recich"] = 2
        hub_info["function"] = '{"RF":7}'  # channels 1, 2, 3
        select = self._make(hub_info)
        assert select.current_option == "2"

        select.coordinator.data["hubs"] = [dict(hub_info, recich=3)]
        assert select.current_option == "3"

    def test_current_option_none_when_the_hub_is_absent(self):
        """A hub out of the device list reports no channel rather than a stale one.

        Deliberately unlike RainPointHubFirmwareSensor, which holds its last
        value. A channel is a setting someone can change in the app while the
        hub is missing here, so there is no last-known value that stays true.
        Matches RainPointHubBroadcastSwitch and the transmission-power select.
        """
        hub_info = _make_hub_info()
        hub_info["recich"] = 2
        select = self._make(hub_info)
        assert select.current_option == "2"

        select.coordinator.data["hubs"] = []
        assert select.current_option is None

    def test_options_hold_the_snapshot_when_the_hub_is_absent(self):
        """An absent hub keeps its real channel list, not the 1 to 16 fallback.

        The bitmask is a hardware capability rather than a setting, so it
        cannot change while the hub is gone, and dropping to the generic
        fallback would offer channels this hub does not support.
        """
        hub_info = _make_hub_info()
        hub_info["function"] = '{"RF":7}'  # channels 1, 2, 3
        select = self._make(hub_info)
        assert select.options == ["1", "2", "3"]

        select.coordinator.data["hubs"] = []
        assert select.options == ["1", "2", "3"]

    def test_reads_only_its_own_hub(self):
        """Another hub's record in the same poll does not feed this select."""
        hub_info = _make_hub_info(mid=1001)
        hub_info["recich"] = 2
        select = self._make(hub_info)
        select.coordinator.data["hubs"] = [{"mid": 1002, "recich": 9}]
        assert select.current_option is None

    def test_malformed_function_blob_falls_back_to_16(self):
        """An unparseable function blob degrades to the 1-16 fallback, not an error."""
        hub_info = _make_hub_info()
        hub_info["function"] = "not-json"
        select = self._make(hub_info)
        assert len(select.options) == 16

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
    """Tests for the hub broadcast switch's live read-through and write path."""

    def _make(self, param="0|1||", mid=1001, hid=100):
        """Build the switch over a coordinator whose hub record carries the given param."""
        coord = _make_coordinator()
        hub_info = _make_hub_info(hid=hid, mid=mid)
        coord.data["hubs"] = [{**hub_info, "param": param}]
        switch = RainPointHubBroadcastSwitch.__new__(RainPointHubBroadcastSwitch)
        RainPointHubBroadcastSwitch.__init__(switch, coord, hub_info)
        switch.async_write_ha_state = MagicMock()
        return switch

    def test_is_on_reads_the_live_hub_record(self):
        """A switch over a hub record carrying param '0|1||' reports is_on True."""
        switch = self._make(param="0|1||")
        assert switch.is_on is True

    def test_is_on_follows_a_replaced_hub_record(self):
        """The same entity instance reports a different is_on after coordinator.data changes.

        Asserts switch._hub_info stays the same object while is_on changes, which is
        exactly what a regression to reading self._hub_info for the flag would break.
        """
        switch = self._make(param="0|1||")
        assert switch.is_on is True
        frozen_hub_info = switch._hub_info

        switch.coordinator.data["hubs"] = [{**_make_hub_info(mid=1001), "param": "0|0||"}]
        switch._handle_coordinator_update()

        assert switch.is_on is False
        assert switch._hub_info is frozen_hub_info

    def test_available_is_true(self):
        """Broadcast switch should always be available."""
        switch = self._make()
        assert switch.available is True

    @pytest.mark.asyncio
    async def test_turn_off_writes_spliced_param_and_shows_immediately(self):
        """One update_main_param call carrying the spliced param; is_on flips with no poll."""
        switch = self._make(param="0|1||", mid=1001)
        switch.coordinator._client = MagicMock()
        switch.coordinator._client.update_main_param = AsyncMock(return_value=True)

        await switch.async_turn_off()

        switch.coordinator._client.update_main_param.assert_called_once_with(mid=1001, param="0|0||")
        assert switch.is_on is False
        switch.async_write_ha_state.assert_called_once()

    @pytest.mark.asyncio
    async def test_a_raised_write_leaves_no_optimistic_state(self):
        """A client error propagates and is_on still reflects the unchanged poll value."""
        switch = self._make(param="0|1||", mid=1001)
        switch.coordinator._client = MagicMock()
        switch.coordinator._client.update_main_param = AsyncMock(side_effect=RainPointApiError("main/update failed: code 5"))

        with pytest.raises(RainPointApiError):
            await switch.async_turn_off()

        assert switch._optimistic is None
        assert switch.is_on is True
        switch.async_write_ha_state.assert_not_called()

    def test_unique_id_contains_broadcast(self):
        """unique_id contains 'broadcast' and equals the pre-change source's literal shape."""
        switch = self._make()
        assert "broadcast" in switch._attr_unique_id
        assert switch._attr_unique_id == "rainpoint_hub_100_1001_broadcast"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("bad_param", [None, "", "1"])
    async def test_turn_on_refuses_when_param_is_unreadable(self, bad_param):
        """A None, empty, or single-field param raises and issues zero client calls.

        Asserting the zero call count explicitly is what makes this test fail
        against an implementation that called the client first and raised
        afterwards, rather than refusing before ever reaching it.
        """
        from homeassistant.exceptions import HomeAssistantError

        switch = self._make(param=bad_param)
        switch.coordinator._client = MagicMock()
        switch.coordinator._client.update_main_param = AsyncMock(return_value=True)

        with pytest.raises(HomeAssistantError):
            await switch.async_turn_on()

        assert switch.coordinator._client.update_main_param.call_count == 0
        assert switch.is_on is None

    @pytest.mark.asyncio
    @pytest.mark.parametrize("bad_param", [None, "", "1"])
    async def test_turn_off_refuses_when_param_is_unreadable(self, bad_param):
        """Same refusal on the turn-off path."""
        from homeassistant.exceptions import HomeAssistantError

        switch = self._make(param=bad_param)
        switch.coordinator._client = MagicMock()
        switch.coordinator._client.update_main_param = AsyncMock(return_value=True)

        with pytest.raises(HomeAssistantError):
            await switch.async_turn_off()

        assert switch.coordinator._client.update_main_param.call_count == 0
        assert switch.is_on is None

    @pytest.mark.asyncio
    async def test_turn_on_twice_is_idempotent(self):
        """Two consecutive turn_on calls leave is_on True with byte-identical params."""
        switch = self._make(param="0|1||", mid=1001)
        switch.coordinator._client = MagicMock()
        switch.coordinator._client.update_main_param = AsyncMock(return_value=True)

        await switch.async_turn_on()
        assert switch.is_on is True
        first_param = switch.coordinator._client.update_main_param.call_args.kwargs["param"]

        await switch.async_turn_on()
        assert switch.is_on is True
        second_param = switch.coordinator._client.update_main_param.call_args.kwargs["param"]

        assert first_param == second_param == "0|1||"
        assert switch.coordinator._client.update_main_param.call_count == 2


class TestRainPointHubBroadcastButton:
    """Tests for the hub's one-shot time-broadcast button."""

    def _make(self, mid=1001, hid=100, device_name="MAC-AABBCC", product_key="pk123"):
        """Build the button over a coordinator whose hub record carries the given identity."""
        coord = _make_coordinator()
        hub_info = _make_hub_info(hid=hid, mid=mid)
        coord.data["hubs"] = [{**hub_info, "deviceName": device_name, "productKey": product_key}]
        button = RainPointHubBroadcastButton.__new__(RainPointHubBroadcastButton)
        RainPointHubBroadcastButton.__init__(button, coord, hub_info)
        button.coordinator._client = MagicMock()
        button.coordinator._client.control_work_mode = AsyncMock(return_value="")
        return button

    def test_unique_id_ends_with_broadcast_now_and_differs_from_the_switch(self):
        """The button's id is distinct from the switch's persisted _broadcast id."""
        button = self._make()
        switch = RainPointHubBroadcastSwitch.__new__(RainPointHubBroadcastSwitch)
        RainPointHubBroadcastSwitch.__init__(switch, button.coordinator, button._hub_info)

        assert button._attr_unique_id.endswith("_broadcast_now")
        assert button._attr_unique_id != switch._attr_unique_id

    def test_available_is_true(self):
        """The broadcast button is always available."""
        button = self._make()
        assert button.available is True

    @pytest.mark.asyncio
    async def test_press_calls_control_work_mode_with_addr_0_port_1_mode_0_and_no_duration(self):
        """Exactly one control_work_mode call, addressed at the hub itself."""
        button = self._make(mid=1001, device_name="MAC-AABBCC", product_key="pk123")

        result = await button.async_press()

        assert result is None
        button.coordinator._client.control_work_mode.assert_called_once_with(
            mid=1001,
            addr=0,
            device_name="MAC-AABBCC",
            product_key="pk123",
            port=1,
            mode=0,
        )
        call_kwargs = button.coordinator._client.control_work_mode.call_args.kwargs
        assert "duration" not in call_kwargs

    @pytest.mark.asyncio
    async def test_press_reads_identity_from_the_live_record_not_the_frozen_snapshot(self):
        """A hub record replaced after construction still addresses the current identity."""
        button = self._make(mid=1001, device_name="MAC-OLD", product_key="pk-old")
        button.coordinator.data["hubs"] = [{**_make_hub_info(mid=1001), "deviceName": "MAC-NEW", "productKey": "pk-new"}]

        await button.async_press()

        button.coordinator._client.control_work_mode.assert_called_once_with(
            mid=1001,
            addr=0,
            device_name="MAC-NEW",
            product_key="pk-new",
            port=1,
            mode=0,
        )

    @pytest.mark.asyncio
    async def test_press_writes_no_entity_state(self):
        """async_press returns None and calls neither async_write_ha_state nor an _attr_ assignment."""
        button = self._make()
        button.async_write_ha_state = MagicMock()

        await button.async_press()

        button.async_write_ha_state.assert_not_called()

    @pytest.mark.asyncio
    async def test_press_propagates_a_raised_api_error(self):
        """A client raising RainPointApiError propagates out of async_press rather than being swallowed."""
        button = self._make()
        button.coordinator._client.control_work_mode = AsyncMock(side_effect=RainPointApiError("controlWorkMode failed: code 5"))

        with pytest.raises(RainPointApiError):
            await button.async_press()

    @pytest.mark.asyncio
    async def test_press_with_empty_response_state_returns_normally(self):
        """The captured empty 'state' response completes the press without raising."""
        button = self._make()
        button.coordinator._client.control_work_mode = AsyncMock(return_value="")

        await button.async_press()  # must not raise

    @pytest.mark.asyncio
    async def test_press_logs_no_cloud_supplied_free_text(self, caplog):
        """No record emitted by a press contains the hub name, deviceName, or productKey."""
        button = self._make(mid=1001, hid=100, device_name="MAC-SECRET-DEVICE", product_key="secret-product-key")

        with caplog.at_level(logging.DEBUG):
            await button.async_press()

        for record in caplog.records:
            message = record.getMessage()
            assert "MAC-SECRET-DEVICE" not in message
            assert "secret-product-key" not in message
            assert "Test Hub" not in message

    @pytest.mark.asyncio
    async def test_press_refuses_when_the_hub_record_is_absent(self):
        """A momentarily missing hub record raises rather than sending an
        empty deviceName/productKey the cloud might silently misroute.

        Mirrors the sibling switch's refusal on an unreadable param: zero
        client calls, asserted explicitly so this fails against an
        implementation that called the client first and raised afterwards.
        """
        from homeassistant.exceptions import HomeAssistantError

        button = self._make(mid=1001)
        button.coordinator.data["hubs"] = []  # mid 1001 no longer present

        with pytest.raises(HomeAssistantError):
            await button.async_press()

        assert button.coordinator._client.control_work_mode.call_count == 0


class TestRainPointPushLastMessageSensor:
    """Tests for the push last-message-age timestamp entity.

    The entity reads its OWN hub's clock, not the session's. One session
    carries every hub, so a session-wide value would show a busy hub's
    traffic on a hub that has gone quiet, which is the reading this entity
    exists to make visible.
    """

    def _make(self, last_message_at=None, now=1000.0):
        """Build the entity with an injected monotonic clock for deterministic age math."""
        mqtt_client = MagicMock()
        mqtt_client.last_message_at_for.return_value = last_message_at
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

    def test_reads_the_clock_for_its_own_hub(self):
        """A quiet hub reads None while the session is busy elsewhere."""
        entity, mqtt_client = self._make(last_message_at=None)
        mqtt_client.last_message_at = 970.0  # session clock, must not be used

        assert entity.native_value is None
        mqtt_client.last_message_at_for.assert_called_once_with(_make_hub_info()["mid"])

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


class TestEveryHubUniqueIdCarriesTheMid:
    """The eight hub entity ids, spelled out rather than substring-matched.

    The five inline sites and the three that ride on the base are asserted
    together and in full, because the whole defect being fixed is that an id
    can look right in a substring test while still colliding with a sibling
    hub's. Five of these are written inline in hub_entities.py; the rssi and
    two push diagnostics append to the base id device.py builds, which is why
    editing that one base site carried them.
    """

    @staticmethod
    def _hub(**overrides):
        return {
            "hid": 100,
            "mid": 200,
            "name": "Hub",
            "softVer": "2.0",
            "mac": "AA:BB",
            "model": "HTV0540FRF",
            **overrides,
        }

    def _build(self, cls, *args):
        entity = cls.__new__(cls)
        cls.__init__(entity, *args)
        return entity

    def test_inline_ids(self):
        """The five sites that spell the segment inline."""
        coord = _make_coordinator()
        assert self._build(RainPointHubDeviceIDSensor, coord, self._hub())._attr_unique_id == "rainpoint_hub_100_200_device_id"
        assert self._build(RainPointHubFirmwareSensor, coord, self._hub())._attr_unique_id == "rainpoint_hub_100_200_firmware"
        assert self._build(RainPointHubMACSensor, coord, self._hub())._attr_unique_id == "rainpoint_hub_100_200_mac"
        assert self._build(RainPointHubChannelSelect, coord, self._hub())._attr_unique_id == "rainpoint_hub_100_200_channel"
        assert self._build(RainPointHubBroadcastSwitch, coord, self._hub())._attr_unique_id == "rainpoint_hub_100_200_broadcast"

    def test_ids_that_ride_on_the_base(self):
        """The three that append to device.py's base id and were not edited."""
        coord = _make_coordinator()
        mqtt_client = MagicMock()
        assert self._build(RainPointHubRSSISensor, coord, self._hub())._attr_unique_id == "rainpoint_hub_100_200_rssi"
        assert (
            self._build(RainPointPushConnectedBinarySensor, mqtt_client, self._hub())._attr_unique_id
            == f"rainpoint_hub_100_200_{PUSH_CONNECTED_UNIQUE_ID_SUFFIX}"
        )
        assert (
            self._build(RainPointPushLastMessageSensor, mqtt_client, self._hub())._attr_unique_id
            == f"rainpoint_hub_100_200_{PUSH_LAST_MESSAGE_UNIQUE_ID_SUFFIX}"
        )

    def test_two_hubs_in_one_home_share_no_id(self):
        """The defect this phase exists to fix, asserted directly.

        Both hubs carry the same hid, so before the re-key every one of these
        pairs was equal and Home Assistant dropped the second hub's entities.
        """
        coord = _make_coordinator()
        hub_a = self._hub()
        hub_b = self._hub(mid=201)
        for cls in (
            RainPointHubDeviceIDSensor,
            RainPointHubFirmwareSensor,
            RainPointHubMACSensor,
            RainPointHubChannelSelect,
            RainPointHubBroadcastSwitch,
            RainPointHubRSSISensor,
            RainPointHubConnectivityBinarySensor,
        ):
            a = self._build(cls, coord, hub_a)._attr_unique_id
            b = self._build(cls, coord, hub_b)._attr_unique_id
            assert a != b, f"{cls.__name__} still collides across two hubs in one home"


class TestHubBroadcastSwitchRealTimeline:
    """The toggle proven correct on the real construct-then-poll sequence.

    Drives a real RainPointCoordinator rather than an injected coordinator.data
    snapshot: construct, first refresh, platform setup, then repeated
    async_refresh calls with different canned hub records, asserting between
    each step. An implementation that never clears the optimistic override
    would pass the confirming poll below and fail the contradicting one and
    the param-goes-missing one, which is exactly the point of driving this as
    a timeline instead of three independent unit tests.
    """

    @staticmethod
    def _hub_devices(param):
        """A getDeviceByHid hub record carrying the given param, no sub-devices."""
        return [
            {
                "mid": 236547,
                "name": "Hub A",
                "deviceName": "hub-mac",
                "productKey": "hub-pk",
                "homeName": "H",
                "model": "HTV0540FRF",
                "param": param,
                "subDevices": [],
            }
        ]

    @staticmethod
    async def _build_timeline(initial_param):
        """Construct -> first refresh -> switch platform setup.

        Returns (coordinator, client, switch).
        """
        from custom_components.rainpoint.const import CONF_HIDS, DOMAIN
        from custom_components.rainpoint.coordinator import RainPointCoordinator
        from custom_components.rainpoint.switch import async_setup_entry

        client = AsyncMock()
        client.get_devices_by_hid.return_value = TestHubBroadcastSwitchRealTimeline._hub_devices(initial_param)
        client.get_multiple_device_status.return_value = [{"mid": 236547, "subDeviceStatus": []}]

        entry = MagicMock()
        entry.entry_id = "e1"
        entry.data = {CONF_HIDS: [10]}
        entry.options = {}
        hass = MagicMock()
        hass.data = {DOMAIN: {"e1": {}}}

        coordinator = RainPointCoordinator(hass, client, entry)
        hass.data[DOMAIN]["e1"]["coordinator"] = coordinator

        await coordinator.async_config_entry_first_refresh()

        captured = []
        async_add_entities = MagicMock(side_effect=lambda ents, **kw: captured.extend(ents))
        await async_setup_entry(hass, entry, async_add_entities)

        switches = [e for e in captured if isinstance(e, RainPointHubBroadcastSwitch)]
        assert len(switches) == 1
        switch = switches[0]
        switch.async_write_ha_state = MagicMock()
        # Registers the coordinator listener the same way HA's real
        # entity-platform add flow does, so async_refresh below actually
        # reaches _handle_coordinator_update.
        await switch.async_added_to_hass()
        return coordinator, client, switch

    @pytest.mark.asyncio
    async def test_the_full_command_then_confirming_then_contradicting_then_missing_sequence(self):
        """Construct off, flip on, poll confirms, poll contradicts, poll drops param."""
        coordinator, client, switch = await self._build_timeline("0|0||")
        frozen_hub_info = switch._hub_info

        assert switch.is_on is False

        client.update_main_param = AsyncMock(return_value=True)
        await switch.async_turn_on()
        assert switch.is_on is True
        client.update_main_param.assert_called_once_with(mid=236547, param="0|1||")

        # A poll confirming the command: the optimistic override is cleared,
        # and the value now comes from the poll agreeing with it.
        client.get_devices_by_hid.return_value = self._hub_devices("0|1||")
        await coordinator.async_refresh()
        assert switch.is_on is True
        assert switch._optimistic is None

        # A poll contradicting the command: the cloud did not keep the
        # change, and the poll wins rather than the last command.
        client.get_devices_by_hid.return_value = self._hub_devices("0|0||")
        await coordinator.async_refresh()
        assert switch.is_on is False

        # A poll whose hub record carries no param at all: unknown, not the
        # last commanded value.
        no_param_hub = self._hub_devices("0|0||")
        del no_param_hub[0]["param"]
        client.get_devices_by_hid.return_value = no_param_hub
        await coordinator.async_refresh()
        assert switch.is_on is None

        # Across the whole sequence the frozen snapshot never changed object
        # identity, while is_on changed value repeatedly -- the pair that
        # would fail if a regression routed the flag back through
        # self._hub_info instead of the live coordinator read.
        assert switch._hub_info is frozen_hub_info

    @pytest.mark.asyncio
    async def test_an_unrelated_push_does_not_clear_the_optimistic_value(self):
        """A hub connectivity push fires the same _handle_coordinator_update
        hook every entity gets, but never rebuilds coordinator.data["hubs"] --
        so it must not revert a just-set optimistic value the way a real poll
        legitimately would. Only the real poll below is allowed to clear it."""
        coordinator, client, switch = await self._build_timeline("0|0||")

        client.update_main_param = AsyncMock(return_value=True)
        await switch.async_turn_on()
        assert switch.is_on is True
        assert switch._optimistic is True

        # An unrelated hub-level push: apply_hub_push_update shallow-copies
        # coordinator.data and carries "hubs" forward by reference, so this
        # must not be mistaken for the poll that is supposed to confirm or
        # contradict the command.
        coordinator.apply_hub_push_update(236547, True, 1717200000000)
        # Proves the push was applied rather than dropped by one of
        # apply_hub_push_update's guards, which would make the two
        # assertions below pass vacuously.
        assert coordinator.data["hub_connectivity"][236547]["state"] == HUB_CONNECTED
        assert switch.is_on is True
        assert switch._optimistic is True

        # The real poll, still reporting the pre-command value: only now
        # does the optimistic override give way, and the poll's contradicting
        # value wins.
        client.get_devices_by_hid.return_value = self._hub_devices("0|0||")
        await coordinator.async_refresh()
        assert switch.is_on is False
        assert switch._optimistic is None
