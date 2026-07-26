"""Tests for RainPointCoordinator: data fetching, decoder dispatch, fallback, and error handling."""

import logging
import types
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
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

assert "_async_update_data" in _coord_module.RainPointCoordinator.__dict__, (
    "RainPointCoordinator._async_update_data missing or renamed; update tests accordingly"
)
# Grab the raw function (bypasses MagicMock descriptor protocol)
_async_update_data_fn = _coord_module.RainPointCoordinator.__dict__["_async_update_data"]

DECODER_REGISTRY = _coord_module.DECODER_REGISTRY

from custom_components.rainpoint.api import RainPointApiError, decode_htv145frf  # noqa: E402
from custom_components.rainpoint.const import (  # noqa: E402
    MODEL_CO2,
    MODEL_DISPLAY_HUB,
    MODEL_FLOWMETER,
    MODEL_MOISTURE_FULL,
    MODEL_MOISTURE_SIMPLE,
    MODEL_RAIN,
    MODEL_TEMPHUM,
    MODEL_VALVE_113,
    MODEL_VALVE_213,
    MODEL_VALVE_245,
    MODEL_VALVE_345,
    MODEL_VALVE_405,
    MODEL_VALVE_HUB,
)
from tests.payload_samples import (  # noqa: E402
    CATALOG_ANCHOR_MODEL,
    SAMPLE_HTV113_IDLE_PAYLOAD,
    SAMPLE_HTV245_TLV_PAYLOAD,
    SAMPLE_HTV405_TLV_PAYLOAD,
    SAMPLE_UNSUPPORTED_MULTI_SENSOR_PAYLOAD,
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
        _last_valve_command_at={},
        data={},
        hass=mock_hass,
        logger=MagicMock(),
    )
    coord._preserve_recent_valve_command_state = types.MethodType(
        _coord_module.RainPointCoordinator.__dict__["_preserve_recent_valve_command_state"],
        coord,
    )
    return coord, mock_client


async def _run(coord):
    """Call _async_update_data on coord and return the result."""
    return await _async_update_data_fn(coord)


def _make_hub(hid=100, mid=200, model=MODEL_MOISTURE_SIMPLE):
    """Make hub helper."""
    return {
        "mid": mid,
        "name": "Hub1",
        "deviceName": "dev1",
        "productKey": "pk1",
        "homeName": "Home",
        "subDevices": [{"addr": 1, "model": model, "name": "Sensor1", "softVer": "1.0"}],
    }


