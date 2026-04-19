"""Tests for diagnostic sensor classes (diagnostic_sensors.py)."""

from __future__ import annotations

from datetime import datetime
from unittest.mock import MagicMock

from custom_components.rainpoint.const import DOMAIN
from custom_components.rainpoint.diagnostic_sensors import (
    RainPointBatterySensor,
    RainPointDeviceIDSensor,
    RainPointFirmwareVersionSensor,
    RainPointLastUpdatedSensor,
    RainPointRSSISensor,
)
from tests.helpers import make_sensor_coordinator

_SENTINEL = object()


def _make_coordinator(sensor_data=_SENTINEL, firmware_version="1.0"):
    """Return a mock coordinator with a single sensor entry.

    Pass sensor_data=None explicitly to test the "no data" (unavailable) path.
    Omit sensor_data to get a default dict with rssi and battery fields.
    """
    data_value = {"rssi_dbm": -84, "battery_percent": 75} if sensor_data is _SENTINEL else sensor_data
    return make_sensor_coordinator(
        data=data_value,
        firmware_version=firmware_version,
    )


def _make_sensor_info(hid=100, mid=200, addr=1, sub_name="Test Sensor", model="HCS026FRF"):
    """Make sensor info helper."""
    return {
        "hid": hid,
        "mid": mid,
        "addr": addr,
        "sub_name": sub_name,
        "model": model,
    }


class TestRainPointRSSISensor:
    """Tests for RainPointRSSISensor."""

    def _make(self, rssi=-84, data_is_none=False):
        """Make helper."""
        coord = _make_coordinator(sensor_data=None) if data_is_none else _make_coordinator(sensor_data={"rssi_dbm": rssi})
        sensor_info = _make_sensor_info()
        sensor = RainPointRSSISensor.__new__(RainPointRSSISensor)
        RainPointRSSISensor.__init__(sensor, coord, "100_200_1", sensor_info, "100_200_1")
        return sensor

    def test_native_value_returns_rssi(self):
        """native_value should return rssi_dbm from decoded data."""
        sensor = self._make(rssi=-84)
        assert sensor.native_value == -84

    def test_native_value_returns_none_when_no_data(self):
        """native_value should be None when sensor data is None."""
        sensor = self._make(data_is_none=True)
        assert sensor.native_value is None

    def test_available_when_data_present(self):
        """available should be True when data is not None."""
        sensor = self._make(rssi=-80)
        assert sensor.available is True

    def test_unavailable_when_no_data(self):
        """available should be False when sensor data is None."""
        sensor = self._make(data_is_none=True)
        assert sensor.available is False

    def test_unique_id_ends_with_rssi(self):
        """unique_id should end with '_rssi'."""
        sensor = self._make()
        assert sensor._attr_unique_id.endswith("_rssi")

    def test_device_info_identifiers(self):
        """device_info should contain the correct identifier."""
        sensor = self._make()
        assert (DOMAIN, "100_200_1") in sensor.device_info["identifiers"]

    def test_device_info_manufacturer(self):
        """device_info manufacturer should be 'RainPoint'."""
        sensor = self._make()
        assert sensor.device_info["manufacturer"] == "RainPoint"


class TestRainPointBatterySensor:
    """Tests for RainPointBatterySensor."""

    def _make(self, battery=75, data_is_none=False):
        """Make helper."""
        if data_is_none:
            coord = _make_coordinator(sensor_data=None)
        else:
            coord = _make_coordinator(sensor_data={"battery_percent": battery})
        sensor_info = _make_sensor_info()
        sensor = RainPointBatterySensor.__new__(RainPointBatterySensor)
        RainPointBatterySensor.__init__(sensor, coord, "100_200_1", sensor_info, "100_200_1")
        return sensor

    def test_native_value_returns_battery(self):
        """native_value should return battery_percent from decoded data."""
        sensor = self._make(battery=75)
        assert sensor.native_value == 75

    def test_native_value_returns_none_when_no_data(self):
        """native_value should be None when data is absent."""
        sensor = self._make(data_is_none=True)
        assert sensor.native_value is None

    def test_unique_id_ends_with_battery(self):
        """unique_id should end with '_battery'."""
        sensor = self._make()
        assert sensor._attr_unique_id.endswith("_battery")

    def test_available_when_data_present(self):
        """available should be True when data exists."""
        sensor = self._make(battery=50)
        assert sensor.available is True

    def test_unavailable_when_no_data(self):
        """available should be False when data is None."""
        sensor = self._make(data_is_none=True)
        assert sensor.available is False


