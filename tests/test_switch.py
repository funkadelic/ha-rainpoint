"""Tests for switch entity platform setup (switch.py)."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from custom_components.rainpoint.const import CONF_GENERIC_CONTROL_ENABLED, DOMAIN
from custom_components.rainpoint.generic_control import RainPointGenericSwitch
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
    async def test_bluetooth_wrapper_record_gets_no_broadcast_switch(self):
        """A parent record with no hub identity is not a hub and gets no switch.

        Its identity fields are empty strings rather than absent keys, so it
        used to collapse onto the real hub's hid-keyed slot and win.
        """
        real_hub = {"hid": 182509, "mid": 236547, "name": "Hub", "did": "17053410", "mac": "A8:46:74:BB:91:F0"}
        wrapper = {"hid": 182509, "mid": 346965, "name": "", "did": "", "mac": "", "model": "", "productKey": ""}
        hass, entry, _coord = _make_hass(hubs=[real_hub, wrapper])

        mock_add_entities = MagicMock()
        await async_setup_entry(hass, entry, mock_add_entities)

        entities = mock_add_entities.call_args[0][0]
        assert len(entities) == 1
        assert entities[0]._hub_info["did"] == "17053410"

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
    async def test_setup_entry_no_debug_switch_when_url_empty(self, monkeypatch):
        """DEBUG_WORKER_URL is empty by default; no debug switch should be added."""
        # Force the precondition explicitly rather than relying on const.py
        # to stay empty, so this test does not give a misleading hub-count
        # failure if anyone sets a real debug worker URL.
        monkeypatch.setattr("custom_components.rainpoint.switch.DEBUG_WORKER_URL", "")

        hub_info = {"hid": 100, "mid": 1001, "name": "Hub 1", "softVer": "1.0", "mac": "AA:BB"}
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

        hub_info = {"hid": 100, "mid": 1001, "name": "Hub A", "softVer": "1.0", "mac": "AA:BB"}
        hass, entry, _coord = _make_hass(hubs=[hub_info])

        captured: list = []
        mock_add_entities = MagicMock(side_effect=lambda ents, **kw: captured.extend(ents))
        await async_setup_entry(hass, entry, mock_add_entities)

        # One hub broadcast switch + one debug switch = 2 entities
        assert len(captured) == 2


def _socket_sensor_entry() -> dict:
    """HWG004WRF/34: the one real CTL_SOCK candidate in the committed catalog
    with no hand-written decoder (see the note in generic_control.py).
    """
    return {
        "hid": 300,
        "mid": 400,
        "addr": 1,
        "sub_name": "Outlet 1",
        "model": "HWG004WRF",
        "model_code": 34,
        "device_name": "dev2",
        "product_key": "pk2",
        "data": {
            "type": "unknown",
            "model": "HWG004WRF",
            "raw_value": "11#00",
            "generic": {"decoder": "generic-tlv", "fields": [], "field_names": []},
        },
    }


class TestSwitchSetupEntryGenericControl:
    """The generic-control branch: build_generic_switch_entities wiring."""

    @pytest.mark.asyncio
    async def test_option_true_creates_a_generic_switch_alongside_the_broadcast_switch(self):
        """With generic control on, the socket candidate adds a switch next to the hub's."""
        hub_info = {"hid": 100, "mid": 1001, "name": "Hub 1", "softVer": "1.0", "mac": "AA:BB"}
        hass, entry, coord = _make_hass(hubs=[hub_info])
        entry.options = {CONF_GENERIC_CONTROL_ENABLED: True}
        coord.data["sensors"] = {"300_400_1": _socket_sensor_entry()}

        captured: list = []
        mock_add_entities = MagicMock(side_effect=lambda ents, **kw: captured.extend(ents))
        await async_setup_entry(hass, entry, mock_add_entities)

        assert len(captured) == 2
        assert any(isinstance(e, RainPointGenericSwitch) for e in captured)

    @pytest.mark.asyncio
    async def test_option_false_creates_no_generic_switch(self):
        """With generic control off, only the hub broadcast switch is created."""
        hub_info = {"hid": 100, "mid": 1001, "name": "Hub 1", "softVer": "1.0", "mac": "AA:BB"}
        hass, entry, coord = _make_hass(hubs=[hub_info])
        entry.options = {CONF_GENERIC_CONTROL_ENABLED: False}
        coord.data["sensors"] = {"300_400_1": _socket_sensor_entry()}

        captured: list = []
        mock_add_entities = MagicMock(side_effect=lambda ents, **kw: captured.extend(ents))
        await async_setup_entry(hass, entry, mock_add_entities)

        assert len(captured) == 1
        assert all(not isinstance(e, RainPointGenericSwitch) for e in captured)

    @pytest.mark.asyncio
    async def test_a_non_dict_sensor_record_is_skipped_and_the_broadcast_switch_survives(self):
        """A malformed sub-device record must not abort setup and drop the hub switch."""
        hub_info = {"hid": 100, "mid": 1001, "name": "Hub 1", "softVer": "1.0", "mac": "AA:BB"}
        hass, entry, coord = _make_hass(hubs=[hub_info])
        entry.options = {CONF_GENERIC_CONTROL_ENABLED: True}
        coord.data["sensors"] = {"bad": "not-a-dict", "300_400_1": _socket_sensor_entry()}

        captured: list = []
        mock_add_entities = MagicMock(side_effect=lambda ents, **kw: captured.extend(ents))
        await async_setup_entry(hass, entry, mock_add_entities)

        assert any(isinstance(e, RainPointGenericSwitch) for e in captured)
        assert len(captured) == 2

    @pytest.mark.asyncio
    async def test_option_absent_creates_no_generic_switch(self):
        """A missing option key is treated as off, not as enabled."""
        hub_info = {"hid": 100, "mid": 1001, "name": "Hub 1", "softVer": "1.0", "mac": "AA:BB"}
        hass, entry, coord = _make_hass(hubs=[hub_info])
        entry.options = {}
        coord.data["sensors"] = {"300_400_1": _socket_sensor_entry()}

        captured: list = []
        mock_add_entities = MagicMock(side_effect=lambda ents, **kw: captured.extend(ents))
        await async_setup_entry(hass, entry, mock_add_entities)

        assert len(captured) == 1
