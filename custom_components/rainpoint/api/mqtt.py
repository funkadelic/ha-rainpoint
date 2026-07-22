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

# Credential renewal cadence (CRED-02/D-06): max(120, expire - now - 60), jittered.
# The exact subscribeStatus expiry field name was not captured empirically by the
# Phase 8 spike (FINDINGS.md), so absence defaults to the spike-observed ~570s
# disconnect-old/reconnect-new cycle.
_RENEWAL_MIN_INTERVAL_SECONDS = 120.0
_RENEWAL_SAFETY_MARGIN_SECONDS = 60.0
_DEFAULT_CREDENTIAL_LIFETIME_SECONDS = 570.0


class RainPointMqttClient:
    """Supervised push channel to the RainPoint MQTT broker.

    One owned asyncio.Task (_run_supervisor) connects, subscribes, renews
    credentials before their ~570s expiry via a clean disconnect-old/
    reconnect-new cycle, and retries indefinitely under jittered backoff on
    any failure. paho's own auto-reconnect is disabled so the supervisor is
    the sole reconnect authority (CONN-01/D-05, CRED-02/D-06).
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
        self._renew_event = asyncio.Event()

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
        """Own connect -> run -> renew -> reconnect indefinitely (D-05, D-06).

        _renew() is reused both for the very first connect and every
        subsequent renewal cycle, so there is exactly one state machine here
        -- not two independent call_later chains that could silently diverge
        (Pitfall 6). Only the backoff DELAY is capped -- never the attempt
        count (Pitfall 2). Any exception in the loop body (connect failure or
        renewal failure alike) still results in a scheduled retry (Pitfall 7):
        there is no bare return and no uncaught exception that silently ends
        the chain.
        """
        attempt = 0
        while not self._stopping:
            try:
                renew_in = await self._renew()
            except asyncio.CancelledError:
                raise
            except Exception as err:  # must always re-arm, see Pitfall 2/6/7
                attempt += 1
                delay = self._backoff_delay(attempt)
                _LOGGER.warning(
                    "RainPoint MQTT connect/renew failed (attempt=%s): %s; retrying in %.1fs",
                    attempt,
                    err,
                    delay,
                )
                await self._schedule_reconnect(delay)
                continue

            attempt = 0
            await self._wait_for_renewal(renew_in)
        # Loop exits only when self._stopping is set (async_disconnect handles teardown).

    async def _sleep(self, delay: float) -> None:
        """Thin wrapper around asyncio.sleep -- a single patchable seam shared
        by the backoff and renewal waits, so tests can avoid real wall-clock
        waiting without reaching into the global asyncio module."""
        await asyncio.sleep(delay)

    async def _schedule_reconnect(self, delay: float) -> None:
        """Sleep for the computed backoff delay before the next attempt."""
        await self._sleep(delay)

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

    # --- credential renewal (CRED-02/D-06, D-07) ---

    async def _renew(self) -> float:
        """Fetch fresh credentials and (re)connect: disconnect-old/reconnect-new.

        Reused both for the very first connect and every subsequent renewal
        cycle (D-06/spike-confirmed: renewal is a clean disconnect-old/
        reconnect-new cycle, never an in-place credential swap). Returns the
        jittered delay in seconds until the next renewal is due.
        """
        creds = await self._client.get_subscribe_status(self._hub_device_name, self._hub_product_key)
        now = self._time_source()
        self._disconnect_paho()
        self._connect(creds)
        self._subscribe(creds)
        expire_at = now + self._credential_lifetime_seconds(creds)
        return self._renewal_delay_seconds(expire_at, now)

    @staticmethod
    def _credential_lifetime_seconds(creds: dict) -> float:
        """Credential lifetime in seconds from the moment of fetch.

        The exact subscribeStatus response field for this was not captured by
        the Phase 8 spike; if present, `expire` is treated as a lifetime-in-
        seconds duration. Absence defaults to the spike-observed ~570s cycle.
        """
        lifetime = creds.get("expire")
        if isinstance(lifetime, int | float) and lifetime > 0:
            return float(lifetime)
        return _DEFAULT_CREDENTIAL_LIFETIME_SECONDS

    @staticmethod
    def _renewal_base_delay(expire_at: float, now: float) -> float:
        """max(120, expire - now - 60), before jitter (D-06)."""
        return max(_RENEWAL_MIN_INTERVAL_SECONDS, expire_at - now - _RENEWAL_SAFETY_MARGIN_SECONDS)

    def _renewal_delay_seconds(self, expire_at: float, now: float) -> float:
        """Jittered renewal cadence: max(120, expire - now - 60) +-10-30% (D-06/Pitfall 7)."""
        return self._apply_jitter(self._renewal_base_delay(expire_at, now))

    async def _wait_for_renewal(self, delay: float) -> None:
        """Wait for the renewal deadline, an on_http_relogin signal, or a stop
        request -- whichever comes first. A single wait primitive keeps
        renewal and reconnect inside the same supervised loop rather than two
        independent call_later chains that could diverge (Pitfall 6/7).
        """
        self._renew_event.clear()
        sleep_task = asyncio.ensure_future(self._sleep(delay))
        stop_task = asyncio.ensure_future(self._stop_event.wait())
        renew_task = asyncio.ensure_future(self._renew_event.wait())
        try:
            await asyncio.wait({sleep_task, stop_task, renew_task}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for task in (sleep_task, stop_task, renew_task):
                if not task.done():
                    task.cancel()

    def on_http_relogin(self) -> None:
        """Signal that the HTTP layer rotated its token: force an immediate
        credential re-fetch + reconnect rather than waiting for the timed
        renewal deadline, so the supervisor never keeps running on
        credentials the HTTP layer has superseded (D-07/Pitfall 6).
        """
        self._renew_event.set()

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
