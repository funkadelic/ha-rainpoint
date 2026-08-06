"""Tests for valve entity platform (valve.py)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.rainpoint.api import _encode_dp_duration_param
from custom_components.rainpoint.const import DOMAIN, MODEL_HTV210B, MODEL_VALVE_145, MODEL_VALVE_245, MODEL_VALVE_345
from custom_components.rainpoint.coordinator import SILENT_DATA_TYPE, SILENT_DEBOUNCE_POLLS
from custom_components.rainpoint.valve import (
    DEFAULT_DURATION_SECONDS,
    RainPointDpValveEntity,
    RainPointValveEntity,
)
from tests.helpers import (
    htv210b_hub_devices,
    htv210b_silent_status,
    htv210b_status,
    make_mock_session_client,
    make_sensor_coordinator,
    make_valve_zone_status,
    mock_json_response,
)
from tests.payload_samples import SAMPLE_HTV145_OPEN_PAYLOAD, SAMPLE_HTV245_ASCII_PAYLOAD

ONE_ZONE_TLV_PAYLOAD = "11#17E1D70018DC0119D8001D20"
"""The shared two-zone TLV frame with zone 2's state and duration entries removed.

Lives here rather than in tests/helpers.py because only the ledger's
append-across-polls timeline needs a valve that reports one zone now and two
later; every other module drives off the captured frame unmodified.
"""


def _make_valve(zone_data=None, hub_online=True, model="HTV245FRF"):
    """Create a RainPointValveEntity with mock coordinator, bypassing __init__."""
    sensor_key = "100_200_1"
    sensor_info = {
        "hid": 100,
        "mid": 200,
        "addr": 1,
        "sub_name": "Valve Hub 1",
        "model": model,
        "device_name": "dev1",
        "product_key": "pk1",
        "firmware_version": "1.0",
    }
    decoded = {
        "hub_online": hub_online,
        "zones": {1: zone_data if zone_data is not None else {"open": True, "duration_seconds": 300, "state_raw": 1}},
    }
    mock_coordinator = make_sensor_coordinator(
        model=model,
        data=decoded,
        sub_name="Valve Hub 1",
        firmware_version="1.0",
        extra_sensor_info={"device_name": "dev1", "product_key": "pk1"},
    )

    valve = RainPointValveEntity.__new__(RainPointValveEntity)
    valve.coordinator = mock_coordinator
    valve._sensor_key = sensor_key
    valve._sensor_info = sensor_info
    valve._zone_num = 1
    valve.hass = MagicMock()
    valve._attr_unique_id = "rainpoint_100_200_1_zone1"
    valve._attr_name = "Valve Hub 1 Zone 1"
    return valve


class TestValveProperties:
    def test_device_info_carries_firmware_and_identity(self):
        """The valve's device page shows firmware and a stable device id."""
        valve = _make_valve()
        info = valve.device_info
        assert info["sw_version"] == "1.0"
        assert info["serial_number"] == "200_1"
        assert info["via_device"] == (DOMAIN, "hub_100_200")

    """Tests for RainPointValveEntity properties."""

    def test_is_closed_when_open(self):
        """Zone open=True should give is_closed == False."""
        valve = _make_valve(zone_data={"open": True, "duration_seconds": 300, "state_raw": 1})
        assert valve.is_closed is False

    def test_is_closed_when_closed(self):
        """Zone open=False should give is_closed == True."""
        valve = _make_valve(zone_data={"open": False, "duration_seconds": 0, "state_raw": 0})
        assert valve.is_closed is True

    def test_is_closed_when_none(self):
        """Zone open=None should give is_closed == None."""
        valve = _make_valve(zone_data={"open": None, "duration_seconds": 0, "state_raw": None})
        assert valve.is_closed is None

    def test_available_when_hub_online(self):
        """hub_online=True should give available == True."""
        valve = _make_valve(hub_online=True)
        assert valve.available is True

    def test_unavailable_when_hub_offline(self):
        """hub_online=False should give available == False."""
        valve = _make_valve(hub_online=False)
        assert valve.available is False

    def test_unavailable_when_hub_online_missing(self):
        """Decoder error dicts do not include hub_online and should be unavailable."""
        valve = _make_valve()
        del valve.coordinator.data["sensors"]["100_200_1"]["data"]["hub_online"]
        assert valve.available is False

    def test_unavailable_when_no_data(self):
        """No data in sensors should give available == False."""
        valve = _make_valve()
        valve.coordinator.data["sensors"]["100_200_1"]["data"] = None
        assert valve.available is False

    def test_unavailable_when_sensor_entry_turns_silent(self):
        """A valve already bound to a key that goes silent (D-11/D-12) reports
        unavailable rather than raising: raw_status={} carries no hub_online."""
        valve = _make_valve()
        valve.coordinator.data["sensors"]["100_200_1"]["raw_status"] = {}
        valve.coordinator.data["sensors"]["100_200_1"]["data"] = {
            "type": SILENT_DATA_TYPE,
            "silent_state": "stopped_reporting",
        }
        assert valve.available is False

    def test_available_when_hub_online_and_cloud_connected(self):
        """hub_online True and the hub's record connected: available True,
        unchanged from before this correction."""
        valve = _make_valve(hub_online=True)
        valve.coordinator.data["hub_connectivity"] = {200: {"state": "connected"}}
        assert valve.available is True

    def test_unavailable_when_hub_online_but_cloud_disconnected(self):
        """hub_online True but the cloud already reports this hub as
        disconnected: available False. This is the specific lie the cloud
        connectivity gate exists to stop -- hub_online is payload-derived and
        stays healthy off a frozen frame during an outage."""
        valve = _make_valve(hub_online=True)
        valve.coordinator.data["hub_connectivity"] = {200: {"state": "disconnected"}}
        assert valve.available is False

    def test_available_when_hub_online_and_cloud_connectivity_unknown(self):
        """An unknown tri-state marks nothing unavailable: available stays
        True."""
        valve = _make_valve(hub_online=True)
        valve.coordinator.data["hub_connectivity"] = {200: {"state": "unknown"}}
        assert valve.available is True

    def test_available_when_hub_online_and_no_hub_connectivity_key_at_all(self):
        """No hub_connectivity key at all (every pre-existing test fake):
        available stays True, exactly what it was before this plan."""
        valve = _make_valve(hub_online=True)
        assert "hub_connectivity" not in valve.coordinator.data
        assert valve.available is True

    def test_unavailable_when_hub_offline_even_though_cloud_connected(self):
        """hub_online False with a connected cloud report: available False,
        proving the payload-derived signal was not replaced by the cloud
        read."""
        valve = _make_valve(hub_online=False)
        valve.coordinator.data["hub_connectivity"] = {200: {"state": "connected"}}
        assert valve.available is False

    def test_available_false_when_sensor_missing_even_with_connected_hub(self):
        """The early sensor-missing return still wins; the new condition is
        never reached."""
        valve = _make_valve(hub_online=True)
        valve.coordinator.data["hub_connectivity"] = {200: {"state": "connected"}}
        valve._sensor_key = "missing"
        assert valve.available is False

    def test_extra_state_attributes_includes_duration(self):
        """Zone with duration_seconds should appear in extra_state_attributes."""
        valve = _make_valve(zone_data={"open": True, "duration_seconds": 300, "state_raw": 1})
        attrs = valve.extra_state_attributes
        assert attrs["duration_seconds"] == 300

    def test_extra_state_attributes_includes_event_time_when_running(self):
        """A running zone surfaces the moment its run ends."""
        valve = _make_valve(
            zone_data={"open": True, "duration_seconds": 2940, "state_raw": 0x21, "event_time": "2026-07-04T18:29:51"}
        )
        assert valve.extra_state_attributes["event_time"] == "2026-07-04T18:29:51"

    def test_event_time_omitted_when_the_zone_reports_none(self):
        """An idle zone omits the attribute rather than publishing a null one."""
        valve = _make_valve(zone_data={"open": False, "duration_seconds": 0, "state_raw": 0, "event_time": None})
        assert "event_time" not in valve.extra_state_attributes

    def test_device_info_identifiers(self):
        """device_info should contain the correct identifier tuple."""
        valve = _make_valve()
        identifiers = valve.device_info["identifiers"]
        assert (DOMAIN, "100_200_1") in identifiers

    def test_unique_id_format(self):
        """unique_id should match the expected format."""
        valve = _make_valve()
        assert valve._attr_unique_id == "rainpoint_100_200_1_zone1"

    def test_is_closed_when_zone_absent(self):
        """If zone not in zones dict, _zone_data is None, is_closed returns None."""
        valve = _make_valve()
        valve._zone_num = 99  # Zone 99 doesn't exist
        assert valve.is_closed is None