def _make_status(mid=200, sid="D1", value=_MOISTURE_SIMPLE_PAYLOAD, time_ms=1700000000000):
    """Make status helper."""
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
    async def test_raw_hub_record_logged_at_debug(self, caplog):
        """At DEBUG level, the full raw hub record is dumped for field discovery."""
        coord, client = _make_coord()
        client.get_devices_by_hid.return_value = [_make_hub(model="HWG023WBRF-V2")]
        client.get_multiple_device_status.return_value = _make_status()

        with caplog.at_level(logging.DEBUG, logger="custom_components.rainpoint.coordinator"):
            await _run(coord)

        assert any("Raw hub record" in r.message and "HWG023WBRF-V2" in r.message for r in caplog.records)

    @pytest.mark.asyncio
    async def test_raw_hub_record_not_logged_above_debug(self, caplog):
        """Above DEBUG, the raw hub record dump is skipped (guarded json.dumps)."""
        coord, client = _make_coord()
        client.get_devices_by_hid.return_value = [_make_hub(model="HWG023WBRF-V2")]
        client.get_multiple_device_status.return_value = _make_status()

        with caplog.at_level(logging.INFO, logger="custom_components.rainpoint.coordinator"):
            await _run(coord)

        assert not any("Raw hub record" in r.message for r in caplog.records)

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
    async def test_model_code_is_exposed_on_sensor_entry(self):
        """modelCode from the device list is carried through to the sensor entry."""
        coord, client = _make_coord()
        hub = _make_hub(model=MODEL_MOISTURE_SIMPLE)
        hub["subDevices"][0]["modelCode"] = 303
        client.get_devices_by_hid.return_value = [hub]
        client.get_multiple_device_status.return_value = _make_status()

        result = await _run(coord)

        sensor = next(iter(result["sensors"].values()))
        assert sensor["model_code"] == 303

    @pytest.mark.asyncio
    async def test_model_code_absent_is_none_not_an_error(self):
        """A device list without modelCode still decodes, leaving model_code None."""
        coord, client = _make_coord()
        client.get_devices_by_hid.return_value = [_make_hub(model=MODEL_MOISTURE_SIMPLE)]
        client.get_multiple_device_status.return_value = _make_status()

        result = await _run(coord)

        sensor = next(iter(result["sensors"].values()))
        assert sensor["model_code"] is None

    @pytest.mark.asyncio
    async def test_unknown_model_notification_includes_model_code(self):
        """The unsupported-model notification carries modelCode so reports are unambiguous."""
        with patch.object(_coord_module, "async_create") as mock_notify:
            coord, client = _make_coord()
            hub = _make_hub(model="UNKNOWN_CODED")
            hub["subDevices"][0]["modelCode"] = 279
            client.get_devices_by_hid.return_value = [hub]
            client.get_multiple_device_status.return_value = _make_status()

            await _run(coord)

        body = mock_notify.call_args.args[1]
        assert "279" in body
        assert mock_notify.call_args.kwargs["notification_id"] == "rainpoint_unsupported_UNKNOWN_CODED_279"

    @pytest.mark.asyncio
    async def test_notification_includes_prefilled_report_link(self):
        """The unsupported-model notification embeds a GitHub issue-form link with the
        model and raw payload pre-filled, so the reporter does not hand-copy them."""
        with patch.object(_coord_module, "async_create") as mock_notify:
            coord, client = _make_coord()
            client.get_devices_by_hid.return_value = [_make_hub(model="UNKNOWN_LINK")]
            client.get_multiple_device_status.return_value = _make_status()

            await _run(coord)

        body = mock_notify.call_args.args[1]
        assert f"{_coord_module.ISSUE_URL}/new?" in body
        assert "template=new_device.yml" in body
        assert "model=UNKNOWN_LINK" in body
        # The payload's '#' must be percent-encoded so the URL is not truncated.
        assert "primary_payload=10%23E1C600" in body

    def test_build_new_device_issue_url_encodes_fields(self):
        """The builder targets the New device form and URL-encodes model/payload."""
        url = _coord_module._build_new_device_issue_url("HTV999XYZ", "10#ABCD")
        assert url.startswith(f"{_coord_module.ISSUE_URL}/new?")
        assert "template=new_device.yml" in url
        assert "title=Add+support+for+HTV999XYZ" in url
        assert "model=HTV999XYZ" in url
        assert "primary_payload=10%23ABCD" in url

    def test_build_new_device_issue_url_handles_missing_payload(self):
        """A None payload yields an empty primary_payload rather than crashing."""
        url = _coord_module._build_new_device_issue_url("HTV999XYZ", None)
        assert "primary_payload=" in url
        assert "model=HTV999XYZ" in url
        # No payload -> no auto-decode section to pre-fill.
        assert "auto_decoded=" not in url

    def test_build_new_device_issue_url_prefills_auto_decode(self):
        """A decodable payload pre-fills the auto_decoded field with named values."""
        url = _coord_module._build_new_device_issue_url("HTV999XYZ", SAMPLE_HTV245_TLV_PAYLOAD)
        assert "auto_decoded=" in url
        # Vendor field names from the generic decode land in the pre-fill.
        assert "STA_DURATION" in url
        assert "STA_WKSTATE" in url

    def test_build_new_device_issue_url_omits_auto_decode_when_undecodable(self):
        """A payload the generic decoder cannot parse adds no auto_decoded param."""
        url = _coord_module._build_new_device_issue_url("HTV999XYZ", "garbage-no-hash")
        assert "auto_decoded=" not in url

    def test_build_new_device_issue_url_carries_model_code_when_known(self):
        """modelCode reaches the form: one model string can cover variants differing in port count."""
        url = _coord_module._build_new_device_issue_url("HTV999XYZ", "10#ABCD", 303)

        assert "model_code=303" in url

    def test_build_new_device_issue_url_omits_model_code_when_unknown(self):
        """No reported modelCode leaves the field empty rather than seeding a guess."""
        url = _coord_module._build_new_device_issue_url("HTV999XYZ", "10#ABCD")

        assert "model_code=" not in url

    def test_build_new_device_issue_url_prefills_gate_diagnostics(self):
        """What the catalog already explains reaches the form, so triage does not rederive it."""
        url = _coord_module._build_new_device_issue_url("HTV999XYZ", "10#ABCD")

        assert "gate_diagnostics=" in url
        assert "not+in+the+product+catalog" in url

    def test_format_gate_diagnostics_lists_unmapped_readings_and_every_reason(self, monkeypatch):
        """Both the uncurated-reading list and all block reasons are rendered, not just one."""
        import custom_components.rainpoint.generic_entities as generic_entities_module

        monkeypatch.setattr(generic_entities_module, "is_hand_written_model", lambda model: False)
        monkeypatch.setattr(
            generic_entities_module,
            "get_catalog_entry",
            lambda model, model_code=None: [
                {"dpCode": 9, "identity": "STA_ALARM", "dpPort": 1},
                {"dpCode": 9, "identity": "STA_BAT", "dpPort": 2},
            ],
        )
        monkeypatch.setattr(generic_entities_module, "get_catalog_port_number", lambda model, model_code=None: 2)

        text = _coord_module._format_gate_diagnostics("FAKE_MODEL", None)

        assert "Readings with no verified definition yet: STA_ALARM, STA_BAT" in text
        assert len([line for line in text.splitlines() if line.startswith("Blocked: ")]) >= 1

    def test_format_gate_diagnostics_blank_when_nothing_to_say(self, monkeypatch):
        """A model the gate passes contributes no section rather than an empty one."""
        import custom_components.rainpoint.generic_entities as generic_entities_module

        monkeypatch.setattr(generic_entities_module, "is_hand_written_model", lambda model: False)
        monkeypatch.setattr(
            generic_entities_module,
            "get_catalog_entry",
            lambda model, model_code=None: [{"dpCode": 9, "identity": "STA_TEM", "dpPort": 1}],
        )
        monkeypatch.setattr(generic_entities_module, "get_catalog_port_number", lambda model, model_code=None: 1)

        assert _coord_module._format_gate_diagnostics("FAKE_MODEL", None) == ""

    def test_format_generic_fields_empty_returns_blank(self):
        """No decoded fields -> empty string so the caller can omit the section."""
        assert _coord_module._format_generic_fields({"fields": []}) == ""
        assert _coord_module._format_generic_fields(None) == ""

    def test_format_generic_fields_includes_catalog_zone_for_annotated_field(self):
        """A field carrying catalog annotation renders with its zone, for bug-report triage."""
        generic = {
            "dp_id_prefixed": False,
            "fields": [
                {
                    "name": "STA_BAT",
                    "index": 31,
                    "dp_id": 0,
                    "raw": "64",
                    "value": 100,
                    "catalog": {"dp_port": 1, "data_type": "uint8", "port_number": 1, "width_mismatch": False},
                },
            ],
        }

        rendered = _coord_module._format_generic_fields(generic)

        assert "STA_BAT" in rendered
        assert "[zone 1]" in rendered

    def test_format_generic_fields_unannotated_field_renders_unchanged(self):
        """A field without catalog annotation renders exactly as before this plan."""
        generic = {
            "dp_id_prefixed": False,
            "fields": [{"name": "STA_BAT", "index": 31, "dp_id": 0, "raw": "64", "value": 100}],
        }

        assert _coord_module._format_generic_fields(generic) == "STA_BAT: raw=64 value=100"

    @pytest.mark.asyncio
    async def test_notification_id_unchanged_when_model_code_absent(self):
        """Without a modelCode the notification keeps its pre-existing id.

        Suffixing an absent code would produce "..._None", so reloading the
        integration would leave the old notification in place and add a second
        one rather than replacing it.
        """
        with patch.object(_coord_module, "async_create") as mock_notify:
            coord, client = _make_coord()
            client.get_devices_by_hid.return_value = [_make_hub(model="UNKNOWN_NOCODE")]
            client.get_multiple_device_status.return_value = _make_status()

            await _run(coord)

        assert mock_notify.call_args.kwargs["notification_id"] == "rainpoint_unsupported_UNKNOWN_NOCODE"
        assert "modelCode" not in mock_notify.call_args.args[1]

    @pytest.mark.asyncio
    async def test_same_model_different_model_code_each_notify(self):
        """Two variants sharing a model string are reported separately, not deduped.

        The vendor catalog contains model strings mapping to more than one
        modelCode (e.g. HIC801W is both 278 and 279) whose port counts differ,
        so suppressing the second as a duplicate of the first would hide a
        genuinely distinct device.
        """
        with patch.object(_coord_module, "async_create") as mock_notify:
            coord, client = _make_coord()
            client.get_multiple_device_status.return_value = _make_status()

            for code in (278, 279):
                hub = _make_hub(model="UNKNOWN_VARIANT")
                hub["subDevices"][0]["modelCode"] = code
                client.get_devices_by_hid.return_value = [hub]
                await _run(coord)

        assert mock_notify.call_count == 2
        assert coord._notified_unknown_models == {("UNKNOWN_VARIANT", 278), ("UNKNOWN_VARIANT", 279)}

    @pytest.mark.asyncio
    async def test_model_code_reaches_the_catalog_lookup(self):
        """The device's modelCode is threaded into generic decoding, not dropped.

        Without it the catalog cannot tell two variants of one model string
        apart, and enrichment would annotate a payload with the other
        variant's port metadata.
        """
        seen = []

        def _record(model, model_code=None):
            seen.append((model, model_code))
            return None

        with (
            patch.object(_coord_module, "async_create"),
            patch("custom_components.rainpoint.api.generic_decoder.get_catalog_entry", _record),
        ):
            coord, client = _make_coord()
            hub = _make_hub(model="UNKNOWN_VARIANT")
            hub["subDevices"][0]["modelCode"] = 279
            client.get_devices_by_hid.return_value = [hub]
            client.get_multiple_device_status.return_value = _make_status()
            await _run(coord)

        assert ("UNKNOWN_VARIANT", 279) in seen

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
        """When get_multiple_device_status raises a transport error, falls back to get_device_status."""
        coord, client = _make_coord()
        client.get_devices_by_hid.return_value = [_make_hub(model=MODEL_MOISTURE_SIMPLE)]
        client.get_multiple_device_status.side_effect = aiohttp.ClientError("API error")
        client.get_device_status.return_value = {"subDeviceStatus": [{"id": "D1", "value": _MOISTURE_SIMPLE_PAYLOAD}]}

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
        client.get_multiple_device_status.side_effect = aiohttp.ClientError("fail")
        client.get_device_status.return_value = {"subDeviceStatus": []}

        await _run(coord)

        # Each hub must be queried individually by its mid
        called_mids = [
            call.kwargs.get("mid", call.args[0] if call.args else None) for call in client.get_device_status.await_args_list
        ]
        assert sorted(called_mids) == [201, 202]

    @pytest.mark.asyncio
    async def test_update_api_error_raises_exception(self):
        """RainPointApiError is translated to UpdateFailed."""
        from homeassistant.helpers.update_coordinator import UpdateFailed

        coord, client = _make_coord()
        client.get_devices_by_hid.side_effect = RainPointApiError("fail")

        with pytest.raises(UpdateFailed):
            await _run(coord)

    @pytest.mark.asyncio
    async def test_update_no_raw_value_skips_decoding(self):
        """Empty 'value' produces data=None for that sensor."""
        coord, client = _make_coord()
        client.get_devices_by_hid.return_value = [_make_hub(model=MODEL_MOISTURE_SIMPLE)]
        client.get_multiple_device_status.return_value = [{"mid": 200, "subDeviceStatus": [{"id": "D1", "value": ""}]}]

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
            "hid",
            "mid",
            "addr",
            "home_name",
            "hub_name",
            "sub_name",
            "model",
            "firmware_version",
            "device_name",
            "product_key",
            "raw_status",
            "data",
        }
        missing = required - sensor.keys()
        assert not missing, f"Sensor entry missing fields: {missing}"

    @pytest.mark.asyncio
    async def test_update_empty_hids(self):
        """No HIDs configured returns empty hubs and sensors."""
        coord, _ = _make_coord(hids=[])

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
        """Each HID triggers a separate get_devices_by_hid call with the right hid."""
        coord, client = _make_coord(hids=[100, 101])
        client.get_devices_by_hid.return_value = []
        client.get_multiple_device_status.return_value = []

        await _run(coord)

        called_hids = [
            call.kwargs.get("hid", call.args[0] if call.args else None) for call in client.get_devices_by_hid.await_args_list
        ]
        assert sorted(called_hids) == [100, 101]

    @pytest.mark.asyncio
    async def test_update_empty_multiple_status_triggers_fallback(self):
        """Empty list from get_multiple_device_status triggers fallback path."""
        coord, client = _make_coord()
        client.get_devices_by_hid.return_value = [_make_hub(model=MODEL_MOISTURE_SIMPLE)]
        client.get_multiple_device_status.return_value = []
        client.get_device_status.return_value = {"subDeviceStatus": [{"id": "D1", "value": _MOISTURE_SIMPLE_PAYLOAD}]}

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

    @pytest.mark.asyncio
    async def test_stale_valve_poll_does_not_overwrite_command_state(self):
        """Older valve cloud status is ignored after a newer command response."""
        coord, client = _make_coord()
        closed_zone = {"open": False, "duration_seconds": 0, "state_raw": 0}
        coord.data = {
            "sensors": {
                "100_200_1": {
                    "data": {"zones": {1: closed_zone}},
                }
            }
        }
        coord._last_valve_command_at = {("100_200_1", 1): datetime(2024, 1, 2, tzinfo=UTC)}
        client.get_devices_by_hid.return_value = [_make_hub(model=MODEL_VALVE_245)]
        client.get_multiple_device_status.return_value = _make_status(
            value=SAMPLE_HTV245_TLV_PAYLOAD,
            time_ms=int(datetime(2024, 1, 1, tzinfo=UTC).timestamp() * 1000),
        )

        result = await _run(coord)

        zone1 = result["sensors"]["100_200_1"]["data"]["zones"][1]
        assert zone1 == closed_zone

    @pytest.mark.asyncio
    async def test_newer_valve_poll_overwrites_command_state(self):
        """Valve status newer than the command timestamp is accepted."""
        coord, client = _make_coord()
        coord.data = {
            "sensors": {
                "100_200_1": {
                    "data": {"zones": {1: {"open": False, "duration_seconds": 0, "state_raw": 0}}},
                }
            }
        }
        coord._last_valve_command_at = {("100_200_1", 1): datetime(2024, 1, 1, tzinfo=UTC)}
        client.get_devices_by_hid.return_value = [_make_hub(model=MODEL_VALVE_245)]
        client.get_multiple_device_status.return_value = _make_status(
            value=SAMPLE_HTV245_TLV_PAYLOAD,
            time_ms=int(datetime(2024, 1, 2, tzinfo=UTC).timestamp() * 1000),
        )

        result = await _run(coord)

        zone1 = result["sensors"]["100_200_1"]["data"]["zones"][1]
        assert zone1["open"] is True
        assert zone1["duration_seconds"] == 60
        assert zone1["state_raw"] == 1

    @pytest.mark.asyncio
    async def test_missing_timestamp_valve_poll_uses_short_guard_window(self):
        """Untimestamped valve polls are ignored only shortly after a command."""
        coord, client = _make_coord()
        closed_zone = {"open": False, "duration_seconds": 0, "state_raw": 0}
        coord.data = {"sensors": {"100_200_1": {"data": {"zones": {1: closed_zone}}}}}
        coord._last_valve_command_at = {("100_200_1", 1): datetime.now(UTC) - timedelta(minutes=1)}
        client.get_devices_by_hid.return_value = [_make_hub(model=MODEL_VALVE_245)]
        client.get_multiple_device_status.return_value = _make_status(value=SAMPLE_HTV245_TLV_PAYLOAD, time_ms=None)

        result = await _run(coord)

        assert result["sensors"]["100_200_1"]["data"]["zones"][1] == closed_zone

    def test_stale_poll_guard_is_scoped_to_valve_models(self):
        """Non-valve decoded data is returned unchanged."""
        coord, _ = _make_coord()
        decoded = {"type": "not_valve", "zones": {1: {"open": True, "duration_seconds": 60, "state_raw": 1}}}
        coord.data = {"sensors": {"100_200_1": {"data": {"zones": {1: {"open": False}}}}}}
        coord._last_valve_command_at = {("100_200_1", 1): datetime(2024, 1, 2, tzinfo=UTC)}

        result = _coord_module.RainPointCoordinator._preserve_recent_valve_command_state(
            coord,
            "100_200_1",
            MODEL_MOISTURE_SIMPLE,
            decoded,
            {"time": int(datetime(2024, 1, 1, tzinfo=UTC).timestamp() * 1000)},
        )

        assert result is decoded
        assert result["zones"][1]["open"] is True

    def test_preserve_skips_when_no_prior_zone_data(self):
        """First refresh has no prior zone state to preserve."""
        coord, _ = _make_coord()
        decoded = {"zones": {1: {"open": True, "duration_seconds": 60, "state_raw": 1}}}

        result = _coord_module.RainPointCoordinator._preserve_recent_valve_command_state(
            coord,
            "100_200_1",
            MODEL_VALVE_245,
            decoded,
            {},
        )

        assert result is decoded

    def test_preserve_handles_none_data_on_first_poll(self):
        """First poll runs while self.data is still None; must not raise AttributeError."""
        coord, _ = _make_coord()
        coord.data = None
        decoded = {"zones": {1: {"open": True, "duration_seconds": 60, "state_raw": 1}}}

        result = _coord_module.RainPointCoordinator._preserve_recent_valve_command_state(
            coord,
            "100_200_1",
            MODEL_VALVE_245,
            decoded,
            {"time": int(datetime(2024, 1, 1, tzinfo=UTC).timestamp() * 1000)},
        )

        assert result is decoded

    def test_status_entry_time_returns_none_for_invalid_time(self):
        """A malformed status time cannot participate in stale-poll comparisons."""
        assert _coord_module._status_entry_time({"time": "not-a-number"}) is None

    def test_record_valve_command_stores_current_aware_time(self):
        """record_valve_command writes the real command timestamp used by stale-poll protection."""
        instance = object.__new__(_coord_module.RainPointCoordinator)
        instance._last_valve_command_at = {}

        recorded = _coord_module.RainPointCoordinator.record_valve_command(instance, "100_200_1", 1)

        assert recorded.tzinfo is UTC
        assert instance._last_valve_command_at[("100_200_1", 1)] is recorded


