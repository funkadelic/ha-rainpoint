"""Tests for the integration's Repairs surfaces (repairs.py): the push-channel
liveness watchdog and the per-device silent sub-device issue lifecycle."""

from __future__ import annotations

import logging
from datetime import timedelta
from unittest.mock import MagicMock, patch

import pytest

from custom_components.rainpoint import repairs
from custom_components.rainpoint.const import (
    DOMAIN,
    PUSH_WATCHDOG_DEAD_AFTER_SECONDS,
    PUSH_WATCHDOG_ISSUE_ID,
    PUSH_WATCHDOG_MESSAGE_GRACE_SECONDS,
    PUSH_WATCHDOG_SCAN_INTERVAL_SECONDS,
    SILENT_DEVICE_ISSUE_ID_PREFIX,
)
from custom_components.rainpoint.repairs import (
    RainPointPushWatchdog,
    RainPointSilentDeviceIssues,
    SilentDeviceRecord,
    _sanitize_placeholder,
    silent_device_issue_id,
)


class _Clock:
    """A controllable monotonic clock for deterministic threshold math."""

    def __init__(self, start: float = 0.0) -> None:
        self.t = start

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt


def _make_client(connected=False, last_message_at=None):
    client = MagicMock()
    client.connected = connected
    client.last_message_at = last_message_at
    return client


def _make_watchdog(client, clock):
    return RainPointPushWatchdog(MagicMock(), MagicMock(), client, time_source=clock)


@pytest.fixture
def issue_mocks():
    """Isolate the issue-registry create/delete calls per test."""
    with (
        patch.object(repairs.ir, "async_create_issue") as create,
        patch.object(repairs.ir, "async_delete_issue") as delete,
    ):
        yield create, delete


class TestWatchdogRaiseClear:
    """Drop -> raise -> clear state machine."""

    def test_sustained_dead_raises_issue_once(self, issue_mocks):
        """A channel dead past the threshold raises exactly one WARNING issue; a
        second evaluation while still dead does not raise again (dedup)."""
        create, _delete = issue_mocks
        clock = _Clock()
        client = _make_client(connected=False, last_message_at=None)
        watchdog = _make_watchdog(client, clock)

        # First check: outage clock starts, nothing raised yet (transient so far).
        watchdog._async_check()
        create.assert_not_called()

        # Cross the threshold.
        clock.advance(PUSH_WATCHDOG_DEAD_AFTER_SECONDS)
        watchdog._async_check()
        create.assert_called_once()
        kwargs = create.call_args.kwargs
        assert kwargs["is_fixable"] is False
        assert kwargs["severity"] == repairs.ir.IssueSeverity.WARNING
        _hass, domain, issue_id = create.call_args.args
        assert domain == DOMAIN
        assert issue_id == PUSH_WATCHDOG_ISSUE_ID

        # Still dead on the next scan: no second issue.
        clock.advance(PUSH_WATCHDOG_SCAN_INTERVAL_SECONDS)
        watchdog._async_check()
        create.assert_called_once()

    def test_recovery_clears_issue_and_allows_reraise(self, issue_mocks):
        """Recovery deletes the issue and resets the flag so a later outage raises again."""
        create, delete = issue_mocks
        clock = _Clock()
        client = _make_client(connected=False, last_message_at=None)
        watchdog = _make_watchdog(client, clock)

        watchdog._async_check()
        clock.advance(PUSH_WATCHDOG_DEAD_AFTER_SECONDS)
        watchdog._async_check()
        assert create.call_count == 1

        # Recover.
        client.connected = True
        watchdog._async_check()
        delete.assert_called_once_with(watchdog._hass, DOMAIN, PUSH_WATCHDOG_ISSUE_ID)

        # A later, separate outage can raise again.
        client.connected = False
        client.last_message_at = None
        watchdog._async_check()  # starts a fresh outage clock
        clock.advance(PUSH_WATCHDOG_DEAD_AFTER_SECONDS)
        watchdog._async_check()
        assert create.call_count == 2

    def test_recovery_without_active_issue_does_not_delete(self, issue_mocks):
        """An alive check with no active issue must not call delete."""
        _create, delete = issue_mocks
        clock = _Clock()
        client = _make_client(connected=True)
        watchdog = _make_watchdog(client, clock)

        watchdog._async_check()

        delete.assert_not_called()