class TestValveControl:
    """Tests for RainPointValveEntity control methods."""

    @pytest.mark.asyncio
    async def test_async_open_valve(self):
        """async_open_valve should call control_work_mode with mode=1."""
        valve = _make_valve()
        mock_control = AsyncMock(return_value=None)
        valve.coordinator._client.control_work_mode = mock_control
        valve._get_configured_duration_seconds = MagicMock(return_value=600)

        await valve.async_open_valve()

        mock_control.assert_called_once_with(
            mid=200,
            addr=1,
            device_name="dev1",
            product_key="pk1",
            port=1,
            mode=1,
            duration=600,
        )

    @pytest.mark.asyncio
    async def test_async_open_valve_applies_response_state_end_to_end(self):
        """control_work_mode response is decoded and pushed to coordinator, bypassing the next poll.

        Covered end to end in one test: control_work_mode returns a real ASCII
        payload and async_set_updated_data is asserted directly, rather than
        exercising the decode and coordinator-push halves separately.
        """
        valve = _make_valve(model=MODEL_VALVE_245)
        valve.coordinator.async_set_updated_data = MagicMock()
        valve._get_configured_duration_seconds = MagicMock(return_value=600)
        mock_control = AsyncMock(return_value=SAMPLE_HTV245_ASCII_PAYLOAD)
        valve.coordinator._client.control_work_mode = mock_control

        await valve.async_open_valve()

        mock_control.assert_called_once()
        valve.coordinator.async_set_updated_data.assert_called_once()
        updated = valve.coordinator.async_set_updated_data.call_args.args[0]
        assert updated["sensors"]["100_200_1"]["data"]["zones"]

    @pytest.mark.asyncio
    async def test_async_open_valve_with_kwargs_duration(self):
        """async_open_valve with duration kwarg should use that value, not configured."""
        valve = _make_valve()
        mock_control = AsyncMock(return_value=None)
        valve.coordinator._client.control_work_mode = mock_control

        await valve.async_open_valve(duration=120)

        mock_control.assert_called_once()
        _, kwargs = mock_control.call_args
        assert kwargs["duration"] == 120
        assert kwargs["mode"] == 1

    @pytest.mark.asyncio
    async def test_async_close_valve(self):
        """async_close_valve should call control_work_mode with mode=0, duration=0."""
        valve = _make_valve()
        mock_control = AsyncMock(return_value=None)
        valve.coordinator._client.control_work_mode = mock_control

        await valve.async_close_valve()

        mock_control.assert_called_once_with(
            mid=200,
            addr=1,
            device_name="dev1",
            product_key="pk1",
            port=1,
            mode=0,
            duration=0,
        )

    @pytest.mark.asyncio
    async def test_async_close_valve_applies_closed_response_state(self, monkeypatch):
        """A successful close response immediately updates coordinator state."""
        from custom_components.rainpoint import valve as valve_mod

        valve = _make_valve(model=MODEL_VALVE_245)
        valve.coordinator.async_set_updated_data = MagicMock()
        valve.coordinator.record_valve_command = MagicMock()
        valve.coordinator._client.control_work_mode = AsyncMock(return_value="close-response")
        monkeypatch.setattr(
            valve_mod,
            "decode_htv213frf_valve",
            MagicMock(
                return_value={
                    "type": "valve_hub",
                    "hub_online": True,
                    "zones": {1: {"open": False, "duration_seconds": 0, "state_raw": 0}},
                }
            ),
        )

        await valve.async_close_valve()

        valve.coordinator.record_valve_command.assert_called_once_with("100_200_1", 1)
        updated = valve.coordinator.async_set_updated_data.call_args.args[0]
        assert updated["sensors"]["100_200_1"]["data"]["zones"][1]["open"] is False
        assert updated["sensors"]["100_200_1"]["data"]["zones"][1]["duration_seconds"] == 0

    def test_apply_response_state_updates_coordinator(self):
        """_apply_response_state should call async_set_updated_data when raw_state given."""
        valve = _make_valve(model=MODEL_VALVE_245)
        valve.coordinator.async_set_updated_data = MagicMock()

        # Canonical two-zone ASCII payload from maintainer's HTV245FRF
        valve._apply_response_state(SAMPLE_HTV245_ASCII_PAYLOAD)

        valve.coordinator.async_set_updated_data.assert_called_once()
        updated = valve.coordinator.async_set_updated_data.call_args.args[0]
        assert updated["sensors"]["100_200_1"]["data"]["zones"]  # non-empty

    def test_apply_response_state_routes_htv145(self):
        """HTV145FRF control responses decode via the HTV145 decoder."""
        valve = _make_valve(model=MODEL_VALVE_145)
        valve.coordinator.async_set_updated_data = MagicMock()

        valve._apply_response_state(SAMPLE_HTV145_OPEN_PAYLOAD)

        valve.coordinator.async_set_updated_data.assert_called_once()
        updated = valve.coordinator.async_set_updated_data.call_args.args[0]
        zones = updated["sensors"]["100_200_1"]["data"]["zones"]
        assert zones[1]["open"] is True
        assert zones[1]["duration_seconds"] == 1200

    def test_apply_response_state_none_skips(self):
        """_apply_response_state with None should not call async_set_updated_data."""
        valve = _make_valve()
        valve.coordinator.async_set_updated_data = MagicMock()

        valve._apply_response_state(None)

        valve.coordinator.async_set_updated_data.assert_not_called()

    def test_apply_response_state_empty_skips(self):
        """_apply_response_state with empty string should not call async_set_updated_data."""
        valve = _make_valve()
        valve.coordinator.async_set_updated_data = MagicMock()

        valve._apply_response_state("")

        valve.coordinator.async_set_updated_data.assert_not_called()

    def test_get_configured_duration_when_entity_id_not_registered(self, monkeypatch):
        """Registry returns None (duration entity not yet registered) -> fall back to default.

        Covers the ``if entity_id:`` falsy branch at valve.py:187->195, which fires
        on the first open_valve call before the companion number entity has been
        added to the registry.
        """
        import sys

        valve = _make_valve()

        mock_registry = MagicMock()
        mock_registry.async_get_entity_id.return_value = None
        mock_er_module = MagicMock()
        mock_er_module.async_get.return_value = mock_registry

        # Binding via sys.modules alone isn't enough when the parent stub
        # already cached an auto-MagicMock attribute for entity_registry on
        # first access; rebind the parent attr so ``from homeassistant.helpers
        # import entity_registry as er`` resolves to our mock.
        monkeypatch.setitem(sys.modules, "homeassistant.helpers.entity_registry", mock_er_module)
        monkeypatch.setattr(
            sys.modules["homeassistant.helpers"],
            "entity_registry",
            mock_er_module,
            raising=False,
        )

        assert valve._get_configured_duration_seconds() == DEFAULT_DURATION_SECONDS

    def test_get_configured_duration_falls_back_to_default(self, monkeypatch):
        """If entity registry lookup finds entity_id but state is None, fall back to default."""
        import sys

        valve = _make_valve()

        # Mock the entity registry import chain: entity_id found but state is None
        mock_registry = MagicMock()
        mock_registry.async_get_entity_id.return_value = "number.rainpoint_valve_zone1_duration"
        mock_er_module = MagicMock()
        mock_er_module.async_get.return_value = mock_registry

        # hass.states.get returns None, state not available yet
        valve.hass.states.get.return_value = None

        # Use monkeypatch.setitem so that if conftest later adds
        # homeassistant.helpers.entity_registry to _HA_STUBS, pytest's
        # teardown restores the original stub rather than deleting it
        # (which would break tests running after this one in the same session).
        monkeypatch.setitem(sys.modules, "homeassistant.helpers.entity_registry", mock_er_module)

        assert valve._get_configured_duration_seconds() == DEFAULT_DURATION_SECONDS

    def test_get_configured_duration_parses_numeric_state(self, monkeypatch):
        """Entity registry finds entity, state is numeric minutes -> returns minutes*60."""
        import sys

        valve = _make_valve()

        mock_registry = MagicMock()
        mock_registry.async_get_entity_id.return_value = "number.rainpoint_valve_zone1_duration"
        mock_er_module = MagicMock()
        mock_er_module.async_get.return_value = mock_registry

        fake_state = MagicMock()
        fake_state.state = "5"  # 5 minutes -> 300 seconds
        valve.hass.states.get.return_value = fake_state

        monkeypatch.setitem(sys.modules, "homeassistant.helpers.entity_registry", mock_er_module)

        assert valve._get_configured_duration_seconds() == 300

    def test_get_configured_duration_rejects_non_numeric_state(self, monkeypatch):
        """Non-numeric state ('unknown') falls through to the default."""
        import sys

        valve = _make_valve()

        mock_registry = MagicMock()
        mock_registry.async_get_entity_id.return_value = "number.rainpoint_valve_zone1_duration"
        mock_er_module = MagicMock()
        mock_er_module.async_get.return_value = mock_registry

        fake_state = MagicMock()
        fake_state.state = "unknown"
        valve.hass.states.get.return_value = fake_state

        monkeypatch.setitem(sys.modules, "homeassistant.helpers.entity_registry", mock_er_module)

        assert valve._get_configured_duration_seconds() == DEFAULT_DURATION_SECONDS

    def test_get_configured_duration_min_floor_of_one_second(self, monkeypatch):
        """Fractional minutes always produce at least 1 second (min-floor guard)."""
        import sys

        valve = _make_valve()

        mock_registry = MagicMock()
        mock_registry.async_get_entity_id.return_value = "number.rainpoint_valve_zone1_duration"
        mock_er_module = MagicMock()
        mock_er_module.async_get.return_value = mock_registry

        fake_state = MagicMock()
        fake_state.state = "0.001"  # ~0.06s rounds to 0 -> floor to 1
        valve.hass.states.get.return_value = fake_state

        monkeypatch.setitem(sys.modules, "homeassistant.helpers.entity_registry", mock_er_module)

        assert valve._get_configured_duration_seconds() == 1


