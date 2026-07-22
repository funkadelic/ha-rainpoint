"""Push-channel liveness watchdog.

Surfaces a disconnected push channel as a dismissible Settings > Repairs issue
and clears it on recovery, so a sustained outage cannot hide behind the polling
fallback. It catches disconnection-based death; a channel that stays connected
but silently stops delivering data is not flagged (see _channel_functional).

Detection-only by design: it never reconnects (the supervisor owns reconnect)
and never changes the poll cadence. It reads the same connection state and
last-message liveness clock the MQTT client already tracks -- it does not add a
second timer or a second liveness clock.

The channel is considered alive while it is connected or has delivered any
message within the message window (an undecodable message still proves the pipe
is alive). A repair issue is raised only after the channel has stayed
non-functional continuously past the dead-after threshold, and only once per
dead->alive transition, mirroring the once-per-cause restraint the coordinator
uses for unknown-model notifications. Transient blips below the threshold stay
log-only and never raise an issue or spam notifications.
"""

from __future__ import annotations

import logging
import time
from datetime import timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.event import async_track_time_interval

from .const import (
    DOMAIN,
    PUSH_WATCHDOG_DEAD_AFTER_SECONDS,
    PUSH_WATCHDOG_ISSUE_ID,
    PUSH_WATCHDOG_MESSAGE_GRACE_SECONDS,
    PUSH_WATCHDOG_SCAN_INTERVAL_SECONDS,
)

_LOGGER = logging.getLogger(__name__)


class RainPointPushWatchdog:
    """Periodically evaluates push-channel liveness and raises/clears a repair issue.

    The watchdog reads liveness from the MQTT client only; it performs no
    reconnect and never touches the coordinator's update interval.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        mqtt_client,
        *,
        time_source=time.monotonic,
    ) -> None:
        self._hass = hass
        self._entry = entry
        self._mqtt_client = mqtt_client
        self._time_source = time_source
        # Monotonic time the channel was first observed non-functional in the
        # current outage, or None while it is functional. This is the sustained-
        # failure clock; it is distinct from the client's last-message clock.
        self._dead_since: float | None = None
        # Dedup flag so the issue fires once per dead->alive transition.
        self._issue_active = False
        self._cancel_timer = None

    @callback
    def start(self) -> None:
        """Begin periodic liveness checks on the HA event loop."""
        # A fresh instance (e.g. after a reload) has no memory of an issue a
        # prior instance raised. Clear any stale one so this session's checks are
        # authoritative -- otherwise a channel that recovered across the reload
        # would leave a "down" issue that is never deleted. If it is genuinely
        # still down, the periodic check re-raises it after the threshold.
        ir.async_delete_issue(self._hass, DOMAIN, PUSH_WATCHDOG_ISSUE_ID)
        self._cancel_timer = async_track_time_interval(
            self._hass,
            self._async_check,
            timedelta(seconds=PUSH_WATCHDOG_SCAN_INTERVAL_SECONDS),
        )

    @callback
    def async_stop(self) -> None:
        """Stop periodic checks. Idempotent and safe to register with async_on_unload."""
        cancel, self._cancel_timer = self._cancel_timer, None
        if cancel is not None:
            cancel()

    def _channel_functional(self) -> bool:
        """Return whether the channel currently looks alive.

        Alive when connected, or when a message arrived within the short grace
        window. The grace window only bridges the brief connected=False gap while
        the supervisor reconnects at a renewal boundary; it is intentionally much
        shorter than the dead-after window so the two do not stack (a message
        just before a disconnect must not delay flagging by a second dead-after
        window).

        Known limitation: a channel that stays connected but silently stops
        delivering data is reported alive here, because message-absence while
        connected is indistinguishable from a healthy idle channel. Detecting
        that requires an active probe and is out of scope.
        """
        if self._mqtt_client.connected:
            return True
        last = self._mqtt_client.last_message_at
        return last is not None and (self._time_source() - last) <= PUSH_WATCHDOG_MESSAGE_GRACE_SECONDS

    @callback
    def _async_check(self, now=None) -> None:
        """Evaluate liveness once. Raises the issue only after sustained failure."""
        if self._channel_functional():
            self._on_alive()
            return

        current = self._time_source()
        if self._dead_since is None:
            # First observation of a possible outage: start the clock, stay quiet.
            self._dead_since = current
            _LOGGER.debug("RainPoint push channel appears down; watching for sustained failure")
            return

        if current - self._dead_since >= PUSH_WATCHDOG_DEAD_AFTER_SECONDS:
            self._raise_issue()

    def _on_alive(self) -> None:
        """Reset the outage clock and clear any active issue on recovery."""
        self._dead_since = None
        if self._issue_active:
            self._issue_active = False
            ir.async_delete_issue(self._hass, DOMAIN, PUSH_WATCHDOG_ISSUE_ID)
            _LOGGER.info("RainPoint push channel recovered; clearing repair issue")

    def _raise_issue(self) -> None:
        """Raise the repair issue once per outage (deduped on the active flag)."""
        if self._issue_active:
            return
        self._issue_active = True
        ir.async_create_issue(
            self._hass,
            DOMAIN,
            PUSH_WATCHDOG_ISSUE_ID,
            is_fixable=False,
            severity=ir.IssueSeverity.WARNING,
            translation_key=PUSH_WATCHDOG_ISSUE_ID,
        )
        _LOGGER.warning("RainPoint push channel has been down past the threshold; raising repair issue")