class TestCoordinatorEdgeBranches:
    """Edge branches: non-integer addr, device_timestamp ValueError, outer generic except."""

    @pytest.mark.asyncio
    async def test_update_skips_non_integer_addr(self):
        """D-prefixed sid with non-integer tail is skipped; valid entries are kept."""
        coord, client = _make_coord()
        hub = _make_hub()
        # Add addr=1 to subDevices so the valid sid="D1" resolves
        hub["subDevices"] = [{"addr": 1, "model": MODEL_MOISTURE_SIMPLE, "name": "Sensor1", "softVer": "1.0"}]
        client.get_devices_by_hid.return_value = [hub]
        client.get_multiple_device_status.return_value = [
            {
                "mid": 200,
                "subDeviceStatus": [
                    {"id": "DABC", "value": _MOISTURE_SIMPLE_PAYLOAD},
                    {"id": "D1", "value": _MOISTURE_SIMPLE_PAYLOAD},
                ],
            }
        ]

        result = await _run(coord)

        assert "100_200_1" in result["sensors"]
        # The DABC entry should have been skipped by the ValueError branch.
        assert len(result["sensors"]) == 1

    @pytest.mark.asyncio
    async def test_update_device_timestamp_value_error_continues(self):
        """A non-numeric 'time' value is swallowed; decoded data lacks device_timestamp."""
        coord, client = _make_coord()
        client.get_devices_by_hid.return_value = [_make_hub(model=MODEL_MOISTURE_SIMPLE)]
        client.get_multiple_device_status.return_value = [
            {
                "mid": 200,
                "subDeviceStatus": [
                    {
                        "id": "D1",
                        "value": _MOISTURE_SIMPLE_PAYLOAD,
                        "time": "not-a-number",
                    }
                ],
            }
        ]

        result = await _run(coord)

        sensor = result["sensors"]["100_200_1"]
        assert sensor["data"] is not None
        assert "device_timestamp" not in sensor["data"]

    @pytest.mark.asyncio
    async def test_update_generic_exception_wraps_as_update_failed(self):
        """A non-RainPointApiError exception is wrapped with 'Unexpected RainPoint error'."""
        from homeassistant.helpers.update_coordinator import UpdateFailed

        coord, client = _make_coord()
        client.get_devices_by_hid.side_effect = RuntimeError("boom")

        with pytest.raises(UpdateFailed, match="Unexpected RainPoint error"):
            await _run(coord)

    @pytest.mark.asyncio
    async def test_update_individual_fallback_hub_error_continues(self):
        """When multipleDeviceStatus fails AND a per-hub get_device_status also fails
        with a transport error, that hub is recorded with an empty subDeviceStatus list
        and iteration continues."""
        coord, client = _make_coord()
        hub1 = _make_hub(mid=301, model=MODEL_MOISTURE_SIMPLE)
        hub2 = _make_hub(mid=302, model=MODEL_MOISTURE_SIMPLE)
        client.get_devices_by_hid.return_value = [hub1, hub2]
        client.get_multiple_device_status.side_effect = aiohttp.ClientError("first call fails")

        # Second fallback call per-hub: first hub raises a transport error, second returns empty
        def per_hub(mid):
            if mid == 301:
                raise aiohttp.ClientError("per-hub transport boom")
            return {"subDeviceStatus": []}

        client.get_device_status.side_effect = per_hub

        # Should NOT raise: transport errors per-hub are logged and the loop continues.
        result = await _run(coord)

        # Both hubs made it into the output; neither has decoded sensor data
        # (hub1 fallback produced empty list; hub2 explicitly returned empty list).
        assert len(result["hubs"]) == 2
        assert result["sensors"] == {}


