"""Tests for number entity platform (number.py)."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from custom_components.rainpoint.const import DOMAIN
from custom_components.rainpoint.number import (
    DURATION_DEFAULT_MINUTES,
    DURATION_MAX_MINUTES,
    DURATION_MIN_MINUTES,
    DURATION_STEP_MINUTES,
    RainPointZoneDurationNumber,
)


def _make_number(current_value=10.0, firmware_version="1.0"):
    """Create a RainPointZoneDurationNumber with mock coordinator, bypassing __init__."""
    mock_coordinator = MagicMock()
    sensor_key = "100_200_1"
    sensor_info = {
        "hid": 100,
        "mid": 200,
        "addr": 1,
        "sub_name": "Valve Hub 1",
        "model": "HTV245FRF",
    }
    mock_coordinator.data = {
        "sensors": {
            sensor_key: {
                "firmware_version": firmware_version,
                "data": {},
            }
        }
    }

    num = RainPointZoneDurationNumber.__new__(RainPointZoneDurationNumber)
    num.coordinator = mock_coordinator
    num._sensor_key = sensor_key
    num._sensor_info = sensor_info
    num._zone_num = 1
    num._current_value = current_value
    num._attr_unique_id = "rainpoint_100_200_1_zone1_duration"
    num._attr_name = "Valve Hub 1 Zone 1 Duration"
    num.hass = MagicMock()
    num.async_write_ha_state = MagicMock()
    return num


class TestNumberEntity:
    """Tests for RainPointZoneDurationNumber."""

    def test_native_value_returns_current(self):
        """native_value should return _current_value."""
        num = _make_number(current_value=10.0)
        assert num.native_value == 10.0

    @pytest.mark.asyncio
    async def test_set_native_value_updates(self):
        """async_set_native_value should update _current_value and write state."""
        num = _make_number(current_value=10.0)
        await num.async_set_native_value(30.0)
        assert num._current_value == 30.0
        num.async_write_ha_state.assert_called_once()

    def test_unique_id_format(self):
        """unique_id should end with '_duration'."""
        num = _make_number()
        assert num._attr_unique_id.endswith("_duration")

    def test_device_info_manufacturer(self):
        """device_info should have manufacturer == 'RainPoint'."""
        num = _make_number()
        assert num.device_info["manufacturer"] == "RainPoint"

    def test_device_info_identifiers(self):
        """device_info should contain the correct identifier tuple."""
        num = _make_number()
        identifiers = num.device_info["identifiers"]
        assert (DOMAIN, "100_200_1") in identifiers

    def test_extra_state_attributes_firmware(self):
        """extra_state_attributes should contain firmware_version when set."""
        num = _make_number(firmware_version="2.0")
        attrs = num.extra_state_attributes
        assert attrs["firmware_version"] == "2.0"

    def test_extra_state_attributes_no_firmware_when_missing(self):
        """extra_state_attributes should not contain firmware_version when not set."""
        num = _make_number(firmware_version=None)
        # Firmware version is None, so it should not appear
        # (the code checks `if firmware_version:`)
        attrs = num.extra_state_attributes
        assert "firmware_version" not in attrs

    def test_min_max_step(self):
        """Number entity class attributes should have correct min/max/step."""
        assert RainPointZoneDurationNumber._attr_native_min_value == 1
        assert RainPointZoneDurationNumber._attr_native_max_value == 60
        assert RainPointZoneDurationNumber._attr_native_step == 1

    def test_duration_constants(self):
        """Module-level constants should have expected values."""
        assert DURATION_MIN_MINUTES == 1
        assert DURATION_MAX_MINUTES == 60
        assert DURATION_STEP_MINUTES == 1
        assert DURATION_DEFAULT_MINUTES == 10

    @pytest.mark.asyncio
    async def test_set_native_value_stores_float(self):
        """async_set_native_value should store value as float."""
        num = _make_number(current_value=10.0)
        await num.async_set_native_value(15.0)
        assert num._current_value == 15.0
        assert isinstance(num._current_value, float)
