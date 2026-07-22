"""
RainPoint MQTT push channel client.

Connects to the Alibaba Cloud IoT (Aliyun) MQTT broker using fresh,
per-session observer credentials fetched from RainPointClient.get_subscribe_status().
This is the Phase 9 proof-of-pipe: it connects once, subscribes to both
candidate state topics, and logs message receipt. It never touches
coordinator state -- feeding push data into coordinator.data is Phase 10.

Transport is plain TCP, no TLS (spike-confirmed, D-11) -- this overrides any
TLS assumption elsewhere. Every paho on_* callback runs on paho's own network
thread and must never touch HA/integration state directly; each hops onto the
HA event loop via hass.loop.call_soon_threadsafe into an @callback method
before touching self (CONN-02/D-08).
"""

import hashlib
import hmac
import logging
import time

import paho.mqtt.client as paho_mqtt
from homeassistant.core import HomeAssistant, callback

from ..const import (
    MQTT_BROKER_HOST_TEMPLATE,
    MQTT_BROKER_PORT,
    MQTT_KEEPALIVE,
    MQTT_TOPIC_EVENT_POST,
    MQTT_TOPIC_PROPERTY_SET,
)
from .client import RainPointClient

_LOGGER = logging.getLogger(__name__)


class RainPointMqttError(Exception):
    pass


# Fields that must never appear in the clear in logs/diagnostics (CRED-03/D-16).
# Established here from the first credential-issuing commit so redaction is a
# drop-in when diagnostics.py arrives, never a retrofit.
TO_REDACT = {"deviceSecret", "mqtt_password", "client_id"}


def _redact(value: str | None) -> str:
    """Render a secret as length + last-4 only -- never the raw value."""
    if not value:
        return "<empty>"
    if len(value) <= 4:
        return f"len={len(value)} <short>"
    return f"len={len(value)} last4={value[-4:]}"


class RainPointMqttClient:
    """Single-connect push channel to the RainPoint MQTT broker (proof-of-pipe).

    Connects once, subscribes to both candidate state topics, and logs
    message receipt with a running count -- never the payload contents
    (D-03). The supervised reconnect/renewal state machine is expansion
    work in a later plan; this client proves the pipe end-to-end first.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        client: RainPointClient,
        entry,
        hub_device_name: str,
        hub_product_key: str,
        *,
        paho_client_factory=paho_mqtt.Client,
        time_source=time.monotonic,
    ) -> None:
        self._hass = hass
        self._client = client
        self._entry = entry
        self._hub_device_name = hub_device_name
        self._hub_product_key = hub_product_key
        self._paho_client_factory = paho_client_factory
        self._time_source = time_source

        self._paho = None
        self._message_count = 0
        self._connected = False

    @property
    def message_count(self) -> int:
        """Return the running count of messages received since connect (test/diagnostic seam)."""
        return self._message_count

    @property
    def connected(self) -> bool:
        """Return whether the last on_connect callback reported success."""
        return self._connected

    async def async_start(self) -> None:
        """Fetch fresh observer credentials, connect once, and subscribe to both topics.

        Never raises out to the caller in a way that should block config-entry
        setup -- callers must schedule this as a background task (PUSH-04/D-09).
        """
        creds = await self._client.get_subscribe_status(self._hub_device_name, self._hub_product_key)
        self._connect(creds)
        self._subscribe(creds)

    def _connect(self, creds: dict) -> None:
        """Build the paho client from fresh creds and connect. No TLS (D-11)."""
        device_name = creds.get("deviceName", "")
        product_key = creds.get("productKey", "")
        device_secret = creds.get("deviceSecret", "")

        timestamp_ms = int(self._time_source() * 1000)
        username = f"{device_name}&{product_key}"
        client_id = f"{device_name}|securemode=2,signmethod=hmacsha1,timestamp={timestamp_ms}|"
        sign_content = f"clientId{device_name}deviceName{device_name}productKey{product_key}timestamp{timestamp_ms}"
        password = hmac.new(device_secret.encode(), sign_content.encode(), hashlib.sha1).hexdigest()

        _LOGGER.debug(
            "RainPoint MQTT connecting: username=%s client_id=%s password=%s",
            username,
            _redact(client_id),
            _redact(password),
        )

        paho_client = self._paho_client_factory(paho_mqtt.CallbackAPIVersion.VERSION2, client_id=client_id)
        paho_client.username_pw_set(username, password)
        paho_client.on_connect = self._on_connect
        paho_client.on_message = self._on_message
        paho_client.on_disconnect = self._on_disconnect

        host = MQTT_BROKER_HOST_TEMPLATE.format(product_key=product_key)
        paho_client.connect(host, MQTT_BROKER_PORT, MQTT_KEEPALIVE)
        paho_client.loop_start()

        self._paho = paho_client
        self._device_name = device_name
        self._product_key = product_key

    def _subscribe(self, creds: dict) -> None:
        """Subscribe to both candidate state topics (D-04, empirically resolved in Phase 10)."""
        if self._paho is None:
            raise RainPointMqttError("subscribe called before connect")
        device_name = creds.get("deviceName", "")
        product_key = creds.get("productKey", "")
        for template in (MQTT_TOPIC_PROPERTY_SET, MQTT_TOPIC_EVENT_POST):
            topic = template.format(product_key=product_key, device_name=device_name)
            self._paho.subscribe(topic)

    # --- paho callbacks: run on paho's network thread, never on the HA loop.
    # Only cheap, pure reads are allowed here; everything else is dispatched
    # via hass.loop.call_soon_threadsafe into an @callback method (CONN-02/D-08).

    def _on_connect(self, client, userdata, flags, reason_code, properties=None) -> None:
        """Runs on paho's network thread."""
        self._hass.loop.call_soon_threadsafe(self._handle_connect, reason_code)

    def _on_disconnect(self, client, userdata, flags, reason_code, properties=None) -> None:
        """Runs on paho's network thread."""
        self._hass.loop.call_soon_threadsafe(self._handle_disconnect, reason_code)

    def _on_message(self, client, userdata, msg) -> None:
        """Runs on paho's network thread. Reads only topic/payload -- no state mutation."""
        topic = msg.topic
        payload_len = len(msg.payload)
        self._hass.loop.call_soon_threadsafe(self._handle_message, topic, payload_len)

    # --- @callback methods: run on the HA event loop only.

    @callback
    def _handle_connect(self, reason_code) -> None:
        """Runs on the HA event loop."""
        self._connected = True
        _LOGGER.debug("RainPoint MQTT connected: reason_code=%s", reason_code)

    @callback
    def _handle_disconnect(self, reason_code) -> None:
        """Runs on the HA event loop."""
        self._connected = False
        _LOGGER.debug("RainPoint MQTT disconnected: reason_code=%s", reason_code)

    @callback
    def _handle_message(self, topic: str, payload_len: int) -> None:
        """Runs on the HA event loop. Logs receipt only -- never payload contents (D-03)."""
        self._message_count += 1
        _LOGGER.debug("RainPoint MQTT message received: topic=%s len=%s count=%s", topic, payload_len, self._message_count)

    async def async_disconnect(self) -> None:
        """Tear down the single connection. Tolerant of a never-connected client."""
        if self._paho is None:
            return
        try:
            self._paho.loop_stop()
        finally:
            self._paho.disconnect()