class TestCoordinatorConstructor:
    """Direct constructor tests for RainPointCoordinator.__init__ (lines 133-142)."""

    def test_constructor_reads_hids_from_entry_data(self):
        """__init__ must pull CONF_HIDS list off entry.data and seed bookkeeping state."""
        # Bypass the MagicMock-stubbed DataUpdateCoordinator base by calling the
        # real __init__ function directly. We verify our subclass's state-setting
        # happens (the super().__init__ call goes into the mocked base, which is
        # fine -- we only care that the RainPoint-specific assignments ran).
        from types import SimpleNamespace

        import custom_components.rainpoint.coordinator as coord_mod

        real_init = coord_mod.RainPointCoordinator.__dict__["__init__"]

        # Fake entry with known HIDs list
        entry = SimpleNamespace(data={"hids": [11, 22, 33]})
        hass = MagicMock()
        client = MagicMock()

        instance = object.__new__(coord_mod.RainPointCoordinator)
        real_init(instance, hass, client, entry)

        assert instance._client is client
        assert instance._entry is entry
        assert instance._hids == [11, 22, 33]
        assert instance._notified_unknown_models == set()
        assert instance._last_valve_command_at == {}

    def test_constructor_empty_hids_defaults_to_empty_list(self):
        """__init__ falls back to [] when CONF_HIDS missing from entry.data."""
        from types import SimpleNamespace

        import custom_components.rainpoint.coordinator as coord_mod

        real_init = coord_mod.RainPointCoordinator.__dict__["__init__"]

        entry = SimpleNamespace(data={})  # no "hids" key
        hass = MagicMock()
        client = MagicMock()
        instance = object.__new__(coord_mod.RainPointCoordinator)

        real_init(instance, hass, client, entry)

        assert instance._hids == []


