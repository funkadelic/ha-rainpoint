"""Tests for RainPointMqttClient: connect lifecycle, credential redaction, reconnect supervision."""

import asyncio
import inspect
import json
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import paho.mqtt.client as paho
import pytest

import custom_components.rainpoint.api.mqtt as mqtt_module
from custom_components.rainpoint.api.mqtt import RainPointMqttClient, RainPointMqttError
from tests.payload_samples import (
    SAMPLE_HUB_DISCONNECT_FRAME,
    SAMPLE_HUB_FRAME_MID,
    SAMPLE_HUB_RECONNECT_FRAME,
    SAMPLE_NON_HUB_PIPE_FRAME,
)

FAKE_DEVICE_SECRET = "SEKRIT-value-9f3a"


def _fake_creds(device_name="name-A", product_key="pk123") -> dict:
    """Fake fake creds helper."""
    return {
        "deviceName": device_name,
        "productKey": product_key,
        "deviceSecret": FAKE_DEVICE_SECRET,
        "mqttHostUrl": f"{product_key}.iot-as-mqtt.us-west-1.aliyuncs.com:1883",
    }


def _make_mqtt_client(hass, paho_instance, creds=None) -> RainPointMqttClient:
    """Make an MQTT client wired to a fake paho instance via an injected factory."""
    rainpoint_client = MagicMock()
    rainpoint_client.get_subscribe_status = AsyncMock(return_value=creds or _fake_creds())

    factory = MagicMock(return_value=paho_instance)

    return RainPointMqttClient(
        hass,
        rainpoint_client,
        entry=MagicMock(),
        hub_device_name="hub-device",
        hub_product_key="hub-pk",
        paho_client_factory=factory,
        time_source=lambda: 1000.0,
    )


def _make_hass(loop) -> MagicMock:
    """Make hass helper with a real event loop wired in.

    async_add_executor_job is stubbed to invoke and await the submitted callable
    inline (a plain MagicMock hass would return a non-awaitable MagicMock and
    silently stop exercising the real connect path). Running it inline preserves
    every behavioral assertion -- connect() side effects, raising to drive retry,
    call counts -- exactly as when connect() ran synchronously.
    """
    hass = MagicMock()
    hass.loop = loop

    async def _async_add_executor_job(func, *args):
        return func(*args)

    hass.async_add_executor_job = _async_add_executor_job
    return hass


def _make_fake_paho() -> MagicMock:
    """Make a MagicMock spec'd against the real paho Client for attribute/method fidelity."""
    return MagicMock(spec=paho.Client)


async def _settle(times: int = 5) -> None:
    """Yield control back to the event loop repeatedly so a background task can progress."""
    for _ in range(times):
        await asyncio.sleep(0)


async def _instant_sleep(*_args, **_kwargs) -> None:
    """A RainPointMqttClient._sleep replacement that yields once (real checkpoint)
    instead of actually sleeping for the computed delay -- keeps supervisor-retry
    and renewal tests fast while still giving the test driver's _settle() loop a
    chance to observe each iteration.
    """
    await asyncio.sleep(0)


class TestConstructorSeams:
    """The constructor accepts an injectable paho factory + time source."""

    def test_constructor_exposes_test_seams(self):
        """paho_client_factory and time_source are keyword-only constructor params."""
        sig = inspect.signature(RainPointMqttClient.__init__)
        assert "paho_client_factory" in sig.parameters
        assert "time_source" in sig.parameters


class TestConnectDoesNotSubscribe:
    """The observer's productKey forbids client subscriptions: any SUBSCRIBE
    force-closes the connection, and the broker auto-delivers downlink messages
    unsolicited, so the client must never call subscribe()."""

    @pytest.mark.asyncio
    async def test_async_start_never_subscribes(self):
        """async_start() launches the supervisor, which connects but never subscribes."""
        loop = asyncio.get_running_loop()
        hass = _make_hass(loop)
        fake_paho = _make_fake_paho()
        client = _make_mqtt_client(hass, fake_paho)

        await client.async_start()
        await _settle()

        fake_paho.connect.assert_called()
        fake_paho.subscribe.assert_not_called()

        await client.async_disconnect()


class TestMessageReceiptLogging:
    """Logs topic + byte-length + running count at DEBUG, never payload contents."""

    @pytest.mark.asyncio
    async def test_on_message_logs_topic_len_and_count_not_payload(self, caplog):
        """Driving on_message produces exactly one debug record with topic + int len, no raw bytes."""
        loop = asyncio.get_running_loop()
        hass = _make_hass(loop)
        fake_paho = _make_fake_paho()
        client = _make_mqtt_client(hass, fake_paho)
        await client.async_start()
        await _settle()

        payload = b"x" * 425
        msg = SimpleNamespace(topic="/sys/pk123/name-A/thing/service/property/set", payload=payload)

        with caplog.at_level(logging.DEBUG):
            client._on_message(fake_paho, None, msg)
            # Not yet applied -- only scheduled via call_soon_threadsafe.
            assert client.message_count == 0

            await asyncio.sleep(0)
            await asyncio.sleep(0)

        assert client.message_count == 1
        receipt_records = [r for r in caplog.records if "message received" in r.message]
        assert len(receipt_records) == 1
        record = receipt_records[0]
        assert msg.topic in record.message
        assert "425" in record.message
        assert payload.decode() not in record.message

        await client.async_disconnect()

    @pytest.mark.asyncio
    async def test_first_unrecognized_shape_announces_itself_at_info_without_the_payload(self, caplog):
        """The first payload of a distinct unrecognized shape announces itself
        at INFO so it is visible without a debug session, which a signal
        visible only under DEBUG is not: that is precisely the condition that
        hid the hub connectivity frames for a whole milestone. The
        announcement names the shape and topic and never carries the payload,
        which observed frames of this family embed an account id and a mid
        into and INFO reaches the default Home Assistant log."""
        loop = asyncio.get_running_loop()
        hass = _make_hass(loop)
        fake_paho = _make_fake_paho()
        client = _make_mqtt_client(hass, fake_paho)
        await client.async_start()
        await _settle()

        # A hub property/set downlink (not a sub-device status push, not a
        # hub connectivity frame) is dropped.
        payload = b'{"method":"thing.service.property.set","params":{"BroadcastTime":1}}'
        msg = SimpleNamespace(topic="/sys/pk123/name-A/thing/service/property/set", payload=payload)

        with caplog.at_level(logging.INFO):
            client._on_message(fake_paho, None, msg)
            await asyncio.sleep(0)
            await asyncio.sleep(0)

        info_records = [r for r in caplog.records if r.levelno == logging.INFO and "unrecognized" in r.message]
        assert len(info_records) == 1
        assert mqtt_module._shape_key(payload) in info_records[0].message
        assert "BroadcastTime" not in info_records[0].message
        assert payload.decode() not in info_records[0].message

        await client.async_disconnect()

    @pytest.mark.asyncio
    async def test_repeat_of_the_same_unrecognized_shape_skips_preview_above_debug(self, caplog):
        """A second payload of an already-seen unrecognized shape is
        DEBUG-only: above DEBUG, the preview call is skipped (the
        isEnabledFor guard's off branch).

        The logger level is pinned to INFO explicitly so this covers the
        guard's skip branch deterministically, regardless of ambient log
        state or test order. Asserting that _payload_preview never runs is
        what proves the guard skipped the work; an absent log record alone
        would also be satisfied by the logger filtering a record whose
        arguments had already been evaluated.
        """
        loop = asyncio.get_running_loop()
        hass = _make_hass(loop)
        fake_paho = _make_fake_paho()
        client = _make_mqtt_client(hass, fake_paho)
        await client.async_start()
        await _settle()

        payload = b'{"method":"thing.service.property.set","params":{"BroadcastTime":1}}'
        msg = SimpleNamespace(topic="/sys/pk123/name-A/thing/service/property/set", payload=payload)

        # First occurrence establishes the shape as seen; not under test here.
        client._on_message(fake_paho, None, msg)
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        caplog.clear()

        with (
            patch.object(mqtt_module, "_payload_preview", wraps=mqtt_module._payload_preview) as preview,
            caplog.at_level(logging.INFO, logger="custom_components.rainpoint.api.mqtt"),
        ):
            client._on_message(fake_paho, None, msg)
            await asyncio.sleep(0)
            await asyncio.sleep(0)

        assert preview.call_count == 0
        assert not any(r.levelno == logging.DEBUG for r in caplog.records)

        await client.async_disconnect()

    def test_payload_preview_returns_short_text_verbatim(self):
        """A payload within the limit is decoded verbatim."""
        assert mqtt_module._payload_preview(b'{"k":1}') == '{"k":1}'

    def test_payload_preview_truncates_long_text(self):
        """A payload over the limit is truncated with a marker."""
        preview = mqtt_module._payload_preview(b"x" * 5000, limit=1024)
        assert preview == "x" * 1024 + "...(truncated)"

    @pytest.mark.asyncio
    async def test_message_count_increments_across_two_messages(self):
        """The running count increments once per delivered message."""
        loop = asyncio.get_running_loop()
        hass = _make_hass(loop)
        fake_paho = _make_fake_paho()
        client = _make_mqtt_client(hass, fake_paho)
        await client.async_start()
        await _settle()

        msg1 = SimpleNamespace(topic="/sys/pk123/name-A/thing/service/property/set", payload=b"one")
        msg2 = SimpleNamespace(topic="/sys/pk123/name-A/thing/event/property/post", payload=b"two")

        client._on_message(fake_paho, None, msg1)
        await asyncio.sleep(0)
        assert client.message_count == 1

        client._on_message(fake_paho, None, msg2)
        await asyncio.sleep(0)
        assert client.message_count == 2

        await client.async_disconnect()

    @pytest.mark.asyncio
    async def test_on_message_never_mutates_state_directly(self):
        """_on_message only schedules work via call_soon_threadsafe -- no direct mutation."""
        loop = asyncio.get_running_loop()
        hass = _make_hass(loop)  # real loop for the supervisor task; call_soon_threadsafe is patched below
        fake_paho = _make_fake_paho()
        client = _make_mqtt_client(hass, fake_paho)
        await client.async_start()
        await _settle()

        # Replace call_soon_threadsafe with a plain mock so it never actually fires.
        hass.loop = MagicMock()
        client._hass = hass

        msg = SimpleNamespace(topic="/sys/pk123/name-A/thing/service/property/set", payload=b"hello")
        client._on_message(fake_paho, None, msg)

        # call_soon_threadsafe was scheduled but never actually invoked (mock loop) --
        # message_count must still be zero, proving no direct mutation happened.
        assert client.message_count == 0
        hass.loop.call_soon_threadsafe.assert_called_once()
        scheduled_func, *scheduled_args = hass.loop.call_soon_threadsafe.call_args.args
        assert scheduled_func == client._handle_message

        # Now simulate the event loop actually running the scheduled callback.
        scheduled_func(*scheduled_args)
        assert client.message_count == 1

        # async_start above spawned the supervisor task. Home Assistant's test
        # cleanup fails a test that leaves one running, so tear it down the way
        # the sibling cases do.
        await client.async_disconnect()


