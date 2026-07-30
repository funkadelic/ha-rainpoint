"""The integration's Settings > Repairs surfaces.

Two independent lifecycles live here:

- ``RainPointPushWatchdog`` surfaces a disconnected push channel as a
  dismissible Repairs issue and clears it on recovery, so a sustained outage
  cannot hide behind the polling fallback. It catches disconnection-based
  death; a channel that stays connected but silently stops delivering data is
  not flagged (see _channel_functional). Detection-only by design: it never
  reconnects (the supervisor owns reconnect) and never changes the poll
  cadence. It reads the same connection state and last-message liveness clock
  the MQTT client already tracks -- it does not add a second timer or a
  second liveness clock.

  The channel is considered alive while it is connected or has delivered any
  message within the message window (an undecodable message still proves the
  pipe is alive). A repair issue is raised only after the channel has stayed
  non-functional continuously past the dead-after threshold, and only once
  per dead->alive transition, mirroring the once-per-cause restraint the
  coordinator uses for unknown-model notifications. Transient blips below the
  threshold stay log-only and never raise an issue or spam notifications.

- ``RainPointSilentDeviceIssues`` re-keys that same raise-once /
  clear-on-recovery shape from a single well-known issue id to one issue per
  sub-device the cloud has stopped reporting on. It holds no knowledge of the
  coordinator's data shape: it is driven by plain ``SilentDeviceRecord``
  instances built by the coordinator, and reconciled from the coordinator's
  own poll and push triggers rather than a timer of its own.
"""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

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
    SILENT_DEVICE_ISSUE_ID_PREFIX,
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
        """Wire the watchdog to a hub's MQTT client; time_source is injectable for tests."""
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


@dataclass(frozen=True)
class SilentDeviceRecord:
    """One sub-device's current silence state, as plain data for the Repairs surface.

    This is deliberately not the coordinator's own sensors dict entry: the
    coordinator translates its data into this shape before calling
    ``RainPointSilentDeviceIssues.async_sync``, so this module never has to
    know that dict's layout.
    """

    hid: Any
    mid: int
    addr: int
    model: str | None
    hub_name: str | None
    missed_polls: int
    silent: bool


def silent_device_issue_id(hid: Any, mid: int, addr: int) -> str:
    """Return the per-device issue id; this string is itself the dedup key."""
    return f"{SILENT_DEVICE_ISSUE_ID_PREFIX}_{hid}_{mid}_{addr}"


# Markdown- and HTML-active characters stripped from a value before it reaches
# a Repairs translation placeholder: backtick, angle brackets, square
# brackets, parentheses, pipe, backslash, asterisk, underscore, and hash, plus
# the colon, forward slash and at sign that make a bare address linkable.
_MARKDOWN_HTML_TRANSLATION = str.maketrans("", "", "`<>[]()|\\*_#:/@")
_WHITESPACE_RUN_RE = re.compile(r"\s+")
# The bare-host form some Markdown renderers autolink on the prefix alone,
# with no scheme and no surrounding syntax to strip.
_AUTOLINK_PREFIX_RE = re.compile(r"www\.", re.IGNORECASE)


def _sanitize_placeholder(value: Any, limit: int = 64) -> str:
    """Neutralize a cloud-supplied string before it reaches a Repairs translation placeholder.

    model, hub_name and addr all originate from the RainPoint cloud
    unvalidated and are rendered by Home Assistant into a Repairs card as
    Markdown, so an unsanitised value could plant a clickable link, an image,
    or raw HTML there. The goal is that no output of this function can be
    rendered as a link at all, not merely that bracketed link syntax is
    broken.

    Collapses every run of whitespace (including an embedded newline) to a
    single space, breaks the bare-host prefix a renderer autolinks without any
    surrounding syntax, deletes every Markdown-and-HTML-active character
    (which also takes out the scheme separator and the address at sign),
    strips, and caps length; falls back to the literal "unknown" when nothing
    is left. The prefix substitution runs after the whitespace collapse so the
    space it inserts is not collapsed away again.
    """
    text = _WHITESPACE_RUN_RE.sub(" ", str(value))
    text = _AUTOLINK_PREFIX_RE.sub(" ", text)
    text = text.translate(_MARKDOWN_HTML_TRANSLATION)
    text = text.strip()[:limit]
    return text or "unknown"