class TestDecoderRegistry:
    """Tests for the DECODER_REGISTRY constant."""

    def test_registry_is_dict(self):
        """Registry is dict."""
        assert isinstance(DECODER_REGISTRY, dict)

    def test_registry_contains_required_models(self):
        """Registry must cover every model we claim to support."""
        required = {
            MODEL_CO2,
            MODEL_FLOWMETER,
            MODEL_MOISTURE_FULL,
            MODEL_MOISTURE_SIMPLE,
            MODEL_RAIN,
            MODEL_TEMPHUM,
            MODEL_VALVE_213,
            MODEL_VALVE_245,
            MODEL_VALVE_345,
            MODEL_VALVE_405,
            MODEL_VALVE_HUB,
        }
        missing = required - DECODER_REGISTRY.keys()
        assert not missing, f"DECODER_REGISTRY missing required models: {missing}"

    def test_valve_113_dispatches_through_registry_to_htv145_decoder(self):
        """HTV113FRF is decoded by reusing the HTV145FRF decoder. Assert the wiring
        via the registry/dispatch path, not by calling decode_htv145frf directly, so
        a broken or removed MODEL_VALVE_113 registry entry is caught."""
        assert DECODER_REGISTRY[MODEL_VALVE_113] is decode_htv145frf

        dispatched = _coord_module._decode_subdevice_payload(MODEL_VALVE_113, SAMPLE_HTV113_IDLE_PAYLOAD)

        # Registry dispatch must preserve the decoder's own result exactly.
        assert dispatched == decode_htv145frf(SAMPLE_HTV113_IDLE_PAYLOAD)
        assert dispatched["decoder"] == "htv145frf_hex"
        assert dispatched["hub_online"] is True

    def test_registry_contains_moisture_simple(self):
        """Registry contains moisture simple."""
        assert MODEL_MOISTURE_SIMPLE in DECODER_REGISTRY

    def test_registry_contains_moisture_full(self):
        """Registry contains moisture full."""
        assert MODEL_MOISTURE_FULL in DECODER_REGISTRY

    def test_registry_contains_rain(self):
        """Registry contains rain."""
        assert MODEL_RAIN in DECODER_REGISTRY

    def test_registry_contains_temphum(self):
        """Registry contains temphum."""
        assert MODEL_TEMPHUM in DECODER_REGISTRY

    def test_registry_contains_flowmeter(self):
        """Registry contains flowmeter."""
        assert MODEL_FLOWMETER in DECODER_REGISTRY

    def test_registry_contains_co2(self):
        """Registry contains co2."""
        assert MODEL_CO2 in DECODER_REGISTRY

    def test_registry_contains_valve_245(self):
        """Registry contains valve 245."""
        assert MODEL_VALVE_245 in DECODER_REGISTRY

    def test_registry_contains_valve_345(self):
        """Registry contains valve 345."""
        assert MODEL_VALVE_345 in DECODER_REGISTRY

    def test_registry_contains_valve_405(self):
        """Registry contains valve 405."""
        assert MODEL_VALVE_405 in DECODER_REGISTRY

    def test_registry_contains_valve_213(self):
        """Registry contains valve 213."""
        assert MODEL_VALVE_213 in DECODER_REGISTRY

    def test_registry_contains_valve_hub(self):
        """Registry contains valve hub."""
        assert MODEL_VALVE_HUB in DECODER_REGISTRY

    def test_registry_display_hub_not_in_registry(self):
        """MODEL_DISPLAY_HUB uses a special-case code path, not DECODER_REGISTRY."""
        assert MODEL_DISPLAY_HUB not in DECODER_REGISTRY

    def test_registry_values_are_callable(self):
        """Every value is a callable decoder function."""
        for model, fn in DECODER_REGISTRY.items():
            assert callable(fn), f"Decoder for {model!r} is not callable"


