"""
RainPoint MQTT push channel client.

Connects to the Alibaba Cloud IoT (Aliyun) MQTT broker using fresh,
per-session observer credentials fetched from RainPointClient.get_subscribe_status().
A single owned asyncio.Task (_run_supervisor) drives the full lifecycle --
connect, subscribe, wait, renew, reconnect -- indefinitely under jittered
backoff. It subscribes to both candidate state topics and
logs message receipt; it never touches coordinator state -- feeding push
data into coordinator.data is out of scope here.

Transport is plain TCP, no TLS (confirmed against the live broker) -- this overrides any
TLS assumption elsewhere. Every paho on_* callback runs on paho's own network
thread and must never touch HA/integration state directly; each hops onto the
HA event loop via hass.loop.call_soon_threadsafe into an @callback method
before touching self.
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


# Fields that must never appear in the clear in logs/diagnostics.
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


# --- TEMPORARY push-envelope structure capture (remove once the live envelope
# is confirmed) -------------------------------------------------------------
# The push payload shape was never captured structurally against live hardware
# (only its byte length), so this path logs a redacted skeleton of an inbound
# payload to confirm whether the assumed prefix / subdevice-id / raw-value
# segmentation is real. Value bytes could embed credential material, so the
# skeleton preserves only structural delimiters and the leading marker of each
# value run, masking the remainder to a length -- no raw payload content is
# ever emitted. Delete this helper and its caller when the format is confirmed.
_STRUCTURE_DELIMITERS = frozenset("#/,;|:=&{}[]\"' \t\n\r")


def _redacted_payload_skeleton(payload: bytes) -> str:
    """Render an inbound push payload as a redacted structural skeleton.

    Structural delimiters are preserved verbatim so the prefix and segment
    layout stay visible; every value run is masked to ``<first-char>[<len>]``
    so no raw payload byte-content survives into the log. For example
    ``#P1737460800/D01/10#0a1b`` renders as ``#P[11]/D[3]/1[2]#0[4]`` --
    enough to confirm the envelope shape without leaking any value.
    """
    text = payload.decode("utf-8", errors="replace")
    parts: list[str] = []
    run: list[str] = []

    def _flush() -> None:
        if run:
            parts.append(f"{run[0]}[{len(run)}]")
            run.clear()

    for char in text:
        if char in _STRUCTURE_DELIMITERS:
            _flush()
            parts.append(char)
        else:
            run.append(char)
    _flush()
    return "".join(parts)


# Supervisor backoff: only the DELAY is capped, never
# the attempt count. 30s base doubling up to a 480s ceiling (within the 300-600s
# band), with +-10-30% jitter to avoid a synchronized reconnect storm.
_BACKOFF_BASE_SECONDS = 30.0
_BACKOFF_CEILING_SECONDS = 480.0
_JITTER_MIN_FRACTION = 0.10
_JITTER_MAX_FRACTION = 0.30

# Credential renewal cadence: max(120, expire - now - 60), jittered.
# The exact subscribeStatus expiry field name was not captured empirically by an
# earlier connection probe, so absence defaults to the observed ~570s
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
    the sole reconnect authority.
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
        wall_clock_source=time.time,
    ) -> None:
        self._hass = hass
        self._client = client
        self._entry = entry
        self._hub_device_name = hub_device_name
        self._hub_product_key = hub_product_key
        self._paho_client_factory = paho_client_factory
        # Monotonic seam: renewal-interval bookkeeping only (immune to clock steps).
        self._time_source = time_source
        # Wall-clock seam: the protocol timestamp embedded in the clientId/HMAC,
        # which Aliyun expects as a real epoch value -- kept separate from the
        # monotonic renewal seam.
        self._wall_clock_source = wall_clock_source

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

    # --- supervisor lifecycle ---

    async def async_start(self) -> None:
        """Launch the supervisor task that owns connect->run->reconnect.

        Never blocks -- callers must schedule this as a background task; the
        supervisor itself retries indefinitely and this method never raises
        out in a way that should block config-entry setup.
        """
        self._stopping = False
        self._stop_event.clear()
        self._supervisor_task = self._hass.loop.create_task(self._run_supervisor())

    async def _run_supervisor(self) -> None:
        """Own connect -> run -> renew -> reconnect indefinitely.

        _renew() is reused both for the very first connect and every
        subsequent renewal cycle, so there is exactly one state machine here
        -- not two independent call_later chains that could silently diverge.
        Only the backoff DELAY is capped -- never the attempt
        count. Any exception in the loop body (connect failure or
        renewal failure alike) still results in a scheduled retry:
        there is no bare return and no uncaught exception that silently ends
        the chain.
        """
        attempt = 0
        while not self._stopping:
            try:
                renew_in = await self._renew()
            except asyncio.CancelledError:
                raise
            except Exception as err:  # must always re-arm on any connect or renewal failure
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
        """Exponential backoff capped at a ceiling, with jitter.

        There is no cap on `attempt` itself -- the supervisor retries forever
        -- so the exponent is clamped defensively before raising
        2**exponent to avoid an OverflowError on very large attempt counts;
        the ceiling is already reached at a small exponent in practice.
        """
        exponent = min(max(attempt - 1, 0), 32)
        base = min(_BACKOFF_BASE_SECONDS * (2**exponent), _BACKOFF_CEILING_SECONDS)
        return self._apply_jitter(base)

    @staticmethod
    def _apply_jitter(value: float) -> float:
        """Apply +-10-30% random jitter to avoid a synchronized reconnect storm."""
        magnitude = random.uniform(_JITTER_MIN_FRACTION, _JITTER_MAX_FRACTION)
        sign = random.choice((-1.0, 1.0))
        return value * (1 + sign * magnitude)

    # --- credential renewal ---

    async def _renew(self) -> float:
        """Fetch fresh credentials and (re)connect: disconnect-old/reconnect-new.

        Reused both for the very first connect and every subsequent renewal
        cycle (renewal is a clean disconnect-old/
        reconnect-new cycle, never an in-place credential swap). Returns the
        jittered delay in seconds until the next renewal is due.
        """
        creds = await self._client.get_subscribe_status(self._hub_device_name, self._hub_product_key)
        now = self._time_source()
        self._disconnect_paho()
        await self._connect(creds)
        self._subscribe(creds)
        expire_at = now + self._credential_lifetime_seconds(creds)
        return self._renewal_delay_seconds(expire_at, now)

    @staticmethod
    def _credential_lifetime_seconds(creds: dict) -> float:
        """Credential lifetime in seconds from the moment of fetch.

        The exact subscribeStatus response field for this was not captured by
        an earlier connection probe; if present, `expire` is treated as a lifetime-in-
        seconds duration. Absence defaults to the observed ~570s cycle.
        """
        lifetime = creds.get("expire")
        if isinstance(lifetime, int | float) and lifetime > 0:
            return float(lifetime)
        return _DEFAULT_CREDENTIAL_LIFETIME_SECONDS

    @staticmethod
    def _renewal_base_delay(expire_at: float, now: float) -> float:
        """max(120, expire - now - 60), before jitter."""
        return max(_RENEWAL_MIN_INTERVAL_SECONDS, expire_at - now - _RENEWAL_SAFETY_MARGIN_SECONDS)

    def _renewal_delay_seconds(self, expire_at: float, now: float) -> float:
        """Jittered renewal cadence: max(120, expire - now - 60) +-10-30%."""
        return self._apply_jitter(self._renewal_base_delay(expire_at, now))

    async def _wait_for_renewal(self, delay: float) -> None:
        """Wait for the renewal deadline, an on_http_relogin signal, or a stop
        request -- whichever comes first. A single wait primitive keeps
        renewal and reconnect inside the same supervised loop rather than two
        independent call_later chains that could diverge.
        """
        # Check-before-clear: if a relogin fired mid-_renew() -- after
        # this cycle captured its credentials but before returning -- the event is
        # already set for a rotation this cycle never observed. Honor it with an
        # immediate re-fetch instead of clearing it and sleeping out the interval.
        if self._renew_event.is_set():
            self._renew_event.clear()
            return

        self._renew_event.clear()
        sleep_task = asyncio.ensure_future(self._sleep(delay))
        stop_task = asyncio.ensure_future(self._stop_event.wait())
        renew_task = asyncio.ensure_future(self._renew_event.wait())
        try:
            await asyncio.wait({sleep_task, stop_task, renew_task}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            # Cancel AND await the losers: .cancel() only schedules
            # cancellation, so awaiting confirms it before the tasks go out of
            # scope -- otherwise asyncio may log "Task was destroyed but it is
            # pending!" every renewal cycle.
            pending = [task for task in (sleep_task, stop_task, renew_task) if not task.done()]
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)

    def on_http_relogin(self) -> None:
        """Signal that the HTTP layer rotated its token: force an immediate
        credential re-fetch + reconnect rather than waiting for the timed
        renewal deadline, so the supervisor never keeps running on
        credentials the HTTP layer has superseded.
        """
        self._renew_event.set()

    async def _connect(self, creds: dict) -> None:
        """Build the paho client from fresh creds and connect. No TLS.

        paho's connect() is blocking (DNS resolution + a synchronous
        socket.connect); calling it directly on the HA event loop would freeze
        all of Home Assistant for the connection timeout on every initial
        connect, every renewal cycle, and every backoff retry against an
        unreachable broker. It is therefore dispatched to the executor,
        while loop_start() (a cheap network-thread spawn) stays on the loop.
        The blocking connect() still creates the socket before returning, so the
        subscribe-after-connect ordering in _renew() continues to find a live
        connection.
        """
        device_name = creds.get("deviceName", "")
        product_key = creds.get("productKey", "")
        device_secret = creds.get("deviceSecret", "")

        # Wall-clock epoch milliseconds for the Aliyun protocol timestamp -- NOT
        # the monotonic renewal seam, whose reference-point value is nowhere near
        # epoch time and could fail broker anti-replay checks.
        timestamp_ms = int(self._wall_clock_source() * 1000)
        username = f"{device_name}&{product_key}"
        client_id = f"{device_name}|securemode=2,signmethod=hmacsha1,timestamp={timestamp_ms}|"
        sign_content = f"clientId{device_name}deviceName{device_name}productKey{product_key}timestamp{timestamp_ms}"
        password = hmac.new(device_secret.encode(), sign_content.encode(), hashlib.sha1).hexdigest()

        _LOGGER.debug(
            "RainPoint MQTT connecting: username=%s client_id=%s",
            username,
            _redact(client_id),
        )

        # The supervisor is the sole reconnect authority -- paho
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

        # The credential response's host is authoritative for the account's
        # region; fall back to the template host when absent. mqttHostUrl is
        # formatted as "host:port"; parse both, defaulting the port when it is
        # missing or non-numeric.
        host_url = creds.get("mqttHostUrl") or MQTT_BROKER_HOST_TEMPLATE.format(product_key=product_key)
        host, _, port_str = host_url.partition(":")
        port = int(port_str) if port_str.isdigit() else MQTT_BROKER_PORT
        # connect() blocks on DNS + socket.connect; keep it off the event loop.
        await self._hass.async_add_executor_job(paho_client.connect, host, port, MQTT_KEEPALIVE)
        paho_client.loop_start()

        self._paho = paho_client
        self._device_name = device_name
        self._product_key = product_key

    def _subscribe(self, creds: dict) -> None:
        """Subscribe to both candidate state topics (which one carries state is resolved empirically later)."""
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
            paho_client.disconnect()
        finally:
            paho_client.loop_stop()

    # --- paho callbacks: run on paho's network thread, never on the HA loop.
    # Only cheap, pure reads are allowed here; everything else is dispatched
    # via hass.loop.call_soon_threadsafe into an @callback method.

    def _on_connect(self, client, userdata, flags, reason_code, properties=None) -> None:
        """Runs on paho's network thread."""
        self._hass.loop.call_soon_threadsafe(self._handle_connect, reason_code)

    def _on_disconnect(self, client, userdata, flags, reason_code, properties=None) -> None:
        """Runs on paho's network thread."""
        self._hass.loop.call_soon_threadsafe(self._handle_disconnect, reason_code)

    def _on_message(self, client, userdata, msg) -> None:
        """Runs on paho's network thread. Reads only topic/payload -- no state mutation.

        Carries the raw payload bytes across the hop (a cheap attribute read,
        same discipline as topic) so the HA-loop handler can log a redacted
        structure capture; the bytes are never inspected on paho's thread.
        """
        topic = msg.topic
        payload = msg.payload
        self._hass.loop.call_soon_threadsafe(self._handle_message, topic, payload)

    # --- @callback methods: run on the HA event loop only.

    @callback
    def _handle_connect(self, reason_code) -> None:
        """Runs on the HA event loop.

        reason_code is 0 on success and a non-zero failure ReasonCode (e.g. "Not
        authorized") on rejection; only a zero code counts as connected.
        """
        self._connected = reason_code == 0
        if reason_code == 0:
            _LOGGER.debug("RainPoint MQTT connected: reason_code=%s", reason_code)
        else:
            _LOGGER.warning("RainPoint MQTT connect rejected: reason_code=%s", reason_code)

    @callback
    def _handle_disconnect(self, reason_code) -> None:
        """Runs on the HA event loop."""
        self._connected = False
        _LOGGER.debug("RainPoint MQTT disconnected: reason_code=%s", reason_code)
        # Reactive reconnect on an unexpected broker-initiated disconnect is
        # intentionally deferred to a later change (resilience/observability). Today the
        # supervisor only re-arms on the renewal deadline, an on_http_relogin
        # signal, or a stop request, so an unexpected disconnect stays dark until
        # the next scheduled renewal rather than reconnecting immediately.

    @callback
    def _handle_message(self, topic: str, payload: bytes) -> None:
        """Runs on the HA event loop. Logs receipt plus a redacted structure capture.

        The receipt line carries topic + byte-length + running count only. The
        structure line is a TEMPORARY diagnostic (removed once the envelope is
        confirmed) that logs the redacted skeleton of the payload -- delimiters
        and segment lengths, never raw value bytes.
        """
        self._message_count += 1
        payload_len = len(payload)
        _LOGGER.debug("RainPoint MQTT message received: topic=%s len=%s count=%s", topic, payload_len, self._message_count)
        # TEMPORARY push-envelope structure capture -- remove once confirmed.
        _LOGGER.debug(
            "RainPoint MQTT push structure capture (temporary): topic=%s len=%s skeleton=%s",
            topic,
            payload_len,
            _redacted_payload_skeleton(payload),
        )

    async def async_disconnect(self) -> None:
        """Cancel and await the supervisor task, then stop the paho loop and
        disconnect -- in that order, so no reconnect is scheduled after
        teardown begins. Idempotent and tolerant of a
        never-started/never-connected client.
        """
        self._stopping = True
        self._stop_event.set()

        task, self._supervisor_task = self._supervisor_task, None
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                # We requested the supervisor task's cancellation, so its
                # CancelledError is expected and absorbed here. But if
                # async_disconnect itself is being cancelled, we must not
                # swallow that -- re-raise so our own cancellation propagates.
                if asyncio.current_task().cancelling() > 0:
                    raise
            except Exception:
                _LOGGER.exception("RainPoint MQTT supervisor task raised during teardown")

        # Guard the final teardown too: a paho loop_stop()/disconnect()
        # raising here (e.g. already-closed socket, internal thread-join error)
        # must never propagate out of the async_on_unload path and block unload.
        try:
            self._disconnect_paho()
        except Exception:
            _LOGGER.exception("RainPoint MQTT paho teardown raised during disconnect")