class RainPointSilentDeviceIssues:
    """Raises and clears one Repairs issue per silent sub-device.

    Re-keys the raise-once / clear-on-recovery shape of RainPointPushWatchdog
    from a single well-known issue id to one issue per {hid}_{mid}_{addr}: a
    hub can have several silent children at once, and each needs to name its
    own model and address. Driven by the coordinator's poll reconcile and by
    explicit push-arrival clearing rather than a timer of its own -- the
    coordinator already has both triggers to call it from.
    """

    def __init__(self, hass: HomeAssistant) -> None:
        self._hass = hass
        self._active: set[str] = set()

    def async_sync(self, records: list[SilentDeviceRecord], *, unreachable_ids: Iterable[str] = frozenset()) -> None:
        """Reconcile the active issue set against one poll's worth of records.

        A silent record raises its issue once (deduped on _active). A
        non-silent record clears its issue unconditionally rather than only
        when active: a fresh instance after a restart has no memory of an
        issue a prior session raised, so guarding on _active would strand it
        forever, and async_delete_issue is already a no-op for an unknown id.
        Any id still active that no record mentions at all is normally
        cleared, so a device that leaves the hub's sub-device list entirely
        does not leave an orphaned issue behind.

        An id listed in unreachable_ids is the third case: it is left exactly
        as it is, neither raised nor cleared, because the hub that owns it
        could not be reached this poll and an outage is not evidence about any
        particular device. Those ids are opaque strings, so this module still
        needs no knowledge of the coordinator's data shape. The parameter is
        keyword-only and defaults to empty, so the class stays usable, and
        callable in tests, without it.
        """
        mentioned: set[str] = set()
        for record in records:
            issue_id = silent_device_issue_id(record.hid, record.mid, record.addr)
            mentioned.add(issue_id)
            if record.silent:
                self._raise_issue(issue_id, record)
            else:
                self._clear_issue(issue_id)
        for stale_id in self._active - mentioned - set(unreachable_ids):
            self._clear_issue(stale_id)

    def async_clear(self, hid: Any, mid: int, addr: int) -> None:
        """Clear one device's issue explicitly; the push-arrival half of the lifecycle.

        A pushed reading already overwrites the silent sensor entry for free,
        but the active-issue set is separate state the merge does not touch,
        so the coordinator's push path calls this directly rather than
        waiting for the next poll's reconcile.
        """
        self._clear_issue(silent_device_issue_id(hid, mid, addr))

    def _raise_issue(self, issue_id: str, record: SilentDeviceRecord) -> None:
        if issue_id in self._active:
            return
        try:
            ir.async_create_issue(
                self._hass,
                DOMAIN,
                issue_id,
                is_fixable=False,
                severity=ir.IssueSeverity.WARNING,
                translation_key=SILENT_DEVICE_ISSUE_ID_PREFIX,
                translation_placeholders={
                    "model": _sanitize_placeholder(record.model),
                    "address": _sanitize_placeholder(record.addr),
                    "hub_name": _sanitize_placeholder(record.hub_name),
                    "missed_polls": str(record.missed_polls),
                },
            )
            # Marked active only once the registry accepted it. Marking before
            # the call would strand a device whose first raise failed: the
            # dedup guard above would suppress every later attempt, so a
            # transient registry error would silence that device for the rest
            # of the session.
            self._active.add(issue_id)
            _LOGGER.warning(
                "RainPoint sub-device addr=%s (model=%s) has not reported for %s polls; raising repair issue",
                record.addr,
                record.model,
                record.missed_polls,
            )
        except Exception as issue_exc:
            _LOGGER.debug(
                "Failed to create the not-reporting repair issue (id=%s): %s",
                issue_id,
                issue_exc,
            )

    def _clear_issue(self, issue_id: str) -> None:
        self._active.discard(issue_id)
        try:
            ir.async_delete_issue(self._hass, DOMAIN, issue_id)
        except Exception as issue_exc:
            _LOGGER.debug(
                "Failed to delete the not-reporting repair issue (id=%s): %s",
                issue_id,
                issue_exc,
            )
