"""Tests for sensor entity platform (sensor.py)."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from custom_components.rainpoint import generic_control as generic_control_module
from custom_components.rainpoint import generic_entities as generic_entities_module
from custom_components.rainpoint.api import decode_htv213frf_valve
from custom_components.rainpoint.const import (
    DOMAIN,
    MODEL_DISPLAY_HUB,
    MODEL_HCS005FRF,
    MODEL_HCS015ARF,
    MODEL_HCS024FRF_V1,
    MODEL_HCS0528ARF,
    MODEL_MOISTURE_FULL,
    MODEL_MOISTURE_SIMPLE,
    MODEL_RAIN,
    MODEL_VALVE_213,
    MODEL_VALVE_245,
    MODEL_VALVE_345,
    MODEL_VALVE_405,
)
from custom_components.rainpoint.sensor import (
    DisplayHubReadingSensor,
    RainPointBatterySensor,
    RainPointCO2BatterySensor,
    RainPointCO2HighSensor,
    RainPointCO2HumiditySensor,
    RainPointCO2LowSensor,
    RainPointCO2Sensor,
    RainPointCO2TempSensor,
    RainPointFlowBatterySensor,
    RainPointFlowCurrentDurationSensor,
    RainPointFlowCurrentUsedSensor,
    RainPointFlowLastUsedDurationSensor,
    RainPointFlowLastUsedSensor,
    RainPointFlowTotalSensor,
    RainPointFlowTotalTodaySensor,
    RainPointIlluminanceSensor,
    RainPointMoisturePercentSensor,
    RainPointPoolBatterySensor,
    RainPointPoolCurrentTempSensor,
    RainPointPoolHighTempSensor,
    RainPointPoolLowTempSensor,
    RainPointPoolPlusAmbientCurrentTempSensor,
    RainPointPoolPlusAmbientHighTempSensor,
    RainPointPoolPlusAmbientLowTempSensor,
    RainPointPoolPlusHumidityCurrentSensor,
    RainPointPoolPlusHumidityHighSensor,
    RainPointPoolPlusHumidityLowSensor,
    RainPointPoolPlusPoolCurrentTempSensor,
    RainPointPoolPlusPoolHighTempSensor,
    RainPointPoolPlusPoolLowTempSensor,
    RainPointRainSensor,
    RainPointRawPayloadSensor,
    RainPointRSSISensor,
    RainPointTemperatureSensor,
    RainPointTempHumCurrentSensor,
    RainPointTempHumHighSensor,
    RainPointTempHumHumidityCurrentSensor,
    RainPointTempHumHumidityHighSensor,
    RainPointTempHumHumidityLowSensor,
    RainPointTempHumLowSensor,
    RainPointUnknownSensor,
    RainPointZoneWaterUsageSensor,
    _slugify,
    async_setup_entry,
)
from tests.helpers import make_coordinator_data, make_hub_info, make_sensor_entry
from tests.payload_samples import SAMPLE_HTV345_TLV_PAYLOAD, SAMPLE_HTV405_TLV_PAYLOAD

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
    # Without this, entry.options.get(...) on a bare MagicMock returns a
    # truthy mock, silently running every dispatch test with the generic
    # sensor path enabled.
    entry.options = {}
    hass.data = {DOMAIN: {"test_entry": {"coordinator": coordinator}}}
    return hass, entry


class TestAsyncSetupEntryDispatch:
    """Tests for async_setup_entry entity creation dispatch logic."""

    @pytest.mark.asyncio
    async def test_setup_entry_moisture_simple_creates_correct_entities(self):
        """MODEL_MOISTURE_SIMPLE -> 1 moisture + 4 diagnostic = 5 entities + raw payload = 6 total."""
        sensor_key = "100_200_1"
        sensor_info = make_sensor_entry(
            hid=100,
            mid=200,
            addr=1,
            model=MODEL_MOISTURE_SIMPLE,
            sub_name="Soil 1",
            data={"type": "moisture_simple", "moisture_percent": 50, "rssi_dbm": -80, "battery_percent": 75},
        )
        coordinator = _make_mock_coordinator(
            make_coordinator_data(
                sensors={sensor_key: sensor_info},
            )
        )
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
            hid=100,
            mid=200,
            addr=1,
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
        coordinator = _make_mock_coordinator(
            make_coordinator_data(
                sensors={sensor_key: sensor_info},
            )
        )
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
            hid=100,
            mid=200,
            addr=1,
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
        coordinator = _make_mock_coordinator(
            make_coordinator_data(
                sensors={sensor_key: sensor_info},
            )
        )
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
            hid=100,
            mid=200,
            addr=1,
            model=MODEL_DISPLAY_HUB,
            sub_name="Display Hub",
            data={
                "type": "display_hub",
                "readings": {"temp": "707", "humidity": "42", "P": "9709"},
            },
        )
        coordinator = _make_mock_coordinator(
            make_coordinator_data(
                sensors={sensor_key: sensor_info},
            )
        )
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
        """Hub list -> 4 hub sensors (DeviceID, Firmware, MAC, RSSI) per hub."""
        from custom_components.rainpoint.hub_entities import (
            RainPointHubDeviceIDSensor,
            RainPointHubFirmwareSensor,
            RainPointHubMACSensor,
            RainPointHubRSSISensor,
        )

        hub = make_hub_info(hid=100)
        coordinator = _make_mock_coordinator(
            make_coordinator_data(
                hubs=[hub],
                sensors={},
            )
        )
        hass, entry = _make_hass(coordinator)
        captured = []
        async_add_entities = MagicMock(side_effect=lambda ents, **kw: captured.extend(ents))

        await async_setup_entry(hass, entry, async_add_entities)

        assert len(captured) == 4
        types = {type(e) for e in captured}
        assert RainPointHubDeviceIDSensor in types
        assert RainPointHubFirmwareSensor in types
        assert RainPointHubMACSensor in types
        assert RainPointHubRSSISensor in types

    @pytest.mark.asyncio
    async def test_setup_entry_adds_push_last_message_sensor_when_push_enabled(self):
        """When push is enabled (mqtt_client present), one last-message entity is added per hub."""
        from custom_components.rainpoint.hub_entities import RainPointPushLastMessageSensor

        hub = make_hub_info(hid=100)
        coordinator = _make_mock_coordinator(make_coordinator_data(hubs=[hub], sensors={}))
        hass, entry = _make_hass(coordinator)
        hass.data[DOMAIN]["test_entry"]["mqtt_client"] = MagicMock()
        captured = []
        async_add_entities = MagicMock(side_effect=lambda ents, **kw: captured.extend(ents))

        await async_setup_entry(hass, entry, async_add_entities)

        last_message = [e for e in captured if isinstance(e, RainPointPushLastMessageSensor)]
        assert len(last_message) == 1

    @pytest.mark.asyncio
    async def test_setup_entry_unknown_model_creates_no_reading_entities(self):
        """Unknown model does not create reading entities, only raw payload."""
        sensor_key = "100_200_1"
        sensor_info = make_sensor_entry(
            hid=100,
            mid=200,
            addr=1,
            model="UNKNOWN_XYZ",
            sub_name="Mystery Sensor",
            data={"type": "other"},
        )
        coordinator = _make_mock_coordinator(
            make_coordinator_data(
                sensors={sensor_key: sensor_info},
            )
        )
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
                hid=100,
                mid=200,
                addr=1,
                model=MODEL_MOISTURE_SIMPLE,
                data={"type": "moisture_simple", "moisture_percent": 50, "rssi_dbm": -80, "battery_percent": 75},
            ),
            "100_200_2": make_sensor_entry(
                hid=100,
                mid=200,
                addr=2,
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

    def test_rain_sensor_returns_none_when_key_value_is_none(self):
        """Data dict present but the specific data_key maps to None -> native_value is None."""
        sensor = _make_sensor_base(
            RainPointRainSensor,
            "100_200_1",
            {"type": "rain", "rain_last_24h_mm": None},
        )
        sensor._data_key = "rain_last_24h_mm"
        sensor._attr_unique_id = "rainpoint_100_200_1_rain_last_24h_mm"
        sensor._attr_name = "Rain Sensor Rain (Last 24 Hours)"
        assert sensor.native_value is None


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

    def test_device_info_carries_firmware_and_serial(self):
        """Firmware and a stable device id reach the sensor's device page."""
        sensor = self._make_base()
        info = sensor.device_info
        assert info["sw_version"] == "1.0.0"
        assert info["serial_number"] == "200_1"

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

    def test_extra_state_attributes_battery_flag_without_percent(self):
        """An uncorroborated flag is still surfaced when no percentage is derived."""
        sensor = self._make_base(data={"type": "x", "battery_flag": 3, "battery_percent": None})
        attrs = sensor.extra_state_attributes
        assert attrs.get("battery_flag") == 3
        assert "battery_percent" not in attrs

    def test_extra_state_attributes_report_time(self):
        """The device's own wall clock is surfaced when the frame carries it."""
        sensor = self._make_base(data={"type": "x", "report_time": "2026-07-29T12:19:33"})
        attrs = sensor.extra_state_attributes
        assert attrs.get("report_time") == "2026-07-29T12:19:33"

    def test_extra_state_attributes_server_timestamp_fallback(self):
        """server_timestamp is reported as device_timestamp when device_timestamp missing."""
        sensor = self._make_base(
            data={"type": "x", "server_timestamp": "2024-01-01T00:00:00+00:00", "timestamp_source": "server"}
        )
        attrs = sensor.extra_state_attributes
        assert attrs["device_timestamp"] == "2024-01-01T00:00:00+00:00"
        assert attrs["timestamp_source"] == "server"

    def test_extra_state_attributes_device_timestamp_present(self):
        """device_timestamp field flows through with timestamp_method/source."""
        sensor = self._make_base(
            data={
                "type": "x",
                "device_timestamp": "2024-06-06T00:00:00+00:00",
                "timestamp_method": "rtc",
                "timestamp_source": "device",
            }
        )
        attrs = sensor.extra_state_attributes
        assert attrs["device_timestamp"] == "2024-06-06T00:00:00+00:00"
        assert attrs["timestamp_method"] == "rtc"
        assert attrs["timestamp_source"] == "device"

    def test_extra_state_attributes_legacy_last_updated_from_raw_status(self):
        """raw_status.time (ms since epoch) is exposed as last_updated ISO string."""
        sensor = self._make_base()
        attrs = sensor.extra_state_attributes
        assert attrs["last_updated"] == "2023-11-14T22:13:20+00:00"

    def test_extra_state_attributes_legacy_last_updated_bad_time_swallowed(self):
        """A non-numeric raw_status.time does not raise; last_updated is omitted."""
        sensor = self._make_base()
        # inject a bad time value via info dict
        key = sensor._sensor_key
        sensor.coordinator.data["sensors"][key]["raw_status"] = {"time": "notanumber"}
        attrs = sensor.extra_state_attributes
        # last_updated must not be present for bad time; no exception
        assert "last_updated" not in attrs

    def test_extra_state_attributes_no_firmware_or_raw_time(self):
        """Covers the 'skip firmware_version' and 'skip last_updated' no-op branches."""
        sensor = self._make_base()
        key = sensor._sensor_key
        # Drop firmware_version entirely + empty raw_status so no `ts` is set.
        sensor.coordinator.data["sensors"][key].pop("firmware_version", None)
        sensor.coordinator.data["sensors"][key]["raw_status"] = {}
        attrs = sensor.extra_state_attributes
        assert "firmware_version" not in attrs
        assert "last_updated" not in attrs


