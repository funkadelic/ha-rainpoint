"""Tests for select entity platform setup (select.py)."""

from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, MagicMock

from custom_components.rainpoint.select import async_setup_entry
from custom_components.rainpoint.const import DOMAIN


def _make_hass(hubs=None):
    """Return a mock hass with coordinator data."""
    coord = MagicMock()
    coord.data = {"hubs": hubs if hubs is not None else [], "sensors": {}}
    hass = MagicMock()
    entry = MagicMock()
    entry.entry_id = "test_entry"
    hass.data = {DOMAIN: {entry.entry_id: {"coordinator": coord}}}
    return hass, entry, coord


class TestSelectSetupEntry:
    """Tests for select async_setup_entry."""

    @pytest.mark.asyncio
    async def test_setup_entry_creates_entities_for_each_hub(self):
        """One channel select entity should be created per hub."""
        hub_info = {"hid": 100, "name": "Hub 1", "softVer": "1.0", "mac": "AA:BB"}
        hass, entry, coord = _make_hass(hubs=[hub_info])

        mock_add_entities = MagicMock()
        await async_setup_entry(hass, entry, mock_add_entities)

        mock_add_entities.assert_called_once()
        entities = mock_add_entities.call_args[0][0]
        assert len(entities) == 1

    @pytest.mark.asyncio
    async def test_setup_entry_no_hubs_adds_empty_list(self):
        """No hubs should result in async_add_entities called with empty list."""
        hass, entry, coord = _make_hass(hubs=[])

        mock_add_entities = MagicMock()
        await async_setup_entry(hass, entry, mock_add_entities)

        mock_add_entities.assert_called_once()
        entities = mock_add_entities.call_args[0][0]
        assert len(entities) == 0

    @pytest.mark.asyncio
    async def test_setup_entry_multiple_hubs(self):
        """Multiple hubs should each get a channel select entity."""
        hubs = [
            {"hid": 100, "name": "Hub 1", "softVer": "1.0"},
            {"hid": 200, "name": "Hub 2", "softVer": "2.0"},
        ]
        hass, entry, coord = _make_hass(hubs=hubs)

        mock_add_entities = MagicMock()
        await async_setup_entry(hass, entry, mock_add_entities)

        mock_add_entities.assert_called_once()
        entities = mock_add_entities.call_args[0][0]
        assert len(entities) == 2

    @pytest.mark.asyncio
    async def test_setup_entry_returns_early_for_non_list_hubs(self):
        """If hubs is not a list, setup should return early without adding entities."""
        coord = MagicMock()
        coord.data = {"hubs": "not-a-list", "sensors": {}}
        hass = MagicMock()
        entry = MagicMock()
        entry.entry_id = "test_entry"
        hass.data = {DOMAIN: {entry.entry_id: {"coordinator": coord}}}

        mock_add_entities = MagicMock()
        await async_setup_entry(hass, entry, mock_add_entities)

        # async_add_entities should not be called when hubs is invalid
        mock_add_entities.assert_not_called()
