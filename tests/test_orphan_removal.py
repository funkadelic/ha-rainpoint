"""End-to-end tests for the orphaned entity removal path.

The defect this path exists for is an ordering property, not an end state: a
sub-device that leaves its hub's subDevices enumeration mid-session keeps the
entities the late adder already emitted for it, permanently unavailable, and
every later re-key adds another set. Nothing short of a driven timeline proves
the counter, the card and the removal are wired to each other, so every test
here constructs a real coordinator, runs a real first refresh, runs the real
platform setup, and then drives real refreshes, asserting between the steps.

Injecting an already-past-threshold coordinator.data snapshot is deliberately
avoided: that substitution is how two earlier defects on this surface shipped
under a green suite at full branch coverage.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.rainpoint import (
    _remove_orphaned_key_rows,
    _sync_orphaned_entity_issues_on_updates,
    repairs,
)
from custom_components.rainpoint.const import CONF_HIDS, DOMAIN, MODEL_VALVE_245
from custom_components.rainpoint.coordinator import (
    ORPHANED_KEY_DEBOUNCE_POLLS,
    RainPointCoordinator,
)
from custom_components.rainpoint.entity import LATE_ADDER_STORE_KEY
from custom_components.rainpoint.repairs import (
    async_create_fix_flow,
    orphaned_entities_issue_id,
)
from custom_components.rainpoint.valve import async_setup_entry as valve_async_setup_entry
from tests.helpers import make_valve_zone_status

HID = 100
MID = 200
ADDR = 1
SENSOR_KEY = f"{HID}_{MID}_{ADDR}"
ENTRY_ID = "e1"
FOREIGN_ENTRY_ID = "other_entry"

ZONE_1_UNIQUE_ID = f"rainpoint_{SENSOR_KEY}_zone1"
ZONE_2_UNIQUE_ID = f"rainpoint_{SENSOR_KEY}_zone2"

# (config entry id, entity_id, unique_id). Two rows this session's valve adder
# really emits, one same-entry row for a different sensor key, and one row on a
# foreign config entry carrying the very same unique_id. The last two are the
# blast-radius assertion: neither may ever be removed.
_REGISTRY_ROWS = [
    (ENTRY_ID, "valve.zone1", ZONE_1_UNIQUE_ID),
    (ENTRY_ID, "valve.zone2", ZONE_2_UNIQUE_ID),
    (ENTRY_ID, "valve.unrelated", f"rainpoint_{HID}_{MID}_9_zone1"),
    (FOREIGN_ENTRY_ID, "valve.foreign", ZONE_1_UNIQUE_ID),
]


def _hub_record(*, with_child: bool) -> list[dict]:
    """One real valve hub whose subDevices either lists its child or does not."""
    sub_devices = [{"addr": ADDR, "name": "Hub A", "model": MODEL_VALVE_245, "softVer": "127"}]
    return [
        {
            "mid": MID,
            "name": "Hub A",
            "deviceName": "d",
            "productKey": "pk",
            "homeName": "H",
            "subDevices": sub_devices if with_child else [],
        }
    ]


def _make_entity_registry():
    """Return (removed, async_get, async_entries) over a seeded fake registry.

    Rows are re-derived per call and keyed on their own entity_id, so
    "removed the right rows" cannot be confused with "removed any rows", and
    the config-entry scope is honoured so the sweep's config-entry-scoped
    lookup is exercised rather than papered over.
    """
    removed: list[str] = []

    class _FakeEntityRegistry:
        """Records each removal against the row's own entity id."""

        def async_remove(self, entity_id):
            """Record one removal call."""
            removed.append(entity_id)

    registry = _FakeEntityRegistry()

    def _async_get(hass):
        """Return the seeded fake registry."""
        return registry

    def _async_entries_for_config_entry(reg, entry_id):
        """Return this config entry's surviving rows, re-derived per call."""
        return [
            SimpleNamespace(entity_id=entity_id, unique_id=unique_id)
            for row_entry_id, entity_id, unique_id in _REGISTRY_ROWS
            if row_entry_id == entry_id and entity_id not in removed
        ]

    return removed, _async_get, _async_entries_for_config_entry


def _build_timeline(*, zones_reported: bool = True):
    """Return (coordinator, hass, entry, client) for one real valve hub."""
    client = AsyncMock()
    client.get_devices_by_hid.return_value = _hub_record(with_child=True)
    client.get_multiple_device_status.return_value = make_valve_zone_status(mid=MID, zones_reported=zones_reported)

    entry = MagicMock()
    entry.entry_id = ENTRY_ID
    entry.data = {CONF_HIDS: [HID]}
    entry.options = {}

    hass = MagicMock()
    hass.data = {DOMAIN: {ENTRY_ID: {}}}

    coordinator = RainPointCoordinator(hass, client, entry)
    hass.data[DOMAIN][ENTRY_ID]["coordinator"] = coordinator
    return coordinator, hass, entry, client


