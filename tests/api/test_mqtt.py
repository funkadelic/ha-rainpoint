"""Tests for RainPointMqttClient (PUSH-04, CRED-01/03, CONN-01/02, D-03/D-04/D-05/D-08/D-14)."""

import asyncio
import inspect
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import paho.mqtt.client as paho
import pytest

import custom_components.rainpoint.api.mqtt as mqtt_module
from custom_components.rainpoint.api.mqtt import RainPointMqttClient, RainPointMqttError

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
    """D-14: constructor accepts an injectable paho factory + time source."""

    def test_constructor_exposes_test_seams(self):
        """paho_client_factory and time_source are keyword-only constructor params."""
        sig = inspect.signature(RainPointMqttClient.__init__)
        assert "paho_client_factory" in sig.parameters
        assert "time_source" in sig.parameters


class TestAsyncStartSubscribesBothTopics:
    """D-04: subscribe to both service/property/set and event/property/post."""

    @pytest.mark.asyncio
    async def test_async_start_subscribes_both_topics(self):
        """async_start() launches the supervisor, which subscribes both formatted topics."""
        loop = asyncio.get_running_loop()
        hass = _make_hass(loop)
        fake_paho = _make_fake_paho()
        client = _make_mqtt_client(hass, fake_paho)

        await client.async_start()
        await _settle()

        subscribed_topics = [call.args[0] for call in fake_paho.subscribe.call_args_list]
        assert "/sys/pk123/name-A/thing/service/property/set" in subscribed_topics
        assert "/sys/pk123/name-A/thing/event/property/post" in subscribed_topics
        assert len(subscribed_topics) == 2

        await client.async_disconnect()

    @pytest.mark.asyncio
    async def test_subscribe_before_connect_raises(self):
        """_subscribe() called before _connect() raises RainPointMqttError (no live socket yet)."""
        loop = asyncio.get_running_loop()
        hass = _make_hass(loop)
        fake_paho = _make_fake_paho()
        client = _make_mqtt_client(hass, fake_paho)

        with pytest.raises(RainPointMqttError):
            client._subscribe(_fake_creds())


class TestMessageReceiptLogging:
    """D-03: log topic + byte-length + running count at DEBUG, never payload contents."""

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


class TestSecretRedaction:
    """CRED-03/D-16: no log line emits deviceSecret, the derived password, or a full clientId."""

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
    """on_connect/on_disconnect hop onto the HA loop before touching state (CONN-02/D-08)."""

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
        """A non-zero reason_code (auth rejection) keeps connected False and warns (CR-02)."""
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
    """TO_REDACT + _redact() are established from the first credential-issuing commit (D-16)."""
    assert "deviceSecret" in mqtt_module.TO_REDACT
    assert mqtt_module._redact("SEKRIT-value-9f3a") == "len=17 last4=9f3a"
    assert mqtt_module._redact(None) == "<empty>"
    assert mqtt_module._redact("ab") == "len=2 <short>"


class TestPahoAutoReconnectDisabled:
    """CONN-01/D-05: paho's own auto-reconnect must never race the supervisor."""

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
    """D-05/Pitfall 7: exponential backoff capped at a ceiling, with jitter."""

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
    """D-15a/CONN-01: 6+ consecutive connect failures still schedule a further retry."""

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
    """Pitfall 2 guard: no bounded attempt-count loop governs reconnect in the module source."""
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
    """CRED-02/D-06: clean disconnect-old/reconnect-new renewal cycle before ~570s expiry."""

    @pytest.mark.asyncio
    async def test_renewal_crosses_boundary_and_reconnects_with_fresh_creds(self):
        """Crossing the (patched-instant) renewal deadline re-fetches creds, disconnects
        the old paho client, and reconnects+resubscribes a new one (D-15b/D-06)."""
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
        # The new client subscribed to both topics again after renewal.
        assert instances[-1].subscribe.call_count == 2

        await client.async_disconnect()

    @pytest.mark.asyncio
    async def test_on_http_relogin_forces_immediate_reconnect_without_waiting_deadline(self):
        """on_http_relogin() re-fetches credentials and reconnects immediately -- the
        real (unpatched, ~510s) renewal delay is never actually waited out (D-07)."""
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
    """D-06: renew_in = max(120, expire - now - 60), jittered."""

    def _client(self):
        return _make_mqtt_client(MagicMock(), _make_fake_paho())

    def test_renewal_base_delay_formula(self):
        client = self._client()
        assert client._renewal_base_delay(expire_at=1570.0, now=1000.0) == 510.0

    def test_renewal_base_delay_clamps_to_floor_for_short_expire(self):
        client = self._client()
        assert client._renewal_base_delay(expire_at=1050.0, now=1000.0) == mqtt_module._RENEWAL_MIN_INTERVAL_SECONDS

    def test_renewal_delay_seconds_applies_jitter_within_band(self):
        client = self._client()
        samples = {client._renewal_delay_seconds(1570.0, 1000.0) for _ in range(10)}

        assert len(samples) > 1
        assert all(510.0 * 0.7 <= delay <= 510.0 * 1.3 for delay in samples)

    def test_credential_lifetime_defaults_when_absent(self):
        assert RainPointMqttClient._credential_lifetime_seconds({}) == mqtt_module._DEFAULT_CREDENTIAL_LIFETIME_SECONDS

    def test_credential_lifetime_uses_expire_field_when_present(self):
        assert RainPointMqttClient._credential_lifetime_seconds({"expire": 300}) == 300.0


class TestProtocolTimestampUsesWallClock:
    """WR-05: the clientId/HMAC timestamp is a wall-clock epoch value, not monotonic."""

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
    """D-07: on_http_relogin() exists as the documented HTTP-re-login trigger."""
    assert hasattr(RainPointMqttClient, "on_http_relogin")
    assert not inspect.iscoroutinefunction(RainPointMqttClient.on_http_relogin)


class TestSupervisorTeardown:
    """CONN-03/D-10, D-15c: async_disconnect cancels AND awaits the supervisor task,
    leaving no dangling task/thread/timer, and schedules no further reconnect."""

    @pytest.mark.asyncio
    async def test_disconnect_cancels_and_awaits_task_before_returning(self):
        """The supervisor task is done() immediately after async_disconnect() returns
        -- not merely cancel-requested -- so no dangling task remains (D-15c)."""
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
        growth in live asyncio tasks attributable to the client (D-10/D-15c)."""
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
        propagated -- async_disconnect must never block unload (WR-03)."""
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
