"""Tests for the integration's Repairs surfaces (repairs.py): the push-channel
liveness watchdog and the per-device silent sub-device issue lifecycle."""

from __future__ import annotations

import logging
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from custom_components.rainpoint import repairs
from custom_components.rainpoint.const import (
    DOMAIN,
    HUB_CONNECTIVITY_ISSUE_ID_PREFIX,
    ORPHANED_ENTITIES_ISSUE_ID_PREFIX,
    PUSH_HUB_IDENTITY_ISSUE_ID,
    PUSH_WATCHDOG_DEAD_AFTER_SECONDS,
    PUSH_WATCHDOG_ISSUE_ID,
    PUSH_WATCHDOG_MESSAGE_GRACE_SECONDS,
    PUSH_WATCHDOG_SCAN_INTERVAL_SECONDS,
    SILENT_DEVICE_ISSUE_ID_PREFIX,
)
from custom_components.rainpoint.repairs import (
    _ENTITY_LIST_LIMIT,
    HubConnectivityRecord,
    OrphanedEntitiesRecord,
    RainPointHubConnectivityIssues,
    RainPointOrphanedEntitiesRepairFlow,
    RainPointOrphanedEntityIssues,
    RainPointPushWatchdog,
    RainPointSilentDeviceIssues,
    SilentDeviceRecord,
    _format_entity_list,
    _sanitize_placeholder,
    _snapshot_offered_pairs,
    async_create_fix_flow,
    hub_connectivity_issue_id,
    orphaned_entities_issue_id,
    push_hub_identity_issue_id,
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


class TestFormatEntityList:
    """Entity ids are validated and passed, never sanitized and hoped over.

    The card's list is the strongest part of its promise about what Submit
    takes, so it has to name a row the user can find in their own registry.
    _sanitize_placeholder cannot do that job: it deletes underscores and dots,
    which is most of what an entity id is made of.
    """

    def test_one_entity_id_renders_inside_a_code_span(self):
        """The code span is what keeps an underscore an underscore rather than
        the start of emphasis."""
        assert _format_entity_list(["sensor.htv210b_unsupported_htv210b"]) == "  - `sensor.htv210b_unsupported_htv210b`"

    def test_the_sanitizer_would_have_destroyed_the_same_value(self):
        """Stated as a comparison, because it is the whole argument for a
        second function rather than a reuse of the first."""
        assert _sanitize_placeholder("sensor.htv210b_unsupported_htv210b") == "sensor.htv210bunsupportedhtv210b"
        assert "sensor.htv210b_unsupported_htv210b" in _format_entity_list(["sensor.htv210b_unsupported_htv210b"])

    def test_every_id_gets_its_own_line(self):
        """One clause of fact per line, which is also what makes a list of ten
        readable in a card."""
        rendered = _format_entity_list(["sensor.a_one", "valve.b_two"])

        assert rendered.splitlines() == ["  - `sensor.a_one`", "  - `valve.b_two`"]

    @pytest.mark.parametrize(
        "entity_id",
        [
            "sensor.has`backtick",
            "sensor.Has_Capitals",
            "sensor.has space",
            "no_domain_at_all",
            "sensor.two.dots",
            "sensor.<img src=x>",
            "",
        ],
    )
    def test_a_value_outside_the_charset_is_dropped_rather_than_repaired(self, entity_id):
        """Repairing would invent an id, and this list is a promise about which
        rows Submit takes. A name the user cannot match against their own
        registry is worse than one fewer name."""
        assert _format_entity_list([entity_id]) == ""

    def test_a_non_string_value_is_dropped_too(self):
        """The record is plain data any caller can build, so the type is
        checked rather than assumed."""
        assert _format_entity_list([None, 42]) == ""

    def test_a_trailing_newline_does_not_slip_past_the_anchor(self):
        """The one value a charset check anchored on ``$`` still admits.

        ``$`` matches before a final newline as well as at the end of the
        string, so "sensor.foo\\n" passes a check that reads as if it could not
        and then breaks the list item it is rendered into across two lines. The
        whole claim this function makes is that it validates rather than
        sanitizes, so the anchor has to mean the end of the string.
        """
        assert _format_entity_list(["sensor.trailing_newline\n"]) == ""
        assert _format_entity_list(["sensor.good_row", "sensor.trailing_newline\n"]).splitlines() == [
            "  - `sensor.good_row`",
            "  - and 1 more",
        ]

    def test_a_dropped_value_cannot_close_the_code_span_it_would_have_sat_in(self):
        """The security property the backticks rest on, driven rather than
        asserted about the pattern: a value carrying a backtick never reaches
        the rendered list, so it cannot escape into the surrounding Markdown."""
        rendered = _format_entity_list(["sensor.good_row", "sensor.evil`](http://evil.example)"])

        assert rendered.splitlines() == ["  - `sensor.good_row`", "  - and 1 more"]
        assert "evil.example" not in rendered
        assert rendered.count("`") == 2

    def test_nothing_supplied_renders_nothing(self):
        """An empty list leaves the card its count line and no list at all,
        rather than an empty bullet."""
        assert _format_entity_list([]) == ""

    def test_a_long_list_is_capped_and_the_rest_is_counted_in_plain_language(self):
        """A departed device can carry ten rows or more, and an uncapped list
        would run a translation placeholder to whatever length the registry
        happens to hold."""
        rendered = _format_entity_list([f"sensor.row_{index}" for index in range(_ENTITY_LIST_LIMIT + 3)])
        lines = rendered.splitlines()

        assert len(lines) == _ENTITY_LIST_LIMIT + 1
        assert lines[-1] == "  - and 3 more"

    def test_a_dropped_value_is_counted_in_the_overflow_rather_than_vanishing(self):
        """The remainder is measured against everything supplied, so the list
        and the count line above it cannot disagree about how many rows are in
        scope."""
        rendered = _format_entity_list(["sensor.good_row", "sensor.Bad Row"], limit=1)

        assert rendered.splitlines() == ["  - `sensor.good_row`", "  - and 1 more"]


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

    def test_recovery_from_a_raised_issue_logs_once(self, issue_mocks, caplog):
        """The WARNING on raise needs a matching line on clear.

        Mirrors RainPointHubConnectivityIssues; without it the log shows a
        device going silent and never coming back.
        """
        manager = RainPointSilentDeviceIssues(MagicMock())

        with caplog.at_level(logging.INFO, logger="custom_components.rainpoint.repairs"):
            manager.async_sync([_make_record()])
            manager.async_sync([_make_record(silent=False)])

        recovered = [r for r in caplog.records if "reporting again" in r.getMessage()]
        assert len(recovered) == 1
        assert silent_device_issue_id(100, 200, 1) in recovered[0].getMessage()

    def test_healthy_device_polls_never_log_the_recovery_line(self, issue_mocks, caplog):
        """The clear runs on every poll for every reporting device, so it is gated."""
        manager = RainPointSilentDeviceIssues(MagicMock())

        with caplog.at_level(logging.INFO, logger="custom_components.rainpoint.repairs"):
            for _ in range(5):
                manager.async_sync([_make_record(silent=False)])

        assert not [r for r in caplog.records if "reporting again" in r.getMessage()]

    def test_a_removed_device_is_not_logged_as_having_resumed_reporting(self, issue_mocks, caplog):
        """The stale sweep is a removal, not a recovery, and must not claim otherwise.

        stale_id is drawn from _active by construction, so the was_active gate
        is always true on that path. A device dropped from the hub's
        sub-device list would otherwise be logged as reporting again, telling
        an operator reading the log the opposite of what happened.
        """
        manager = RainPointSilentDeviceIssues(MagicMock())

        with caplog.at_level(logging.INFO, logger="custom_components.rainpoint.repairs"):
            manager.async_sync([_make_record()])
            manager.async_sync([])

        messages = [r.getMessage() for r in caplog.records]
        assert not [m for m in messages if "reporting again" in m]
        assert [m for m in messages if "no longer listed on its hub" in m]

    def test_push_arrival_clearing_does_not_log_per_message(self, issue_mocks, caplog):
        """async_clear runs per pushed message, far more often than the poll.

        Gating on the active set is what stops a chatty device turning the log
        into one line per push for the rest of the session. The first call
        after a raise still logs, because that one is a real recovery.
        """
        manager = RainPointSilentDeviceIssues(MagicMock())

        with caplog.at_level(logging.INFO, logger="custom_components.rainpoint.repairs"):
            manager.async_sync([_make_record()])
            for _ in range(10):
                manager.async_clear(100, 200, 1)

        assert len([r for r in caplog.records if "reporting again" in r.getMessage()]) == 1

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


def _make_hub_record(hid=100, mid=200, hub_name="Hub1", disconnected=True, missed_polls=3, model: str | None = "HWG023WBRF-V2"):
    """Build a HubConnectivityRecord with sensible defaults for one hub."""
    return HubConnectivityRecord(
        hid=hid,
        mid=mid,
        hub_name=hub_name,
        disconnected=disconnected,
        missed_polls=missed_polls,
        model=model,
    )


class TestHubConnectivityIssueId:
    """The issue id doubles as the per-hub dedup key, so its shape is a contract."""

    def test_id_shape(self):
        """Pinned because the id is persisted and drives dedup across polls."""
        assert hub_connectivity_issue_id(100, 200) == f"{HUB_CONNECTIVITY_ISSUE_ID_PREFIX}_100_200"


class TestRainPointHubConnectivityIssues:
    """Raise-once / dedupe / clear-on-recovery, re-keyed per hub."""

    def test_first_sync_with_disconnected_record_creates_issue_once(self, issue_mocks):
        """The raise-once half of the lifecycle."""
        create, _delete = issue_mocks
        manager = RainPointHubConnectivityIssues(MagicMock())
        record = _make_hub_record()

        manager.async_sync([record])

        create.assert_called_once()
        _hass, domain, issue_id = create.call_args.args
        assert domain == DOMAIN
        assert issue_id == hub_connectivity_issue_id(100, 200)
        kwargs = create.call_args.kwargs
        assert kwargs["is_fixable"] is False
        assert kwargs["severity"] == repairs.ir.IssueSeverity.WARNING
        assert kwargs["translation_key"] == HUB_CONNECTIVITY_ISSUE_ID_PREFIX
        placeholders = kwargs["translation_placeholders"]
        assert set(placeholders) == {"hub_name", "model", "missed_polls"}
        assert placeholders["hub_name"] == "Hub1"
        assert placeholders["model"] == "HWG023WBRF-V2"
        assert placeholders["missed_polls"] == "3"

    def test_absent_model_falls_back_rather_than_rendering_blank_parens(self, issue_mocks):
        """A hub record with no model must still render a readable card.

        The model rides the same sanitizer as the hub name, so an absent value
        becomes the literal "unknown" instead of leaving empty parentheses.
        """
        create, _delete = issue_mocks
        manager = RainPointHubConnectivityIssues(MagicMock())

        manager.async_sync([_make_hub_record(model=None)])

        assert create.call_args.kwargs["translation_placeholders"]["model"] == "unknown"

    def test_hub_name_reaches_the_placeholder_sanitized(self, issue_mocks):
        """T-16-04: a hostile hub name must not survive into the card.

        Feeds inputs an attacker would actually construct, not an
        already-clean specimen: a separator embedded inside a bare host
        prefix, a scheme separator, and bracketed link syntax.
        """
        create, _delete = issue_mocks
        manager = RainPointHubConnectivityIssues(MagicMock())

        manager.async_sync([_make_hub_record(hub_name="ww[w.evil.example")])
        assert not create.call_args.kwargs["translation_placeholders"]["hub_name"].lower().startswith("www.")

        manager.async_sync([_make_hub_record(mid=201, hub_name="http://evil.example/phish")])
        sanitized = create.call_args.kwargs["translation_placeholders"]["hub_name"]
        assert ":" not in sanitized
        assert "/" not in sanitized

        manager.async_sync([_make_hub_record(mid=202, hub_name="[Click here](http://evil.example)")])
        sanitized = create.call_args.kwargs["translation_placeholders"]["hub_name"]
        assert "[" not in sanitized
        assert "]" not in sanitized
        assert "(" not in sanitized
        assert ")" not in sanitized

    def test_second_sync_with_same_record_does_not_recreate(self, issue_mocks):
        """A hub staying disconnected must not raise a second issue every poll."""
        create, _delete = issue_mocks
        manager = RainPointHubConnectivityIssues(MagicMock())
        record = _make_hub_record()

        manager.async_sync([record])
        manager.async_sync([record])

        create.assert_called_once()

    def test_flipping_to_connected_deletes_the_issue(self, issue_mocks):
        """Recovery clears the issue without waiting for anything else."""
        create, delete = issue_mocks
        manager = RainPointHubConnectivityIssues(MagicMock())
        record = _make_hub_record()

        manager.async_sync([record])
        create.assert_called_once()

        recovered = _make_hub_record(disconnected=False)
        manager.async_sync([recovered])

        delete.assert_called_once()
        _hass, domain, issue_id = delete.call_args.args
        assert domain == DOMAIN
        assert issue_id == hub_connectivity_issue_id(100, 200)

    def test_omitting_a_previously_active_record_still_clears_it(self, issue_mocks):
        """A hub that disappears from the device list entirely is not in the
        next poll's records at all; its issue must still be cleared."""
        create, delete = issue_mocks
        manager = RainPointHubConnectivityIssues(MagicMock())
        record = _make_hub_record()

        manager.async_sync([record])
        create.assert_called_once()

        manager.async_sync([])

        delete.assert_called_once()
        _hass, domain, issue_id = delete.call_args.args
        assert domain == DOMAIN
        assert issue_id == hub_connectivity_issue_id(100, 200)

    def test_never_active_record_still_issues_idempotent_delete(self, issue_mocks):
        """Clearing unconditionally is what stops a pre-reload issue stranding."""
        _create, delete = issue_mocks
        manager = RainPointHubConnectivityIssues(MagicMock())
        record = _make_hub_record(disconnected=False)

        manager.async_sync([record])

        delete.assert_called_once()

    def test_recovery_from_a_raised_issue_logs_once(self, issue_mocks, caplog):
        """The WARNING on raise needs a matching line on clear.

        Without it the log shows an outage starting and never ending, which is
        indistinguishable from an outage still running.
        """
        manager = RainPointHubConnectivityIssues(MagicMock())

        with caplog.at_level(logging.INFO, logger="custom_components.rainpoint.repairs"):
            manager.async_sync([_make_hub_record()])
            manager.async_sync([_make_hub_record(disconnected=False)])

        recovered = [r for r in caplog.records if "connectivity restored" in r.getMessage()]
        assert len(recovered) == 1
        assert hub_connectivity_issue_id(100, 200) in recovered[0].getMessage()

    def test_healthy_hub_polls_never_log_the_recovery_line(self, issue_mocks, caplog):
        """The clear runs on every connected poll, so the line must be gated.

        Ungated it would print once per hub per scan interval forever, which is
        the reason the delete itself stays unconditional but the log does not.
        """
        manager = RainPointHubConnectivityIssues(MagicMock())

        with caplog.at_level(logging.INFO, logger="custom_components.rainpoint.repairs"):
            for _ in range(5):
                manager.async_sync([_make_hub_record(disconnected=False)])

        assert not [r for r in caplog.records if "connectivity restored" in r.getMessage()]

    def test_a_removed_hub_is_not_logged_as_having_reconnected(self, issue_mocks, caplog):
        """The stale sweep is a removal, not a reconnection, and must not claim otherwise.

        Mirrors the silent-device case. A hub unpaired or lost to a home
        restructure would otherwise be logged as connectivity restored.
        """
        manager = RainPointHubConnectivityIssues(MagicMock())

        with caplog.at_level(logging.INFO, logger="custom_components.rainpoint.repairs"):
            manager.async_sync([_make_hub_record()])
            manager.async_sync([])

        messages = [r.getMessage() for r in caplog.records]
        assert not [m for m in messages if "connectivity restored" in m]
        assert [m for m in messages if "no longer listed on the account" in m]

    def test_recovery_line_does_not_repeat_on_later_connected_polls(self, issue_mocks, caplog):
        """One line per outage, not one per poll for the rest of the session."""
        manager = RainPointHubConnectivityIssues(MagicMock())

        with caplog.at_level(logging.INFO, logger="custom_components.rainpoint.repairs"):
            manager.async_sync([_make_hub_record()])
            manager.async_sync([_make_hub_record(disconnected=False)])
            manager.async_sync([_make_hub_record(disconnected=False)])
            manager.async_sync([_make_hub_record(disconnected=False)])

        assert len([r for r in caplog.records if "connectivity restored" in r.getMessage()]) == 1

    def test_registry_error_is_swallowed_and_logged(self, issue_mocks, caplog):
        """A failing diagnostic surface must never break the poll that drives it."""
        create, delete = issue_mocks
        create.side_effect = RuntimeError("registry unavailable")
        delete.side_effect = RuntimeError("registry unavailable")
        manager = RainPointHubConnectivityIssues(MagicMock())

        with caplog.at_level(logging.DEBUG, logger="custom_components.rainpoint.repairs"):
            manager.async_sync([_make_hub_record()])
            manager.async_sync([_make_hub_record(disconnected=False)])

        messages = [r.getMessage() for r in caplog.records]
        assert any("Failed to create the hub connectivity repair issue" in m for m in messages)
        assert any("Failed to delete the hub connectivity repair issue" in m for m in messages)

    def test_a_failed_raise_is_retried_on_the_next_poll(self, issue_mocks):
        """A hub must not be silenced for the session by one registry error."""
        create, _delete = issue_mocks
        create.side_effect = [RuntimeError("registry unavailable"), None]
        manager = RainPointHubConnectivityIssues(MagicMock())

        manager.async_sync([_make_hub_record()])
        manager.async_sync([_make_hub_record()])

        assert create.call_count == 2

    def test_async_clear_deletes_by_key(self, issue_mocks):
        """The push-arrival half of the lifecycle, which does not wait for a poll."""
        _create, delete = issue_mocks
        manager = RainPointHubConnectivityIssues(MagicMock())
        manager.async_sync([_make_hub_record()])

        manager.async_clear(100, 200)

        assert delete.call_count == 1
        _hass, domain, issue_id = delete.call_args.args
        assert domain == DOMAIN
        assert issue_id == hub_connectivity_issue_id(100, 200)

    def test_async_clear_on_an_id_never_raised_is_still_an_idempotent_delete(self, issue_mocks):
        """A fresh instance after a reload can still clear an issue a prior
        session raised, because deleting an unknown id is already a no-op."""
        _create, delete = issue_mocks
        manager = RainPointHubConnectivityIssues(MagicMock())

        manager.async_clear(100, 200)

        assert delete.call_count == 1
        _hass, domain, issue_id = delete.call_args.args
        assert domain == DOMAIN
        assert issue_id == hub_connectivity_issue_id(100, 200)


class TestHubConnectivityUnreachableIdsAreNotCleared:
    """An id whose owning hub's connectivity could not be determined this poll
    is left exactly as it is."""

    def test_unmentioned_but_unreachable_id_is_not_cleared(self, issue_mocks):
        """The unknown-tri-state case: no record mentions it, so the stale
        sweep must skip it rather than read the silence as a recovery."""
        create, delete = issue_mocks
        manager = RainPointHubConnectivityIssues(MagicMock())
        issue_id = hub_connectivity_issue_id(100, 200)

        manager.async_sync([_make_hub_record()])
        create.assert_called_once()

        manager.async_sync([], unreachable_ids={issue_id})

        assert delete.call_count == 0

    def test_unmentioned_and_reachable_id_is_still_cleared(self, issue_mocks):
        """The contrast case, proving the skip is scoped to the unreachable set."""
        _create, delete = issue_mocks
        manager = RainPointHubConnectivityIssues(MagicMock())

        manager.async_sync([_make_hub_record()])
        manager.async_sync([], unreachable_ids=set())

        assert delete.call_count == 1
        _hass, _domain, issue_id = delete.call_args.args
        assert issue_id == hub_connectivity_issue_id(100, 200)

    def test_unreachable_id_that_is_not_active_produces_nothing(self, issue_mocks):
        """Skipping an unreachable id must not invent work for one never raised."""
        create, delete = issue_mocks
        manager = RainPointHubConnectivityIssues(MagicMock())

        manager.async_sync([], unreachable_ids={hub_connectivity_issue_id(100, 999)})

        assert create.call_count == 0
        assert delete.call_count == 0

    def test_a_mentioned_disconnected_record_still_raises_once_alongside_an_unreachable_id(self, issue_mocks):
        """Another hub's unknown connectivity must not suppress a raise for a hub
        that is genuinely disconnected."""
        create, delete = issue_mocks
        manager = RainPointHubConnectivityIssues(MagicMock())
        other_id = hub_connectivity_issue_id(100, 300)

        manager.async_sync([_make_hub_record()], unreachable_ids={other_id})
        manager.async_sync([_make_hub_record()], unreachable_ids={other_id})

        assert create.call_count == 1
        assert delete.call_count == 0


def _make_orphan_record(
    sensor_key="100_200_1",
    entry_id="e1",
    addr=1,
    model="HTV245FRF",
    sub_name="Front Valve",
    hub_name="Hub A",
    entity_count=2,
    missed_polls=30,
    orphaned=True,
    hub_paired=True,
    device_name=None,
    hub_device_name=None,
    leftover=False,
    entity_ids=(),
):
    """Build an OrphanedEntitiesRecord with sensible defaults for one key.

    device_name defaults to None, which is the shape a departed key or an
    unreadable device registry produces, so a caller that does not pass it
    exercises the sub_name fallback exactly as most existing tests here rely
    on it doing.

    leftover defaults to False, the departed-key shape, which is the one that
    shipped first and the one every existing test here means.

    hub_device_name defaults to None for the same reason device_name does: it
    is the shape an unreadable device registry, or a hub with no row of the
    migrated identifier shape, produces, and it exercises the cloud hub_name
    fallback every existing test here relies on.

    entity_ids defaults to empty, which is what the departed-key shape always
    carries and what a still-present record whose ids could not be resolved
    carries too.
    """
    return OrphanedEntitiesRecord(
        entry_id=entry_id,
        sensor_key=sensor_key,
        addr=addr,
        model=model,
        sub_name=sub_name,
        hub_name=hub_name,
        entity_count=entity_count,
        missed_polls=missed_polls,
        orphaned=orphaned,
        hub_paired=hub_paired,
        device_name=device_name,
        hub_device_name=hub_device_name,
        leftover=leftover,
        entity_ids=entity_ids,
    )


class TestOrphanedEntitiesIssueId:
    """The issue id doubles as the per-key dedup key, so its shape is a contract."""

    def test_id_shape(self):
        """Pinned because the id drives dedup across every update."""
        assert orphaned_entities_issue_id("100_200_1", "e1") == f"{ORPHANED_ENTITIES_ISSUE_ID_PREFIX}_e1_100_200_1"

    def test_two_config_entries_sharing_a_key_get_two_distinct_ids(self):
        """A sensor key is {hid}_{mid}_{addr}, so two config entries resolving
        the same home produce the same key for the same sub-device.

        An unscoped id would make both entries dedup against one card and,
        worse, make either entry's unload withdraw a card the other raised and
        never consented to. This manager is the only one that withdraws its own
        cards, which is why neither sibling id needs the entry."""
        assert orphaned_entities_issue_id("100_200_1", "e1") != orphaned_entities_issue_id("100_200_1", "e2")

    def test_two_distinct_keys_give_two_distinct_ids(self):
        """One card per key means two keys must never converge on one card."""
        assert orphaned_entities_issue_id("100_200_1", "e1") != orphaned_entities_issue_id("100_200_2", "e1")

    def test_a_longer_addr_does_not_collide_with_a_shorter_one(self):
        """The case a prefix match would confuse, and the reason the flow reads
        the key from the issue data rather than parsing it back out of the id."""
        assert orphaned_entities_issue_id("100_200_1", "e1") != orphaned_entities_issue_id("100_200_11", "e1")


class TestPushHubIdentityIssueId:
    """The issue id is entry-scoped so two config entries never clobber or clear one another's card."""

    def test_id_shape(self):
        """Pinned because the id is the registry-facing dedup/clear key."""
        assert push_hub_identity_issue_id("e1") == f"{PUSH_HUB_IDENTITY_ISSUE_ID}_e1"

    def test_two_config_entries_get_two_distinct_ids(self):
        """An unscoped id would let one entry's resolving setup silently clear
        another entry's still-unresolved card, or strand a card with no code
        path left to clear it if the entry that raised it were removed."""
        assert push_hub_identity_issue_id("e1") != push_hub_identity_issue_id("e2")


class TestRainPointOrphanedEntityIssues:
    """Raise-once / dedupe / clear-on-return, re-keyed per sensor key."""

    def test_an_orphaned_record_raises_one_fixable_issue(self, issue_mocks):
        """The integration's only fixable card: everything about it is pinned."""
        create, _delete = issue_mocks
        manager = RainPointOrphanedEntityIssues(MagicMock())

        manager.async_sync([_make_orphan_record()])

        create.assert_called_once()
        _hass, domain, issue_id = create.call_args.args
        assert domain == DOMAIN
        assert issue_id == orphaned_entities_issue_id("100_200_1", "e1")
        kwargs = create.call_args.kwargs
        assert kwargs["is_fixable"] is True
        assert kwargs["severity"] == repairs.ir.IssueSeverity.WARNING
        assert kwargs["translation_key"] == ORPHANED_ENTITIES_ISSUE_ID_PREFIX
        assert kwargs["data"] == {"entry_id": "e1", "sensor_key": "100_200_1"}
        assert "is_persistent" not in kwargs
        placeholders = kwargs["translation_placeholders"]
        assert placeholders["device_name"] == "Front Valve"
        assert placeholders["model"] == "HTV245FRF"
        assert placeholders["address"] == "1"
        assert placeholders["hub_name"] == "Hub A"
        assert placeholders["entity_count"] == "2"
        assert placeholders["missed_polls"] == "30"

    def test_a_repeat_sync_does_not_raise_a_second_card(self, issue_mocks):
        """A key stays aged out for as long as it stays gone, and every update
        reconciles, so an undeduped raise would be one card per poll."""
        create, _delete = issue_mocks
        manager = RainPointOrphanedEntityIssues(MagicMock())
        record = _make_orphan_record()

        manager.async_sync([record])
        manager.async_sync([record])

        create.assert_called_once()

    def test_a_key_that_comes_back_clears_its_own_card(self, issue_mocks):
        """The transient case is handled without a human ever seeing it."""
        create, delete = issue_mocks
        manager = RainPointOrphanedEntityIssues(MagicMock())

        manager.async_sync([_make_orphan_record()])
        create.assert_called_once()

        manager.async_sync([_make_orphan_record(orphaned=False)])

        delete.assert_called_once()
        _hass, domain, issue_id = delete.call_args.args
        assert domain == DOMAIN
        assert issue_id == orphaned_entities_issue_id("100_200_1", "e1")

    def test_a_non_orphaned_record_clears_even_an_id_this_instance_never_raised(self, issue_mocks):
        """A fresh manager after a reload has no memory of what a prior session
        raised, so guarding the delete on the active set would strand it."""
        _create, delete = issue_mocks
        manager = RainPointOrphanedEntityIssues(MagicMock())

        manager.async_sync([_make_orphan_record(orphaned=False)])

        delete.assert_called_once()

    def test_an_id_no_record_mentions_is_cleared_as_a_removal(self, issue_mocks, caplog):
        """Once a confirmed fix empties the ledger entry, the key produces no
        record at all; the card must go, and must not claim the device came
        back when it did not."""
        _create, delete = issue_mocks
        manager = RainPointOrphanedEntityIssues(MagicMock())
        manager.async_sync([_make_orphan_record()])

        with caplog.at_level(logging.INFO, logger="custom_components.rainpoint.repairs"):
            manager.async_sync([])

        delete.assert_called_once()
        messages = [r.getMessage() for r in caplog.records]
        assert [m for m in messages if "No leftover entities remain to offer" in m]
        assert not [m for m in messages if "lists this device again" in m]

    def test_a_returning_key_logs_the_recovery_line_instead(self, issue_mocks, caplog):
        """The pair to the test above: the two clears are different events and
        one message cannot honestly describe both."""
        _create, _delete = issue_mocks
        manager = RainPointOrphanedEntityIssues(MagicMock())
        manager.async_sync([_make_orphan_record()])

        with caplog.at_level(logging.INFO, logger="custom_components.rainpoint.repairs"):
            manager.async_sync([_make_orphan_record(orphaned=False)])

        assert [r.getMessage() for r in caplog.records if "lists this device again" in r.getMessage()]

    def test_a_failed_raise_is_retried_rather_than_suppressed_forever(self, issue_mocks, caplog):
        """Marking active before the registry accepted would let one transient
        error silence this key for the rest of the session."""
        create, _delete = issue_mocks
        create.side_effect = RuntimeError("registry down")
        manager = RainPointOrphanedEntityIssues(MagicMock())

        with caplog.at_level(logging.DEBUG, logger="custom_components.rainpoint.repairs"):
            manager.async_sync([_make_orphan_record()])
            manager.async_sync([_make_orphan_record()])

        assert create.call_count == 2
        assert [r.getMessage() for r in caplog.records if "Failed to create the orphaned entities" in r.getMessage()]

    @staticmethod
    def _registry_holding(*issue_ids):
        """Return an issue registry stand-in holding exactly these ids."""
        held = {(DOMAIN, issue_id): object() for issue_id in issue_ids}
        return SimpleNamespace(async_get_issue=lambda domain, issue_id: held.get((domain, issue_id)))

    def test_a_card_the_registry_still_holds_is_not_raised_twice(self, issue_mocks):
        """The ordinary dedup, reconciled rather than assumed. A key stays aged
        out for as long as it stays gone and every update reconciles, so the
        card the registry still holds must not be raised a second time."""
        create, _delete = issue_mocks
        manager = RainPointOrphanedEntityIssues(MagicMock())
        issue_id = orphaned_entities_issue_id("100_200_1", "e1")

        with patch.object(repairs.ir, "async_get", return_value=self._registry_holding(issue_id)):
            manager.async_sync([_make_orphan_record()])
            manager.async_sync([_make_orphan_record()])

        create.assert_called_once()

    def test_a_card_home_assistant_deleted_after_a_no_op_confirm_is_raised_again(self, issue_mocks):
        """The hole the reconcile exists to close, and the one place this card
        differs from the two non-fixable ones.

        Home Assistant's repairs flow manager deletes a fixable issue itself on
        any non-abort flow result, so a confirm that removed nothing leaves the
        card gone from the UI while the active set still holds its id. Deduping
        on that set alone would suppress every later raise and leave the user
        with leftover entities and no surface left to act on."""
        create, _delete = issue_mocks
        manager = RainPointOrphanedEntityIssues(MagicMock())
        issue_id = orphaned_entities_issue_id("100_200_1", "e1")

        with patch.object(repairs.ir, "async_get", return_value=self._registry_holding(issue_id)):
            manager.async_sync([_make_orphan_record()])
        create.assert_called_once()

        # The submit: Home Assistant deleted the card, the key never left the
        # ledger, and the very next sweep still finds it orphaned.
        with patch.object(repairs.ir, "async_get", return_value=self._registry_holding()):
            manager.async_sync([_make_orphan_record()])

        assert create.call_count == 2

    def test_a_live_card_republishes_once_its_count_moves(self, issue_mocks):
        """The card has to describe what Submit will take.

        The confirm re-derives its removal scope, so a count frozen at the
        first raise can have the user approve removing one entity and lose
        two. Re-raising the same id updates the card rather than stacking a
        second one, and a record whose values have not moved still costs the
        registry nothing.
        """
        create, _delete = issue_mocks
        manager = RainPointOrphanedEntityIssues(MagicMock())
        issue_id = orphaned_entities_issue_id("100_200_1", "e1")

        with patch.object(repairs.ir, "async_get", return_value=self._registry_holding(issue_id)):
            manager.async_sync([_make_orphan_record(entity_count=1, leftover=True)])
            manager.async_sync([_make_orphan_record(entity_count=1, leftover=True)])
            assert create.call_count == 1

            manager.async_sync([_make_orphan_record(entity_count=2, leftover=True)])

        assert create.call_count == 2
        assert create.call_args.args[2] == issue_id
        assert create.call_args.kwargs["translation_placeholders"]["entity_count"] == "2"

    def test_an_unreadable_issue_registry_leaves_the_dedup_in_force(self, issue_mocks, caplog):
        """A failed read establishes nothing, and the safe direction for a card
        whose only outcome is a deletion offer is to keep the dedup: a
        suppressed re-raise costs one poll interval, a wrong re-raise stacks a
        second card over a live one."""
        create, _delete = issue_mocks
        manager = RainPointOrphanedEntityIssues(MagicMock())

        with (
            patch.object(repairs.ir, "async_get", side_effect=RuntimeError("registry down")),
            caplog.at_level(logging.DEBUG, logger="custom_components.rainpoint.repairs"),
        ):
            manager.async_sync([_make_orphan_record()])
            manager.async_sync([_make_orphan_record()])

        create.assert_called_once()
        assert [r.getMessage() for r in caplog.records if "Could not reconcile the orphaned entities issue" in r.getMessage()]

    def test_unload_withdraws_every_card_this_instance_raised(self, issue_mocks, caplog):
        """A card raised before a reload survives it, because the issue
        registry is not per config entry. Every structure that could clear it
        afterwards is rebuilt empty, and a key that has left the hub's
        enumeration can never be mentioned by a fresh record, so the stale-set
        sweep has nothing to sweep and the card is stuck for good."""
        _create, delete = issue_mocks
        manager = RainPointOrphanedEntityIssues(MagicMock())
        manager.async_sync([_make_orphan_record(), _make_orphan_record(sensor_key="100_200_2")])
        delete.reset_mock()

        with caplog.at_level(logging.INFO, logger="custom_components.rainpoint.repairs"):
            manager.async_clear_all()

        assert sorted(call.args[2] for call in delete.call_args_list) == [
            orphaned_entities_issue_id("100_200_1", "e1"),
            orphaned_entities_issue_id("100_200_2", "e1"),
        ]
        assert manager._active == set()
        messages = [r.getMessage() for r in caplog.records]
        # Withdrawn, not resolved: nothing about either device changed.
        withdrawn = [r for r in caplog.records if "withdrawing the orphaned entities repair issue" in r.getMessage()]
        assert len(withdrawn) == 2
        assert not [m for m in messages if "lists this device again" in m]
        assert not [m for m in messages if "No leftover entities remain" in m]
        # Warning, unlike the other two clear reasons, and the level is the
        # assertion rather than incidental. A recovery and a removal both end
        # with nothing left to offer; a withdrawal ends with the rows still
        # registered and no surface left to offer them through, so it is the
        # only one of the three that leaves the user with work to do. The line
        # has to say so.
        assert {r.levelno for r in withdrawn} == {logging.WARNING}
        assert all("remove them from the entity registry by hand" in r.getMessage() for r in withdrawn)

    def test_unload_is_a_no_op_when_no_card_is_active(self, issue_mocks):
        """The ordinary unload, which is every unload on a healthy account."""
        _create, delete = issue_mocks
        manager = RainPointOrphanedEntityIssues(MagicMock())

        manager.async_clear_all()

        delete.assert_not_called()

    def test_a_failed_clear_is_swallowed_and_logged(self, issue_mocks, caplog):
        """A registry error on the way out must not raise into a listener."""
        _create, delete = issue_mocks
        delete.side_effect = RuntimeError("registry down")
        manager = RainPointOrphanedEntityIssues(MagicMock())

        with caplog.at_level(logging.DEBUG, logger="custom_components.rainpoint.repairs"):
            manager.async_sync([_make_orphan_record(orphaned=False)])

        assert [r.getMessage() for r in caplog.records if "Failed to delete the orphaned entities" in r.getMessage()]

    def test_a_bluetooth_device_names_no_hub_at_all(self, issue_mocks):
        """A Bluetooth wrapper record carries an empty hub name, which the
        sanitizer would render as "unknown" -- lost state, when the truth is
        that there is no hub. The literal matches the not-reporting card, whose
        own hub line has drawn this distinction since it shipped. Observed on
        hardware: the leftover card for a Bluetooth-paired HTV210B read
        "Hub: unknown", its least useful line."""
        create, _delete = issue_mocks
        manager = RainPointOrphanedEntityIssues(MagicMock())

        manager.async_sync([_make_orphan_record(hub_name="", hub_paired=False)])

        assert create.call_args.kwargs["translation_placeholders"]["hub_name"] == "none"

    def test_a_hub_paired_device_still_names_its_hub(self, issue_mocks):
        """The companion direction, so the branch above cannot be satisfied by
        answering "none" for every device."""
        create, _delete = issue_mocks
        manager = RainPointOrphanedEntityIssues(MagicMock())

        manager.async_sync([_make_orphan_record(hub_name="Hub A", hub_paired=True)])

        assert create.call_args.kwargs["translation_placeholders"]["hub_name"] == "Hub A"

    def test_every_cloud_supplied_placeholder_is_sanitized(self, issue_mocks):
        """Both the card and its confirm dialog render as Markdown, and all
        four of these arrive from the RainPoint payload unvalidated."""
        create, _delete = issue_mocks
        manager = RainPointOrphanedEntityIssues(MagicMock())

        manager.async_sync(
            [
                _make_orphan_record(
                    sub_name="[Click](http://evil.example)\nsecond line",
                    model="<img src=x>www.evil.example",
                    hub_name="a: b/c@d",
                    addr="**1**",
                )
            ]
        )

        placeholders = create.call_args.kwargs["translation_placeholders"]
        for name in ("device_name", "model", "hub_name", "address"):
            value = placeholders[name]
            assert not set(value) & set("`<>[]()|\\*_#:/@")
            assert "\n" not in value
            assert "www." not in value

    def test_a_user_set_device_name_crosses_the_same_sanitizer_boundary(self, issue_mocks):
        """Driven through _raise_issue with a record whose device_name carries
        the hostile content, so the proof is on the path this value actually
        takes rather than on _sanitize_placeholder called directly."""
        create, _delete = issue_mocks
        manager = RainPointOrphanedEntityIssues(MagicMock())

        manager.async_sync([_make_orphan_record(device_name="[Click](http://evil.example)\nsecond line")])

        value = create.call_args.kwargs["translation_placeholders"]["device_name"]
        assert not set(value) & set("`<>[]()|\\*_#:/@")
        assert "\n" not in value
        assert "www." not in value

    def test_a_user_set_name_and_a_cloud_string_with_the_same_content_sanitize_identically(self, issue_mocks):
        """The property that makes "the same boundary" true rather than merely
        claimed: a user-set device_name and RainPoint's own sub_name, carrying
        identical hostile content, must produce the identical placeholder."""
        create, _delete = issue_mocks
        manager = RainPointOrphanedEntityIssues(MagicMock())
        hostile = "[Click](http://evil.example)\nsecond line"

        manager.async_sync([_make_orphan_record(device_name=hostile)])
        from_device_name = create.call_args.kwargs["translation_placeholders"]["device_name"]

        manager.async_sync([_make_orphan_record(sensor_key="100_200_2", device_name=None, sub_name=hostile)])
        from_sub_name = create.call_args.kwargs["translation_placeholders"]["device_name"]

        assert from_device_name == from_sub_name

    def test_a_device_name_longer_than_the_limit_is_capped_at_64_code_points(self, issue_mocks):
        """A name made entirely of a two-byte-in-UTF-8 character proves the cap
        counts Python string positions, not encoded bytes or grapheme
        clusters: 100 of them cap to 64, not to some byte-derived shorter
        count."""
        create, _delete = issue_mocks
        manager = RainPointOrphanedEntityIssues(MagicMock())

        manager.async_sync([_make_orphan_record(device_name="é" * 100)])

        value = create.call_args.kwargs["translation_placeholders"]["device_name"]
        assert len(value) == 64

    def test_no_device_name_and_no_sub_name_renders_the_sanitizer_fallback(self, issue_mocks):
        """Neither None reaches the card as a blank bullet or a Python repr."""
        create, _delete = issue_mocks
        manager = RainPointOrphanedEntityIssues(MagicMock())

        manager.async_sync([_make_orphan_record(device_name=None, sub_name=None)])

        assert create.call_args.kwargs["translation_placeholders"]["device_name"] == "unknown"

    def test_no_device_name_falls_back_to_the_sanitized_sub_name(self, issue_mocks):
        """The ordinary case for a device the owner has never renamed."""
        create, _delete = issue_mocks
        manager = RainPointOrphanedEntityIssues(MagicMock())

        manager.async_sync([_make_orphan_record(device_name=None, sub_name="Front Valve")])

        assert create.call_args.kwargs["translation_placeholders"]["device_name"] == "Front Valve"

    def test_no_log_line_carries_the_device_name(self, issue_mocks, caplog):
        """Log lines on this path carry only the sensor key and integers,
        never a cloud-supplied name or a user-set one."""
        _create, _delete = issue_mocks
        manager = RainPointOrphanedEntityIssues(MagicMock())

        with caplog.at_level(logging.WARNING, logger="custom_components.rainpoint.repairs"):
            manager.async_sync([_make_orphan_record(device_name="Distinctive Device Name Xyzzy")])

        assert "Distinctive Device Name Xyzzy" not in caplog.text


class TestTheHubBulletResolvesLikeTheDeviceBullet:
    """One home, named one way, on both card shapes.

    The Home Assistant hub name first, then RainPoint's own string for it,
    then the sanitizer's fallback. A hub with no pairing at all is not on that
    chain: it renders the literal "none", which is a different statement.
    """

    def test_the_home_assistant_hub_name_wins(self, issue_mocks):
        """The owner renamed the hub, so that is what every other Home
        Assistant surface calls it and what this card calls it too."""
        create, _delete = issue_mocks
        manager = RainPointOrphanedEntityIssues(MagicMock())

        manager.async_sync([_make_orphan_record(hub_device_name="HWG023WBRF-V2 Hub", hub_name="Hub")])

        assert create.call_args.kwargs["translation_placeholders"]["hub_name"] == "HWG023WBRF-V2 Hub"

    def test_no_hub_row_falls_back_to_the_cloud_hub_name(self, issue_mocks):
        """A hub whose device row could not be resolved is still named."""
        create, _delete = issue_mocks
        manager = RainPointOrphanedEntityIssues(MagicMock())

        manager.async_sync([_make_orphan_record(hub_device_name=None, hub_name="Hub")])

        assert create.call_args.kwargs["translation_placeholders"]["hub_name"] == "Hub"

    def test_neither_name_renders_the_sanitizer_fallback(self, issue_mocks):
        """Neither None reaches the card as a blank bullet or a Python repr."""
        create, _delete = issue_mocks
        manager = RainPointOrphanedEntityIssues(MagicMock())

        manager.async_sync([_make_orphan_record(hub_device_name=None, hub_name=None)])

        assert create.call_args.kwargs["translation_placeholders"]["hub_name"] == "unknown"

    def test_a_device_with_no_hub_still_renders_none(self, issue_mocks):
        """The Bluetooth wrapper case is unchanged and must stay that way: the
        card goes on to suggest pairing the device to a hub, so any name at all
        on that line reads as lost state rather than as the truth."""
        create, _delete = issue_mocks
        manager = RainPointOrphanedEntityIssues(MagicMock())

        manager.async_sync([_make_orphan_record(hub_paired=False, hub_device_name="HWG023WBRF-V2 Hub")])

        assert create.call_args.kwargs["translation_placeholders"]["hub_name"] == "none"

    def test_a_user_set_hub_name_crosses_the_same_sanitizer_boundary(self, issue_mocks):
        """A name from the Home Assistant registry is untrusted Markdown on
        exactly the same terms a cloud string is. Nothing about where a value
        came from earns it a laxer boundary."""
        create, _delete = issue_mocks
        manager = RainPointOrphanedEntityIssues(MagicMock())

        manager.async_sync([_make_orphan_record(hub_device_name="[Click](http://evil.example)\nsecond line")])

        value = create.call_args.kwargs["translation_placeholders"]["hub_name"]
        assert not set(value) & set("`<>[]()|\\*_#:/@")
        assert "\n" not in value

    def test_a_user_set_hub_name_and_a_cloud_string_sanitize_identically(self, issue_mocks):
        """The property that makes "the same boundary" true rather than merely
        claimed."""
        create, _delete = issue_mocks
        manager = RainPointOrphanedEntityIssues(MagicMock())
        hostile = "www.evil.example\nsecond line"

        manager.async_sync([_make_orphan_record(hub_device_name=hostile)])
        from_hub_device_name = create.call_args.kwargs["translation_placeholders"]["hub_name"]

        manager.async_sync([_make_orphan_record(sensor_key="100_200_2", hub_device_name=None, hub_name=hostile)])
        from_hub_name = create.call_args.kwargs["translation_placeholders"]["hub_name"]

        assert from_hub_device_name == from_hub_name

    def test_no_log_line_carries_the_hub_name(self, issue_mocks, caplog):
        """Log lines on this path carry only the sensor key and integers."""
        _create, _delete = issue_mocks
        manager = RainPointOrphanedEntityIssues(MagicMock())

        with caplog.at_level(logging.WARNING, logger="custom_components.rainpoint.repairs"):
            manager.async_sync([_make_orphan_record(hub_device_name="Distinctive Hub Name Xyzzy")])

        assert "Distinctive Hub Name Xyzzy" not in caplog.text

    def test_the_still_present_shape_names_its_hub_the_same_way(self, issue_mocks):
        """Both card bodies render {hub_name}, so both get the same treatment."""
        create, _delete = issue_mocks
        manager = RainPointOrphanedEntityIssues(MagicMock())

        manager.async_sync([_make_orphan_record(leftover=True, hub_device_name="HWG023WBRF-V2 Hub", hub_name="Hub")])

        assert create.call_args.kwargs["translation_placeholders"]["hub_name"] == "HWG023WBRF-V2 Hub"


class TestTheCardNamesTheEntitiesItWouldRemove:
    """The still-present card lists the rows behind its count.

    A bare count is the weakest part of a promise about exactly what Submit
    deletes, and the list is what makes it checkable against the user's own
    entity registry.
    """

    def test_the_still_present_card_names_its_rows(self, issue_mocks):
        """The maintainer's own card, with the one row it has."""
        create, _delete = issue_mocks
        manager = RainPointOrphanedEntityIssues(MagicMock())

        manager.async_sync(
            [_make_orphan_record(leftover=True, entity_count=1, entity_ids=("sensor.htv210b_unsupported_htv210b",))]
        )

        placeholders = create.call_args.kwargs["translation_placeholders"]
        assert placeholders["entity_list"] == "  - `sensor.htv210b_unsupported_htv210b`"
        # The count stays visible alongside the names rather than being
        # replaced by them.
        assert placeholders["entity_count"] == "1"

    def test_the_departed_key_card_supplies_no_list_at_all(self, issue_mocks):
        """Its scope comes from this session's adder ledgers, which record
        unique ids rather than entity ids, so it has nothing to name. A
        placeholder its copy does not carry would render nowhere, and one its
        copy carried without a supplier would ship a literal brace."""
        create, _delete = issue_mocks
        manager = RainPointOrphanedEntityIssues(MagicMock())

        manager.async_sync([_make_orphan_record(leftover=False)])

        assert "entity_list" not in create.call_args.kwargs["translation_placeholders"]

    def test_a_still_present_card_with_no_resolvable_ids_still_renders(self, issue_mocks):
        """Nothing to name leaves the count line standing on its own rather
        than leaving the placeholder unsupplied."""
        create, _delete = issue_mocks
        manager = RainPointOrphanedEntityIssues(MagicMock())

        manager.async_sync([_make_orphan_record(leftover=True, entity_ids=())])

        assert create.call_args.kwargs["translation_placeholders"]["entity_list"] == ""

    def test_the_confirm_dialog_reads_back_the_same_list_the_card_published(self, issue_mocks):
        """One supplier for both surfaces, exactly as every other placeholder
        on this card has: the dialog reads the raised issue rather than
        building a second list that could disagree with the first."""
        create, _delete = issue_mocks
        manager = RainPointOrphanedEntityIssues(MagicMock())
        manager.async_sync([_make_orphan_record(leftover=True, entity_ids=("sensor.left_over_row",))])
        published = create.call_args.kwargs["translation_placeholders"]

        issue = SimpleNamespace(translation_placeholders=published)
        flow = RainPointOrphanedEntitiesRepairFlow({"entry_id": "e1", "sensor_key": "100_200_1", "leftover": True})
        flow.hass = MagicMock()
        with patch.object(repairs.ir, "async_get", return_value=SimpleNamespace(async_get_issue=lambda *_: issue)):
            assert flow._description_placeholders(flow._read_issue())["entity_list"] == "  - `sensor.left_over_row`"

    def test_a_card_whose_named_rows_change_is_republished(self, issue_mocks):
        """The list is part of what the user is being asked to approve, so a
        card that gains a row has to say so rather than keeping the list it was
        first raised with."""
        create, _delete = issue_mocks
        manager = RainPointOrphanedEntityIssues(MagicMock())
        registry = SimpleNamespace(async_get_issue=lambda domain, issue_id: object())

        with patch.object(repairs.ir, "async_get", return_value=registry):
            manager.async_sync([_make_orphan_record(leftover=True, entity_count=1, entity_ids=("sensor.first_row",))])
            manager.async_sync(
                [_make_orphan_record(leftover=True, entity_count=2, entity_ids=("sensor.first_row", "sensor.second_row"))]
            )

        assert create.call_count == 2
        assert "sensor.second_row" in create.call_args.kwargs["translation_placeholders"]["entity_list"]

    def test_no_log_line_carries_the_entity_list(self, issue_mocks, caplog):
        """The log discipline on this path is keys and integer counts. A card's
        rendered Markdown is neither."""
        _create, _delete = issue_mocks
        manager = RainPointOrphanedEntityIssues(MagicMock())

        with caplog.at_level(logging.WARNING, logger="custom_components.rainpoint.repairs"):
            manager.async_sync([_make_orphan_record(leftover=True, entity_ids=("sensor.distinctive_xyzzy_row",))])

        assert "distinctive_xyzzy_row" not in caplog.text


class _FakeIssue:
    """An issue registry entry carrying only what the confirm dialog reads."""

    def __init__(self, translation_placeholders, data=None):
        """Hold the placeholders the raised card supplied, and its data dict."""
        self.translation_placeholders = translation_placeholders
        self.data = data


def _flow_hass(remover=None, *, with_entry=True, with_remover=True):
    """Build a hass stand-in whose entry store holds (or lacks) a remover."""
    entry_store = {}
    if with_remover:
        entry_store["orphan_entity_remover"] = remover
    data = {DOMAIN: {"e1": entry_store}} if with_entry else {}
    return SimpleNamespace(data=data)


def _make_flow(hass, sensor_key="100_200_1", entry_id="e1", *, leftover=None):
    """Construct the flow the way async_create_fix_flow does, then bind hass.

    ``leftover`` mirrors the marker _raise_issue stamps into the issue's data
    dict, and None is the departed-key card, whose data dict carries no such
    key at all rather than carrying it set to False.
    """
    data = {"entry_id": entry_id, "sensor_key": sensor_key}
    if leftover is not None:
        data["leftover"] = leftover
    flow = RainPointOrphanedEntitiesRepairFlow(data)
    flow.hass = hass
    return flow


def _recording_remover(result=0):
    """Return (calls, remover), recording the key, the shape and the offer.

    All three are recorded rather than ignored because all three are arguments
    the flow has to supply itself. The executor cannot recover the shape from
    the key or from anything it derives, and getting it wrong deletes a live
    device's whole entity set; it cannot recover the offer at all, because the
    only record of what the user was shown is the one this flow took when it
    showed it.
    """
    calls: list[tuple[str, bool, object]] = []

    def _remover(sensor_key, *, leftover_shape=True, offered_pairs=None):
        """Record one removal request exactly as the flow made it."""
        calls.append((sensor_key, leftover_shape, offered_pairs))
        return result

    return calls, _remover


class TestRainPointOrphanedEntitiesRepairFlow:
    """The confirmation dialog, which is the only path to a removal."""

    @pytest.mark.asyncio
    async def test_create_fix_flow_returns_this_flow(self):
        """The repairs platform hook, and the integration's first."""
        flow = await async_create_fix_flow(MagicMock(), "some_id", {"entry_id": "e1", "sensor_key": "100_200_1"})
        assert isinstance(flow, RainPointOrphanedEntitiesRepairFlow)

    @pytest.mark.asyncio
    async def test_a_flow_built_with_no_data_still_works(self):
        """Home Assistant types the issue data as optional, so None must not
        raise on the way into a dialog the user has already opened."""
        flow = await async_create_fix_flow(MagicMock(), "some_id", None)
        flow.hass = _flow_hass()
        assert (await flow.async_step_confirm({}))["type"] == "create_entry"

    @pytest.mark.asyncio
    async def test_opening_the_card_shows_a_form_and_removes_nothing(self):
        """The irreversible step is always a submit, never an open."""
        calls, remover = _recording_remover()
        flow = _make_flow(_flow_hass(remover))

        step = await flow.async_step_init()

        assert step["type"] == "form"
        assert step["step_id"] == "confirm"
        assert calls == []

    @pytest.mark.asyncio
    async def test_submitting_calls_the_remover_once_with_the_key_from_the_data(self):
        """The key comes from the issue data, never from parsing the issue id."""
        calls, remover = _recording_remover()
        flow = _make_flow(_flow_hass(remover))

        result = await flow.async_step_confirm({})

        # No form was shown, so the flow has no snapshot to hand over and the
        # executor falls back to its own re-derivation.
        assert calls == [("100_200_1", False, None)]
        assert result["type"] == "create_entry"

    @pytest.mark.asyncio
    async def test_the_card_s_own_shape_goes_to_the_remover_rather_than_being_inferred(self):
        """The still-present card names its shape, and the departed one names
        its own by carrying no marker.

        The executor's two scopes are not variations of one another: one takes
        the whole of the session's ledger for the key and releases the device
        row, the other takes only the rows that are still dead at the moment of
        the confirm. Left to infer the shape, an executor reads a still-present
        card whose rows all recovered as the departed one, and deletes every
        entity of a device that is on the account and reporting.
        """
        leftover_calls, leftover_remover = _recording_remover()
        departed_calls, departed_remover = _recording_remover()

        await _make_flow(_flow_hass(leftover_remover), leftover=True).async_step_confirm({})
        await _make_flow(_flow_hass(departed_remover)).async_step_confirm({})

        assert leftover_calls == [("100_200_1", True, None)]
        assert departed_calls == [("100_200_1", False, None)]

    @pytest.mark.asyncio
    async def test_the_flow_never_deletes_the_issue_itself(self, issue_mocks):
        """Home Assistant's flow manager already deletes the issue on a
        non-abort result, so deleting here would be a double delete."""
        _create, delete = issue_mocks
        flow = _make_flow(_flow_hass(_recording_remover(2)[1]))

        await flow.async_step_init()
        await flow.async_step_confirm({})

        delete.assert_not_called()

    @pytest.mark.asyncio
    async def test_a_torn_down_entry_store_leaves_the_step_returning_normally(self, caplog):
        """A flow submitted after its config entry unloaded finds nothing."""
        flow = _make_flow(_flow_hass(with_entry=False))

        with caplog.at_level(logging.DEBUG, logger="custom_components.rainpoint.repairs"):
            result = await flow.async_step_confirm({})

        assert result["type"] == "create_entry"
        assert [r.getMessage() for r in caplog.records if "No orphaned entity remover" in r.getMessage()]

    @pytest.mark.asyncio
    async def test_a_missing_remover_leaves_the_step_returning_normally(self):
        """The entry store exists but nothing published a remover into it."""
        flow = _make_flow(_flow_hass(with_remover=False))
        assert (await flow.async_step_confirm({}))["type"] == "create_entry"

    @pytest.mark.asyncio
    async def test_a_remover_that_raises_does_not_break_the_dialog(self, caplog):
        """An exception here surfaces to the user as a broken repair dialog."""

        def _boom(key, *, leftover_shape=True):
            """Fail the way a torn-down registry would."""
            raise RuntimeError("registry down")

        flow = _make_flow(_flow_hass(_boom))

        with caplog.at_level(logging.DEBUG, logger="custom_components.rainpoint.repairs"):
            result = await flow.async_step_confirm({})

        assert result["type"] == "create_entry"
        assert [r.getMessage() for r in caplog.records if "failed" in r.getMessage()]

    @pytest.mark.asyncio
    async def test_the_dialog_reuses_the_raised_card_s_own_placeholders(self):
        """Building a second dict would let the card and the dialog drift, and
        would add a second place a value could reach copy unsanitized."""
        supplied = {"sub_name": "Front Valve", "entity_count": "2"}
        registry = MagicMock()
        registry.async_get_issue.return_value = _FakeIssue(supplied)
        flow = _make_flow(_flow_hass(_recording_remover()[1]))

        with patch.object(repairs.ir, "async_get", return_value=registry):
            step = await flow.async_step_init()

        assert step["description_placeholders"] == supplied
        registry.async_get_issue.assert_called_once_with(DOMAIN, orphaned_entities_issue_id("100_200_1", "e1"))

    @pytest.mark.asyncio
    async def test_an_absent_issue_yields_no_placeholders(self):
        """A card already dismissed elsewhere leaves the dialog unadorned
        rather than raising out of a flow step."""
        registry = MagicMock()
        registry.async_get_issue.return_value = None
        flow = _make_flow(_flow_hass(_recording_remover()[1]))

        with patch.object(repairs.ir, "async_get", return_value=registry):
            step = await flow.async_step_init()

        assert step["description_placeholders"] is None

    @pytest.mark.asyncio
    async def test_an_unreadable_registry_yields_no_placeholders(self, caplog):
        """Same outcome by the other route, because a raising flow step is the
        one thing a confirmation dialog must never do."""
        flow = _make_flow(_flow_hass(_recording_remover()[1]))

        with (
            patch.object(repairs.ir, "async_get", side_effect=RuntimeError("no registry")),
            caplog.at_level(logging.DEBUG, logger="custom_components.rainpoint.repairs"),
        ):
            step = await flow.async_step_init()

        assert step["description_placeholders"] is None
        assert [r.getMessage() for r in caplog.records if "Could not read the orphaned entities issue" in r.getMessage()]

    @pytest.mark.asyncio
    async def test_an_issue_that_will_not_yield_its_placeholders_still_shows_a_form(self, caplog):
        """The registry answered, the entry it answered with did not.

        A separate guard from the registry read above, because it fails at a
        different point: the issue is in hand, so the snapshot the dialog takes
        from it is unaffected, and only the text degrades.
        """

        class _UnreadableIssue:
            """An issue entry that raises the moment its placeholders are read."""

            def __init__(self):
                """Carry an ordinary offer, so only the text can fail."""
                self.data = {"leftover_pairs": (("sensor", "rainpoint_100_200_1_left_over"),)}

            @property
            def translation_placeholders(self):
                """Raise, the way a registry entry mid-migration might."""
                raise RuntimeError("no placeholders")

        registry = MagicMock()
        registry.async_get_issue.return_value = _UnreadableIssue()
        flow = _make_flow(_flow_hass(_recording_remover()[1]), leftover=True)

        with (
            patch.object(repairs.ir, "async_get", return_value=registry),
            caplog.at_level(logging.DEBUG, logger="custom_components.rainpoint.repairs"),
        ):
            step = await flow.async_step_init()

        assert step["description_placeholders"] is None
        assert [r.getMessage() for r in caplog.records if "issue placeholders" in r.getMessage()]
        # The offer was still read, so the Submit behind this dialog is still
        # held to it.
        assert flow._offered_pairs == frozenset({("sensor", "rainpoint_100_200_1_left_over")})


PAIR = ("sensor", "rainpoint_100_200_1_left_over")
SECOND_PAIR = ("number", "rainpoint_100_200_1_duration")


class TestTheOfferTheDialogWasShown:
    """What the card is offering, snapshotted when the dialog is shown.

    The re-derivation at Submit answers a row that recovered while the dialog
    sat open. It cannot answer a row that went dead while it sat open, because
    the sweep behind the card keeps running: a second row can finish its window
    and enter the re-derived scope under a dialog whose text still describes one
    row. This snapshot is the ceiling that answers that one.
    """

    def test_the_offer_is_read_back_as_pairs(self):
        """The shape the removal is keyed on, and no other."""
        assert _snapshot_offered_pairs(_FakeIssue({}, {"leftover_pairs": (PAIR, SECOND_PAIR)})) == frozenset({PAIR, SECOND_PAIR})

    def test_a_list_of_lists_reads_back_the_same(self):
        """Nothing here depends on the offer still being tuples.

        The value travels through Home Assistant's issue registry rather than
        straight from the caller, so a round trip that turned every tuple into
        a list must not silently empty the ceiling and hand the confirm an
        unconstrained scope.
        """
        assert _snapshot_offered_pairs(_FakeIssue({}, {"leftover_pairs": [list(PAIR)]})) == frozenset({PAIR})

    def test_a_departed_key_card_offers_no_pairs_at_all(self):
        """That shape's scope comes from the session's ledgers, so there is
        nothing to hold it to and None says exactly that."""
        assert _snapshot_offered_pairs(_FakeIssue({}, {"entry_id": "e1"})) is None

    def test_no_issue_yields_no_ceiling_rather_than_an_empty_one(self):
        """A card already dismissed elsewhere leaves the confirm to its own
        re-derivation, which is what this path did before the snapshot."""
        assert _snapshot_offered_pairs(None) is None

    def test_an_unreadable_data_dict_yields_an_empty_ceiling_not_a_missing_one(self, caplog):
        """A card whose offer cannot be read is not a card with no offer.

        The offer exists and is unknown, so Submit takes nothing rather than
        falling back to a re-derivation no dialog was ever held to. Guarded
        rather than allowed to raise either way: this runs inside the step that
        shows the dialog.
        """

        class _Exploding:
            """A data dict that raises the moment it is asked for a key."""

            def get(self, _key):
                """Raise the way a half-migrated registry entry might."""
                raise RuntimeError("no data")

        with caplog.at_level(logging.DEBUG, logger="custom_components.rainpoint.repairs"):
            assert _snapshot_offered_pairs(_FakeIssue({}, _Exploding())) == frozenset()

        assert [r.getMessage() for r in caplog.records if "what the orphaned entities card is offering" in r.getMessage()]

    def test_an_issue_that_raises_on_its_data_attribute_is_caught_too(self):
        """The attribute read sits inside the guard, not outside it.

        ``getattr``'s default covers a missing attribute and nothing else, so an
        entry that raises on ``data`` would propagate out of the flow step that
        shows the dialog and leave the user with a broken one rather than a
        degraded one.
        """

        class _RaisingIssue:
            """An issue entry whose data attribute raises when it is read."""

            def __init__(self):
                """Carry ordinary placeholders, so only the offer can fail."""
                self.translation_placeholders = {}

            @property
            def data(self):
                """Raise the way a half-migrated registry entry might."""
                raise RuntimeError("no data attribute")

        assert _snapshot_offered_pairs(_RaisingIssue()) == frozenset()

    def test_a_first_member_that_cannot_be_read_leaves_an_empty_ceiling(self):
        """The narrowing holds at its own boundary.

        A read that fails before anything survives it leaves a ceiling of
        nothing, which is the same direction as dropping one malformed member
        out of several, rather than the no-ceiling answer a missing offer gets.
        """
        assert _snapshot_offered_pairs(_FakeIssue({}, {"leftover_pairs": 42})) == frozenset()
        assert _snapshot_offered_pairs(_FakeIssue({}, {"leftover_pairs": ["not-a-pair", PAIR]})) == frozenset()

    def test_a_pair_that_is_not_a_pair_stops_the_read_without_losing_the_rest(self, caplog):
        """A malformed member narrows the ceiling rather than voiding it.

        Narrower is the safe direction on a surface whose Submit deletes
        recorder history: a genuinely dead row that falls out here waits for the
        next card, while a repaired one would be a row the user never approved.
        """
        with caplog.at_level(logging.DEBUG, logger="custom_components.rainpoint.repairs"):
            snapshot = _snapshot_offered_pairs(_FakeIssue({}, {"leftover_pairs": [PAIR, "not-a-pair-at-all"]}))

        assert snapshot == frozenset({PAIR})
        assert [r.getMessage() for r in caplog.records if "one of the pairs" in r.getMessage()]

    def test_a_non_string_member_is_dropped(self):
        """The value is plain data by the time it comes back, so the type is
        checked rather than assumed."""
        assert _snapshot_offered_pairs(_FakeIssue({}, {"leftover_pairs": [PAIR, ("sensor", None)]})) == frozenset({PAIR})

    @pytest.mark.asyncio
    async def test_showing_the_dialog_snapshots_the_offer_and_submitting_hands_it_over(self):
        """The two halves in one flow, in the order a user drives them."""
        calls, remover = _recording_remover()
        registry = MagicMock()
        registry.async_get_issue.return_value = _FakeIssue({"entity_count": "1"}, {"leftover_pairs": (PAIR,)})
        flow = _make_flow(_flow_hass(remover), leftover=True)

        with patch.object(repairs.ir, "async_get", return_value=registry):
            await flow.async_step_init()
            await flow.async_step_confirm({})

        assert calls == [("100_200_1", True, frozenset({PAIR}))]

    @pytest.mark.asyncio
    async def test_a_card_that_gains_a_row_under_an_open_dialog_hands_over_the_old_offer(self):
        """The defect this exists for, at the seam where it happens.

        The card is republished with a second row while the dialog sits open.
        What reaches the executor is still the one row the user was shown, so
        the row that arrived after they read it cannot be taken by that Submit.
        """
        calls, remover = _recording_remover()
        registry = MagicMock()
        registry.async_get_issue.return_value = _FakeIssue({"entity_count": "1"}, {"leftover_pairs": (PAIR,)})
        flow = _make_flow(_flow_hass(remover), leftover=True)

        with patch.object(repairs.ir, "async_get", return_value=registry):
            await flow.async_step_init()
            # The sweep runs on under the open dialog and republishes the card.
            registry.async_get_issue.return_value = _FakeIssue({"entity_count": "2"}, {"leftover_pairs": (PAIR, SECOND_PAIR)})
            await flow.async_step_confirm({})

        assert calls == [("100_200_1", True, frozenset({PAIR}))]
        assert SECOND_PAIR not in calls[0][2]

    @pytest.mark.asyncio
    async def test_a_departed_key_confirm_carries_no_ceiling(self):
        """That shape ignores the offer entirely, and passing an empty set
        rather than None would read as a ceiling of nothing."""
        calls, remover = _recording_remover()
        registry = MagicMock()
        registry.async_get_issue.return_value = _FakeIssue({"entity_count": "1"}, {"entry_id": "e1"})
        flow = _make_flow(_flow_hass(remover))

        with patch.object(repairs.ir, "async_get", return_value=registry):
            await flow.async_step_init()
            await flow.async_step_confirm({})

        assert calls == [("100_200_1", False, None)]
