"""Tests for RainPointCoordinator: data fetching, decoder dispatch, fallback, and error handling."""

import asyncio
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
# self._notified_unknown_models, self.hass, and self.logger: all attributes we
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
    DOMAIN,
    MODEL_CO2,
    MODEL_DISPLAY_HUB,
    MODEL_FLOWMETER,
    MODEL_HTV210B,
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
    SAMPLE_HUB_DISCONNECT_CHANGED_AT_ISO,
    SAMPLE_HUB_DISCONNECT_FRAME,
    SAMPLE_HUB_FRAME_MID,
    SAMPLE_HUB_RECONNECT_CHANGED_AT_ISO,
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
        _last_poll_hub_keys=set(),
        _hub_absent_poll_counts={},
        _last_poll_sensor_keys=set(),
        _orphaned_key_poll_counts={},
        _aged_out_sensor_keys=frozenset(),
        _warned_empty_enumeration=set(),
        _warned_malformed_records=set(),
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
        # RainPoint field names from the generic decode land in the pre-fill.
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

        The RainPoint catalog contains model strings mapping to more than one
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


class TestHtv210bStalenessGuardCoverage:
    """DPCTL-06's downstream consequence: admitting MODEL_HTV210B to
    VALVE_MODELS enrols it in the command-versus-poll staleness guard with no
    coordinator source change. _preserve_recent_valve_command_state gates
    purely on model not in VALVE_MODELS, so this is confirmed by test rather
    than by reading, mirroring the RF-model tests above.
    """

    # Zone 1 running, zone 2 idle: state 0x21, a 120s duration, and an event
    # time exactly two minutes after the run started. Every field value is
    # taken from tests/api/test_decoders.py's TestDecodeHtv210b.RUNNING_PAYLOAD,
    # itself pinned against a timed two-minute run on real hardware.
    RUNNING_PAYLOAD = "11#18DC0117E1B40119D8211AD80021B71132FB1922B70000000025AF7800000026AF00000000FEFF0F1527FB19"

    @pytest.mark.asyncio
    async def test_stale_poll_preserves_the_commanded_zone_and_takes_the_fresh_sibling(self):
        """A poll older than a just-recorded command for zone 1 keeps zone 1's
        previously stored state; zone 2, never commanded, takes the fresh
        poll's value. The command is recorded through record_valve_command,
        the same entry point the DP valve entity's _record_successful_command
        calls, rather than by writing _last_valve_command_at directly."""
        coord, client = _make_coord()
        zone1_current = {"open": False, "duration_seconds": 0, "state_raw": 0}
        zone2_current = {"open": True, "duration_seconds": 999, "state_raw": 77}
        coord.data = {
            "sensors": {
                "100_200_1": {
                    "data": {"zones": {1: zone1_current, 2: zone2_current}},
                }
            }
        }
        _coord_module.RainPointCoordinator.record_valve_command(coord, "100_200_1", 1)

        client.get_devices_by_hid.return_value = [_make_hub(model=MODEL_HTV210B)]
        client.get_multiple_device_status.return_value = _make_status(
            value=self.RUNNING_PAYLOAD,
            time_ms=int(datetime(2024, 1, 1, tzinfo=UTC).timestamp() * 1000),
        )

        result = await _run(coord)

        zones = result["sensors"]["100_200_1"]["data"]["zones"]
        assert zones[1] == zone1_current
        assert zones[2]["open"] is False
        assert zones[2]["duration_seconds"] == 0
        assert zones[2]["state_raw"] == 0

    @pytest.mark.asyncio
    async def test_newer_poll_applies_normally_for_the_dp_commanded_model(self):
        """A poll newer than the recorded command is accepted, so the guard is
        a staleness window and not a permanent freeze."""
        coord, client = _make_coord()
        coord.data = {
            "sensors": {
                "100_200_1": {
                    "data": {"zones": {1: {"open": False, "duration_seconds": 0, "state_raw": 0}, 2: {}}},
                }
            }
        }
        coord._last_valve_command_at = {("100_200_1", 1): datetime(2024, 1, 1, tzinfo=UTC)}
        client.get_devices_by_hid.return_value = [_make_hub(model=MODEL_HTV210B)]
        client.get_multiple_device_status.return_value = _make_status(
            value=self.RUNNING_PAYLOAD,
            time_ms=int(datetime(2024, 1, 2, tzinfo=UTC).timestamp() * 1000),
        )

        result = await _run(coord)

        zone1 = result["sensors"]["100_200_1"]["data"]["zones"][1]
        assert zone1["open"] is True
        assert zone1["duration_seconds"] == 120
        assert zone1["state_raw"] == 0x21

    def test_guard_is_a_noop_for_a_decode_with_no_zones_dict(self):
        """The silent entry's own decode shape (task 1's build-gate guard)
        carries no zones dict at all, so the staleness guard and the
        silent-unit guard can never interact."""
        coord, _ = _make_coord()
        silent_decoded = {
            "type": SILENT_DATA_TYPE,
            "model": MODEL_HTV210B,
            "silent_state": "stopped_reporting",
            "last_seen": None,
            "missed_polls": 3,
        }
        coord.data = {"sensors": {"100_200_1": {"data": {"zones": {1: {"open": True}}}}}}
        coord._last_valve_command_at = {("100_200_1", 1): datetime(2024, 1, 2, tzinfo=UTC)}

        result = _coord_module.RainPointCoordinator._preserve_recent_valve_command_state(
            coord,
            "100_200_1",
            MODEL_HTV210B,
            silent_decoded,
            {"time": int(datetime(2024, 1, 1, tzinfo=UTC).timestamp() * 1000)},
        )

        assert result is silent_decoded


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

    def test_hub_connectivity_record_returns_the_record_for_mid(self):
        """A present record is handed back unchanged."""
        record = {"state": _coord_module.HUB_CONNECTED, "changed_at": None, "state_raw": None}
        coordinator = types.SimpleNamespace(data={"hub_connectivity": {7: record}})
        assert _coord_module.hub_connectivity_record(coordinator, 7) is record

    @pytest.mark.parametrize(
        "data",
        [
            None,
            {},
            {"hub_connectivity": None},
            {"hub_connectivity": {}},
            {"hub_connectivity": {9: {"state": _coord_module.HUB_CONNECTED}}},
            {"hub_connectivity": {7: None}},
        ],
        ids=["no-data", "no-key", "none-under-key", "empty-map", "other-mid", "none-record"],
    )
    def test_hub_connectivity_record_degrades_to_empty(self, data):
        """Every partial snapshot yields {}, which hub_connected_flag reads as unknown.

        The none-under-key case is the one a plain .get("hub_connectivity", {})
        would not catch: the default only fires on a missing key, not on a key
        holding None, so that lookup would raise on the following .get(mid).
        """
        coordinator = types.SimpleNamespace(data=data)
        assert _coord_module.hub_connectivity_record(coordinator, 7) == {}
        assert _coord_module.hub_connected_flag(_coord_module.hub_connectivity_record(coordinator, 7)) is None


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
                "hub_paired": False,
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
                "hub_paired": True,
                "data": {"type": "moisture"},
            },
        }

        _coord_module.RainPointCoordinator._sync_silent_device_issues(coord, decoded_sensors, [])

        (records,) = coord._silent_issues.async_sync.call_args.args
        by_key = {(r.hid, r.mid, r.addr): r for r in records}
        assert by_key[(100, 346965, 1)].hub_paired is False
        assert by_key[(100, 200, 2)].hub_paired is True

    def test_hub_paired_reads_the_stamped_field_not_the_raw_hub_fields(self):
        """A sensor entry stamped hub_paired True with empty raw hub fields
        still yields hub_paired True: this cannot pass under the retired
        inline predicate, which is the proof the stamped field is the source."""
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
                "hub_paired": True,
                "data": {"type": SILENT_DATA_TYPE, "missed_polls": 3},
            }
        }

        _coord_module.RainPointCoordinator._sync_silent_device_issues(coord, decoded_sensors, [])

        (records,) = coord._silent_issues.async_sync.call_args.args
        assert records[0].hub_paired is True

    def test_hub_paired_false_wins_even_with_populated_raw_hub_fields(self):
        """A sensor entry stamped hub_paired False with populated raw hub
        fields still yields hub_paired False, pinning that the raw fields are
        never consulted once the stamped field is present."""
        coord, _ = _make_coord()
        decoded_sensors = {
            "100_200_2": {
                "hid": 100,
                "mid": 200,
                "addr": 2,
                "model": MODEL_MOISTURE_SIMPLE,
                "hub_name": "Hub1",
                "product_key": "a3QrDxYPTM2",
                "device_name": "MAC-A84674BB91F0",
                "hub_paired": False,
                "data": {"type": "moisture"},
            }
        }

        _coord_module.RainPointCoordinator._sync_silent_device_issues(coord, decoded_sensors, [])

        (records,) = coord._silent_issues.async_sync.call_args.args
        assert records[0].hub_paired is False

    def test_hub_paired_absent_key_defaults_true(self):
        """A sensor entry with no hub_paired key at all defaults to hub-linked,
        matching build_sub_device_info's own absent-key default."""
        coord, _ = _make_coord()
        decoded_sensors = {
            "100_200_2": {
                "hid": 100,
                "mid": 200,
                "addr": 2,
                "model": MODEL_MOISTURE_SIMPLE,
                "hub_name": "Hub1",
                "data": {"type": "moisture"},
            }
        }

        _coord_module.RainPointCoordinator._sync_silent_device_issues(coord, decoded_sensors, [])

        (records,) = coord._silent_issues.async_sync.call_args.args
        assert records[0].hub_paired is True

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


class TestSilentIssueSurvivesPartialHubShrink:
    """A hub missing from a non-empty device list is an outage for that hub
    only: its still-silent children keep their debounce counter and their
    not-reporting card through the gap instead of being cleared and
    re-raised roughly three polls later. This is the churn observed on real
    hardware (an HTV210B moving mid between two polls), and the case the
    total-empty-list guard in TestSilentIssueSurvivesHubOutage does not
    cover, since that guard only fires when every hub disappears at once.
    """

    @staticmethod
    def _hub(hid=100, mid=200, addrs=(1,)):
        """A hub record listing the given addrs as children."""
        return {
            "hid": hid,
            "mid": mid,
            "name": f"Hub{mid}",
            "deviceName": f"dev{mid}",
            "productKey": "pk1",
            "homeName": "Home",
            "subDevices": [{"addr": addr, "model": "HTV210B", "name": f"Sub{addr}", "softVer": "1.0"} for addr in addrs],
        }

    @staticmethod
    def _arrived_empty(mid):
        """A status response that arrived and named nobody, for one hub."""
        return {"mid": mid, "subDeviceStatus": []}

    def _build(self, hubs):
        """Return (coord, client) with a real issue manager and the given hubs present."""
        coord, client = _make_coord()
        client.get_devices_by_hid.return_value = hubs
        client.get_multiple_device_status.return_value = [self._arrived_empty(hub["mid"]) for hub in hubs]
        coord._silent_issues = RainPointSilentDeviceIssues(MagicMock())
        return coord, client

    @pytest.mark.asyncio
    async def test_a_shrunken_device_list_is_an_outage_for_the_missing_hub(self):
        """The core regression this guard exists for: one poll with a
        shrunken (not empty) device list mid-sequence must not clear a
        still-silent child's card.

        References only symbols that already exist before this task's
        source change (create.call_count, delete.call_count,
        coord._silent_issues._active, coord._silent_poll_counts), so it
        fails with a real assertion error against the pre-change source
        rather than an AttributeError.
        """
        hub_a = self._hub(hid=100, mid=200, addrs=(1,))
        hub_b = self._hub(hid=100, mid=300, addrs=())
        coord, client = self._build([hub_a, hub_b])
        issue_id = silent_device_issue_id(100, 200, 1)

        with (
            patch.object(_repairs_module.ir, "async_create_issue") as create,
            patch.object(_repairs_module.ir, "async_delete_issue") as delete,
        ):
            for _ in range(3):
                await _run(coord)
            assert create.call_count == 1

            # Poll 4: hub A (mid 200) is missing; hub B (mid 300) is still
            # present. The list is not empty.
            client.get_devices_by_hid.return_value = [hub_b]
            client.get_multiple_device_status.return_value = [self._arrived_empty(300)]
            await _run(coord)

            assert delete.call_count == 0
            assert issue_id in coord._silent_issues._active
            assert coord._silent_poll_counts

            # Poll 5: hub A returns with the same child.
            client.get_devices_by_hid.return_value = [hub_a, hub_b]
            client.get_multiple_device_status.return_value = [
                self._arrived_empty(200),
                self._arrived_empty(300),
            ]
            await _run(coord)

            assert create.call_count == 1
            assert delete.call_count == 0

    @pytest.mark.asyncio
    async def test_a_healthy_sibling_hub_still_raises_and_clears_during_another_hubs_gap(self):
        """Suppression is scoped per hub, never global: hub B's own
        silent child still raises its card on schedule during hub A's gap,
        and hub A's own issue never gets created in the first place since
        its counter is frozen, not evidence for anything."""
        hub_a = self._hub(hid=100, mid=200, addrs=(1,))
        hub_b = self._hub(hid=100, mid=300, addrs=(1,))
        coord, client = self._build([hub_a, hub_b])
        issue_id_a = silent_device_issue_id(100, 200, 1)
        issue_id_b = silent_device_issue_id(100, 300, 1)
        key_a = "100_200_1"

        with (
            patch.object(_repairs_module.ir, "async_create_issue") as create,
            patch.object(_repairs_module.ir, "async_delete_issue") as delete,
        ):
            # Two polls with both hubs present: neither child has crossed
            # SILENT_DEBOUNCE_POLLS yet.
            await _run(coord)
            await _run(coord)
            assert create.call_count == 0
            assert coord._silent_poll_counts[key_a] == 2

            # Poll 3: hub A is missing; hub B is still present. B's own
            # child crosses the threshold on this poll.
            client.get_devices_by_hid.return_value = [hub_b]
            client.get_multiple_device_status.return_value = [self._arrived_empty(300)]
            await _run(coord)

            assert create.call_count == 1
            _hass, _domain, created_issue_id = create.call_args.args
            assert created_issue_id == issue_id_b
            assert issue_id_a not in coord._silent_issues._active
            assert coord._silent_poll_counts[key_a] == 2
            assert delete.call_count == 0

    @pytest.mark.asyncio
    async def test_a_missing_hub_is_released_on_the_fourth_consecutive_absence(self):
        """The stated rule: absences one through
        HUB_ABSENT_DEBOUNCE_POLLS suppress, the next one releases and the
        shrunken list becomes authoritative, clearing the missing hub's
        still-tracked card."""
        hub_a = self._hub(hid=100, mid=200, addrs=(1,))
        hub_b = self._hub(hid=100, mid=300, addrs=())
        coord, client = self._build([hub_a, hub_b])
        issue_id = silent_device_issue_id(100, 200, 1)

        with (
            patch.object(_repairs_module.ir, "async_create_issue") as create,
            patch.object(_repairs_module.ir, "async_delete_issue") as delete,
        ):
            for _ in range(3):
                await _run(coord)
            assert create.call_count == 1

            client.get_devices_by_hid.return_value = [hub_b]
            client.get_multiple_device_status.return_value = [self._arrived_empty(300)]

            for _ in range(_coord_module.HUB_ABSENT_DEBOUNCE_POLLS):
                await _run(coord)
                assert delete.call_count == 0

            # The next absence exceeds the threshold and releases hub A.
            await _run(coord)

            assert delete.call_count == 1
            _hass, _domain, deleted_issue_id = delete.call_args.args
            assert deleted_issue_id == issue_id

    @pytest.mark.asyncio
    async def test_a_hub_reappearing_resets_its_absence_counter_regardless_of_subdevices(self):
        """A hub reappearing at all counts as back, regardless of what
        its subDevices lists. The child's absence from subDevices is then
        definitive, and a later disappearance starts a fresh window."""
        hub_a = self._hub(hid=100, mid=200, addrs=(1,))
        hub_b = self._hub(hid=100, mid=300, addrs=())
        coord, client = self._build([hub_a, hub_b])
        key_a = (100, 200)

        with (
            patch.object(_repairs_module.ir, "async_create_issue"),
            patch.object(_repairs_module.ir, "async_delete_issue") as delete,
        ):
            for _ in range(3):
                await _run(coord)

            client.get_devices_by_hid.return_value = [hub_b]
            client.get_multiple_device_status.return_value = [self._arrived_empty(300)]
            await _run(coord)
            assert coord._hub_absent_poll_counts[key_a] == 1

            # Hub A reappears with an empty subDevices list -- still "back".
            hub_a_empty = self._hub(hid=100, mid=200, addrs=())
            client.get_devices_by_hid.return_value = [hub_a_empty, hub_b]
            client.get_multiple_device_status.return_value = [
                self._arrived_empty(200),
                self._arrived_empty(300),
            ]
            await _run(coord)

            assert key_a not in coord._hub_absent_poll_counts
            # The child's absence from subDevices is now definitive.
            assert delete.call_count == 1

            # A fresh disappearance starts a new window from zero.
            client.get_devices_by_hid.return_value = [hub_b]
            client.get_multiple_device_status.return_value = [self._arrived_empty(300)]
            await _run(coord)
            assert coord._hub_absent_poll_counts[key_a] == 1

    @pytest.mark.asyncio
    async def test_a_mid_debounce_silent_counter_freezes_across_the_gap_and_resumes(self):
        """A child's silent counter neither advances nor resets
        while its hub is missing, and reaches the threshold only on the poll
        after the hub returns."""
        hub_a = self._hub(hid=100, mid=200, addrs=(1,))
        hub_b = self._hub(hid=100, mid=300, addrs=())
        coord, client = self._build([hub_a, hub_b])
        key_a = "100_200_1"

        with (
            patch.object(_repairs_module.ir, "async_create_issue") as create,
            patch.object(_repairs_module.ir, "async_delete_issue"),
        ):
            # One poll: counter reaches 1, below the raise threshold.
            await _run(coord)
            assert coord._silent_poll_counts[key_a] == 1
            assert create.call_count == 0

            client.get_devices_by_hid.return_value = [hub_b]
            client.get_multiple_device_status.return_value = [self._arrived_empty(300)]
            await _run(coord)
            await _run(coord)
            assert coord._silent_poll_counts[key_a] == 1
            assert create.call_count == 0

            client.get_devices_by_hid.return_value = [hub_a, hub_b]
            client.get_multiple_device_status.return_value = [
                self._arrived_empty(200),
                self._arrived_empty(300),
            ]
            await _run(coord)
            assert coord._silent_poll_counts[key_a] == 2
            assert create.call_count == 0

            await _run(coord)
            assert coord._silent_poll_counts[key_a] == 3
            assert create.call_count == 1

    @pytest.mark.asyncio
    async def test_release_drops_both_the_counter_and_the_remembered_hub_key(self):
        """Release drops both the counter and the remembered hub
        key, so the memory this method owns cannot grow without bound.

        Release is driven through a shrunken list rather than an empty one,
        because an empty device list is a total outage and freezes the
        enumeration memory instead of advancing it. Driving it the other way
        would assert that a total outage releases, which is the opposite of
        what the empty-list door is for; a second hub keeps the list
        non-empty so the absence is a shrink.
        """
        hub_a = self._hub(hid=100, mid=200, addrs=())
        hub_b = self._hub(hid=100, mid=300, addrs=())
        coord, client = self._build([hub_a, hub_b])
        key_a = (100, 200)

        with (
            patch.object(_repairs_module.ir, "async_create_issue"),
            patch.object(_repairs_module.ir, "async_delete_issue"),
        ):
            await _run(coord)
            assert key_a in coord._last_poll_hub_keys

            client.get_devices_by_hid.return_value = [hub_b]
            client.get_multiple_device_status.return_value = [self._arrived_empty(300)]
            for _ in range(_coord_module.HUB_ABSENT_DEBOUNCE_POLLS + 1):
                await _run(coord)

            assert key_a not in coord._hub_absent_poll_counts
            assert key_a not in coord._last_poll_hub_keys
            # Hub B, present throughout, is still remembered: release is
            # scoped to the hub that actually went missing.
            assert (100, 300) in coord._last_poll_hub_keys

    @pytest.mark.asyncio
    async def test_a_total_empty_list_freezes_the_enumeration_memory_entirely(self):
        """The pre-existing total-empty-list guard freezes the
        enumeration memory exactly as it freezes every other debounce
        counter, and a partial list arriving afterward still computes the
        correct missing set from the pre-outage memory."""
        hub_a = self._hub(hid=100, mid=200, addrs=(1,))
        hub_b = self._hub(hid=100, mid=300, addrs=())
        coord, client = self._build([hub_a, hub_b])

        with (
            patch.object(_repairs_module.ir, "async_create_issue") as create,
            patch.object(_repairs_module.ir, "async_delete_issue"),
        ):
            for _ in range(3):
                await _run(coord)
            assert create.call_count == 1

            hub_keys_before = set(coord._last_poll_hub_keys)
            absent_counts_before = dict(coord._hub_absent_poll_counts)

            client.get_devices_by_hid.return_value = []
            await _run(coord)

            assert coord._last_poll_hub_keys == hub_keys_before
            assert coord._hub_absent_poll_counts == absent_counts_before

            client.get_devices_by_hid.return_value = [hub_b]
            client.get_multiple_device_status.return_value = [self._arrived_empty(300)]
            await _run(coord)

            assert coord._hub_absent_poll_counts[(100, 200)] == 1