class TestWatchdogTransient:
    """Transient blips below the threshold stay log-only."""

    def test_blip_below_threshold_raises_nothing(self, issue_mocks):
        create, delete = issue_mocks
        clock = _Clock()
        client = _make_client(connected=False, last_message_at=None)
        watchdog = _make_watchdog(client, clock)

        watchdog._async_check()  # outage clock starts
        clock.advance(PUSH_WATCHDOG_DEAD_AFTER_SECONDS - 1)
        watchdog._async_check()  # still below threshold
        client.connected = True
        watchdog._async_check()  # recovered before threshold

        create.assert_not_called()
        delete.assert_not_called()

    def test_message_grace_uses_short_window_not_dead_after(self, issue_mocks):
        """While disconnected, a message keeps the channel alive only within the
        short grace window, not the full dead-after window. This is what stops
        the message grace and the sustained-dead clock from stacking into a
        doubled time-to-flag."""
        create, _delete = issue_mocks
        clock = _Clock(start=10_000.0)
        client = _make_client(connected=False, last_message_at=None)
        watchdog = _make_watchdog(client, clock)

        # A message inside the grace window keeps it alive despite being disconnected.
        client.last_message_at = clock.t - (PUSH_WATCHDOG_MESSAGE_GRACE_SECONDS - 1)
        watchdog._async_check()
        assert watchdog._dead_since is None

        # A message older than the grace window (but far within dead-after) no
        # longer counts as alive: the outage clock starts. Under the old code
        # (grace == dead-after) this same message would still read as alive.
        client.last_message_at = clock.t - (PUSH_WATCHDOG_MESSAGE_GRACE_SECONDS + 1)
        watchdog._async_check()
        assert watchdog._dead_since is not None
        create.assert_not_called()

    def test_connected_channel_is_alive_regardless_of_message_age(self, issue_mocks):
        """A connected channel is treated as alive even with no recent message.

        This encodes the deliberate limitation: an idle channel with no device
        activity is indistinguishable from a silently detached one from message
        age alone, so staleness while connected is not flagged (which would spam
        repairs during normal quiet periods)."""
        create, _delete = issue_mocks
        clock = _Clock(start=100_000.0)
        client = _make_client(connected=True, last_message_at=0.0)  # ancient message
        watchdog = _make_watchdog(client, clock)

        watchdog._async_check()
        clock.advance(PUSH_WATCHDOG_DEAD_AFTER_SECONDS * 3)
        watchdog._async_check()

        assert watchdog._dead_since is None
        create.assert_not_called()


class TestWatchdogDetectionOnly:
    """The watchdog surfaces only; it never reconnects or changes poll cadence."""

    def test_never_reconnects_or_touches_coordinator(self, issue_mocks):
        clock = _Clock()
        client = _make_client(connected=False, last_message_at=None)
        watchdog = _make_watchdog(client, clock)

        watchdog._async_check()
        clock.advance(PUSH_WATCHDOG_DEAD_AFTER_SECONDS)
        watchdog._async_check()

        # No reconnect / renewal method was ever invoked on the client.
        client.async_start.assert_not_called()
        client.on_http_relogin.assert_not_called()
        client._renew.assert_not_called()
        # The watchdog holds no coordinator reference at all.
        assert not hasattr(watchdog, "_coordinator")
        assert not hasattr(watchdog, "coordinator")


class TestWatchdogTimer:
    """Scheduler wiring: start registers a periodic check; stop cancels it, idempotently."""

    def test_start_registers_interval_and_stop_cancels(self, issue_mocks):
        clock = _Clock()
        client = _make_client()
        watchdog = _make_watchdog(client, clock)

        cancel = MagicMock()
        with patch.object(repairs, "async_track_time_interval", return_value=cancel) as track:
            watchdog.start()

        track.assert_called_once()
        args = track.call_args.args
        assert args[0] is watchdog._hass
        assert args[1] == watchdog._async_check
        assert args[2] == timedelta(seconds=PUSH_WATCHDOG_SCAN_INTERVAL_SECONDS)

        watchdog.async_stop()
        cancel.assert_called_once()

        # Idempotent: a second stop does not cancel again.
        watchdog.async_stop()
        cancel.assert_called_once()

    def test_stop_without_start_is_noop(self):
        watchdog = _make_watchdog(_make_client(), _Clock())
        # No timer registered yet; must not raise.
        watchdog.async_stop()

    def test_start_clears_stale_issue_from_prior_session(self, issue_mocks):
        """start() deletes any pre-existing issue so a reloaded watchdog does not
        leave a stale 'down' issue that a since-recovered channel would never
        clear (the fresh instance has no in-memory record of it)."""
        _create, delete = issue_mocks
        watchdog = _make_watchdog(_make_client(connected=True), _Clock())

        with patch.object(repairs, "async_track_time_interval", return_value=MagicMock()):
            watchdog.start()

        delete.assert_called_once()