class TestValveInit:
    """Tests for RainPointValveEntity.__init__ (lines 75-86)."""

    def test_init_builds_unique_id_and_name(self):
        """__init__ populates unique_id and name using hid/mid/addr/sub_name/zone."""
        from custom_components.rainpoint.valve import RainPointValveEntity

        mock_coordinator = MagicMock()
        mock_coordinator.data = {"sensors": {}}
        sensor_info = {
            "hid": 10,
            "mid": 20,
            "addr": 3,
            "sub_name": "Backyard",
            "model": "HTV245FRF",
        }

        valve = RainPointValveEntity(mock_coordinator, "10_20_3", sensor_info, 2)

        assert valve._sensor_key == "10_20_3"
        assert valve._zone_num == 2
        assert valve._attr_unique_id == "rainpoint_10_20_3_zone2"
        assert valve._attr_name == "Backyard Zone 2"

    def test_init_defaults_sub_name_when_missing(self):
        """Missing sub_name falls back to 'Valve Hub {addr}'."""
        from custom_components.rainpoint.valve import RainPointValveEntity

        mock_coordinator = MagicMock()
        mock_coordinator.data = {"sensors": {}}
        sensor_info = {"hid": 1, "mid": 2, "addr": 7, "model": "HTV245FRF"}

        valve = RainPointValveEntity(mock_coordinator, "1_2_7", sensor_info, 1)

        assert valve._attr_name == "Valve Hub 7 Zone 1"


