"""Tests for the shipped translation copy (translations/en.json).

This file has no source module of its own. Its subject is the contract between
user-facing copy and the code that renders it: a placeholder name present in
one and absent from the other ships a Repairs card with a literal brace or a
blank, and nothing else in the suite would catch it.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from unittest.mock import MagicMock, patch

import custom_components.rainpoint as rainpoint_pkg
from custom_components.rainpoint import generic_control, repairs
from custom_components.rainpoint.api import RainPointApiError
from custom_components.rainpoint.const import (
    GENERIC_CONTROL_ISSUE_ID_PREFIX,
    LEFTOVER_ENTITIES_TRANSLATION_KEY,
    PUSH_HUB_IDENTITY_ISSUE_ID,
)
from custom_components.rainpoint.repairs import (
    HubConnectivityRecord,
    OrphanedEntitiesRecord,
    RainPointHubConnectivityIssues,
    RainPointOrphanedEntityIssues,
    RainPointSilentDeviceIssues,
    SilentDeviceRecord,
    async_sync_push_hub_identity_issue,
)

_PLACEHOLDER_RE = re.compile(r"{(\w+)}")

_MANIFEST_PATH = Path(rainpoint_pkg.__file__).parent / "manifest.json"

_ACCEPTED_INTEGRATION_TYPES = {
    "entity",
    "device",
    "hardware",
    "helper",
    "hub",
    "service",
    "system",
    "virtual",
}

_SETTLED_INTEGRATION_TYPE = "service"


def _load_en_translations() -> dict:
    """Parse en.json, resolved through the installed package.

    Going via the package's __file__ rather than a relative traversal from the
    tests directory keeps the test correct however the suite is invoked.
    """
    path = Path(rainpoint_pkg.__file__).parent / "translations" / "en.json"
    return json.loads(path.read_text(encoding="utf-8"))


def _placeholders_in(text: str) -> set[str]:
    """The set of brace-delimited placeholder names appearing in a copy string."""
    return set(_PLACEHOLDER_RE.findall(text))


def _not_reporting_entry() -> dict:
    """The issues entry whose copy the code renders placeholders into."""
    return _load_en_translations()["issues"]["device_not_reporting"]


def _hub_disconnected_entry() -> dict:
    """The issues entry whose copy RainPointHubConnectivityIssues renders into."""
    return _load_en_translations()["issues"]["hub_disconnected"]


def _orphaned_entities_entry() -> dict:
    """The issues entry whose copy RainPointOrphanedEntityIssues renders into."""
    return _load_en_translations()["issues"]["orphaned_device_entities"]


def _leftover_entities_entry() -> dict:
    """The issues entry the still-present shape of that same card renders into."""
    return _load_en_translations()["issues"][LEFTOVER_ENTITIES_TRANSLATION_KEY]


def _generic_control_failed_entry() -> dict:
    """The issues entry whose copy _create_command_failed_issue renders into."""
    return _load_en_translations()["issues"][GENERIC_CONTROL_ISSUE_ID_PREFIX]


def _push_hub_identity_entry() -> dict:
    """The issues entry whose copy async_sync_push_hub_identity_issue renders into."""
    return _load_en_translations()["issues"]["push_hub_identity_unresolved"]


class TestIssueCopyStructure:
    """Every Repairs issue must have copy for both halves of its card."""

    def test_file_parses_and_has_issues_block(self):
        """Invalid JSON here breaks every Repairs card at once."""
        data = _load_en_translations()
        assert isinstance(data, dict)
        assert isinstance(data.get("issues"), dict)
        assert data["issues"], "issues block must not be empty"

    def test_every_issue_entry_has_a_non_empty_title(self):
        """Iterated rather than named, so a fifth issue added later is covered
        without editing this test."""
        for key, entry in _load_en_translations()["issues"].items():
            value = entry.get("title")
            assert isinstance(value, str), f"issues.{key}.title must be a string"
            assert value.strip(), f"issues.{key}.title must not be empty"

    def test_every_issue_entry_has_a_non_empty_body_wherever_its_body_lives(self):
        """A card's body is its description, or its confirm step's description
        when it is fixable. Both are the same thing to a user, so both are
        required; which key holds it is decided by the test below."""
        for key, entry in _load_en_translations()["issues"].items():
            if "fix_flow" in entry:
                value = entry["fix_flow"]["step"]["confirm"].get("description")
                where = f"issues.{key}.fix_flow.step.confirm.description"
            else:
                value = entry.get("description")
                where = f"issues.{key}.description"
            assert isinstance(value, str), f"{where} must be a string"
            assert value.strip(), f"{where} must not be empty"

    def test_a_fixable_issue_carries_no_description_of_its_own(self):
        """hassfest marks description and fix_flow mutually exclusive, and it
        is the only gate that checks: nothing in the local suite, ruff or the
        HACS validation reads this file against Home Assistant's schema, so a
        fixable issue that also carries a description passes everything here
        and fails CI. A fixable card renders its body from the flow's step, so
        a description there would also be copy no user is ever shown."""
        for key, entry in _load_en_translations()["issues"].items():
            if "fix_flow" in entry:
                assert "description" not in entry, (
                    f"issues.{key} is fixable, so its body belongs in fix_flow.step.confirm.description"
                )

    def test_no_issue_copy_ships_a_link_of_its_own(self):
        """The placeholders are sanitized so cloud text cannot plant a link in
        a Repairs card. That protection is worth nothing if the copy the
        integration itself ships carries one, so neither Markdown link syntax
        nor the bare host form a renderer autolinks may appear anywhere in the
        issues block, fix-flow copy included."""
        blob = json.dumps(_load_en_translations()["issues"])
        assert "](" not in blob
        assert "www." not in blob
        assert "http" not in blob


class TestNotReportingIssuePlaceholderParity:
    """The copy's placeholders and the ones _raise_issue supplies must match."""

    @staticmethod
    def _supplied_placeholders() -> dict[str, str]:
        """Raise a real issue and capture what the code passed to the registry."""
        manager = RainPointSilentDeviceIssues(MagicMock())
        record = SilentDeviceRecord(
            hid=100,
            mid=200,
            addr=1,
            model="HTV210B",
            hub_name="Hub1",
            missed_polls=3,
            silent=True,
        )
        with patch.object(repairs.ir, "async_create_issue") as create:
            manager.async_sync([record])
        create.assert_called_once()
        return create.call_args.kwargs["translation_placeholders"]

    def test_copy_placeholders_match_the_ones_the_code_supplies(self):
        """A mismatch in either direction ships a card with a literal brace or a blank."""
        entry = _not_reporting_entry()
        in_copy = _placeholders_in(entry["title"]) | _placeholders_in(entry["description"])
        assert in_copy == set(self._supplied_placeholders())

    def test_copy_renders_with_the_supplied_values_and_leaves_no_brace(self):
        """Proves the parity holds under an actual render, not just by name comparison."""
        entry = _not_reporting_entry()
        supplied = self._supplied_placeholders()
        rendered = entry["description"].format(**supplied)
        assert "{" not in rendered
        assert "}" not in rendered
        assert entry["title"].format(**supplied)


