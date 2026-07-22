"""Tests for the push-channel liveness watchdog (repairs.py)."""

from __future__ import annotations

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
)
from custom_components.rainpoint.repairs import RainPointPushWatchdog


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