def _capturing_add_entities():
    """Return (captured, async_add_entities) recording every emitted entity."""
    captured: list = []
    return captured, MagicMock(side_effect=lambda ents, **kw: captured.extend(ents))


class TestOrphanedKeyEndToEnd:
    """A vanished key becomes one confirmable card that removes its own rows."""

    @pytest.mark.asyncio
    async def test_vanished_key_ages_out_into_one_card_whose_confirm_removes_its_rows(self):
        """The whole path, driven in the order a real install runs it."""
        coordinator, hass, entry, client = _build_timeline()
        removed, async_get, async_entries = _make_entity_registry()
        captured, async_add_entities = _capturing_add_entities()

        with (
            patch.object(repairs.ir, "async_create_issue") as create,
            patch.object(repairs.ir, "async_delete_issue"),
        ):
            await coordinator.async_config_entry_first_refresh()
            _sync_orphaned_entity_issues_on_updates(hass, entry, coordinator)
            await valve_async_setup_entry(hass, entry, async_add_entities)

            # Poll 1 and 2: the child is listed and reporting, so the adder
            # emitted its zones and nothing is being counted.
            assert sorted(e._attr_unique_id for e in captured) == [ZONE_1_UNIQUE_ID, ZONE_2_UNIQUE_ID]
            await coordinator.async_refresh()
            assert create.call_count == 0
            assert coordinator._orphaned_key_poll_counts == {}

            # Poll 3 onwards: the hub is still listed, its child is not.
            client.get_devices_by_hid.return_value = _hub_record(with_child=False)
            for _ in range(ORPHANED_KEY_DEBOUNCE_POLLS - 1):
                await coordinator.async_refresh()

            # The boundary: one poll short of the threshold, no card exists.
            assert coordinator._orphaned_key_poll_counts[SENSOR_KEY] == ORPHANED_KEY_DEBOUNCE_POLLS - 1
            assert create.call_count == 0

            await coordinator.async_refresh()
            assert create.call_count == 1

            # Steady state: further polls raise no second card.
            await coordinator.async_refresh()
            await coordinator.async_refresh()
            assert create.call_count == 1

        issue_id = create.call_args.args[2]
        kwargs = create.call_args.kwargs
        assert issue_id == orphaned_entities_issue_id(SENSOR_KEY)
        assert issue_id == f"orphaned_device_entities_{SENSOR_KEY}"
        assert kwargs["is_fixable"] is True
        assert kwargs["translation_key"] == "orphaned_device_entities"
        assert kwargs["data"] == {"entry_id": ENTRY_ID, "sensor_key": SENSOR_KEY}
        assert kwargs["translation_placeholders"]["entity_count"] == "2"
        assert kwargs["translation_placeholders"]["missed_polls"] == str(ORPHANED_KEY_DEBOUNCE_POLLS)

        adder = hass.data[DOMAIN][ENTRY_ID][LATE_ADDER_STORE_KEY][0]
        assert adder.ledger.unique_ids_for(SENSOR_KEY) == frozenset({ZONE_1_UNIQUE_ID, ZONE_2_UNIQUE_ID})

        flow = await async_create_fix_flow(hass, issue_id, kwargs["data"])
        flow.hass = hass

        with (
            patch("custom_components.rainpoint.er.async_get", side_effect=async_get),
            patch("custom_components.rainpoint.er.async_entries_for_config_entry", side_effect=async_entries),
        ):
            shown = await flow.async_step_init()
            assert shown["type"] == "form"
            assert shown["step_id"] == "confirm"
            # Opening the card removes nothing.
            assert removed == []

            result = await flow.async_step_confirm({})

        assert result["type"] == "create_entry"
        assert sorted(removed) == ["valve.zone1", "valve.zone2"]
        # The unrelated same-entry row and the foreign-entry row are untouched.
        assert "valve.unrelated" not in removed
        assert "valve.foreign" not in removed

        # Forgotten in lockstep with the removal, in both structures.
        assert adder.ledger.unique_ids_for(SENSOR_KEY) == frozenset()
        assert SENSOR_KEY not in adder.ledger.keys()  # noqa: SIM118 -- a named accessor, not a mapping
        assert ZONE_1_UNIQUE_ID not in adder._emitted
        assert ZONE_2_UNIQUE_ID not in adder._emitted

    @pytest.mark.asyncio
    async def test_a_key_this_session_never_emitted_for_removes_nothing(self):
        """The empty-union early return: no ledger entry, no registry touched."""
        _coordinator, hass, entry, _client = _build_timeline()
        removed, async_get, async_entries = _make_entity_registry()

        with (
            patch("custom_components.rainpoint.er.async_get", side_effect=async_get),
            patch("custom_components.rainpoint.er.async_entries_for_config_entry", side_effect=async_entries),
        ):
            assert _remove_orphaned_key_rows(hass, entry, SENSOR_KEY) == 0

        assert removed == []
