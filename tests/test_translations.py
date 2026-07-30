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
    return _load_en_translations()["issues"]["device_not_reporting"]


class TestIssueCopyStructure:
    """Every Repairs issue must have copy for both halves of its card."""

    def test_file_parses_and_has_issues_block(self):
        data = _load_en_translations()
        assert isinstance(data, dict)
        assert isinstance(data.get("issues"), dict)
        assert data["issues"], "issues block must not be empty"

    @pytest.mark.parametrize("field", ["title", "description"])
    def test_every_issue_entry_has_non_empty_copy(self, field):
        """Iterated rather than named, so a fourth issue added later is covered
        without editing this test."""
        for key, entry in _load_en_translations()["issues"].items():
            value = entry.get(field)
            assert isinstance(value, str), f"issues.{key}.{field} must be a string"
            assert value.strip(), f"issues.{key}.{field} must not be empty"


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
        entry = _not_reporting_entry()
        in_copy = _placeholders_in(entry["title"]) | _placeholders_in(entry["description"])
        assert in_copy == set(self._supplied_placeholders())

    def test_copy_renders_with_the_supplied_values_and_leaves_no_brace(self):
        entry = _not_reporting_entry()
        supplied = self._supplied_placeholders()
        rendered = entry["description"].format(**supplied)
        assert "{" not in rendered
        assert "}" not in rendered
        assert entry["title"].format(**supplied)