def _make_record(hid=100, mid=200, addr=1, model="HTV210B", hub_name="Hub1", missed_polls=3, silent=True, hub_paired=True):
    """Build a SilentDeviceRecord with sensible defaults for one device."""
    return SilentDeviceRecord(
        hid=hid,
        mid=mid,
        addr=addr,
        model=model,
        hub_name=hub_name,
        missed_polls=missed_polls,
        silent=silent,
        hub_paired=hub_paired,
    )


class TestSanitizePlaceholder:
    """T-15-05: cloud-supplied text must not carry Markdown/HTML into a Repairs card."""

    def test_neutralises_markdown_link(self):
        """Bracketed link syntax must not survive into the card."""
        result = _sanitize_placeholder("[Click here](http://evil.example/x)")
        assert "[" not in result
        assert "]" not in result
        assert "(" not in result
        assert ")" not in result

    def test_neutralises_html_tag(self):
        """Angle brackets must not survive, so raw HTML cannot render."""
        result = _sanitize_placeholder("<img src=x onerror=alert(1)>")
        assert "<" not in result
        assert ">" not in result

    def test_collapses_embedded_newline(self):
        """A newline would let cloud text forge its own paragraph in the card."""
        result = _sanitize_placeholder("Hub\nRoom\nBasement")
        assert "\n" not in result
        assert result == "Hub Room Basement"

    def test_truncates_over_long_name(self):
        """An unbounded cloud string must not swamp the card."""
        result = _sanitize_placeholder("x" * 200, limit=64)
        assert len(result) == 64

    def test_empty_input_falls_back_to_unknown(self):
        """An empty value must render as a word, not as a gap in the sentence."""
        assert _sanitize_placeholder("") == "unknown"
        assert _sanitize_placeholder("   ") == "unknown"
        assert _sanitize_placeholder("[]()") == "unknown"

    def test_defangs_a_scheme_prefixed_address_with_a_path(self):
        """Deleting the scheme separator and the slashes leaves display text, not a link."""
        result = _sanitize_placeholder("https://evil.example/phish")
        assert ":" not in result
        assert "/" not in result

    def test_defangs_a_bare_scheme_prefixed_host(self):
        """A bare URL autolinks in Markdown with no surrounding syntax at all."""
        result = _sanitize_placeholder("http://evil.example")
        assert ":" not in result
        assert "/" not in result

    def test_breaks_the_bare_host_prefix_a_renderer_autolinks(self):
        """The www form carries no scheme to delete, so the prefix itself is broken."""
        result = _sanitize_placeholder("www.evil.example")
        assert not result.lower().startswith("www.")
        assert result == "evil.example"

    def test_breaks_the_bare_host_prefix_case_insensitively(self):
        """Renderers autolink WWW. as readily as www."""
        assert not _sanitize_placeholder("WWW.evil.example").lower().startswith("www.")

    @pytest.mark.parametrize("value", ["www_.evil.example", "w#ww.evil.example", "WWW*.evil.example", "ww[w.evil.example"])
    def test_character_deletion_cannot_assemble_an_autolink_prefix(self, value):
        """The deletion pass must not reconstruct a prefix the break already passed.

        Deleting a Markdown-active character can join text either side of it, so
        "www_.evil" becomes "www.evil" during sanitizing. If the break ran first
        it would see the separator, find no prefix, and let the assembled host
        through.
        """
        assert not _sanitize_placeholder(value).lower().startswith("www.")

    def test_removes_the_address_at_sign(self):
        """The at sign is the other prefix a renderer turns into a link."""
        result = _sanitize_placeholder("admin@evil.example")
        assert "@" not in result

    def test_an_address_only_value_still_shows_the_user_something_odd_arrived(self):
        """Defanging must not reduce a hostile value to the unknown fallback, which
        would hide the fact that the cloud sent something strange."""
        result = _sanitize_placeholder("https://evil.example/phish")
        assert result != "unknown"
        assert "evil.example" in result