# ---------------------------------------------------------------------------
# Parametrized native_value coverage for simple "return data.get(KEY)" sensors.
# This collapses 32 near-identical one-liners into 32 table-driven assertions
# without re-implementing each sensor's init signature.
# ---------------------------------------------------------------------------


# (class, data_key, unique_id_suffix, sample_value)
_NATIVE_VALUE_CASES = [
    # TempHum
    (RainPointTempHumCurrentSensor, "tempcurrent", "temphum_current", 21.4),
    (RainPointTempHumHighSensor, "temphigh", "temphum_high", 29.1),
    (RainPointTempHumLowSensor, "templow", "temphum_low", 8.2),
    (RainPointTempHumHumidityCurrentSensor, "humiditycurrent", "temphum_humidity_current", 55),
    (RainPointTempHumHumidityHighSensor, "humidityhigh", "temphum_humidity_high", 80),
    (RainPointTempHumHumidityLowSensor, "humiditylow", "temphum_humidity_low", 30),
    # Flow
    (RainPointFlowCurrentUsedSensor, "flowcurrentused", "flow_current_used", 3.5),
    (RainPointFlowCurrentDurationSensor, "flowcurrenduration", "flow_current_duration", 60),
    (RainPointFlowLastUsedSensor, "flowlastused", "flow_last_used", 12.0),
    (RainPointFlowLastUsedDurationSensor, "flowlastusedduration", "flow_last_used_duration", 600),
    (RainPointFlowTotalTodaySensor, "flowtotaltoday", "flow_total_today", 42.0),
    (RainPointFlowTotalSensor, "flowtotal", "flow_total", 999.0),
    (RainPointFlowBatterySensor, "flowbatt", "flow_battery", 75),
    # CO2
    (RainPointCO2Sensor, "co2", "co2", 450),
    (RainPointCO2LowSensor, "co2low", "co2_low", 400),
    (RainPointCO2HighSensor, "co2high", "co2_high", 500),
    (RainPointCO2TempSensor, "co2temp", "co2_temp", 22.5),
    (RainPointCO2HumiditySensor, "co2humidity", "co2_humidity", 45),
    (RainPointCO2BatterySensor, "co2batt", "co2_battery", 90),
    # Pool
    (RainPointPoolCurrentTempSensor, "tempcurrent", "pool_current_temp", 24.0),
    (RainPointPoolHighTempSensor, "temphigh", "pool_high_temp", 28.5),
    (RainPointPoolLowTempSensor, "templow", "pool_low_temp", 20.0),
    (RainPointPoolBatterySensor, "tempbatt", "pool_battery", 70),
    # Pool Plus
    (RainPointPoolPlusPoolCurrentTempSensor, "pool_tempcurrent", "pool_plus_pool_current_temp", 25.5),
    (RainPointPoolPlusPoolHighTempSensor, "pool_temphigh", "pool_plus_pool_high_temp", 30.0),
    (RainPointPoolPlusPoolLowTempSensor, "pool_templow", "pool_plus_pool_low_temp", 15.0),
    (RainPointPoolPlusAmbientCurrentTempSensor, "ambient_tempcurrent", "pool_plus_ambient_current_temp", 22.0),
    (RainPointPoolPlusAmbientHighTempSensor, "ambient_temphigh", "pool_plus_ambient_high_temp", 35.0),
    (RainPointPoolPlusAmbientLowTempSensor, "ambient_templow", "pool_plus_ambient_low_temp", -5.0),
    (RainPointPoolPlusHumidityCurrentSensor, "humidity_current", "pool_plus_humidity_current", 65),
    (RainPointPoolPlusHumidityHighSensor, "humidity_high", "pool_plus_humidity_high", 95),
    (RainPointPoolPlusHumidityLowSensor, "humidity_low", "pool_plus_humidity_low", 10),
]


