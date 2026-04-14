"""Tests for RainPointCoordinator: data fetching, decoder dispatch, fallback, and error handling."""

import types
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Strategy: call _async_update_data as an unbound function.
#
# RainPointCoordinator inherits from DataUpdateCoordinator which is a MagicMock
# stub.  Instantiating RainPointCoordinator yields a MagicMock instance, not a
# real Python object; every attribute access returns a new MagicMock.
#
# Solution: extract the real coroutine function from the class dict and call it
# with a plain SimpleNamespace as `self`.  This is safe because
# _async_update_data only uses: self._client, self._hids,
# self._notified_unknown_models, self.hass, and self.logger — all attributes we
# can set on a SimpleNamespace.
# ---------------------------------------------------------------------------
import custom_components.rainpoint.coordinator as _coord_module

# Grab the raw function (bypasses MagicMock descriptor protocol)
_async_update_data_fn = _coord_module.RainPointCoordinator.__dict__["_async_update_data"]

DECODER_REGISTRY = _coord_module.DECODER_REGISTRY

from custom_components.rainpoint.api import RainPointApiError  # noqa: E402
from custom_components.rainpoint.const import (  # noqa: E402
    MODEL_CO2,
    MODEL_DISPLAY_HUB,
    MODEL_FLOWMETER,
    MODEL_MOISTURE_FULL,
    MODEL_MOISTURE_SIMPLE,
    MODEL_RAIN,
    MODEL_TEMPHUM,
    MODEL_VALVE_213,
    MODEL_VALVE_245,
    MODEL_VALVE_HUB,
)

# ---------------------------------------------------------------------------
# Sample raw payloads
# ---------------------------------------------------------------------------

_MOISTURE_SIMPLE_PAYLOAD = "10#E1C600DC01881AFF0F5E21F718"
_DISPLAY_HUB_PAYLOAD = "1,0,1;707(707/694/1),42(42/39/1),P=9709(9709/9701/1),"


# ---------------------------------------------------------------------------
# Helper: build a fake coordinator namespace and a mock client.
# ---------------------------------------------------------------------------

def _make_coord(hids=None):
    """Return (coord_ns, mock_client).

    coord_ns is a SimpleNamespace with the attributes that _async_update_data
    reads from self.
    """
    mock_client = AsyncMock()
    mock_hass = MagicMock()
    mock_hass.data = {}

    coord = types.SimpleNamespace(
        _client=mock_client,
        _hids=hids if hids is not None else [100],
        _notified_unknown_models=set(),
        hass=mock_hass,
        logger=MagicMock(),
    )
    return coord, mock_client


async def _run(coord):
    """Call _async_update_data on coord and return the result."""
    return await _async_update_data_fn(coord)


def _make_hub(hid=100, mid=200, model="HCS026FRF"):
    return {
        "mid": mid,
        "name": "Hub1",
        "deviceName": "dev1",
        "productKey": "pk1",
        "homeName": "Home",
        "subDevices": [
            {"addr": 1, "model": model, "name": "Sensor1", "softVer": "1.0"}
        ],
    }


