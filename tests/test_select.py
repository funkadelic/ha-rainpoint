"""Tests for select entity platform setup (select.py)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from homeassistant.exceptions import HomeAssistantError

from custom_components.rainpoint.const import DOMAIN, MODEL_HTV210B
from custom_components.rainpoint.select import RainPointSubDevicePowerSelect, _sub_device_record, async_setup_entry
from tests.helpers import htv210b_status, make_mock_session_client, mock_json_response


def _make_hass(hubs=None):
    """Return a mock hass with coordinator data."""
    coord = MagicMock()
    coord.data = {"hubs": hubs if hubs is not None else [], "sensors": {}}
    hass = MagicMock()
    entry = MagicMock()
    entry.entry_id = "test_entry"
    hass.data = {DOMAIN: {entry.entry_id: {"coordinator": coord}}}
    return hass, entry, coord


class TestSelectSetupEntry:
    """Tests for select async_setup_entry."""

    @pytest.mark.asyncio
    async def test_setup_entry_creates_entities_for_each_hub(self):
        """One channel select entity should be created per hub."""
        hub_info = {"hid": 100, "mid": 1001, "name": "Hub 1", "softVer": "1.0", "mac": "AA:BB"}
        hass, entry, _coord = _make_hass(hubs=[hub_info])

        mock_add_entities = MagicMock()
        await async_setup_entry(hass, entry, mock_add_entities)

        mock_add_entities.assert_called_once()
        entities = mock_add_entities.call_args[0][0]
        assert len(entities) == 1

    @pytest.mark.asyncio
    async def test_bluetooth_wrapper_record_gets_no_channel_select(self):
        """A parent record with no hub identity is not a hub and gets no select.

        Its identity fields are empty strings rather than absent keys, so it
        used to collapse onto the real hub's hid-keyed slot and win.
        """
        real_hub = {"hid": 182509, "mid": 236547, "name": "Hub", "did": "17053410", "mac": "A8:46:74:BB:91:F0"}
        wrapper = {"hid": 182509, "mid": 346965, "name": "", "did": "", "mac": "", "model": "", "productKey": ""}
        hass, entry, _coord = _make_hass(hubs=[real_hub, wrapper])

        mock_add_entities = MagicMock()
        await async_setup_entry(hass, entry, mock_add_entities)

        entities = mock_add_entities.call_args[0][0]
        assert len(entities) == 1
        assert entities[0]._hub_info["did"] == "17053410"

    @pytest.mark.asyncio
    async def test_setup_entry_no_hubs_adds_empty_list(self):
        """No hubs should result in async_add_entities called with empty list."""
        hass, entry, _coord = _make_hass(hubs=[])

        mock_add_entities = MagicMock()
        await async_setup_entry(hass, entry, mock_add_entities)

        mock_add_entities.assert_called_once()
        entities = mock_add_entities.call_args[0][0]
        assert len(entities) == 0

    @pytest.mark.asyncio
    async def test_setup_entry_multiple_hubs(self):
        """Multiple hubs should each get a channel select entity."""
        hubs = [
            {"hid": 100, "mid": 1001, "name": "Hub 1", "softVer": "1.0", "mac": "AA:BB"},
            {"hid": 200, "mid": 2002, "name": "Hub 2", "softVer": "2.0", "mac": "CC:DD"},
        ]
        hass, entry, _coord = _make_hass(hubs=hubs)

        mock_add_entities = MagicMock()
        await async_setup_entry(hass, entry, mock_add_entities)

        mock_add_entities.assert_called_once()
        entities = mock_add_entities.call_args[0][0]
        assert len(entities) == 2

    @pytest.mark.asyncio
    async def test_setup_entry_returns_early_for_non_list_hubs(self):
        """If hubs is not a list, setup should return early without adding entities."""
        coord = MagicMock()
        coord.data = {"hubs": "not-a-list", "sensors": {}}
        hass = MagicMock()
        entry = MagicMock()
        entry.entry_id = "test_entry"
        hass.data = {DOMAIN: {entry.entry_id: {"coordinator": coord}}}

        mock_add_entities = MagicMock()
        await async_setup_entry(hass, entry, mock_add_entities)

        # async_add_entities should not be called when hubs is invalid
        mock_add_entities.assert_not_called()


class TestSubDevicePowerSelectRealTimeline:
    """End to end: an HTV210B sub-device's transmission power, driven through the
    real construct -> first refresh -> platform setup -> async_select_option
    sequence (PSET-01, PSET-02), per the repo's timeline-not-end-state test rule.

    An injected coordinator.data snapshot would pass at full branch coverage
    while the D-07 discovery path (sensors_cfg / LateEntityAdder / add-once
    ledger) stayed dead in a live install, exactly the trap CLAUDE.md's
    testing section names.
    """

    _HID = 10
    _MID = 236547
    _ADDR = 1
    _SID = 491657
    _INITIAL_PARAM = (
        "5=01,11=58020a001e000000000000000000,12=58020a001e000000000000000000,"
        "50=646464646464646464646464,51=646464646464646464646464"
    )
    _EXPECTED_ENHANCE_PARAM = (
        "5=02,11=58020a001e000000000000000000,12=58020a001e000000000000000000,"
        "50=646464646464646464646464,51=646464646464646464646464"
    )

    @classmethod
    def _hub_devices(cls, param):
        """A getDeviceByHid hub record carrying one hub-paired HTV210B sub-device."""
        return [
            {
                "mid": cls._MID,
                "name": "Hub A",
                "deviceName": "hub-mac",
                "productKey": "hub-pk",
                "homeName": "H",
                "model": "HWG023WBRF-V2",
                "subDevices": [
                    {
                        "addr": cls._ADDR,
                        "sid": cls._SID,
                        "name": "BT Valve",
                        "model": MODEL_HTV210B,
                        "modelCode": 41,
                        "softVer": "1.0",
                        "param": param,
                    }
                ],
            }
        ]

    @classmethod
    async def _build_timeline(cls):
        """Construct -> first refresh -> select platform setup.

        Returns (coordinator, client, select).
        """
        from custom_components.rainpoint.const import CONF_HIDS
        from custom_components.rainpoint.coordinator import RainPointCoordinator

        client = AsyncMock()
        client.get_devices_by_hid.return_value = cls._hub_devices(cls._INITIAL_PARAM)
        client.get_multiple_device_status.return_value = htv210b_status(mid=cls._MID)

        entry = MagicMock()
        entry.entry_id = "e1"
        entry.data = {CONF_HIDS: [cls._HID]}
        entry.options = {}
        hass = MagicMock()
        hass.data = {DOMAIN: {"e1": {}}}

        coordinator = RainPointCoordinator(hass, client, entry)
        hass.data[DOMAIN]["e1"]["coordinator"] = coordinator

        await coordinator.async_config_entry_first_refresh()

        captured = []
        async_add_entities = MagicMock(side_effect=lambda ents, **kw: captured.extend(ents))
        await async_setup_entry(hass, entry, async_add_entities)

        power_selects = [e for e in captured if isinstance(e, RainPointSubDevicePowerSelect)]
        assert len(power_selects) == 1
        select = power_selects[0]
        select.async_write_ha_state = MagicMock()
        # Registers the coordinator listener the same way HA's real
        # entity-platform add flow does, matching the sibling hub-broadcast
        # timeline's setup.
        await select.async_added_to_hass()
        return coordinator, client, select

    @pytest.mark.asyncio
    async def test_setup_yields_exactly_one_power_select_showing_standard(self):
        """The HTV210B's key-5 value 01 reads as Standard at setup."""
        _coordinator, _client, select = await self._build_timeline()

        assert select.current_option == "Standard"

    @pytest.mark.asyncio
    async def test_select_option_posts_read_modify_write_and_updates_display(self):
        """Choosing Enhance posts exactly one sub/update splicing only key 5.

        Drives the real client method (not a bare AsyncMock assertion) so the
        posted JSON body is asserted at the far end of the stack, the way
        TestDpValveTracer proves controlWorkModeDP's body.
        """
        coordinator, client, select = await self._build_timeline()

        real_client = make_mock_session_client()
        real_client.ensure_logged_in = AsyncMock()
        real_client._session.post = MagicMock(
            return_value=mock_json_response({"code": 0, "msg": "SUCCESS", "data": {"homeVersion": 1, "paramVersion": 3}})
        )
        client.update_sub_param = real_client.update_sub_param
        # D-04's fresh pre-write read: still the value read at setup.
        client.get_devices_by_hid.return_value = self._hub_devices(self._INITIAL_PARAM)

        await select.async_select_option("Enhance")

        real_client._session.post.assert_called_once()
        call = real_client._session.post.call_args
        url = call.args[0]
        body = call.kwargs["json"]

        assert url.endswith("/app/device/sub/update")
        assert set(body.keys()) == {"mid", "sid", "param"}
        assert body["mid"] == self._MID
        assert body["sid"] == self._SID
        assert body["param"] == self._EXPECTED_ENHANCE_PARAM

        assert select.current_option == "Enhance"

        # The fresh read's own hub list identity was never assigned to
        # coordinator.data["hubs"], so the optimistic override the write set
        # is still what a subsequent read reflects -- proven by re-reading
        # current_option a second time rather than trusting the first.
        assert select.current_option == "Enhance"
        assert coordinator.data["sensors"]["10_236547_1"]["data"] is not None

    @pytest.mark.asyncio
    async def test_an_unrelated_push_does_not_clear_the_optimistic_value(self):
        """A hub connectivity push fires the same _handle_coordinator_update
        hook every entity gets, but never rebuilds coordinator.data["hubs"] --
        so it must not revert a just-set optimistic value the way a real poll
        legitimately would. Mirrors TestHubBroadcastSwitchRealTimeline's
        equivalent case for the sibling hub-broadcast switch."""
        coordinator, client, select = await self._build_timeline()

        real_client = make_mock_session_client()
        real_client.ensure_logged_in = AsyncMock()
        real_client._session.post = MagicMock(
            return_value=mock_json_response({"code": 0, "msg": "SUCCESS", "data": {"paramVersion": 3}})
        )
        client.update_sub_param = real_client.update_sub_param
        client.get_devices_by_hid.return_value = self._hub_devices(self._INITIAL_PARAM)

        await select.async_select_option("Enhance")
        assert select.current_option == "Enhance"
        assert select._optimistic == "2"

        coordinator.apply_hub_push_update(self._MID, True, 1717200000000)
        assert select.current_option == "Enhance"
        assert select._optimistic == "2"

        # The real poll, still reporting the pre-command value: only now does
        # the optimistic override give way, and the poll's own value wins.
        client.get_devices_by_hid.return_value = self._hub_devices(self._INITIAL_PARAM)
        await coordinator.async_refresh()
        assert select._optimistic is None
        assert select.current_option == "Standard"

    @pytest.mark.asyncio
    async def test_unknown_option_raises(self):
        """An option string that does not resolve to a canonical digit refuses."""
        _coordinator, _client, select = await self._build_timeline()

        with pytest.raises(HomeAssistantError, match="Unknown transmission power option"):
            await select.async_select_option("Not A Real Option")

    @pytest.mark.asyncio
    async def test_missing_sid_on_the_fresh_read_raises(self):
        """A fresh sub-device record with no `sid` cannot be addressed for the write."""
        _coordinator, client, select = await self._build_timeline()

        no_sid_devices = self._hub_devices(self._INITIAL_PARAM)
        del no_sid_devices[0]["subDevices"][0]["sid"]
        client.get_devices_by_hid.return_value = no_sid_devices

        with pytest.raises(HomeAssistantError, match="could not be addressed"):
            await select.async_select_option("Enhance")

    @pytest.mark.asyncio
    async def test_unsplicable_fresh_param_raises(self):
        """A fresh read whose param the splice gate refuses cannot be written."""
        _coordinator, client, select = await self._build_timeline()

        unreadable_devices = self._hub_devices("not-a-valid-blob")
        client.get_devices_by_hid.return_value = unreadable_devices

        with pytest.raises(HomeAssistantError, match="could not be read"):
            await select.async_select_option("Enhance")