@pytest.mark.parametrize(("cls", "data_key", "uid_suffix", "value"), _NATIVE_VALUE_CASES)
def test_native_value_returns_data_key(cls, data_key, uid_suffix, value):
    """Each simple sensor reads its dedicated data key and returns that value."""
    sensor = _make_sensor_base(cls, "100_200_1", {"type": "x", data_key: value})
    sensor._attr_unique_id = f"rainpoint_100_200_1_{uid_suffix}"
    sensor._attr_name = f"Test Sensor {uid_suffix}"
    assert sensor.native_value == value


@pytest.mark.parametrize(("cls", "data_key", "uid_suffix", "_value"), _NATIVE_VALUE_CASES)
def test_native_value_none_when_data_missing(cls, data_key, uid_suffix, _value):
    """Each simple sensor returns None when _sensor_data is None."""
    sensor = _make_sensor_base(cls, "100_200_1", None)
    sensor._attr_unique_id = f"rainpoint_100_200_1_{uid_suffix}"
    sensor._attr_name = f"Test Sensor {uid_suffix}"
    assert sensor.native_value is None


# ---------------------------------------------------------------------------
# RainPointUnknownSensor + RainPointRawPayloadSensor
# ---------------------------------------------------------------------------


_UNK_SENTINEL = object()


def _make_unknown_sensor(data=_UNK_SENTINEL, model="MYSTERY"):
    """Build a RainPointUnknownSensor, shared by the classes that exercise it."""
    if data is _UNK_SENTINEL:
        data = {"type": "unknown", "model": model, "raw_value": "10#ABC"}
    sensor = _make_sensor_base(
        RainPointUnknownSensor,
        "100_200_1",
        data,
        sensor_info_overrides={"model": model, "sub_name": "Mystery"},
    )
    sensor._attr_unique_id = f"rainpoint_100_200_1_unknown_{model}"
    sensor._attr_name = f"Mystery Unsupported ({model})"
    return sensor


def _make_unknown_sensor_with_model_code(model, model_code, data=_UNK_SENTINEL):
    """Build a RainPointUnknownSensor whose sensor_info also carries a model_code.

    _make_unknown_sensor doesn't expose model_code, but describe_control_gate
    keys the catalog lookup on (model, model_code), so a real-catalog "admits"
    test needs it set.
    """
    if data is _UNK_SENTINEL:
        data = {"type": "unknown", "model": model, "raw_value": "10#ABC"}
    sensor = _make_sensor_base(
        RainPointUnknownSensor,
        "100_200_1",
        data,
        sensor_info_overrides={"model": model, "model_code": model_code, "sub_name": "Mystery"},
    )
    sensor._attr_unique_id = f"rainpoint_100_200_1_unknown_{model}"
    sensor._attr_name = f"Mystery Unsupported ({model})"
    return sensor