class TestValveSetupEntry:
    """Tests for valve.async_setup_entry (lines 31-58)."""

    @pytest.mark.asyncio
    async def test_setup_entry_creates_one_entity_per_zone(self):
        """One valve entity per zone reported in the decoded payload."""
        from custom_components.rainpoint.valve import async_setup_entry

        sensors = {
            "10_20_1": {
                "hid": 10,
                "mid": 20,
                "addr": 1,
                "sub_name": "Hub A",
                "model": MODEL_VALVE_245,
                "data": {
                    "hub_online": True,
                    "zones": {
                        1: {"open": False, "duration_seconds": 0, "state_raw": 0},
                        2: {"open": True, "duration_seconds": 300, "state_raw": 1},
                    },
                },
            }
        }
        mock_coordinator = MagicMock()
        mock_coordinator.data = {"sensors": sensors}

        hass = MagicMock()
        entry = MagicMock()
        entry.entry_id = "e1"
        hass.data = {DOMAIN: {"e1": {"coordinator": mock_coordinator}}}

        captured = []
        async_add_entities = MagicMock(side_effect=lambda ents, **kw: captured.extend(ents))

        await async_setup_entry(hass, entry, async_add_entities)

        assert len(captured) == 2
        # Zones are processed in sorted order
        assert captured[0]._zone_num == 1
        assert captured[1]._zone_num == 2

    @pytest.mark.asyncio
    async def test_setup_entry_creates_entities_for_valve_345(self):
        """HTV345FRF creates one valve entity per reported zone."""
        from custom_components.rainpoint.valve import async_setup_entry

        sensors = {
            "10_20_1": {
                "hid": 10,
                "mid": 20,
                "addr": 1,
                "sub_name": "HTV345",
                "model": MODEL_VALVE_345,
                "data": {
                    "hub_online": True,
                    "zones": {
                        1: {"open": False, "duration_seconds": 0, "state_raw": 0},
                        2: {"open": False, "duration_seconds": 0, "state_raw": 0},
                        3: {"open": True, "duration_seconds": 300, "state_raw": 1},
                    },
                },
            }
        }
        mock_coordinator = MagicMock()
        mock_coordinator.data = {"sensors": sensors}

        hass = MagicMock()
        entry = MagicMock()
        entry.entry_id = "e1"
        hass.data = {DOMAIN: {"e1": {"coordinator": mock_coordinator}}}

        captured = []
        async_add_entities = MagicMock(side_effect=lambda ents, **kw: captured.extend(ents))

        await async_setup_entry(hass, entry, async_add_entities)

        assert [entity._zone_num for entity in captured] == [1, 2, 3]
        assert all(entity._sensor_info["model"] == MODEL_VALVE_345 for entity in captured)

    @pytest.mark.asyncio
    async def test_setup_entry_skips_non_valve_models(self):
        """Non-valve models are skipped; no entities created."""
        from custom_components.rainpoint.valve import async_setup_entry

        sensors = {
            "10_20_1": {
                "hid": 10,
                "mid": 20,
                "addr": 1,
                "model": "HCS021FRF",  # not a valve model
                "data": {"hub_online": True, "zones": {1: {"open": False}}},
            }
        }
        mock_coordinator = MagicMock()
        mock_coordinator.data = {"sensors": sensors}

        hass = MagicMock()
        entry = MagicMock()
        entry.entry_id = "e1"
        hass.data = {DOMAIN: {"e1": {"coordinator": mock_coordinator}}}

        async_add_entities = MagicMock()
        await async_setup_entry(hass, entry, async_add_entities)

        assert not async_add_entities.called

    @pytest.mark.asyncio
    async def test_setup_entry_creates_no_valve_for_a_silent_entry(self):
        """A valve model with no status at all (raw_status={}, D-11/D-12) has no
        zones to walk, so it produces no valve entity rather than raising."""
        from custom_components.rainpoint.valve import async_setup_entry

        sensors = {
            "10_20_1": {
                "hid": 10,
                "mid": 20,
                "addr": 1,
                "sub_name": "Hub A",
                "model": MODEL_VALVE_245,
                "raw_status": {},
                "data": {"type": SILENT_DATA_TYPE, "silent_state": "never_reported"},
            }
        }
        mock_coordinator = MagicMock()
        mock_coordinator.data = {"sensors": sensors}

        hass = MagicMock()
        entry = MagicMock()
        entry.entry_id = "e1"
        entry.options = {}
        hass.data = {DOMAIN: {"e1": {"coordinator": mock_coordinator}}}

        async_add_entities = MagicMock()
        await async_setup_entry(hass, entry, async_add_entities)

        assert not async_add_entities.called

    @pytest.mark.asyncio
    async def test_setup_entry_skips_non_dict_sensor_records(self):
        """A malformed sub-device record must not abort setup and drop valid valve entities."""
        from custom_components.rainpoint.valve import async_setup_entry

        sensors = {
            "bad": "not-a-dict",
            "10_20_1": {
                "hid": 10,
                "mid": 20,
                "addr": 1,
                "model": MODEL_VALVE_245,
                "data": {"hub_online": True, "zones": {1: {"open": False, "duration_seconds": 0, "state_raw": 0}}},
            },
        }
        mock_coordinator = MagicMock()
        mock_coordinator.data = {"sensors": sensors}

        hass = MagicMock()
        entry = MagicMock()
        entry.entry_id = "e1"
        entry.options = {}
        hass.data = {DOMAIN: {"e1": {"coordinator": mock_coordinator}}}

        captured = []
        async_add_entities = MagicMock(side_effect=lambda ents, **kw: captured.extend(ents))
        await async_setup_entry(hass, entry, async_add_entities)

        assert len(captured) == 1
        assert captured[0]._zone_num == 1

    @pytest.mark.asyncio
    async def test_setup_entry_skips_when_no_zones(self):
        """Valve model with empty zones dict produces no entities."""
        from custom_components.rainpoint.valve import async_setup_entry

        sensors = {
            "10_20_1": {
                "hid": 10,
                "mid": 20,
                "addr": 1,
                "model": MODEL_VALVE_245,
                "data": {"hub_online": False, "zones": {}},
            }
        }
        mock_coordinator = MagicMock()
        mock_coordinator.data = {"sensors": sensors}

        hass = MagicMock()
        entry = MagicMock()
        entry.entry_id = "e1"
        hass.data = {DOMAIN: {"e1": {"coordinator": mock_coordinator}}}

        async_add_entities = MagicMock()
        await async_setup_entry(hass, entry, async_add_entities)

        assert not async_add_entities.called

    @pytest.mark.asyncio
    async def test_setup_entry_handles_missing_data(self):
        """Sensor entry with no 'data' key yields empty zones -> no entities."""
        from custom_components.rainpoint.valve import async_setup_entry

        sensors = {
            "10_20_1": {
                "hid": 10,
                "mid": 20,
                "addr": 1,
                "model": MODEL_VALVE_245,
                # No "data" key at all
            }
        }
        mock_coordinator = MagicMock()
        mock_coordinator.data = {"sensors": sensors}

        hass = MagicMock()
        entry = MagicMock()
        entry.entry_id = "e1"
        hass.data = {DOMAIN: {"e1": {"coordinator": mock_coordinator}}}

        async_add_entities = MagicMock()
        await async_setup_entry(hass, entry, async_add_entities)

        assert not async_add_entities.called


class TestValveExtraAttributes:
    """Cover branches in extra_state_attributes (lines 131-150)."""

    def test_extra_attrs_device_timestamp_present(self):
        """device_timestamp in data flows through with method/source."""
        valve = _make_valve()
        sensors = valve.coordinator.data["sensors"]
        sensors["100_200_1"]["data"]["device_timestamp"] = "2024-01-01T00:00:00+00:00"
        sensors["100_200_1"]["data"]["timestamp_method"] = "rtc"
        sensors["100_200_1"]["data"]["timestamp_source"] = "device"

        attrs = valve.extra_state_attributes
        assert attrs["device_timestamp"] == "2024-01-01T00:00:00+00:00"
        assert attrs["timestamp_method"] == "rtc"
        assert attrs["timestamp_source"] == "device"

    def test_extra_attrs_server_timestamp_fallback(self):
        """server_timestamp used when device_timestamp missing."""
        valve = _make_valve()
        sensors = valve.coordinator.data["sensors"]
        sensors["100_200_1"]["data"]["server_timestamp"] = "2024-02-02T00:00:00+00:00"

        attrs = valve.extra_state_attributes
        assert attrs["device_timestamp"] == "2024-02-02T00:00:00+00:00"
        assert attrs["timestamp_source"] == "server"

    def test_extra_attrs_no_zone_no_firmware(self):
        """No zone data and no firmware_version -> empty attrs (aside from possibly timestamps)."""
        valve = _make_valve()
        sensors = valve.coordinator.data["sensors"]
        sensors["100_200_1"]["data"]["zones"] = {}  # zone 1 vanishes
        sensors["100_200_1"].pop("firmware_version", None)

        attrs = valve.extra_state_attributes
        assert "duration_seconds" not in attrs
        assert "firmware_version" not in attrs
        assert "device_timestamp" not in attrs

    def test_extra_attrs_zone_without_duration_still_emits_state_raw(self):
        """Zone dict without duration_seconds still sets state_raw."""
        valve = _make_valve(zone_data={"open": True, "state_raw": 9})
        attrs = valve.extra_state_attributes
        assert attrs["state_raw"] == 9
        assert "duration_seconds" not in attrs


