"""Tests for RainPointCoordinator: data fetching, decoder dispatch, fallback, and error handling."""

import json
import logging
import types
from datetime import UTC, datetime, timedelta
from typing import ClassVar
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
SILENT_DATA_TYPE = _coord_module.SILENT_DATA_TYPE

import custom_components.rainpoint.repairs as _repairs_module  # noqa: E402
from custom_components.rainpoint.api import RainPointApiError, decode_htv145frf  # noqa: E402
from custom_components.rainpoint.const import (  # noqa: E402
    CONF_HIDS,
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
from custom_components.rainpoint.entity import sub_device_attributes  # noqa: E402
from custom_components.rainpoint.repairs import (  # noqa: E402
    RainPointSilentDeviceIssues,
    hub_connectivity_issue_id,
    silent_device_issue_id,
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
        _silent_poll_counts={},
        _silent_issues=MagicMock(),
        _hub_disconnect_poll_counts={},
        _hub_connectivity_issues=MagicMock(),
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
        hub = _make_hub(model="HWG023WBRF-V2")
        # Only the serialized record can carry this; model and mid reach the log
        # line as their own arguments, so asserting on them alone would not prove
        # the whole hub record was dumped.
        hub["rfChannel"] = "canary-7"
        client.get_devices_by_hid.return_value = [hub]
        client.get_multiple_device_status.return_value = _make_status()

        with caplog.at_level(logging.DEBUG, logger="custom_components.rainpoint.coordinator"):
            await _run(coord)

        assert any(
            "Raw hub record" in r.message and "HWG023WBRF-V2" in r.message and "canary-7" in r.message for r in caplog.records
        )

    @pytest.mark.asyncio
    async def test_raw_hub_record_not_logged_above_debug(self, caplog):
        """Above DEBUG, the raw hub record dump is skipped (guarded json.dumps).

        Asserting that json.dumps never runs is what proves the guard skipped the
        serialization; an absent log record alone would also be satisfied by the
        logger filtering a record whose arguments had already been evaluated.
        """
        coord, client = _make_coord()
        client.get_devices_by_hid.return_value = [_make_hub(model="HWG023WBRF-V2")]
        client.get_multiple_device_status.return_value = _make_status()

        with (
            patch.object(_coord_module.json, "dumps", wraps=json.dumps) as dumps,
            caplog.at_level(logging.INFO, logger="custom_components.rainpoint.coordinator"),
        ):
            await _run(coord)

        assert dumps.call_count == 0
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

    def test_notification_neutralizes_the_cloud_model_but_keeps_the_payload_intact(self):
        """The notification is Markdown, so the model is treated; the payload is not.

        Running the placeholder sanitizer over the payload would strip "#" and
        "|" and destroy the one thing in the notification that cannot be
        regenerated, so it only loses what could close the code fence.
        """
        coord, _client = _make_coord()

        with patch.object(_coord_module, "async_create") as mock_notify:
            _coord_module.RainPointCoordinator._notify_unknown_model(
                coord,
                model="[Evil](http://evil.example/x)",
                model_code=None,
                mid=200,
                addr=1,
                raw_value="10#E1BC00|1,-84;2`\n```",
            )

        message = mock_notify.call_args.args[1]
        # The model can no longer render as a link.
        assert "[Evil]" not in message
        assert "://" not in message.split("Report this device")[0]
        # The payload keeps every character a real one carries.
        assert "10#E1BC00|1,-84;2" in message
        # ...and loses only what would break out of the fence.
        assert message.count("```") == 2

    def test_notification_id_still_keys_on_the_raw_model(self):
        """Sanitizing the copy must not re-key the dedup id, or a reload would
        add a second notification instead of replacing the first."""
        coord, _client = _make_coord()

        with patch.object(_coord_module, "async_create") as mock_notify:
            _coord_module.RainPointCoordinator._notify_unknown_model(
                coord, model="ODD*MODEL", model_code=None, mid=200, addr=1, raw_value="10#AA"
            )

        assert mock_notify.call_args.kwargs["notification_id"] == "rainpoint_unsupported_ODD*MODEL"

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

    def test_build_new_device_issue_url_payload_note_fills_primary_payload(self):
        """payload_note lands in primary_payload verbatim, not left blank (D-15)."""
        url = _coord_module._build_new_device_issue_url("HTV210B", None, 360, payload_note=_coord_module.NO_STATUS_PAYLOAD_MARKER)

        primary_payload_value = url.split("primary_payload=")[1].split("&")[0]
        assert primary_payload_value != ""
        assert "returns+no+status" in url
        assert "model_code=360" in url

    def test_build_new_device_issue_url_payload_note_suppresses_auto_decode(self):
        """A payload_note skips the auto-decode step even when raw_value is also a decodable payload.

        decode_generic cannot read prose, so running it against a marker string
        would waste a decode to produce nothing; payload_note takes over the
        primary_payload field entirely and auto_decoded must not appear.
        """
        url = _coord_module._build_new_device_issue_url(
            "HTV210B",
            SAMPLE_HTV245_TLV_PAYLOAD,
            payload_note=_coord_module.NO_STATUS_PAYLOAD_MARKER,
        )

        assert "auto_decoded=" not in url
        assert "returns+no+status" in url

    def test_build_new_device_issue_url_omitting_payload_note_is_unaffected(self):
        """Existing callers that pass no payload_note keep byte-identical output."""
        without_kwarg = _coord_module._build_new_device_issue_url("HTV210B", None, 123)
        with_default = _coord_module._build_new_device_issue_url("HTV210B", None, 123, payload_note=None)

        assert without_kwarg == with_default
        assert "auto_decoded=" not in without_kwarg

    def test_format_gate_diagnostics_lists_unmapped_readings_and_every_reason(self, monkeypatch):
        """Both the uncurated-reading list and all block reasons are rendered, not just one."""
        import custom_components.rainpoint.generic_entities as generic_entities_module

        monkeypatch.setattr(generic_entities_module, "is_hand_written_model", lambda model: False)
        monkeypatch.setattr(
            generic_entities_module,
            "get_catalog_entry",
            lambda model, model_code=None: [
                {"dpCode": 9, "identity": "STA_ALARM", "dpPort": 1},
                {"dpCode": 9, "identity": "STA_TREND", "dpPort": 2},
            ],
        )
        monkeypatch.setattr(generic_entities_module, "get_catalog_port_number", lambda model, model_code=None: 2)

        text = _coord_module._format_gate_diagnostics("FAKE_MODEL", None)

        assert "Readings with no verified definition yet: STA_ALARM, STA_TREND" in text
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
            """Stand in for the per-hub status fallback the coordinator tries next."""
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


class TestSilentSubDeviceEndToEnd:
    """Full-pipeline coverage for the status-less sub-device path (D-04..D-11)."""

    @pytest.mark.asyncio
    async def test_one_and_two_omitted_polls_yield_no_entry(self):
        """An addr omitted from an arrived status for fewer than 3 polls contributes nothing (D-04/D-07)."""
        coord, client = _make_coord()
        client.get_devices_by_hid.return_value = [_make_hub(mid=200)]
        client.get_multiple_device_status.return_value = [{"mid": 200, "subDeviceStatus": []}]

        result = await _run(coord)
        assert "100_200_1" not in result["sensors"]
        coord.data = result

        result = await _run(coord)
        assert "100_200_1" not in result["sensors"]

    @pytest.mark.asyncio
    async def test_third_consecutive_omission_yields_silent_entry(self):
        """The third consecutive arrived-but-omitted poll surfaces a "silent" entry (D-07/D-09)."""
        coord, client = _make_coord()
        client.get_devices_by_hid.return_value = [_make_hub(mid=200)]
        client.get_multiple_device_status.return_value = [{"mid": 200, "subDeviceStatus": []}]

        for _ in range(2):
            coord.data = await _run(coord)
        result = await _run(coord)

        entry = result["sensors"]["100_200_1"]
        assert entry["data"]["type"] == SILENT_DATA_TYPE
        assert entry["data"]["missed_polls"] == 3
        assert entry["data"]["silent_state"] == "never_reported"
        assert entry["raw_status"] == {}

    @pytest.mark.asyncio
    async def test_a_reading_resets_the_counter_so_a_later_omission_restarts_at_one(self):
        """Any reading for an addr resets its debounce counter to zero (D-07)."""
        coord, client = _make_coord()
        client.get_devices_by_hid.return_value = [_make_hub(mid=200)]
        client.get_multiple_device_status.return_value = [{"mid": 200, "subDeviceStatus": []}]

        for _ in range(2):
            coord.data = await _run(coord)
        assert coord._silent_poll_counts["100_200_1"] == 2

        client.get_multiple_device_status.return_value = _make_status()
        coord.data = await _run(coord)
        assert "100_200_1" not in coord._silent_poll_counts

        client.get_multiple_device_status.return_value = [{"mid": 200, "subDeviceStatus": []}]
        result = await _run(coord)
        assert coord._silent_poll_counts["100_200_1"] == 1
        assert "100_200_1" not in result["sensors"]

    @pytest.mark.asyncio
    async def test_hub_status_absent_yields_no_silent_entries_while_sibling_hub_decodes(self):
        """A hub whose status could not be obtained contributes zero silent entries
        for any of its children, while a sibling hub in the same poll still decodes
        its healthy children normally (D-06)."""
        coord, client = _make_coord()
        outage_hub = _make_hub(mid=301, model=MODEL_MOISTURE_SIMPLE)
        healthy_hub = _make_hub(mid=302, model=MODEL_MOISTURE_SIMPLE)
        client.get_devices_by_hid.return_value = [outage_hub, healthy_hub]
        client.get_multiple_device_status.side_effect = aiohttp.ClientError("boom")

        def per_hub(mid):
            """Stand in for the per-hub status fallback the coordinator tries next."""
            if mid == 301:
                raise aiohttp.ClientError("outage")
            return {"subDeviceStatus": [{"id": "D1", "value": _MOISTURE_SIMPLE_PAYLOAD}]}

        client.get_device_status.side_effect = per_hub

        result = await _run(coord)

        assert "100_301_1" not in result["sensors"]
        assert result["sensors"]["100_302_1"]["data"] is not None
        assert "100_301_1" not in coord._silent_poll_counts

    @pytest.mark.asyncio
    async def test_fallback_transport_error_records_status_absent(self):
        """A transport error in the per-hub fallback records STATUS_ABSENT, not an
        arrived-empty status, so the hub-outage distinction survives one level
        below _async_update_data (D-05/D-06)."""
        coord, client = _make_coord()
        hub = _make_hub(mid=301)
        client.get_device_status.side_effect = aiohttp.ClientError("boom")

        result = await _coord_module.RainPointCoordinator._fallback_per_hub_status(coord, [hub])

        assert result[301] is _coord_module.STATUS_ABSENT

    @pytest.mark.asyncio
    async def test_mid_omitted_from_successful_multi_status_is_filled_arrived_empty(self):
        """A mid the multipleDeviceStatus response simply did not mention is filled
        with an arrived-but-empty status, not treated as an outage, so its
        children still reach the silent debounce (D-05)."""
        coord, client = _make_coord()
        client.get_devices_by_hid.return_value = [_make_hub(mid=301)]
        # The response array is non-empty (truthy) but never mentions mid 301.
        client.get_multiple_device_status.return_value = [{"mid": 999, "subDeviceStatus": []}]

        for _ in range(2):
            coord.data = await _run(coord)
        result = await _run(coord)

        assert result["sensors"]["100_301_1"]["data"]["type"] == SILENT_DATA_TYPE

    def test_silent_data_type_is_not_unknown(self):
        """Trust boundary (D-10): the two type strings must never collapse into one.

        type == "unknown" is the admission ticket for build_generic_entities and
        generic_control._build_generic_entities; if "silent" ever equalled
        "unknown" a device the cloud reports nothing about would become eligible
        for a generic valve entity. The full gate-call assertion lands in plan 15-03.
        """
        assert SILENT_DATA_TYPE != "unknown"

    def test_silent_entry_raw_status_empty_dict_does_not_raise_sub_device_attributes(self):
        """A silent entry's raw_status={} is tolerated by the shared attribute
        reader; only the firmware attribute is returned, without raising (D-11)."""
        coordinator = types.SimpleNamespace(
            data={
                "sensors": {
                    "100_200_1": {
                        "firmware_version": "1.0",
                        "raw_status": {},
                        "data": {"type": SILENT_DATA_TYPE, "silent_state": "never_reported", "last_seen": None},
                    }
                }
            }
        )

        attrs = sub_device_attributes(coordinator, "100_200_1")

        assert attrs == {"firmware_version": "1.0", "hub_connected": None}


class TestBuildSilentSubdevice:
    """Direct-call tests for _build_silent_subdevice and _prune_silent_state."""

    @staticmethod
    def _hub_and_sub():
        """One hub record carrying a single sub-device at the given addr."""
        hub = _make_hub(mid=200)
        hub["hid"] = 100
        sub = hub["subDevices"][0]
        return hub, sub

    def test_below_threshold_returns_none(self):
        """One or two misses are absorbed as a transient, not surfaced."""
        coord, _ = _make_coord()
        hub, sub = self._hub_and_sub()

        result = _coord_module.RainPointCoordinator._build_silent_subdevice(coord, hub, 200, 1, sub, "100_200_1")

        assert result is None
        assert coord._silent_poll_counts["100_200_1"] == 1

    def test_never_reported_when_no_prior_entry(self):
        """No prior reading means the device has never been seen at all."""
        coord, _ = _make_coord()
        hub, sub = self._hub_and_sub()
        coord._silent_poll_counts["100_200_1"] = 2  # about to cross the threshold

        result = _coord_module.RainPointCoordinator._build_silent_subdevice(coord, hub, 200, 1, sub, "100_200_1")

        assert result["data"]["silent_state"] == "never_reported"
        assert result["data"]["last_seen"] is None
        assert result["data"]["missed_polls"] == 3
        assert result["raw_status"] == {}

    def test_stopped_reporting_when_prior_entry_had_a_reading(self):
        """A device that used to report gets the honest 'stopped' wording."""
        coord, _ = _make_coord()
        hub, sub = self._hub_and_sub()
        coord._silent_poll_counts["100_200_1"] = 2
        coord.data = {
            "sensors": {
                "100_200_1": {
                    "data": {"device_timestamp": "2026-01-01T00:00:00+00:00"},
                    "raw_status": {},
                }
            }
        }

        result = _coord_module.RainPointCoordinator._build_silent_subdevice(coord, hub, 200, 1, sub, "100_200_1")

        assert result["data"]["silent_state"] == "stopped_reporting"
        assert result["data"]["last_seen"] == "2026-01-01T00:00:00+00:00"

    def test_carried_last_seen_survives_a_second_silent_poll_unchanged(self):
        """last_seen must not drift forward while the device stays silent."""
        coord, _ = _make_coord()
        hub, sub = self._hub_and_sub()
        coord._silent_poll_counts["100_200_1"] = 2
        coord.data = {
            "sensors": {
                "100_200_1": {
                    "data": {"device_timestamp": "2026-01-01T00:00:00+00:00"},
                    "raw_status": {},
                }
            }
        }
        first = _coord_module.RainPointCoordinator._build_silent_subdevice(coord, hub, 200, 1, sub, "100_200_1")
        coord.data = {"sensors": {"100_200_1": first}}

        second = _coord_module.RainPointCoordinator._build_silent_subdevice(coord, hub, 200, 1, sub, "100_200_1")

        assert second["data"]["last_seen"] == "2026-01-01T00:00:00+00:00"
        assert second["data"]["missed_polls"] == 4

    def test_notify_unknown_model_not_called_on_silent_path(self):
        """The silent path bypasses _decode_one_subdevice entirely, so the
        unknown-model notification is never reachable from it (D-17)."""
        coord, _ = _make_coord()
        hub, sub = self._hub_and_sub()
        coord._silent_poll_counts["100_200_1"] = 2

        with patch.object(_coord_module.RainPointCoordinator, "_notify_unknown_model") as notify:
            _coord_module.RainPointCoordinator._build_silent_subdevice(coord, hub, 200, 1, sub, "100_200_1")

        notify.assert_not_called()


class TestHubConnectivity:
    """Tests for _read_hub_connectivity and hub_connected_flag."""

    def test_connected_value_one_yields_connected_state(self):
        """A `connected` entry with value '1' maps to the connected constant."""
        status = {"subDeviceStatus": [{"id": "connected", "value": "1"}]}
        record = _coord_module._read_hub_connectivity(status)
        assert record["state"] == _coord_module.HUB_CONNECTED

    def test_connected_value_zero_yields_disconnected_state(self):
        """A `connected` entry with value '0' maps to the disconnected constant."""
        status = {"subDeviceStatus": [{"id": "connected", "value": "0"}]}
        record = _coord_module._read_hub_connectivity(status)
        assert record["state"] == _coord_module.HUB_DISCONNECTED

    def test_absent_status_yields_unknown_with_no_other_fields(self):
        """STATUS_ABSENT never coerces to disconnected; it is unknown with no timestamp or raw value."""
        record = _coord_module._read_hub_connectivity(_coord_module.STATUS_ABSENT)
        assert record == {
            "state": _coord_module.HUB_CONNECTIVITY_UNKNOWN,
            "changed_at": None,
            "state_raw": None,
        }

    def test_no_connected_entry_in_arrived_status_yields_unknown(self):
        """An arrived status with no `connected` id at all is unknown, not disconnected."""
        status = {"subDeviceStatus": [{"id": "state", "value": "0,-52"}]}
        record = _coord_module._read_hub_connectivity(status)
        assert record["state"] == _coord_module.HUB_CONNECTIVITY_UNKNOWN

    def test_non_string_connected_value_yields_unknown(self):
        """The cloud's own framing is a string; an int value must not be coerced to a definite state."""
        status = {"subDeviceStatus": [{"id": "connected", "value": 1}]}
        record = _coord_module._read_hub_connectivity(status)
        assert record["state"] == _coord_module.HUB_CONNECTIVITY_UNKNOWN

    def test_changed_at_derived_from_connected_entry_time(self):
        """The connected entry's `time` reaches changed_at as an ISO-8601 UTC string."""
        status = {"subDeviceStatus": [{"id": "connected", "value": "0", "time": 1785464564888}]}
        record = _coord_module._read_hub_connectivity(status)
        assert record["changed_at"] == datetime.fromtimestamp(1785464564888 / 1000, tz=UTC).isoformat()

    def test_changed_at_none_when_connected_entry_has_no_time(self):
        """A `connected` entry with no time field leaves changed_at None rather than raising."""
        status = {"subDeviceStatus": [{"id": "connected", "value": "1"}]}
        record = _coord_module._read_hub_connectivity(status)
        assert record["changed_at"] is None

    def test_state_raw_carries_the_whole_string_undecoded(self):
        """The `state` id's whole value rides along unmodified, split into nothing."""
        status = {
            "subDeviceStatus": [
                {"id": "connected", "value": "1"},
                {"id": "state", "value": "0,-52"},
            ]
        }
        record = _coord_module._read_hub_connectivity(status)
        assert record["state_raw"] == "0,-52"

    def test_state_raw_none_when_no_state_entry(self):
        """A connected entry with no `state` entry leaves state_raw None."""
        status = {"subDeviceStatus": [{"id": "connected", "value": "1"}]}
        record = _coord_module._read_hub_connectivity(status)
        assert record["state_raw"] is None

    def test_non_dict_status_entry_is_skipped(self):
        """A malformed subDeviceStatus entry that is not a dict is skipped, not crashed on."""
        status = {"subDeviceStatus": [None, {"id": "connected", "value": "1"}]}
        record = _coord_module._read_hub_connectivity(status)
        assert record["state"] == _coord_module.HUB_CONNECTED

    def test_changed_at_none_for_explicit_none_time(self):
        """An explicit time=None is swallowed the same as a missing field."""
        status = {"subDeviceStatus": [{"id": "connected", "value": "1", "time": None}]}
        record = _coord_module._read_hub_connectivity(status)
        assert record["changed_at"] is None

    def test_changed_at_none_for_non_numeric_time(self):
        """A non-numeric time value cannot raise; it degrades to no timestamp."""
        status = {"subDeviceStatus": [{"id": "connected", "value": "1", "time": "not-a-number"}]}
        record = _coord_module._read_hub_connectivity(status)
        assert record["changed_at"] is None

    def test_changed_at_none_for_out_of_range_epoch(self):
        """An epoch far outside datetime's representable range degrades to no timestamp."""
        status = {"subDeviceStatus": [{"id": "connected", "value": "1", "time": 99999999999999999999}]}
        record = _coord_module._read_hub_connectivity(status)
        assert record["changed_at"] is None

    def test_hub_connected_flag_maps_tri_state(self):
        """hub_connected_flag maps the tri-state to True/False/None."""
        assert _coord_module.hub_connected_flag({"state": _coord_module.HUB_CONNECTED}) is True
        assert _coord_module.hub_connected_flag({"state": _coord_module.HUB_DISCONNECTED}) is False
        assert _coord_module.hub_connected_flag({"state": _coord_module.HUB_CONNECTIVITY_UNKNOWN}) is None

    def test_hub_connected_flag_none_or_empty_record(self):
        """A None record or an empty record both map to None, not an error."""
        assert _coord_module.hub_connected_flag(None) is None
        assert _coord_module.hub_connected_flag({}) is None


class TestHubConnectivityIntegration:
    """hub_connectivity on the coordinator's returned dict (fourth top-level key)."""

    @pytest.mark.asyncio
    async def test_hub_connectivity_present_for_real_hub(self):
        """A real hub's mid gets a shaped connectivity record on every poll."""
        coord, client = _make_coord()
        client.get_devices_by_hid.return_value = [_make_hub(hid=100, mid=200)]
        client.get_multiple_device_status.return_value = [{"mid": 200, "subDeviceStatus": [{"id": "connected", "value": "1"}]}]

        result = await _run(coord)

        assert result["hub_connectivity"][200]["state"] == _coord_module.HUB_CONNECTED

    @pytest.mark.asyncio
    async def test_hub_connectivity_absent_hub_yields_unknown(self):
        """A hub whose status could not be obtained this poll reads unknown, never disconnected."""
        coord, client = _make_coord()
        client.get_devices_by_hid.return_value = [_make_hub(mid=301)]
        client.get_device_status.side_effect = aiohttp.ClientError("boom")
        client.get_multiple_device_status.side_effect = aiohttp.ClientError("boom")

        result = await _run(coord)

        assert result["hub_connectivity"][301]["state"] == _coord_module.HUB_CONNECTIVITY_UNKNOWN

    @pytest.mark.asyncio
    async def test_bluetooth_wrapper_record_contributes_no_hub_connectivity_entry(self):
        """A record whose identity fields are all empty strings gets no connectivity
        record at all, not a falsy one -- the mid is simply absent from the dict."""
        coord, client = _make_coord()
        wrapper_hub = {
            "hid": 100,
            "mid": 346965,
            "did": "",
            "mac": "",
            "productKey": "",
            "model": "",
            "name": "",
            "subDevices": [{"addr": 1, "model": "HTV210B", "name": "BT Valve", "softVer": "1.0"}],
        }
        client.get_devices_by_hid.return_value = [wrapper_hub]
        client.get_multiple_device_status.return_value = [{"mid": 346965, "subDeviceStatus": [{"id": "connected", "value": "1"}]}]

        result = await _run(coord)

        assert 346965 not in result["hub_connectivity"]

    @pytest.mark.asyncio
    async def test_multi_status_and_fallback_paths_produce_identical_hub_connectivity(self):
        """Both status-fetch paths must shape the identical hub_connectivity record
        for the same underlying payload, including the connected value and timestamp."""
        raw_status = [{"id": "connected", "value": "0", "time": 1785464564888}]

        coord1, client1 = _make_coord()
        client1.get_devices_by_hid.return_value = [_make_hub(mid=200)]
        client1.get_multiple_device_status.return_value = [{"mid": 200, "subDeviceStatus": raw_status}]
        result1 = await _run(coord1)

        coord2, client2 = _make_coord()
        client2.get_devices_by_hid.return_value = [_make_hub(mid=200)]
        client2.get_multiple_device_status.side_effect = aiohttp.ClientError("boom")
        client2.get_device_status.return_value = {"subDeviceStatus": raw_status}
        result2 = await _run(coord2)

        assert result1["hub_connectivity"] == result2["hub_connectivity"]


class TestPruneSilentState:
    """Direct-call tests for _prune_silent_state (T-15-03)."""

    def test_drops_counter_for_a_device_no_longer_listed(self):
        """A device that leaves the hub must not keep a debounce counter alive."""
        coord, _ = _make_coord()
        coord._silent_poll_counts = {"100_200_1": 2, "100_200_2": 1}
        hub = _make_hub(mid=200)
        hub["hid"] = 100

        _coord_module.RainPointCoordinator._prune_silent_state(coord, [hub])

        assert coord._silent_poll_counts == {"100_200_1": 2}

    def test_keeps_counters_for_devices_still_listed_across_multiple_hubs(self):
        """Pruning one hub's departed device must not disturb another hub's."""
        coord, _ = _make_coord()
        coord._silent_poll_counts = {"100_200_1": 1, "100_300_5": 2}
        hub1 = _make_hub(mid=200)
        hub1["hid"] = 100
        hub2 = {"hid": 100, "mid": 300, "subDevices": [{"addr": 5, "model": MODEL_MOISTURE_SIMPLE}]}

        _coord_module.RainPointCoordinator._prune_silent_state(coord, [hub1, hub2])

        assert coord._silent_poll_counts == {"100_200_1": 1, "100_300_5": 2}


class TestPruneHubConnectivityState:
    """Direct-call tests for _prune_hub_connectivity_state."""

    def test_drops_counter_for_a_hub_no_longer_listed(self):
        """A hub that leaves the device list must not keep a debounce counter alive."""
        coord, _ = _make_coord()
        coord._hub_disconnect_poll_counts = {(100, 200): 2, (100, 300): 1}
        hub = _make_hub(mid=200)
        hub["hid"] = 100

        _coord_module.RainPointCoordinator._prune_hub_connectivity_state(coord, [hub])

        assert coord._hub_disconnect_poll_counts == {(100, 200): 2}

    def test_keeps_counters_for_hubs_still_listed(self):
        """Pruning one hub's departure must not disturb another hub's counter."""
        coord, _ = _make_coord()
        coord._hub_disconnect_poll_counts = {(100, 200): 1, (100, 300): 2}
        hub1 = _make_hub(mid=200)
        hub1["hid"] = 100
        hub2 = _make_hub(mid=300)
        hub2["hid"] = 100

        _coord_module.RainPointCoordinator._prune_hub_connectivity_state(coord, [hub1, hub2])

        assert coord._hub_disconnect_poll_counts == {(100, 200): 1, (100, 300): 2}

    def test_bluetooth_wrapper_record_is_never_treated_as_a_live_key(self):
        """A stray counter under a wrapper's key (which should never exist in
        practice) must not be kept alive by pruning either."""
        coord, _ = _make_coord()
        coord._hub_disconnect_poll_counts = {(100, 346965): 2}
        wrapper_hub = {
            "hid": 100,
            "mid": 346965,
            "did": "",
            "mac": "",
            "productKey": "",
            "model": "",
            "name": "",
            "subDevices": [{"addr": 1, "model": "HTV210B", "name": "BT Valve", "softVer": "1.0"}],
        }

        _coord_module.RainPointCoordinator._prune_hub_connectivity_state(coord, [wrapper_hub])

        assert coord._hub_disconnect_poll_counts == {}


class TestSyncHubConnectivityIssues:
    """Direct-call tests for _sync_hub_connectivity_issues: one HubConnectivityRecord
    per real hub, translated correctly from the coordinator's own tri-state shape."""

    def test_connected_hub_emits_a_non_disconnected_record_and_resets_the_counter(self):
        """Recovery clears the counter back to zero, proven directly rather than
        only through the reconcile call it feeds."""
        coord, _ = _make_coord()
        coord._hub_disconnect_poll_counts = {(100, 200): 2}
        hub = _make_hub(mid=200)
        hub["hid"] = 100
        hub_connectivity = {200: {"state": _coord_module.HUB_CONNECTED}}

        _coord_module.RainPointCoordinator._sync_hub_connectivity_issues(coord, [hub], hub_connectivity)

        coord._hub_connectivity_issues.async_sync.assert_called_once()
        (records,) = coord._hub_connectivity_issues.async_sync.call_args.args
        assert len(records) == 1
        record = records[0]
        assert record.hid == 100
        assert record.mid == 200
        assert record.disconnected is False
        assert record.missed_polls == 0
        assert (100, 200) not in coord._hub_disconnect_poll_counts

    def test_disconnected_hub_below_threshold_increments_but_emits_no_record(self):
        """One or two consecutive disconnected polls say nothing either way.

        The counter still advances, but no record reaches the reconcile: a
        below-threshold poll must not emit a non-disconnected record, because
        that is indistinguishable from a confirmed-connected one by the time
        it reaches the unconditional clear in repairs.py. The id goes into
        unreachable_ids instead, so the poll neither raises nor clears.
        """
        coord, _ = _make_coord()
        hub = _make_hub(mid=200)
        hub["hid"] = 100
        hub_connectivity = {200: {"state": _coord_module.HUB_DISCONNECTED}}

        _coord_module.RainPointCoordinator._sync_hub_connectivity_issues(coord, [hub], hub_connectivity)

        assert coord._hub_disconnect_poll_counts[(100, 200)] == 1
        (records,), kwargs = (
            coord._hub_connectivity_issues.async_sync.call_args.args,
            coord._hub_connectivity_issues.async_sync.call_args.kwargs,
        )
        assert records == []
        assert kwargs["unreachable_ids"] == {hub_connectivity_issue_id(100, 200)}

    def test_disconnected_hub_at_threshold_flags_disconnected(self):
        """The third consecutive disconnected poll is where the flag flips true."""
        coord, _ = _make_coord()
        coord._hub_disconnect_poll_counts = {(100, 200): _coord_module.HUB_DISCONNECT_DEBOUNCE_POLLS - 1}
        hub = _make_hub(mid=200)
        hub["hid"] = 100
        hub_connectivity = {200: {"state": _coord_module.HUB_DISCONNECTED}}

        _coord_module.RainPointCoordinator._sync_hub_connectivity_issues(coord, [hub], hub_connectivity)

        (records,) = coord._hub_connectivity_issues.async_sync.call_args.args
        assert records[0].disconnected is True
        assert records[0].missed_polls == _coord_module.HUB_DISCONNECT_DEBOUNCE_POLLS

    def test_hub_model_rides_the_record_to_the_repairs_card(self):
        """The card names the model, so it has to survive the translation step.

        Read from the hub record's own "model" key, the same field the hub's
        DeviceInfo carries, so the card and the device page cannot disagree.
        """
        coord, _ = _make_coord()
        coord._hub_disconnect_poll_counts = {(100, 200): _coord_module.HUB_DISCONNECT_DEBOUNCE_POLLS - 1}
        hub = _make_hub(mid=200)
        hub["hid"] = 100
        hub["model"] = "HWG023WBRF-V2"
        hub_connectivity = {200: {"state": _coord_module.HUB_DISCONNECTED}}

        _coord_module.RainPointCoordinator._sync_hub_connectivity_issues(coord, [hub], hub_connectivity)

        (records,) = coord._hub_connectivity_issues.async_sync.call_args.args
        assert records[0].model == "HWG023WBRF-V2"

    def test_absent_hub_model_reaches_the_record_as_none_not_empty_string(self):
        """An empty model must arrive as None so the sanitizer's fallback fires.

        An empty string would pass straight through and render blank
        parentheses on the card.
        """
        coord, _ = _make_coord()
        coord._hub_disconnect_poll_counts = {(100, 200): _coord_module.HUB_DISCONNECT_DEBOUNCE_POLLS - 1}
        hub = _make_hub(mid=200)
        hub["hid"] = 100
        hub["model"] = ""
        hub_connectivity = {200: {"state": _coord_module.HUB_DISCONNECTED}}

        _coord_module.RainPointCoordinator._sync_hub_connectivity_issues(coord, [hub], hub_connectivity)

        (records,) = coord._hub_connectivity_issues.async_sync.call_args.args
        assert records[0].model is None

    def test_unknown_state_emits_no_record_and_leaves_the_counter_untouched(self):
        """An unknown state is not evidence about the hub in either direction."""
        coord, _ = _make_coord()
        coord._hub_disconnect_poll_counts = {(100, 200): 2}
        hub = _make_hub(mid=200)
        hub["hid"] = 100
        hub_connectivity = {200: {"state": _coord_module.HUB_CONNECTIVITY_UNKNOWN}}

        _coord_module.RainPointCoordinator._sync_hub_connectivity_issues(coord, [hub], hub_connectivity)

        assert coord._hub_disconnect_poll_counts == {(100, 200): 2}
        (records,), kwargs = (
            coord._hub_connectivity_issues.async_sync.call_args.args,
            coord._hub_connectivity_issues.async_sync.call_args.kwargs,
        )
        assert records == []
        assert kwargs["unreachable_ids"] == {hub_connectivity_issue_id(100, 200)}

    def test_missing_hub_connectivity_entry_is_treated_as_unknown(self):
        """A real hub whose mid is altogether absent from hub_connectivity must
        not be coerced to a definite state."""
        coord, _ = _make_coord()
        hub = _make_hub(mid=200)
        hub["hid"] = 100

        _coord_module.RainPointCoordinator._sync_hub_connectivity_issues(coord, [hub], {})

        assert coord._hub_disconnect_poll_counts == {}
        (records,) = coord._hub_connectivity_issues.async_sync.call_args.args
        kwargs = coord._hub_connectivity_issues.async_sync.call_args.kwargs
        assert records == []
        assert kwargs["unreachable_ids"] == {hub_connectivity_issue_id(100, 200)}

    def test_bluetooth_wrapper_record_contributes_no_record_and_no_counter(self):
        """The Bluetooth wrapper carries no cloud connection to report on."""
        coord, _ = _make_coord()
        wrapper_hub = {
            "hid": 100,
            "mid": 346965,
            "did": "",
            "mac": "",
            "productKey": "",
            "model": "",
            "name": "",
            "subDevices": [{"addr": 1, "model": "HTV210B", "name": "BT Valve", "softVer": "1.0"}],
        }
        hub_connectivity = {346965: {"state": _coord_module.HUB_DISCONNECTED}}

        _coord_module.RainPointCoordinator._sync_hub_connectivity_issues(coord, [wrapper_hub], hub_connectivity)

        assert coord._hub_disconnect_poll_counts == {}
        (records,) = coord._hub_connectivity_issues.async_sync.call_args.args
        assert records == []

    def test_empty_hub_name_is_treated_as_absent_not_an_empty_string(self):
        """The cloud returns an empty string rather than omitting the field; the
        sanitizer's "unknown" fallback should fire, not render a blank."""
        coord, _ = _make_coord()
        hub = _make_hub(mid=200)
        hub["hid"] = 100
        hub["name"] = ""
        hub_connectivity = {200: {"state": _coord_module.HUB_CONNECTED}}

        _coord_module.RainPointCoordinator._sync_hub_connectivity_issues(coord, [hub], hub_connectivity)

        (records,) = coord._hub_connectivity_issues.async_sync.call_args.args
        assert records[0].hub_name is None


class TestSyncSilentDeviceIssues:
    """Direct-call tests for _sync_silent_device_issues: one SilentDeviceRecord
    per sensor entry, translated correctly from the coordinator's own shape."""

    def test_builds_one_record_per_entry_with_correct_silent_flag_and_missed_polls(self):
        """The coordinator-to-repairs translation carries the whole poll."""
        coord, _ = _make_coord()
        decoded_sensors = {
            "100_200_1": {
                "hid": 100,
                "mid": 200,
                "addr": 1,
                "model": "HTV210B",
                "hub_name": "Hub1",
                "data": {"type": SILENT_DATA_TYPE, "missed_polls": 3},
            },
            "100_200_2": {
                "hid": 100,
                "mid": 200,
                "addr": 2,
                "model": MODEL_MOISTURE_SIMPLE,
                "hub_name": "Hub1",
                "data": {"type": "moisture"},
            },
        }

        _coord_module.RainPointCoordinator._sync_silent_device_issues(coord, decoded_sensors, [])

        coord._silent_issues.async_sync.assert_called_once()
        (records,) = coord._silent_issues.async_sync.call_args.args
        by_key = {(r.hid, r.mid, r.addr): r for r in records}

        silent_record = by_key[(100, 200, 1)]
        assert silent_record.silent is True
        assert silent_record.missed_polls == 3
        assert silent_record.model == "HTV210B"
        assert silent_record.hub_name == "Hub1"

        reporting_record = by_key[(100, 200, 2)]
        assert reporting_record.silent is False
        assert reporting_record.missed_polls == 0

    def test_hub_paired_is_false_only_for_the_placeholder_parent_record(self):
        """The record must say whether a hub exists, not just what it is called.

        The cloud parks a Bluetooth-only device under a parent carrying no
        product_key and no device_name, alongside an empty name. A real hub
        always carries both. Without this flag the Repairs card cannot tell
        that apart from a hub whose name is simply missing, and renders both
        as "unknown".
        """
        coord, _ = _make_coord()
        decoded_sensors = {
            "100_346965_1": {
                "hid": 100,
                "mid": 346965,
                "addr": 1,
                "model": "HTV210B",
                "hub_name": "",
                "product_key": "",
                "device_name": "",
                "data": {"type": SILENT_DATA_TYPE, "missed_polls": 3},
            },
            "100_200_2": {
                "hid": 100,
                "mid": 200,
                "addr": 2,
                "model": MODEL_MOISTURE_SIMPLE,
                "hub_name": "Hub1",
                "product_key": "a3QrDxYPTM2",
                "device_name": "MAC-A84674BB91F0",
                "data": {"type": "moisture"},
            },
        }

        _coord_module.RainPointCoordinator._sync_silent_device_issues(coord, decoded_sensors, [])

        (records,) = coord._silent_issues.async_sync.call_args.args
        by_key = {(r.hid, r.mid, r.addr): r for r in records}
        assert by_key[(100, 346965, 1)].hub_paired is False
        assert by_key[(100, 200, 2)].hub_paired is True

    def test_empty_decoded_sensors_syncs_an_empty_record_list(self):
        """A poll that decoded nothing still reconciles, so stale issues clear."""
        coord, _ = _make_coord()

        _coord_module.RainPointCoordinator._sync_silent_device_issues(coord, {}, [])

        coord._silent_issues.async_sync.assert_called_once_with([], unreachable_ids=set())

    def test_does_not_call_notify_unknown_model(self):
        """D-17: the silent path adds no call site for the unknown-model notification."""
        coord, _ = _make_coord()
        decoded_sensors = {
            "100_200_1": {
                "hid": 100,
                "mid": 200,
                "addr": 1,
                "model": "HTV210B",
                "hub_name": "Hub1",
                "data": {"type": SILENT_DATA_TYPE, "missed_polls": 3},
            }
        }
        coord._notify_unknown_model = MagicMock()

        _coord_module.RainPointCoordinator._sync_silent_device_issues(coord, decoded_sensors, [])

        coord._notify_unknown_model.assert_not_called()

    def test_an_absent_hub_contributes_every_one_of_its_children_as_unreachable(self):
        """Two children, so the comprehension is proven to cover more than the first."""
        coord, _ = _make_coord()
        absent_hub = {
            "hid": 100,
            "mid": 200,
            "subDevices": [{"addr": 1}, {"addr": 2}],
        }

        _coord_module.RainPointCoordinator._sync_silent_device_issues(coord, {}, [absent_hub])

        _records, kwargs = coord._silent_issues.async_sync.call_args
        assert kwargs["unreachable_ids"] == {
            silent_device_issue_id(100, 200, 1),
            silent_device_issue_id(100, 200, 2),
        }


class TestSilentIssueSurvivesHubOutage:
    """An active not-reporting issue must survive a poll in which its hub was unreachable.

    Drives the real poll sequence through _async_update_data against a real
    RainPointSilentDeviceIssues rather than pre-seeding an active set, because
    the defect was that a routine transport failure looked identical to a
    device leaving the hub's sub-device list, producing a clear-then-reraise
    cycle that broke the raised-once guarantee.
    """

    ISSUE_ID = silent_device_issue_id(100, 200, 1)

    @staticmethod
    def _hub(addrs=(1,)):
        """A hub record listing the given addrs as children."""
        return {
            "mid": 200,
            "name": "Hub1",
            "deviceName": "dev1",
            "productKey": "pk1",
            "homeName": "Home",
            "subDevices": [{"addr": addr, "model": "HTV210B", "name": f"Sub{addr}", "softVer": "1.0"} for addr in addrs],
        }

    @staticmethod
    def _arrived_empty():
        """A status response that arrived and named nobody."""
        return [{"mid": 200, "subDeviceStatus": []}]

    def _build(self):
        """Return (coord, client) with a real issue manager and a silent child."""
        coord, client = _make_coord()
        client.get_devices_by_hid.return_value = [self._hub()]
        client.get_multiple_device_status.return_value = self._arrived_empty()
        coord._silent_issues = RainPointSilentDeviceIssues(MagicMock())
        return coord, client

    @staticmethod
    def _go_unreachable(client):
        """Make both the batch call and the per-hub fallback fail at the transport layer."""
        client.get_multiple_device_status.side_effect = aiohttp.ClientError("boom")
        client.get_device_status.side_effect = aiohttp.ClientError("boom")

    def _restore(self, client):
        """Undo _go_unreachable."""
        client.get_multiple_device_status.side_effect = None
        client.get_device_status.side_effect = None
        client.get_multiple_device_status.return_value = self._arrived_empty()

    @pytest.mark.asyncio
    async def test_outage_poll_neither_clears_nor_reraises_the_issue(self):
        """The regression this fix exists for: an outage is not evidence about a device."""
        coord, client = self._build()

        with (
            patch.object(_repairs_module.ir, "async_create_issue") as create,
            patch.object(_repairs_module.ir, "async_delete_issue") as delete,
        ):
            for _ in range(3):
                await _run(coord)
            assert create.call_count == 1

            self._go_unreachable(client)
            await _run(coord)

            assert delete.call_count == 0
            assert self.ISSUE_ID in coord._silent_issues._active

            self._restore(client)
            await _run(coord)

            # The raised-once guarantee itself, not a proxy for it.
            assert create.call_count == 1
            assert delete.call_count == 0

    @pytest.mark.asyncio
    async def test_an_empty_device_list_is_an_outage_not_a_mass_removal(self):
        """The same clear-then-reraise cycle, entering by the device-list door.

        getDeviceByHid answering code 0 with an empty data array drops every
        hub, which would otherwise wipe each debounce counter and let the stale
        sweep reap a still-valid issue, then re-raise it once the list came
        back. An installation that had devices a moment ago did not lose all of
        them at once.
        """
        coord, client = self._build()

        with (
            patch.object(_repairs_module.ir, "async_create_issue") as create,
            patch.object(_repairs_module.ir, "async_delete_issue") as delete,
        ):
            for _ in range(3):
                await _run(coord)
            assert create.call_count == 1

            client.get_devices_by_hid.return_value = []
            await _run(coord)

            assert delete.call_count == 0
            assert self.ISSUE_ID in coord._silent_issues._active
            # The counter has to survive too, or the device restarts its
            # debounce from zero and goes quiet on the UI for three more polls.
            assert coord._silent_poll_counts

            client.get_devices_by_hid.return_value = [self._hub()]
            await _run(coord)

            assert create.call_count == 1
            assert delete.call_count == 0

    @pytest.mark.asyncio
    async def test_an_account_with_nothing_tracked_still_reconciles_on_an_empty_list(self):
        """The skip is conditional on there being state to protect.

        A genuinely empty installation must keep reconciling, or a first poll
        that legitimately returns nothing would stop the sweep from ever
        running.
        """
        coord, client = self._build()
        client.get_devices_by_hid.return_value = []

        with patch.object(_repairs_module.ir, "async_delete_issue"):
            await _run(coord)

        assert coord._silent_poll_counts == {}

    @pytest.mark.asyncio
    async def test_a_genuine_removal_after_an_outage_still_clears(self):
        """The skip is scoped to the outage poll, so removal still reaps the issue."""
        coord, client = self._build()

        with (
            patch.object(_repairs_module.ir, "async_create_issue") as create,
            patch.object(_repairs_module.ir, "async_delete_issue") as delete,
        ):
            for _ in range(3):
                await _run(coord)
            assert create.call_count == 1

            self._go_unreachable(client)
            await _run(coord)
            assert delete.call_count == 0

            self._restore(client)
            client.get_devices_by_hid.return_value = [self._hub(addrs=())]
            await _run(coord)

            assert delete.call_count == 1
            _hass, _domain, issue_id = delete.call_args.args
            assert issue_id == self.ISSUE_ID


class TestHubConnectivitySurvivesDeviceListOutage:
    """An active hub-connectivity issue and its debounce counter must survive a
    poll in which the device list itself came back empty, mirroring
    TestSilentIssueSurvivesHubOutage's device-list-door regression for the
    not-reporting lifecycle."""

    HUB_ISSUE_ID = hub_connectivity_issue_id(100, 200)

    @staticmethod
    def _hub():
        """A single real hub with no sub-devices, hid injected by _collect_hubs
        from _hids. No subDevices keeps the not-reporting lifecycle, which
        shares the same ir.async_create_issue/async_delete_issue mocks, from
        also firing and confusing the call-count assertions below."""
        return {
            "mid": 200,
            "name": "Hub1",
            "deviceName": "dev1",
            "productKey": "pk1",
            "homeName": "Home",
            "subDevices": [],
        }

    def _build(self):
        """Return (coord, client) with a real hub-connectivity issue manager."""
        coord, client = _make_coord()
        client.get_devices_by_hid.return_value = [self._hub()]
        client.get_multiple_device_status.return_value = [{"mid": 200, "subDeviceStatus": [{"id": "connected", "value": "0"}]}]
        coord._hub_connectivity_issues = _repairs_module.RainPointHubConnectivityIssues(MagicMock())
        return coord, client

    @pytest.mark.asyncio
    async def test_an_empty_device_list_leaves_the_counter_and_issue_untouched(self):
        """getDeviceByHid answering with an empty data array must not wipe the
        debounce counter or let the stale sweep reap a still-valid issue.

        Below-threshold disconnected polls call the idempotent clear
        unconditionally (mirroring the not-reporting lifecycle), so the
        assertion is a delta across the outage poll rather than an absolute
        zero: the outage poll itself must add no further delete calls.
        """
        coord, client = self._build()

        with (
            patch.object(_repairs_module.ir, "async_create_issue") as create,
            patch.object(_repairs_module.ir, "async_delete_issue") as delete,
        ):
            for _ in range(3):
                await _run(coord)
            assert create.call_count == 1
            deletes_before_outage = delete.call_count

            client.get_devices_by_hid.return_value = []
            await _run(coord)

            assert delete.call_count == deletes_before_outage
            assert self.HUB_ISSUE_ID in coord._hub_connectivity_issues._active
            # The counter has to survive too, or the hub restarts its debounce
            # from zero and the card disappears for three more polls.
            assert coord._hub_disconnect_poll_counts

            client.get_devices_by_hid.return_value = [self._hub()]
            await _run(coord)

            assert create.call_count == 1
            assert delete.call_count == deletes_before_outage


class TestHubConnectivityDebounceRealTimeline:
    """Drives the real coordinator construct -> first refresh -> repeated
    refresh sequence, asserting between every step, rather than proving the
    debounce from an injected already-past-threshold coordinator.data
    snapshot -- the specific pattern that shipped two critical defects under
    100% branch coverage in a prior phase."""

    @staticmethod
    def _build(connected_value="1"):
        """Return (coordinator, client) wired the way __init__.py wires it.

        The hub carries no subDevices so the not-reporting lifecycle -- which
        shares the same ir.async_create_issue/async_delete_issue mocks --
        never fires and confuses this class's call-count assertions.
        """
        client = AsyncMock()
        client.get_devices_by_hid.return_value = [
            {
                "mid": 200,
                "name": "Hub1",
                "deviceName": "dev1",
                "productKey": "pk1",
                "homeName": "Home",
                "subDevices": [],
            }
        ]
        client.get_multiple_device_status.return_value = [
            {"mid": 200, "subDeviceStatus": [{"id": "connected", "value": connected_value}]}
        ]

        entry = MagicMock()
        entry.entry_id = "test_entry"
        entry.data = {CONF_HIDS: [100]}

        hass = MagicMock()
        hass.data = {}

        coordinator = _coord_module.RainPointCoordinator(hass, client, entry)
        return coordinator, client

    @staticmethod
    def _set_connected(client, value):
        """Mutate the next poll's connected value on an already-built client."""
        client.get_multiple_device_status.return_value = [{"mid": 200, "subDeviceStatus": [{"id": "connected", "value": value}]}]

    @pytest.mark.asyncio
    async def test_three_consecutive_disconnected_polls_raise_exactly_one_issue(self):
        """The full debounce lifecycle: raise once at the threshold, no second
        raise while it stays down, clear and reset on recovery, and prove the
        reset is real by crossing the threshold again from zero."""
        coordinator, client = self._build(connected_value="1")

        with (
            patch.object(_repairs_module.ir, "async_create_issue") as create,
            patch.object(_repairs_module.ir, "async_delete_issue") as delete,
        ):
            await coordinator.async_config_entry_first_refresh()
            assert create.call_count == 0

            self._set_connected(client, "0")
            await coordinator.async_refresh()  # poll 1 disconnected: counter 1
            assert coordinator._hub_disconnect_poll_counts[(100, 200)] == 1
            assert create.call_count == 0

            await coordinator.async_refresh()  # poll 2 disconnected: counter 2
            assert coordinator._hub_disconnect_poll_counts[(100, 200)] == 2
            assert create.call_count == 0

            await coordinator.async_refresh()  # poll 3 disconnected: counter 3 -> raise
            assert coordinator._hub_disconnect_poll_counts[(100, 200)] == 3
            assert create.call_count == 1
            _hass, _domain, issue_id = create.call_args.args
            assert issue_id == hub_connectivity_issue_id(100, 200)

            await coordinator.async_refresh()  # poll 4 disconnected: no second raise
            assert create.call_count == 1

            self._set_connected(client, "1")
            deletes_before_recovery = delete.call_count
            await coordinator.async_refresh()  # poll 5 connected: clears and resets
            assert delete.call_count > deletes_before_recovery
            assert create.call_count == 1
            assert (100, 200) not in coordinator._hub_disconnect_poll_counts

            self._set_connected(client, "0")
            await coordinator.async_refresh()  # poll 6 disconnected: counter restarts at 1
            assert coordinator._hub_disconnect_poll_counts[(100, 200)] == 1
            assert create.call_count == 1

    @pytest.mark.asyncio
    async def test_unknown_poll_neither_raises_nor_advances_the_counter(self):
        """A poll whose connected id is missing entirely is not evidence about
        the hub in either direction, driven across a real refresh sequence."""
        coordinator, client = self._build(connected_value="1")

        with patch.object(_repairs_module.ir, "async_create_issue") as create:
            await coordinator.async_config_entry_first_refresh()

            self._set_connected(client, "0")
            await coordinator.async_refresh()
            await coordinator.async_refresh()
            assert coordinator._hub_disconnect_poll_counts[(100, 200)] == 2
            assert create.call_count == 0

            client.get_multiple_device_status.return_value = [{"mid": 200, "subDeviceStatus": []}]
            await coordinator.async_refresh()  # unknown: counter must not move
            assert coordinator._hub_disconnect_poll_counts[(100, 200)] == 2
            assert create.call_count == 0

            self._set_connected(client, "0")
            await coordinator.async_refresh()  # counter resumes from where it left off
            assert coordinator._hub_disconnect_poll_counts[(100, 200)] == 3
            assert create.call_count == 1

    @pytest.mark.asyncio
    async def test_restart_while_still_disconnected_does_not_clear_the_issue(self):
        """A reload or restart mid-outage must not delete a still-accurate card.

        The debounce counter and the manager's active set are both
        per-instance, so a second coordinator built while the hub is still
        down starts counting from one again. If those below-threshold polls
        emitted a non-disconnected record, the unconditional clear in
        repairs.py could not tell them apart from a genuine recovery and would
        delete a card describing an outage that is still happening, leaving
        the user with no notice until the new instance re-crossed the
        threshold two polls later.

        Driven as a real timeline across two coordinator instances sharing one
        pair of issue-registry mocks, because the defect only exists in the
        seam between them and is invisible to any single-instance test.
        """
        first, first_client = self._build(connected_value="1")

        with (
            patch.object(_repairs_module.ir, "async_create_issue") as create,
            patch.object(_repairs_module.ir, "async_delete_issue") as delete,
        ):
            await first.async_config_entry_first_refresh()
            self._set_connected(first_client, "0")
            for _ in range(_coord_module.HUB_DISCONNECT_DEBOUNCE_POLLS):
                await first.async_refresh()
            assert create.call_count == 1
            deletes_before_restart = delete.call_count

            # The restart: a brand new coordinator, hub still reporting "0".
            second, second_client = self._build(connected_value="0")
            assert second._hub_disconnect_poll_counts == {}

            await second.async_config_entry_first_refresh()
            assert second._hub_disconnect_poll_counts[(100, 200)] == 1
            assert delete.call_count == deletes_before_restart

            await second.async_refresh()
            assert second._hub_disconnect_poll_counts[(100, 200)] == 2
            assert delete.call_count == deletes_before_restart

            # Once the new instance confirms the outage itself it re-asserts
            # the issue rather than having spent the gap with the card gone.
            await second.async_refresh()
            assert second._hub_disconnect_poll_counts[(100, 200)] == _coord_module.HUB_DISCONNECT_DEBOUNCE_POLLS
            assert delete.call_count == deletes_before_restart
            assert create.call_count == 2

            # Recovery still clears, so suppressing the clear below threshold
            # did not cost the only path that legitimately removes the card.
            self._set_connected(second_client, "1")
            await second.async_refresh()
            assert delete.call_count > deletes_before_restart
            assert (100, 200) not in second._hub_disconnect_poll_counts


class TestLastSeenFromEntry:
    """Direct-call tests covering every resolution path of _last_seen_from_entry."""

    def test_none_previous_returns_none(self):
        """Nothing known means nothing to carry forward."""
        assert _coord_module._last_seen_from_entry(None) is None

    def test_previous_silent_entry_carries_last_seen_forward(self):
        """The carried timestamp is the whole point of distinguishing the two silent states."""
        previous = {"data": {"type": SILENT_DATA_TYPE, "last_seen": "2026-01-01T00:00:00+00:00"}}
        assert _coord_module._last_seen_from_entry(previous) == "2026-01-01T00:00:00+00:00"

    def test_previous_real_entry_uses_device_timestamp(self):
        """The device's own clock is preferred over the server's."""
        previous = {"data": {"device_timestamp": "2026-02-01T00:00:00+00:00"}, "raw_status": {"time": 1}}
        assert _coord_module._last_seen_from_entry(previous) == "2026-02-01T00:00:00+00:00"

    def test_previous_real_entry_falls_back_to_raw_status_time(self):
        """Without a device timestamp the status time is the best available."""
        previous = {"data": {}, "raw_status": {"time": 1700000000000}}
        expected = datetime.fromtimestamp(1700000000000 / 1000, tz=UTC).isoformat()
        assert _coord_module._last_seen_from_entry(previous) == expected

    def test_previous_entry_with_nothing_usable_returns_none(self):
        """An entry with no timestamp at all must not invent one."""
        previous = {"data": {}, "raw_status": {}}
        assert _coord_module._last_seen_from_entry(previous) is None


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

    def test_no_registered_decoder_delegates_to_decode_unknown(self):
        """A registered model is a claim of real support, so no decoder may be a
        passthrough to decode_unknown.

        Registering a stub is worse than registering nothing: the model lands in
        HAND_WRITTEN_MODELS, which makes is_hand_written_model keep it out of both
        generic paths, so its owner gets neither a real decode nor the opt-in
        catalog-driven one. Unregistered models already fall through to the
        unknown-model notification with the pre-filled report link.
        """
        import ast
        from pathlib import Path

        source = Path(_coord_module.__file__).parent / "api" / "decoders.py"
        tree = ast.parse(source.read_text())
        passthrough = set()
        for node in tree.body:
            if not isinstance(node, ast.FunctionDef):
                continue
            body = [n for n in node.body if not (isinstance(n, ast.Expr) and isinstance(n.value, ast.Constant))]
            if (
                len(body) == 1
                and isinstance(body[0], ast.Return)
                and isinstance(body[0].value, ast.Call)
                and isinstance(body[0].value.func, ast.Name)
                and body[0].value.func.id == "decode_unknown"
            ):
                passthrough.add(node.name)

        offenders = {model: fn.__name__ for model, fn in DECODER_REGISTRY.items() if fn.__name__ in passthrough}
        assert not offenders, f"registered models decode nothing: {offenders}"

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

    @pytest.mark.parametrize(
        ("reason", "mid", "sid", "seed"),
        [
            ("before first poll", 200, "D1", False),
            ("unknown mid", 999, "D1", True),
            ("unresolvable sid", 200, "state", True),
            ("unknown addr", 200, "D9", True),
        ],
    )
    def test_every_drop_path_logs_the_raw_value(self, caplog, reason, mid, sid, seed):
        """A dropped push must leave its payload in the log.

        A device paired between two polls pushes against a sub-device map that
        does not list it yet, so its first frames are dropped. Those frames are
        the ones worth having when the model has no decoder, and they used to be
        discarded without ever being written down.
        """
        sentinel = "11#DEADBEEFCAFE"
        if seed:
            coord = _seed_push_coord(_push_hub(addr=1), sensors={"100_200_1": {"data": None}})
        else:
            coord, _ = _make_coord()
            coord.data = None
            coord.async_update_listeners = MagicMock()

        with caplog.at_level(logging.DEBUG, logger="custom_components.rainpoint.coordinator"):
            _APPLY(coord, mid, sid, sentinel, 1717200000000)

        dropped = [r.getMessage() for r in caplog.records if "Dropping push" in r.getMessage()]
        assert len(dropped) == 1, f"{reason}: expected one drop log, got {dropped}"
        assert sentinel in dropped[0], f"{reason}: payload missing from {dropped[0]}"
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

    def test_push_clears_debounce_counter_for_the_sensor_key(self):
        """A push for a sensor key with a live debounce count leaves it absent afterward."""
        hub = _push_hub()
        coord = _seed_push_coord(hub, sensors={"100_200_1": {"data": None}})
        coord._silent_poll_counts["100_200_1"] = 2

        _APPLY(coord, 200, "D1", SAMPLE_HTV245_TLV_PAYLOAD, 1717200000000)

        assert "100_200_1" not in coord._silent_poll_counts

    def test_push_clears_the_repair_issue_with_hid_mid_addr(self):
        """The same push calls async_clear once with the device's hid, mid and addr."""
        hub = _push_hub()
        coord = _seed_push_coord(hub, sensors={"100_200_1": {"data": None}})

        _APPLY(coord, 200, "D1", SAMPLE_HTV245_TLV_PAYLOAD, 1717200000000)

        coord._silent_issues.async_clear.assert_called_once_with(100, 200, 1)

    def test_push_replaces_a_silent_entry_with_a_decoded_one(self):
        """A pushed frame for a currently-silent sensor key replaces it; the type
        string is no longer the silent sentinel."""
        hub = _push_hub()
        silent_entry = {
            "hid": 100,
            "mid": 200,
            "addr": 1,
            "data": {"type": SILENT_DATA_TYPE, "missed_polls": 3},
        }
        coord = _seed_push_coord(hub, sensors={"100_200_1": silent_entry})
        coord._silent_poll_counts["100_200_1"] = 3

        _APPLY(coord, 200, "D1", SAMPLE_HTV245_TLV_PAYLOAD, 1717200000000)

        updated = coord.data["sensors"]["100_200_1"]["data"]
        assert updated["type"] != SILENT_DATA_TYPE
        assert "zones" in updated
        assert "100_200_1" not in coord._silent_poll_counts
        coord._silent_issues.async_clear.assert_called_once_with(100, 200, 1)

    @pytest.mark.parametrize(
        ("reason", "mid", "sid", "seed"),
        [
            ("before first poll", 200, "D1", False),
            ("unknown mid", 999, "D1", True),
            ("unresolvable sid", 200, "state", True),
            ("unknown addr", 200, "D9", True),
        ],
    )
    def test_dropped_push_clears_neither_counter_nor_issue(self, reason, mid, sid, seed):
        """Each early-return drop path in apply_push_update must not clear
        _silent_poll_counts or call async_clear, since none of them reach the
        merge that follows those drops."""
        if seed:
            coord = _seed_push_coord(_push_hub(addr=1), sensors={"100_200_1": {"data": None}})
        else:
            coord, _ = _make_coord()
            coord.data = None
            coord.async_update_listeners = MagicMock()
        coord._silent_poll_counts["100_200_1"] = 2

        _APPLY(coord, mid, sid, "11#DEADBEEFCAFE", 1717200000000)

        assert coord._silent_poll_counts.get("100_200_1") == 2, reason
        coord._silent_issues.async_clear.assert_not_called()


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


class TestIsHubRecord:
    """Tests for is_hub_record and first_hub_record.

    Pairing a Bluetooth valve makes getDeviceByHid return a second top-level
    record in the same home whose identity fields are all empty strings rather
    than absent keys, so `.get(key, default)` hands back "" and the default
    never fires. These two helpers are what keeps such a record from being
    presented as a hub.
    """

    # The real captured shapes: a HWG023WBRF-V2 hub and the wrapper the cloud
    # added to hold an HTV210B, both under hid 182509.
    REAL_HUB: ClassVar[dict] = {
        "mid": 236547,
        "name": "Hub",
        "did": "17053410",
        "mac": "A8:46:74:BB:91:F0",
        "model": "HWG023WBRF-V2",
        "productKey": "a3QrDxYPTM2",
        "hid": 182509,
    }
    WRAPPER: ClassVar[dict] = {
        "mid": 346965,
        "name": "",
        "did": "",
        "mac": "",
        "model": "",
        "productKey": "",
        "deviceName": "",
        "hid": 182509,
    }

    def test_real_hub_is_a_hub(self):
        """A hub carrying did, mac, productKey and model is a hub."""
        assert _coord_module.is_hub_record(self.REAL_HUB) is True

    def test_wrapper_with_all_empty_identity_is_not_a_hub(self):
        """Empty strings are identity absent, so the wrapper is not a hub."""
        assert _coord_module.is_hub_record(self.WRAPPER) is False

    def test_empty_record_is_not_a_hub(self):
        """A record with no keys at all is not a hub."""
        assert _coord_module.is_hub_record({}) is False

    @pytest.mark.parametrize("field", ["did", "mac", "productKey", "model"])
    def test_any_single_identity_field_is_enough(self, field):
        """Each identity field on its own qualifies the record as a hub."""
        assert _coord_module.is_hub_record({field: "x"}) is True

    def test_first_hub_record_skips_a_leading_wrapper(self):
        """A wrapper in slot 0 is passed over in favour of the real hub behind it."""
        assert _coord_module.first_hub_record([self.WRAPPER, self.REAL_HUB]) is self.REAL_HUB

    def test_first_hub_record_keeps_api_order_among_real_hubs(self):
        """With several real hubs the first in API order wins, matching the MQTT client."""
        second = {"mid": 999, "did": "d999"}
        assert _coord_module.first_hub_record([self.REAL_HUB, second]) is self.REAL_HUB

    def test_first_hub_record_returns_none_when_only_wrappers(self):
        """An all-wrapper list resolves to no hub rather than a phantom one."""
        assert _coord_module.first_hub_record([self.WRAPPER]) is None

    def test_first_hub_record_returns_none_for_empty_list(self):
        """No hubs collected yet resolves to None."""
        assert _coord_module.first_hub_record([]) is None