class TestPureHelpers:
    """Direct-call tests for module-level pure helpers extracted from _async_update_data."""

    # _resolve_addr_from_sid
    def test_resolve_addr_from_sid_valid(self):
        """A 'D'-prefixed sid with integer tail returns the integer."""
        assert _coord_module._resolve_addr_from_sid("D1") == 1

    def test_resolve_addr_from_sid_multi_digit(self):
        """Multi-digit integer tails are parsed as a single base-10 integer."""
        assert _coord_module._resolve_addr_from_sid("D42") == 42

    def test_resolve_addr_from_sid_non_d_prefix(self):
        """sids that do not start with 'D' return None."""
        assert _coord_module._resolve_addr_from_sid("X1") is None

    def test_resolve_addr_from_sid_non_integer_tail(self):
        """sids whose tail is not a base-10 integer return None."""
        assert _coord_module._resolve_addr_from_sid("DABC") is None

    # _decode_subdevice_payload
    def test_decode_subdevice_payload_known_model(self):
        """Known models dispatch through DECODER_REGISTRY and return the decoded dict."""
        result = _coord_module._decode_subdevice_payload(MODEL_MOISTURE_SIMPLE, _MOISTURE_SIMPLE_PAYLOAD)
        assert result["type"] == "moisture_simple"

    def test_decode_subdevice_payload_valve_345_model(self):
        """HTV345FRF dispatches through the shared HTV213/245 valve decoder."""
        result = _coord_module._decode_subdevice_payload(MODEL_VALVE_345, SAMPLE_HTV245_TLV_PAYLOAD)
        assert result["type"] == "valve_hub"
        assert result["decoder"] == "htv213frf_hex"

    def test_decode_subdevice_payload_valve_405_model(self):
        """HTV405FRF dispatches through the shared HTV213/245 valve decoder."""
        result = _coord_module._decode_subdevice_payload(MODEL_VALVE_405, SAMPLE_HTV405_TLV_PAYLOAD)
        assert result["type"] == "valve_hub"
        assert result["decoder"] == "htv213frf_hex"
        assert set(result["zones"]) == {1, 2, 3, 4}

    def test_decode_subdevice_payload_display_hub_special_case(self):
        """MODEL_DISPLAY_HUB routes to decode_hws019wrf_v2, not the registry."""
        result = _coord_module._decode_subdevice_payload(MODEL_DISPLAY_HUB, _DISPLAY_HUB_PAYLOAD)
        assert result["type"] == "hws019wrf_v2"

    def test_decode_subdevice_payload_unknown_model(self):
        """Unknown models return the {'type': 'unknown', ...} shape."""
        result = _coord_module._decode_subdevice_payload("UNKNOWN_XYZ", "10#DEAD")
        assert result["type"] == "unknown"
        assert result["model"] == "UNKNOWN_XYZ"
        assert result["raw_value"] == "10#DEAD"
        # Unknown payloads carry a best-effort structural decode for diagnostics.
        assert result["generic"]["decoder"] == "generic-tlv"

    def test_decode_subdevice_payload_unknown_model_carries_catalog_annotation(self):
        """A catalog-recognized unsupported model's unknown-branch decode is enriched."""
        # STA_BAT is declared by the anchor model in the committed catalog.
        result = _coord_module._decode_subdevice_payload(CATALOG_ANCHOR_MODEL, SAMPLE_UNSUPPORTED_MULTI_SENSOR_PAYLOAD)

        assert result["type"] == "unknown"
        fields_by_name = {f["name"]: f for f in result["generic"]["fields"]}
        catalog = fields_by_name["STA_BAT"]["catalog"]
        assert catalog["dp_port"] == 0
        assert catalog["declared_width"] == 1
        assert catalog["port_number"] == 1
        assert catalog["width_mismatch"] is False

    def test_decode_subdevice_payload_registered_model_never_reaches_generic_path(self):
        """A DECODER_REGISTRY model always dispatches to its hand-written decoder,
        never diverting into the generic/unknown branch, confirming the trust
        boundary between hand-written and catalog-driven decoding holds."""
        result = _coord_module._decode_subdevice_payload(MODEL_MOISTURE_SIMPLE, _MOISTURE_SIMPLE_PAYLOAD)

        assert result["type"] != "unknown"
        assert "generic" not in result

    # _attach_device_timestamp
    def test_attach_device_timestamp_valid_ms(self):
        """A valid epoch-ms 'time' adds device_timestamp + timestamp_source."""
        decoded = {"type": "x"}
        _coord_module._attach_device_timestamp(decoded, {"time": 1700000000000})
        assert "device_timestamp" in decoded
        assert decoded["timestamp_source"] == "device"

    def test_attach_device_timestamp_decoded_is_none_is_noop(self):
        """A None decoded value is a no-op and does not raise."""
        # Should not raise; nothing to mutate.
        _coord_module._attach_device_timestamp(None, {"time": 1700000000000})

    def test_attach_device_timestamp_invalid_time_swallowed(self):
        """A non-numeric 'time' value is swallowed; decoded gains no timestamp keys."""
        decoded = {"type": "x"}
        _coord_module._attach_device_timestamp(decoded, {"time": "not-a-number"})
        assert "device_timestamp" not in decoded

    def test_attach_device_timestamp_no_time_key_is_noop(self):
        """Missing 'time' key leaves decoded unchanged."""
        decoded = {"type": "x"}
        _coord_module._attach_device_timestamp(decoded, {})
        assert "device_timestamp" not in decoded

    def test_attach_device_timestamp_zero_is_valid_epoch(self):
        """A 'time' of 0 is the Unix epoch, not a missing value."""
        decoded = {"type": "x"}
        _coord_module._attach_device_timestamp(decoded, {"time": 0})
        assert decoded["device_timestamp"] == "1970-01-01T00:00:00+00:00"
        assert decoded["timestamp_source"] == "device"

    # _build_sensor_entry
    def test_build_sensor_entry_returns_all_fields(self):
        """The returned dict carries every required metadata key."""
        hub = {"hid": 100, "name": "MyHub", "homeName": "Home", "deviceName": "dev1", "productKey": "pk1"}
        sub = {"name": "Sensor1", "model": "MODEL_X", "softVer": "1.0"}
        s = {"id": "D1", "value": "10#AB"}
        entry = _coord_module._build_sensor_entry(hub, sub, mid=200, addr=1, status_entry=s, decoded={"type": "x"})
        for key in (
            "hid",
            "mid",
            "addr",
            "home_name",
            "hub_name",
            "sub_name",
            "model",
            "firmware_version",
            "device_name",
            "product_key",
            "raw_status",
            "data",
        ):
            assert key in entry
        assert entry["hub_name"] == "MyHub"
        assert entry["data"] == {"type": "x"}

    def test_build_sensor_entry_hub_name_defaults_to_Hub(self):
        """When hub has no 'name' key, hub_name falls back to 'Hub'."""
        hub = {"hid": 100, "homeName": "Home", "deviceName": "d", "productKey": "p"}  # no "name" key
        sub = {"name": "S", "model": "M", "softVer": "1.0"}
        entry = _coord_module._build_sensor_entry(hub, sub, mid=200, addr=1, status_entry={"id": "D1"}, decoded=None)
        assert entry["hub_name"] == "Hub"


class TestApiErrorSurfacing:
    """RainPointApiError from the multi-status or per-hub fallback path must surface as UpdateFailed."""

    @pytest.mark.asyncio
    async def test_multi_status_api_error_surfaces_as_update_failed(self):
        """RainPointApiError from get_multiple_device_status propagates to UpdateFailed.

        Previously the inner ``except Exception`` swallowed RainPointApiError and silently
        fell back to per-hub get_device_status, masking auth/token/5xx failures from HA.
        With the narrowed ``except RainPointApiError: raise`` clause, the error must reach
        the outer UpdateFailed wrapper and the per-hub fallback must NOT run.
        """
        from homeassistant.helpers.update_coordinator import UpdateFailed

        coord, client = _make_coord()
        client.get_devices_by_hid.return_value = [_make_hub(model=MODEL_MOISTURE_SIMPLE)]
        client.get_multiple_device_status.side_effect = RainPointApiError("token expired")
        # Even if a fallback per-hub call would have succeeded, RainPointApiError must surface.
        client.get_device_status.return_value = {"subDeviceStatus": [{"id": "D1", "value": _MOISTURE_SIMPLE_PAYLOAD}]}

        with pytest.raises(UpdateFailed):
            await _run(coord)

        # Critical assertion: the per-hub fallback must NOT have been invoked, because the
        # narrow ``except RainPointApiError: raise`` re-raises before reaching the per-hub loop.
        assert client.get_device_status.await_count == 0

    @pytest.mark.asyncio
    async def test_per_hub_api_error_surfaces_as_update_failed(self):
        """RainPointApiError from per-hub get_device_status (during fallback) propagates to UpdateFailed.

        Previously the inner ``except Exception as individual_e`` swallowed RainPointApiError
        and silently recorded ``{"subDeviceStatus": []}`` for that hub, hiding the failure.
        With the narrowed ``except RainPointApiError: raise`` clause in the per-hub fallback,
        the error must reach the outer UpdateFailed wrapper.
        """
        from homeassistant.helpers.update_coordinator import UpdateFailed

        coord, client = _make_coord()
        client.get_devices_by_hid.return_value = [_make_hub(model=MODEL_MOISTURE_SIMPLE)]
        # Force fallback: a transport-level error trips the narrow
        # ``except (aiohttp.ClientError, TimeoutError):`` clause on multi-status.
        client.get_multiple_device_status.side_effect = aiohttp.ClientError("transient")
        # Then per-hub raises RainPointApiError.
        client.get_device_status.side_effect = RainPointApiError("per-hub auth failure")

        with pytest.raises(UpdateFailed):
            await _run(coord)


