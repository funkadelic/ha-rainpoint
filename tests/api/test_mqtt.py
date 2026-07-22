"""Tests for RainPointMqttClient (PUSH-04, CRED-01/03, CONN-02, D-03/D-04/D-08/D-14)."""

import asyncio
import inspect
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

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
    """Make hass helper with a real event loop wired in."""
    hass = MagicMock()
    hass.loop = loop
    return hass


def _make_fake_paho() -> MagicMock:
    """Make a MagicMock spec'd against the real paho Client for attribute/method fidelity."""
    return MagicMock(spec=paho.Client)


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
        """async_start() calls client.subscribe for both formatted topics."""
        loop = asyncio.get_running_loop()
        hass = _make_hass(loop)
        fake_paho = _make_fake_paho()
        client = _make_mqtt_client(hass, fake_paho)

        await client.async_start()

        subscribed_topics = [call.args[0] for call in fake_paho.subscribe.call_args_list]
        assert "/sys/pk123/name-A/thing/service/property/set" in subscribed_topics
        assert "/sys/pk123/name-A/thing/event/property/post" in subscribed_topics
        assert len(subscribed_topics) == 2

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

    @pytest.mark.asyncio
    async def test_message_count_increments_across_two_messages(self):
        """The running count increments once per delivered message."""
        loop = asyncio.get_running_loop()
        hass = _make_hass(loop)
        fake_paho = _make_fake_paho()
        client = _make_mqtt_client(hass, fake_paho)
        await client.async_start()

        msg1 = SimpleNamespace(topic="/sys/pk123/name-A/thing/service/property/set", payload=b"one")
        msg2 = SimpleNamespace(topic="/sys/pk123/name-A/thing/event/property/post", payload=b"two")

        client._on_message(fake_paho, None, msg1)
        await asyncio.sleep(0)
        assert client.message_count == 1

        client._on_message(fake_paho, None, msg2)
        await asyncio.sleep(0)
        assert client.message_count == 2

    @pytest.mark.asyncio
    async def test_on_message_never_mutates_state_directly(self):
        """_on_message only schedules work via call_soon_threadsafe -- no direct mutation."""
        loop = asyncio.get_running_loop()
        hass = MagicMock()
        hass.loop = MagicMock()  # replaced with a plain mock: call_soon_threadsafe never fires
        fake_paho = _make_fake_paho()
        client = _make_mqtt_client(hass, fake_paho)
        await client.async_start()

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

        await asyncio.sleep(0)
        _ = loop  # silence unused-variable style checks; loop unused in this mocked-loop test


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

        assert client.connected is False
        client._on_connect(fake_paho, None, MagicMock(), 0, None)
        await asyncio.sleep(0)
        assert client.connected is True

    @pytest.mark.asyncio
    async def test_on_disconnect_marks_disconnected_via_loop(self):
        """_on_disconnect schedules _handle_disconnect; connected flips false after it runs."""
        loop = asyncio.get_running_loop()
        hass = _make_hass(loop)
        fake_paho = _make_fake_paho()
        client = _make_mqtt_client(hass, fake_paho)
        await client.async_start()

        client._on_connect(fake_paho, None, MagicMock(), 0, None)
        await asyncio.sleep(0)
        assert client.connected is True

        client._on_disconnect(fake_paho, None, MagicMock(), 0, None)
        await asyncio.sleep(0)
        assert client.connected is False


def test_module_defines_to_redact_and_redact_helper():
    """TO_REDACT + _redact() are established from the first credential-issuing commit (D-16)."""
    assert "deviceSecret" in mqtt_module.TO_REDACT
    assert mqtt_module._redact("SEKRIT-value-9f3a") == "len=17 last4=9f3a"
    assert mqtt_module._redact(None) == "<empty>"
    assert mqtt_module._redact("ab") == "len=2 <short>"