class TestRainPointFirmwareVersionSensor:
    """Tests for RainPointFirmwareVersionSensor."""

    def _make(self, firmware="3.0"):
        """Make helper."""
        coord = _make_coordinator(firmware_version=firmware)
        sensor_info = _make_sensor_info()
        sensor = RainPointFirmwareVersionSensor.__new__(RainPointFirmwareVersionSensor)
        RainPointFirmwareVersionSensor.__init__(sensor, coord, "100_200_1", sensor_info, "100_200_1")
        return sensor

    def test_native_value_returns_firmware_version(self):
        """native_value should return firmware_version from sensor entry."""
        sensor = self._make(firmware="3.0")
        assert sensor.native_value == "3.0"

    def test_native_value_returns_none_when_missing(self):
        """native_value should be None when firmware_version is absent."""
        coord = MagicMock()
        coord.data = {"sensors": {"100_200_1": {}}}  # no firmware_version key
        sensor_info = _make_sensor_info()
        sensor = RainPointFirmwareVersionSensor.__new__(RainPointFirmwareVersionSensor)
        RainPointFirmwareVersionSensor.__init__(sensor, coord, "100_200_1", sensor_info, "100_200_1")
        assert sensor.native_value is None

    def test_unique_id_ends_with_firmware_version(self):
        """unique_id should end with '_firmware_version'."""
        sensor = self._make()
        assert sensor._attr_unique_id.endswith("_firmware_version")


class TestRainPointLastUpdatedSensor:
    """Tests for RainPointLastUpdatedSensor."""

    def _make(self, device_timestamp=None):
        """Make helper."""
        data = {}
        if device_timestamp is not None:
            data["device_timestamp"] = device_timestamp
        coord = _make_coordinator(sensor_data=data)
        sensor_info = _make_sensor_info()
        sensor = RainPointLastUpdatedSensor.__new__(RainPointLastUpdatedSensor)
        RainPointLastUpdatedSensor.__init__(sensor, coord, "100_200_1", sensor_info, "100_200_1")
        return sensor

    def test_native_value_returns_datetime(self):
        """native_value should return a datetime when device_timestamp is present."""
        sensor = self._make(device_timestamp="2024-01-01T00:00:00+00:00")
        result = sensor.native_value
        assert isinstance(result, datetime)

    def test_native_value_returns_none_when_no_timestamp(self):
        """native_value should be None when device_timestamp is absent."""
        sensor = self._make(device_timestamp=None)
        assert sensor.native_value is None

    def test_native_value_datetime_is_utc_aware(self):
        """Parsed datetime should be timezone-aware."""
        sensor = self._make(device_timestamp="2024-06-15T12:30:00+00:00")
        result = sensor.native_value
        assert result.tzinfo is not None

    def test_unique_id_ends_with_last_updated(self):
        """unique_id should end with '_last_updated'."""
        sensor = self._make()
        assert sensor._attr_unique_id.endswith("_last_updated")

    def test_native_value_handles_z_suffix(self):
        """native_value should handle 'Z' UTC suffix in timestamp."""
        sensor = self._make(device_timestamp="2024-01-15T08:00:00Z")
        result = sensor.native_value
        assert isinstance(result, datetime)