class TestSecretRedaction:
    """No log line emits deviceSecret, the derived password, or a full clientId."""

    @pytest.mark.asyncio
    async def test_no_secret_leaks_across_full_lifecycle(self, caplog):
        """deviceSecret and the derived HMAC password never appear in caplog text."""
        loop = asyncio.get_running_loop()
        hass = _make_hass(loop)
        fake_paho = _make_fake_paho()
        client = _make_mqtt_client(hass, fake_paho, creds=_fake_creds())

        with caplog.at_level(logging.DEBUG):
            await client.async_start()
            await _settle()

            derived_password = client._paho.username_pw_set.call_args.args[1]

            msg = SimpleNamespace(topic="/sys/pk123/name-A/thing/service/property/set", payload=b"abc")
            client._on_message(fake_paho, None, msg)
            await asyncio.sleep(0)

            await client.async_disconnect()

        assert FAKE_DEVICE_SECRET not in caplog.text
        assert derived_password not in caplog.text

    @pytest.mark.asyncio
    async def test_no_log_line_emits_the_device_name_or_product_key(self, caplog):
        """The connect line logs the username, which is "<deviceName>&<productKey>".

        Redacting it with the secret helper was not enough: that helper keeps the
        last four characters, and for this value those are the tail of the
        productKey. It goes through the identifier helper instead, which renders
        a length and nothing else.
        """
        loop = asyncio.get_running_loop()
        hass = _make_hass(loop)
        fake_paho = _make_fake_paho()
        creds = _fake_creds(device_name="MAC-A84674BB91F0", product_key="a3QrDxYPTM2")
        client = _make_mqtt_client(hass, fake_paho, creds=creds)

        with caplog.at_level(logging.DEBUG):
            await client.async_start()
            await _settle()
            await client.async_disconnect()

        emitted = caplog.text
        assert "MAC-A84674BB91F0" not in emitted
        assert "a3QrDxYPTM2" not in emitted
        # The tail specifically, which is what a last-4 redaction would have left.
        assert "PTM2" not in emitted
        # The line still ran, so the assertions above are not passing on an empty log.
        assert "RainPoint MQTT connecting" in emitted


def _captured_push_payload(subdevices, ts=1784707302285):
    """Build a realistic captured-shape push payload from the confirmed envelope.

    subdevices maps sid -> raw_value ("11#..." TLV strings). The "update"/"state"
    housekeeping keys are included so tests prove they are ignored.
    """
    inner = {sid: {"time": ts, "value": value} for sid, value in subdevices.items()}
    inner["update"] = {"time": ts, "value": 1}
    inner["state"] = {"time": ts, "value": "0,-56"}
    param = "|".join(
        [
            "#P" + "0" * 30,
            json.dumps(inner),
            str(ts),
            "abcdef012345#",
        ]
    )
    outer = {
        "method": "thing.service.property.set",
        "id": "123456789",
        "params": {"param": param},
        "version": "1.0.0",
    }
    return json.dumps(outer).encode()


def _push_outer(method="thing.service.property.set", params=None):
    """Encode an outer AliCloud IoT payload with the given method/params."""
    return json.dumps({"method": method, "params": params}).encode()


def _push_param_payload(inner):
    """Encode an outer payload whose params.param pipe-string carries inner JSON."""
    param = "|".join(["#P0", json.dumps(inner), "1", "t"])
    return _push_outer(params={"param": param})


def _make_push_client(hass, fake_paho, coordinator, hub_mid=4242) -> RainPointMqttClient:
    """Build an MQTT client wired to a coordinator and a fixed hub mid."""
    rainpoint_client = MagicMock()
    rainpoint_client.get_subscribe_status = AsyncMock(return_value=_fake_creds())
    factory = MagicMock(return_value=fake_paho)
    return RainPointMqttClient(
        hass,
        rainpoint_client,
        entry=MagicMock(),
        hub_device_name="hub-device",
        hub_product_key="hub-pk",
        coordinator=coordinator,
        hub_mid=hub_mid,
        paho_client_factory=factory,
        time_source=lambda: 1000.0,
    )


class TestPushEnvelopeFailSafe:
    """Malformed, truncated, oversized, prefix-missing, and sub-device-token-
    missing payloads are dropped without raising and without touching the
    coordinator (fail-safe parse)."""

    @pytest.mark.parametrize(
        "payload",
        [
            b"",  # empty
            b"not-json-at-all",  # non-JSON
            b'{"method":"thing.service.property.set","params":',  # truncated JSON
            _push_outer("other.method", {"param": "x|{}"}),  # method mismatch
            _push_outer(params="not-a-dict"),  # params not a dict
            _push_outer(params={"param": 123}),  # param value not a string
            _push_outer(params={"param": "sect1|sect3|tok"}),  # prefix / inner JSON missing
            _push_param_payload({"update": {"time": 1, "value": 1}}),  # no D-token
            _push_param_payload({"D01": {"time": 1, "value": 99}}),  # D-entry value not a string
            ("x" * 100_000).encode(),  # oversized non-JSON blob
        ],
    )
    def test_parse_push_envelope_drops_bad_payload_without_raising(self, payload):
        """Every malformed shape yields an empty update list, never an exception."""
        assert mqtt_module._parse_push_envelope(payload) == []

    @pytest.mark.asyncio
    async def test_valid_payload_without_coordinator_wiring_is_dropped(self, caplog):
        """A parseable payload received before a coordinator is wired is dropped
        (defensive) without raising and without any apply_push_update target."""
        loop = asyncio.get_running_loop()
        hass = _make_hass(loop)
        fake_paho = _make_fake_paho()
        client = _make_mqtt_client(hass, fake_paho)  # no coordinator wired
        await client.async_start()
        await _settle()

        payload = _captured_push_payload({"D01": "11#" + "0a1b" * 28})
        msg = SimpleNamespace(topic="/sys/pk123/name-A/thing/service/property/set", payload=payload)

        with caplog.at_level(logging.DEBUG):
            client._on_message(fake_paho, None, msg)
            await asyncio.sleep(0)
            await asyncio.sleep(0)

        assert any("before coordinator wiring" in r.message for r in caplog.records)

        await client.async_disconnect()

    def test_parse_push_envelope_accepts_single_unnamed_param_value(self):
        """When params has a single value under a non-'param' key, it is still used."""
        inner = {"D01": {"time": 123, "value": "11#ab"}}
        param = "|".join(["#P" + "0" * 10, json.dumps(inner), "123", "tok#"])
        payload = json.dumps({"method": "thing.service.property.set", "params": {"anything": param}}).encode()
        assert mqtt_module._parse_push_envelope(payload) == [("D01", "11#ab", 123)]

    def test_parse_push_envelope_drops_oversized_payload(self):
        """An oversized payload is dropped before parsing, even if it would
        otherwise be valid JSON, so a huge message cannot drive work."""
        inner = {"D01": {"time": 123, "value": "11#" + "a" * 20000}}
        param = "|".join(["#P" + "0" * 10, json.dumps(inner), "123", "tok#"])
        payload = json.dumps({"method": "thing.service.property.set", "params": {"param": param}}).encode()
        assert len(payload) > mqtt_module.MQTT_PUSH_MAX_PAYLOAD_BYTES
        assert mqtt_module._parse_push_envelope(payload) == []

    def test_subdevice_updates_returns_empty_for_non_dict(self):
        """The sub-device extractor drops a structurally odd (non-dict) inner
        section instead of raising."""
        assert mqtt_module._subdevice_updates(["not", "a", "dict"]) == []

    @pytest.mark.asyncio
    async def test_malformed_payload_through_handler_never_calls_coordinator(self):
        """A malformed payload driven through the HA-loop handler drops silently."""
        loop = asyncio.get_running_loop()
        hass = _make_hass(loop)
        fake_paho = _make_fake_paho()
        coordinator = MagicMock()
        client = _make_push_client(hass, fake_paho, coordinator)
        await client.async_start()
        await _settle()

        msg = SimpleNamespace(topic="/sys/pk123/name-A/thing/service/property/set", payload=b'{"method":"nope"}')
        client._on_message(fake_paho, None, msg)
        await asyncio.sleep(0)
        await asyncio.sleep(0)

        coordinator.apply_push_update.assert_not_called()

        await client.async_disconnect()

    @pytest.mark.asyncio
    async def test_handler_never_logs_raw_payload_content(self, caplog):
        """The push path logs topic + length only; a secret-shaped token embedded
        in the payload never reaches a log record in the clear."""
        loop = asyncio.get_running_loop()
        hass = _make_hass(loop)
        fake_paho = _make_fake_paho()
        coordinator = MagicMock()
        client = _make_push_client(hass, fake_paho, coordinator)
        await client.async_start()
        await _settle()

        secret_token = "a9f3c1e2SECRETdeviceSecretValue7b4d0a2f"
        payload = _captured_push_payload({"D01": "11#" + secret_token})
        msg = SimpleNamespace(topic="/sys/pk123/name-A/thing/service/property/set", payload=payload)

        with caplog.at_level(logging.DEBUG):
            client._on_message(fake_paho, None, msg)
            await asyncio.sleep(0)
            await asyncio.sleep(0)

        assert secret_token not in caplog.text

        await client.async_disconnect()