class TestSilentDeviceIssueId:
    """The issue id doubles as the per-device dedup key, so its shape is a contract."""

    def test_id_shape(self):
        """Pinned because the id is persisted and drives dedup across polls."""
        assert silent_device_issue_id(100, 200, 1) == f"{SILENT_DEVICE_ISSUE_ID_PREFIX}_100_200_1"


class TestRainPointSilentDeviceIssues:
    """Raise-once / dedupe / clear-on-recovery, re-keyed per device."""

    def test_first_sync_with_silent_record_creates_issue_once(self, issue_mocks):
        """The raise-once half of the lifecycle."""
        create, _delete = issue_mocks
        manager = RainPointSilentDeviceIssues(MagicMock())
        record = _make_record()

        manager.async_sync([record])

        create.assert_called_once()
        _hass, domain, issue_id = create.call_args.args
        assert domain == DOMAIN
        assert issue_id == silent_device_issue_id(100, 200, 1)
        kwargs = create.call_args.kwargs
        assert kwargs["is_fixable"] is False
        assert kwargs["severity"] == repairs.ir.IssueSeverity.WARNING
        assert kwargs["translation_key"] == SILENT_DEVICE_ISSUE_ID_PREFIX
        placeholders = kwargs["translation_placeholders"]
        assert placeholders["model"] == "HTV210B"
        assert placeholders["address"] == "1"
        assert placeholders["hub_name"] == "Hub1"
        assert placeholders["missed_polls"] == "3"

    def test_a_device_with_no_hub_names_none_rather_than_unknown(self, issue_mocks):
        """The Bluetooth wrapper case must not render its hub as "unknown".

        The cloud parks a Bluetooth-only device under a placeholder parent
        whose name is an empty string, which the sanitizer would turn into its
        "unknown" fallback. That is the wrong answer here: the card goes on to
        suggest pairing the device to a hub, so "unknown" reads as lost state
        rather than as the absence of a hub. Observed rendering as
        "Hub: unknown" on hardware before this was fixed.
        """
        create, _delete = issue_mocks
        manager = RainPointSilentDeviceIssues(MagicMock())

        manager.async_sync([_make_record(hub_name="", hub_paired=False)])

        assert create.call_args.kwargs["translation_placeholders"]["hub_name"] == "none"

    def test_a_hub_paired_device_with_no_name_still_reports_unknown(self, issue_mocks):
        """A missing name on a real hub is genuinely unknown and must stay that way.

        The pair to the test above: only the absence of a hub earns "none",
        so the sanitizer's fallback still has to fire for a hub that exists
        but did not tell us what it is called.
        """
        create, _delete = issue_mocks
        manager = RainPointSilentDeviceIssues(MagicMock())

        manager.async_sync([_make_record(hub_name="", hub_paired=True)])

        assert create.call_args.kwargs["translation_placeholders"]["hub_name"] == "unknown"

    def test_second_sync_with_same_record_does_not_recreate(self, issue_mocks):
        """A device staying silent must not raise a second issue every poll."""
        create, _delete = issue_mocks
        manager = RainPointSilentDeviceIssues(MagicMock())
        record = _make_record()

        manager.async_sync([record])
        manager.async_sync([record])

        create.assert_called_once()

    def test_flipping_to_not_silent_deletes_the_issue(self, issue_mocks):
        """Recovery clears the issue without waiting for anything else."""
        create, delete = issue_mocks
        manager = RainPointSilentDeviceIssues(MagicMock())
        record = _make_record()

        manager.async_sync([record])
        create.assert_called_once()

        recovered = _make_record(silent=False)
        manager.async_sync([recovered])

        delete.assert_called_once()
        _hass, domain, issue_id = delete.call_args.args
        assert domain == DOMAIN
        assert issue_id == silent_device_issue_id(100, 200, 1)

    def test_omitting_a_previously_active_record_still_clears_it(self, issue_mocks):
        """A device that disappears from subDevices entirely (D-03) is not in the
        next poll's records at all; its issue must still be cleared."""
        create, delete = issue_mocks
        manager = RainPointSilentDeviceIssues(MagicMock())
        record = _make_record()

        manager.async_sync([record])
        create.assert_called_once()

        manager.async_sync([])

        delete.assert_called_once()
        _hass, domain, issue_id = delete.call_args.args
        assert domain == DOMAIN
        assert issue_id == silent_device_issue_id(100, 200, 1)

    def test_never_active_record_still_issues_idempotent_delete(self, issue_mocks):
        """Clearing unconditionally is what stops a pre-reload issue stranding."""
        _create, delete = issue_mocks
        manager = RainPointSilentDeviceIssues(MagicMock())
        record = _make_record(silent=False)

        manager.async_sync([record])

        delete.assert_called_once()

    def test_registry_error_is_swallowed_and_logged(self, issue_mocks, caplog):
        """A failing diagnostic surface must never break the poll that drives it."""
        create, delete = issue_mocks
        create.side_effect = RuntimeError("registry unavailable")
        delete.side_effect = RuntimeError("registry unavailable")
        manager = RainPointSilentDeviceIssues(MagicMock())

        with caplog.at_level(logging.DEBUG, logger="custom_components.rainpoint.repairs"):
            manager.async_sync([_make_record()])
            manager.async_sync([_make_record(silent=False)])

        messages = [r.getMessage() for r in caplog.records]
        assert any("Failed to create the not-reporting repair issue" in m for m in messages)
        assert any("Failed to delete the not-reporting repair issue" in m for m in messages)

    def test_a_failed_raise_is_retried_on_the_next_poll(self, issue_mocks):
        """A device must not be silenced for the session by one registry error.

        The dedup guard keys on the active set, so marking the issue active
        before the registry accepted it would make a transient failure
        permanent: every later poll would take the already-active early return
        and never retry.
        """
        create, _delete = issue_mocks
        create.side_effect = [RuntimeError("registry unavailable"), None]
        manager = RainPointSilentDeviceIssues(MagicMock())

        manager.async_sync([_make_record()])
        manager.async_sync([_make_record()])

        assert create.call_count == 2

    def test_async_clear_deletes_by_key(self, issue_mocks):
        """The push-arrival half of the lifecycle, which does not wait for a poll."""
        _create, delete = issue_mocks
        manager = RainPointSilentDeviceIssues(MagicMock())
        manager.async_sync([_make_record()])

        manager.async_clear(100, 200, 1)

        assert delete.call_count == 1
        _hass, domain, issue_id = delete.call_args.args
        assert domain == DOMAIN
        assert issue_id == silent_device_issue_id(100, 200, 1)