class TestApplyResponseStateBranches:
    """Cover _apply_response_state edge branches (lines 213, 215, 219)."""

    def test_apply_response_state_uses_valve_hub_decoder_for_non_213_245(self, monkeypatch):
        """Model not in (213, 245) routes through decode_valve_hub and short-circuits on falsy decode."""
        from custom_components.rainpoint import valve as valve_mod

        valve = _make_valve(model=MODEL_VALVE_245)
        valve._sensor_info["model"] = "HWV100FRF"  # unknown valve-hub variant
        valve.coordinator.async_set_updated_data = MagicMock()

        spy = MagicMock(return_value=None)
        monkeypatch.setattr(valve_mod, "decode_valve_hub", spy)

        valve._apply_response_state("whatever-payload")

        spy.assert_called_once_with("whatever-payload")
        valve.coordinator.async_set_updated_data.assert_not_called()

    def test_apply_response_state_uses_htv_decoder_for_valve_345(self, monkeypatch):
        """HTV345FRF routes control responses through the shared HTV213/245 decoder."""
        from custom_components.rainpoint import valve as valve_mod

        valve = _make_valve(model=MODEL_VALVE_345)
        valve.coordinator.async_set_updated_data = MagicMock()

        decoded = {
            "type": "valve_hub",
            "hub_online": True,
            "zones": {1: {"open": False, "duration_seconds": 0, "state_raw": 0}},
        }
        spy = MagicMock(return_value=decoded)
        monkeypatch.setattr(valve_mod, "decode_htv213frf_valve", spy)

        valve._apply_response_state("whatever-payload")

        spy.assert_called_once_with("whatever-payload")
        valve.coordinator.async_set_updated_data.assert_called_once()

    def test_apply_response_state_key_missing_in_sensors(self):
        """If the sensor_key is not in coordinator.data['sensors'], return without update."""
        valve = _make_valve(model=MODEL_VALVE_245)
        valve._sensor_key = "not_in_data"
        valve.coordinator.async_set_updated_data = MagicMock()

        valve._apply_response_state("1,-84,1;1,0,0,300;0,0,0,0")

        valve.coordinator.async_set_updated_data.assert_not_called()

    def test_apply_response_state_decoder_returns_empty(self, monkeypatch):
        """decode_valve_hub returning falsy short-circuits before async_set_updated_data."""
        from custom_components.rainpoint import valve as valve_mod

        valve = _make_valve(model=MODEL_VALVE_245)
        valve._sensor_info["model"] = "HWV100FRF"  # not in (VALVE_213, VALVE_245)
        valve.coordinator.async_set_updated_data = MagicMock()

        monkeypatch.setattr(valve_mod, "decode_valve_hub", lambda raw: None)
        valve._apply_response_state("anything")

        valve.coordinator.async_set_updated_data.assert_not_called()


class TestValveZoneDataEdges:
    """Cover _zone_data/available branches when sensors/info/data missing."""

    def test_zone_data_returns_none_when_sensor_key_absent(self):
        """Sensor key not in coordinator.data['sensors'] -> _zone_data is None."""
        valve = _make_valve()
        valve._sensor_key = "missing"
        assert valve._zone_data is None

    def test_zone_data_returns_none_when_data_absent(self):
        """Sensor entry present but 'data' is falsy -> _zone_data is None."""
        valve = _make_valve()
        valve.coordinator.data["sensors"]["100_200_1"]["data"] = None
        assert valve._zone_data is None

    def test_available_false_when_sensor_key_absent(self):
        """available returns False when sensor key not in sensors dict."""
        valve = _make_valve()
        valve._sensor_key = "missing"
        assert valve.available is False


class TestValveEntitiesAppearWithinTheSession:
    """A valve reporting no zones at setup still gains them later.

    Drives the real order rather than injecting a ready-made snapshot:
    construct, first refresh, platform setup, then further refreshes. The
    entity set is otherwise frozen at the first refresh, so a valve that is
    silent or zone-less when the platform sets up would never gain a control
    entity in that session no matter how long it ran.
    """

    @staticmethod
    def _hub(zones_reported):
        """A valve hub record whose child reports zones only when asked to."""
        return make_valve_zone_status(zones_reported=zones_reported)

    @pytest.mark.asyncio
    async def test_zones_arriving_after_setup_create_their_valve_entities(self):
        """The listener is what turns a later poll into a control entity."""
        from custom_components.rainpoint.const import CONF_HIDS
        from custom_components.rainpoint.coordinator import RainPointCoordinator
        from custom_components.rainpoint.valve import async_setup_entry

        client = AsyncMock()
        client.get_devices_by_hid.return_value = [
            {
                "mid": 20,
                "name": "Hub A",
                "deviceName": "d",
                "productKey": "pk",
                "homeName": "H",
                "subDevices": [{"addr": 1, "name": "Hub A", "model": MODEL_VALVE_245, "softVer": "127"}],
            }
        ]
        client.get_multiple_device_status.return_value = self._hub(zones_reported=False)

        entry = MagicMock()
        entry.entry_id = "e1"
        entry.data = {CONF_HIDS: [10]}
        entry.options = {}
        hass = MagicMock()
        hass.data = {DOMAIN: {"e1": {}}}

        coordinator = RainPointCoordinator(hass, client, entry)
        hass.data[DOMAIN]["e1"]["coordinator"] = coordinator

        captured = []
        async_add_entities = MagicMock(side_effect=lambda ents, **kw: captured.extend(ents))

        await coordinator.async_config_entry_first_refresh()
        await async_setup_entry(hass, entry, async_add_entities)

        assert captured == []
        assert coordinator._listeners

        client.get_multiple_device_status.return_value = self._hub(zones_reported=True)
        await coordinator.async_refresh()

        assert [e._zone_num for e in captured] == [1, 2]

        # Steady state must not offer the same zones a second time.
        await coordinator.async_refresh()
        assert [e._zone_num for e in captured] == [1, 2]

    @pytest.mark.asyncio
    async def test_a_zone_arriving_later_joins_the_first_zones_ledger_entry(self):
        """The append rule, driven through a real coordinator timeline.

        A per-zone platform emits for one key across several polls, so a
        ledger that replaced rather than appended would leave the first zone's
        row unrecorded and therefore unremovable, while every other test in
        the suite still passed.
        """
        from custom_components.rainpoint.const import CONF_HIDS
        from custom_components.rainpoint.coordinator import RainPointCoordinator
        from custom_components.rainpoint.entity import late_adders
        from custom_components.rainpoint.valve import async_setup_entry

        client = AsyncMock()
        client.get_devices_by_hid.return_value = [
            {
                "mid": 20,
                "name": "Hub A",
                "deviceName": "d",
                "productKey": "pk",
                "homeName": "H",
                "subDevices": [{"addr": 1, "name": "Hub A", "model": MODEL_VALVE_245, "softVer": "127"}],
            }
        ]
        # The captured two-zone frame with zone 2's entries removed, so the
        # first poll reports one zone and the second reports both.
        one_zone = [{"mid": 20, "subDeviceStatus": [{"id": "D01", "value": ONE_ZONE_TLV_PAYLOAD, "time": 1785420002247}]}]
        client.get_multiple_device_status.return_value = one_zone

        entry = MagicMock()
        entry.entry_id = "e1"
        entry.data = {CONF_HIDS: [10]}
        entry.options = {}
        hass = MagicMock()
        hass.data = {DOMAIN: {"e1": {}}}

        coordinator = RainPointCoordinator(hass, client, entry)
        hass.data[DOMAIN]["e1"]["coordinator"] = coordinator

        offered = []
        async_add_entities = MagicMock(side_effect=lambda ents, **kw: offered.extend(e._attr_unique_id for e in ents))

        await coordinator.async_config_entry_first_refresh()
        await async_setup_entry(hass, entry, async_add_entities)

        key = "10_20_1"
        adder = late_adders(hass.data[DOMAIN]["e1"])[0]
        assert adder.ledger.unique_ids_for(key) == frozenset({"rainpoint_10_20_1_zone1"})

        client.get_multiple_device_status.return_value = make_valve_zone_status(zones_reported=True)
        await coordinator.async_refresh()

        assert adder.ledger.unique_ids_for(key) == frozenset({"rainpoint_10_20_1_zone1", "rainpoint_10_20_1_zone2"})
        assert offered.count("rainpoint_10_20_1_zone1") == 1