class TestPushEnvelopeParsing:
    """The HA-loop handler parses the confirmed envelope and routes each
    D-subdevice to coordinator.apply_push_update with the fixed hub mid."""

    @pytest.mark.asyncio
    async def test_captured_payload_drives_one_apply_push_update_per_subdevice(self):
        """Each D-prefixed sub-device produces exactly one apply_push_update call
        with the fixed hub mid, the sid, its raw value, and the device timestamp."""
        loop = asyncio.get_running_loop()
        hass = _make_hass(loop)
        fake_paho = _make_fake_paho()
        coordinator = MagicMock()
        client = _make_push_client(hass, fake_paho, coordinator, hub_mid=4242)
        assert client.hub_mid == 4242
        await client.async_start()
        await _settle()

        body_a = "11#" + "0a1b" * 28
        body_b = "11#" + "1c2d" * 28
        payload = _captured_push_payload({"D01": body_a, "D02": body_b}, ts=1784707302285)
        msg = SimpleNamespace(topic="/sys/pk123/name-A/thing/service/property/set", payload=payload)

        client._on_message(fake_paho, None, msg)
        await asyncio.sleep(0)
        await asyncio.sleep(0)

        calls = coordinator.apply_push_update.call_args_list
        assert len(calls) == 2
        by_sid = {call.args[1]: call.args for call in calls}
        assert by_sid["D01"] == (4242, "D01", body_a, 1784707302285)
        assert by_sid["D02"] == (4242, "D02", body_b, 1784707302285)
        # Liveness clock updated from the injected monotonic seam.
        assert client.last_message_at == 1000.0

        await client.async_disconnect()

    @pytest.mark.asyncio
    async def test_housekeeping_keys_are_ignored(self):
        """Only D-prefixed keys route; the update/state keys never call the coordinator."""
        loop = asyncio.get_running_loop()
        hass = _make_hass(loop)
        fake_paho = _make_fake_paho()
        coordinator = MagicMock()
        client = _make_push_client(hass, fake_paho, coordinator)
        await client.async_start()
        await _settle()

        payload = _captured_push_payload({"D01": "11#" + "0a1b" * 28})
        msg = SimpleNamespace(topic="/sys/pk123/name-A/thing/service/property/set", payload=payload)

        client._on_message(fake_paho, None, msg)
        await asyncio.sleep(0)
        await asyncio.sleep(0)

        assert coordinator.apply_push_update.call_count == 1
        assert coordinator.apply_push_update.call_args.args[1] == "D01"

        await client.async_disconnect()

    @pytest.mark.asyncio
    async def test_last_message_at_updates_even_on_undecodable_payload(self):
        """An undecodable payload still stamps liveness and never calls the coordinator."""
        loop = asyncio.get_running_loop()
        hass = _make_hass(loop)
        fake_paho = _make_fake_paho()
        coordinator = MagicMock()
        client = _make_push_client(hass, fake_paho, coordinator)
        await client.async_start()
        await _settle()

        assert client.last_message_at is None
        msg = SimpleNamespace(topic="/sys/pk123/name-A/thing/service/property/set", payload=b"not-json-at-all")

        client._on_message(fake_paho, None, msg)
        await asyncio.sleep(0)
        await asyncio.sleep(0)

        assert client.last_message_at == 1000.0
        coordinator.apply_push_update.assert_not_called()

        await client.async_disconnect()

    def test_parse_push_envelope_returns_updates_for_valid_payload(self):
        """The parser returns (sid, raw_value, device_ts) tuples for D-subdevices."""
        payload = _captured_push_payload({"D01": "11#ab", "D02": "11#cd"}, ts=1784707302285)
        updates = mqtt_module._parse_push_envelope(payload)
        assert sorted(updates) == [
            ("D01", "11#ab", 1784707302285),
            ("D02", "11#cd", 1784707302285),
        ]


