"""Tests for valve entity platform (valve.py)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.rainpoint.const import DOMAIN, MODEL_VALVE_245
from custom_components.rainpoint.valve import (
    DEFAULT_DURATION_SECONDS,
    RainPointValveEntity,
)


def _make_valve(zone_data=None, hub_online=True, model="HTV245FRF"):
    """Create a RainPointValveEntity with mock coordinator, bypassing __init__."""
    mock_coordinator = MagicMock()
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
    mock_coordinator.data = {
        "sensors": {
            sensor_key: {
                "hid": 100,
                "mid": 200,
                "addr": 1,
                "data": {
                    "hub_online": hub_online,
                    "zones": {1: zone_data if zone_data is not None else {"open": True, "duration_seconds": 300, "state_raw": 1}},
                },
                "firmware_version": "1.0",
            }
        }
    }

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

    def test_unavailable_when_no_data(self):
        """No data in sensors should give available == False."""
        valve = _make_valve()
        valve.coordinator.data["sensors"]["100_200_1"]["data"] = None
        assert valve.available is False

    def test_extra_state_attributes_includes_duration(self):
        """Zone with duration_seconds should appear in extra_state_attributes."""
        valve = _make_valve(zone_data={"open": True, "duration_seconds": 300, "state_raw": 1})
        attrs = valve.extra_state_attributes
        assert attrs["duration_seconds"] == 300

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

    def test_apply_response_state_updates_coordinator(self):
        """_apply_response_state should call async_set_updated_data when raw_state given."""
        valve = _make_valve(model=MODEL_VALVE_245)
        valve.coordinator.async_set_updated_data = MagicMock()

        # Use a realistic ASCII-format payload for HTV245FRF
        valve._apply_response_state("1,-84,1;1,0,0,300;0,0,0,0")

        valve.coordinator.async_set_updated_data.assert_called_once()

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

    def test_get_configured_duration_falls_back_to_default(self, monkeypatch):
        """If entity registry lookup finds entity_id but state is None, fall back to default."""
        import sys

        valve = _make_valve()

        # Mock the entity registry import chain: entity_id found but state is None
        mock_registry = MagicMock()
        mock_registry.async_get_entity_id.return_value = "number.rainpoint_valve_zone1_duration"
        mock_er_module = MagicMock()
        mock_er_module.async_get.return_value = mock_registry

        # hass.states.get returns None — state not available yet
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
