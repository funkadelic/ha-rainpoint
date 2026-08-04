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

- ``RainPointHubConnectivityIssues`` re-keys the same raise-once /
  clear-on-recovery shape again, this time onto the hub itself rather than a
  sub-device: one issue per hub the RainPoint cloud reports as disconnected
  for a sustained run of polls. Like its sibling it holds no knowledge of the
  coordinator's data shape, is driven by plain ``HubConnectivityRecord``
  instances the coordinator builds, and is reconciled from the coordinator's
  poll loop only.

- ``RainPointOrphanedEntityIssues`` raises the same one-per-subject card for a
  sensor key that has left the hub's sub-device enumeration and stayed gone,
  whose entities are therefore stranded and permanently unavailable. It is the
  integration's only fixable issue: ``RainPointOrphanedEntitiesRepairFlow``,
  reached through the module-level ``async_create_fix_flow`` hook, is where the
  removal actually happens, so nothing is ever deleted without a human
  confirming it. Like its siblings it holds no knowledge of the coordinator's
  data shape and is driven by plain ``OrphanedEntitiesRecord`` instances.
"""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

import voluptuous as vol
from homeassistant.components.repairs import RepairsFlow
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.event import async_track_time_interval

from .const import (
    DOMAIN,
    HUB_CONNECTIVITY_ISSUE_ID_PREFIX,
    ORPHANED_ENTITIES_ISSUE_ID_PREFIX,
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
    # False when the cloud parks this device under a placeholder parent record
    # rather than a real hub, which is what a Bluetooth-only pairing looks
    # like. Distinguishes "there is no hub" from "the hub's name is missing",
    # which the card would otherwise render identically as "unknown".
    hub_paired: bool = True


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

# Why a raised issue is being cleared, which decides what the recovery log
# line says. Both managers reach _clear_issue from two structurally different
# places: a record that reports the thing healthy again, and a stale-set sweep
# for an id no record mentions at all. The second is a removal from the
# account, not a recovery, and one message cannot honestly describe both.
_CLEAR_REASON_RECOVERED = "recovered"
_CLEAR_REASON_REMOVED = "removed"
# A third reason, used by the orphaned-entities manager alone: the config entry
# that raised the card is being unloaded, so the card is withdrawn rather than
# resolved. Nothing about the device changed.
_CLEAR_REASON_UNLOADED = "unloaded"


def _sanitize_placeholder(value: Any, limit: int = 64) -> str:
    """Neutralize a cloud-supplied string before it reaches a Repairs translation placeholder.

    model, hub_name and addr all originate from the RainPoint cloud
    unvalidated and are rendered by Home Assistant into a Repairs card as
    Markdown, so an unsanitised value could plant a clickable link, an image,
    or raw HTML there. The goal is that no output of this function can be
    rendered as a link at all, not merely that bracketed link syntax is
    broken.

    Collapses every run of whitespace (including an embedded newline) to a
    single space, deletes every Markdown-and-HTML-active character (which also
    takes out the scheme separator and the address at sign), breaks the
    bare-host prefix a renderer autolinks without any surrounding syntax,
    strips, and caps length; falls back to the literal "unknown" when nothing
    is left. None short-circuits to that same fallback rather than being
    stringified: str(None) is "None", which survives every pass above and would
    print a Python repr into a card a user reads.

    Order matters and is the whole correctness of this function. The deletion
    pass has to run BEFORE the autolink break, because deleting characters can
    assemble a prefix that was not there when the text arrived: "www_.evil" and
    "ww[w.evil" both become "www.evil" once the deletion removes the separator,
    so a break that ran first would have already passed over them. Both passes
    run after the whitespace collapse, so the space the break inserts is not
    collapsed away again.
    """
    if value is None:
        return "unknown"
    text = _WHITESPACE_RUN_RE.sub(" ", str(value))
    text = text.translate(_MARKDOWN_HTML_TRANSLATION)
    text = _AUTOLINK_PREFIX_RE.sub(" ", text)
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
        """Start with an empty active set, which is per-instance by design.

        A reload builds a fresh instance with no memory of what a prior
        session raised, which is why _clear_issue deletes unconditionally
        rather than only when it believes the issue is active.
        """
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
            # Drawn from _active, so this device was mentioned by an earlier
            # poll and is not mentioned now: it left the sub-device list
            # rather than resumed reporting. Logging it as recovery would
            # assert the opposite of what happened.
            self._clear_issue(stale_id, reason=_CLEAR_REASON_REMOVED)

    def async_clear(self, hid: Any, mid: int, addr: int) -> None:
        """Clear one device's issue explicitly; the push-arrival half of the lifecycle.

        A pushed reading already overwrites the silent sensor entry for free,
        but the active-issue set is separate state the merge does not touch,
        so the coordinator's push path calls this directly rather than
        waiting for the next poll's reconcile.
        """
        self._clear_issue(silent_device_issue_id(hid, mid, addr))

    def _raise_issue(self, issue_id: str, record: SilentDeviceRecord) -> None:
        """Raise one device's issue, at most once per active period.

        Every cloud-supplied placeholder is sanitized on the way in: Home
        Assistant renders this card as Markdown, so an unfiltered model or hub
        name could plant a link in it.

        A device with no hub renders the literal "none" rather than going
        through the sanitizer, whose "unknown" fallback would be a worse
        answer: the card goes on to suggest pairing the device to a hub, so
        naming its hub "unknown" reads as lost state instead of the truth,
        which is that there is no hub to name. The literal is ours, not the
        cloud's, so it needs no sanitizing.
        """
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
                    "hub_name": _sanitize_placeholder(record.hub_name) if record.hub_paired else "none",
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

    def _clear_issue(self, issue_id: str, *, reason: str = _CLEAR_REASON_RECOVERED) -> None:
        """Delete one device's issue, unconditionally rather than only when active.

        A fresh instance after a reload has no record of an issue a prior
        session raised, so guarding on the active set would strand it forever.
        Deleting an unknown id is already a no-op.

        The log line is gated on the id having been active, which is the one
        thing the delete itself is deliberately not gated on. Every poll
        reaches this method for every reporting device, and async_clear
        reaches it for every pushed message, so an ungated line would print
        per device per message forever; gated, it fires once per raised-then-
        resolved transition and pairs with the WARNING from _raise_issue.

        The reason decides what that line claims. A record-driven clear is a
        recovery; the stale-set sweep in async_sync is a device that left the
        sub-device list, which is not the same event and must not be reported
        as one. Note the gate cannot fire at all for an issue raised before a
        restart, since _active is per-instance: the delete is still correct
        there, but the log carries only the outages that began in this
        session. RainPointHubConnectivityIssues._clear_issue is the same shape
        for the same reasons; keep the two in step.
        """
        was_active = issue_id in self._active
        self._active.discard(issue_id)
        try:
            ir.async_delete_issue(self._hass, DOMAIN, issue_id)
            if was_active and reason == _CLEAR_REASON_RECOVERED:
                _LOGGER.info(
                    "RainPoint device is reporting again; clearing repair issue (id=%s)",
                    issue_id,
                )
            elif was_active:
                _LOGGER.info(
                    "RainPoint device is no longer listed on its hub; clearing repair issue (id=%s)",
                    issue_id,
                )
        except Exception as issue_exc:
            _LOGGER.debug(
                "Failed to delete the not-reporting repair issue (id=%s): %s",
                issue_id,
                issue_exc,
            )


@dataclass(frozen=True)
class OrphanedEntitiesRecord:
    """One sensor key's leftover-entity state, as plain data for the Repairs surface.

    Sibling of SilentDeviceRecord and deliberately not the coordinator's own
    state: the caller translates whatever it knows into this shape before
    calling ``RainPointOrphanedEntityIssues.async_sync``, so this module never
    has to learn the coordinator's data layout, the entity registry's, or the
    late adders'.

    ``entry_id`` rides along because the fix flow needs it to find the config
    entry whose rows it may touch, and the issue's ``data`` is the only place
    a flow can read it from.
    """

    entry_id: str
    sensor_key: str
    addr: Any
    model: str | None
    sub_name: str | None
    hub_name: str | None
    entity_count: int
    missed_polls: int
    orphaned: bool


def orphaned_entities_issue_id(sensor_key: str, entry_id: str) -> str:
    """Return the per-entry, per-key issue id; this string is itself the dedup key.

    Scoped to the config entry as well as the key, which its two non-fixable
    siblings do not need to be. A sensor key is {hid}_{mid}_{addr}, so two
    RainPoint config entries resolving the same home -- two accounts sharing an
    invited home, the same premise the device-row release is built on -- produce
    the same key for the same sub-device. Without the entry id both would then
    raise, dedup and withdraw one another's card: unloading either entry would
    delete a card the other raised and never consented to.
    """
    return f"{ORPHANED_ENTITIES_ISSUE_ID_PREFIX}_{entry_id}_{sensor_key}"


class RainPointOrphanedEntityIssues:
    """Raises and clears one fixable Repairs issue per orphaned sensor key.

    Structurally the same raise-once / clear-on-return reconcile as
    RainPointSilentDeviceIssues, with two differences that matter.

    The issue is fixable, which no other issue in this integration is: the
    card carries a confirmation flow, and confirming it is the only way an
    entity registry row is ever removed here.

    There is no unreachable-ids third bucket. A key whose hub could not be
    reached never reaches the aged-out set in the first place, because the
    freeze is applied where the counting happens, so there is nothing for this
    class to leave alone.
    """

    def __init__(self, hass: HomeAssistant) -> None:
        """Start with an empty active set, which is per-instance by design.

        A reload builds a fresh instance with no memory of what a prior
        session raised, which is why _clear_issue deletes unconditionally
        rather than only when it believes the issue is active.
        """
        self._hass = hass
        self._active: set[str] = set()

    def async_sync(self, records: list[OrphanedEntitiesRecord]) -> None:
        """Reconcile the active issue set against one update's worth of records.

        An orphaned record raises its issue once (deduped on _active). A
        record that is no longer orphaned -- a key that came back before the
        user acted -- clears its issue unconditionally rather than only when
        active, for the same reason its sibling does: a fresh instance after a
        reload has no memory of an issue a prior session raised, and
        async_delete_issue is already a no-op for an unknown id. Any still
        active id no record mentions at all is cleared as a removal rather
        than a recovery, which is what happens once a confirmed fix has
        emptied the ledger entry that produced the record.
        """
        mentioned: set[str] = set()
        for record in records:
            issue_id = orphaned_entities_issue_id(record.sensor_key, record.entry_id)
            mentioned.add(issue_id)
            if record.orphaned:
                self._raise_issue(issue_id, record)
            else:
                self._clear_issue(issue_id)
        for stale_id in self._active - mentioned:
            self._clear_issue(stale_id, reason=_CLEAR_REASON_REMOVED)

    @callback
    def async_clear_all(self) -> None:
        """Withdraw every card this instance raised, on config entry unload.

        Every id in the active set carries this entry's own id, so this
        withdraws only what this entry raised even when a second RainPoint
        entry resolves the same home and therefore the same sensor keys.

        This manager is the one that needs it, and the reason is that its
        record source is session-scoped bookkeeping rather than the current
        poll. Its two siblings rebuild their records from every poll's
        coordinator data, so a stale id is always mentioned again after a
        reload and the stale-set sweep clears it. A departed sensor key cannot
        be mentioned again: it is absent from the hub's enumeration, so no
        fresh adder ledger ever records it and no record is ever built for it.

        Without this, a card raised before a reload survives it -- the issue
        registry is not per entry, and only is_persistent decides survival
        across a restart -- while the fresh manager's active set, the fresh
        adder ledgers and the fresh absence counters all know nothing about it.
        Nothing could then clear it, and its Submit button resolved to an
        executor that removes nothing while Home Assistant deleted the card
        anyway, telling the user leftover entities had been removed when they
        had not.

        Withdrawn rather than resolved: nothing about the device changed. The
        next session does not raise it again either, for exactly the reason
        nothing could have cleared it. A departed key is absent from every
        fresh ledger, so no fresh record mentions it, so no raise is ever
        reached for it. The leftover rows therefore survive the reload with no
        surface left to offer them, and removing them then means removing them
        from the entity registry by hand.

        That is the deliberate trade, and it is the better half of a choice
        with no clean side: the alternative is a card that outlives its own
        scope, whose Submit resolves to an executor with nothing left to act on
        and which Home Assistant deletes anyway, telling the user leftover
        entities were removed when they were not.
        """
        for issue_id in sorted(self._active):
            self._clear_issue(issue_id, reason=_CLEAR_REASON_UNLOADED)

    def _raise_issue(self, issue_id: str, record: OrphanedEntitiesRecord) -> None:
        """Raise one key's fixable issue, at most once per active period.

        Every cloud-supplied placeholder is sanitized on the way in. Home
        Assistant renders both this card and its confirm dialog as Markdown,
        and sub_name, model, addr and hub_name all arrive from the RainPoint
        payload unvalidated, so an unfiltered value could plant a link, an
        image or raw HTML in a card whose whole purpose is to ask the user to
        approve a destructive action. The two integers are this integration's
        own and are stringified rather than sanitized.

        is_persistent is deliberately not passed. The default False means the
        issue registry does not restore this card across a restart, so no
        stale card can outlive the session that raised it, and the sweep
        simply raises it again once the key ages out in the new session.

        The dedup is reconciled against the issue registry rather than trusted
        on its own, which is where this diverges from its two non-fixable
        siblings. Home Assistant's own repairs flow manager deletes a fixable
        issue whenever its flow finishes anything other than an abort, so a
        confirm that reached the executor and removed nothing -- an unreadable
        entry store, an unreadable entity registry -- leaves the card gone from
        the UI while this set still holds its id. Deduping on the set alone
        would then suppress every later attempt and strand the user with
        leftover entities and no surface left to act on.
        """
        if issue_id in self._active:
            if self._issue_still_registered(issue_id):
                return
            # Home Assistant deleted it out from under this set, so the mark is
            # stale. Dropped here rather than inside the test above, so the
            # predicate stays a predicate and reordering this condition cannot
            # silently change the bookkeeping.
            self._active.discard(issue_id)
        try:
            ir.async_create_issue(
                self._hass,
                DOMAIN,
                issue_id,
                is_fixable=True,
                severity=ir.IssueSeverity.WARNING,
                translation_key=ORPHANED_ENTITIES_ISSUE_ID_PREFIX,
                data={"entry_id": record.entry_id, "sensor_key": record.sensor_key},
                # The threat, stated where the values are: Home Assistant
                # renders this card and its confirm dialog as Markdown, and
                # sub_name, model, addr and hub_name all arrive from the
                # RainPoint payload with nothing validating them. Every one of
                # them goes through the sanitizer; the two counts are this
                # integration's own and are only stringified.
                translation_placeholders={
                    "sub_name": _sanitize_placeholder(record.sub_name),
                    "model": _sanitize_placeholder(record.model),
                    "address": _sanitize_placeholder(record.addr),
                    "hub_name": _sanitize_placeholder(record.hub_name),
                    "entity_count": str(record.entity_count),
                    "missed_polls": str(record.missed_polls),
                },
            )
            # Marked active only once the registry accepted it, for the same
            # reason its sibling does: marking first would let one transient
            # registry error suppress every later attempt for this key.
            self._active.add(issue_id)
            _LOGGER.warning(
                "RainPoint no longer lists sensor key %s after %s checks; offering its %s leftover entity/entities for removal",
                record.sensor_key,
                record.missed_polls,
                record.entity_count,
            )
        except Exception as issue_exc:
            _LOGGER.debug(
                "Failed to create the orphaned entities repair issue (id=%s): %s",
                issue_id,
                issue_exc,
            )

    def _issue_still_registered(self, issue_id: str) -> bool:
        """Return True when the issue registry still holds this id.

        A predicate and nothing else: it reads, it does not touch the active
        set. The caller owns the mark, so the bookkeeping cannot be changed by
        reordering or short-circuiting the condition it sits in.

        An unreadable registry answers True, which keeps the dedup in force.
        That is the safe direction for a card whose only outcome is an offer to
        delete: a suppressed re-raise costs the user one poll interval, while
        raising on a failed read could stack a second card over a live one.
        """
        try:
            return ir.async_get(self._hass).async_get_issue(DOMAIN, issue_id) is not None
        except Exception as exc:
            _LOGGER.debug("Could not reconcile the orphaned entities issue (id=%s) against the registry: %s", issue_id, exc)
            return True

    def _clear_issue(self, issue_id: str, *, reason: str = _CLEAR_REASON_RECOVERED) -> None:
        """Delete one key's issue, unconditionally rather than only when active.

        Same shape and same reasoning as its two siblings: a fresh instance
        after a reload has no record of an issue a prior session raised, so
        guarding the delete on the active set would strand it forever, while
        the log line is gated so it fires once per raised-then-resolved
        transition rather than on every update.

        Three reasons rather than the siblings' two, because this manager also
        withdraws its cards on unload, and a withdrawal is neither a recovery
        nor a removal from the account.
        """
        was_active = issue_id in self._active
        self._active.discard(issue_id)
        try:
            ir.async_delete_issue(self._hass, DOMAIN, issue_id)
            if not was_active:
                return
            if reason == _CLEAR_REASON_RECOVERED:
                _LOGGER.info(
                    "RainPoint lists this device again; clearing the orphaned entities repair issue (id=%s)",
                    issue_id,
                )
            elif reason == _CLEAR_REASON_UNLOADED:
                # Warning rather than info, unlike the other two reasons, and
                # the only one of the three that leaves the user worse off. A
                # recovery and a removal both end with nothing left to offer;
                # a withdrawal ends with the leftover rows still registered and
                # no surface left to offer them through, because the same fact
                # that makes the card unclearable after a reload makes it
                # unraisable. The rows have to be removed from the entity
                # registry by hand from here, so the line names them.
                #
                # It cannot become noise: was_active gates it, so it fires only
                # where a card was genuinely up at unload, and at most once per
                # withdrawn card.
                _LOGGER.warning(
                    "Unloading this config entry; withdrawing the orphaned entities repair issue (id=%s). "
                    "Its leftover entity rows are still registered and will not be offered again after the reload; "
                    "remove them from the entity registry by hand",
                    issue_id,
                )
            else:
                _LOGGER.info(
                    "No leftover entities remain to offer; clearing the orphaned entities repair issue (id=%s)",
                    issue_id,
                )
        except Exception as issue_exc:
            _LOGGER.debug(
                "Failed to delete the orphaned entities repair issue (id=%s): %s",
                issue_id,
                issue_exc,
            )


class RainPointOrphanedEntitiesRepairFlow(RepairsFlow):
    """The confirmation dialog behind the orphaned entities card.

    This is the only place in the integration where an entity registry row is
    removed on account of a device leaving the RainPoint device list, and it
    is reached only after a human submits this form. There is no automatic
    path to it and no expiry that deletes on its own.

    Both the config entry and the sensor key come from the issue's own ``data``
    dict and are never parsed back out of the issue id. The id is opaque by
    contract, so parsing it would make a future change to its prefix silently
    resolve to the wrong key and remove another device's entities.
    """

    def __init__(self, data: dict | None) -> None:
        """Hold the issue's data dict, which names what may be removed."""
        self._flow_data = dict(data or {})

    @property
    def _sensor_key(self) -> str:
        """The one sensor key this flow is allowed to act on."""
        return str(self._flow_data.get("sensor_key", ""))

    @property
    def _entry_id(self) -> str:
        """The one config entry this flow is allowed to act on."""
        return str(self._flow_data.get("entry_id", ""))

    async def async_step_init(self, user_input: dict | None = None):
        """Handle the first step of the fix flow."""
        return await self.async_step_confirm()

    async def async_step_confirm(self, user_input: dict | None = None):
        """Show the confirmation form, then remove on submit.

        Showing the form removes nothing; only a submitted form does.
        """
        if user_input is not None:
            self._remove_rows()
            return self.async_create_entry(data={})
        return self.async_show_form(
            step_id="confirm",
            data_schema=vol.Schema({}),
            description_placeholders=self._description_placeholders(),
        )

    def _description_placeholders(self) -> dict | None:
        """Reuse the raised issue's own placeholders for the confirm dialog.

        Reading them back rather than building a second dict is what keeps the
        card and the dialog from drifting, and means there is exactly one
        sanitized supplier for both. An unreadable registry degrades to no
        placeholders rather than raising out of a flow step and leaving the
        user with a broken dialog.
        """
        try:
            issue_id = orphaned_entities_issue_id(self._sensor_key, self._entry_id)
            issue = ir.async_get(self.hass).async_get_issue(DOMAIN, issue_id)
            if issue is None:
                return None
            return issue.translation_placeholders
        except Exception as exc:
            _LOGGER.debug("Could not read the orphaned entities issue placeholders: %s", exc)
            return None

    def _remove_rows(self) -> None:
        """Call this config entry's removal executor for this key.

        The executor is published by the config entry's own setup, so a flow
        submitted after that entry was torn down finds nothing and removes
        nothing. Guarded rather than allowed to raise, because an exception
        here surfaces as a broken repair dialog.
        """
        entry_id = self._flow_data.get("entry_id")
        try:
            remover = self.hass.data[DOMAIN][entry_id]["orphan_entity_remover"]
        except Exception as exc:
            _LOGGER.debug("No orphaned entity remover is registered for entry %s: %s", entry_id, exc)
            return
        try:
            remover(self._sensor_key)
        except Exception as exc:
            _LOGGER.debug("Removing the entities for sensor key %s failed: %s", self._sensor_key, exc)


