"""Tests for switch entity platform setup (switch.py)."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from custom_components.rainpoint.const import DOMAIN
from custom_components.rainpoint.switch import async_setup_entry


def _make_hass(hubs=None):
    """Return a mock hass with coordinator data."""
    coord = MagicMock()
    coord.data = {"hubs": hubs if hubs is not None else [], "sensors": {}}
    hass = MagicMock()
    entry = MagicMock()
    entry.entry_id = "test_entry"
    hass.data = {DOMAIN: {entry.entry_id: {"coordinator": coord}}}
    return hass, entry, coord


class TestSwitchSetupEntry:
    """Tests for switch async_setup_entry."""

    @pytest.mark.asyncio
    async def test_setup_entry_creates_broadcast_switch_per_hub(self):
        """One broadcast switch should be created per hub."""
        hub_info = {"hid": 100, "name": "Hub 1", "softVer": "1.0", "mac": "AA:BB"}
        hass, entry, _coord = _make_hass(hubs=[hub_info])

        mock_add_entities = MagicMock()
        await async_setup_entry(hass, entry, mock_add_entities)

        mock_add_entities.assert_called_once()
        entities = mock_add_entities.call_args[0][0]
        # At minimum one broadcast switch per hub (no debug switch since DEBUG_WORKER_URL is empty)
        assert len(entities) == 1

    @pytest.mark.asyncio
    async def test_setup_entry_no_hubs(self):
        """No hubs should result in no broadcast switch entities."""
        hass, entry, _coord = _make_hass(hubs=[])

        mock_add_entities = MagicMock()
        await async_setup_entry(hass, entry, mock_add_entities)

        mock_add_entities.assert_called_once()
        entities = mock_add_entities.call_args[0][0]
        assert len(entities) == 0

    @pytest.mark.asyncio
    async def test_setup_entry_multiple_hubs(self):
        """Multiple hubs should each get a broadcast switch entity."""
        hubs = [
            {"hid": 100, "name": "Hub 1", "softVer": "1.0"},
            {"hid": 200, "name": "Hub 2", "softVer": "2.0"},
        ]
        hass, entry, _coord = _make_hass(hubs=hubs)

        mock_add_entities = MagicMock()
        await async_setup_entry(hass, entry, mock_add_entities)

        mock_add_entities.assert_called_once()
        entities = mock_add_entities.call_args[0][0]
        assert len(entities) == 2

    @pytest.mark.asyncio
    async def test_setup_entry_no_debug_switch_when_url_empty(self, monkeypatch):
        """DEBUG_WORKER_URL is empty by default; no debug switch should be added."""
        # Force the precondition explicitly rather than relying on const.py
        # to stay empty, so this test does not give a misleading hub-count
        # failure if anyone sets a real debug worker URL.
        monkeypatch.setattr("custom_components.rainpoint.switch.DEBUG_WORKER_URL", "")

        hub_info = {"hid": 100, "name": "Hub 1", "softVer": "1.0"}
        hass, entry, _coord = _make_hass(hubs=[hub_info])

        mock_add_entities = MagicMock()
        await async_setup_entry(hass, entry, mock_add_entities)

        entities = mock_add_entities.call_args[0][0]
        # Verify none of the entities is a debug entity by checking count == hub count
        assert len(entities) == 1

    @pytest.mark.asyncio
    async def test_setup_entry_debug_switch_when_url_configured(self, monkeypatch):
        """A truthy DEBUG_WORKER_URL appends a RainPointDebugSwitchEntity alongside hubs."""
        # Patch the module-level name seen by switch.py, not the const module.
        monkeypatch.setattr(
            "custom_components.rainpoint.switch.DEBUG_WORKER_URL",
            "https://example.com",
        )
        # Stub the debug entity constructor so we do not exercise its real body.
        monkeypatch.setattr(
            "custom_components.rainpoint.debug.RainPointDebugSwitchEntity",
            MagicMock(),
        )

        hub_info = {"hid": 100, "name": "Hub A", "softVer": "1.0"}
        hass, entry, _coord = _make_hass(hubs=[hub_info])

        captured: list = []
        mock_add_entities = MagicMock(side_effect=lambda ents, **kw: captured.extend(ents))
        await async_setup_entry(hass, entry, mock_add_entities)

        # One hub broadcast switch + one debug switch = 2 entities
        assert len(captured) == 2
