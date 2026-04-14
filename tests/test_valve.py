"""Tests for valve entity platform (valve.py)."""

from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from custom_components.rainpoint.valve import (
    RainPointValveEntity,
    DEFAULT_DURATION_SECONDS,
)
from custom_components.rainpoint.const import DOMAIN, MODEL_VALVE_245


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
                    "zones": {
                        1: zone_data if zone_data is not None else {"open": True, "duration_seconds": 300, "state_raw": 1}
                    },
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

    def test_get_configured_duration_falls_back_to_default(self):
        """If entity registry lookup finds entity_id but state is None, fall back to default."""
        valve = _make_valve()

        # Mock the entity registry import chain: entity_id found but state is None
        mock_registry = MagicMock()
        mock_registry.async_get_entity_id.return_value = "number.rainpoint_valve_zone1_duration"
        mock_er_module = MagicMock()
        mock_er_module.async_get.return_value = mock_registry

        # hass.states.get returns None — state not available yet
        valve.hass.states.get.return_value = None

        import sys
        original = sys.modules.get("homeassistant.helpers.entity_registry")
        sys.modules["homeassistant.helpers.entity_registry"] = mock_er_module
        try:
            result = valve._get_configured_duration_seconds()
        finally:
            if original is not None:
                sys.modules["homeassistant.helpers.entity_registry"] = original
            else:
                del sys.modules["homeassistant.helpers.entity_registry"]

        assert result == DEFAULT_DURATION_SECONDS