class TestHubFrameParsing:
    """_parse_hub_frame as a pure function: fully fail-safe, and
    fail-safe via two exception scopes rather than one (the load-bearing fix
    a literal transcription of _parse_push_envelope's shape would have missed)."""

    def test_bare_capture_parses_with_no_json_wrapper(self):
        """The exact 2026-07-31 capture, bare, with no JSON wrapper at all,
        must parse -- this is the frame the two-exception-scope structure
        exists to read, and pins the load-bearing requirement against
        regression. A single-outer-try transcription of _parse_push_envelope's
        shape would raise ValueError on json.loads and return None here."""
        frame = mqtt_module._parse_hub_frame(SAMPLE_HUB_DISCONNECT_FRAME.encode())
        assert frame is not None
        assert frame.connected is False
        assert frame.changed_ts == 1785521850011

    def test_reconnect_frame_parses_connected_true(self):
        """The reconstructed reconnect frame parses with connected True."""
        frame = mqtt_module._parse_hub_frame(SAMPLE_HUB_RECONNECT_FRAME.encode())
        assert frame is not None
        assert frame.connected is True
        assert frame.changed_ts == 1785523062039

    def test_captured_non_hub_pipe_frame_is_rejected(self):
        """The only OBSERVED payload that must be rejected, captured on the
        same downlink topic moments after a hub reconnect during the
        2026-08-01 UAT.

        Every other rejection case here is a hand-mutated variant of a
        known-good frame, each failing one clause in isolation. This one is
        real traffic and fails two at once (three sections, and an empty
        section 2 rather than a literal 0/1), which is how malformed input
        actually arrives. Its section-1 tail is 182509, the hid rather than a
        mid, so the mid cross-check would drop it as well even if both parse
        clauses were relaxed: three independent layers, not one.
        """
        assert mqtt_module._parse_hub_frame(SAMPLE_NON_HUB_PIPE_FRAME.encode()) is None

    def test_captured_non_hub_pipe_frame_is_rejected_inside_its_envelope(self):
        """Rejected on the JSON-wrapped route too, which is how it arrived.

        The bare-text assertion above would still pass if envelope unwrapping
        were the thing that changed, so both routes are pinned.
        """
        payload = json.dumps(
            {
                "method": "thing.service.property.set",
                "id": "429833999",
                "params": {"param": SAMPLE_NON_HUB_PIPE_FRAME},
                "version": "1.0.0",
            }
        ).encode()
        assert mqtt_module._parse_hub_frame(payload) is None

    def test_captured_non_hub_pipe_frame_reaches_no_coordinator_entry_point(self):
        """Rejection at the parser is not enough on its own: the frame must
        also leave both sanctioned coordinator entry points untouched, since
        that is the actual harm a misread would cause."""
        coordinator = MagicMock()
        client = _make_push_client(MagicMock(), MagicMock(), coordinator, hub_mid=SAMPLE_HUB_FRAME_MID)
        client._dispatch_push("topic", SAMPLE_NON_HUB_PIPE_FRAME.encode())
        coordinator.apply_hub_push_update.assert_not_called()
        coordinator.apply_push_update.assert_not_called()

    def test_json_wrapped_capture_parses_to_the_identical_frame(self):
        """The same frame carried inside an AliCloud envelope converges on the
        identical _HubFrame as the bare route -- both routes are required and
        neither is redundant."""
        payload = json.dumps({"method": "thing.service.property.set", "params": {"param": SAMPLE_HUB_DISCONNECT_FRAME}}).encode()
        frame = mqtt_module._parse_hub_frame(payload)
        assert frame == mqtt_module._parse_hub_frame(SAMPLE_HUB_DISCONNECT_FRAME.encode())

    def test_json_wrapped_single_unnamed_param_fallback(self):
        """A params dict with exactly one entry under a non-'param' key is
        still used, mirroring _parse_push_envelope's identical fallback."""
        payload = json.dumps(
            {"method": "thing.service.property.set", "params": {"anything": SAMPLE_HUB_DISCONNECT_FRAME}}
        ).encode()
        frame = mqtt_module._parse_hub_frame(payload)
        assert frame is not None
        assert frame.connected is False

    def test_missing_trailing_terminator_still_parses(self):
        """The terminator strip only fires when present; its absence is not an error."""
        no_terminator = SAMPLE_HUB_DISCONNECT_FRAME[:-1]
        frame = mqtt_module._parse_hub_frame(no_terminator.encode())
        assert frame is not None
        assert frame.changed_ts == 1785521850011

    def test_oversized_payload_is_dropped(self):
        """The MQTT_PUSH_MAX_PAYLOAD_BYTES bound is applied before any parse."""
        oversized = SAMPLE_HUB_DISCONNECT_FRAME.encode() + b"0" * mqtt_module.MQTT_PUSH_MAX_PAYLOAD_BYTES
        assert mqtt_module._parse_hub_frame(oversized) is None

    def test_non_utf8_bytes_do_not_raise(self):
        """Undecodable bytes degrade via 'replace' rather than raising, and the
        resulting garbled text simply fails shape recognition."""
        assert mqtt_module._parse_hub_frame(b"\xff\xfe\x00#P|0|1|2#") is None

    @pytest.mark.parametrize(
        ("mutated", "reason"),
        [
            ("{" + SAMPLE_HUB_DISCONNECT_FRAME, "brace-prefixed section"),
            ("#P1|0|2#", "wrong section count (3)"),
            ("#P1|0|2|3|4#", "wrong section count (5)"),
            ("P260731181730000016822282236547|0|1785521850011|112882164350#", "missing #P prefix"),
            ("#P260731181730000016822282236547|2|1785521850011|112882164350#", "section 2 not literal 0/1"),
            ("#P260731181730000016822282236547|0|not-an-int|112882164350#", "section 3 not int"),
            ("#P260731181730000016822282236547|0|1785521850011|not-an-int#", "section 4 not int"),
        ],
    )
    def test_each_d05_clause_failing_yields_none(self, mutated, reason):
        """Each of the five recognition clauses, failing independently, drops the frame."""
        assert mqtt_module._parse_hub_frame(mutated.encode()) is None, reason

    @pytest.mark.parametrize(
        "payload",
        [
            json.dumps([1, 2, 3]).encode(),  # valid JSON, not a dict
            json.dumps({"method": "other.method", "params": {"param": SAMPLE_HUB_DISCONNECT_FRAME}}).encode(),
            json.dumps({"method": "thing.service.property.set", "params": "not-a-dict"}).encode(),
            json.dumps({"method": "thing.service.property.set", "params": {"a": "x", "b": "y"}}).encode(),
            json.dumps({"method": "thing.service.property.set", "params": {"param": 123}}).encode(),
        ],
    )
    def test_candidate_resolution_falls_through_to_bare_text_and_still_fails_shape(self, payload):
        """Every non-standard envelope shape leaves the candidate as the raw
        JSON text, which is not a hub frame either, so the result is None --
        proving the candidate-resolution branches degrade safely rather than
        raising or accidentally matching."""
        assert mqtt_module._parse_hub_frame(payload) is None


class TestHubFrameRouting:
    """_dispatch_push routes a recognized hub frame to apply_hub_push_update,
    always with the client's own construction-supplied mid, never the frame's
    parsed mid_tail."""

    def test_hub_frame_reaches_apply_hub_push_update_with_the_clients_own_mid(self):
        coordinator = MagicMock()
        client = _make_push_client(MagicMock(), MagicMock(), coordinator, hub_mid=SAMPLE_HUB_FRAME_MID)

        client._dispatch_push("topic", SAMPLE_HUB_DISCONNECT_FRAME.encode())

        coordinator.apply_hub_push_update.assert_called_once_with(SAMPLE_HUB_FRAME_MID, False, 1785521850011)
        coordinator.apply_push_update.assert_not_called()

    def test_differing_six_digit_mid_drops_without_calling_either_entry_point(self):
        """The equal-width exact-slice path: a genuinely different 6-digit mid
        cannot satisfy the fixed-width comparison."""
        coordinator = MagicMock()
        client = _make_push_client(MagicMock(), MagicMock(), coordinator, hub_mid=999999)

        client._dispatch_push("topic", SAMPLE_HUB_DISCONNECT_FRAME.encode())

        coordinator.apply_hub_push_update.assert_not_called()
        coordinator.apply_push_update.assert_not_called()

    def test_fallback_width_mismatch_drops(self):
        """own is 7 digits, not the observed 6-digit width, so the suffix
        fallback applies -- and still correctly rejects a genuinely different
        tail."""
        coordinator = MagicMock()
        client = _make_push_client(MagicMock(), MagicMock(), coordinator, hub_mid=1236547)

        client._dispatch_push("topic", SAMPLE_HUB_DISCONNECT_FRAME.encode())

        coordinator.apply_hub_push_update.assert_not_called()

    def test_fallback_width_admits_a_proper_suffix_mid_as_documented_residual(self):
        """own is 5 digits, not the observed 6-digit width, and happens to be a
        proper suffix of the frame's real mid: the fallback path admits it.
        This is the documented, accepted residual for an unobserved mid
        width -- degrading to a weaker check beats dropping every frame and
        silently disabling the feature for that width."""
        coordinator = MagicMock()
        client = _make_push_client(MagicMock(), MagicMock(), coordinator, hub_mid=36547)

        client._dispatch_push("topic", SAMPLE_HUB_DISCONNECT_FRAME.encode())

        coordinator.apply_hub_push_update.assert_called_once_with(36547, False, 1785521850011)

    def test_coordinator_not_wired_drops_a_recognized_hub_frame(self, caplog):
        client = _make_push_client(MagicMock(), MagicMock(), coordinator=None, hub_mid=SAMPLE_HUB_FRAME_MID)

        with caplog.at_level(logging.DEBUG):
            client._dispatch_push("topic", SAMPLE_HUB_DISCONNECT_FRAME.encode())

        assert any("before coordinator wiring" in r.message for r in caplog.records)

    def test_sub_device_envelope_and_hub_frame_each_route_to_exactly_one_entry_point(self):
        """The companion invariant test named in the plan's assumption-delta
        decision: a sub-device envelope reaches only apply_push_update and a
        hub frame reaches only apply_hub_push_update, never both -- it goes
        red the instant a future change lets one payload reach both."""
        coordinator = MagicMock()
        client = _make_push_client(MagicMock(), MagicMock(), coordinator, hub_mid=SAMPLE_HUB_FRAME_MID)

        subdevice_payload = _captured_push_payload({"D01": "11#" + "0a1b" * 28})
        client._dispatch_push("topic", subdevice_payload)
        coordinator.apply_push_update.assert_called_once()
        coordinator.apply_hub_push_update.assert_not_called()

        coordinator.reset_mock()
        client._dispatch_push("topic", SAMPLE_HUB_DISCONNECT_FRAME.encode())
        coordinator.apply_hub_push_update.assert_called_once()
        coordinator.apply_push_update.assert_not_called()