class TestNonTransportErrorsPropagate:
    """Non-transport exceptions (programming bugs) must surface as UpdateFailed
    instead of being silently swallowed by the multi-status / per-hub fallbacks."""

    @pytest.mark.asyncio
    async def test_multi_status_non_transport_error_does_not_fall_back(self):
        """A KeyError from get_multiple_device_status surfaces as UpdateFailed and
        does NOT trigger the per-hub fallback, so the bug is visible to operators."""
        from homeassistant.helpers.update_coordinator import UpdateFailed

        coord, client = _make_coord()
        client.get_devices_by_hid.return_value = [_make_hub(model=MODEL_MOISTURE_SIMPLE)]
        # Programming bug shape: KeyError is NOT aiohttp.ClientError / TimeoutError.
        client.get_multiple_device_status.side_effect = KeyError("missing-key")
        # If the fallback were wrongly invoked, this would mask the bug.
        client.get_device_status.return_value = {"subDeviceStatus": []}

        with pytest.raises(UpdateFailed, match="Unexpected RainPoint error"):
            await _run(coord)

        # Critical assertion: the per-hub fallback must NOT have been invoked, because
        # KeyError no longer matches the narrow except clause.
        assert client.get_device_status.await_count == 0

    @pytest.mark.asyncio
    async def test_per_hub_non_transport_error_does_not_swallow(self):
        """An AttributeError raised by per-hub get_device_status surfaces as UpdateFailed
        instead of being recorded as an empty subDeviceStatus list."""
        from homeassistant.helpers.update_coordinator import UpdateFailed

        coord, client = _make_coord()
        client.get_devices_by_hid.return_value = [_make_hub(model=MODEL_MOISTURE_SIMPLE)]
        # Force fallback through a real transport error.
        client.get_multiple_device_status.side_effect = aiohttp.ClientError("transient")
        # Per-hub raises a programming bug, NOT a transport error.
        client.get_device_status.side_effect = AttributeError("bad attr")

        with pytest.raises(UpdateFailed, match="Unexpected RainPoint error"):
            await _run(coord)


def _push_hub(hid=100, mid=200, addr=1, model=MODEL_VALVE_245):
    """Hub record shaped as coordinator.data carries it (hid already injected)."""
    return {
        "hid": hid,
        "mid": mid,
        "name": "Hub1",
        "deviceName": "dev1",
        "productKey": "pk1",
        "homeName": "Home",
        "subDevices": [{"addr": addr, "model": model, "name": "Valve", "softVer": "1.0"}],
    }


def _seed_push_coord(hub, sensors=None, status=None):
    """Build a coord namespace with data seeded and listener spies attached."""
    coord, _client = _make_coord()
    coord.data = {
        "hubs": [hub],
        "status": status if status is not None else {},
        "sensors": sensors if sensors is not None else {},
    }
    coord.async_update_listeners = MagicMock()
    coord.async_set_updated_data = MagicMock()
    return coord


_APPLY = _coord_module.RainPointCoordinator.apply_push_update


class TestApplyPushUpdate:
    """apply_push_update: copy-on-write merge through the poll decode path,
    notifying listeners without resetting the poll timer, and dropping misses."""

    def test_push_updates_target_and_preserves_sibling_identity(self):
        """The pushed sub-device's data is decoded and merged; every other sensors
        key keeps its object identity and listeners are notified exactly once."""
        hub = _push_hub()
        sibling = {"data": {"unchanged": True}}
        coord = _seed_push_coord(
            hub,
            sensors={"100_200_1": {"data": None}, "999_999_9": sibling},
            status={200: {"subDeviceStatus": [{"id": "D1", "value": "old", "time": 1}]}},
        )
        device_ts = int(datetime(2024, 6, 1, tzinfo=UTC).timestamp() * 1000)

        _APPLY(coord, 200, "D1", SAMPLE_HTV245_TLV_PAYLOAD, device_ts)

        updated = coord.data["sensors"]["100_200_1"]["data"]
        assert updated is not None
        assert "zones" in updated
        # Sibling sensor object identity preserved (copy-on-write, not rebuild).
        assert coord.data["sensors"]["999_999_9"] is sibling
        # Hub list carried by reference.
        assert coord.data["hubs"][0] is hub
        # device_ts threaded into the synthetic status entry.
        assert coord.data["status"][200]["subDeviceStatus"][0]["time"] == device_ts
        coord.async_update_listeners.assert_called_once()
        coord.async_set_updated_data.assert_not_called()

    def test_push_status_entry_appended_when_absent(self):
        """A push for a mid with no prior status branch appends a fresh entry."""
        hub = _push_hub()
        coord = _seed_push_coord(hub, sensors={"100_200_1": {"data": None}}, status={})

        _APPLY(coord, 200, "D1", SAMPLE_HTV245_TLV_PAYLOAD, 1717200000000)

        sub_status = coord.data["status"][200]["subDeviceStatus"]
        assert [e["id"] for e in sub_status] == ["D1"]
        coord.async_update_listeners.assert_called_once()

    def test_push_replaces_matching_entry_after_iterating_past_others(self):
        """When the mid's status already holds an unrelated sub-device id, the
        merge iterates past it and replaces the matching entry in place, leaving
        the unrelated entry untouched."""
        hub = _push_hub()
        coord = _seed_push_coord(
            hub,
            sensors={"100_200_1": {"data": None}},
            status={
                200: {
                    "subDeviceStatus": [
                        {"id": "D9", "value": "other", "time": 1},
                        {"id": "D1", "value": "old", "time": 1},
                    ]
                }
            },
        )
        device_ts = 1717200000000

        _APPLY(coord, 200, "D1", SAMPLE_HTV245_TLV_PAYLOAD, device_ts)

        sub_status = coord.data["status"][200]["subDeviceStatus"]
        # Unrelated D9 entry preserved; only the matching D1 entry is replaced.
        assert [e["id"] for e in sub_status] == ["D9", "D1"]
        assert sub_status[0]["value"] == "other"
        assert sub_status[1]["time"] == device_ts
        coord.async_update_listeners.assert_called_once()

    def test_unknown_mid_is_dropped_without_mutating_or_notifying(self):
        """A push whose mid is not in data['hubs'] leaves data identity-unchanged."""
        hub = _push_hub()
        coord = _seed_push_coord(hub, sensors={"100_200_1": {"data": None}})
        original = coord.data

        _APPLY(coord, 999, "D1", SAMPLE_HTV245_TLV_PAYLOAD, 1717200000000)

        assert coord.data is original
        coord.async_update_listeners.assert_not_called()
        coord.async_set_updated_data.assert_not_called()

    def test_unknown_addr_is_dropped_without_mutating_or_notifying(self):
        """A push whose resolved addr is not a reported sub-device is dropped."""
        hub = _push_hub(addr=1)
        coord = _seed_push_coord(hub, sensors={"100_200_1": {"data": None}})
        original = coord.data

        _APPLY(coord, 200, "D9", SAMPLE_HTV245_TLV_PAYLOAD, 1717200000000)

        assert coord.data is original
        coord.async_update_listeners.assert_not_called()

    def test_unresolvable_sid_is_dropped(self):
        """A sid that does not resolve to an integer addr is dropped."""
        hub = _push_hub()
        coord = _seed_push_coord(hub, sensors={"100_200_1": {"data": None}})
        original = coord.data

        _APPLY(coord, 200, "state", SAMPLE_HTV245_TLV_PAYLOAD, 1717200000000)

        assert coord.data is original
        coord.async_update_listeners.assert_not_called()

    def test_push_before_first_poll_is_dropped(self):
        """A push arriving before the first poll seeded data is dropped safely."""
        coord, _ = _make_coord()
        coord.data = None
        coord.async_update_listeners = MagicMock()

        _APPLY(coord, 200, "D1", SAMPLE_HTV245_TLV_PAYLOAD, 1717200000000)

        coord.async_update_listeners.assert_not_called()

    def test_stale_push_after_command_preserves_commanded_zone(self):
        """A push whose device timestamp predates a fresh valve command does not
        revert the just-commanded zone state (the valve-race guard, via push)."""
        hub = _push_hub(model=MODEL_VALVE_245)
        closed_zone = {"open": False, "duration_seconds": 0, "state_raw": 0}
        coord = _seed_push_coord(hub, sensors={"100_200_1": {"data": {"zones": {1: closed_zone}}}})
        command_dt = _coord_module.RainPointCoordinator.record_valve_command(coord, "100_200_1", 1)
        stale_ts = int((command_dt - timedelta(seconds=30)).timestamp() * 1000)

        # SAMPLE_HTV245_TLV_PAYLOAD decodes zone 1 to OPEN; the stale push must not apply it.
        _APPLY(coord, 200, "D1", SAMPLE_HTV245_TLV_PAYLOAD, stale_ts)

        assert coord.data["sensors"]["100_200_1"]["data"]["zones"][1] == closed_zone

    def test_fresh_push_after_command_applies_new_zone_state(self):
        """A push whose device timestamp postdates the command is applied normally."""
        hub = _push_hub(model=MODEL_VALVE_245)
        closed_zone = {"open": False, "duration_seconds": 0, "state_raw": 0}
        coord = _seed_push_coord(hub, sensors={"100_200_1": {"data": {"zones": {1: closed_zone}}}})
        command_dt = _coord_module.RainPointCoordinator.record_valve_command(coord, "100_200_1", 1)
        fresh_ts = int((command_dt + timedelta(seconds=30)).timestamp() * 1000)

        _APPLY(coord, 200, "D1", SAMPLE_HTV245_TLV_PAYLOAD, fresh_ts)

        zone1 = coord.data["sensors"]["100_200_1"]["data"]["zones"][1]
        assert zone1["open"] is True
        assert zone1["state_raw"] == 1