class TestHubDisconnectedIssuePlaceholderParity:
    """The copy's placeholders and the ones _raise_issue supplies must match."""

    @staticmethod
    def _supplied_placeholders() -> dict[str, str]:
        """Raise a real issue and capture what the code passed to the registry."""
        manager = RainPointHubConnectivityIssues(MagicMock())
        record = HubConnectivityRecord(
            hid=100,
            mid=200,
            hub_name="Hub1",
            disconnected=True,
            offline_seconds=194,
            model="HWG023WBRF-V2",
        )
        with patch.object(repairs.ir, "async_create_issue") as create:
            manager.async_sync([record])
        create.assert_called_once()
        return create.call_args.kwargs["translation_placeholders"]

    def test_copy_placeholders_match_the_ones_the_code_supplies(self):
        """A mismatch in either direction ships a card with a literal brace or a blank."""
        entry = _hub_disconnected_entry()
        in_copy = _placeholders_in(entry["title"]) | _placeholders_in(entry["description"])
        assert in_copy == set(self._supplied_placeholders())

    def test_copy_renders_with_the_supplied_values_and_leaves_no_brace(self):
        """Proves the parity holds under an actual render, not just by name comparison."""
        entry = _hub_disconnected_entry()
        supplied = self._supplied_placeholders()
        rendered = entry["description"].format(**supplied)
        assert "{" not in rendered
        assert "}" not in rendered
        assert entry["title"].format(**supplied)