class TestUnrecognisedShapeLogging:
    """The first payload of a distinct unrecognized shape announces that shape
    once at INFO, carrying no payload; every truncated preview stays DEBUG;
    the per-client bookkeeping is hard-capped so it cannot grow unbounded."""

    def test_shape_key_classifies_by_structure_not_content(self):
        """Two payloads with the same section-count/class shape but
        different content classify to the same key."""
        key_a = mqtt_module._shape_key(b'{"method":"a","params":{"x":1}}')
        key_b = mqtt_module._shape_key(b'{"method":"b","params":{"y":2}}')
        assert key_a == key_b

    def test_shape_key_differs_for_a_different_section_count(self):
        key_two_sections = mqtt_module._shape_key(b"a|b")
        key_three_sections = mqtt_module._shape_key(b"a|b|c")
        assert key_two_sections != key_three_sections

    def test_section_class_covers_every_bucket(self):
        """The empty and all-digits buckets have no other exercising path."""
        assert mqtt_module._section_class("") == "E"
        assert mqtt_module._section_class("{not-really-json") == "J"
        assert mqtt_module._section_class("112882164350") == "D"
        assert mqtt_module._section_class("not-digits-or-json") == "O"

    def test_first_shape_logs_info_repeat_logs_debug_new_shape_logs_info_again(self, caplog):
        """Under DEBUG each of the three payloads yields a preview: the two
        first-of-shape ones from the announcement's own nested debug call, the
        repeat from the already-seen branch. Only the two first-of-shape
        payloads add an INFO announcement."""
        coordinator = MagicMock()
        client = _make_push_client(MagicMock(), MagicMock(), coordinator)
        shape_a = b'{"method":"thing.service.property.set","params":{"BroadcastTime":1}}'
        shape_a_repeat = b'{"method":"thing.service.property.set","params":{"BroadcastTime":2}}'
        shape_b = b"not-a-json-payload-at-all"

        with caplog.at_level(logging.DEBUG, logger="custom_components.rainpoint.api.mqtt"):
            client._dispatch_push("topic", shape_a)
            client._dispatch_push("topic", shape_a_repeat)
            client._dispatch_push("topic", shape_b)

        info_records = [r for r in caplog.records if r.levelno == logging.INFO and "unrecognized" in r.message]
        assert len(info_records) == 2
        debug_records = [r for r in caplog.records if r.levelno == logging.DEBUG]
        assert len(debug_records) == 3
        # No INFO record carries payload content, on either first-of-shape path.
        assert not any("BroadcastTime" in r.message or "not-a-json" in r.message for r in info_records)

    def test_first_shape_preview_is_withheld_when_debug_is_off(self, caplog):
        """Above DEBUG the announcement still fires, and the preview it would
        otherwise carry is never produced: the identifiers observed frames
        embed stay out of the default log."""
        coordinator = MagicMock()
        client = _make_push_client(MagicMock(), MagicMock(), coordinator)
        payload = b"#P26073118173000001682228223654|9|9|9#"

        with (
            caplog.at_level(logging.INFO, logger="custom_components.rainpoint.api.mqtt"),
            patch.object(mqtt_module, "_payload_preview") as preview,
        ):
            client._dispatch_push("topic", payload)

        preview.assert_not_called()
        info_records = [r for r in caplog.records if r.levelno == logging.INFO and "unrecognized" in r.message]
        assert len(info_records) == 1
        assert "16822282" not in info_records[0].message

    def test_unrecognized_shape_cap_stops_growth_and_further_info_records(self, caplog):
        coordinator = MagicMock()
        client = _make_push_client(MagicMock(), MagicMock(), coordinator)
        limit = mqtt_module.MQTT_UNRECOGNISED_SHAPE_LOG_LIMIT

        with caplog.at_level(logging.DEBUG, logger="custom_components.rainpoint.api.mqtt"):
            for i in range(limit + 2):
                # A distinct pipe-count per iteration guarantees a distinct shape key.
                payload = "|".join(["x"] * (i + 1)).encode()
                client._dispatch_push("topic", payload)

        assert len(client._unrecognised_shapes) == limit
        info_records = [r for r in caplog.records if r.levelno == logging.INFO and "unrecognized" in r.message]
        assert len(info_records) == limit


class TestAsyncDisconnect:
    """async_disconnect() tears down cleanly and tolerates a never-connected client."""

    @pytest.mark.asyncio
    async def test_disconnect_before_connect_is_a_noop(self):
        """Calling async_disconnect() before async_start() does not raise."""
        loop = asyncio.get_running_loop()
        hass = _make_hass(loop)
        fake_paho = _make_fake_paho()
        client = _make_mqtt_client(hass, fake_paho)

        await client.async_disconnect()  # must not raise

    @pytest.mark.asyncio
    async def test_disconnect_stops_loop_and_disconnects(self):
        """async_disconnect() calls loop_stop() then disconnect() on the underlying paho client."""
        loop = asyncio.get_running_loop()
        hass = _make_hass(loop)
        fake_paho = _make_fake_paho()
        client = _make_mqtt_client(hass, fake_paho)
        await client.async_start()
        await _settle()

        await client.async_disconnect()

        fake_paho.loop_stop.assert_called_once()
        fake_paho.disconnect.assert_called_once()


class TestConnectCallbackHandling:
    """on_connect/on_disconnect hop onto the HA loop before touching state."""

    @pytest.mark.asyncio
    async def test_on_connect_marks_connected_via_loop(self):
        """_on_connect schedules _handle_connect via call_soon_threadsafe; connected flips after it runs."""
        loop = asyncio.get_running_loop()
        hass = _make_hass(loop)
        fake_paho = _make_fake_paho()
        client = _make_mqtt_client(hass, fake_paho)
        await client.async_start()
        await _settle()

        assert client.connected is False
        client._on_connect(fake_paho, None, MagicMock(), 0, None)
        await asyncio.sleep(0)
        assert client.connected is True

        await client.async_disconnect()

    @pytest.mark.asyncio
    async def test_on_connect_nonzero_reason_code_reports_not_connected(self, caplog):
        """A non-zero reason_code (auth rejection) keeps connected False and warns."""
        loop = asyncio.get_running_loop()
        hass = _make_hass(loop)
        fake_paho = _make_fake_paho()
        client = _make_mqtt_client(hass, fake_paho)
        await client.async_start()
        await _settle()

        with caplog.at_level(logging.WARNING):
            client._on_connect(fake_paho, None, MagicMock(), 5, None)
            await asyncio.sleep(0)

        assert client.connected is False
        assert any("connect rejected" in r.message and "5" in r.message for r in caplog.records)

        await client.async_disconnect()

    @pytest.mark.asyncio
    async def test_on_disconnect_marks_disconnected_via_loop(self):
        """_on_disconnect schedules _handle_disconnect; connected flips false after it runs."""
        loop = asyncio.get_running_loop()
        hass = _make_hass(loop)
        fake_paho = _make_fake_paho()
        client = _make_mqtt_client(hass, fake_paho)
        await client.async_start()
        await _settle()

        client._on_connect(fake_paho, None, MagicMock(), 0, None)
        await asyncio.sleep(0)
        assert client.connected is True

        client._on_disconnect(fake_paho, None, MagicMock(), 0, None)
        await asyncio.sleep(0)
        assert client.connected is False

        await client.async_disconnect()


def test_module_defines_to_redact_and_redact_helper():
    """TO_REDACT + a redaction helper are established from the first credential-issuing commit.

    The helper was this module's own _redact() until it and client.py's
    byte-identical _redact_secret() were collapsed into one shared
    implementation in api/utils.py. What this test pins is unchanged: the
    module reaches a redaction helper with the length-plus-last-4 behaviour,
    and TO_REDACT still names deviceSecret.
    """
    assert "deviceSecret" in mqtt_module.TO_REDACT
    assert mqtt_module._redact_secret("SEKRIT-value-9f3a") == "len=17 last4=9f3a"
    assert mqtt_module._redact_secret(None) == "<empty>"
    assert mqtt_module._redact_secret("ab") == "len=2 <short>"


class TestPahoAutoReconnectDisabled:
    """paho's own auto-reconnect must never race the supervisor."""

    @pytest.mark.asyncio
    async def test_connect_passes_reconnect_on_failure_false_to_factory(self):
        """The paho client factory is called with reconnect_on_failure=False."""
        loop = asyncio.get_running_loop()
        hass = _make_hass(loop)
        fake_paho = _make_fake_paho()
        rainpoint_client = MagicMock()
        rainpoint_client.get_subscribe_status = AsyncMock(return_value=_fake_creds())
        factory = MagicMock(return_value=fake_paho)

        client = RainPointMqttClient(
            hass,
            rainpoint_client,
            entry=MagicMock(),
            hub_device_name="hub-device",
            hub_product_key="hub-pk",
            paho_client_factory=factory,
            time_source=lambda: 1000.0,
        )
        await client.async_start()
        await _settle()

        assert factory.call_args.kwargs["reconnect_on_failure"] is False

        await client.async_disconnect()


class TestBackoffDelay:
    """Exponential backoff is capped at a ceiling, with jitter."""

    def _client(self):
        hass = MagicMock()
        return _make_mqtt_client(hass, _make_fake_paho())

    def test_backoff_delay_monotonic_then_capped(self):
        """Pre-jitter base grows exponentially then flattens at the ceiling."""
        client = self._client()
        with patch.object(RainPointMqttClient, "_apply_jitter", staticmethod(lambda value: value)):
            delays = [client._backoff_delay(attempt) for attempt in range(1, 8)]

        assert delays == sorted(delays)
        assert delays[-1] == delays[-2] == mqtt_module._BACKOFF_CEILING_SECONDS
        assert delays[0] == mqtt_module._BACKOFF_BASE_SECONDS

    def test_backoff_delay_jitter_present_and_bounded(self):
        """Two calls at the same attempt differ (jitter) and never exceed ceiling*1.3."""
        client = self._client()
        samples = {client._backoff_delay(5) for _ in range(10)}

        assert len(samples) > 1
        assert all(delay <= mqtt_module._BACKOFF_CEILING_SECONDS * 1.3 for delay in samples)
        assert all(delay >= mqtt_module._BACKOFF_CEILING_SECONDS * 0.7 for delay in samples)


