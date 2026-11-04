"""Tests for the shipped manifest (manifest.json): the client pin and the logger routing."""

from __future__ import annotations

import importlib.metadata
import json
from pathlib import Path

import custom_components.rainpoint as rainpoint_pkg

_MANIFEST_PATH = Path(rainpoint_pkg.__file__).parent / "manifest.json"


def _manifest() -> dict:
    return json.loads(_MANIFEST_PATH.read_text(encoding="utf-8"))


def test_the_installed_client_is_the_one_the_manifest_pins():
    """CI installs from the manifest, so a leg that installed another version fails here."""
    (requirement,) = _manifest()["requirements"]
    name, separator, pinned = requirement.partition("=" * 2)

    assert name == "aiorainpoint"
    assert separator == "=" * 2
    assert importlib.metadata.version(name) == pinned


def test_the_library_logger_is_routed_alongside_the_integration():
    """The library logs under its own root, which is what the second entry routes."""
    assert _manifest()["loggers"] == ["custom_components.rainpoint", "aiorainpoint"]