class TestUnknownSensor:
    """Tests for RainPointUnknownSensor."""

    _make = staticmethod(_make_unknown_sensor)

    def test_native_value_reports_model_when_data_present(self):
        # native_value reads data["model"], not sensor_info["model"], so pass
        # the model in the data dict explicitly.
        sensor = self._make(model="WIDGET", data={"type": "unknown", "model": "WIDGET", "raw_value": "10#"})
        assert sensor.native_value == "Unsupported: WIDGET"

    def test_native_value_reports_unknown_when_model_missing(self):
        sensor = self._make(data={"type": "unknown"})
        assert sensor.native_value == "Unsupported: unknown"

    def test_native_value_no_data(self):
        sensor = self._make(data=None)
        assert sensor.native_value == "No data"

    def test_extra_state_attributes_includes_model_and_raw_payload(self):
        sensor = self._make(model="MODELX", data={"type": "unknown", "model": "MODELX", "raw_value": "10#ZZ"})
        attrs = sensor.extra_state_attributes
        assert attrs["model"] == "MODELX"
        assert attrs["raw_payload"] == "10#ZZ"
        assert "report_url" in attrs
        assert "instructions" in attrs

    def test_report_url_is_the_prefilled_form_not_the_bare_issue_list(self):
        """The durable surface gets the good link.

        The unsupported-model notification fires once per variant and can be
        dismissed, so a user returning to the device later finds only this
        attribute. Pointing it at the bare issue list would leave the lasting
        path worse than the transient one.
        """
        sensor = self._make(model="MODELX", data={"type": "unknown", "model": "MODELX", "raw_value": "10#ZZ"})

        report_url = sensor.extra_state_attributes["report_url"]

        assert "/issues/new?" in report_url
        assert "template=new_device.yml" in report_url
        assert "model=MODELX" in report_url
        assert "primary_payload=10%23ZZ" in report_url

    def test_report_url_survives_a_missing_model(self):
        """A payload with no model name still yields a usable link rather than raising."""
        sensor = self._make(model=None, data={"type": "unknown", "raw_value": "10#ZZ"})

        assert "/issues/new?" in sensor.extra_state_attributes["report_url"]

    def test_extra_state_attributes_surfaces_generic_decode(self):
        """A generic structural decode is exposed as decoded_fields/_values."""
        generic = {
            "decoder": "generic-tlv",
            "field_names": ["STA_BAT", "STA_WKSTATE"],
            "fields": [
                {"name": "STA_BAT", "index": 31, "dp_id": 24, "raw": "01", "value": 1},
                {"name": "STA_WKSTATE", "index": 30, "dp_id": 25, "raw": "00", "value": 0},
            ],
        }
        sensor = self._make(data={"type": "unknown", "model": "MODELX", "raw_value": "11#x", "generic": generic})
        attrs = sensor.extra_state_attributes
        assert attrs["decoded_fields"] == ["STA_BAT", "STA_WKSTATE"]
        assert attrs["decoded_values"] == generic["fields"]

    def test_extra_state_attributes_omits_generic_when_no_fields(self):
        """An empty/failed generic decode adds no decoded_* attributes."""
        sensor = self._make(
            data={
                "type": "unknown",
                "model": "MODELX",
                "raw_value": "10#ZZ",
                "generic": {"decoder": "generic-tlv", "error": "bad"},
            }
        )
        attrs = sensor.extra_state_attributes
        assert "decoded_fields" not in attrs
        assert "decoded_values" not in attrs

    def test_unmapped_identity_attributes_present_regardless_of_toggle_state(self, monkeypatch):
        """The two new attributes never depend on the generic-entities options toggle."""
        dp_entries = [{"identity": "STA_TEM", "dpPort": 0}]
        monkeypatch.setattr(generic_entities_module, "get_catalog_entry", lambda model, model_code=None: dp_entries)
        sensor = self._make(model="MYSTERY")

        attrs = sensor.extra_state_attributes

        assert "unmapped_generic_identities" in attrs
        assert "generic_gate_blocked_by" in attrs
        # No toggle is ever read here - the value is identical whether or not
        # an options entry even exists for this sensor.
        assert attrs["unmapped_generic_identities"] == []
        assert attrs["generic_gate_blocked_by"] == []

    def test_unmapped_list_contains_exactly_the_uncurated_identity(self, monkeypatch):
        dp_entries = [{"identity": "STA_TEM", "dpPort": 0}, {"identity": "STA_BAT", "dpPort": 0, "dpCode": 2}]
        monkeypatch.setattr(generic_entities_module, "get_catalog_entry", lambda model, model_code=None: dp_entries)
        sensor = self._make(model="MYSTERY")

        attrs = sensor.extra_state_attributes

        assert attrs["unmapped_generic_identities"] == ["STA_BAT"]
        assert len(attrs["generic_gate_blocked_by"]) == 1
        assert "1 of this device's 2 status readings" in attrs["generic_gate_blocked_by"][0]

    def test_fully_curated_variant_reports_no_unmapped_identities_and_no_reason(self, monkeypatch):
        dp_entries = [{"identity": "STA_TEM", "dpPort": 0}]
        monkeypatch.setattr(generic_entities_module, "get_catalog_entry", lambda model, model_code=None: dp_entries)
        monkeypatch.setattr(generic_entities_module, "get_catalog_port_number", lambda model, model_code=None: 1)
        sensor = self._make(model="MYSTERY")

        attrs = sensor.extra_state_attributes

        assert attrs["unmapped_generic_identities"] == []
        assert attrs["generic_gate_blocked_by"] == []

    def test_model_absent_from_catalog_reports_a_blocked_reason(self, monkeypatch):
        monkeypatch.setattr(generic_entities_module, "get_catalog_entry", lambda model, model_code=None: None)
        sensor = self._make(model="MYSTERY")

        attrs = sensor.extra_state_attributes

        assert attrs["unmapped_generic_identities"] == []
        assert attrs["generic_gate_blocked_by"]

    def test_duplicate_identity_and_port_names_the_identity_in_the_reason(self, monkeypatch):
        dp_entries = [
            {"identity": "STA_RH", "dpPort": 0, "dpCode": 1},
            {"identity": "STA_RH", "dpPort": 0, "dpCode": 2},
        ]
        monkeypatch.setattr(generic_entities_module, "get_catalog_entry", lambda model, model_code=None: dp_entries)
        sensor = self._make(model="MYSTERY")

        attrs = sensor.extra_state_attributes

        assert attrs["generic_gate_blocked_by"]
        assert any("STA_RH" in reason for reason in attrs["generic_gate_blocked_by"])

    def test_preexisting_attributes_are_unchanged_alongside_the_new_keys(self, monkeypatch):
        dp_entries = [{"identity": "STA_RH", "dpPort": 0}]
        monkeypatch.setattr(generic_entities_module, "get_catalog_entry", lambda model, model_code=None: dp_entries)
        sensor = self._make(model="MODELX", data={"type": "unknown", "model": "MODELX", "raw_value": "10#ZZ"})

        attrs = sensor.extra_state_attributes

        assert attrs["model"] == "MODELX"
        assert attrs["raw_payload"] == "10#ZZ"
        assert "report_url" in attrs
        assert "instructions" in attrs
        assert "unmapped_generic_identities" in attrs
        assert "generic_gate_blocked_by" in attrs

    def test_no_generic_sub_dict_does_not_raise(self):
        """No decoded payload at all still yields the two always-on keys, never an exception."""
        sensor = self._make(data=None)

        attrs = sensor.extra_state_attributes

        assert "unmapped_generic_identities" in attrs
        assert "generic_gate_blocked_by" in attrs