def _orphan_series_hubs(*, hub_a_present=True, child_listed=True):
    """The two-hub device list every orphan-freeze timeline drives.

    Hub B carries no children on purpose: it exists only to keep the device
    list non-empty when hub A goes missing, so that absence is a shrink rather
    than the total outage the enclosing guard handles separately.
    """
    hubs = []
    if hub_a_present:
        hubs.append(
            {
                "mid": 200,
                "name": "HubA",
                "deviceName": "devA",
                "productKey": "pk1",
                "homeName": "Home",
                "subDevices": ([{"addr": 1, "model": "HTV210B", "name": "Sub1", "softVer": "1.0"}] if child_listed else []),
            }
        )
    hubs.append(
        {
            "mid": 300,
            "name": "HubB",
            "deviceName": "devB",
            "productKey": "pk1",
            "homeName": "Home",
            "subDevices": [],
        }
    )
    return hubs


def _point_at(client, hubs):
    """Point the next poll at the given device list, with arrived-but-empty status."""
    client.get_devices_by_hid.return_value = hubs
    client.get_multiple_device_status.return_value = [{"mid": hub["mid"], "subDeviceStatus": []} for hub in hubs]


def _build_orphan_series_coord():
    """Return (coordinator, client) wired the way __init__.py wires it.

    A real RainPointCoordinator, not a SimpleNamespace, so the interaction
    between the hub-absence window and the orphan window is exercised through
    the real _reconcile_repairs_surfaces ordering rather than a direct helper
    call.
    """
    client = AsyncMock()
    _point_at(client, _orphan_series_hubs())

    entry = MagicMock()
    entry.entry_id = "test_entry"
    entry.data = {CONF_HIDS: [100]}
    entry.options = {}

    hass = MagicMock()
    hass.data = {}

    return _coord_module.RainPointCoordinator(hass, client, entry), client


class TestOrphanedKeyCounterFreeze:
    """A key whose hub is inside its provisional absence window is neither
    counted toward removal nor reset, and resumes at its stored count once the
    hub returns or is released.

    A missing hub is an outage, not evidence about any addr, so it can neither
    confirm nor deny that a child has left. Without the freeze one transient
    hub blip walks every child of that hub thirty polls closer to a deletion
    offer, on evidence the poll did not contain.
    """

    KEY = "100_200_1"

    @staticmethod
    def _hub(hid=100, mid=200, addrs=(1,)):
        """A hub record listing the given addrs as children."""
        return {
            "hid": hid,
            "mid": mid,
            "name": f"Hub{mid}",
            "deviceName": f"dev{mid}",
            "productKey": "pk1",
            "homeName": "Home",
            "subDevices": [{"addr": addr, "model": "HTV210B", "name": f"Sub{addr}", "softVer": "1.0"} for addr in addrs],
        }

    @staticmethod
    def _arrived_empty(mid):
        """A status response that arrived and named nobody, for one hub."""
        return {"mid": mid, "subDeviceStatus": []}

    def _build(self, hubs):
        """Return (coord, client) with a real issue manager and the given hubs present."""
        coord, client = _make_coord()
        client.get_devices_by_hid.return_value = hubs
        client.get_multiple_device_status.return_value = [self._arrived_empty(hub["mid"]) for hub in hubs]
        coord._silent_issues = RainPointSilentDeviceIssues(MagicMock())
        return coord, client

    def _point(self, client, hubs):
        """Point the next poll at the given device list."""
        client.get_devices_by_hid.return_value = hubs
        client.get_multiple_device_status.return_value = [self._arrived_empty(hub["mid"]) for hub in hubs]

    async def _drive_to_three(self, coord, client):
        """Two polls with the child listed, then three with it dropped."""
        hub_a = self._hub(mid=200, addrs=(1,))
        hub_b = self._hub(mid=300, addrs=())
        for _ in range(2):
            await _run(coord)
        assert coord._orphaned_key_poll_counts == {}

        self._point(client, [self._hub(mid=200, addrs=()), hub_b])
        for expected in (1, 2, 3):
            await _run(coord)
            assert coord._orphaned_key_poll_counts[self.KEY] == expected
        assert coord._aged_out_sensor_keys == frozenset()
        return hub_a, hub_b

    @pytest.mark.asyncio
    async def test_a_key_whose_hub_is_provisionally_missing_is_neither_counted_nor_reset(self):
        """The core rule: three polls of hub outage leave the stored count at
        exactly the value it had when the hub vanished, and add nothing to the
        aged-out set."""
        coord, client = self._build([self._hub(mid=200, addrs=(1,)), self._hub(mid=300, addrs=())])

        with (
            patch.object(_repairs_module.ir, "async_create_issue"),
            patch.object(_repairs_module.ir, "async_delete_issue"),
        ):
            _hub_a, hub_b = await self._drive_to_three(coord, client)

            # Hub A leaves the device list entirely; hub B keeps it non-empty.
            self._point(client, [hub_b])
            for _ in range(_coord_module.HUB_ABSENT_DEBOUNCE_POLLS):
                await _run(coord)
                assert coord._orphaned_key_poll_counts[self.KEY] == 3
                assert coord._aged_out_sensor_keys == frozenset()

            # The key is still remembered, which is what makes the resume work.
            assert self.KEY in coord._last_poll_sensor_keys

    @pytest.mark.asyncio
    async def test_the_counter_resumes_where_it_stopped_when_the_hub_returns(self):
        """Hub A comes back still not listing the child: the freeze lifts and
        the counter advances from 3 to 4 rather than restarting."""
        coord, client = self._build([self._hub(mid=200, addrs=(1,)), self._hub(mid=300, addrs=())])

        with (
            patch.object(_repairs_module.ir, "async_create_issue"),
            patch.object(_repairs_module.ir, "async_delete_issue"),
        ):
            _hub_a, hub_b = await self._drive_to_three(coord, client)

            self._point(client, [hub_b])
            await _run(coord)
            assert coord._orphaned_key_poll_counts[self.KEY] == 3

            self._point(client, [self._hub(mid=200, addrs=()), hub_b])
            await _run(coord)
            assert coord._orphaned_key_poll_counts[self.KEY] == 4

    @pytest.mark.asyncio
    async def test_the_counter_resumes_where_it_stopped_when_the_hub_is_released(self):
        """Hub A never comes back. The release rule drops it from the missing
        set, so the freeze lifts with no carry-forward code and the counter
        advances from 3 on the very poll that releases it."""
        coord, client = self._build([self._hub(mid=200, addrs=(1,)), self._hub(mid=300, addrs=())])

        with (
            patch.object(_repairs_module.ir, "async_create_issue"),
            patch.object(_repairs_module.ir, "async_delete_issue"),
        ):
            _hub_a, hub_b = await self._drive_to_three(coord, client)

            self._point(client, [hub_b])
            for _ in range(_coord_module.HUB_ABSENT_DEBOUNCE_POLLS):
                await _run(coord)
            assert coord._orphaned_key_poll_counts[self.KEY] == 3
            assert (100, 200) in coord._hub_absent_poll_counts

            # The next absence exceeds the hub threshold and releases hub A.
            await _run(coord)
            assert (100, 200) not in coord._hub_absent_poll_counts
            assert coord._orphaned_key_poll_counts[self.KEY] == 4

            await _run(coord)
            assert coord._orphaned_key_poll_counts[self.KEY] == 5

    @pytest.mark.asyncio
    async def test_a_key_returning_with_its_hub_drops_its_count_entirely(self):
        """A key that reappears at any point resets to zero, regardless of how
        high it had climbed or of what its hub's subDevices listed on the poll
        it returned."""
        coord, client = self._build([self._hub(mid=200, addrs=(1,)), self._hub(mid=300, addrs=())])

        with (
            patch.object(_repairs_module.ir, "async_create_issue"),
            patch.object(_repairs_module.ir, "async_delete_issue"),
        ):
            hub_a, hub_b = await self._drive_to_three(coord, client)

            self._point(client, [hub_b])
            await _run(coord)
            assert coord._orphaned_key_poll_counts[self.KEY] == 3

            self._point(client, [hub_a, hub_b])
            await _run(coord)
            assert self.KEY not in coord._orphaned_key_poll_counts
            assert coord._aged_out_sensor_keys == frozenset()

    @pytest.mark.asyncio
    async def test_a_total_empty_device_list_advances_no_counter(self):
        """An empty device list is a total outage: no counter advances and the
        enumeration memory is unchanged, so a partial list arriving afterward
        still computes the correct missing set."""
        coord, client = self._build([self._hub(mid=200, addrs=(1,)), self._hub(mid=300, addrs=())])

        with (
            patch.object(_repairs_module.ir, "async_create_issue"),
            patch.object(_repairs_module.ir, "async_delete_issue"),
        ):
            _hub_a, hub_b = await self._drive_to_three(coord, client)
            sensor_keys_before = set(coord._last_poll_sensor_keys)

            client.get_devices_by_hid.return_value = []
            await _run(coord)

            assert coord._orphaned_key_poll_counts[self.KEY] == 3
            assert coord._last_poll_sensor_keys == sensor_keys_before

            self._point(client, [self._hub(mid=200, addrs=()), hub_b])
            await _run(coord)
            assert coord._orphaned_key_poll_counts[self.KEY] == 4

    @pytest.mark.asyncio
    async def test_an_aged_out_key_keeps_its_verdict_through_a_hub_outage(self):
        """A key that already reached the threshold keeps its aged-out verdict
        while its hub is missing. Withdrawing it there would clear the card on
        the outage and re-raise it when the hub returned, which is the
        clear-then-reraise cycle every outage guard in this file exists to
        prevent, arriving from the other side."""
        coord, client = self._build([self._hub(mid=200, addrs=(1,)), self._hub(mid=300, addrs=())])
        hub_b = self._hub(mid=300, addrs=())

        with (
            patch.object(_repairs_module.ir, "async_create_issue"),
            patch.object(_repairs_module.ir, "async_delete_issue"),
        ):
            await _run(coord)
            self._point(client, [self._hub(mid=200, addrs=()), hub_b])
            for _ in range(_coord_module.ORPHANED_KEY_DEBOUNCE_POLLS):
                await _run(coord)
            assert coord._aged_out_sensor_keys == frozenset({self.KEY})

            self._point(client, [hub_b])
            for _ in range(_coord_module.HUB_ABSENT_DEBOUNCE_POLLS):
                await _run(coord)
                assert coord._orphaned_key_poll_counts[self.KEY] == _coord_module.ORPHANED_KEY_DEBOUNCE_POLLS
                assert coord._aged_out_sensor_keys == frozenset({self.KEY})

    @pytest.mark.asyncio
    async def test_the_freeze_leaves_one_debug_breadcrumb_per_frozen_poll(self, caplog):
        """A frozen window is visible in production logs without one line per
        key per poll, and the line carries only integer counts."""
        coord, client = self._build([self._hub(mid=200, addrs=(1,)), self._hub(mid=300, addrs=())])

        with (
            patch.object(_repairs_module.ir, "async_create_issue"),
            patch.object(_repairs_module.ir, "async_delete_issue"),
        ):
            _hub_a, hub_b = await self._drive_to_three(coord, client)

            caplog.clear()
            with caplog.at_level(logging.DEBUG, logger="custom_components.rainpoint.coordinator"):
                self._point(client, [hub_b])
                await _run(coord)

        frozen_lines = [r for r in caplog.records if "orphan candidate" in r.message]
        assert len(frozen_lines) == 1
        assert frozen_lines[0].levelno == logging.DEBUG
        assert "Sub1" not in frozen_lines[0].getMessage()

    @pytest.mark.asyncio
    async def test_the_two_windows_run_in_series_for_a_child_of_a_departing_hub(self):
        """Driven through a real coordinator construct then repeated refresh.

        A child of a hub that leaves and never returns is protected by
        HUB_ABSENT_DEBOUNCE_POLLS and then by ORPHANED_KEY_DEBOUNCE_POLLS, in
        series, not by either window alone. Roughly 66 minutes at the default
        scan interval, which is a stated and accepted cost rather than an
        oversight.
        """
        coordinator, client = _build_orphan_series_coord()
        key = "100_200_1"

        with (
            patch.object(_repairs_module.ir, "async_create_issue"),
            patch.object(_repairs_module.ir, "async_delete_issue"),
        ):
            await coordinator.async_config_entry_first_refresh()
            assert coordinator._last_poll_sensor_keys == {key}
            assert coordinator._orphaned_key_poll_counts == {}

            # Hub A leaves the device list entirely and never returns.
            _point_at(client, _orphan_series_hubs(hub_a_present=False))
            for _ in range(_coord_module.HUB_ABSENT_DEBOUNCE_POLLS):
                await coordinator.async_refresh()
                assert coordinator._orphaned_key_poll_counts == {}
                assert coordinator.aged_out_sensor_keys() == frozenset()

            # The release poll is the first one that counts the child.
            for expected in range(1, _coord_module.ORPHANED_KEY_DEBOUNCE_POLLS + 1):
                await coordinator.async_refresh()
                assert coordinator._orphaned_key_poll_counts[key] == expected
                if expected < _coord_module.ORPHANED_KEY_DEBOUNCE_POLLS:
                    assert coordinator.aged_out_sensor_keys() == frozenset()

            assert coordinator.aged_out_sensor_keys() == frozenset({key})