def _orphan_record(**overrides) -> OrphanedEntitiesRecord:
    """One orphaned-entities record, with only what a test cares about named.

    The five constructions in this module differed by model, sub_name,
    entity_count, leftover and device_name and agreed on everything else, so a
    field added to the record meant five edits and five chances to leave one
    behind saying something different from its neighbours. Mirrors
    _make_orphan_record in tests/test_repairs.py.
    """
    fields = {
        "entry_id": "e1",
        "sensor_key": "100_200_1",
        "addr": 1,
        "model": "HTV245FRF",
        "sub_name": "Front Valve",
        "hub_name": "Hub A",
        "entity_count": 2,
        "missed_polls": 30,
        "orphaned": True,
    }
    fields.update(overrides)
    return OrphanedEntitiesRecord(**fields)


class TestOrphanedEntitiesIssuePlaceholderParity:
    """The copy's placeholders and the ones _raise_issue supplies must match.

    Reads the confirm step rather than a description, which its two siblings
    do not: this is the integration's only fixable issue, and a fixable issue
    may not carry a description at all. Its whole body is the flow's step, fed
    by the placeholder dict the card supplied, which the flow reads back off
    the raised issue.
    """

    @staticmethod
    def _supplied_placeholders() -> dict[str, str]:
        """Raise a real issue and capture what the code passed to the registry."""
        manager = RainPointOrphanedEntityIssues(MagicMock())
        record = _orphan_record()
        with patch.object(repairs.ir, "async_create_issue") as create:
            manager.async_sync([record])
        create.assert_called_once()
        return create.call_args.kwargs["translation_placeholders"]

    def test_copy_placeholders_match_the_ones_the_code_supplies(self):
        """A mismatch in either direction ships a card with a literal brace or a blank.

        Equality, not a subset, and it spans both halves: every name the code
        supplies has to be shown somewhere, and every name shown has to have a
        supplier. The card's own title carries none of them, so the confirm
        step is where they all have to land.
        """
        entry = _orphaned_entities_entry()
        confirm = entry["fix_flow"]["step"]["confirm"]
        in_copy = _placeholders_in(entry["title"]) | _placeholders_in(confirm["title"]) | _placeholders_in(confirm["description"])
        assert in_copy == set(self._supplied_placeholders())

    def test_copy_renders_with_the_supplied_values_and_leaves_no_brace(self):
        """Proves the parity holds under an actual render, not just by name comparison."""
        entry = _orphaned_entities_entry()
        confirm = entry["fix_flow"]["step"]["confirm"]
        supplied = self._supplied_placeholders()
        rendered = confirm["description"].format(**supplied)
        assert "{" not in rendered
        assert "}" not in rendered
        assert confirm["title"].format(**supplied)
        assert entry["title"].format(**supplied)

    def test_a_device_name_on_the_record_renders_ahead_of_the_cloud_sub_name(self):
        """The ordinary case: an owner-renamed device names the card by that
        name rather than by the cloud's own name for it, and the copy still
        renders clean with no unresolved brace."""
        manager = RainPointOrphanedEntityIssues(MagicMock())
        record = _orphan_record(sub_name="HTV245FRF", device_name="Front Lawn Valve")
        with patch.object(repairs.ir, "async_create_issue") as create:
            manager.async_sync([record])

        placeholders = create.call_args.kwargs["translation_placeholders"]
        assert placeholders["device_name"] == "Front Lawn Valve"
        confirm = _orphaned_entities_entry()["fix_flow"]["step"]["confirm"]
        rendered = confirm["description"].format(**placeholders)
        assert "{" not in rendered
        assert "}" not in rendered