class TestUnknownSensorAttributeCost:
    """extra_state_attributes is read on every state write and every template render.

    Two of the values it returns are expensive: a full catalog gate
    evaluation, and a report link that restructures the payload. Neither
    input changes between most reads, so both are memoised. These cases fix
    what the memo is keyed on, so a later change cannot make it stale.
    """

    def test_re_reading_attributes_evaluates_the_catalog_gate_no_further_times(self, monkeypatch):
        """Both memoised values are gate-backed, so a repeat read must cost nothing.

        The report link builds its own catalog summary, so the first read
        evaluates the gate more than once. What matters is that the second
        read adds none.
        """
        calls = []

        def _counting(model, model_code=None):
            calls.append((model, model_code))
            return {"unmapped_generic_identities": ["STA_X"], "generic_gate_blocked_by": ["nope"]}

        monkeypatch.setattr(generic_entities_module, "describe_generic_gate", _counting)
        sensor = _make_unknown_sensor(model="MODELX")

        first = sensor.extra_state_attributes
        after_first_read = len(calls)
        second = sensor.extra_state_attributes

        assert after_first_read >= 1
        assert len(calls) == after_first_read
        assert first["unmapped_generic_identities"] == ["STA_X"]
        assert second["generic_gate_blocked_by"] == ["nope"]

    def test_editing_the_returned_lists_cannot_reach_the_memo(self, monkeypatch):
        """The attributes dict is handed out; the cached lists behind it stay intact."""
        monkeypatch.setattr(
            generic_entities_module,
            "describe_generic_gate",
            lambda model, model_code=None: {"unmapped_generic_identities": ["STA_X"], "generic_gate_blocked_by": []},
        )
        sensor = _make_unknown_sensor(model="MODELX")

        sensor.extra_state_attributes["unmapped_generic_identities"].append("STA_INJECTED")

        assert sensor.extra_state_attributes["unmapped_generic_identities"] == ["STA_X"]

    def test_the_report_link_is_rebuilt_only_when_the_payload_changes(self, monkeypatch):
        import custom_components.rainpoint.sensor as sensor_module

        calls = []

        def _counting(model, raw_value, model_code=None):
            calls.append(raw_value)
            return f"https://example.invalid/{raw_value}"

        monkeypatch.setattr(sensor_module, "_build_new_device_issue_url", _counting)
        sensor = _make_unknown_sensor(model="MODELX", data={"type": "unknown", "model": "MODELX", "raw_value": "10#AA"})

        assert sensor.extra_state_attributes["report_url"].endswith("10#AA")
        assert sensor.extra_state_attributes["report_url"].endswith("10#AA")
        assert calls == ["10#AA"]

        sensor.coordinator.data["sensors"]["100_200_1"]["data"]["raw_value"] = "10#BB"

        assert sensor.extra_state_attributes["report_url"].endswith("10#BB")
        assert calls == ["10#AA", "10#BB"]