class TestSensorKeysForHubKeys:
    """Direct-call tests for _sensor_keys_for_hub_keys."""

    def test_filters_only_keys_whose_hub_half_matches(self):
        """A protected hub's children are returned; another hub's are not."""
        sensor_keys = {"100_200_1", "100_200_2", "100_300_1"}

        protected = _coord_module._sensor_keys_for_hub_keys(sensor_keys, {(100, 200)})

        assert protected == {"100_200_1", "100_200_2"}

    def test_a_neighbouring_hid_mid_pair_is_not_a_prefix_match(self):
        """A naive startswith("100_20") would over-match "100_200_1" against
        the hub key (100, 20); the exact hub-half comparison must not."""
        sensor_keys = {"100_200_1"}

        protected = _coord_module._sensor_keys_for_hub_keys(sensor_keys, {(100, 20)})

        assert protected == set()

    def test_derived_issue_id_matches_silent_device_issue_id_exactly(self):
        """The round-trip _sync_silent_device_issues depends on: recovering
        the three typed parts from a key and calling silent_device_issue_id
        must equal the id built from the original typed values, including an
        integer hid."""
        key = _coord_module._sensor_key(100, 200, 1)
        hid_part, mid_part, addr_part = key.rsplit("_", 2)

        derived = silent_device_issue_id(hid_part, int(mid_part), int(addr_part))

        assert derived == silent_device_issue_id(100, 200, 1)


class TestTrackMissingHubs:
    """Direct-call tests for _track_missing_hubs."""

    def test_a_wrapper_record_is_never_remembered_as_a_hub_key(self):
        """A Bluetooth wrapper record must never enter _last_poll_hub_keys,
        so its disappearance is never treated as a hub shrink."""
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

        provisional = _coord_module.RainPointCoordinator._track_missing_hubs(coord, [wrapper_hub])

        assert provisional == set()
        assert coord._last_poll_hub_keys == set()

    def test_provisional_boundary_is_the_third_absence_and_release_is_the_fourth(self):
        """Pins the "<=" boundary at exactly HUB_ABSENT_DEBOUNCE_POLLS,
        reading the constant rather than the literal 3."""
        coord, _ = _make_coord()
        key = (100, 200)
        coord._last_poll_hub_keys = {key}
        coord._hub_absent_poll_counts = {key: _coord_module.HUB_ABSENT_DEBOUNCE_POLLS - 1}

        provisional = _coord_module.RainPointCoordinator._track_missing_hubs(coord, [])

        assert coord._hub_absent_poll_counts[key] == _coord_module.HUB_ABSENT_DEBOUNCE_POLLS
        assert provisional == {key}

        provisional = _coord_module.RainPointCoordinator._track_missing_hubs(coord, [])

        assert key not in coord._hub_absent_poll_counts
        assert provisional == set()


_TRACK_ORPHANS = _coord_module.RainPointCoordinator._track_orphaned_keys


class TestTrackOrphanedKeys:
    """Direct-call tests for _track_orphaned_keys."""

    KEY = "100_200_1"

    @staticmethod
    def _hub(hid=100, mid=200, addrs=()):
        """A hub record listing the given addrs as children."""
        return {
            "hid": hid,
            "mid": mid,
            "name": f"Hub{mid}",
            "deviceName": f"dev{mid}",
            "productKey": "pk1",
            "homeName": "Home",
            "subDevices": [{"addr": addr, "model": "HTV210B", "name": f"Sub{addr}", "softVer": "1.0"} for addr in addrs],
        }

    def test_the_boundary_is_the_thirtieth_consecutive_absence(self):
        """Pinned in both directions, reading the constant rather than the
        literal 30: one short of the threshold does not age out, and the
        threshold itself does. The comparison is ">=", deliberately not the
        "<=" its nearest neighbour _track_missing_hubs uses.
        """
        coord, _ = _make_coord()
        coord._last_poll_sensor_keys = {self.KEY}
        coord._orphaned_key_poll_counts = {self.KEY: _coord_module.ORPHANED_KEY_DEBOUNCE_POLLS - 2}

        aged_out = _TRACK_ORPHANS(coord, [self._hub()])

        assert coord._orphaned_key_poll_counts[self.KEY] == _coord_module.ORPHANED_KEY_DEBOUNCE_POLLS - 1
        assert aged_out == frozenset()

        aged_out = _TRACK_ORPHANS(coord, [self._hub()])

        assert coord._orphaned_key_poll_counts[self.KEY] == _coord_module.ORPHANED_KEY_DEBOUNCE_POLLS
        assert aged_out == frozenset({self.KEY})

    def test_a_key_that_reappears_resets_its_counter_to_zero(self):
        """Mid-count reappearance drops the entry entirely rather than
        decrementing it, so a later disappearance starts a fresh window."""
        coord, _ = _make_coord()
        coord._last_poll_sensor_keys = {self.KEY}
        coord._orphaned_key_poll_counts = {self.KEY: 12}

        aged_out = _TRACK_ORPHANS(coord, [self._hub(addrs=(1,))])

        assert self.KEY not in coord._orphaned_key_poll_counts
        assert aged_out == frozenset()

        aged_out = _TRACK_ORPHANS(coord, [self._hub()])

        assert coord._orphaned_key_poll_counts[self.KEY] == 1

    def test_a_key_that_reappears_after_ageing_out_leaves_the_aged_out_set(self):
        """A returning key is offered for removal no longer, however long it
        had been gone."""
        coord, _ = _make_coord()
        coord._last_poll_sensor_keys = {self.KEY}
        coord._orphaned_key_poll_counts = {self.KEY: _coord_module.ORPHANED_KEY_DEBOUNCE_POLLS - 1}

        assert _TRACK_ORPHANS(coord, [self._hub()]) == frozenset({self.KEY})

        coord._aged_out_sensor_keys = frozenset({self.KEY})

        assert _TRACK_ORPHANS(coord, [self._hub(addrs=(1,))]) == frozenset()
        assert self.KEY not in coord._orphaned_key_poll_counts

    def test_a_never_seen_key_is_never_counted(self):
        """The input is the enumeration memory, so a key no poll ever listed
        cannot be counted into existence by a hub that lists nothing."""
        coord, _ = _make_coord()

        aged_out = _TRACK_ORPHANS(coord, [self._hub()])

        assert coord._orphaned_key_poll_counts == {}
        assert aged_out == frozenset()
        assert coord._last_poll_sensor_keys == set()

    def test_the_wrapper_records_children_are_counted_not_frozen(self):
        """The reproduction case. A Bluetooth wrapper record fails
        is_hub_record, so _track_missing_hubs never remembers it and it can
        never appear in the missing-hub set. Its children are therefore counted
        immediately when it disappears, which is correct: a wrapper record
        vanishing as a hub's mid changes is the event this surface exists for,
        and freezing on it would make the surface unable to fire on its own
        reproduction.
        """
        coord, _ = _make_coord()
        wrapper = {
            "hid": 100,
            "mid": 346965,
            "did": "",
            "mac": "",
            "productKey": "",
            "model": "",
            "name": "",
            "subDevices": [{"addr": 1, "model": "HTV210B", "name": "BT Valve", "softVer": "1.0"}],
        }
        real_hub = self._hub(mid=200)
        wrapper_key = "100_346965_1"

        # Poll 1: both records present. The wrapper's child is remembered even
        # though the wrapper itself is not remembered as a hub key.
        missing_hub_keys = _coord_module.RainPointCoordinator._track_missing_hubs(coord, [wrapper, real_hub])
        _TRACK_ORPHANS(coord, [wrapper, real_hub], missing_hub_keys=missing_hub_keys)
        assert coord._last_poll_hub_keys == {(100, 200)}
        assert wrapper_key in coord._last_poll_sensor_keys

        # Poll 2: the wrapper record is gone. No hub is missing, so nothing is
        # frozen and its child starts counting on this very poll.
        missing_hub_keys = _coord_module.RainPointCoordinator._track_missing_hubs(coord, [real_hub])
        _TRACK_ORPHANS(coord, [real_hub], missing_hub_keys=missing_hub_keys)

        assert missing_hub_keys == frozenset()
        assert coord._orphaned_key_poll_counts[wrapper_key] == 1

    def test_the_age_out_breadcrumb_fires_once_per_transition(self, caplog):
        """One INFO line at the moment a card can appear, not one per poll
        thereafter, and it carries only the sensor key and integer counts."""
        coord, _ = _make_coord()
        coord._last_poll_sensor_keys = {self.KEY}
        coord._orphaned_key_poll_counts = {self.KEY: _coord_module.ORPHANED_KEY_DEBOUNCE_POLLS - 1}

        with caplog.at_level(logging.INFO, logger="custom_components.rainpoint.coordinator"):
            coord._aged_out_sensor_keys = _TRACK_ORPHANS(coord, [self._hub()])
            coord._aged_out_sensor_keys = _TRACK_ORPHANS(coord, [self._hub()])

        aged_lines = [r for r in caplog.records if "no longer listed" in r.message]
        assert len(aged_lines) == 1
        assert aged_lines[0].levelno == logging.INFO
        assert self.KEY in aged_lines[0].getMessage()
        assert "Sub1" not in aged_lines[0].getMessage()


class TestOrphanedKeyCounterIsInertOnExistingSurfaces:
    """The removal counter's input is the subDevices enumeration and nothing
    else, so the not-reporting surface, the shrink guard and both push entry
    points all behave exactly as they did before it existed."""

    KEY = "100_200_1"

    _hub = staticmethod(TestOrphanedKeyCounterFreeze._hub)
    _arrived_empty = staticmethod(TestOrphanedKeyCounterFreeze._arrived_empty)

    def _build(self, hubs):
        """Return (coord, client) with a real issue manager and the given hubs present."""
        coord, client = _make_coord()
        client.get_devices_by_hid.return_value = hubs
        client.get_multiple_device_status.return_value = [self._arrived_empty(hub["mid"]) for hub in hubs]
        coord._silent_issues = RainPointSilentDeviceIssues(MagicMock())
        return coord, client

    @pytest.mark.asyncio
    async def test_a_silent_but_still_listed_device_never_starts_a_removal_count(self):
        """A device that is merely quiet stays listed in subDevices, so it
        never leaves the enumeration and never starts a removal count, driven
        past both thresholds rather than only past the silent one."""
        coord, _client = self._build([self._hub(mid=200, addrs=(1,))])

        with (
            patch.object(_repairs_module.ir, "async_create_issue") as create,
            patch.object(_repairs_module.ir, "async_delete_issue"),
        ):
            for poll in range(1, _coord_module.ORPHANED_KEY_DEBOUNCE_POLLS + 3):
                await _run(coord)
                assert coord._orphaned_key_poll_counts == {}
                assert coord._aged_out_sensor_keys == frozenset()
                if poll < _coord_module.SILENT_DEBOUNCE_POLLS:
                    assert create.call_count == 0

            # The silent card was raised on schedule and exactly once.
            assert create.call_count == 1
            assert coord._silent_poll_counts[self.KEY] >= _coord_module.SILENT_DEBOUNCE_POLLS

    @pytest.mark.asyncio
    async def test_a_key_that_goes_silent_then_leaves_is_counted_from_the_poll_it_left(self):
        """The two counters move independently: the removal count starts on the
        poll the key left the enumeration, not on the poll it went quiet, and
        the silent counter is pruned exactly as it was before."""
        coord, client = self._build([self._hub(mid=200, addrs=(1,))])

        with (
            patch.object(_repairs_module.ir, "async_create_issue"),
            patch.object(_repairs_module.ir, "async_delete_issue"),
        ):
            for _ in range(_coord_module.SILENT_DEBOUNCE_POLLS + 2):
                await _run(coord)
            assert coord._silent_poll_counts[self.KEY] == _coord_module.SILENT_DEBOUNCE_POLLS + 2
            assert coord._orphaned_key_poll_counts == {}

            hub_without_child = self._hub(mid=200, addrs=())
            client.get_devices_by_hid.return_value = [hub_without_child]
            client.get_multiple_device_status.return_value = [self._arrived_empty(200)]
            await _run(coord)

            assert coord._orphaned_key_poll_counts[self.KEY] == 1
            assert self.KEY not in coord._silent_poll_counts

    def test_both_push_entry_points_leave_the_orphan_counter_untouched(self):
        """No push path reads or writes any of the three structures this
        counter owns; only the poll does."""
        hub = _push_hub()
        coord = _seed_push_coord(hub, sensors={"100_200_1": {"data": None}})
        coord.data["hub_connectivity"] = {}
        coord._last_poll_sensor_keys = {self.KEY}
        coord._orphaned_key_poll_counts = {self.KEY: 5}
        coord._aged_out_sensor_keys = frozenset({"100_200_9"})
        device_ts = int(datetime(2024, 6, 1, tzinfo=UTC).timestamp() * 1000)

        _APPLY(coord, 200, "D1", SAMPLE_HTV245_TLV_PAYLOAD, device_ts)
        _APPLY_HUB(coord, 200, False, device_ts)

        assert coord._last_poll_sensor_keys == {self.KEY}
        assert coord._orphaned_key_poll_counts == {self.KEY: 5}
        assert coord._aged_out_sensor_keys == frozenset({"100_200_9"})

    @pytest.mark.asyncio
    async def test_the_existing_shrink_guard_timeline_produces_no_aged_out_key(self):
        """The shrink guard's own timeline, re-driven with the aged-out set
        asserted empty at every step: the new counter must be inert across a
        sequence that guard already owns."""
        hub_a = self._hub(hid=100, mid=200, addrs=(1,))
        hub_b = self._hub(hid=100, mid=300, addrs=())
        coord, client = self._build([hub_a, hub_b])
        issue_id = silent_device_issue_id(100, 200, 1)

        with (
            patch.object(_repairs_module.ir, "async_create_issue") as create,
            patch.object(_repairs_module.ir, "async_delete_issue") as delete,
        ):
            for _ in range(3):
                await _run(coord)
                assert coord._aged_out_sensor_keys == frozenset()
            assert create.call_count == 1

            client.get_devices_by_hid.return_value = [hub_b]
            client.get_multiple_device_status.return_value = [self._arrived_empty(300)]
            await _run(coord)

            assert delete.call_count == 0
            assert issue_id in coord._silent_issues._active
            assert coord._aged_out_sensor_keys == frozenset()

            client.get_devices_by_hid.return_value = [hub_a, hub_b]
            client.get_multiple_device_status.return_value = [
                self._arrived_empty(200),
                self._arrived_empty(300),
            ]
            await _run(coord)

            assert create.call_count == 1
            assert delete.call_count == 0
            assert coord._aged_out_sensor_keys == frozenset()


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