class TestUnreachableIdsAreNotCleared:
    """An id whose owning hub could not be reached this poll is left exactly as it is."""

    def test_unmentioned_but_unreachable_id_is_not_cleared(self, issue_mocks):
        """The outage case: no record mentions it, but its hub is down, so the
        stale sweep must skip it rather than read the silence as a removal."""
        create, delete = issue_mocks
        manager = RainPointSilentDeviceIssues(MagicMock())
        issue_id = silent_device_issue_id(100, 200, 1)

        manager.async_sync([_make_record()])
        create.assert_called_once()

        manager.async_sync([], unreachable_ids={issue_id})

        assert delete.call_count == 0

    def test_unmentioned_and_reachable_id_is_still_cleared(self, issue_mocks):
        """The contrast case, proving the skip is scoped to the unreachable set."""
        _create, delete = issue_mocks
        manager = RainPointSilentDeviceIssues(MagicMock())

        manager.async_sync([_make_record()])
        manager.async_sync([], unreachable_ids=set())

        assert delete.call_count == 1
        _hass, _domain, issue_id = delete.call_args.args
        assert issue_id == silent_device_issue_id(100, 200, 1)

    def test_unreachable_id_that_is_not_active_produces_nothing(self, issue_mocks):
        """Skipping an unreachable id must not invent work for one never raised."""
        create, delete = issue_mocks
        manager = RainPointSilentDeviceIssues(MagicMock())

        manager.async_sync([], unreachable_ids={silent_device_issue_id(100, 200, 9)})

        assert create.call_count == 0
        assert delete.call_count == 0

    def test_a_mentioned_silent_record_still_raises_once_alongside_an_unreachable_id(self, issue_mocks):
        """Another hub being down must not suppress a raise for a hub that reported."""
        create, delete = issue_mocks
        manager = RainPointSilentDeviceIssues(MagicMock())
        other_id = silent_device_issue_id(100, 300, 1)

        manager.async_sync([_make_record()], unreachable_ids={other_id})
        manager.async_sync([_make_record()], unreachable_ids={other_id})

        assert create.call_count == 1
        assert delete.call_count == 0