class TestSupervisorUnboundedRetry:
    """Six or more consecutive connect failures still schedule a further retry."""

    @pytest.mark.asyncio
    async def test_supervisor_retries_after_six_plus_failures(self):
        """A fake paho whose connect() raises repeatedly never permanently kills the supervisor."""
        loop = asyncio.get_running_loop()
        hass = _make_hass(loop)
        fake_paho = _make_fake_paho()
        fake_paho.connect.side_effect = [OSError("connection refused")] * 6 + [None] * 10
        client = _make_mqtt_client(hass, fake_paho)

        with patch.object(RainPointMqttClient, "_schedule_reconnect", new=AsyncMock(side_effect=_instant_sleep)):
            await client.async_start()
            await _settle(times=30)

        assert fake_paho.connect.call_count >= 7
        assert client._supervisor_task is not None
        assert not client._supervisor_task.done()

        await client.async_disconnect()

    @pytest.mark.asyncio
    async def test_supervisor_reraises_on_generic_exception_not_bounded_by_range(self):
        """No bounded range(N) governs the loop -- it keeps re-arming past any fixed count."""
        loop = asyncio.get_running_loop()
        hass = _make_hass(loop)
        fake_paho = _make_fake_paho()
        fake_paho.connect.side_effect = RainPointMqttError("boom")
        client = _make_mqtt_client(hass, fake_paho)

        with patch.object(RainPointMqttClient, "_schedule_reconnect", new=AsyncMock(side_effect=_instant_sleep)):
            await client.async_start()
            await _settle(times=50)

        assert fake_paho.connect.call_count > 10
        assert not client._supervisor_task.done()

        await client.async_disconnect()


def test_no_bounded_range_governs_reconnect():
    """No bounded attempt-count loop governs reconnect in the module source."""
    import inspect as _inspect

    source = _inspect.getsource(mqtt_module.RainPointMqttClient._run_supervisor)
    assert "range(" not in source


def _make_mqtt_client_with_distinct_paho_instances(hass, get_subscribe_status_mock=None) -> tuple:
    """Build a client whose paho factory returns a NEW mock instance per call, so
    tests can distinguish the old (pre-renewal) client from the new one."""
    rainpoint_client = MagicMock()
    rainpoint_client.get_subscribe_status = get_subscribe_status_mock or AsyncMock(return_value=_fake_creds())
    instances: list = []

    def _factory(*_args, **_kwargs):
        instance = MagicMock(spec=paho.Client)
        instances.append(instance)
        return instance

    factory = MagicMock(side_effect=_factory)
    client = RainPointMqttClient(
        hass,
        rainpoint_client,
        entry=MagicMock(),
        hub_device_name="hub-device",
        hub_product_key="hub-pk",
        paho_client_factory=factory,
        time_source=lambda: 1000.0,
    )
    return client, rainpoint_client, instances


class TestCredentialRenewal:
    """Clean disconnect-old/reconnect-new renewal cycle before ~570s expiry."""

    @pytest.mark.asyncio
    async def test_renewal_crosses_boundary_and_reconnects_with_fresh_creds(self):
        """Crossing the (patched-instant) renewal deadline re-fetches creds, disconnects
        the old paho client, and reconnects a new one."""
        loop = asyncio.get_running_loop()
        hass = _make_hass(loop)
        client, rainpoint_client, instances = _make_mqtt_client_with_distinct_paho_instances(hass)

        with patch.object(RainPointMqttClient, "_sleep", new=AsyncMock(side_effect=_instant_sleep)):
            await client.async_start()
            await _settle(times=15)

            assert rainpoint_client.get_subscribe_status.call_count >= 1
            first_paho = instances[0]

            await _settle(times=15)

        assert rainpoint_client.get_subscribe_status.call_count >= 2
        assert len(instances) >= 2
        first_paho.loop_stop.assert_called()
        first_paho.disconnect.assert_called()
        # The reconnected client still never subscribes.
        instances[-1].subscribe.assert_not_called()

        await client.async_disconnect()

    @pytest.mark.asyncio
    async def test_on_http_relogin_forces_immediate_reconnect_without_waiting_deadline(self):
        """on_http_relogin() re-fetches credentials and reconnects immediately -- the
        real (unpatched, ~510s) renewal delay is never actually waited out."""
        loop = asyncio.get_running_loop()
        hass = _make_hass(loop)
        client, rainpoint_client, instances = _make_mqtt_client_with_distinct_paho_instances(hass)

        await client.async_start()
        await _settle(times=15)
        assert rainpoint_client.get_subscribe_status.call_count == 1

        client.on_http_relogin()
        await _settle(times=15)

        assert rainpoint_client.get_subscribe_status.call_count >= 2
        assert len(instances) >= 2

        await client.async_disconnect()

    @pytest.mark.asyncio
    async def test_renewal_failure_does_not_kill_supervisor(self):
        """A get_subscribe_status failure during renewal is caught by the same
        retry path -- the supervisor keeps running, a retry is scheduled."""
        loop = asyncio.get_running_loop()
        hass = _make_hass(loop)
        call_state = {"n": 0}

        async def _flaky_get_subscribe_status(*_args, **_kwargs):
            call_state["n"] += 1
            if call_state["n"] == 2:
                raise RuntimeError("renewal boom")
            return _fake_creds()

        get_subscribe_status_mock = AsyncMock(side_effect=_flaky_get_subscribe_status)
        client, _rainpoint_client, _instances = _make_mqtt_client_with_distinct_paho_instances(hass, get_subscribe_status_mock)

        with patch.object(RainPointMqttClient, "_sleep", new=AsyncMock(side_effect=_instant_sleep)):
            await client.async_start()
            await _settle(times=40)

        assert call_state["n"] >= 3
        assert not client._supervisor_task.done()

        await client.async_disconnect()


class TestRenewalDelayFormula:
    """renew_in = max(120, expire - now - 60), jittered."""

    def _client(self):
        return _make_mqtt_client(MagicMock(), _make_fake_paho())

    def test_renewal_base_delay_formula(self):
        client = self._client()
        assert client._renewal_base_delay(expire_at=1570.0, now=1000.0) == 510.0

    def test_renewal_base_delay_clamps_to_floor_for_short_expire(self):
        client = self._client()
        assert client._renewal_base_delay(expire_at=1050.0, now=1000.0) == mqtt_module._RENEWAL_MIN_INTERVAL_SECONDS

    def test_renewal_delay_seconds_clips_positive_jitter_to_safe_deadline(self):
        # Base delay (510) equals the safe deadline (510) at this lifetime, so any
        # positive-signed jitter draw overshoots and must clip to exactly 510.0.
        client = self._client()
        with patch.object(mqtt_module.random, "choice", return_value=1.0):
            samples = {client._renewal_delay_seconds(1570.0, 1000.0) for _ in range(10)}

        assert samples == {510.0}

    def test_renewal_delay_seconds_applies_jitter_within_band(self):
        # Negative-signed draws are the only ones that vary, since positive draws
        # all clip to the same safe-deadline value above.
        client = self._client()
        with patch.object(mqtt_module.random, "choice", return_value=-1.0):
            samples = {client._renewal_delay_seconds(1570.0, 1000.0) for _ in range(10)}

        assert len(samples) > 1
        assert all(510.0 * 0.7 <= delay < 510.0 for delay in samples)

    def test_renewal_delay_never_exceeds_safe_deadline_under_max_jitter(self):
        """A short-lived credential must renew before expiry even when jitter and
        the 120s floor would otherwise push the delay past the expiry deadline."""
        client = self._client()
        now = 1000.0
        expire_at = now + 150.0  # 150s lifetime: base delay hits the 120s floor
        latest_safe = expire_at - now - mqtt_module._RENEWAL_SAFETY_MARGIN_SECONDS  # 90.0
        # Force jitter to inflate the delay far past the deadline; the cap must hold.
        with patch.object(RainPointMqttClient, "_apply_jitter", staticmethod(lambda value: value * 10)):
            delay = client._renewal_delay_seconds(expire_at, now)
        assert delay == latest_safe
        assert delay < (expire_at - now)  # renews strictly before expiry

    def _client_with_wall_clock(self, wall_now):
        rainpoint_client = MagicMock()
        rainpoint_client.get_subscribe_status = AsyncMock(return_value=_fake_creds())
        return RainPointMqttClient(
            MagicMock(),
            rainpoint_client,
            entry=MagicMock(),
            hub_device_name="h",
            hub_product_key="p",
            paho_client_factory=MagicMock(return_value=_make_fake_paho()),
            time_source=lambda: 1000.0,
            wall_clock_source=lambda: wall_now,
        )

    def test_credential_lifetime_defaults_when_absent(self):
        assert self._client()._credential_lifetime_seconds({}) == mqtt_module._DEFAULT_CREDENTIAL_LIFETIME_SECONDS

    def test_credential_lifetime_converts_absolute_expire_ms_to_remaining_seconds(self):
        # expire is an absolute epoch in MILLISECONDS; lifetime is (expire/1000 - wall_now).
        wall_now = 1_784_786_000.0
        client = self._client_with_wall_clock(wall_now)
        creds = {"expire": (wall_now + 300.0) * 1000}
        assert client._credential_lifetime_seconds(creds) == 300.0

    def test_credential_lifetime_clamps_far_future_expire_to_default_ceiling(self):
        wall_now = 1_784_786_000.0
        client = self._client_with_wall_clock(wall_now)
        creds = {"expire": (wall_now + 100_000.0) * 1000}
        assert client._credential_lifetime_seconds(creds) == mqtt_module._DEFAULT_CREDENTIAL_LIFETIME_SECONDS

    def test_credential_lifetime_falls_back_when_expire_already_passed(self):
        wall_now = 1_784_786_000.0
        client = self._client_with_wall_clock(wall_now)
        creds = {"expire": (wall_now - 50.0) * 1000}
        assert client._credential_lifetime_seconds(creds) == mqtt_module._DEFAULT_CREDENTIAL_LIFETIME_SECONDS