def _valve_hub_status(connected_value):
    """One valve hub whose D01 sub-device reports open zones on every poll
    (never silent) and whose hub-level connected id carries the given value."""
    return [
        {
            "mid": 200,
            "subDeviceStatus": [
                {"id": "D01", "value": SAMPLE_HTV245_ASCII_PAYLOAD, "time": 1785420002247},
                {"id": "connected", "value": connected_value, "time": 1785420002247},
            ],
        }
    ]


async def _build_valve_availability_timeline():
    """Drive construct -> first refresh (connected) -> platform setup and
    return (coordinator, client, valve).

    The shared preamble of every valve-availability timeline test: a real
    entity object built by the real platform setup off a real first refresh,
    rather than an injected coordinator.data snapshot. Callers keep their own
    assertions and drive their own subsequent polls and pushes off the
    returned client and coordinator.
    """
    from custom_components.rainpoint.const import CONF_HIDS
    from custom_components.rainpoint.coordinator import RainPointCoordinator
    from custom_components.rainpoint.valve import async_setup_entry

    client = AsyncMock()
    client.get_devices_by_hid.return_value = [
        {
            "mid": 200,
            "name": "Hub A",
            "deviceName": "d",
            "productKey": "pk",
            "homeName": "H",
            "subDevices": [{"addr": 1, "name": "Hub A", "model": MODEL_VALVE_245, "softVer": "127"}],
        }
    ]
    client.get_multiple_device_status.return_value = _valve_hub_status(connected_value="1")

    entry = MagicMock()
    entry.entry_id = "e1"
    entry.data = {CONF_HIDS: [100]}
    entry.options = {}
    hass = MagicMock()
    hass.data = {DOMAIN: {"e1": {}}}

    coordinator = RainPointCoordinator(hass, client, entry)
    hass.data[DOMAIN]["e1"]["coordinator"] = coordinator

    await coordinator.async_config_entry_first_refresh()

    captured = []
    async_add_entities = MagicMock(side_effect=lambda ents, **kw: captured.extend(ents))
    await async_setup_entry(hass, entry, async_add_entities)

    assert len(captured) >= 1
    return coordinator, client, captured[0]


class TestValveAvailabilityRealTimeline:
    """Drives the real construct -> first refresh -> platform setup ->
    refresh sequence rather than an injected coordinator.data snapshot, so
    the connected-to-disconnected transition is proven on an
    already-constructed entity object."""

    @pytest.mark.asyncio
    async def test_connected_to_disconnected_transition_moves_available(self):
        """Construct, first refresh with connected '1', platform setup,
        assert available True, then a refresh with connected flipped to '0'
        moves the same entity object to available False -- no reload, no
        second setup."""
        coordinator, client, valve = await _build_valve_availability_timeline()
        assert valve.available is True

        client.get_multiple_device_status.return_value = _valve_hub_status(connected_value="0")
        await coordinator.async_refresh()

        assert valve.available is False


class TestValveAvailabilityPushedReconnect:
    """A pushed reconnect must close the valve-availability gate at
    push latency, with no async_refresh() between the pushed dispatch and
    the read that follows it. Companion to TestValveAvailabilityRealTimeline
    above (poll-only) and to TestHubConnectivityPushClearInterleavedTimeline
    in tests/test_coordinator.py, which asserts the Repairs-card
    raise/clear call counts on a fixture that deliberately carries no
    sub-device -- this class's fixture reports the valve sub-device on
    every poll instead, so the two never collide. Do not assert
    ir.async_create_issue / ir.async_delete_issue call counts here, and do
    not merge the two classes back together."""

    @pytest.mark.asyncio
    async def test_pushed_reconnect_flips_availability_and_hub_connected_before_any_poll(self):
        """Construct, first refresh connected, platform setup, poll to
        disconnected (unavailable), then a pushed reconnect with no
        async_refresh() in between moves the same entity object to
        available True and its hub_connected attribute to True."""
        coordinator, client, valve = await _build_valve_availability_timeline()
        assert valve.available is True

        client.get_multiple_device_status.return_value = _valve_hub_status(connected_value="0")
        await coordinator.async_refresh()

        assert valve.available is False
        assert valve.extra_state_attributes["hub_connected"] is False

        # The pushed reconnect: the third pipe-delimited field of
        # SAMPLE_HUB_RECONNECT_FRAME (tests/payload_samples.py), strictly
        # newer than the held poll moment (1785420002247 ms) so the ordering
        # guard admits it. No async_refresh() runs between this dispatch and
        # the reads below.
        coordinator.apply_hub_push_update(200, True, 1785523062039)

        assert valve.available is True
        assert valve.extra_state_attributes["hub_connected"] is True


async def _build_dp_valve_tracer_timeline():
    """Construct -> first refresh -> platform setup for a hub-paired HTV210B.

    Returns (coordinator, client, entities), entities sorted by zone number.
    Mirrors _build_valve_availability_timeline's shape for the DP endpoint's
    own fixture rather than reusing it, since this one needs a different
    model and a different subDevices shape (modelCode carried).
    """
    from custom_components.rainpoint.const import CONF_HIDS
    from custom_components.rainpoint.coordinator import RainPointCoordinator
    from custom_components.rainpoint.valve import async_setup_entry

    client = AsyncMock()
    client.get_devices_by_hid.return_value = htv210b_hub_devices()
    client.get_multiple_device_status.return_value = htv210b_status()

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

    captured.sort(key=lambda e: e._zone_num)
    return coordinator, client, captured


def _bind_real_dp_control(client, response_body):
    """Point client.control_work_mode_dp at a real client method reading response_body.

    Returns the real client so a test can assert on the exact JSON body its
    session.post received -- the far end of the stack, not a mocked call.
    """
    real_client = make_mock_session_client()
    real_client.ensure_logged_in = AsyncMock()
    real_client._session.post = MagicMock(return_value=mock_json_response(response_body))
    client.control_work_mode_dp = real_client.control_work_mode_dp
    return real_client