class TestUnknownSensorControlGateAttribute:
    """The control-gate reasons attribute (generic_control_blocked_by) is
    toggle-independent, exactly like the sensor-gate attributes it merges
    alongside -- it is computed from the catalog and the in-source allowlist
    alone, so a user who never enabled generic control still sees it.
    """

    def test_key_present_with_no_options_entry_at_all(self):
        """No config-entry option is ever read here -- the key's presence and
        value are identical whether or not an options entry even exists for
        this sensor, matching the two sensor-gate attributes it sits beside.
        """
        sensor = _make_unknown_sensor(model="MYSTERY")

        attrs = sensor.extra_state_attributes

        assert "generic_control_blocked_by" in attrs
        assert isinstance(attrs["generic_control_blocked_by"], list)

    def test_value_is_empty_list_for_a_model_the_control_gate_admits(self):
        """HTV103FRF/31 is the anchor variant the control-gate test suite
        proves admits exactly one CTL_WATER datapoint.
        """
        sensor = _make_unknown_sensor_with_model_code("HTV103FRF", 31)

        attrs = sensor.extra_state_attributes

        assert attrs["generic_control_blocked_by"] == []

    def test_value_is_non_empty_list_for_a_model_the_control_gate_refuses(self):
        sensor = _make_unknown_sensor(model="MYSTERY")

        attrs = sensor.extra_state_attributes

        assert attrs["generic_control_blocked_by"]

    def test_reasons_match_the_order_describe_control_gate_produces(self, monkeypatch):
        monkeypatch.setattr(
            generic_control_module,
            "describe_control_gate",
            lambda model, model_code=None: {"generic_control_blocked_by": ["first reason", "second reason"]},
        )
        sensor = _make_unknown_sensor(model="MODELX")

        attrs = sensor.extra_state_attributes

        assert attrs["generic_control_blocked_by"] == ["first reason", "second reason"]

    def test_computed_once_and_cached_alongside_the_sensor_gate_description(self, monkeypatch):
        calls = []

        def _counting(model, model_code=None):
            calls.append((model, model_code))
            return {"generic_control_blocked_by": ["nope"]}

        monkeypatch.setattr(generic_control_module, "describe_control_gate", _counting)
        sensor = _make_unknown_sensor(model="MODELX")

        first = sensor.extra_state_attributes
        after_first_read = len(calls)
        second = sensor.extra_state_attributes

        assert after_first_read >= 1
        assert len(calls) == after_first_read
        assert first["generic_control_blocked_by"] == ["nope"]
        assert second["generic_control_blocked_by"] == ["nope"]

    def test_mutating_the_returned_list_does_not_reach_the_cached_value(self):
        sensor = _make_unknown_sensor(model="MYSTERY")

        sensor.extra_state_attributes["generic_control_blocked_by"].append("INJECTED")

        assert "INJECTED" not in sensor.extra_state_attributes["generic_control_blocked_by"]

    def test_a_raising_projection_does_not_break_the_rest_of_the_attributes(self, monkeypatch):
        monkeypatch.setattr(
            generic_control_module,
            "get_catalog_entry",
            lambda model, model_code=None: (_ for _ in ()).throw(RuntimeError("boom")),
        )
        sensor = _make_unknown_sensor(model="MYSTERY")

        attrs = sensor.extra_state_attributes

        # evaluate_control_gate never raises (it degrades to a fail-closed
        # result internally), so the sensor's other attributes are still
        # present and the gate reason list is simply non-empty.
        assert attrs["model"] == "MYSTERY"
        assert "generic_control_blocked_by" in attrs
        assert attrs["generic_control_blocked_by"]


class TestRawPayloadSensor:
    """Tests for RainPointRawPayloadSensor."""

    def _make(self, raw_value="10#AABBCC"):
        sensor = _make_sensor_base(
            RainPointRawPayloadSensor,
            "100_200_1",
            {"type": "x"},
        )
        # Inject raw_status.value for this test
        key = sensor._sensor_key
        sensor.coordinator.data["sensors"][key]["raw_status"] = {"value": raw_value}
        sensor._attr_unique_id = "rainpoint_100_200_1_raw_payload"
        sensor._attr_name = "Sensor Raw Payload"
        return sensor

    def test_native_value_returns_raw_payload(self):
        sensor = self._make(raw_value="10#CAFE")
        assert sensor.native_value == "10#CAFE"

    def test_native_value_none_when_missing(self):
        sensor = self._make(raw_value=None)
        # raw_status.value is None -> returns None
        assert sensor.native_value is None

    def test_native_value_none_when_sensor_key_absent(self):
        sensor = self._make()
        sensor.coordinator.data["sensors"].clear()
        assert sensor.native_value is None


# ---------------------------------------------------------------------------
# HCS sensor model dispatch in async_setup_entry (covers elif branches 176-286)
# ---------------------------------------------------------------------------