class TestProtocolTimestampUsesWallClock:
    """The clientId/HMAC timestamp is a wall-clock epoch value, not monotonic."""

    @pytest.mark.asyncio
    async def test_protocol_timestamp_uses_wall_clock_source_not_monotonic(self):
        """The clientId embeds int(wall_clock_source() * 1000), independent of the
        monotonic renewal seam (time_source)."""
        loop = asyncio.get_running_loop()
        hass = _make_hass(loop)
        fake_paho = _make_fake_paho()
        rainpoint_client = MagicMock()
        rainpoint_client.get_subscribe_status = AsyncMock(return_value=_fake_creds())
        factory = MagicMock(return_value=fake_paho)

        client = RainPointMqttClient(
            hass,
            rainpoint_client,
            entry=MagicMock(),
            hub_device_name="hub-device",
            hub_product_key="hub-pk",
            paho_client_factory=factory,
            time_source=lambda: 1000.0,  # monotonic seam -- must NOT feed the timestamp
            wall_clock_source=lambda: 1_700_000_000.0,  # wall-clock epoch seconds
        )

        await client.async_start()
        await _settle()

        client_id = factory.call_args.kwargs["client_id"]
        assert "timestamp=1700000000000" in client_id
        assert "timestamp=1000000" not in client_id  # the monotonic seam was not used

        await client.async_disconnect()


def test_on_http_relogin_is_a_public_method():
    """on_http_relogin() exists as the documented HTTP-re-login trigger."""
    assert hasattr(RainPointMqttClient, "on_http_relogin")
    assert not inspect.iscoroutinefunction(RainPointMqttClient.on_http_relogin)


class TestSupervisorTeardown:
    """async_disconnect cancels AND awaits the supervisor task,
    leaving no dangling task/thread/timer, and schedules no further reconnect."""

    @pytest.mark.asyncio
    async def test_disconnect_cancels_and_awaits_task_before_returning(self):
        """The supervisor task is done() immediately after async_disconnect() returns
        -- not merely cancel-requested -- so no dangling task remains."""
        loop = asyncio.get_running_loop()
        hass = _make_hass(loop)
        fake_paho = _make_fake_paho()
        client = _make_mqtt_client(hass, fake_paho)

        await client.async_start()
        await _settle()
        task = client._supervisor_task
        assert task is not None
        assert not task.done()

        await client.async_disconnect()

        assert task.done()
        assert client._supervisor_task is None
        fake_paho.loop_stop.assert_called_once()
        fake_paho.disconnect.assert_called_once()

    @pytest.mark.asyncio
    async def test_no_further_connect_attempt_after_disconnect(self):
        """Connect call count is frozen after teardown -- no reconnect is scheduled
        once teardown has begun, even though the loop would otherwise retry forever."""
        loop = asyncio.get_running_loop()
        hass = _make_hass(loop)
        fake_paho = _make_fake_paho()
        client = _make_mqtt_client(hass, fake_paho)

        await client.async_start()
        await _settle()
        assert fake_paho.connect.call_count == 1

        await client.async_disconnect()
        frozen_count = fake_paho.connect.call_count

        await _settle(times=15)

        assert fake_paho.connect.call_count == frozen_count

    @pytest.mark.asyncio
    async def test_disconnect_is_idempotent_after_start(self):
        """Calling async_disconnect() twice after async_start() raises nothing."""
        loop = asyncio.get_running_loop()
        hass = _make_hass(loop)
        fake_paho = _make_fake_paho()
        client = _make_mqtt_client(hass, fake_paho)

        await client.async_start()
        await _settle()

        await client.async_disconnect()
        await client.async_disconnect()  # must not raise

    @pytest.mark.asyncio
    async def test_disconnect_before_start_raises_nothing(self):
        """Calling async_disconnect() when async_start() was never invoked is a no-op."""
        loop = asyncio.get_running_loop()
        hass = _make_hass(loop)
        fake_paho = _make_fake_paho()
        client = _make_mqtt_client(hass, fake_paho)

        await client.async_disconnect()  # must not raise -- no task, no paho client

    @pytest.mark.asyncio
    async def test_no_task_leak_across_reload_cycles(self):
        """Repeated start/disconnect cycles (simulating unload-then-setup) leave no
        growth in live asyncio tasks attributable to the client."""
        loop = asyncio.get_running_loop()
        hass = _make_hass(loop)

        async def _one_cycle() -> None:
            fake_paho = _make_fake_paho()
            client = _make_mqtt_client(hass, fake_paho)
            await client.async_start()
            await _settle()
            await client.async_disconnect()

        await _one_cycle()
        await _settle()
        baseline = len(asyncio.all_tasks())

        for _ in range(3):
            await _one_cycle()
            await _settle()

        assert len(asyncio.all_tasks()) == baseline

    @pytest.mark.asyncio
    async def test_disconnect_cancels_pending_renewal_wait(self):
        """Cancelling the supervisor task while it is suspended inside the (long,
        unpatched) renewal wait still tears down cleanly and promptly."""
        loop = asyncio.get_running_loop()
        hass = _make_hass(loop)
        fake_paho = _make_fake_paho()
        client = _make_mqtt_client(hass, fake_paho)

        await client.async_start()
        await _settle(times=10)  # let it connect and settle into the (real, ~510s) wait
        assert client._paho is not None

        await client.async_disconnect()

        assert client._supervisor_task is None
        fake_paho.loop_stop.assert_called_once()
        fake_paho.disconnect.assert_called_once()

    @pytest.mark.asyncio
    async def test_supervisor_loop_exits_when_stopping_flag_set_without_cancel(self):
        """Setting _stopping directly (not via task.cancel()) still ends the loop
        the next time the top-of-loop condition is checked -- a stop flag is
        checked after every await, not only reachable via cancellation."""
        loop = asyncio.get_running_loop()
        hass = _make_hass(loop)
        fake_paho = _make_fake_paho()
        client = _make_mqtt_client(hass, fake_paho)

        async def _stop_after_connect() -> None:
            await _settle(times=10)
            client._stopping = True
            client._stop_event.set()

        stopper = asyncio.ensure_future(_stop_after_connect())
        supervisor = asyncio.ensure_future(client._run_supervisor())
        await asyncio.wait_for(supervisor, timeout=2)
        await stopper

        assert supervisor.done()
        assert not supervisor.cancelled()

    @pytest.mark.asyncio
    async def test_cancel_during_renew_propagates_via_explicit_reraise(self):
        """Cancelling the supervisor task while it's suspended inside _renew()
        itself (not the post-connect wait) still results in a cleanly
        cancelled task -- the explicit `except asyncio.CancelledError: raise`
        re-arms nothing and lets cancellation propagate."""
        loop = asyncio.get_running_loop()
        hass = _make_hass(loop)
        rainpoint_client = MagicMock()

        async def _hang_forever(*_args, **_kwargs):
            await asyncio.Event().wait()

        rainpoint_client.get_subscribe_status = AsyncMock(side_effect=_hang_forever)
        factory = MagicMock(return_value=_make_fake_paho())
        client = RainPointMqttClient(
            hass,
            rainpoint_client,
            entry=MagicMock(),
            hub_device_name="hub-device",
            hub_product_key="hub-pk",
            paho_client_factory=factory,
            time_source=lambda: 1000.0,
        )

        task = asyncio.ensure_future(client._run_supervisor())
        await _settle(times=5)  # supervisor is now hung inside _renew()'s get_subscribe_status await

        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert task.cancelled()

    @pytest.mark.asyncio
    async def test_disconnect_logs_and_swallows_non_cancelled_exception_from_task(self):
        """If the awaited supervisor task somehow raises something other than
        CancelledError during teardown, async_disconnect logs it rather than
        propagating (so a buggy task can never block unload)."""
        loop = asyncio.get_running_loop()
        hass = _make_hass(loop)
        fake_paho = _make_fake_paho()
        client = _make_mqtt_client(hass, fake_paho)

        async def _raises_on_cancel() -> None:
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                raise RuntimeError("boom during teardown") from None

        task = asyncio.ensure_future(_raises_on_cancel())
        await asyncio.sleep(0)
        client._supervisor_task = task

        await client.async_disconnect()  # must not raise

        assert task.done()
        assert client._supervisor_task is None

    @pytest.mark.asyncio
    async def test_disconnect_swallows_paho_teardown_exception(self, caplog):
        """A paho loop_stop()/disconnect() raising during teardown is logged, not
        propagated -- async_disconnect must never block unload."""
        loop = asyncio.get_running_loop()
        hass = _make_hass(loop)
        fake_paho = _make_fake_paho()
        client = _make_mqtt_client(hass, fake_paho)

        await client.async_start()
        await _settle()

        with (
            patch.object(client, "_disconnect_paho", side_effect=RuntimeError("paho boom")),
            caplog.at_level(logging.ERROR),
        ):
            await client.async_disconnect()  # must not raise

        assert any("paho teardown raised" in r.message for r in caplog.records)

    @pytest.mark.asyncio
    async def test_disconnect_paho_disconnects_before_stopping_loop(self):
        """paho's clean-shutdown order is disconnect() then loop_stop()."""
        loop = asyncio.get_running_loop()
        hass = _make_hass(loop)
        fake_paho = _make_fake_paho()
        client = _make_mqtt_client(hass, fake_paho)

        await client.async_start()
        await _settle()

        await client.async_disconnect()

        ordered = [c[0] for c in fake_paho.mock_calls if c[0] in ("disconnect", "loop_stop")]
        assert ordered.index("disconnect") < ordered.index("loop_stop")

    @pytest.mark.asyncio
    async def test_async_disconnect_reraises_when_itself_cancelled(self):
        """If async_disconnect is itself cancelled while awaiting the supervisor
        task's own cancellation, the CancelledError must propagate rather than be
        swallowed as the (expected) supervisor cancellation."""
        loop = asyncio.get_running_loop()
        hass = _make_hass(loop)
        fake_paho = _make_fake_paho()
        client = _make_mqtt_client(hass, fake_paho)

        started = asyncio.Event()
        cancels = {"n": 0}

        async def _supervisor_stub() -> None:
            """Stand-in supervisor that swallows the FIRST cancellation (the one
            async_disconnect issues via task.cancel()), so async_disconnect stays
            parked at `await task`. It honors the SECOND cancellation -- the one
            forwarded when we cancel the async_disconnect task itself -- so that
            `await task` finally raises CancelledError back into async_disconnect
            while its own cancelling() count is positive."""
            started.set()
            while True:
                try:
                    await asyncio.sleep(3600)
                except asyncio.CancelledError:
                    cancels["n"] += 1
                    if cancels["n"] >= 2:
                        raise
                    continue

        supervisor = asyncio.ensure_future(_supervisor_stub())
        await started.wait()
        client._supervisor_task = supervisor

        disconnect_task = asyncio.ensure_future(client.async_disconnect())
        await _settle()  # let async_disconnect reach `await task` (first cancel swallowed)

        disconnect_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await disconnect_task

        assert supervisor.cancelled()