class TestHubConnectivitySurvivesPartialHubShrink:
    """A hub missing from a non-empty device list is an outage for its
    connectivity card too, mirroring TestSilentIssueSurvivesPartialHubShrink
    for the not-reporting lifecycle: both surfaces have to move together,
    since the empty-list guard already treats them as one outage.

    Drives the real coordinator through async_config_entry_first_refresh
    then repeated async_refresh, the pattern
    TestHubConnectivityDebounceRealTimeline establishes, rather than an
    injected already-past-threshold coordinator.data snapshot.
    """

    HUB_A_MID = 200
    HUB_B_MID = 300

    @staticmethod
    def _hub(mid, name):
        """A real hub record with no subDevices, mirroring
        _build_hub_connectivity_coord's reasoning: a declared sub-device
        going silent would raise its own issue on the same mocks these
        tests assert call counts against."""
        return {
            "mid": mid,
            "name": name,
            "deviceName": f"dev{mid}",
            "productKey": "pk1",
            "homeName": "Home",
            "subDevices": [],
        }

    def _set_connected(self, client, hub_a_connected=None, hub_b_connected=None):
        """Mutate the next poll's connected entries for both hubs. A None
        value omits that hub's status entirely, matching a hub genuinely
        absent from the device list this poll."""
        status = []
        if hub_a_connected is not None:
            status.append({"mid": self.HUB_A_MID, "subDeviceStatus": [{"id": "connected", "value": hub_a_connected}]})
        if hub_b_connected is not None:
            status.append({"mid": self.HUB_B_MID, "subDeviceStatus": [{"id": "connected", "value": hub_b_connected}]})
        client.get_multiple_device_status.return_value = status

    def _build(self, hub_a_connected="1", hub_b_connected="1"):
        """Return (coordinator, client) wired the way __init__.py wires it,
        with two real hubs."""
        client = AsyncMock()
        client.get_devices_by_hid.return_value = [
            self._hub(self.HUB_A_MID, "HubA"),
            self._hub(self.HUB_B_MID, "HubB"),
        ]
        self._set_connected(client, hub_a_connected, hub_b_connected)

        entry = MagicMock()
        entry.entry_id = "test_entry"
        entry.data = {CONF_HIDS: [100]}

        hass = MagicMock()
        hass.data = {}

        return _coord_module.RainPointCoordinator(hass, client, entry), client

    @pytest.mark.asyncio
    async def test_a_shrunken_device_list_leaves_the_missing_hubs_counter_and_issue_untouched(self):
        """A hub missing from a non-empty device list keeps its
        disconnect counter and its connectivity card through the gap.

        Asserted by the issue id never appearing among the deletes made
        during the gap poll specifically (an index slice of the mock's call
        list, not its full history), rather than a raw call-count delta:
        hub A's own issue id is legitimately cleared idempotently while it
        is still connected, before the gap begins, and hub B's own record
        clears its (never-raised) issue idempotently every poll it stays
        connected (mirroring the empty-list sibling test's own note that
        this clear is unconditional), so only the calls made during the gap
        poll itself say anything about the gap.
        """
        coordinator, client = self._build(hub_a_connected="1", hub_b_connected="1")
        hub_a_issue_id = hub_connectivity_issue_id(100, self.HUB_A_MID)

        with (
            patch.object(_repairs_module.ir, "async_create_issue") as create,
            patch.object(_repairs_module.ir, "async_delete_issue") as delete,
        ):
            await coordinator.async_config_entry_first_refresh()

            self._set_connected(client, hub_a_connected="0", hub_b_connected="1")
            for _ in range(_coord_module.HUB_DISCONNECT_DEBOUNCE_POLLS):
                await coordinator.async_refresh()
            assert create.call_count == 1

            # Hub A drops out of the device list entirely; hub B stays.
            client.get_devices_by_hid.return_value = [self._hub(self.HUB_B_MID, "HubB")]
            self._set_connected(client, hub_a_connected=None, hub_b_connected="1")
            calls_before_gap = len(delete.call_args_list)
            await coordinator.async_refresh()

            deleted_ids_during_gap = {call.args[2] for call in delete.call_args_list[calls_before_gap:]}
            assert hub_a_issue_id not in deleted_ids_during_gap
            assert hub_a_issue_id in coordinator._hub_connectivity_issues._active
            assert (100, self.HUB_A_MID) in coordinator._hub_disconnect_poll_counts

    @pytest.mark.asyncio
    async def test_a_missing_hub_holds_its_last_known_connectivity_record(self):
        """The Cloud Connection binary sensor and valve availability
        both read coordinator data through hub_connectivity_record /
        hub_connected_flag, so the hold has to be proven through those two
        functions rather than the raw dict."""
        coordinator, client = self._build(hub_a_connected="1", hub_b_connected="1")

        with (
            patch.object(_repairs_module.ir, "async_create_issue"),
            patch.object(_repairs_module.ir, "async_delete_issue"),
        ):
            await coordinator.async_config_entry_first_refresh()
            assert _coord_module.hub_connected_flag(_coord_module.hub_connectivity_record(coordinator, self.HUB_A_MID)) is True

            client.get_devices_by_hid.return_value = [self._hub(self.HUB_B_MID, "HubB")]
            self._set_connected(client, hub_a_connected=None, hub_b_connected="1")
            await coordinator.async_refresh()

            assert _coord_module.hub_connected_flag(_coord_module.hub_connectivity_record(coordinator, self.HUB_A_MID)) is True
            # A hub with no prior record at all must not gain an invented one.
            assert _coord_module.hub_connectivity_record(coordinator, 999999) == {}

    @pytest.mark.asyncio
    async def test_a_held_disconnected_record_on_a_missing_hub_does_not_advance_the_debounce(self):
        """A held disconnected record on a hub that is also missing
        this poll must not advance the debounce, or a card would raise from
        evidence the poll never contained."""
        coordinator, client = self._build(hub_a_connected="1", hub_b_connected="1")

        with (
            patch.object(_repairs_module.ir, "async_create_issue") as create,
            patch.object(_repairs_module.ir, "async_delete_issue"),
        ):
            await coordinator.async_config_entry_first_refresh()

            self._set_connected(client, hub_a_connected="0", hub_b_connected="1")
            await coordinator.async_refresh()
            assert coordinator._hub_disconnect_poll_counts[(100, self.HUB_A_MID)] == 1
            assert create.call_count == 0

            client.get_devices_by_hid.return_value = [self._hub(self.HUB_B_MID, "HubB")]
            self._set_connected(client, hub_a_connected=None, hub_b_connected="1")
            for _ in range(_coord_module.HUB_ABSENT_DEBOUNCE_POLLS):
                await coordinator.async_refresh()

            assert coordinator._hub_disconnect_poll_counts[(100, self.HUB_A_MID)] == 1
            assert create.call_count == 0

    @pytest.mark.asyncio
    async def test_a_missing_hub_released_after_the_window_clears_its_connectivity_card(self):
        """The release rule on the connectivity surface: one absence past
        HUB_ABSENT_DEBOUNCE_POLLS and the counter key is gone, a delete fires for
        hub_connectivity_issue_id, and the held record stops being carried
        into coordinator.data["hub_connectivity"].

        Asserted by issue id within each poll's own slice of delete calls,
        for the same reason as the sibling test above: hub A's own issue id
        is legitimately cleared idempotently while still connected, before
        the gap begins, and hub B's own record clears its (never-raised)
        issue idempotently every poll it stays connected.
        """
        coordinator, client = self._build(hub_a_connected="1", hub_b_connected="1")
        hub_a_issue_id = hub_connectivity_issue_id(100, self.HUB_A_MID)

        with (
            patch.object(_repairs_module.ir, "async_create_issue") as create,
            patch.object(_repairs_module.ir, "async_delete_issue") as delete,
        ):
            await coordinator.async_config_entry_first_refresh()

            self._set_connected(client, hub_a_connected="0", hub_b_connected="1")
            for _ in range(_coord_module.HUB_DISCONNECT_DEBOUNCE_POLLS):
                await coordinator.async_refresh()
            assert create.call_count == 1

            client.get_devices_by_hid.return_value = [self._hub(self.HUB_B_MID, "HubB")]
            self._set_connected(client, hub_a_connected=None, hub_b_connected="1")

            for _ in range(_coord_module.HUB_ABSENT_DEBOUNCE_POLLS):
                calls_before_poll = len(delete.call_args_list)
                await coordinator.async_refresh()
                deleted_ids_this_poll = {call.args[2] for call in delete.call_args_list[calls_before_poll:]}
                assert hub_a_issue_id not in deleted_ids_this_poll

            # The next absence exceeds the threshold and releases hub A.
            calls_before_release = len(delete.call_args_list)
            await coordinator.async_refresh()

            deleted_ids_on_release = {call.args[2] for call in delete.call_args_list[calls_before_release:]}
            assert hub_a_issue_id in deleted_ids_on_release
            assert (100, self.HUB_A_MID) not in coordinator._hub_disconnect_poll_counts
            assert self.HUB_A_MID not in coordinator.data["hub_connectivity"]

    @pytest.mark.asyncio
    async def test_a_push_during_a_gap_leaves_the_hub_absence_counter_untouched(self):
        """MQTT carries no enumeration information, so a push must not
        move _hub_absent_poll_counts or _last_poll_hub_keys, even while
        clearing the pushed child's own silent counter and issue exactly as
        it always has."""
        hub_b_with_child = {
            "mid": self.HUB_B_MID,
            "name": "HubB",
            "deviceName": f"dev{self.HUB_B_MID}",
            "productKey": "pk1",
            "homeName": "Home",
            "subDevices": [{"addr": 1, "model": MODEL_MOISTURE_SIMPLE, "name": "Sub1", "softVer": "1.0"}],
        }
        coordinator, client = self._build(hub_a_connected="1", hub_b_connected="1")
        client.get_devices_by_hid.return_value = [self._hub(self.HUB_A_MID, "HubA"), hub_b_with_child]
        # Hub B's child arrives but never reports (arrived-but-empty), so its
        # silent counter advances every poll.
        client.get_multiple_device_status.return_value = [
            {"mid": self.HUB_A_MID, "subDeviceStatus": [{"id": "connected", "value": "1"}]},
            {"mid": self.HUB_B_MID, "subDeviceStatus": []},
        ]

        with (
            patch.object(_repairs_module.ir, "async_create_issue"),
            patch.object(_repairs_module.ir, "async_delete_issue"),
        ):
            await coordinator.async_config_entry_first_refresh()
            await coordinator.async_refresh()

            # Hub A drops out; hub B's silent child keeps being tracked.
            client.get_devices_by_hid.return_value = [hub_b_with_child]
            client.get_multiple_device_status.return_value = [{"mid": self.HUB_B_MID, "subDeviceStatus": []}]
            await coordinator.async_refresh()

            absent_counts_before = dict(coordinator._hub_absent_poll_counts)
            hub_keys_before = set(coordinator._last_poll_hub_keys)
            assert (100, self.HUB_A_MID) in absent_counts_before

            coordinator.apply_push_update(self.HUB_B_MID, "D1", _MOISTURE_SIMPLE_PAYLOAD, device_ts=1700000000000)

            assert coordinator._hub_absent_poll_counts == absent_counts_before
            assert coordinator._last_poll_hub_keys == hub_keys_before
            sensor_key = _coord_module._sensor_key(100, self.HUB_B_MID, 1)
            assert sensor_key not in coordinator._silent_poll_counts

    @pytest.mark.asyncio
    async def test_a_healthy_sibling_hub_still_raises_its_own_card_during_another_hubs_gap(self):
        """Per-hub scoping on the connectivity surface, the mirror of the
        not-reporting door's sibling test.

        Suppression is scoped to the hub that actually went missing. Hub A
        vanishing from the device list must not stop hub B, which is still
        listed and still reporting, from advancing its own disconnect
        debounce and raising its own card. A global guard would freeze B and
        would still pass every other test in this class, since all of them
        assert only about A.
        """
        coordinator, client = self._build(hub_a_connected="1", hub_b_connected="1")
        hub_b_issue_id = hub_connectivity_issue_id(100, self.HUB_B_MID)

        with (
            patch.object(_repairs_module.ir, "async_create_issue") as create,
            patch.object(_repairs_module.ir, "async_delete_issue"),
        ):
            await coordinator.async_config_entry_first_refresh()

            # Hub A leaves the device list and stays gone; hub B remains
            # listed but starts reporting disconnected on the same poll.
            client.get_devices_by_hid.return_value = [self._hub(self.HUB_B_MID, "HubB")]
            self._set_connected(client, hub_a_connected=None, hub_b_connected="0")
            for _ in range(_coord_module.HUB_DISCONNECT_DEBOUNCE_POLLS):
                await coordinator.async_refresh()

            created_ids = {call.args[2] for call in create.call_args_list}
            assert hub_b_issue_id in created_ids
            assert coordinator._hub_disconnect_poll_counts[(100, self.HUB_B_MID)] == _coord_module.HUB_DISCONNECT_DEBOUNCE_POLLS
            # And A really was in its gap for the whole of it, so the
            # assertion above is about scoping rather than about A having
            # already been released.
            assert (100, self.HUB_A_MID) in coordinator._hub_absent_poll_counts

    @pytest.mark.asyncio
    async def test_a_totally_empty_device_list_never_advances_enumeration_state(self):
        """A total outage freezes the enumeration memory whatever else is
        being tracked, so both outage doors agree about the same event.

        The branch condition is "hubs or not (silent or disconnect)", which
        does not partition total outages cleanly: with nothing being
        debounced it is true, so an empty device list lands in the same
        branch a partial shrink does. Keying the freeze on the device list
        itself rather than on that branch is what stops a total outage from
        being processed as a partial shrink in a quiet installation while
        being frozen once a single device happens to be mid-debounce.

        Drives the quiet case specifically, since that is the one the branch
        condition sends down the shrink path.
        """
        coordinator, client = self._build(hub_a_connected="1", hub_b_connected="1")

        with (
            patch.object(_repairs_module.ir, "async_create_issue"),
            patch.object(_repairs_module.ir, "async_delete_issue"),
        ):
            await coordinator.async_config_entry_first_refresh()
            hub_keys_after_healthy_poll = set(coordinator._last_poll_hub_keys)
            assert hub_keys_after_healthy_poll == {(100, self.HUB_A_MID), (100, self.HUB_B_MID)}
            # Nothing is being debounced, which is what routes the empty list
            # below into the shrink branch rather than the total-outage one.
            assert not coordinator._silent_poll_counts
            assert not coordinator._hub_disconnect_poll_counts

            client.get_devices_by_hid.return_value = []
            client.get_multiple_device_status.return_value = []
            await coordinator.async_refresh()

            assert coordinator._hub_absent_poll_counts == {}
            assert coordinator._last_poll_hub_keys == hub_keys_after_healthy_poll

            # The pre-outage memory is intact, so a later partial list
            # computes its missing set against it rather than against the
            # outage.
            client.get_devices_by_hid.return_value = [self._hub(self.HUB_B_MID, "HubB")]
            self._set_connected(client, hub_a_connected=None, hub_b_connected="1")
            await coordinator.async_refresh()

            assert coordinator._hub_absent_poll_counts == {(100, self.HUB_A_MID): 1}