class TestDpValveTracer:
    """End to end: open one HTV210B zone through controlWorkModeDP and decode
    the response's own state blob.

    Covers the open path only. TestDpValveCloseAndCodeFour covers close and the
    code-4 response, and TestDpApplyResponseStateBranches covers
    _apply_response_state's guard clauses.
    """

    @pytest.mark.asyncio
    async def test_setup_builds_dp_entities_for_both_zones(self):
        """A hub-paired HTV210B produces RainPointDpValveEntity for zones 1 and 2."""
        _coordinator, _client, entities = await _build_dp_valve_tracer_timeline()

        assert [e._zone_num for e in entities] == [1, 2]
        assert all(isinstance(e, RainPointDpValveEntity) for e in entities)

    @pytest.mark.asyncio
    async def test_open_posts_captured_body_and_applies_response_state(self):
        """Opening zone 1 posts the captured controlWorkModeDP body and the
        response is decoded straight into that entity's read-back state."""
        _coordinator, client, entities = await _build_dp_valve_tracer_timeline()
        zone1 = entities[0]

        real_client = _bind_real_dp_control(client, {"code": 0, "data": {"state": "1,D821AF3C000000B7D1230B1A"}})

        await zone1.async_open_valve(duration=60)

        real_client._session.post.assert_called_once()
        call = real_client._session.post.call_args
        url = call.args[0]
        body = call.kwargs["json"]

        assert url.endswith("/app/device/controlWorkModeDP")
        assert set(body.keys()) == {"mid", "productKey", "deviceName", "mode", "addr", "port", "param", "dpCode"}
        assert body["mode"] == 1
        assert body["port"] == 1
        assert body["dpCode"] == 1
        assert body["param"] == "3C000000"
        assert "duration" not in body

        assert zone1.is_closed is False
        attrs = zone1.extra_state_attributes
        assert attrs["duration_seconds"] == 60
        assert attrs["state_raw"] == 0x21
        assert attrs["event_time"] == "2026-08-05T18:15:17"

    @pytest.mark.asyncio
    async def test_open_merges_zone_and_leaves_the_rest_of_the_poll_intact(self):
        """The merge preserves battery/rssi/hub_online and zone 2's poll-derived entry."""
        coordinator, client, entities = await _build_dp_valve_tracer_timeline()
        zone1 = entities[0]
        key = "10_20_1"
        before = coordinator.data["sensors"][key]["data"]
        assert before["battery_flag"] is not None
        assert before["rssi_dbm"] is not None
        zone2_before = before["zones"][2]

        _bind_real_dp_control(client, {"code": 0, "data": {"state": "1,D821AF3C000000B7D1230B1A"}})

        await zone1.async_open_valve(duration=60)

        after = coordinator.data["sensors"][key]["data"]
        assert after["battery_flag"] == before["battery_flag"]
        assert after["rssi_dbm"] == before["rssi_dbm"]
        assert after["hub_online"] == before["hub_online"]
        assert after["zones"][2] == zone2_before
        assert after["zones"][1]["open"] is True


class TestSilentUnitGuardRealTimeline:
    """The explicit silent-type guard in valve.py's build(), proven through the
    real coordinator-then-setup-then-refresh sequence rather than an injected
    coordinator.data snapshot.

    A Bluetooth-only HTV210B carries model == HTV210B on its silent entry, so
    the model-set check in build() alone would admit it; the silent type is
    what actually discriminates. These tests drive the real timeline so the
    silence genuinely develops (SILENT_DEBOUNCE_POLLS consecutive
    arrived-but-silent polls) before asserting the guard, and the
    silent-then-reporting / reporting-then-silent halves prove the guard
    blocks creation only, never removal.
    """

    @staticmethod
    async def _build_silent_timeline():
        """Construct -> first refresh -> platform setup for an HTV210B that
        never reports. Returns (coordinator, client, hass, entry, captured)."""
        from custom_components.rainpoint.const import CONF_HIDS
        from custom_components.rainpoint.coordinator import RainPointCoordinator
        from custom_components.rainpoint.valve import async_setup_entry

        client = AsyncMock()
        client.get_devices_by_hid.return_value = htv210b_hub_devices()
        client.get_multiple_device_status.return_value = htv210b_silent_status()

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

        return coordinator, client, hass, entry, captured

    @staticmethod
    async def _build_reporting_timeline():
        """Construct -> first refresh -> platform setup for an HTV210B that
        reports normally from the start. Returns (coordinator, client, hass, entry, captured)."""
        from custom_components.rainpoint.const import CONF_HIDS
        from custom_components.rainpoint.coordinator import RainPointCoordinator
        from custom_components.rainpoint.valve import async_setup_entry

        client = AsyncMock()
        client.get_devices_by_hid.return_value = htv210b_hub_devices()
        client.get_multiple_device_status.return_value = htv210b_status()

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

        return coordinator, client, hass, entry, captured

    @pytest.mark.asyncio
    async def test_a_silent_from_the_start_htv210b_never_offers_a_valve_entity(self):
        """No valve entity across the whole silence timeline, even once the
        debounce elapses and the entry's model alone would have admitted it."""
        coordinator, _client, _hass, _entry, captured = await self._build_silent_timeline()
        key = "10_20_1"
        assert captured == []

        # Every refresh short of the debounce: the key has no trace at all yet.
        for _ in range(SILENT_DEBOUNCE_POLLS - 2):
            await coordinator.async_refresh()
            assert key not in coordinator.data["sensors"]
            assert captured == []

        # The debounce elapses: a silent entry appears, proving the model-set
        # gate alone would have admitted it, and still no entity is offered.
        await coordinator.async_refresh()
        entry = coordinator.data["sensors"][key]
        assert entry["data"]["type"] == SILENT_DATA_TYPE
        assert entry["model"] == MODEL_HTV210B
        assert captured == []

    @pytest.mark.asyncio
    async def test_a_silent_htv210b_that_starts_reporting_gains_its_valve_entities(self):
        """The late adder promotes the entry the moment it stops being silent,
        through the same coordinator listener, with no reload and no second
        async_setup_entry call."""
        coordinator, client, _hass, _entry, captured = await self._build_silent_timeline()
        for _ in range(SILENT_DEBOUNCE_POLLS - 1):
            await coordinator.async_refresh()
        key = "10_20_1"
        assert coordinator.data["sensors"][key]["data"]["type"] == SILENT_DATA_TYPE
        assert captured == []

        client.get_multiple_device_status.return_value = htv210b_status()
        await coordinator.async_refresh()

        assert [e._zone_num for e in captured] == [1, 2]
        assert all(isinstance(e, RainPointDpValveEntity) for e in captured)

    @pytest.mark.asyncio
    async def test_a_reporting_htv210b_that_goes_silent_keeps_its_valve_entities(self):
        """The guard blocks creation only: entities already offered are never
        withdrawn once the same device later goes silent."""
        from custom_components.rainpoint.entity import late_adders

        coordinator, client, hass, entry, captured = await self._build_reporting_timeline()
        assert [e._zone_num for e in captured] == [1, 2]
        offered_before = list(captured)

        client.get_multiple_device_status.return_value = htv210b_silent_status()
        for _ in range(SILENT_DEBOUNCE_POLLS):
            await coordinator.async_refresh()

        key = "10_20_1"
        assert coordinator.data["sensors"][key]["data"]["type"] == SILENT_DATA_TYPE
        assert captured == offered_before

        adder = late_adders(hass.data[DOMAIN][entry.entry_id])[0]
        assert adder.ledger.unique_ids_for(key) == frozenset({"rainpoint_10_20_1_zone1", "rainpoint_10_20_1_zone2"})