class TestLeftoverEntitiesIssuePlaceholderParity:
    """The second shape of the fixable card renders from its own copy.

    Mirrors TestOrphanedEntitiesIssuePlaceholderParity above, against the entry
    a record whose leftover flag is set selects. The two shapes share one issue
    id and one placeholder supplier but not one body, so a placeholder added to
    the supplier for either of them has to appear in both copies or one card
    ships a blank.
    """

    @staticmethod
    def _supplied_placeholders() -> dict[str, str]:
        """Raise a real leftover-shaped issue and capture what the code passed."""
        manager = RainPointOrphanedEntityIssues(MagicMock())
        record = _orphan_record(model="HTV210B", entity_count=1, leftover=True)
        with patch.object(repairs.ir, "async_create_issue") as create:
            manager.async_sync([record])
        create.assert_called_once()
        return create.call_args.kwargs["translation_placeholders"]

    def test_the_leftover_record_selects_the_leftover_copy(self):
        """The card has to describe the shape that raised it: this one says the
        device is still on the account, its sibling says it is gone."""
        manager = RainPointOrphanedEntityIssues(MagicMock())
        record = _orphan_record(model="HTV210B", entity_count=1, leftover=True)
        with patch.object(repairs.ir, "async_create_issue") as create:
            manager.async_sync([record])

        assert create.call_args.kwargs["translation_key"] == LEFTOVER_ENTITIES_TRANSLATION_KEY
        assert LEFTOVER_ENTITIES_TRANSLATION_KEY in _load_en_translations()["issues"]

    def test_copy_placeholders_match_the_ones_the_code_supplies(self):
        """A mismatch in either direction ships a card with a literal brace or a blank."""
        entry = _leftover_entities_entry()
        confirm = entry["fix_flow"]["step"]["confirm"]
        in_copy = _placeholders_in(entry["title"]) | _placeholders_in(confirm["title"]) | _placeholders_in(confirm["description"])
        assert in_copy == set(self._supplied_placeholders())

    def test_copy_renders_with_the_supplied_values_and_leaves_no_brace(self):
        """Proves the parity holds under an actual render, not just by name comparison."""
        entry = _leftover_entities_entry()
        confirm = entry["fix_flow"]["step"]["confirm"]
        supplied = self._supplied_placeholders()
        rendered = confirm["description"].format(**supplied)
        assert "{" not in rendered
        assert "}" not in rendered
        assert confirm["title"].format(**supplied)
        assert entry["title"].format(**supplied)

    def test_the_copy_says_a_missing_reading_comes_back_on_its_own(self):
        """The disclosure line is the mitigation for the one class of false
        positive the technical gates reduce but cannot eliminate, so it may not
        be edited away."""
        body = _leftover_entities_entry()["fix_flow"]["step"]["confirm"]["description"]

        assert "come back on its own" in body
        assert "Cancel" in body
        assert "cannot be undone" in body

    def test_a_device_name_on_the_record_renders_ahead_of_the_cloud_sub_name(self):
        """The still-present shape gets the same naming treatment: an
        owner-renamed device names the card by that name, and the copy still
        renders clean with no unresolved brace."""
        manager = RainPointOrphanedEntityIssues(MagicMock())
        record = _orphan_record(
            model="HTV210B", sub_name="HTV210B", entity_count=1, leftover=True, device_name="HTV210B Hub Paired"
        )
        with patch.object(repairs.ir, "async_create_issue") as create:
            manager.async_sync([record])

        placeholders = create.call_args.kwargs["translation_placeholders"]
        assert placeholders["device_name"] == "HTV210B Hub Paired"
        confirm = _leftover_entities_entry()["fix_flow"]["step"]["confirm"]
        rendered = confirm["description"].format(**placeholders)
        assert "{" not in rendered
        assert "}" not in rendered