async def async_create_fix_flow(hass: HomeAssistant, issue_id: str, data: dict | None) -> RepairsFlow:
    """Home Assistant's repairs platform hook, and the integration's first.

    Every other issue this module raises is is_fixable=False and is therefore
    unreachable from here: Home Assistant only builds a flow for an issue the
    registry records as fixable.

    The flow is constructed from ``data`` rather than from ``issue_id`` so the
    id stays opaque; see RainPointOrphanedEntitiesRepairFlow.
    """
    return RainPointOrphanedEntitiesRepairFlow(data)


@dataclass(frozen=True)
class HubConnectivityRecord:
    """One hub's current cloud-connectivity state, as plain data for the Repairs surface.

    This is deliberately not the coordinator's own hub_connectivity dict
    entry: the coordinator translates its data into this shape before calling
    ``RainPointHubConnectivityIssues.async_sync``, so this module never has to
    know that dict's layout, matching the ``SilentDeviceRecord`` rationale.
    """

    hid: Any
    mid: int
    hub_name: str | None
    disconnected: bool
    missed_polls: int
    model: str | None = None


def hub_connectivity_issue_id(hid: Any, mid: int) -> str:
    """Return the per-hub issue id; this string is itself the dedup key."""
    return f"{HUB_CONNECTIVITY_ISSUE_ID_PREFIX}_{hid}_{mid}"