class TestBrokerHostSelection:
    """The broker host comes from the credential-provided mqttHostUrl (or the
    templated host when absent); the port is always the TLS port, never the
    plaintext one the mqttHostUrl advertises."""

    @pytest.mark.asyncio
    async def test_connect_uses_credential_host_and_ignores_advertised_plaintext_port(self):
        """mqttHostUrl "host:1883" yields that host but the TLS port, not 1883."""
        loop = asyncio.get_running_loop()
        hass = _make_hass(loop)
        fake_paho = _make_fake_paho()
        client = _make_mqtt_client(hass, fake_paho)  # _fake_creds carries mqttHostUrl :1883

        await client.async_start()
        await _settle()

        host, port, _keepalive = fake_paho.connect.call_args.args
        assert host == "pk123.iot-as-mqtt.us-west-1.aliyuncs.com"
        # The advertised plaintext 1883 is ignored: connect uses the const, and
        # the const pins the TLS port 8883 as a transport-contract invariant.
        assert mqtt_module.MQTT_BROKER_PORT == 8883
        assert port == mqtt_module.MQTT_BROKER_PORT

        await client.async_disconnect()

    @pytest.mark.asyncio
    async def test_connect_falls_back_to_template_host_when_url_absent(self):
        """No mqttHostUrl -> templated host + the TLS MQTT_BROKER_PORT."""
        loop = asyncio.get_running_loop()
        hass = _make_hass(loop)
        fake_paho = _make_fake_paho()
        creds = _fake_creds()
        creds.pop("mqttHostUrl")
        client = _make_mqtt_client(hass, fake_paho, creds=creds)

        await client.async_start()
        await _settle()

        host, port, _keepalive = fake_paho.connect.call_args.args
        assert host == "pk123.iot-as-mqtt.us-west-1.aliyuncs.com"
        assert port == mqtt_module.MQTT_BROKER_PORT

        await client.async_disconnect()

    @pytest.mark.asyncio
    async def test_connect_uses_bare_host_url_and_tls_port(self):
        """mqttHostUrl with a bare host (no port) -> that host + the TLS port."""
        loop = asyncio.get_running_loop()
        hass = _make_hass(loop)
        fake_paho = _make_fake_paho()
        creds = _fake_creds()
        creds["mqttHostUrl"] = "broker.example.com"
        client = _make_mqtt_client(hass, fake_paho, creds=creds)

        await client.async_start()
        await _settle()

        host, port, _keepalive = fake_paho.connect.call_args.args
        assert host == "broker.example.com"
        assert port == mqtt_module.MQTT_BROKER_PORT

        await client.async_disconnect()


class TestTlsConnection:
    """The broker connection is TLS, verified against the pinned private root CA."""

    @pytest.mark.asyncio
    async def test_connect_calls_tls_set_with_pinned_ca_before_connecting(self):
        """tls_set(ca_certs=MQTT_TLS_CA_CERT) is invoked, and before connect()."""
        loop = asyncio.get_running_loop()
        hass = _make_hass(loop)
        fake_paho = _make_fake_paho()

        call_order = []
        fake_paho.tls_set.side_effect = lambda *a, **k: call_order.append("tls_set")
        fake_paho.connect.side_effect = lambda *a, **k: call_order.append("connect")

        client = _make_mqtt_client(hass, fake_paho)
        await client.async_start()
        await _settle()

        fake_paho.tls_set.assert_called_once_with(ca_certs=mqtt_module.MQTT_TLS_CA_CERT)
        assert call_order == ["tls_set", "connect"], "tls_set must precede connect"

        await client.async_disconnect()

    def test_pinned_ca_file_exists_and_matches_published_md5(self):
        """The vendored CA is Aliyun's published root, guarded by its MD5.

        Aliyun publishes ali_iot_ca.crt (the 8883/TLS root, "Aliyun IoT Root CA")
        with MD5 c7a6afb466713832af778a7bcb6d1aef. A mismatch means the pinned
        trust anchor was corrupted or swapped -- fail loudly rather than ship a
        cert that will not verify the live broker.
        """
        import hashlib
        from pathlib import Path

        ca_path = Path(mqtt_module.MQTT_TLS_CA_CERT)
        assert ca_path.is_file(), f"pinned CA missing at {ca_path}"
        digest = hashlib.md5(ca_path.read_bytes(), usedforsecurity=False).hexdigest()
        assert digest == "c7a6afb466713832af778a7bcb6d1aef"


class TestStateListeners:
    """State listeners fire on every connect/disconnect/message transition so the
    push diagnostic entities can re-render (their live state is not in coordinator.data)."""

    def _make_offline_client(self):
        """A client with no running supervisor -- state handlers are driven directly."""
        hass = MagicMock()
        return _make_mqtt_client(hass, _make_fake_paho())

    def test_add_and_remove_state_listener(self):
        """A removed listener is no longer fired; remove is tolerant of an unknown listener."""
        client = self._make_offline_client()
        listener = MagicMock()

        client.add_state_listener(listener)
        client._handle_connect(0)
        assert listener.call_count == 1

        client.remove_state_listener(listener)
        client._handle_connect(0)
        assert listener.call_count == 1  # not fired again after removal

        # Removing an unregistered listener must not raise.
        client.remove_state_listener(MagicMock())

    def test_handle_connect_fires_listeners(self):
        """A successful connect notifies every registered listener."""
        client = self._make_offline_client()
        listener = MagicMock()
        client.add_state_listener(listener)

        client._handle_connect(0)

        listener.assert_called_once_with()
        assert client.connected is True

    def test_handle_disconnect_fires_listeners(self):
        """A disconnect notifies every registered listener."""
        client = self._make_offline_client()
        listener = MagicMock()
        client.add_state_listener(listener)

        client._handle_disconnect(0)

        listener.assert_called_once_with()
        assert client.connected is False

    def test_handle_message_fires_listeners(self):
        """An inbound message notifies every registered listener (and stamps liveness)."""
        client = self._make_offline_client()
        listener = MagicMock()
        client.add_state_listener(listener)

        client._handle_message("topic/x", b"{}")

        listener.assert_called_once_with()
        assert client.last_message_at == 1000.0

    def test_listener_that_unregisters_during_callback_does_not_break_iteration(self):
        """A listener removing itself mid-notify is safe (iteration copies the list)."""
        client = self._make_offline_client()
        calls = []

        def self_removing():
            calls.append("fired")
            client.remove_state_listener(self_removing)

        other = MagicMock()
        client.add_state_listener(self_removing)
        client.add_state_listener(other)

        client._handle_connect(0)

        assert calls == ["fired"]
        other.assert_called_once_with()