class TestDpValveCloseAndCodeFour:
    """Close, and the code-4 already-in-state response, on the DP entity."""

    @pytest.mark.asyncio
    async def test_close_posts_zeroed_param_and_applies_response(self):
        """A close posts mode 0 and param 00000000 with no duration key, and
        the closed response blob is applied to the entity's read-back state."""
        _coordinator, client, entities = await _build_dp_valve_tracer_timeline()
        zone1 = entities[0]

        real_client = _bind_real_dp_control(client, {"code": 0, "data": {"state": "0,D800AF00000000B700000000"}})

        await zone1.async_close_valve()

        body = real_client._session.post.call_args.kwargs["json"]
        assert body["mode"] == 0
        assert body["param"] == "00000000"
        assert "duration" not in body
        assert zone1.is_closed is True

    @pytest.mark.asyncio
    async def test_code_4_response_still_records_command_and_applies_state(self):
        """Code 4 (already in the requested state) completes the command exactly
        as code 0 does: the staleness guard is armed and the blob is applied."""
        coordinator, client, entities = await _build_dp_valve_tracer_timeline()
        zone1 = entities[0]

        _bind_real_dp_control(client, {"code": 4, "data": {"state": "1,D821AF3C000000B7D1230B1A"}})

        await zone1.async_open_valve(duration=60)

        assert ("10_20_1", 1) in coordinator._last_valve_command_at
        assert zone1.is_closed is False
        assert zone1.extra_state_attributes["duration_seconds"] == 60


def _make_dp_valve(zone_data=None, hub_online=True):
    """Create a RainPointDpValveEntity with a mock coordinator, bypassing __init__.

    A lighter-weight sibling of _make_valve for the DP subclass's own
    _apply_response_state guard-clause branches, which do not need the full
    real-coordinator timeline the tracer tests above drive.
    """
    sensor_key = "100_200_1"
    sensor_info = {
        "hid": 100,
        "mid": 200,
        "addr": 1,
        "sub_name": "BT Valve",
        "model": MODEL_HTV210B,
        "device_name": "dev1",
        "product_key": "pk1",
        "firmware_version": "1.0",
    }
    decoded = {
        "hub_online": hub_online,
        "battery_flag": 1,
        "rssi_dbm": -70,
        "zones": {
            1: zone_data
            if zone_data is not None
            else {"open": True, "duration_seconds": 60, "state_raw": 0x21, "event_time": None}
        },
    }
    mock_coordinator = make_sensor_coordinator(
        model=MODEL_HTV210B,
        data=decoded,
        sub_name="BT Valve",
        firmware_version="1.0",
        extra_sensor_info={"device_name": "dev1", "product_key": "pk1"},
    )

    valve = RainPointDpValveEntity.__new__(RainPointDpValveEntity)
    valve.coordinator = mock_coordinator
    valve._sensor_key = sensor_key
    valve._sensor_info = sensor_info
    valve._zone_num = 1
    valve.hass = MagicMock()
    valve._attr_unique_id = "rainpoint_100_200_1_zone1"
    valve._attr_name = "BT Valve Zone 1"
    return valve


class TestDpApplyResponseStateBranches:
    """Cover RainPointDpValveEntity._apply_response_state's guard clauses."""

    def test_apply_response_state_none_skips(self):
        """A None response is a no-op, matching the RF parent's shape."""
        valve = _make_dp_valve()
        valve.coordinator.async_set_updated_data = MagicMock()

        valve._apply_response_state(None)

        valve.coordinator.async_set_updated_data.assert_not_called()

    def test_apply_response_state_empty_skips(self):
        """An empty-string response is a no-op."""
        valve = _make_dp_valve()
        valve.coordinator.async_set_updated_data = MagicMock()

        valve._apply_response_state("")

        valve.coordinator.async_set_updated_data.assert_not_called()

    def test_apply_response_state_decoder_returns_none_skips(self, monkeypatch):
        """A malformed blob the decoder rejects short-circuits before the merge."""
        from custom_components.rainpoint import valve as valve_mod

        valve = _make_dp_valve()
        valve.coordinator.async_set_updated_data = MagicMock()
        monkeypatch.setattr(valve_mod, "decode_htv210b_dp_state", lambda raw: None)

        valve._apply_response_state("garbage")

        valve.coordinator.async_set_updated_data.assert_not_called()

    def test_apply_response_state_key_missing_in_sensors(self):
        """If the sensor_key is not in coordinator.data['sensors'], return without update."""
        valve = _make_dp_valve()
        valve._sensor_key = "not_in_data"
        valve.coordinator.async_set_updated_data = MagicMock()

        valve._apply_response_state("1,D821AF3C000000B7D1230B1A")

        valve.coordinator.async_set_updated_data.assert_not_called()


class TestEncodeDpDurationParam:
    """The pure seconds-to-param hex encoder."""

    @pytest.mark.parametrize(
        ("seconds", "expected"),
        [
            (60, "3C000000"),
            (120, "78000000"),
            (0, "00000000"),
            (600, "58020000"),
            (3600, "100E0000"),
        ],
    )
    def test_known_values(self, seconds, expected):
        """Established encoding checkpoints from the capture."""
        assert _encode_dp_duration_param(seconds) == expected

    def test_repeated_calls_return_identical_string(self):
        """The encoder holds no state, so an interleaved call cannot disturb a repeat.

        Both halves assert against the expected literal rather than against
        each other. Comparing one call to another would pass just as happily if
        the encoder returned the same wrong answer twice, and the interleaved
        call is what actually distinguishes a stateless encoder from one whose
        output depends on what it was asked last.
        """
        expected = "58020000"

        assert _encode_dp_duration_param(600) == expected
        _encode_dp_duration_param(120)
        assert _encode_dp_duration_param(600) == expected

    @pytest.mark.parametrize(
        ("seconds", "expected"),
        [
            (-1, "00000000"),
            (-86400, "00000000"),
            (0xFFFFFFFF, "FFFFFFFF"),
            (0x100000000, "FFFFFFFF"),
            (2**40, "FFFFFFFF"),
        ],
    )
    def test_out_of_range_clamps_rather_than_raising(self, seconds, expected):
        """Out-of-range durations clamp to the 4-byte range instead of raising.

        This is the boundary the encoder's own docstring promises, and the
        reason it promises it: ``int.to_bytes`` raises OverflowError outside
        four unsigned bytes, and an exception on this path would reach the user
        as a valve command that failed with no indication that the duration was
        the problem. Clamping keeps the command well formed.
        """
        assert _encode_dp_duration_param(seconds) == expected

    def test_every_output_is_eight_hex_characters(self):
        """The wire format is fixed width, so no input may shorten or lengthen it.

        A short string here would silently shift every field the server reads
        after it.
        """
        for seconds in (-1, 0, 1, 59, 60, 3600, 86400, 0xFFFFFFFF, 0x100000000):
            encoded = _encode_dp_duration_param(seconds)
            assert len(encoded) == 8
            assert encoded == encoded.upper()
            int(encoded, 16)

    def test_boolean_is_accepted_as_its_integer_value(self):
        """bool is an int subclass, so it encodes rather than raising.

        Pinned because the coercion is silent: this records what the encoder
        does today rather than endorsing a caller that passes one.
        """
        assert _encode_dp_duration_param(True) == _encode_dp_duration_param(1)


class TestDpClassDispatch:
    """build() discriminates by catalog identity rather than replacing the RF
    class for every VALVE_MODELS member."""

    @pytest.mark.asyncio
    async def test_non_bluetooth_model_still_builds_rf_entity(self):
        """A VALVE_MODELS member with no CTL_BT_WATER identity (MODEL_VALVE_245)
        still builds the plain RF entity, not the DP subclass."""
        _coordinator, _client, valve = await _build_valve_availability_timeline()

        assert isinstance(valve, RainPointValveEntity)
        assert not isinstance(valve, RainPointDpValveEntity)