class TestPushHubIdentityIssueCopy:
    """The push-hub-identity card's copy is proven placeholder-free, not parity-matched.

    Its three siblings above prove a placeholder set matches between the copy
    and the code. This card carries none by design: the message names no
    specific missing field, so the executable claim here is the opposite one:
    that there is nothing to match, on both the copy side and the code side.
    """

    def test_the_issue_id_constant_has_an_issues_entry(self):
        """The constant's value doubles as the issue id, the translation_key
        and the en.json key. A mismatch on any of the three ships a blank card."""
        data = _load_en_translations()
        assert PUSH_HUB_IDENTITY_ISSUE_ID in data["issues"]

    def test_the_copy_carries_no_placeholders_and_the_code_supplies_none(self):
        """Both halves of the contract in one test.

        Asserting only the copy half would pass while the code passed a dict
        the card silently drops; asserting only the code half would pass while
        the copy carried a brace nothing ever fills.
        """
        entry = _push_hub_identity_entry()
        in_copy = _placeholders_in(entry["title"]) | _placeholders_in(entry["description"])
        assert in_copy == set()

        with patch.object(repairs.ir, "async_create_issue") as create:
            async_sync_push_hub_identity_issue(MagicMock(), "some_entry_id", unresolved=True)
        create.assert_called_once()
        assert "translation_placeholders" not in create.call_args.kwargs

    def test_the_copy_names_no_cloud_record_key(self):
        """The card describes the outcome, never which cloud field was missing.

        The card is Markdown-rendered, so a message naming a specific cloud
        field would be the first surface in this integration to put vendor
        vocabulary in front of a user who cannot act on the distinction. Kept
        to the two key names _resolve_hub_identity reads, rather than a
        general-purpose secret scanner.
        """
        entry = _push_hub_identity_entry()
        body = (entry["title"] + entry["description"]).lower()
        assert "devicename" not in body
        assert "productkey" not in body

    def test_the_copy_closes_with_the_same_remedy_bullets_as_push_channel_down(self):
        """The same two remedy bullets push_channel_down uses, derived
        from the file rather than hard-coded so the two cards stay in step if
        either is reworded later."""
        sibling = _load_en_translations()["issues"]["push_channel_down"]
        entry = _push_hub_identity_entry()
        assert entry["description"].split("\n")[-2:] == sibling["description"].split("\n")[-2:]


class TestOptionsFormPushDefaultCopy:
    """Pins the options form's copy to the post-flip claim so it cannot regress.

    The pre-flip claim -- push off until turned on -- must never come back
    through a copy edit, on either the shared step description or the
    push_enabled field itself. The two generic toggles keep their own
    off-by-default claim, which moved here from the step description.
    """

    def _init_step(self) -> dict:
        return _load_en_translations()["options"]["step"]["init"]

    def test_push_enabled_copy_states_on_by_default(self):
        """The push_enabled data_description states push is on by default."""
        step = self._init_step()
        assert "on by default" in step["data_description"]["push_enabled"]

    def test_neither_description_nor_push_copy_claims_off_by_default(self):
        """The pre-flip claim cannot come back through the shared description
        or the push_enabled body."""
        step = self._init_step()
        assert "off by default" not in step["description"]
        assert "off by default" not in step["data_description"]["push_enabled"]

    def test_both_generic_toggles_still_claim_off_by_default(self):
        """The statement the step description used to make for the generic
        toggles must not simply disappear; it moved into their own bodies."""
        step = self._init_step()
        assert "off by default" in step["data_description"]["generic_entities_enabled"]
        assert "off by default" in step["data_description"]["generic_control_enabled"]