class TestHCSSensorDispatch:
    """Verify async_setup_entry creates the right entities for each HCS model."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("model", "data", "expected_moisture_like"),
        [
            (MODEL_HCS005FRF, {"moisture_percent": 50}, 1),
            (MODEL_HCS024FRF_V1, {"moisture_percent": 50, "temperature_c": 20, "illuminance_lux": 1000}, 3),
        ],
    )
    async def test_hcs_moisture_like_models(self, model, data, expected_moisture_like):
        sensor_key = "100_200_1"
        sensor_info = make_sensor_entry(hid=100, mid=200, addr=1, model=model, sub_name="Sensor", data=data)
        coordinator = _make_mock_coordinator(make_coordinator_data(sensors={sensor_key: sensor_info}))
        hass, entry = _make_hass(coordinator)
        captured = []
        async_add_entities = MagicMock(side_effect=lambda ents, **kw: captured.extend(ents))
        await async_setup_entry(hass, entry, async_add_entities)
        # expected_moisture_like reading entities + 1 raw payload sensor
        assert len(captured) == expected_moisture_like + 1

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "model",
        [
            MODEL_HCS015ARF,
            MODEL_HCS0528ARF,
        ],
    )
    async def test_hcs_pool_like_models(self, model):
        sensor_key = "100_200_1"
        sensor_info = make_sensor_entry(
            hid=100,
            mid=200,
            addr=1,
            model=model,
            sub_name="Pool",
            data={"tempcurrent": 24, "temphigh": 28, "templow": 20, "tempbatt": 80},
        )
        coordinator = _make_mock_coordinator(make_coordinator_data(sensors={sensor_key: sensor_info}))
        hass, entry = _make_hass(coordinator)
        captured = []
        async_add_entities = MagicMock(side_effect=lambda ents, **kw: captured.extend(ents))
        await async_setup_entry(hass, entry, async_add_entities)
        # 4 pool entities + 1 raw payload sensor
        assert len(captured) == 5

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("model", "count"),
        [
            ("HCS010WRF", 7),  # temphum + flowmeter? Actually it's MODEL_FLOWMETER -> 7 entities + 1 raw
            ("HCS0530THO", 6),  # MODEL_CO2 -> 6 + 1 raw
            ("HCS014ARF", 6),  # MODEL_TEMPHUM -> 6 + 1 raw
        ],
    )
    async def test_core_models_dispatch(self, model, count):
        """Core non-HCS-variant models also dispatch to their entity classes."""
        from custom_components.rainpoint.const import MODEL_CO2, MODEL_FLOWMETER, MODEL_TEMPHUM

        MODEL_TO_CONST = {
            "HCS010WRF": MODEL_FLOWMETER,
            "HCS0530THO": MODEL_CO2,
            "HCS014ARF": MODEL_TEMPHUM,
        }
        real_model = MODEL_TO_CONST[model]
        sensor_key = "100_200_1"
        sensor_info = make_sensor_entry(
            hid=100,
            mid=200,
            addr=1,
            model=real_model,
            sub_name="Device",
            data={"foo": 1},
        )
        coordinator = _make_mock_coordinator(make_coordinator_data(sensors={sensor_key: sensor_info}))
        hass, entry = _make_hass(coordinator)
        captured = []
        async_add_entities = MagicMock(side_effect=lambda ents, **kw: captured.extend(ents))
        await async_setup_entry(hass, entry, async_add_entities)
        assert len(captured) == count + 1

    @pytest.mark.asyncio
    async def test_unknown_model_with_unknown_type_creates_unknown_sensor(self):
        """An unrecognized model + data.type=='unknown' spawns RainPointUnknownSensor."""
        sensor_key = "100_200_1"
        sensor_info = make_sensor_entry(
            hid=100,
            mid=200,
            addr=1,
            model="ZZZ_NO_SUCH_MODEL",
            sub_name="Mystery",
            data={"type": "unknown", "model": "ZZZ_NO_SUCH_MODEL", "raw_value": "10#"},
        )
        coordinator = _make_mock_coordinator(make_coordinator_data(sensors={sensor_key: sensor_info}))
        hass, entry = _make_hass(coordinator)
        captured = []
        async_add_entities = MagicMock(side_effect=lambda ents, **kw: captured.extend(ents))
        await async_setup_entry(hass, entry, async_add_entities)
        # 1 unknown diagnostic + 1 raw payload
        assert len(captured) == 2
        assert any(isinstance(e, RainPointUnknownSensor) for e in captured)

    @pytest.mark.asyncio
    async def test_pool_plus_creates_9_entities(self):
        """MODEL_POOL_PLUS creates 9 reading sensors + 1 raw payload."""
        from custom_components.rainpoint.const import MODEL_POOL_PLUS

        sensor_key = "100_200_1"
        sensor_info = make_sensor_entry(
            hid=100,
            mid=200,
            addr=1,
            model=MODEL_POOL_PLUS,
            sub_name="Pool+",
            data={
                "pool_tempcurrent": 25,
                "pool_temphigh": 30,
                "pool_templow": 20,
                "ambient_tempcurrent": 22,
                "ambient_temphigh": 30,
                "ambient_templow": 15,
                "humidity_current": 55,
                "humidity_high": 70,
                "humidity_low": 30,
            },
        )
        coordinator = _make_mock_coordinator(make_coordinator_data(sensors={sensor_key: sensor_info}))
        hass, entry = _make_hass(coordinator)
        captured = []
        async_add_entities = MagicMock(side_effect=lambda ents, **kw: captured.extend(ents))
        await async_setup_entry(hass, entry, async_add_entities)
        assert len(captured) == 10

    @pytest.mark.asyncio
    async def test_pool_creates_4_entities(self):
        """MODEL_POOL creates 4 reading sensors + 1 raw payload."""
        from custom_components.rainpoint.const import MODEL_POOL

        sensor_key = "100_200_1"
        sensor_info = make_sensor_entry(
            hid=100,
            mid=200,
            addr=1,
            model=MODEL_POOL,
            sub_name="Pool",
            data={"tempcurrent": 24, "temphigh": 28, "templow": 20, "tempbatt": 85},
        )
        coordinator = _make_mock_coordinator(make_coordinator_data(sensors={sensor_key: sensor_info}))
        hass, entry = _make_hass(coordinator)
        captured = []
        async_add_entities = MagicMock(side_effect=lambda ents, **kw: captured.extend(ents))
        await async_setup_entry(hass, entry, async_add_entities)
        assert len(captured) == 5


class TestHtvValveDiagnosticDispatch:
    """The HTV213/245 valve family gets battery + signal sensors from the sensor platform."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("model", [MODEL_VALVE_213, MODEL_VALVE_245, MODEL_VALVE_345, MODEL_VALVE_405])
    async def test_valve_family_creates_battery_and_rssi_sensors(self, model):
        """A valve entry yields a battery + RSSI sensor plus the raw payload sensor."""
        sensor_key = "100_200_1"
        sensor_info = make_sensor_entry(
            hid=100,
            mid=200,
            addr=1,
            model=model,
            sub_name="Valve",
            data={"type": "valve_hub", "zones": {}, "rssi_dbm": -37, "battery_percent": 100},
        )
        coordinator = _make_mock_coordinator(make_coordinator_data(sensors={sensor_key: sensor_info}))
        hass, entry = _make_hass(coordinator)
        captured = []
        async_add_entities = MagicMock(side_effect=lambda ents, **kw: captured.extend(ents))
        await async_setup_entry(hass, entry, async_add_entities)

        battery = [e for e in captured if isinstance(e, RainPointBatterySensor)]
        rssi = [e for e in captured if isinstance(e, RainPointRSSISensor)]
        assert len(battery) == 1
        assert battery[0].native_value == 100
        assert len(rssi) == 1
        assert rssi[0].native_value == -37
        # 1 battery + 1 RSSI + 1 raw payload sensor, nothing else from this platform.
        assert len(captured) == 3