def _make_status(mid=200, sid="D1", value=_MOISTURE_SIMPLE_PAYLOAD, time_ms=1700000000000):
    entry = {"id": sid, "value": value}
    if time_ms is not None:
        entry["time"] = time_ms
    return [{"mid": mid, "subDeviceStatus": [entry]}]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestCoordinatorUpdate:
    """Tests for RainPointCoordinator._async_update_data."""

    @pytest.mark.asyncio
    async def test_update_returns_correct_shape(self):
        """Result has 'hubs', 'status', 'sensors' keys."""
        coord, client = _make_coord()
        client.get_devices_by_hid.return_value = [_make_hub()]
        client.get_multiple_device_status.return_value = _make_status()

        result = await _run(coord)

        assert "hubs" in result
        assert "status" in result
        assert "sensors" in result

    @pytest.mark.asyncio
    async def test_update_sensor_key_format(self):
        """Sensor dict key is '{hid}_{mid}_{addr}'."""
        coord, client = _make_coord(hids=[100])
        client.get_devices_by_hid.return_value = [_make_hub(hid=100, mid=200)]
        client.get_multiple_device_status.return_value = _make_status(mid=200)

        result = await _run(coord)

        assert "100_200_1" in result["sensors"]

    @pytest.mark.asyncio
    async def test_update_decoder_dispatch_known_model(self):
        """Known model is dispatched to DECODER_REGISTRY and decoded correctly."""
        coord, client = _make_coord()
        client.get_devices_by_hid.return_value = [_make_hub(model=MODEL_MOISTURE_SIMPLE)]
        client.get_multiple_device_status.return_value = _make_status()

        result = await _run(coord)

        sensor = result["sensors"]["100_200_1"]
        assert sensor["data"] is not None
        assert sensor["data"]["type"] == "moisture_simple"

    @pytest.mark.asyncio
    async def test_update_unknown_model_returns_type_unknown(self):
        """Unknown model produces data dict with type='unknown'."""
        coord, client = _make_coord()
        client.get_devices_by_hid.return_value = [_make_hub(model="UNKNOWN_XYZ")]
        client.get_multiple_device_status.return_value = _make_status()

        result = await _run(coord)

        sensor = result["sensors"]["100_200_1"]
        assert sensor["data"]["type"] == "unknown"
        assert sensor["data"]["model"] == "UNKNOWN_XYZ"

    @pytest.mark.asyncio
    async def test_update_unknown_model_triggers_notification(self):
        """First unknown model encounter triggers async_create notification."""
        # The coordinator binds async_create by name at import time via
        # `from homeassistant.components.persistent_notification import async_create`.
        # We must patch that binding in the coordinator module's namespace.
        with patch.object(_coord_module, "async_create") as mock_notify:
            coord, client = _make_coord()
            client.get_devices_by_hid.return_value = [_make_hub(model="UNKNOWN_NOTIFY")]
            client.get_multiple_device_status.return_value = _make_status()

            await _run(coord)

        assert mock_notify.called

    @pytest.mark.asyncio
    async def test_update_unknown_model_notification_sent_once(self):
        """Notification for the same unknown model is sent only once."""
        with patch.object(_coord_module, "async_create") as mock_notify:
            coord, client = _make_coord()
            client.get_devices_by_hid.return_value = [_make_hub(model="UNKNOWN_ONCE")]
            client.get_multiple_device_status.return_value = _make_status()

            await _run(coord)
            await _run(coord)

        assert mock_notify.call_count == 1

    @pytest.mark.asyncio
    async def test_update_display_hub_model(self):
        """MODEL_DISPLAY_HUB routes to decode_hws019wrf_v2 (special-case path)."""
        coord, client = _make_coord()
        client.get_devices_by_hid.return_value = [_make_hub(model=MODEL_DISPLAY_HUB)]
        client.get_multiple_device_status.return_value = _make_status(value=_DISPLAY_HUB_PAYLOAD)

        result = await _run(coord)

        sensor = result["sensors"]["100_200_1"]
        assert sensor["data"] is not None
        assert sensor["data"]["type"] == "hws019wrf_v2"

    @pytest.mark.asyncio
    async def test_update_fallback_to_individual_calls(self):
        """When get_multiple_device_status raises, falls back to get_device_status."""
        coord, client = _make_coord()
        client.get_devices_by_hid.return_value = [_make_hub(model=MODEL_MOISTURE_SIMPLE)]
        client.get_multiple_device_status.side_effect = Exception("API error")
        client.get_device_status.return_value = {
            "subDeviceStatus": [{"id": "D1", "value": _MOISTURE_SIMPLE_PAYLOAD}]
        }

        result = await _run(coord)

        assert "100_200_1" in result["sensors"]
        assert result["sensors"]["100_200_1"]["data"] is not None

    @pytest.mark.asyncio
    async def test_update_fallback_individual_call_invoked_per_hub(self):
        """Fallback path calls get_device_status once per hub mid."""
        coord, client = _make_coord()
        hub1 = _make_hub(mid=201, model=MODEL_MOISTURE_SIMPLE)
        hub2 = _make_hub(mid=202, model=MODEL_MOISTURE_SIMPLE)
        client.get_devices_by_hid.return_value = [hub1, hub2]
        client.get_multiple_device_status.side_effect = Exception("fail")
        client.get_device_status.return_value = {"subDeviceStatus": []}

        await _run(coord)

        assert client.get_device_status.call_count == 2

    @pytest.mark.asyncio
    async def test_update_api_error_raises_exception(self):
        """RainPointApiError propagates as an exception (wrapped in UpdateFailed)."""
        coord, client = _make_coord()
        client.get_devices_by_hid.side_effect = RainPointApiError("fail")

        with pytest.raises(Exception):  # noqa: B017 - UpdateFailed is a MagicMock stub in tests
            await _run(coord)

    @pytest.mark.asyncio
    async def test_update_no_raw_value_skips_decoding(self):
        """Empty 'value' produces data=None for that sensor."""
        coord, client = _make_coord()
        client.get_devices_by_hid.return_value = [_make_hub(model=MODEL_MOISTURE_SIMPLE)]
        client.get_multiple_device_status.return_value = [
            {"mid": 200, "subDeviceStatus": [{"id": "D1", "value": ""}]}
        ]

        result = await _run(coord)

        assert result["sensors"]["100_200_1"]["data"] is None

    @pytest.mark.asyncio
    async def test_update_device_timestamp_extracted(self):
        """'time' field in status is decoded into device_timestamp on data dict."""
        coord, client = _make_coord()
        client.get_devices_by_hid.return_value = [_make_hub(model=MODEL_MOISTURE_SIMPLE)]
        client.get_multiple_device_status.return_value = _make_status(time_ms=1700000000000)

        result = await _run(coord)

        sensor = result["sensors"]["100_200_1"]
        assert sensor["data"] is not None
        assert "device_timestamp" in sensor["data"]
        assert sensor["data"]["timestamp_source"] == "device"

    @pytest.mark.asyncio
    async def test_update_sensor_entry_has_all_fields(self):
        """Each sensor entry contains all required metadata fields."""
        coord, client = _make_coord()
        client.get_devices_by_hid.return_value = [_make_hub()]
        client.get_multiple_device_status.return_value = _make_status()

        result = await _run(coord)

        sensor = result["sensors"]["100_200_1"]
        required = {
            "hid", "mid", "addr", "home_name", "hub_name", "sub_name",
            "model", "firmware_version", "device_name", "product_key",
            "raw_status", "data",
        }
        missing = required - sensor.keys()
        assert not missing, f"Sensor entry missing fields: {missing}"

    @pytest.mark.asyncio
    async def test_update_empty_hids(self):
        """No HIDs configured returns empty hubs and sensors."""
        coord, _client = _make_coord(hids=[])

        result = await _run(coord)

        assert result["hubs"] == []
        assert result["sensors"] == {}

    @pytest.mark.asyncio
    async def test_update_hubs_get_hid_and_brand_injected(self):
        """Coordinator injects 'hid' and 'brand' into each hub dict."""
        coord, client = _make_coord(hids=[100])
        client.get_devices_by_hid.return_value = [_make_hub()]
        client.get_multiple_device_status.return_value = _make_status()

        result = await _run(coord)

        hub = result["hubs"][0]
        assert hub["hid"] == 100
        assert hub["brand"] == "RainPoint"

    @pytest.mark.asyncio
    async def test_update_multiple_hids_each_call_get_devices(self):
        """Each HID triggers a separate get_devices_by_hid call."""
        coord, client = _make_coord(hids=[100, 101])
        client.get_devices_by_hid.return_value = []
        client.get_multiple_device_status.return_value = []

        await _run(coord)

        assert client.get_devices_by_hid.call_count == 2

    @pytest.mark.asyncio
    async def test_update_empty_multiple_status_triggers_fallback(self):
        """Empty list from get_multiple_device_status triggers fallback path."""
        coord, client = _make_coord()
        client.get_devices_by_hid.return_value = [_make_hub(model=MODEL_MOISTURE_SIMPLE)]
        client.get_multiple_device_status.return_value = []
        client.get_device_status.return_value = {
            "subDeviceStatus": [{"id": "D1", "value": _MOISTURE_SIMPLE_PAYLOAD}]
        }

        result = await _run(coord)

        assert "100_200_1" in result["sensors"]

    @pytest.mark.asyncio
    async def test_update_non_D_prefixed_sid_is_skipped(self):
        """Status entries with ID not starting with 'D' are ignored."""
        coord, client = _make_coord()
        client.get_devices_by_hid.return_value = [_make_hub()]
        client.get_multiple_device_status.return_value = [
            {"mid": 200, "subDeviceStatus": [{"id": "X1", "value": _MOISTURE_SIMPLE_PAYLOAD}]}
        ]

        result = await _run(coord)

        assert len(result["sensors"]) == 0

    @pytest.mark.asyncio
    async def test_update_unmatched_addr_skipped(self):
        """Status entry addr not in subDevices is skipped."""
        coord, client = _make_coord()
        hub = _make_hub()
        hub["subDevices"] = [{"addr": 99, "model": MODEL_MOISTURE_SIMPLE, "name": "X", "softVer": "1.0"}]
        client.get_devices_by_hid.return_value = [hub]
        client.get_multiple_device_status.return_value = [
            {"mid": 200, "subDeviceStatus": [{"id": "D1", "value": _MOISTURE_SIMPLE_PAYLOAD}]}
        ]

        result = await _run(coord)

        # D1 -> addr=1, but only addr=99 in subDevices
        assert len(result["sensors"]) == 0

    @pytest.mark.asyncio
    async def test_update_decode_exception_yields_none_data(self):
        """Decoder exceptions set data=None without propagating."""
        coord, client = _make_coord()
        client.get_devices_by_hid.return_value = [_make_hub(model=MODEL_MOISTURE_SIMPLE)]
        client.get_multiple_device_status.return_value = _make_status()

        with patch.dict(DECODER_REGISTRY, {MODEL_MOISTURE_SIMPLE: MagicMock(side_effect=ValueError("boom"))}):
            result = await _run(coord)

        assert result["sensors"]["100_200_1"]["data"] is None