def _set_hub_connected(client, value, time_ms=None):
    """Mutate the next poll's connected entry on an already-built client.

    Omitting time_ms leaves the entry with no "time" key, which is what a
    firmware that reports connectivity without a change timestamp sends and
    what the ordering guard has to treat as unorderable.
    """
    entry = {"id": "connected", "value": value}
    if time_ms is not None:
        entry["time"] = time_ms
    client.get_multiple_device_status.return_value = [{"mid": 200, "subDeviceStatus": [entry]}]


def _build_hub_connectivity_coord(connected_value="1", time_ms=None):
    """Return (coordinator, client) wired the way __init__.py wires it.

    Shared by every hub connectivity class that drives a real construct ->
    first refresh -> further refresh timeline. The hub carries no subDevices
    on purpose: the not-reporting lifecycle shares the same
    ir.async_create_issue / ir.async_delete_issue mocks those classes assert
    call counts against, so a declared sub-device going silent for three
    consecutive polls would raise its own issue on those same mocks at
    exactly the poll asserted to be the only create call. Do not declare a
    sub-device here.
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
    _set_hub_connected(client, connected_value, time_ms)

    entry = MagicMock()
    entry.entry_id = "test_entry"
    entry.data = {CONF_HIDS: [100]}

    hass = MagicMock()
    hass.data = {}

    return _coord_module.RainPointCoordinator(hass, client, entry), client


class TestHubConnectivityDebounceRealTimeline:
    """Drives the real coordinator construct -> first refresh -> repeated
    refresh sequence, asserting between every step, rather than proving the
    debounce from an injected already-past-threshold coordinator.data
    snapshot -- the specific pattern that shipped two critical defects under
    100% branch coverage in a prior phase."""

    _build = staticmethod(_build_hub_connectivity_coord)
    _set_connected = staticmethod(_set_hub_connected)

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


class TestPollOnlyHubConnectivityParity:
    """With the push channel down and no pushed edge ever applied,
    hub connectivity behaves exactly as Phase 16 shipped it. This class
    does not duplicate TestHubConnectivityDebounceRealTimeline,
    TestHubConnectivityIntegration or TestHubConnectivitySurvivesDeviceListOutage
    (the per-behaviour proofs); it is the composed proof that the
    poll-side ordering guard changed none of them, driven with no apply_hub_push_update call
    anywhere in the class and no direct assignment to coordinator.data.

    _guard_hub_connectivity_order is instrumented rather than trusted: a
    spy wraps the real function for the duration of every poll-only
    sequence below and asserts it returned its polled argument on every
    single call, so a future change that made the guard fire without push
    history goes red here rather than silently altering poll behaviour."""

    _build = staticmethod(_build_hub_connectivity_coord)
    _set_connected = staticmethod(_set_hub_connected)

    @staticmethod
    def _spy_guard(calls):
        """Return a side_effect callable that delegates to the real guard and
        records (polled, result) for every invocation."""
        original = _coord_module._guard_hub_connectivity_order

        def _wrapped(polled, prior):
            result = original(polled, prior)
            calls.append((polled, result))
            return result

        return _wrapped

    @staticmethod
    def _assert_guard_always_returned_polled(calls):
        assert calls, "the guard must be called at least once for this assertion to mean anything"
        for polled, result in calls:
            assert result is polled

    @pytest.mark.asyncio
    async def test_full_raise_clear_reset_cycle_matches_phase_16_with_the_guard_inert(self):
        """Connected; three disconnected polls raise exactly one card; a
        fourth raises no second card; recovery clears and resets the
        counter; a fresh outage crosses the threshold again from zero --
        the same lifecycle TestHubConnectivityDebounceRealTimeline pins,
        composed here as one proof that the ordering guard changed none
        of it."""
        coordinator, client = self._build(connected_value="1")
        guard_calls: list[tuple[dict, dict]] = []

        with (
            patch.object(_repairs_module.ir, "async_create_issue") as create,
            patch.object(_repairs_module.ir, "async_delete_issue") as delete,
            patch.object(_coord_module, "_guard_hub_connectivity_order", side_effect=self._spy_guard(guard_calls)),
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
            await coordinator.async_refresh()  # poll 7: counter 2
            await coordinator.async_refresh()  # poll 8: counter 3 -> raise again
            assert create.call_count == 2

        self._assert_guard_always_returned_polled(guard_calls)

    @pytest.mark.asyncio
    async def test_unknown_poll_and_absent_status_move_neither_counter_nor_card_with_the_guard_inert(self):
        """An unknown poll (connected id missing entirely) is not evidence in
        either direction; an absent status (both fetch paths fail
        transiently) yields the unknown tri-state, never disconnected --
        Phase 16's absent-never-coerced-to-disconnected rule, unaffected by
        the guard when there is no push history to hold anything against."""
        coordinator, client = self._build(connected_value="1")
        guard_calls: list[tuple[dict, dict]] = []

        with (
            patch.object(_repairs_module.ir, "async_create_issue") as create,
            patch.object(_coord_module, "_guard_hub_connectivity_order", side_effect=self._spy_guard(guard_calls)),
        ):
            await coordinator.async_config_entry_first_refresh()

            self._set_connected(client, "0")
            await coordinator.async_refresh()
            await coordinator.async_refresh()
            assert coordinator._hub_disconnect_poll_counts[(100, 200)] == 2
            assert create.call_count == 0

            # Unknown: the connected id is missing entirely this poll.
            client.get_multiple_device_status.return_value = [{"mid": 200, "subDeviceStatus": []}]
            await coordinator.async_refresh()
            assert coordinator._hub_disconnect_poll_counts[(100, 200)] == 2
            assert create.call_count == 0
            assert coordinator.data["hub_connectivity"][200]["state"] == _coord_module.HUB_CONNECTIVITY_UNKNOWN

            # Absent: both fetch paths fail transiently for this poll, so
            # this hub's status was never obtained at all.
            client.get_multiple_device_status.side_effect = aiohttp.ClientError("boom")
            client.get_device_status.side_effect = aiohttp.ClientError("boom")
            await coordinator.async_refresh()
            assert coordinator._hub_disconnect_poll_counts[(100, 200)] == 2
            assert create.call_count == 0
            assert coordinator.data["hub_connectivity"][200]["state"] == _coord_module.HUB_CONNECTIVITY_UNKNOWN

            # The counter resumes from where it left off, proving neither
            # gap moment reset or advanced it.
            client.get_multiple_device_status.side_effect = None
            client.get_device_status.side_effect = None
            self._set_connected(client, "0")
            await coordinator.async_refresh()
            assert coordinator._hub_disconnect_poll_counts[(100, 200)] == 3
            assert create.call_count == 1

        self._assert_guard_always_returned_polled(guard_calls)


class TestHubConnectivityPushClearInterleavedTimeline:
    """Raise and push-clear interleaved over a real driven timeline: raising the card
    stays poll-counted only, a pushed reconnect clears it and the counter
    immediately, and a hub that goes down again starts a fresh three-poll
    count. Companion to tests/test_valve.py's
    TestValveAvailabilityPushedReconnect, which carries every valve-
    availability and hub_connected-attribute assertion for the same pushed
    edge instead of duplicating them here.

    This class's fixture deliberately carries no subDevices, the reason
    _build_hub_connectivity_coord records: the not-reporting lifecycle
    shares the same ir.async_create_issue / ir.async_delete_issue mocks this
    class asserts call counts against, and a declared sub-device that goes
    silent for three consecutive polls would raise its own issue on those
    same mocks at exactly the poll this class asserts is the only create
    call. Do not declare a sub-device here and do not merge this class with
    TestValveAvailabilityPushedReconnect."""

    # The third pipe-delimited field of SAMPLE_HUB_RECONNECT_FRAME
    # (tests/payload_samples.py), reused here as the ordering key for a
    # driven push timeline rather than re-deriving new literals. Later pushes
    # advance the moment by whole seconds so the ordering guard is genuinely
    # exercised, not bypassed by an untouched value.
    _RECONNECT_TS = 1785523062039
    _DISCONNECT_TS_1 = _RECONNECT_TS + 1000
    _DISCONNECT_TS_2 = _RECONNECT_TS + 2000

    _build = staticmethod(_build_hub_connectivity_coord)
    _set_connected = staticmethod(_set_hub_connected)

    @staticmethod
    def _push_hub_edge(coordinator, connected, changed_ts):
        """Dispatch a hub connectivity edge into apply_hub_push_update via
        class-level dispatch, matching the repo's standing test idiom."""
        _coord_module.RainPointCoordinator.apply_hub_push_update(coordinator, 200, connected, changed_ts)

    @pytest.mark.asyncio
    async def test_pushed_reconnect_clears_immediately_and_the_next_outage_restarts_the_count(self):
        """The full interleaved sequence: three disconnected polls raise the
        card, a pushed reconnect clears it and the counter before any further
        poll, back-to-back pushed disconnects flip the entity but raise
        nothing and touch no counter, and a fresh three-poll outage is
        required to raise the card a second time -- proving the counter
        genuinely restarted rather than resumed."""
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

            # The pushed reconnect: cleared before any further poll runs.
            deletes_before_push = delete.call_count
            self._push_hub_edge(coordinator, True, self._RECONNECT_TS)
            assert delete.call_count == deletes_before_push + 1
            assert (100, 200) not in coordinator._hub_disconnect_poll_counts
            assert coordinator.data["hub_connectivity"][200]["state"] == _coord_module.HUB_CONNECTED

            # A pushed disconnect delivered mid-sequence flips the entity
            # immediately but must not touch the counter or raise a card, no
            # matter how many arrive back to back.
            self._push_hub_edge(coordinator, False, self._DISCONNECT_TS_1)
            assert coordinator.data["hub_connectivity"][200]["state"] == _coord_module.HUB_DISCONNECTED
            assert (100, 200) not in coordinator._hub_disconnect_poll_counts
            assert create.call_count == 1

            self._push_hub_edge(coordinator, False, self._DISCONNECT_TS_2)
            assert coordinator.data["hub_connectivity"][200]["state"] == _coord_module.HUB_DISCONNECTED
            assert (100, 200) not in coordinator._hub_disconnect_poll_counts
            assert create.call_count == 1

            # The second outage: three fresh disconnected polls are required
            # to raise again, proving the counter genuinely restarted at
            # zero rather than resuming from wherever the pushed edges left
            # it.
            self._set_connected(client, "0")
            await coordinator.async_refresh()  # poll 4 disconnected: counter 1
            assert coordinator._hub_disconnect_poll_counts[(100, 200)] == 1
            assert create.call_count == 1

            await coordinator.async_refresh()  # poll 5 disconnected: counter 2
            assert coordinator._hub_disconnect_poll_counts[(100, 200)] == 2
            assert create.call_count == 1

            await coordinator.async_refresh()  # poll 6 disconnected: counter 3 -> raise again
            assert coordinator._hub_disconnect_poll_counts[(100, 200)] == 3
            assert create.call_count == 2


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

    def test_sub_device_push_leaves_hub_connectivity_object_identical(self):
        """Only the connectivity frame changes connectivity. A pushed
        sub-device reading must not move hub_connectivity in either
        direction, even though it updates the sensor entry it targets --
        silence cannot imply disconnected, and the rule's inverse (a fresh
        reading implies connected) would be equally meaningless. Matches the
        repo's standing never-write-state-it-has-not-read discipline."""
        hub = _push_hub()
        coord = _seed_push_coord(hub, sensors={"100_200_1": {"data": None}})
        connectivity = {200: {"state": _coord_module.HUB_CONNECTED, "changed_at": "x", "state_raw": "raw"}}
        coord.data["hub_connectivity"] = connectivity
        device_ts = int(datetime(2024, 6, 1, tzinfo=UTC).timestamp() * 1000)

        _APPLY(coord, 200, "D1", SAMPLE_HTV245_TLV_PAYLOAD, device_ts)

        assert coord.data["hub_connectivity"] is connectivity
        assert coord.data["sensors"]["100_200_1"]["data"] is not None

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

    def test_a_push_survives_a_hub_carrying_an_unusable_sub_device_record(self):
        """The push path must tolerate what the poll now stores.

        Before the poll learned to skip an unusable record it raised on one,
        so self.data could never hold it and every consumer inherited that
        guarantee for free. The poll is tolerant now, so the record does
        reach coordinator.data["hubs"], and a raw index here would raise
        KeyError on paho's callback thread rather than in the poll. Reverting
        _sub_devices_by_addr at that call site fails this test.
        """
        hub = _push_hub(addr=1)
        hub["subDevices"].append({"model": MODEL_VALVE_245, "name": "NoAddr", "softVer": "1.0"})
        coord = _seed_push_coord(hub, sensors={"100_200_1": {"data": None}})

        _APPLY(coord, 200, "D1", SAMPLE_HTV245_TLV_PAYLOAD, 1717200000000)

        assert coord.data["sensors"]["100_200_1"]["data"] is not None
        coord.async_update_listeners.assert_called_once()

    def test_a_push_survives_a_status_list_carrying_a_non_dict_entry(self):
        """The merge target is the cloud's own subDeviceStatus as the poll
        stored it, and the poll no longer raises on a non-dict entry, so one
        can be sitting in the list this merge iterates. Calling .get on it
        would raise AttributeError; the entry is skipped instead, and the
        push still lands.
        """
        hub = _push_hub(addr=1)
        coord = _seed_push_coord(
            hub,
            sensors={"100_200_1": {"data": None}},
            status={200: {"subDeviceStatus": ["not-a-dict", {"id": "D1", "value": "old", "time": 1}]}},
        )

        _APPLY(coord, 200, "D1", SAMPLE_HTV245_TLV_PAYLOAD, 1717200000000)

        sub_status = coord.data["status"][200]["subDeviceStatus"]
        assert sub_status[0] == "not-a-dict"
        assert sub_status[1]["value"] == SAMPLE_HTV245_TLV_PAYLOAD
        assert coord.data["sensors"]["100_200_1"]["data"] is not None


class TestChangedAtDatetime:
    """_changed_at_datetime: the ordering primitive both connectivity guards
    consume -- apply_hub_push_update's push-side guard and
    _guard_hub_connectivity_order's poll-side one. Tested directly here so
    every degradation case is pinned without a driving poll."""

    def test_none_record_yields_none(self):
        assert _coord_module._changed_at_datetime(None) is None

    def test_empty_record_yields_none(self):
        assert _coord_module._changed_at_datetime({}) is None

    def test_non_string_changed_at_yields_none(self):
        assert _coord_module._changed_at_datetime({"changed_at": 12345}) is None

    def test_unparseable_string_yields_none(self):
        assert _coord_module._changed_at_datetime({"changed_at": "not-a-timestamp"}) is None

    def test_valid_iso_string_parses(self):
        record = {"changed_at": "2026-07-31T18:17:30.011000+00:00"}
        assert _coord_module._changed_at_datetime(record) == datetime.fromisoformat("2026-07-31T18:17:30.011000+00:00")

    def test_offsetless_string_is_assumed_utc(self):
        """No writer emits one, but a naive result would raise TypeError at
        both ordering sites rather than degrade, so it is normalized here."""
        parsed = _coord_module._changed_at_datetime({"changed_at": "2026-07-31T18:17:30.011000"})
        assert parsed == datetime.fromisoformat("2026-07-31T18:17:30.011000+00:00")
        assert parsed.tzinfo is UTC


_APPLY_HUB = _coord_module.RainPointCoordinator.apply_hub_push_update