class TestHubWordingNamesHardwareOnly:
    """Guards against a sweep across the word hub that reaches copy naming real hardware.

    This integration genuinely has hubs: HWG023WBRF-V2 and its siblings are
    real hub hardware, and the three Repairs card bodies correctly name it in
    a bullet. The account itself is not a hub, and the fix for that is a
    single metadata field (manifest.json's integration_type). Any change that
    reaches the prose naming real hub hardware, in the shipped translations or
    in the README, is a regression rather than part of that fix.
    """

    def test_the_hub_bullet_appears_exactly_three_times_in_shipped_translations(self):
        """One occurrence per issue body that names a device's parent hub:
        the not-reporting body, the leftover-entities body and the
        orphaned-entities body."""
        blob = json.dumps(_load_en_translations())
        assert blob.count("- **Hub:** {hub_name}") == 3

    def test_the_manifest_integration_type_is_the_settled_value(self):
        """Pinned directly, so a later revert to the manifest fails a test
        here rather than passing silently."""
        manifest = json.loads(_MANIFEST_PATH.read_text(encoding="utf-8"))
        assert manifest["integration_type"] == _SETTLED_INTEGRATION_TYPE
        assert manifest["integration_type"] in _ACCEPTED_INTEGRATION_TYPES

    def test_the_readme_hub_count_is_unchanged(self):
        """A tripwire against a bulk edit, not a claim about how many times
        the document discusses hubs: this counts substring matches of "hub"
        case-insensitively across the whole file, including the ones inside
        repository host names in the badge URLs."""
        readme_path = Path(rainpoint_pkg.__file__).parent.parent.parent / "README.md"
        blob = readme_path.read_text(encoding="utf-8")
        assert len(re.findall("hub", blob, re.IGNORECASE)) == 69


class TestGenericControlFailedIssuePlaceholderParity:
    """The copy's placeholders and the ones _create_command_failed_issue supplies must match.

    The last of the placeholder-carrying cards to get this guard. The other two
    uncovered issue entries, push_channel_down and push_hub_identity_unresolved,
    carry no placeholders at all, so there is nothing for a parity test to pin
    on them.
    """

    @staticmethod
    def _supplied_placeholders(model: str | None = "HCS003FRF", exc: Exception | None = None) -> dict[str, str]:
        """Raise a real issue and capture what the code passed to the registry."""
        with patch.object(generic_control.ir, "async_create_issue") as create:
            generic_control._create_command_failed_issue(
                MagicMock(),
                model,
                exc or RainPointApiError("controlWorkMode failed: code 3"),
            )
        create.assert_called_once()
        return create.call_args.kwargs["translation_placeholders"]

    def test_copy_placeholders_match_the_ones_the_code_supplies(self):
        """A mismatch in either direction ships a card with a literal brace or a blank."""
        entry = _generic_control_failed_entry()
        in_copy = _placeholders_in(entry["title"]) | _placeholders_in(entry["description"])
        assert in_copy == set(self._supplied_placeholders())

    def test_copy_renders_with_the_supplied_values_and_leaves_no_brace(self):
        """Proves the parity holds under an actual render, not just by name comparison."""
        entry = _generic_control_failed_entry()
        supplied = self._supplied_placeholders()
        rendered = entry["description"].format(**supplied)
        assert "{" not in rendered
        assert "}" not in rendered
        # Held to the same brace test as the body rather than to truthiness.
        # A non-empty title carrying an escaped brace is exactly what this
        # test is named for catching, and truthiness passes it.
        rendered_title = entry["title"].format(**supplied)
        assert "{" not in rendered_title
        assert "}" not in rendered_title

    def test_a_missing_model_still_renders_clean(self):
        """model is Optional at the call site, and None must not reach the card as a blank."""
        supplied = self._supplied_placeholders(model=None)
        assert supplied["model"] == "unknown"
        rendered = _generic_control_failed_entry()["description"].format(**supplied)
        assert "{" not in rendered

    def test_the_placeholders_are_sanitized_before_they_reach_the_card(self):
        """Both values are cloud-supplied and the card is rendered as Markdown.

        Pinned here as well as at the sanitizer, because parity alone would be
        satisfied by a card that renders an injected link perfectly.
        """
        supplied = self._supplied_placeholders(
            model="[click](http://evil.example)",
            exc=RainPointApiError("[click](http://evil.example)"),
        )
        for value in supplied.values():
            assert "](" not in value