class TestRainPointDeviceIDSensor:
    """Tests for RainPointDeviceIDSensor."""

    def _make(self, sensor_entry=None):
        """Make helper."""
        coord = MagicMock()
        if sensor_entry is None:
            sensor_entry = {
                "firmware_version": "1.0",
                "data": {},
                "addr": 12345678901,  # 11-digit ID starting with 1
            }
        coord.data = {"sensors": {"100_200_1": sensor_entry}}
        sensor_info = _make_sensor_info()
        sensor = RainPointDeviceIDSensor.__new__(RainPointDeviceIDSensor)
        RainPointDeviceIDSensor.__init__(sensor, coord, "100_200_1", sensor_info, "100_200_1")
        return sensor

    def test_native_value_returns_addr_when_long_int(self):
        """native_value should return addr when it is a 9+ digit number."""
        sensor = self._make(
            sensor_entry={
                "firmware_version": "1.0",
                "data": {},
                "addr": 12345678901,
            }
        )
        assert sensor.native_value == 12345678901

    def test_native_value_returns_none_when_short_addr(self):
        """native_value should be None when addr is too short to be a device ID."""
        sensor = self._make(
            sensor_entry={
                "firmware_version": "1.0",
                "data": {},
                "addr": 1,  # short addr, not a device ID
            }
        )
        assert sensor.native_value is None

    def test_native_value_returns_none_when_no_sensor_entry(self):
        """native_value should return None when sensor_key not in coordinator data."""
        coord = MagicMock()
        coord.data = {"sensors": {}}  # no entry for sensor_key
        sensor_info = _make_sensor_info()
        sensor = RainPointDeviceIDSensor.__new__(RainPointDeviceIDSensor)
        RainPointDeviceIDSensor.__init__(sensor, coord, "100_200_1", sensor_info, "100_200_1")
        assert sensor.native_value is None

    def test_unique_id_ends_with_device_id(self):
        """unique_id should end with '_device_id'."""
        sensor = self._make()
        assert sensor._attr_unique_id.endswith("_device_id")

    def test_native_value_from_decoded_data_device_id(self):
        """Long device_id inside decoded `data` dict is returned (covers decoded-data loop)."""
        sensor = self._make(
            sensor_entry={
                "firmware_version": "1.0",
                "data": {"deviceId": "1234567890"},
                # No top-level addr / device_id fields - force fall-through to decoded data
            }
        )
        assert sensor.native_value == "1234567890"

    def test_native_value_from_raw_payload_regex(self):
        """Device ID is extracted from the raw_value string via the regex fallback."""
        sensor = self._make(
            sensor_entry={
                "firmware_version": "1.0",
                "data": {"raw_value": "prefix 1234567890 suffix"},
            }
        )
        # 10-digit match starting with 1 -> returned as int
        assert sensor.native_value == 1234567890

    def test_native_value_raw_payload_no_match_returns_none(self):
        """Raw payload without a 9+ digit match yields None."""
        sensor = self._make(
            sensor_entry={
                "firmware_version": "1.0",
                "data": {"raw_value": "no digits here"},
            }
        )
        assert sensor.native_value is None

    def test_native_value_raw_payload_match_not_starting_with_one(self):
        """Regex match exists but does not start with '1' - no device ID returned."""
        sensor = self._make(
            sensor_entry={
                "firmware_version": "1.0",
                "data": {"raw_value": "xx 2234567890 yy"},
            }
        )
        assert sensor.native_value is None

    def test_native_value_decoded_data_short_id_rejected(self):
        """device_id field in decoded data exists but is too short - loop continues."""
        sensor = self._make(
            sensor_entry={
                "firmware_version": "1.0",
                "data": {"deviceId": "123"},  # too short, fails len>=9 check
            }
        )
        assert sensor.native_value is None

    def test_native_value_decoded_data_no_raw_value(self):
        """Decoded data has no raw_value key - raw-payload regex branch skipped."""
        sensor = self._make(
            sensor_entry={
                "firmware_version": "1.0",
                "data": {"some_other_key": "whatever"},
            }
        )
        assert sensor.native_value is None


class TestDiagnosticBaseNoInfo:
    """Cover RainPointDiagnosticSensorBase._sensor_data when info is missing (line 42)."""

    def test_sensor_data_none_when_key_missing(self):
        """_sensor_data returns None when sensor_key is absent from sensors dict."""
        coord = MagicMock()
        coord.data = {"sensors": {}}
        sensor_info = _make_sensor_info()
        sensor = RainPointBatterySensor.__new__(RainPointBatterySensor)
        RainPointBatterySensor.__init__(sensor, coord, "missing_key", sensor_info, "100_200_1")
        assert sensor._sensor_data is None
        assert sensor.available is False


class TestLastUpdatedSensorBadTimestamp:
    """Cover lines 232-233 (ValueError/AttributeError swallowed)."""

    def test_invalid_timestamp_returns_none(self):
        """device_timestamp that isn't an ISO string raises and is swallowed."""
        coord = MagicMock()
        coord.data = {
            "sensors": {
                "100_200_1": {
                    "firmware_version": "1.0",
                    "data": {"device_timestamp": "not-a-timestamp"},
                }
            }
        }
        sensor_info = _make_sensor_info()
        sensor = RainPointLastUpdatedSensor.__new__(RainPointLastUpdatedSensor)
        RainPointLastUpdatedSensor.__init__(sensor, coord, "100_200_1", sensor_info, "100_200_1")
        assert sensor.native_value is None

    def test_non_string_timestamp_returns_none(self):
        """Non-string device_timestamp triggers AttributeError on .replace() and returns None."""
        coord = MagicMock()
        coord.data = {
            "sensors": {
                "100_200_1": {
                    "firmware_version": "1.0",
                    "data": {"device_timestamp": 12345},  # int, not str
                }
            }
        }
        sensor_info = _make_sensor_info()
        sensor = RainPointLastUpdatedSensor.__new__(RainPointLastUpdatedSensor)
        RainPointLastUpdatedSensor.__init__(sensor, coord, "100_200_1", sensor_info, "100_200_1")
        assert sensor.native_value is None