class TestSubDevicePowerSelectAdmissionRealTimeline:
    """The D-01 model gate and the D-07 silent-type guard, proven through the
    real timeline rather than an injected coordinator.data snapshot -- the
    same trap TestSilentUnitGuardRealTimeline in test_valve.py exists for.
    """

    @staticmethod
    async def _build(hub_devices, status):
        from custom_components.rainpoint.const import CONF_HIDS
        from custom_components.rainpoint.coordinator import RainPointCoordinator

        client = AsyncMock()
        client.get_devices_by_hid.return_value = hub_devices
        client.get_multiple_device_status.return_value = status

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
        return coordinator, client, captured

    @pytest.mark.asyncio
    async def test_a_non_htv210b_model_gets_no_power_select(self):
        """A model outside SUB_POWER_MODE_MODELS produces no power select."""
        hub_devices = [
            {
                "mid": 1001,
                "name": "Hub 1",
                "deviceName": "hub-mac",
                "productKey": "hub-pk",
                "homeName": "H",
                "model": "HTV0540FRF",
                "subDevices": [{"addr": 1, "sid": 555, "name": "Soil", "model": "HCS026FRF", "modelCode": 1, "param": "5=01"}],
            }
        ]
        status = [{"mid": 1001, "subDeviceStatus": []}]
        _coordinator, _client, captured = await self._build(hub_devices, status)

        assert not [e for e in captured if isinstance(e, RainPointSubDevicePowerSelect)]

    @pytest.mark.asyncio
    async def test_a_silent_htv210b_gets_no_power_select(self):
        """A hub-paired but silent HTV210B carries model == HTV210B on its
        debounced silent entry, so the model check alone would admit it --
        the SILENT_DATA_TYPE guard is what actually blocks creation.

        The debounce means the sensors-dict entry does not even exist at the
        first refresh async_setup_entry runs against; the registered
        coordinator listener is what reaches the build() closure once the
        entry exists and carries the silent type, exercising the guard.
        """
        from custom_components.rainpoint.coordinator import SILENT_DATA_TYPE, SILENT_DEBOUNCE_POLLS
        from tests.helpers import htv210b_hub_devices, htv210b_silent_status

        coordinator, _client, captured = await self._build(htv210b_hub_devices(), htv210b_silent_status())
        assert not [e for e in captured if isinstance(e, RainPointSubDevicePowerSelect)]

        for _ in range(SILENT_DEBOUNCE_POLLS):
            await coordinator.async_refresh()

        key = "10_20_1"
        assert coordinator.data["sensors"][key]["data"]["type"] == SILENT_DATA_TYPE
        assert not [e for e in captured if isinstance(e, RainPointSubDevicePowerSelect)]


class TestSubDeviceRecordHelper:
    """Unit tests for _sub_device_record's malformed-input tolerance."""

    def test_non_list_hub_records_returns_empty_dict(self):
        """A non-list hub_records (e.g. an odd coordinator.data snapshot) yields {}."""
        assert _sub_device_record("not-a-list", 1, 1) == {}

    def test_non_dict_hub_entries_are_skipped(self):
        """A malformed (non-dict) hub entry does not raise and is simply skipped."""
        hub_records = ["not-a-dict", {"mid": 1, "subDevices": [{"addr": 1, "sid": 9}]}]
        assert _sub_device_record(hub_records, 1, 1) == {"addr": 1, "sid": 9}

    def test_no_matching_mid_returns_empty_dict(self):
        """No hub carrying the requested mid yields {}."""
        hub_records = [{"mid": 999, "subDevices": []}]
        assert _sub_device_record(hub_records, 1, 1) == {}

    def test_no_matching_addr_returns_empty_dict(self):
        """A matching hub with no sub-device at the requested addr yields {}."""
        hub_records = [{"mid": 1, "subDevices": [{"addr": 2, "sid": 9}]}]
        assert _sub_device_record(hub_records, 1, 1) == {}