class TestIssueUrlLengthBudget:
    """The pre-filled report link is capped so GitHub cannot answer it with 414 URI Too Long.

    Three growable fields feed this URL. The raw payload is preferred over
    the other two: it is the only one that cannot be regenerated later. The
    auto-decode is recomputable from that payload and the catalog summary
    from the model and modelCode, so both are cut first. A payload that blows
    the budget on its own is the exception, since a link too long to open
    carries nothing at all.
    """

    _PAYLOAD = "11#" + ("0100AD3C00" * 20)

    def _url(self, budget=None):
        import custom_components.rainpoint.coordinator as coord

        if budget is None:
            return coord._build_new_device_issue_url("HTV445FRF", self._PAYLOAD, 360)
        with patch.object(coord, "ISSUE_URL_MAX_LENGTH", budget):
            return coord._build_new_device_issue_url("HTV445FRF", self._PAYLOAD, 360)

    @staticmethod
    def _fields(url):
        from urllib.parse import parse_qs, urlparse

        return parse_qs(urlparse(url).query)

    def test_realistic_worst_case_needs_no_truncation(self):
        """The largest committed catalog variant with a long payload still fits comfortably."""
        url = self._url()

        assert len(url) < _coord_module.ISSUE_URL_MAX_LENGTH
        assert "truncated to keep" not in url

    def test_gate_diagnostics_is_sacrificed_before_the_auto_decode(self):
        """Lowest-value field first: the catalog summary is fully derivable from model plus modelCode."""
        url = self._url(budget=700)
        fields = self._fields(url)

        assert len(url) <= 700
        assert "gate_diagnostics" not in fields
        assert "auto_decoded" in fields

    def test_the_raw_payload_outlives_both_optional_fields(self):
        """At a budget only the payload fits, it survives intact and the optional fields go."""
        url = self._url(budget=400)
        fields = self._fields(url)

        assert fields["primary_payload"][0] == self._PAYLOAD
        assert "auto_decoded" not in fields
        assert "gate_diagnostics" not in fields

    def test_a_payload_that_blows_the_budget_alone_is_replaced_by_an_instruction(self):
        """A link GitHub refuses to open would carry the payload nowhere, so the link wins.

        The payload is not lost with it: the device's raw payload sensor is
        named in the field the payload would have filled, so the reporter is
        told where to copy it from.
        """
        url = self._url(budget=300)
        fields = self._fields(url)

        assert len(url) <= 300
        assert fields["primary_payload"][0] == _coord_module._ISSUE_PAYLOAD_TOO_LONG_NOTE
        assert self._PAYLOAD not in url
        assert "auto_decoded" not in fields
        assert "gate_diagnostics" not in fields

    def test_a_field_that_fits_only_partially_is_truncated_with_a_marker(self):
        """A budget leaving room for some of the text keeps that text and says it was cut."""
        params = {"template": "new_device.yml", "model": "X"}
        long_value = "D" * 4000

        with patch.object(_coord_module, "ISSUE_URL_MAX_LENGTH", 500):
            fitted = _coord_module._fit_param(params, "gate_diagnostics", long_value)

        assert "gate_diagnostics" in fitted
        assert fitted["gate_diagnostics"].startswith("DDDD")
        assert len(fitted["gate_diagnostics"]) < len(long_value)
        assert "truncated to keep" in fitted["gate_diagnostics"]
        assert len(_coord_module._url_for_params(fitted)) <= 500

    def test_a_field_with_no_room_at_all_is_omitted_not_left_as_a_bare_marker(self):
        """A lone truncation marker would be noise; the field is dropped instead."""
        params = {"template": "new_device.yml", "model": "X"}

        with patch.object(_coord_module, "ISSUE_URL_MAX_LENGTH", 60):
            fitted = _coord_module._fit_param(params, "gate_diagnostics", "D" * 4000)

        assert "gate_diagnostics" not in fitted
