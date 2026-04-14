"""Tests for sensor entity platform (sensor.py)."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from custom_components.rainpoint.const import (
    DOMAIN,
    MODEL_DISPLAY_HUB,
    MODEL_MOISTURE_FULL,
    MODEL_MOISTURE_SIMPLE,
    MODEL_RAIN,
)
from custom_components.rainpoint.sensor import (
    DisplayHubReadingSensor,
    RainPointIlluminanceSensor,
    RainPointMoisturePercentSensor,
    RainPointRainSensor,
    RainPointTemperatureSensor,
    _slugify,
    async_setup_entry,
)
from tests.helpers import make_coordinator_data, make_hub_info, make_sensor_entry

# ---------------------------------------------------------------------------
# _slugify helper
# ---------------------------------------------------------------------------

class TestSlugify:
    """Tests for the _slugify helper."""

    def test_slugify_basic(self):
        """Slugify basic."""
        assert _slugify("Hello World") == "hello_world"

    def test_slugify_special_chars(self):
        """Slugify special chars."""
        assert _slugify("Sensor #1 (test)") == "sensor_1_test"

    def test_slugify_multiple_underscores(self):
        """Slugify multiple underscores."""
        assert _slugify("a---b___c") == "a_b_c"

    def test_slugify_leading_trailing(self):
        """Slugify leading trailing."""
        assert _slugify("__hello__") == "hello"

    def test_slugify_already_clean(self):
        """Slugify already clean."""
        assert _slugify("hello_world") == "hello_world"


# ---------------------------------------------------------------------------
# async_setup_entry dispatch tests
# ---------------------------------------------------------------------------

def _make_mock_coordinator(data):
    """Make mock coordinator helper."""
    mock = MagicMock()
    mock.data = data
    return mock


def _make_hass(coordinator):
    """Make hass helper."""
    hass = MagicMock()
    entry = MagicMock()
    entry.entry_id = "test_entry"
    hass.data = {DOMAIN: {"test_entry": {"coordinator": coordinator}}}
    return hass, entry


class TestAsyncSetupEntryDispatch:
    """Tests for async_setup_entry entity creation dispatch logic."""

    @pytest.mark.asyncio
    async def test_setup_entry_moisture_simple_creates_correct_entities(self):
        """MODEL_MOISTURE_SIMPLE -> 1 moisture + 4 diagnostic = 5 entities + raw payload = 6 total."""
        sensor_key = "100_200_1"
        sensor_info = make_sensor_entry(
            hid=100, mid=200, addr=1,
            model=MODEL_MOISTURE_SIMPLE,
            sub_name="Soil 1",
            data={"type": "moisture_simple", "moisture_percent": 50, "rssi_dbm": -80, "battery_percent": 75},
        )
        coordinator = _make_mock_coordinator(make_coordinator_data(
            sensors={sensor_key: sensor_info},
        ))
        hass, entry = _make_hass(coordinator)
        captured = []
        async_add_entities = MagicMock(side_effect=lambda ents, **kw: captured.extend(ents))

        await async_setup_entry(hass, entry, async_add_entities)

        # 1 moisture + 4 diagnostics (RSSI, battery, firmware, last_updated) + 1 raw payload = 6
        assert async_add_entities.called
        assert len(captured) == 6

    @pytest.mark.asyncio
    async def test_setup_entry_moisture_full_creates_correct_entities(self):
        """MODEL_MOISTURE_FULL -> 3 reading sensors + 4 diagnostic + 1 raw payload = 8."""
        sensor_key = "100_200_1"
        sensor_info = make_sensor_entry(
            hid=100, mid=200, addr=1,
            model=MODEL_MOISTURE_FULL,
            sub_name="Soil Full",
            data={
                "type": "moisture_full",
                "moisture_percent": 42,
                "temperature_c": 20.5,
                "illuminance_lux": 1000,
                "rssi_dbm": -75,
                "battery_percent": 80,
            },
        )
        coordinator = _make_mock_coordinator(make_coordinator_data(
            sensors={sensor_key: sensor_info},
        ))
        hass, entry = _make_hass(coordinator)
        captured = []
        async_add_entities = MagicMock(side_effect=lambda ents, **kw: captured.extend(ents))

        await async_setup_entry(hass, entry, async_add_entities)

        # 3 reading (moisture, temp, lux) + 4 diagnostics + 1 raw payload = 8
        assert len(captured) == 8

    @pytest.mark.asyncio
    async def test_setup_entry_rain_creates_4_rain_sensors(self):
        """MODEL_RAIN -> 4 rain sensors + 1 raw payload = 5."""
        sensor_key = "100_200_1"
        sensor_info = make_sensor_entry(
            hid=100, mid=200, addr=1,
            model=MODEL_RAIN,
            sub_name="Rain Gauge",
            data={
                "type": "rain",
                "rain_last_hour_mm": 0.5,
                "rain_last_24h_mm": 18.7,
                "rain_last_7d_mm": 42.0,
                "rain_total_mm": 100.0,
            },
        )
        coordinator = _make_mock_coordinator(make_coordinator_data(
            sensors={sensor_key: sensor_info},
        ))
        hass, entry = _make_hass(coordinator)
        captured = []
        async_add_entities = MagicMock(side_effect=lambda ents, **kw: captured.extend(ents))

        await async_setup_entry(hass, entry, async_add_entities)

        # 4 rain sensors + 1 raw payload = 5
        assert len(captured) == 5
        rain_sensors = [e for e in captured if isinstance(e, RainPointRainSensor)]
        assert len(rain_sensors) == 4

    @pytest.mark.asyncio
    async def test_setup_entry_display_hub_creates_reading_sensors(self):
        """MODEL_DISPLAY_HUB -> 3 reading sensors (from readings dict) + 1 raw payload = 4."""
        sensor_key = "100_200_1"
        sensor_info = make_sensor_entry(
            hid=100, mid=200, addr=1,
            model=MODEL_DISPLAY_HUB,
            sub_name="Display Hub",
            data={
                "type": "display_hub",
                "readings": {"temp": "707", "humidity": "42", "P": "9709"},
            },
        )
        coordinator = _make_mock_coordinator(make_coordinator_data(
            sensors={sensor_key: sensor_info},
        ))
        hass, entry = _make_hass(coordinator)
        captured = []
        async_add_entities = MagicMock(side_effect=lambda ents, **kw: captured.extend(ents))

        await async_setup_entry(hass, entry, async_add_entities)

        # 3 reading sensors + 1 raw payload = 4
        assert len(captured) == 4
        display_sensors = [e for e in captured if isinstance(e, DisplayHubReadingSensor)]
        assert len(display_sensors) == 3

    @pytest.mark.asyncio
    async def test_setup_entry_hub_sensors_created(self):
        """Hub list -> 3 hub sensors (DeviceID, Firmware, MAC) per hub."""
        from custom_components.rainpoint.hub_entities import (
            RainPointHubDeviceIDSensor,
            RainPointHubFirmwareSensor,
            RainPointHubMACSensor,
        )
        hub = make_hub_info(hid=100)
        coordinator = _make_mock_coordinator(make_coordinator_data(
            hubs=[hub],
            sensors={},
        ))
        hass, entry = _make_hass(coordinator)
        captured = []
        async_add_entities = MagicMock(side_effect=lambda ents, **kw: captured.extend(ents))

        await async_setup_entry(hass, entry, async_add_entities)

        assert len(captured) == 3
        types = {type(e) for e in captured}
        assert RainPointHubDeviceIDSensor in types
        assert RainPointHubFirmwareSensor in types
        assert RainPointHubMACSensor in types

    @pytest.mark.asyncio
    async def test_setup_entry_unknown_model_creates_no_reading_entities(self):
        """Unknown model does not create reading entities, only raw payload."""
        sensor_key = "100_200_1"
        sensor_info = make_sensor_entry(
            hid=100, mid=200, addr=1,
            model="UNKNOWN_XYZ",
            sub_name="Mystery Sensor",
            data={"type": "other"},
        )
        coordinator = _make_mock_coordinator(make_coordinator_data(
            sensors={sensor_key: sensor_info},
        ))
        hass, entry = _make_hass(coordinator)
        captured = []
        async_add_entities = MagicMock(side_effect=lambda ents, **kw: captured.extend(ents))

        await async_setup_entry(hass, entry, async_add_entities)

        # Only raw payload sensor created (unknown model, data type != "unknown")
        assert len(captured) == 1

    @pytest.mark.asyncio
    async def test_setup_entry_no_entities_skips_add_call(self):
        """Empty data -> no add_entities call."""
        coordinator = _make_mock_coordinator(make_coordinator_data(hubs=[], sensors={}))
        hass, entry = _make_hass(coordinator)
        async_add_entities = MagicMock()

        await async_setup_entry(hass, entry, async_add_entities)

        assert not async_add_entities.called

    @pytest.mark.asyncio
    async def test_setup_entry_multiple_sensors(self):
        """Multiple sensors each dispatch correctly."""
        sensors = {
            "100_200_1": make_sensor_entry(
                hid=100, mid=200, addr=1,
                model=MODEL_MOISTURE_SIMPLE,
                data={"type": "moisture_simple", "moisture_percent": 50, "rssi_dbm": -80, "battery_percent": 75},
            ),
            "100_200_2": make_sensor_entry(
                hid=100, mid=200, addr=2,
                model=MODEL_RAIN,
                data={"type": "rain", "rain_last_hour_mm": 0, "rain_last_24h_mm": 0, "rain_last_7d_mm": 0, "rain_total_mm": 0},
            ),
        }
        coordinator = _make_mock_coordinator(make_coordinator_data(sensors=sensors))
        hass, entry = _make_hass(coordinator)
        captured = []
        async_add_entities = MagicMock(side_effect=lambda ents, **kw: captured.extend(ents))

        await async_setup_entry(hass, entry, async_add_entities)

        # sensor 1: 6 entities; sensor 2: 5 entities = 11 total
        assert len(captured) == 11


# ---------------------------------------------------------------------------
# Representative sensor class unit tests
# ---------------------------------------------------------------------------

def _make_sensor_base(sensor_cls, sensor_key, data, sensor_info_overrides=None, extra_attrs=None):
    """Create a sensor instance via __new__ with mock coordinator."""
    info = {
        "hid": 100,
        "mid": 200,
        "addr": 1,
        "sub_name": "Test Sensor",
        "model": "HCS026FRF",
        "firmware_version": "1.0.0",
        "raw_status": {"value": "test", "time": 1700000000000},
    }
    if sensor_info_overrides:
        info.update(sensor_info_overrides)

    mock_coordinator = MagicMock()
    mock_coordinator.data = {
        "sensors": {
            sensor_key: {
                **info,
                "data": data,
            }
        }
    }

    sensor = sensor_cls.__new__(sensor_cls)
    sensor.coordinator = mock_coordinator
    sensor._sensor_key = sensor_key
    sensor._sensor_info = info
    sensor._base_slug = "100_200_1"
    if extra_attrs:
        for k, v in extra_attrs.items():
            setattr(sensor, k, v)
    return sensor


class TestMoisturePercentSensor:
    """Tests for RainPointMoisturePercentSensor."""

    def _make(self, moisture_percent=42, simple=True):
        """Make helper."""
        sensor = _make_sensor_base(
            RainPointMoisturePercentSensor,
            "100_200_1",
            {"type": "moisture_simple", "moisture_percent": moisture_percent, "rssi_dbm": -80, "battery_percent": 75},
        )
        sensor._simple = simple
        sensor._attr_unique_id = "rainpoint_100_200_1_moisture_percent"
        sensor._attr_name = "Test Sensor Moisture Percent"
        return sensor

    def test_moisture_sensor_native_value(self):
        """Moisture sensor native value."""
        sensor = self._make(moisture_percent=42)
        assert sensor.native_value == 42

    def test_moisture_sensor_unique_id(self):
        """Moisture sensor unique id."""
        sensor = self._make()
        assert "moisture" in sensor._attr_unique_id

    def test_moisture_sensor_native_value_none_when_no_data(self):
        """Moisture sensor native value none when no data."""
        sensor = _make_sensor_base(
            RainPointMoisturePercentSensor,
            "100_200_1",
            None,
        )
        sensor._simple = True
        sensor._attr_unique_id = "rainpoint_100_200_1_moisture_percent"
        sensor._attr_name = "Test Sensor Moisture Percent"
        assert sensor.native_value is None

    def test_moisture_sensor_available_with_data(self):
        """Moisture sensor available with data."""
        sensor = self._make()
        assert sensor.available is True

    def test_moisture_sensor_device_info_manufacturer(self):
        """Moisture sensor device info manufacturer."""
        sensor = self._make()
        assert sensor.device_info["manufacturer"] == "RainPoint"


class TestRainSensor:
    """Tests for RainPointRainSensor."""

    def _make(self, data_key="rain_last_24h_mm", rain_value=18.7):
        """Make helper."""
        sensor = _make_sensor_base(
            RainPointRainSensor,
            "100_200_1",
            {
                "type": "rain",
                "rain_last_hour_mm": 0.5,
                "rain_last_24h_mm": rain_value,
                "rain_last_7d_mm": 42.0,
                "rain_total_mm": 100.0,
            },
        )
        sensor._data_key = data_key
        sensor._attr_unique_id = f"rainpoint_100_200_1_{data_key}"
        sensor._attr_name = "Rain Sensor Rain (Last 24 Hours)"
        return sensor

    def test_rain_sensor_native_value(self):
        """Rain sensor native value."""
        sensor = self._make(data_key="rain_last_24h_mm", rain_value=18.7)
        assert sensor.native_value == 18.7

    def test_rain_sensor_native_value_rounded(self):
        """Rain sensor native value rounded."""
        sensor = self._make(data_key="rain_last_24h_mm", rain_value=18.723)
        assert sensor.native_value == 18.7

    def test_rain_sensor_device_info(self):
        """Rain sensor device info."""
        sensor = self._make()
        assert sensor.device_info["manufacturer"] == "RainPoint"

    def test_rain_sensor_returns_none_when_no_data(self):
        """Rain sensor returns none when no data."""
        sensor = _make_sensor_base(RainPointRainSensor, "100_200_1", None)
        sensor._data_key = "rain_last_24h_mm"
        sensor._attr_unique_id = "rainpoint_100_200_1_rain_last_24h_mm"
        sensor._attr_name = "Rain Sensor Rain"
        assert sensor.native_value is None

    def test_rain_sensor_last_hour(self):
        """Rain sensor last hour."""
        sensor = self._make(data_key="rain_last_hour_mm", rain_value=0.5)
        assert sensor.native_value == 0.5


class TestTemperatureSensor:
    """Tests for RainPointTemperatureSensor."""

    def _make(self, temperature_c=22.5):
        """Make helper."""
        sensor = _make_sensor_base(
            RainPointTemperatureSensor,
            "100_200_1",
            {"type": "moisture_full", "moisture_percent": 42, "temperature_c": temperature_c, "illuminance_lux": 1000},
        )
        sensor._attr_unique_id = "rainpoint_100_200_1_temperature"
        sensor._attr_name = "Test Sensor Temperature"
        return sensor

    def test_temperature_sensor_native_value(self):
        """Temperature sensor native value."""
        sensor = self._make(temperature_c=22.5)
        assert sensor.native_value == 22.5

    def test_temperature_sensor_native_value_rounded(self):
        """Temperature sensor native value rounded."""
        sensor = self._make(temperature_c=22.567)
        assert sensor.native_value == 22.6

    def test_temperature_sensor_none_when_missing(self):
        """Temperature sensor none when missing."""
        sensor = _make_sensor_base(
            RainPointTemperatureSensor,
            "100_200_1",
            {"type": "moisture_full", "moisture_percent": 42},
        )
        sensor._attr_unique_id = "rainpoint_100_200_1_temperature"
        sensor._attr_name = "Test Sensor Temperature"
        assert sensor.native_value is None

    def test_temperature_sensor_device_info(self):
        """Temperature sensor device info."""
        sensor = self._make()
        assert sensor.device_info["manufacturer"] == "RainPoint"


class TestDisplayHubReadingSensor:
    """Tests for DisplayHubReadingSensor."""

    def _make(self, reading_key="temp", readings=None):
        """Make helper."""
        if readings is None:
            readings = {"temp": "707", "humidity": "42", "P": "9709"}
        sensor = _make_sensor_base(
            DisplayHubReadingSensor,
            "100_200_1",
            {"type": "display_hub", "readings": readings},
        )
        sensor._reading_key = reading_key
        sensor._attr_unique_id = f"rainpoint_100_200_1_displayhub_{reading_key}"
        sensor._attr_name = f"Display Hub {reading_key}"
        return sensor

    def test_display_hub_reading_sensor_returns_float_for_numeric(self):
        """Display hub reading sensor returns float for numeric."""
        sensor = self._make(reading_key="temp", readings={"temp": "707"})
        assert sensor.native_value == 707.0

    def test_display_hub_reading_sensor_returns_string_for_non_numeric(self):
        """Display hub reading sensor returns string for non numeric."""
        sensor = self._make(reading_key="status", readings={"status": "ok"})
        assert sensor.native_value == "ok"

    def test_display_hub_reading_sensor_none_when_no_data(self):
        """Display hub reading sensor none when no data."""
        sensor = _make_sensor_base(DisplayHubReadingSensor, "100_200_1", None)
        sensor._reading_key = "temp"
        sensor._attr_unique_id = "rainpoint_100_200_1_displayhub_temp"
        sensor._attr_name = "Display Hub temp"
        assert sensor.native_value is None

    def test_display_hub_reading_sensor_unique_id(self):
        """Display hub reading sensor unique id."""
        sensor = self._make(reading_key="temp")
        assert "displayhub" in sensor._attr_unique_id
        assert "temp" in sensor._attr_unique_id


class TestIlluminanceSensor:
    """Tests for RainPointIlluminanceSensor."""

    def _make(self, illuminance_lux=1000):
        """Make helper."""
        sensor = _make_sensor_base(
            RainPointIlluminanceSensor,
            "100_200_1",
            {"type": "moisture_full", "moisture_percent": 42, "temperature_c": 20.0, "illuminance_lux": illuminance_lux},
        )
        sensor._attr_unique_id = "rainpoint_100_200_1_illuminance"
        sensor._attr_name = "Test Sensor Illuminance"
        return sensor

    def test_illuminance_sensor_native_value(self):
        """Illuminance sensor native value."""
        sensor = self._make(illuminance_lux=1500)
        assert sensor.native_value == 1500

    def test_illuminance_sensor_none_when_missing(self):
        """Illuminance sensor none when missing."""
        sensor = _make_sensor_base(
            RainPointIlluminanceSensor,
            "100_200_1",
            {"type": "moisture_full"},
        )
        sensor._attr_unique_id = "rainpoint_100_200_1_illuminance"
        sensor._attr_name = "Test Sensor Illuminance"
        assert sensor.native_value is None


_SENSOR_BASE_DATA = {"type": "moisture_simple", "moisture_percent": 50, "rssi_dbm": -80, "battery_percent": 75}
_SENSOR_BASE_SENTINEL = object()  # sentinel to distinguish "not passed" from None


class TestSensorBaseProperties:
    """Tests for RainPointSensorBase common properties."""

    def _make_base(self, data=_SENSOR_BASE_SENTINEL):
        """Make base helper."""
        if data is _SENSOR_BASE_SENTINEL:
            data = _SENSOR_BASE_DATA
        sensor = _make_sensor_base(
            RainPointMoisturePercentSensor,
            "100_200_1",
            data,
        )
        sensor._simple = True
        sensor._attr_unique_id = "rainpoint_100_200_1_moisture_percent"
        sensor._attr_name = "Test Sensor Moisture Percent"
        return sensor

    def test_available_true_with_data(self):
        """Available true with data."""
        sensor = self._make_base()
        assert sensor.available is True

    def test_available_false_with_none_data(self):
        """available returns False when sensor key is absent from coordinator sensors."""
        sensor = self._make_base()
        # Remove the sensor entry entirely so _sensor_data returns None
        sensor.coordinator.data["sensors"].clear()
        assert sensor.available is False

    def test_device_info_identifiers(self):
        """Device info identifiers."""
        sensor = self._make_base()
        identifiers = sensor.device_info["identifiers"]
        assert (DOMAIN, "100_200_1") in identifiers

    def test_device_info_via_device(self):
        """Device info via device."""
        sensor = self._make_base()
        via = sensor.device_info["via_device"]
        assert via == (DOMAIN, "hub_100")

    def test_extra_state_attributes_rssi(self):
        """Extra state attributes rssi."""
        sensor = self._make_base(data={"type": "moisture_simple", "moisture_percent": 50, "rssi_dbm": -80})
        attrs = sensor.extra_state_attributes
        assert attrs.get("rssi_dbm") == -80

    def test_extra_state_attributes_battery(self):
        """Extra state attributes battery."""
        sensor = self._make_base(data={"type": "moisture_simple", "moisture_percent": 50, "battery_percent": 75})
        attrs = sensor.extra_state_attributes
        assert attrs.get("battery_percent") == 75