class TestZoneWaterUsageSensor:
    """One water-usage entity per reported zone on the HTV213/245 valve family."""

    @staticmethod
    def _valve_entry(zones):
        return make_sensor_entry(
            hid=100,
            mid=200,
            addr=1,
            model=MODEL_VALVE_245,
            sub_name="Valve",
            data={"type": "valve_hub", "zones": zones, "rssi_dbm": -37, "battery_percent": 100},
        )

    @staticmethod
    def _first_usage(entities):
        return next(e for e in entities if isinstance(e, RainPointZoneWaterUsageSensor))

    async def _setup(self, zones):
        sensor_key = "100_200_1"
        coordinator = _make_mock_coordinator(make_coordinator_data(sensors={sensor_key: self._valve_entry(zones)}))
        hass, entry = _make_hass(coordinator)
        captured = []
        async_add_entities = MagicMock(side_effect=lambda ents, **kw: captured.extend(ents))
        await async_setup_entry(hass, entry, async_add_entities)
        return captured

    @pytest.mark.asyncio
    async def test_one_usage_entity_per_reported_zone(self):
        """Two reported zones yield two usage entities, named and keyed per zone."""
        zones = {
            1: {"open": False, "last_usage_counts": 421, "last_usage_gallons": 0.842},
            2: {"open": False, "last_usage_counts": 48, "last_usage_gallons": 0.096},
        }
        usage = [e for e in await self._setup(zones) if isinstance(e, RainPointZoneWaterUsageSensor)]
        assert len(usage) == 2
        assert [e.native_value for e in usage] == [0.842, 0.096]
        assert usage[0]._attr_unique_id == "rainpoint_100_200_1_zone1_water_used"
        assert usage[0]._attr_name == "Valve Zone 1 Water Used"

    @pytest.mark.asyncio
    async def test_no_usage_entities_when_no_zones_reported(self):
        """A frame reporting no zones grows no phantom usage entities."""
        usage = [e for e in await self._setup({}) if isinstance(e, RainPointZoneWaterUsageSensor)]
        assert usage == []

    @pytest.mark.asyncio
    async def test_no_usage_entities_when_zones_are_malformed(self):
        """A decode that yields no usable zones dict still produces the battery and signal pair."""
        sensor_key = "100_200_1"
        sensor_info = make_sensor_entry(
            hid=100,
            mid=200,
            addr=1,
            model=MODEL_VALVE_245,
            sub_name="Valve",
            data={"type": "valve_hub", "rssi_dbm": -37, "battery_percent": 100},
        )
        coordinator = _make_mock_coordinator(make_coordinator_data(sensors={sensor_key: sensor_info}))
        hass, entry = _make_hass(coordinator)
        captured = []
        async_add_entities = MagicMock(side_effect=lambda ents, **kw: captured.extend(ents))
        await async_setup_entry(hass, entry, async_add_entities)

        assert [e for e in captured if isinstance(e, RainPointZoneWaterUsageSensor)] == []
        assert len(captured) == 3

    @pytest.mark.asyncio
    async def test_stays_out_of_long_term_statistics(self):
        """No state class and no device class: the conversion factor is not settled enough for either."""
        zones = {1: {"open": False, "last_usage_counts": 421, "last_usage_gallons": 0.842}}
        usage = self._first_usage(await self._setup(zones))
        # Asserted through the _attr_ declarations rather than the public
        # properties: the entity is never added to hass here, and the test
        # harness leaves the cached-property side of that pair unresolvable.
        assert usage._attr_state_class is None
        assert getattr(usage, "_attr_device_class", None) is None
        assert usage._attr_native_unit_of_measurement == "gal"

    @pytest.mark.asyncio
    async def test_raw_count_is_exposed_for_auditing(self):
        """The count and the factor ride along so the conversion can be checked against the app."""
        zones = {1: {"open": False, "last_usage_counts": 421, "last_usage_gallons": 0.842}}
        usage = self._first_usage(await self._setup(zones))
        attrs = usage.extra_state_attributes
        assert attrs["zone"] == 1
        assert attrs["last_usage_counts"] == 421
        assert attrs["gallons_per_count"] == 1 / 500

    @pytest.mark.asyncio
    async def test_missing_zone_record_reads_unknown(self):
        """A zone that drops out of a later frame reads unknown rather than stale or zero."""
        zones = {1: {"open": False, "last_usage_counts": 421, "last_usage_gallons": 0.842}}
        usage = self._first_usage(await self._setup(zones))
        usage.coordinator.data["sensors"]["100_200_1"]["data"]["zones"] = {}
        assert usage.native_value is None
        assert usage.extra_state_attributes["last_usage_counts"] is None

    @pytest.mark.asyncio
    async def test_malformed_zones_payload_reads_unknown(self):
        """A non-dict zones value degrades to unknown instead of raising into the state machine."""
        zones = {1: {"open": False, "last_usage_counts": 421, "last_usage_gallons": 0.842}}
        usage = self._first_usage(await self._setup(zones))
        usage.coordinator.data["sensors"]["100_200_1"]["data"]["zones"] = ["not", "a", "dict"]
        assert usage.native_value is None


class TestWiderValveFamilyUsageEntities:
    """The 3- and 4-zone family members get the same per-zone usage entities."""

    @staticmethod
    async def _setup(model, payload):
        sensor_key = "100_200_1"
        sensor_info = make_sensor_entry(
            hid=100,
            mid=200,
            addr=1,
            model=model,
            sub_name="Valve",
            data=decode_htv213frf_valve(payload),
        )
        coordinator = _make_mock_coordinator(make_coordinator_data(sensors={sensor_key: sensor_info}))
        hass, entry = _make_hass(coordinator)
        captured = []
        async_add_entities = MagicMock(side_effect=lambda ents, **kw: captured.extend(ents))
        await async_setup_entry(hass, entry, async_add_entities)
        return [e for e in captured if isinstance(e, RainPointZoneWaterUsageSensor)]

    @pytest.mark.asyncio
    async def test_htv345_gets_one_usage_entity_per_zone(self):
        """The 3-zone capture carries usage records, so all three read a value."""
        usage = await self._setup(MODEL_VALVE_345, SAMPLE_HTV345_TLV_PAYLOAD)
        assert len(usage) == 3
        assert [e.native_value for e in usage] == [0.0, 0.0, 0.0]

    @pytest.mark.asyncio
    async def test_htv405_zones_read_unknown_when_the_frame_omits_usage(self):
        """The 4-zone capture carries no usage records: four entities, all unknown.

        Creating them anyway is the deliberate choice - an entity reading
        unknown says "this zone reports no usage", where a missing entity
        would read as "this zone was not reported at all".
        """
        usage = await self._setup(MODEL_VALVE_405, SAMPLE_HTV405_TLV_PAYLOAD)
        assert len(usage) == 4
        assert all(e.native_value is None for e in usage)
        assert all(e.extra_state_attributes["last_usage_counts"] is None for e in usage)
