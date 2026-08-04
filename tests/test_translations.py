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

import pytest

import custom_components.rainpoint as rainpoint_pkg
from custom_components.rainpoint import repairs
from custom_components.rainpoint.repairs import (
    HubConnectivityRecord,
    OrphanedEntitiesRecord,
    RainPointHubConnectivityIssues,
    RainPointOrphanedEntityIssues,
    RainPointSilentDeviceIssues,
    SilentDeviceRecord,
)

_PLACEHOLDER_RE = re.compile(r"{(\w+)}")


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


class TestIssueCopyStructure:
    """Every Repairs issue must have copy for both halves of its card."""

    def test_file_parses_and_has_issues_block(self):
        """Invalid JSON here breaks every Repairs card at once."""
        data = _load_en_translations()
        assert isinstance(data, dict)
        assert isinstance(data.get("issues"), dict)
        assert data["issues"], "issues block must not be empty"

    @pytest.mark.parametrize("field", ["title", "description"])
    def test_every_issue_entry_has_non_empty_copy(self, field):
        """Iterated rather than named, so a fifth issue added later is covered
        without editing this test."""
        for key, entry in _load_en_translations()["issues"].items():
            value = entry.get(field)
            assert isinstance(value, str), f"issues.{key}.{field} must be a string"
            assert value.strip(), f"issues.{key}.{field} must not be empty"

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
            missed_polls=3,
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


class TestOrphanedEntitiesIssuePlaceholderParity:
    """The copy's placeholders and the ones _raise_issue supplies must match.

    Carries one assertion its two siblings do not: this is the integration's
    only fixable issue, so its confirm dialog is a second piece of copy fed by
    the same placeholder dict, and a name present there but not on the card
    would ship a dialog with a literal brace.
    """

    @staticmethod
    def _supplied_placeholders() -> dict[str, str]:
        """Raise a real issue and capture what the code passed to the registry."""
        manager = RainPointOrphanedEntityIssues(MagicMock())
        record = OrphanedEntitiesRecord(
            entry_id="e1",
            sensor_key="100_200_1",
            addr=1,
            model="HTV245FRF",
            sub_name="Front Valve",
            hub_name="Hub A",
            entity_count=2,
            missed_polls=30,
            orphaned=True,
        )
        with patch.object(repairs.ir, "async_create_issue") as create:
            manager.async_sync([record])
        create.assert_called_once()
        return create.call_args.kwargs["translation_placeholders"]

    def test_copy_placeholders_match_the_ones_the_code_supplies(self):
        """A mismatch in either direction ships a card with a literal brace or a blank."""
        entry = _orphaned_entities_entry()
        in_copy = _placeholders_in(entry["title"]) | _placeholders_in(entry["description"])
        assert in_copy == set(self._supplied_placeholders())

    def test_copy_renders_with_the_supplied_values_and_leaves_no_brace(self):
        """Proves the parity holds under an actual render, not just by name comparison."""
        entry = _orphaned_entities_entry()
        supplied = self._supplied_placeholders()
        rendered = entry["description"].format(**supplied)
        assert "{" not in rendered
        assert "}" not in rendered
        assert entry["title"].format(**supplied)

    def test_the_confirm_step_only_uses_placeholders_the_card_supplies(self):
        """The dialog reuses the card's own placeholder dict, so a name it uses
        and the card does not has no supplier at all."""
        confirm = _orphaned_entities_entry()["fix_flow"]["step"]["confirm"]
        in_confirm = _placeholders_in(confirm["title"]) | _placeholders_in(confirm["description"])
        assert in_confirm <= set(self._supplied_placeholders())

    def test_the_confirm_step_renders_with_the_supplied_values(self):
        """The same render proof for the second half of the card."""
        confirm = _orphaned_entities_entry()["fix_flow"]["step"]["confirm"]
        supplied = self._supplied_placeholders()
        rendered = confirm["description"].format(**supplied)
        assert "{" not in rendered
        assert "}" not in rendered
        assert confirm["title"].format(**supplied)