class RainPointHubConnectivityIssues:
    """Raises and clears one Repairs issue per hub the RainPoint cloud reports as gone.

    Re-keys the raise-once / clear-on-recovery shape of
    RainPointSilentDeviceIssues onto the hub rather than the sub-device: a
    home can have several hubs, and each needs to name itself on its own
    card. The poll reconcile drives async_sync; the push path drives
    async_clear directly, exactly as RainPointSilentDeviceIssues does for the
    sub-device pair. Either way the class holds no knowledge of the
    coordinator's data shape, exactly like its sibling.
    """

    def __init__(self, hass: HomeAssistant) -> None:
        """Start with an empty active set, which is per-instance by design.

        A reload builds a fresh instance with no memory of what a prior
        session raised, which is why _clear_issue deletes unconditionally
        rather than only when it believes the issue is active.
        """
        self._hass = hass
        self._active: set[str] = set()

    def async_sync(self, records: list[HubConnectivityRecord], *, unreachable_ids: Iterable[str] = frozenset()) -> None:
        """Reconcile the active issue set against one poll's worth of records.

        A disconnected record raises its issue once (deduped on _active). A
        connected record clears its issue unconditionally rather than only
        when active, for the same reload-survives-a-restart reason
        RainPointSilentDeviceIssues.async_sync gives. Any id still active that
        no record mentions at all is normally cleared, so a hub that leaves
        the device list entirely does not leave an orphaned issue behind.

        An id listed in unreachable_ids is the third case: it is left exactly
        as it is, neither raised nor cleared, because the coordinator could
        not determine that hub's connectivity this poll, and an unknown
        tri-state is not evidence about the hub. Those ids are opaque
        strings, so this module still needs no knowledge of the coordinator's
        data shape. The parameter is keyword-only and defaults to empty, so
        the class stays usable, and callable in tests, without it.
        """
        mentioned: set[str] = set()
        for record in records:
            issue_id = hub_connectivity_issue_id(record.hid, record.mid)
            mentioned.add(issue_id)
            if record.disconnected:
                self._raise_issue(issue_id, record)
            else:
                self._clear_issue(issue_id)
        for stale_id in self._active - mentioned - set(unreachable_ids):
            # Drawn from _active, so this hub was mentioned by an earlier poll
            # and is not mentioned now: it left the account rather than
            # reconnected. Logging it as recovery would assert the opposite of
            # what happened.
            self._clear_issue(stale_id, reason=_CLEAR_REASON_REMOVED)

    def async_clear(self, hid: Any, mid: int) -> None:
        """Clear one hub's issue explicitly; the push-arrival half of the lifecycle.

        A pushed connected edge already overwrites the held connectivity
        record for free, but the active-issue set is separate state the
        merge does not touch, so the coordinator's push path calls this
        directly rather than waiting for the next poll's reconcile.
        """
        self._clear_issue(hub_connectivity_issue_id(hid, mid))

    def _raise_issue(self, issue_id: str, record: HubConnectivityRecord) -> None:
        """Raise one hub's issue, at most once per active period.

        The hub name is sanitized on the way in: Home Assistant renders this
        card as Markdown, so an unfiltered hub name could plant a link in it.
        """
        if issue_id in self._active:
            return
        try:
            ir.async_create_issue(
                self._hass,
                DOMAIN,
                issue_id,
                is_fixable=False,
                severity=ir.IssueSeverity.WARNING,
                translation_key=HUB_CONNECTIVITY_ISSUE_ID_PREFIX,
                translation_placeholders={
                    "hub_name": _sanitize_placeholder(record.hub_name),
                    "model": _sanitize_placeholder(record.model),
                    "missed_polls": str(record.missed_polls),
                },
            )
            # Marked active only once the registry accepted it. Marking before
            # the call would strand a hub whose first raise failed: the dedup
            # guard above would suppress every later attempt, so a transient
            # registry error would silence that hub for the rest of the
            # session.
            self._active.add(issue_id)
            _LOGGER.warning(
                "RainPoint hub hid=%s mid=%s has been unreachable from the cloud for at least %s polls; raising repair issue",
                record.hid,
                record.mid,
                record.missed_polls,
            )
        except Exception as issue_exc:
            _LOGGER.debug(
                "Failed to create the hub connectivity repair issue (id=%s): %s",
                issue_id,
                issue_exc,
            )

    def _clear_issue(self, issue_id: str, *, reason: str = _CLEAR_REASON_RECOVERED) -> None:
        """Delete one hub's issue, unconditionally rather than only when active.

        A fresh instance after a reload has no record of an issue a prior
        session raised, so guarding on the active set would strand it forever.
        Deleting an unknown id is already a no-op.

        The log line is gated on the id having been active, which is the one
        thing the delete itself is deliberately not gated on. Every connected
        poll reaches this method for every healthy hub, so an ungated line
        would print once per hub every scan interval forever; gated, it fires
        once per raised-then-resolved transition and pairs with the WARNING
        from _raise_issue, so the log carries both ends of an outage instead
        of only its start.

        The reason decides what that line claims. A record-driven clear is a
        reconnection; the stale-set sweep in async_sync is a hub that left the
        account, which is not the same event and must not be reported as one.
        Note the gate cannot fire at all for an issue raised before a restart,
        since _active is per-instance: the delete is still correct there, but
        the log carries only the outages that began in this session.
        RainPointSilentDeviceIssues._clear_issue is the same shape for the
        same reasons; keep the two in step.
        """
        was_active = issue_id in self._active
        self._active.discard(issue_id)
        try:
            ir.async_delete_issue(self._hass, DOMAIN, issue_id)
            if was_active and reason == _CLEAR_REASON_RECOVERED:
                _LOGGER.info(
                    "RainPoint hub connectivity restored; clearing repair issue (id=%s)",
                    issue_id,
                )
            elif was_active:
                _LOGGER.info(
                    "RainPoint hub is no longer listed on the account; clearing repair issue (id=%s)",
                    issue_id,
                )
        except Exception as issue_exc:
            _LOGGER.debug(
                "Failed to delete the hub connectivity repair issue (id=%s): %s",
                issue_id,
                issue_exc,
            )
