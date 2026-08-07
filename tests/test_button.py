"""Tests for button entity platform setup (button.py)."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from custom_components.rainpoint.button import async_setup_entry
from custom_components.rainpoint.const import DOMAIN
from custom_components.rainpoint.hub_entities import RainPointHubBroadcastButton


def _make_hass(hubs=None):
    """Return a mock hass with coordinator data."""
    coord = MagicMock()
    coord.data = {"hubs": hubs if hubs is not None else [], "sensors": {}}
    hass = MagicMock()
    entry = MagicMock()
    entry.entry_id = "test_entry"
    hass.data = {DOMAIN: {entry.entry_id: {"coordinator": coord}}}
    return hass, entry, coord


class TestButtonSetupEntry:
    """Tests for button async_setup_entry."""

    @pytest.mark.asyncio
    async def test_setup_entry_creates_one_button_per_real_hub(self):
        """A real hub plus a Bluetooth wrapper record yields exactly one button.

        The wrapper record's identity fields are empty strings rather than
        absent keys, which is the shape is_hub_record was written for.
        """
        real_hub = {"hid": 182509, "mid": 236547, "name": "Hub", "did": "17053410", "mac": "A8:46:74:BB:91:F0"}
        wrapper = {"hid": 182509, "mid": 346965, "name": "", "did": "", "mac": "", "model": "", "productKey": ""}
        hass, entry, _coord = _make_hass(hubs=[real_hub, wrapper])

        mock_add_entities = MagicMock()
        await async_setup_entry(hass, entry, mock_add_entities)

        mock_add_entities.assert_called_once()
        entities = mock_add_entities.call_args[0][0]
        assert len(entities) == 1
        assert isinstance(entities[0], RainPointHubBroadcastButton)
        assert entities[0]._hub_info["did"] == "17053410"

    @pytest.mark.asyncio
    async def test_setup_entry_no_hubs_adds_empty_list(self):
        """No hubs should result in async_add_entities called with an empty list, no raise."""
        hass, entry, _coord = _make_hass(hubs=[])

        mock_add_entities = MagicMock()
        await async_setup_entry(hass, entry, mock_add_entities)

        mock_add_entities.assert_called_once()
        entities = mock_add_entities.call_args[0][0]
        assert len(entities) == 0

    @pytest.mark.asyncio
    async def test_setup_entry_multiple_hubs(self):
        """Multiple real hubs should each get a broadcast button."""
        hubs = [
            {"hid": 100, "mid": 1001, "name": "Hub 1", "softVer": "1.0", "mac": "AA:BB"},
            {"hid": 200, "mid": 2002, "name": "Hub 2", "softVer": "2.0", "mac": "CC:DD"},
        ]
        hass, entry, _coord = _make_hass(hubs=hubs)

        mock_add_entities = MagicMock()
        await async_setup_entry(hass, entry, mock_add_entities)

        mock_add_entities.assert_called_once()
        entities = mock_add_entities.call_args[0][0]
        assert len(entities) == 2

    @pytest.mark.asyncio
    async def test_setup_entry_returns_early_for_non_list_hubs(self):
        """A non-list hubs value logs an error and never calls async_add_entities."""
        coord = MagicMock()
        coord.data = {"hubs": "not-a-list", "sensors": {}}
        hass = MagicMock()
        entry = MagicMock()
        entry.entry_id = "test_entry"
        hass.data = {DOMAIN: {entry.entry_id: {"coordinator": coord}}}

        mock_add_entities = MagicMock()
        await async_setup_entry(hass, entry, mock_add_entities)

        mock_add_entities.assert_not_called()

    @pytest.mark.asyncio
    async def test_setup_entry_returns_early_for_non_list_hubs_logs_error(self, caplog):
        """The non-list case emits an error log record, matching select.py's strict shape."""
        import logging

        coord = MagicMock()
        coord.data = {"hubs": "not-a-list", "sensors": {}}
        hass = MagicMock()
        entry = MagicMock()
        entry.entry_id = "test_entry"
        hass.data = {DOMAIN: {entry.entry_id: {"coordinator": coord}}}

        mock_add_entities = MagicMock()
        with caplog.at_level(logging.ERROR):
            await async_setup_entry(hass, entry, mock_add_entities)

        assert any(record.levelno == logging.ERROR for record in caplog.records)