class TestApplyHubPushUpdate:
    """apply_hub_push_update: the second sanctioned push entry point.
    Copy-on-write merge of one hub connectivity record, notifying listeners
    without resetting the poll timer, and dropping misses via a ladder that
    mirrors apply_push_update's."""

    def test_push_before_first_poll_is_dropped(self):
        coord, _ = _make_coord()
        coord.data = None
        coord.async_update_listeners = MagicMock()

        _APPLY_HUB(coord, 200, False, 1717200000000)

        coord.async_update_listeners.assert_not_called()
        coord._hub_connectivity_issues.async_clear.assert_not_called()

    def test_unknown_mid_is_dropped_without_mutating_or_notifying(self):
        hub = _push_hub()
        coord = _seed_push_coord(hub, sensors={})
        original = coord.data

        _APPLY_HUB(coord, 999, False, 1717200000000)

        assert coord.data is original
        coord.async_update_listeners.assert_not_called()
        coord._hub_connectivity_issues.async_clear.assert_not_called()

    def test_non_hub_record_is_dropped_without_mutating_or_notifying(self):
        """The Bluetooth wrapper record (every identity field empty) is found
        by mid but fails is_hub_record, so it contributes no connectivity
        record -- writing one here would create a record the next poll deletes."""
        wrapper_hub = {"hid": 100, "mid": 346965, "did": "", "mac": "", "productKey": "", "model": "", "name": ""}
        coord = _seed_push_coord(wrapper_hub, sensors={})
        original = coord.data

        _APPLY_HUB(coord, 346965, True, 1717200000000)

        assert coord.data is original
        coord.async_update_listeners.assert_not_called()
        coord._hub_connectivity_issues.async_clear.assert_not_called()

    def test_unconvertible_timestamp_declines_the_increment(self):
        """changed_ts=10**20 is out of datetime.fromtimestamp's representable
        range: the held record is left byte-identical and listeners
        are not notified, deliberately diverging from the poll path's
        unknown-on-malformed rule."""
        hub = _push_hub()
        coord = _seed_push_coord(hub, sensors={})
        coord.data["hub_connectivity"] = {200: {"state": _coord_module.HUB_CONNECTED, "changed_at": "x", "state_raw": "raw"}}
        original_record = coord.data["hub_connectivity"][200]

        _APPLY_HUB(coord, 200, False, 10**20)

        assert coord.data["hub_connectivity"][200] is original_record
        coord.async_update_listeners.assert_not_called()
        coord._hub_connectivity_issues.async_clear.assert_not_called()

    def test_merge_writes_three_key_record_and_preserves_sibling_identity(self):
        """The merge writes exactly state/changed_at/state_raw, carries
        state_raw forward from the held record, preserves sibling mid
        object identity, carries hubs by reference, and notifies listeners
        exactly once without resetting the poll timer."""
        hub = _push_hub()
        coord = _seed_push_coord(hub, sensors={})
        sibling_record = {"state": _coord_module.HUB_CONNECTED, "changed_at": "y", "state_raw": "sib"}
        coord.data["hub_connectivity"] = {
            200: {"state": _coord_module.HUB_CONNECTED, "changed_at": "old", "state_raw": "held-raw"},
            999: sibling_record,
        }
        original_hubs = coord.data["hubs"]

        _APPLY_HUB(coord, 200, False, 1717200000000)

        record = coord.data["hub_connectivity"][200]
        assert record == {
            "state": _coord_module.HUB_DISCONNECTED,
            "changed_at": datetime.fromtimestamp(1717200000000 / 1000, tz=UTC).isoformat(),
            "state_raw": "held-raw",
        }
        assert coord.data["hub_connectivity"][999] is sibling_record
        assert coord.data["hubs"] is original_hubs
        coord.async_update_listeners.assert_called_once()
        coord.async_set_updated_data.assert_not_called()

    def test_merge_with_no_held_record_writes_state_raw_none(self):
        """A first-ever push for a mid with no prior connectivity record
        writes state_raw None rather than raising on the missing key."""
        hub = _push_hub()
        coord = _seed_push_coord(hub, sensors={})

        _APPLY_HUB(coord, 200, True, 1717200000000)

        assert coord.data["hub_connectivity"][200]["state_raw"] is None
        assert coord.data["hub_connectivity"][200]["state"] == _coord_module.HUB_CONNECTED

    def test_dropped_push_leaves_hub_disconnect_poll_counts_untouched(self):
        """A drop must not touch the counter or the issue set either way,
        regardless of whether the frame it dropped was connected or not."""
        hub = _push_hub()
        coord = _seed_push_coord(hub, sensors={})
        coord._hub_disconnect_poll_counts[(100, 200)] = 2

        _APPLY_HUB(coord, 999, False, 1717200000000)

        assert coord._hub_disconnect_poll_counts.get((100, 200)) == 2
        coord._hub_connectivity_issues.async_clear.assert_not_called()

    def test_connected_edge_pops_the_counter_and_clears_the_issue_once(self):
        """A pushed connected edge against a hub whose counter
        reads 2 pops that key and clears the Repairs card exactly once."""
        hub = _push_hub()
        coord = _seed_push_coord(hub, sensors={})
        coord._hub_disconnect_poll_counts[(100, 200)] = 2

        _APPLY_HUB(coord, 200, True, 1717200000000)

        assert (100, 200) not in coord._hub_disconnect_poll_counts
        coord._hub_connectivity_issues.async_clear.assert_called_once_with(100, 200)

    def test_disconnected_edge_leaves_the_counter_and_issue_untouched(self):
        """Raising the card stays poll-counted only, so a pushed
        disconnect must never increment the counter or clear anything."""
        hub = _push_hub()
        coord = _seed_push_coord(hub, sensors={})
        coord._hub_disconnect_poll_counts[(100, 200)] = 2

        _APPLY_HUB(coord, 200, False, 1717200000000)

        assert coord._hub_disconnect_poll_counts[(100, 200)] == 2
        coord._hub_connectivity_issues.async_clear.assert_not_called()

    @pytest.mark.parametrize(
        ("reason", "mid", "seed", "wrapper"),
        [
            ("before first poll", 200, False, False),
            ("unknown mid", 999, True, False),
            ("non-hub record", 346965, True, True),
        ],
    )
    def test_dropped_push_notifies_no_listener(self, reason, mid, seed, wrapper):
        """Each early-return drop path must not notify listeners or clear the
        issue, since none of them reach the merge that follows those drops."""
        if wrapper:
            hub = {"hid": 100, "mid": 346965, "did": "", "mac": "", "productKey": "", "model": "", "name": ""}
            coord = _seed_push_coord(hub, sensors={})
        elif seed:
            coord = _seed_push_coord(_push_hub(), sensors={})
        else:
            coord, _ = _make_coord()
            coord.data = None
            coord.async_update_listeners = MagicMock()

        _APPLY_HUB(coord, mid, False, 1717200000000)

        assert coord.async_update_listeners.call_count == 0, reason
        assert coord._hub_connectivity_issues.async_clear.call_count == 0, reason


class TestApplyHubPushUpdateOrderingGuard:
    """apply_hub_push_update's ordering guard: the pushed
    change moment is compared against the held record's changed_at, ordering
    both channels against one identical value. Strictly newer wins; an equal
    or older moment is a no-op; a held moment that cannot be established
    (None, absent, or unparseable) always falls through to apply."""

    def test_strictly_older_pushed_edge_is_dropped_without_merging_or_notifying(self):
        """The example from the plan: held changed_at is the 18:37:42
        reconnect moment; the 18:17:30 disconnect frame arrives late."""
        hub = _push_hub()
        coord = _seed_push_coord(hub, sensors={})
        held_record = {
            "state": _coord_module.HUB_CONNECTED,
            "changed_at": SAMPLE_HUB_RECONNECT_CHANGED_AT_ISO,
            "state_raw": "held-raw",
        }
        coord.data["hub_connectivity"] = {200: held_record}

        _APPLY_HUB(coord, 200, False, 1785521850011)

        assert coord.data["hub_connectivity"][200] is held_record
        coord.async_update_listeners.assert_not_called()
        coord._hub_connectivity_issues.async_clear.assert_not_called()

    def test_equal_pushed_edge_is_dropped_without_merging_or_notifying(self, caplog):
        """An equal moment is the same edge already held -- the common case
        where the poll picks up the very edge the push already delivered."""
        hub = _push_hub()
        coord = _seed_push_coord(hub, sensors={})
        held_record = {
            "state": _coord_module.HUB_DISCONNECTED,
            "changed_at": SAMPLE_HUB_DISCONNECT_CHANGED_AT_ISO,
            "state_raw": "held-raw",
        }
        coord.data["hub_connectivity"] = {200: held_record}

        with caplog.at_level(logging.DEBUG, logger="custom_components.rainpoint.coordinator"):
            _APPLY_HUB(coord, 200, False, 1785521850011)

        assert coord.data["hub_connectivity"][200] is held_record
        coord.async_update_listeners.assert_not_called()
        coord._hub_connectivity_issues.async_clear.assert_not_called()
        assert any("already-recorded" in r.getMessage() or "already recorded" in r.getMessage() for r in caplog.records)

    def test_held_changed_at_none_falls_through_and_applies(self):
        """There is no recorded moment for the pushed edge to be older than."""
        hub = _push_hub()
        coord = _seed_push_coord(hub, sensors={})
        coord.data["hub_connectivity"] = {
            200: {"state": _coord_module.HUB_CONNECTED, "changed_at": None, "state_raw": "held-raw"}
        }

        _APPLY_HUB(coord, 200, False, 1785521850011)

        record = coord.data["hub_connectivity"][200]
        assert record["state"] == _coord_module.HUB_DISCONNECTED
        assert record["changed_at"] == SAMPLE_HUB_DISCONNECT_CHANGED_AT_ISO
        coord.async_update_listeners.assert_called_once()

    def test_no_held_record_falls_through_and_applies(self):
        hub = _push_hub()
        coord = _seed_push_coord(hub, sensors={})

        _APPLY_HUB(coord, 200, False, 1785521850011)

        assert coord.data["hub_connectivity"][200]["state"] == _coord_module.HUB_DISCONNECTED
        coord.async_update_listeners.assert_called_once()

    def test_strictly_newer_pushed_edge_against_a_valid_older_held_moment_applies(self):
        """ "Strictly newer wins" from the winning side: a held record
        with a genuine, parseable, older changed_at is not a barrier."""
        hub = _push_hub()
        coord = _seed_push_coord(hub, sensors={})
        coord.data["hub_connectivity"] = {
            200: {
                "state": _coord_module.HUB_DISCONNECTED,
                "changed_at": SAMPLE_HUB_DISCONNECT_CHANGED_AT_ISO,
                "state_raw": "held-raw",
            }
        }

        _APPLY_HUB(coord, 200, True, 1785523062039)

        record = coord.data["hub_connectivity"][200]
        assert record["state"] == _coord_module.HUB_CONNECTED
        assert record["changed_at"] == SAMPLE_HUB_RECONNECT_CHANGED_AT_ISO
        coord.async_update_listeners.assert_called_once()

    def test_unparseable_held_changed_at_falls_through_and_applies(self):
        """Refusing here would make push a permanent no-op on any firmware
        whose connected entry carries no usable time."""
        hub = _push_hub()
        coord = _seed_push_coord(hub, sensors={})
        coord.data["hub_connectivity"] = {
            200: {"state": _coord_module.HUB_CONNECTED, "changed_at": "not-a-timestamp", "state_raw": "held-raw"}
        }

        _APPLY_HUB(coord, 200, False, 1785521850011)

        record = coord.data["hub_connectivity"][200]
        assert record["state"] == _coord_module.HUB_DISCONNECTED
        coord.async_update_listeners.assert_called_once()


class TestGuardHubConnectivityOrder:
    """_guard_hub_connectivity_order: the poll-side ordering guard
    applied at the single _async_update_data call site. A strictly older
    poll record must not overwrite a newer held one; state_raw always takes
    the latest polled value regardless of whether the guard fired."""

    def test_older_poll_is_held_off_by_a_newer_held_record(self):
        """The example from the plan: held changed_at is the 18:37:42
        reconnect moment and stays connected; the 18:17:30 disconnect poll
        arrives late and only state_raw comes from it."""
        polled = _coord_module._read_hub_connectivity(
            {
                "subDeviceStatus": [
                    {"id": "connected", "value": "0", "time": 1785521850011},
                    {"id": "state", "value": "poll-raw"},
                ]
            }
        )
        prior = {
            "state": _coord_module.HUB_CONNECTED,
            "changed_at": SAMPLE_HUB_RECONNECT_CHANGED_AT_ISO,
            "state_raw": "held-raw",
        }

        result = _coord_module._guard_hub_connectivity_order(polled, prior)

        assert result["state"] == _coord_module.HUB_CONNECTED
        assert result["changed_at"] == SAMPLE_HUB_RECONNECT_CHANGED_AT_ISO
        assert result["state_raw"] == "poll-raw"

    def test_strictly_newer_poll_wins_whole(self):
        """A poll whose moment is genuinely newer than the held record is
        not a barrier -- the polled record wins whole, all three keys."""
        polled = _coord_module._read_hub_connectivity(
            {"subDeviceStatus": [{"id": "connected", "value": "1", "time": 1785523062039}]}
        )
        prior = {
            "state": _coord_module.HUB_DISCONNECTED,
            "changed_at": SAMPLE_HUB_DISCONNECT_CHANGED_AT_ISO,
            "state_raw": "held-raw",
        }

        result = _coord_module._guard_hub_connectivity_order(polled, prior)

        assert result is polled

    def test_exactly_equal_moment_poll_wins_whole(self):
        """An equal moment is the same edge already held; only strictly
        older is held off."""
        polled = _coord_module._read_hub_connectivity(
            {"subDeviceStatus": [{"id": "connected", "value": "0", "time": 1785521850011}]}
        )
        prior = {
            "state": _coord_module.HUB_DISCONNECTED,
            "changed_at": SAMPLE_HUB_DISCONNECT_CHANGED_AT_ISO,
            "state_raw": "held-raw",
        }

        result = _coord_module._guard_hub_connectivity_order(polled, prior)

        assert result is polled

    def test_prior_changed_at_none_polled_wins_whole(self):
        """A held record with no recorded moment has nothing for the polled
        moment to be older than."""
        polled = _coord_module._read_hub_connectivity(
            {"subDeviceStatus": [{"id": "connected", "value": "0", "time": 1785521850011}]}
        )
        prior = {"state": _coord_module.HUB_CONNECTED, "changed_at": None, "state_raw": "held-raw"}

        result = _coord_module._guard_hub_connectivity_order(polled, prior)

        assert result is polled

    def test_prior_changed_at_unparseable_polled_wins_whole(self):
        """An unparseable held changed_at degrades the same as a missing one."""
        polled = _coord_module._read_hub_connectivity(
            {"subDeviceStatus": [{"id": "connected", "value": "0", "time": 1785521850011}]}
        )
        prior = {"state": _coord_module.HUB_CONNECTED, "changed_at": "not-a-timestamp", "state_raw": "held-raw"}

        result = _coord_module._guard_hub_connectivity_order(polled, prior)

        assert result is polled

    def test_polled_moment_none_with_a_valid_held_moment_polled_wins_whole(self):
        """The poll's connected entry carries no usable time at all, so the
        polled moment is None; unordered is not strictly older."""
        polled = _coord_module._read_hub_connectivity({"subDeviceStatus": [{"id": "connected", "value": "0"}]})
        prior = {
            "state": _coord_module.HUB_CONNECTED,
            "changed_at": SAMPLE_HUB_RECONNECT_CHANGED_AT_ISO,
            "state_raw": "held-raw",
        }

        result = _coord_module._guard_hub_connectivity_order(polled, prior)

        assert result is polled

    def test_absent_status_wins_whole_even_over_a_newer_held_record(self):
        """The unknown record from an absent status wins whole -- Phase 16's
        absent-never-disconnected rule is not something this guard may
        quietly change."""
        polled = _coord_module._read_hub_connectivity(_coord_module.STATUS_ABSENT)
        prior = {
            "state": _coord_module.HUB_CONNECTED,
            "changed_at": SAMPLE_HUB_RECONNECT_CHANGED_AT_ISO,
            "state_raw": "held-raw",
        }

        result = _coord_module._guard_hub_connectivity_order(polled, prior)

        assert result is polled

    def test_no_prior_snapshot_at_all_polled_wins_and_no_lookup_raises(self):
        """The first poll after startup has no prior snapshot at all."""
        polled = _coord_module._read_hub_connectivity(
            {"subDeviceStatus": [{"id": "connected", "value": "1", "time": 1785523062039}]}
        )

        result = _coord_module._guard_hub_connectivity_order(polled, None)

        assert result is polled

    @pytest.mark.asyncio
    async def test_guard_fires_identically_on_the_fallback_fetch_path(self):
        """Both fetch paths funnel into status_by_mid at the one guard call
        site in _async_update_data (mirrors test_update_fallback_to_individual_calls),
        so a held pushed edge survives a lagging poll delivered via
        _fallback_per_hub_status exactly as it would via multipleDeviceStatus."""
        coord, client = _make_coord()
        coord.data = {
            "hub_connectivity": {
                200: {
                    "state": _coord_module.HUB_CONNECTED,
                    "changed_at": SAMPLE_HUB_RECONNECT_CHANGED_AT_ISO,
                    "state_raw": "held-raw",
                }
            }
        }
        client.get_devices_by_hid.return_value = [_make_hub(hid=100, mid=200)]
        client.get_multiple_device_status.side_effect = aiohttp.ClientError("transport error")
        client.get_device_status.return_value = {"subDeviceStatus": [{"id": "connected", "value": "0", "time": 1785521850011}]}

        result = await _run(coord)

        record = result["hub_connectivity"][200]
        assert record["state"] == _coord_module.HUB_CONNECTED
        assert record["changed_at"] == SAMPLE_HUB_RECONNECT_CHANGED_AT_ISO