class TestDecoderRegistry:
    """Tests for the DECODER_REGISTRY constant."""

    def test_registry_is_dict(self):
        assert isinstance(DECODER_REGISTRY, dict)

    def test_registry_has_minimum_entries(self):
        """Registry has at least 20 model entries."""
        assert len(DECODER_REGISTRY) >= 20

    def test_registry_contains_moisture_simple(self):
        assert MODEL_MOISTURE_SIMPLE in DECODER_REGISTRY

    def test_registry_contains_moisture_full(self):
        assert MODEL_MOISTURE_FULL in DECODER_REGISTRY

    def test_registry_contains_rain(self):
        assert MODEL_RAIN in DECODER_REGISTRY

    def test_registry_contains_temphum(self):
        assert MODEL_TEMPHUM in DECODER_REGISTRY

    def test_registry_contains_flowmeter(self):
        assert MODEL_FLOWMETER in DECODER_REGISTRY

    def test_registry_contains_co2(self):
        assert MODEL_CO2 in DECODER_REGISTRY

    def test_registry_contains_valve_245(self):
        assert MODEL_VALVE_245 in DECODER_REGISTRY

    def test_registry_contains_valve_213(self):
        assert MODEL_VALVE_213 in DECODER_REGISTRY

    def test_registry_contains_valve_hub(self):
        assert MODEL_VALVE_HUB in DECODER_REGISTRY

    def test_registry_display_hub_not_in_registry(self):
        """MODEL_DISPLAY_HUB uses a special-case code path, not DECODER_REGISTRY."""
        assert MODEL_DISPLAY_HUB not in DECODER_REGISTRY

    def test_registry_values_are_callable(self):
        """Every value is a callable decoder function."""
        for model, fn in DECODER_REGISTRY.items():
            assert callable(fn), f"Decoder for {model!r} is not callable"
