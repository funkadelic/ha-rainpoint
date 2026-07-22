"""
RainPoint MQTT push channel client.

Connects to the Alibaba Cloud IoT (Aliyun) MQTT broker using fresh,
per-session observer credentials fetched from RainPointClient.get_subscribe_status().
A single owned asyncio.Task (_run_supervisor) drives the full lifecycle --
connect, subscribe, wait, renew, reconnect -- indefinitely under jittered
backoff (CONN-01/D-05). It subscribes to both candidate state topics and
logs message receipt; it never touches coordinator state -- feeding push
data into coordinator.data is Phase 10.

Transport is plain TCP, no TLS (spike-confirmed, D-11) -- this overrides any
TLS assumption elsewhere. Every paho on_* callback runs on paho's own network
thread and must never touch HA/integration state directly; each hops onto the
HA event loop via hass.loop.call_soon_threadsafe into an @callback method
before touching self (CONN-02/D-08).
"""

import asyncio
import hashlib
import hmac
import logging
import random
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


# Supervisor backoff (CONN-01/D-05, Pitfall 2/7): only the DELAY is capped, never
# the attempt count. 30s base doubling up to a 480s ceiling (within the 300-600s
# band), with +-10-30% jitter to avoid a synchronized reconnect storm.
_BACKOFF_BASE_SECONDS = 30.0
_BACKOFF_CEILING_SECONDS = 480.0
_JITTER_MIN_FRACTION = 0.10
_JITTER_MAX_FRACTION = 0.30


class RainPointMqttClient:
    """Supervised push channel to the RainPoint MQTT broker.

    One owned asyncio.Task (_run_supervisor) connects, subscribes, and stays
    connected; on any connect failure it retries indefinitely under jittered
    backoff. paho's own auto-reconnect is disabled so the supervisor is the
    sole reconnect authority (CONN-01/D-05).
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

        self._stopping = False
        self._supervisor_task: asyncio.Task | None = None
        self._stop_event = asyncio.Event()

    @property
    def message_count(self) -> int:
        """Return the running count of messages received since connect (test/diagnostic seam)."""
        return self._message_count

    @property
    def connected(self) -> bool:
        """Return whether the last on_connect callback reported success."""
        return self._connected

    # --- supervisor lifecycle (CONN-01/D-05) ---

    async def async_start(self) -> None:
        """Launch the supervisor task that owns connect->run->reconnect.

        Never blocks -- callers must schedule this as a background task; the
        supervisor itself retries indefinitely and this method never raises
        out in a way that should block config-entry setup (PUSH-04/D-09).
        """
        self._stopping = False
        self._stop_event.clear()
        self._supervisor_task = self._hass.loop.create_task(self._run_supervisor())

    async def _run_supervisor(self) -> None:
        """Own connect -> run -> reconnect indefinitely (D-05).

        Only the backoff DELAY is capped -- never the attempt count (Pitfall 2).
        Any exception in the loop body still results in a scheduled retry
        (Pitfall 7): there is no bare return and no uncaught exception that
        silently ends the chain.
        """
        attempt = 0
        while not self._stopping:
            try:
                await self._connect_and_subscribe()
            except asyncio.CancelledError:
                raise
            except Exception as err:  # must always re-arm, see Pitfall 2/7
                attempt += 1
                delay = self._backoff_delay(attempt)
                _LOGGER.warning(
                    "RainPoint MQTT connect failed (attempt=%s): %s; retrying in %.1fs",
                    attempt,
                    err,
                    delay,
                )
                await self._schedule_reconnect(delay)
                continue

            attempt = 0
            await self._stop_event.wait()
        # Loop exits only when self._stopping is set.

    async def _schedule_reconnect(self, delay: float) -> None:
        """Sleep for the computed backoff delay before the next attempt.

        A thin wrapper around asyncio.sleep so tests can patch a single
        module-level seam without real wall-clock waiting.
        """
        await asyncio.sleep(delay)

    def _backoff_delay(self, attempt: int) -> float:
        """Exponential backoff capped at a ceiling, with jitter (D-05/Pitfall 7).

        There is no cap on `attempt` itself -- the supervisor retries forever
        (Pitfall 2) -- so the exponent is clamped defensively before raising
        2**exponent to avoid an OverflowError on very large attempt counts;
        the ceiling is already reached at a small exponent in practice.
        """
        exponent = min(max(attempt - 1, 0), 32)
        base = min(_BACKOFF_BASE_SECONDS * (2**exponent), _BACKOFF_CEILING_SECONDS)
        return self._apply_jitter(base)

    @staticmethod
    def _apply_jitter(value: float) -> float:
        """Apply +-10-30% random jitter to avoid a synchronized reconnect storm (Pitfall 7)."""
        magnitude = random.uniform(_JITTER_MIN_FRACTION, _JITTER_MAX_FRACTION)
        sign = random.choice((-1.0, 1.0))
        return value * (1 + sign * magnitude)

    async def _connect_and_subscribe(self) -> None:
        """Fetch fresh credentials, connect, and subscribe to both topics."""
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

        # The supervisor is the sole reconnect authority (CONN-01/D-05) -- paho
        # must never attempt its own reconnect_on_failure behavior in parallel.
        # reconnect_on_failure is a paho-mqtt constructor-only kwarg (no public
        # setter), so it must be passed here rather than set post-construction.
        paho_client = self._paho_client_factory(
            paho_mqtt.CallbackAPIVersion.VERSION2, client_id=client_id, reconnect_on_failure=False
        )
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

    def _disconnect_paho(self) -> None:
        """Cleanly stop and disconnect the current paho client, if any.

        Tolerant of no active client.
        """
        paho_client, self._paho = self._paho, None
        if paho_client is None:
            return
        try:
            paho_client.loop_stop()
        finally:
            paho_client.disconnect()

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
        """Tear down the connection. Tolerant of a never-connected client."""
        if self._paho is None:
            return
        try:
            self._paho.loop_stop()
        finally:
            self._paho.disconnect()