class TestGuardHubConnectivityOrderRealTimeline:
    """The poll-side guard proven against a real prior snapshot -- construct ->
    first refresh -> a pushed edge -> a further poll -- rather than a
    hand-built prior dict, for at least three of the pure-function cases
    TestGuardHubConnectivityOrder already covers directly."""

    # Reuses the captured frame's own moments (tests/payload_samples.py)
    # rather than deriving new literals: the later one matches
    # SAMPLE_HUB_RECONNECT_CHANGED_AT_ISO, the earlier one matches
    # SAMPLE_HUB_DISCONNECT_CHANGED_AT_ISO.
    _NEWER_TS = 1785523062039
    _OLDER_TS = 1785521850011
    _EVEN_NEWER_TS = _NEWER_TS + 5000

    _build = staticmethod(_build_hub_connectivity_coord)
    _set_connected = staticmethod(_set_hub_connected)

    @pytest.mark.asyncio
    async def test_a_lagging_disconnected_poll_cannot_revert_a_newer_pushed_reconnect(self):
        """Case 1: a held pushed connected record survives a poll whose
        connected time is strictly older."""
        coordinator, client = self._build()
        await coordinator.async_config_entry_first_refresh()

        _coord_module.RainPointCoordinator.apply_hub_push_update(coordinator, 200, True, self._NEWER_TS)
        assert coordinator.data["hub_connectivity"][200]["state"] == _coord_module.HUB_CONNECTED

        self._set_connected(client, "0", time_ms=self._OLDER_TS)
        await coordinator.async_refresh()

        record = coordinator.data["hub_connectivity"][200]
        assert record["state"] == _coord_module.HUB_CONNECTED
        assert record["changed_at"] == SAMPLE_HUB_RECONNECT_CHANGED_AT_ISO

    @pytest.mark.asyncio
    async def test_a_strictly_newer_poll_overrides_a_held_pushed_edge(self):
        """Case 2: a poll whose moment is genuinely newer than the held
        pushed edge wins whole, including state_raw."""
        coordinator, client = self._build()
        await coordinator.async_config_entry_first_refresh()

        _coord_module.RainPointCoordinator.apply_hub_push_update(coordinator, 200, False, self._OLDER_TS)
        assert coordinator.data["hub_connectivity"][200]["state"] == _coord_module.HUB_DISCONNECTED

        self._set_connected(client, "1", time_ms=self._NEWER_TS)
        await coordinator.async_refresh()

        record = coordinator.data["hub_connectivity"][200]
        assert record["state"] == _coord_module.HUB_CONNECTED
        assert record["changed_at"] == SAMPLE_HUB_RECONNECT_CHANGED_AT_ISO

    @pytest.mark.asyncio
    async def test_the_first_poll_after_startup_has_no_prior_snapshot_and_does_not_raise(self):
        """Case 3: no prior snapshot exists at all on the very first poll --
        the guard's prior_connectivity.get(mid) lookup on an empty dict must
        not raise, and the polled record wins."""
        coordinator, _client = self._build()

        await coordinator.async_config_entry_first_refresh()

        assert coordinator.data["hub_connectivity"][200]["state"] == _coord_module.HUB_CONNECTED


class TestHubConnectivityGuardComposition:
    """The poll-side guard composes with _sync_hub_connectivity_issues in both
    directions (the fifth point of _guard_hub_connectivity_order's
    docstring). Deliberately separate from
    TestHubConnectivityPushClearInterleavedTimeline, which pins the
    push-side clear rather than the poll-side guard's
    interaction with the debounce reconcile.

    This class's fixture carries no subDevices, the reason
    _build_hub_connectivity_coord records: the not-reporting lifecycle
    shares the same ir.async_create_issue/async_delete_issue mocks these
    tests assert call counts against."""

    _NEWER_TS = 1785523062039
    _OLDER_TS = 1785521850011
    _EVEN_NEWER_TS = _NEWER_TS + 5000

    _build = staticmethod(_build_hub_connectivity_coord)
    _set_connected = staticmethod(_set_hub_connected)

    @pytest.mark.asyncio
    async def test_held_pushed_connected_against_lagging_disconnected_polls_raises_no_card(self):
        """Direction one: a held pushed connected record leaves
        _hub_disconnect_poll_counts without an incremented entry for that
        hub across three consecutive lagging disconnected polls, and raises
        no card."""
        coordinator, client = self._build()

        with (
            patch.object(_repairs_module.ir, "async_create_issue") as create,
            patch.object(_repairs_module.ir, "async_delete_issue"),
        ):
            await coordinator.async_config_entry_first_refresh()
            assert create.call_count == 0

            _coord_module.RainPointCoordinator.apply_hub_push_update(coordinator, 200, True, self._NEWER_TS)
            assert coordinator.data["hub_connectivity"][200]["state"] == _coord_module.HUB_CONNECTED

            for _ in range(3):
                self._set_connected(client, "0", time_ms=self._OLDER_TS)
                await coordinator.async_refresh()

            assert (100, 200) not in coordinator._hub_disconnect_poll_counts
            assert create.call_count == 0
            assert coordinator.data["hub_connectivity"][200]["state"] == _coord_module.HUB_CONNECTED

    @pytest.mark.asyncio
    async def test_held_pushed_disconnected_against_lagging_connected_polls_raises_on_the_third(self):
        """Direction two, the mirror, and the one most worth pinning: a held
        pushed disconnected record drives the poll-counted debounce even
        though no poll independently observed the hub as disconnected. The
        counter reaches 1, 2, then 3 across three lagging connected polls,
        a card is raised exactly once on the third, and a fourth poll whose
        connected time has advanced past the pushed moment ends the hold,
        wins whole, clears the card and removes the counter key -- proving
        the hold is bounded by the REST timestamp rather than permanent."""
        coordinator, client = self._build()

        with (
            patch.object(_repairs_module.ir, "async_create_issue") as create,
            patch.object(_repairs_module.ir, "async_delete_issue") as delete,
        ):
            await coordinator.async_config_entry_first_refresh()
            assert create.call_count == 0

            _coord_module.RainPointCoordinator.apply_hub_push_update(coordinator, 200, False, self._NEWER_TS)
            assert coordinator.data["hub_connectivity"][200]["state"] == _coord_module.HUB_DISCONNECTED

            self._set_connected(client, "1", time_ms=self._OLDER_TS)
            await coordinator.async_refresh()  # poll 1: held disconnected, counter 1
            assert coordinator._hub_disconnect_poll_counts[(100, 200)] == 1
            assert create.call_count == 0

            await coordinator.async_refresh()  # poll 2: held disconnected, counter 2
            assert coordinator._hub_disconnect_poll_counts[(100, 200)] == 2
            assert create.call_count == 0

            await coordinator.async_refresh()  # poll 3: held disconnected, counter 3 -> raise
            assert coordinator._hub_disconnect_poll_counts[(100, 200)] == 3
            assert create.call_count == 1
            _hass, _domain, issue_id = create.call_args.args
            assert issue_id == hub_connectivity_issue_id(100, 200)

            # This is the poll that no longer lags: its own connected time
            # has advanced past the pushed disconnect moment, so the guard
            # stops holding and the connected record wins whole.
            deletes_before = delete.call_count
            self._set_connected(client, "1", time_ms=self._EVEN_NEWER_TS)
            await coordinator.async_refresh()  # poll 4: wins whole, clears

            assert coordinator.data["hub_connectivity"][200]["state"] == _coord_module.HUB_CONNECTED
            assert (100, 200) not in coordinator._hub_disconnect_poll_counts
            assert delete.call_count > deletes_before


class TestHubConnectivityPushDuringInFlightPoll:
    """Regression: a hub connectivity push landing while a poll is
    suspended inside its awaited fetch must survive that poll's completion,
    not get silently discarded when DataUpdateCoordinator replaces self.data
    wholesale with the poll's return value.

    _async_update_data reads its prior_connectivity snapshot as late as
    possible -- after _fetch_status_by_mid's await, immediately before the
    per-hub loop -- specifically so a push landing during that awaited
    network round-trip is visible to the ordering guard. Hoisting the read
    any earlier (the pre-fix shape, before the await) lets the guard evaluate
    a stale prior, and this test's push gets silently reverted."""

    _BASE_TS = 1785521850011  # matches SAMPLE_HUB_DISCONNECT_CHANGED_AT_ISO
    _PUSH_TS = _BASE_TS + 100_000  # strictly newer than _BASE_TS

    @classmethod
    def _build(cls):
        """The shared builder, pinned to a connected status at _BASE_TS.

        Unlike the other connectivity classes this one needs the first
        refresh to carry a change timestamp, because the poll it races
        against reports that same lagging moment.
        """
        return _build_hub_connectivity_coord(connected_value="1", time_ms=cls._BASE_TS)

    @pytest.mark.asyncio
    async def test_a_push_landing_mid_await_survives_the_completed_poll(self):
        """Construct -> first refresh (connected at _BASE_TS) -> start a
        second poll whose fetch call blocks on an event -> push a disconnect
        at a strictly newer moment while that poll is suspended inside the
        fetch -> release the fetch, whose own status is still connected at
        the lagging _BASE_TS -> the completed poll must show the pushed
        disconnect, not silently revert to what the lagging REST view
        reported."""
        coordinator, client = self._build()
        await coordinator.async_config_entry_first_refresh()
        assert coordinator.data["hub_connectivity"][200]["state"] == _coord_module.HUB_CONNECTED

        release_event = asyncio.Event()
        entered_fetch = asyncio.Event()

        async def _blocking_status(_device_list):
            entered_fetch.set()
            await release_event.wait()
            return [
                {
                    "mid": 200,
                    "subDeviceStatus": [{"id": "connected", "value": "1", "time": self._BASE_TS}],
                }
            ]

        client.get_multiple_device_status.side_effect = _blocking_status

        poll_task = asyncio.create_task(coordinator.async_refresh())
        await asyncio.wait_for(entered_fetch.wait(), timeout=1)

        # The poll is now suspended inside _fetch_status_by_mid's awaited
        # client call. apply_hub_push_update is synchronous, so it runs to
        # completion in exactly this window, merging a disconnected record
        # into self.data before the poll ever resumes.
        _coord_module.RainPointCoordinator.apply_hub_push_update(coordinator, 200, False, self._PUSH_TS)
        assert coordinator.data["hub_connectivity"][200]["state"] == _coord_module.HUB_DISCONNECTED

        release_event.set()
        await asyncio.wait_for(poll_task, timeout=1)

        record = coordinator.data["hub_connectivity"][200]
        expected_changed_at = _coord_module._status_entry_time({"time": self._PUSH_TS}).isoformat()
        assert record["state"] == _coord_module.HUB_DISCONNECTED
        assert record["changed_at"] == expected_changed_at

        # _hub_disconnect_poll_counts stays consistent with the record this
        # same poll just wrote: exactly one poll has now reconciled against a
        # disconnected record for this hub, matching the state above rather
        # than a stale count left over from a snapshot the push never reached.
        assert coordinator._hub_disconnect_poll_counts.get((100, 200)) == 1


class TestHubPushTracerEndToEnd:
    """Drives construct -> first refresh -> platform setup -> one push
    dispatch in real order, proving the pushed disconnect reaches the Phase
    16 connectivity entity with no intervening poll."""

    @pytest.mark.asyncio
    async def test_pushed_disconnect_reaches_the_connectivity_entity_with_no_poll_between(self):
        from custom_components.rainpoint.api.mqtt import RainPointMqttClient
        from custom_components.rainpoint.binary_sensor import async_setup_entry as binary_setup_entry
        from custom_components.rainpoint.hub_entities import RainPointHubConnectivityBinarySensor

        client = AsyncMock()
        client.get_devices_by_hid.return_value = [
            {
                "mid": SAMPLE_HUB_FRAME_MID,
                "hid": 100,
                "name": "Hub1",
                "deviceName": "dev1",
                "productKey": "pk1",
                "homeName": "Home",
                "subDevices": [],
            }
        ]
        client.get_multiple_device_status.return_value = [
            {"mid": SAMPLE_HUB_FRAME_MID, "subDeviceStatus": [{"id": "connected", "value": "1"}]}
        ]

        entry = MagicMock()
        entry.entry_id = "test_entry"
        entry.data = {CONF_HIDS: [100]}

        hass = MagicMock()
        hass.data = {DOMAIN: {"test_entry": {}}}

        coordinator = _coord_module.RainPointCoordinator(hass, client, entry)
        await coordinator.async_config_entry_first_refresh()

        hass.data[DOMAIN]["test_entry"]["coordinator"] = coordinator
        hass.data[DOMAIN]["test_entry"]["mqtt_client"] = None

        captured = []
        add = MagicMock(side_effect=lambda ents, **kw: captured.extend(ents))
        await binary_setup_entry(hass, entry, add)

        connectivity_entities = [e for e in captured if isinstance(e, RainPointHubConnectivityBinarySensor)]
        assert len(connectivity_entities) == 1
        entity = connectivity_entities[0]
        assert entity.is_on is True

        mqtt_client = RainPointMqttClient(
            hass,
            client,
            entry=entry,
            hub_device_name="dev1",
            hub_product_key="pk1",
            coordinator=coordinator,
            hub_mid=SAMPLE_HUB_FRAME_MID,
        )
        mqtt_client._dispatch_push("topic", SAMPLE_HUB_DISCONNECT_FRAME.encode())

        # Same entity object, no second setup call, no reload, no
        # async_refresh anywhere in this test.
        assert entity.is_on is False


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


class TestMalformedCloudRecordsAreSkipped:
    """A malformed cloud record is skipped rather than raising KeyError out of
    the whole poll.

    Covers the decode walk in _decode_hub_subdevices, the two debounce walks
    reached from the same poll (_prune_silent_state, _track_orphaned_keys),
    the WARNING breadcrumb gate, and the healthy-poll control.
    """

    @staticmethod
    def _hub_two_children(mid=200, *, extra_sub_device=None):
        """One good child at addr 1, plus an optional second entry."""
        sub_devices = [{"addr": 1, "model": MODEL_MOISTURE_SIMPLE, "name": "Sensor1", "softVer": "1.0"}]
        if extra_sub_device is not None:
            sub_devices.append(extra_sub_device)
        return {
            "mid": mid,
            "name": f"Hub{mid}",
            "deviceName": f"dev{mid}",
            "productKey": "pk1",
            "homeName": "Home",
            "subDevices": sub_devices,
        }

    @staticmethod
    def _hub_single_child(mid=200, *, malformed=False):
        """A hub whose only child is at addr 1, or has no addr when malformed."""
        child = {"model": MODEL_MOISTURE_SIMPLE, "name": "Sensor1", "softVer": "1.0"}
        if not malformed:
            child["addr"] = 1
        return {
            "mid": mid,
            "name": f"Hub{mid}",
            "deviceName": f"dev{mid}",
            "productKey": "pk1",
            "homeName": "Home",
            "subDevices": [child],
        }

    @staticmethod
    def _status_single(mid=200):
        """A status response naming only the good D1 reading."""
        return [{"mid": mid, "subDeviceStatus": [{"id": "D1", "value": _MOISTURE_SIMPLE_PAYLOAD, "time": 1700000000000}]}]

    @staticmethod
    def _status_arrived_empty(mid=200):
        """A status response that arrived and named nobody."""
        return [{"mid": mid, "subDeviceStatus": []}]

    @staticmethod
    def _build_real_coord(hid=100):
        """A real RainPointCoordinator wired the way __init__.py wires it."""
        client = AsyncMock()
        entry = MagicMock()
        entry.entry_id = "test_entry"
        entry.data = {CONF_HIDS: [hid]}
        entry.options = {}

        hass = MagicMock()
        hass.data = {}

        return _coord_module.RainPointCoordinator(hass, client, entry), client

    @staticmethod
    def _point(client, hub, status):
        """Point the next poll at a single hub and its status response."""
        client.get_devices_by_hid.return_value = [hub]
        client.get_multiple_device_status.return_value = status

    # -- Task 1: the decode walk tolerates a malformed record ---------------

    @pytest.mark.asyncio
    async def test_a_sub_device_entry_missing_addr_is_skipped_others_decode(self):
        """Hub A carries one entry with no addr alongside a good one; hub B is
        healthy. Both good children decode; the malformed entry contributes
        no key."""
        coord, client = _make_coord()
        bad_entry = {"model": MODEL_MOISTURE_SIMPLE, "name": "Bad", "softVer": "1.0"}
        hub_a = self._hub_two_children(mid=200, extra_sub_device=bad_entry)
        hub_b = self._hub_two_children(mid=300)
        client.get_devices_by_hid.return_value = [hub_a, hub_b]
        client.get_multiple_device_status.return_value = self._status_single(200) + self._status_single(300)

        result = await _run(coord)

        assert set(result["sensors"]) == {"100_200_1", "100_300_1"}

    @pytest.mark.parametrize("bad_entry", ["not-a-dict", 123, None], ids=["string", "int", "none"])
    @pytest.mark.asyncio
    async def test_a_non_dict_sub_device_entry_is_skipped_others_decode(self, bad_entry):
        """A subDevices entry that is not a dict at all is skipped the same way."""
        coord, client = _make_coord()
        hub_a = self._hub_two_children(mid=200, extra_sub_device=bad_entry)
        hub_b = self._hub_two_children(mid=300)
        client.get_devices_by_hid.return_value = [hub_a, hub_b]
        client.get_multiple_device_status.return_value = self._status_single(200) + self._status_single(300)

        result = await _run(coord)

        assert set(result["sensors"]) == {"100_200_1", "100_300_1"}

    @pytest.mark.parametrize("bad_addr", [[], {}, set()], ids=["list", "dict", "set"])
    @pytest.mark.asyncio
    async def test_a_sub_device_whose_addr_is_unhashable_is_skipped_others_decode(self, bad_addr):
        """An addr that is present but cannot be a dict key is as unusable as
        a missing one. Indexing it raises TypeError rather than KeyError, and
        TypeError costs the whole poll exactly the same way, so presence
        alone is not the property this walk needs."""
        coord, client = _make_coord()
        bad_entry = {"addr": bad_addr, "model": MODEL_MOISTURE_SIMPLE, "name": "Bad", "softVer": "1.0"}
        hub_a = self._hub_two_children(mid=200, extra_sub_device=bad_entry)
        hub_b = self._hub_two_children(mid=300)
        client.get_devices_by_hid.return_value = [hub_a, hub_b]
        client.get_multiple_device_status.return_value = self._status_single(200) + self._status_single(300)

        result = await _run(coord)

        assert set(result["sensors"]) == {"100_200_1", "100_300_1"}

    @pytest.mark.asyncio
    async def test_a_sub_device_whose_addr_is_none_earns_no_phantom_key(self):
        """A None addr is hashable, so it would survive a hashability check
        and be enumerated under a key no sid can ever resolve to. That key
        would report nothing forever and collect a not-reporting card for a
        device that does not exist, so the record is skipped instead."""
        coord, client = _make_coord()
        bad_entry = {"addr": None, "model": MODEL_MOISTURE_SIMPLE, "name": "Bad", "softVer": "1.0"}
        hub = self._hub_two_children(mid=200, extra_sub_device=bad_entry)
        client.get_devices_by_hid.return_value = [hub]
        client.get_multiple_device_status.return_value = self._status_single(200)

        result = await _run(coord)

        assert set(result["sensors"]) == {"100_200_1"}
        assert "100_200_None" not in result["sensors"]

    @pytest.mark.asyncio
    async def test_malformed_status_entries_are_skipped_the_good_reading_still_decodes(self):
        """A status response with one entry missing id, one with a non-string
        id, and one non-dict entry, alongside a good D1 reading: the good
        reading still decodes."""
        coord, client = _make_coord()
        hub = self._hub_single_child(mid=200, malformed=False)
        client.get_devices_by_hid.return_value = [hub]
        client.get_multiple_device_status.return_value = [
            {
                "mid": 200,
                "subDeviceStatus": [
                    {"value": "no-id-here"},
                    {"id": 5, "value": "non-string-id"},
                    "not-a-dict",
                    {"id": "D1", "value": _MOISTURE_SIMPLE_PAYLOAD, "time": 1700000000000},
                ],
            }
        ]

        result = await _run(coord)

        sensor = result["sensors"]["100_200_1"]
        assert sensor["data"]["type"] == "moisture_simple"

    @pytest.mark.asyncio
    async def test_a_hub_degrading_between_two_polls_raises_nothing_and_keeps_decoding(self):
        """Driven through a real coordinator: well-formed on poll 1, a
        malformed extra entry on poll 2. No exception, and the good child
        keeps decoding."""
        coordinator, client = self._build_real_coord()
        self._point(client, self._hub_two_children(), self._status_single(200))

        with (
            patch.object(_repairs_module.ir, "async_create_issue"),
            patch.object(_repairs_module.ir, "async_delete_issue"),
        ):
            await coordinator.async_refresh()
            assert "100_200_1" in coordinator.data["sensors"]

            bad_entry = {"model": MODEL_MOISTURE_SIMPLE, "name": "Bad", "softVer": "1.0"}
            self._point(client, self._hub_two_children(extra_sub_device=bad_entry), self._status_single(200))
            await coordinator.async_refresh()

        assert set(coordinator.data["sensors"]) == {"100_200_1"}

    @pytest.mark.asyncio
    async def test_a_wholly_healthy_poll_is_unaffected_by_this_change(self):
        """Control: a wholly healthy poll produces exactly the sensors it
        produced before this change."""
        coord, client = _make_coord()
        client.get_devices_by_hid.return_value = [_make_hub()]
        client.get_multiple_device_status.return_value = _make_status()

        result = await _run(coord)

        assert set(result["sensors"]) == {"100_200_1"}
        assert result["sensors"]["100_200_1"]["data"]["type"] == "moisture_simple"

    # -- Task 2: the WARNING gate --------------------------------------------

    @pytest.mark.asyncio
    async def test_gate_fires_exactly_one_warning_at_the_degradation_edge(self, caplog):
        """A hub whose device list first carries an unusable record logs
        exactly one WARNING at that poll."""
        coordinator, client = self._build_real_coord()
        self._point(client, self._hub_two_children(), self._status_single(200))
        with (
            patch.object(_repairs_module.ir, "async_create_issue"),
            patch.object(_repairs_module.ir, "async_delete_issue"),
        ):
            await coordinator.async_refresh()

            bad_entry = {"model": "x", "name": "y", "softVer": "1.0"}
            self._point(client, self._hub_two_children(extra_sub_device=bad_entry), self._status_single(200))
            with caplog.at_level(logging.WARNING, logger="custom_components.rainpoint.coordinator"):
                await coordinator.async_refresh()

        warnings = [r for r in caplog.records if "unusable" in r.getMessage()]
        assert len(warnings) == 1
        assert "100_200" in warnings[0].getMessage()

    @pytest.mark.asyncio
    async def test_gate_stays_quiet_across_further_degraded_polls_while_the_skip_continues(self, caplog):
        """Several more polls with the same malformed shape log nothing
        further, while the skip itself keeps happening on every one of
        them."""
        coordinator, client = self._build_real_coord()
        self._point(client, self._hub_two_children(), self._status_single(200))
        with (
            patch.object(_repairs_module.ir, "async_create_issue"),
            patch.object(_repairs_module.ir, "async_delete_issue"),
        ):
            await coordinator.async_refresh()

            bad_entry = {"model": "x", "name": "y", "softVer": "1.0"}
            self._point(client, self._hub_two_children(extra_sub_device=bad_entry), self._status_single(200))
            with caplog.at_level(logging.WARNING, logger="custom_components.rainpoint.coordinator"):
                for _ in range(4):
                    await coordinator.async_refresh()
                    # The skip really is still happening: only the good key
                    # is ever present, never a key for the malformed extra.
                    assert set(coordinator.data["sensors"]) == {"100_200_1"}

        warnings = [r for r in caplog.records if "unusable" in r.getMessage()]
        assert len(warnings) == 1

    @pytest.mark.asyncio
    async def test_gate_re_arms_after_a_clean_poll(self, caplog):
        """A clean poll for that hub followed by a second degradation logs a
        second line."""
        coordinator, client = self._build_real_coord()
        bad_entry = {"model": "x", "name": "y", "softVer": "1.0"}
        with (
            patch.object(_repairs_module.ir, "async_create_issue"),
            patch.object(_repairs_module.ir, "async_delete_issue"),
        ):
            self._point(client, self._hub_two_children(extra_sub_device=bad_entry), self._status_single(200))
            with caplog.at_level(logging.WARNING, logger="custom_components.rainpoint.coordinator"):
                await coordinator.async_refresh()

                self._point(client, self._hub_two_children(), self._status_single(200))
                await coordinator.async_refresh()

                self._point(client, self._hub_two_children(extra_sub_device=bad_entry), self._status_single(200))
                await coordinator.async_refresh()

        warnings = [r for r in caplog.records if "unusable" in r.getMessage()]
        assert len(warnings) == 2

    @pytest.mark.asyncio
    async def test_a_wholly_healthy_poll_logs_no_gate_line(self, caplog):
        """A wholly healthy poll logs no unusable-record line at all."""
        coordinator, client = self._build_real_coord()
        self._point(client, self._hub_two_children(), self._status_single(200))
        with (
            patch.object(_repairs_module.ir, "async_create_issue"),
            patch.object(_repairs_module.ir, "async_delete_issue"),
            caplog.at_level(logging.WARNING, logger="custom_components.rainpoint.coordinator"),
        ):
            await coordinator.async_refresh()

        assert [r for r in caplog.records if "unusable" in r.getMessage()] == []

    @pytest.mark.asyncio
    async def test_gate_line_carries_only_the_hub_key_and_integer_counts(self, caplog):
        """The line names the hub key and two counts, and no cloud-supplied
        name or model string."""
        coordinator, client = self._build_real_coord()
        self._point(client, self._hub_two_children(), self._status_single(200))
        with (
            patch.object(_repairs_module.ir, "async_create_issue"),
            patch.object(_repairs_module.ir, "async_delete_issue"),
        ):
            await coordinator.async_refresh()

            bad_entry = {"model": "CanaryModel", "name": "CanarySensor", "softVer": "1.0"}
            self._point(client, self._hub_two_children(extra_sub_device=bad_entry), self._status_single(200))
            with caplog.at_level(logging.WARNING, logger="custom_components.rainpoint.coordinator"):
                await coordinator.async_refresh()

        warnings = [r for r in caplog.records if "unusable" in r.getMessage()]
        assert len(warnings) == 1
        message = warnings[0].getMessage()
        assert "100_200" in message
        assert "1 unusable sub-device record(s)" in message
        assert "0 unusable status record(s)" in message
        assert "CanaryModel" not in message
        assert "CanarySensor" not in message

    @pytest.mark.asyncio
    async def test_a_hub_whose_status_is_absent_still_gets_warned_for_a_malformed_device_list(self, caplog):
        """A hub whose status could not be obtained this poll still has its
        device list walked by _track_orphaned_keys in the same poll, so a
        malformed record there is still reported."""
        coordinator, client = self._build_real_coord()
        bad_entry = {"model": "x", "name": "y", "softVer": "1.0"}
        hub = self._hub_two_children(extra_sub_device=bad_entry)
        client.get_devices_by_hid.return_value = [hub]
        client.get_multiple_device_status.side_effect = aiohttp.ClientError("boom")
        client.get_device_status.side_effect = aiohttp.ClientError("boom")

        with (
            patch.object(_repairs_module.ir, "async_create_issue"),
            patch.object(_repairs_module.ir, "async_delete_issue"),
            caplog.at_level(logging.WARNING, logger="custom_components.rainpoint.coordinator"),
        ):
            await coordinator.async_refresh()

        warnings = [r for r in caplog.records if "unusable" in r.getMessage()]
        assert len(warnings) == 1
        message = warnings[0].getMessage()
        assert "100_200" in message
        assert "0 unusable status record(s)" in message

    # -- Task 3: the debounce consequence is pinned --------------------------

    @pytest.mark.asyncio
    async def test_a_key_that_turns_malformed_advances_the_orphan_counter_and_ages_out(self):
        """A key listed on poll 1 whose record turns malformed from poll 2
        onward advances _orphaned_key_poll_counts by one per poll and
        appears in aged_out_sensor_keys() once it reaches
        ORPHANED_KEY_DEBOUNCE_POLLS."""
        coordinator, client = self._build_real_coord()
        key = "100_200_1"
        self._point(client, self._hub_single_child(malformed=False), self._status_arrived_empty(200))

        with (
            patch.object(_repairs_module.ir, "async_create_issue"),
            patch.object(_repairs_module.ir, "async_delete_issue"),
        ):
            await coordinator.async_refresh()
            assert coordinator._orphaned_key_poll_counts == {}

            self._point(client, self._hub_single_child(malformed=True), self._status_arrived_empty(200))
            for expected in range(1, _coord_module.ORPHANED_KEY_DEBOUNCE_POLLS + 1):
                await coordinator.async_refresh()
                assert coordinator._orphaned_key_poll_counts[key] == expected
                if expected < _coord_module.ORPHANED_KEY_DEBOUNCE_POLLS:
                    assert key not in coordinator.aged_out_sensor_keys()

            assert key in coordinator.aged_out_sensor_keys()

    @pytest.mark.asyncio
    async def test_a_key_that_turns_malformed_drops_its_live_silent_counter(self):
        """A key with a live silent counter whose record turns malformed is
        dropped from _silent_poll_counts by _prune_silent_state."""
        coordinator, client = self._build_real_coord()
        key = "100_200_1"
        self._point(client, self._hub_single_child(malformed=False), self._status_arrived_empty(200))

        with (
            patch.object(_repairs_module.ir, "async_create_issue"),
            patch.object(_repairs_module.ir, "async_delete_issue"),
        ):
            await coordinator.async_refresh()
            assert coordinator._silent_poll_counts.get(key) == 1

            self._point(client, self._hub_single_child(malformed=True), self._status_arrived_empty(200))
            await coordinator.async_refresh()

        assert key not in coordinator._silent_poll_counts
