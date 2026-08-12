"""End-to-end tests for the leftover-row half of the leftover entities card.

The departed-key half of this surface, covered by tests/test_orphan_removal.py,
offers a sensor key RainPoint has stopped listing. This module's subject is the
opposite shape: a device that is still listed and still reporting, carrying one
Home Assistant registry row that nothing alive has been behind for the whole
session. Its entities are otherwise unreachable except by hand-editing the
entity registry.

Everything here is driven as a timeline rather than asserted on an injected
snapshot, for the reason its sibling module records: the debounce, the card and
the removal are wired to each other by ordering, and a past-threshold snapshot
proves none of that wiring.
"""

from __future__ import annotations

from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from homeassistant.const import ATTR_RESTORED

from custom_components.rainpoint import _sync_orphaned_entity_issues_on_updates
from custom_components.rainpoint.const import (
    DOMAIN,
    LEFTOVER_ENTITIES_TRANSLATION_KEY,
    LEFTOVER_ROW_DEBOUNCE_UPDATES,
)
from custom_components.rainpoint.entity import LATE_ADDER_STORE_KEY
from custom_components.rainpoint.repairs import async_create_fix_flow
from custom_components.rainpoint.sensor import async_setup_entry as sensor_async_setup_entry
from custom_components.rainpoint.valve import async_setup_entry as valve_async_setup_entry
from tests.test_orphan_removal import (
    ENTRY_ID,
    SENSOR_KEY,
    _build_timeline,
    _patched_issue_registry,
)

SUB_DEVICE_ROW_ID = "device_sub_1"

# The observed shape: a sensor left behind when a model gained proper support
# and stopped falling into the unsupported bucket. No adder records it, no
# builder will ever produce it again, and it carries no zone segment.
LEFTOVER_UNIQUE_ID = f"rainpoint_{SENSOR_KEY}_unsupported"
LEFTOVER_ENTITY_ID = f"sensor.{LEFTOVER_UNIQUE_ID}"


def _live_state() -> SimpleNamespace:
    """A state object a running entity holds: no restored marker at all."""
    return SimpleNamespace(attributes={})


def _restored_state(value=True) -> SimpleNamespace:
    """A state object Home Assistant writes for a row no entity holds."""
    return SimpleNamespace(attributes={ATTR_RESTORED: value})


def _sub_device_row(row_id: str = SUB_DEVICE_ROW_ID, sensor_key: str = SENSOR_KEY) -> SimpleNamespace:
    """One device registry row whose DOMAIN identifier is a sensor key.

    Mirrors SUB_DEVICE_ROW in tests/test_orphan_removal.py, parameterised so a
    guard test can hand the same shape a previous unique_id spelling.
    """
    return SimpleNamespace(id=row_id, identifiers={(DOMAIN, sensor_key)}, config_entries=frozenset({ENTRY_ID}))


class _Harness:
    """The entity registry, device registry and state machine of one install.

    A single object rather than three fixtures, because the derivation under
    test only means anything when all three agree about the same rows: a row's
    device_id has to resolve through the device registry to a sensor key, and
    its entity_id has to resolve through the state machine to a liveness
    verdict. Splitting them let a test double disagree with itself.
    """

    def __init__(self, device_rows=None) -> None:
        """Seed one sub-device row and an otherwise empty install."""
        self.entity_rows: list[SimpleNamespace] = []
        self.device_rows = list(device_rows) if device_rows is not None else [_sub_device_row()]
        self.removed: list[str] = []
        self.released: list[tuple[str, str]] = []
        self.states: dict[str, SimpleNamespace] = {}
        self.entity_get_raises = False
        self.device_get_raises = False
        self.states_raise = False

    def add_row(
        self,
        entity_id: str,
        unique_id: str,
        *,
        state=None,
        disabled_by=None,
        device_id: str | None = SUB_DEVICE_ROW_ID,
        config_entry_id: str = ENTRY_ID,
    ) -> SimpleNamespace:
        """Register one entity registry row and the state behind it."""
        row = SimpleNamespace(
            entity_id=entity_id,
            unique_id=unique_id,
            device_id=device_id,
            disabled_by=disabled_by,
            config_entry_id=config_entry_id,
        )
        self.entity_rows.append(row)
        if state is not None:
            self.states[entity_id] = state
        return row

    def add_leftover_row(self, entity_id: str = LEFTOVER_ENTITY_ID, unique_id: str = LEFTOVER_UNIQUE_ID, **kwargs):
        """Register one dead row: in no ledger, with a restored state behind it."""
        kwargs.setdefault("state", _restored_state())
        return self.add_row(entity_id, unique_id, **kwargs)

    def make_add_entities(self, domain: str):
        """Return an async_add_entities that registers what a platform emitted.

        Home Assistant builds an entity_id as <domain>.<object_id> from the
        emitting platform's domain, and the derivation reads the domain back
        out of it. A double that stamped the integration name there would make
        every row's domain identical and silently defeat the pair match.
        """

        def _add(entities, **kwargs):
            """Register each emitted entity as a live registry row."""
            for entity in entities:
                unique_id = getattr(entity, "_attr_unique_id", None)
                if unique_id is None:
                    continue
                self.add_row(f"{domain}.{unique_id}", unique_id, state=_live_state())

        return MagicMock(side_effect=_add)

    def state_machine(self) -> SimpleNamespace:
        """Return the object that stands in for hass.states."""

        def _get(entity_id):
            """Answer the state registered for an entity id, or None."""
            if self.states_raise:
                raise RuntimeError("state machine unavailable")
            return self.states.get(entity_id)

        return SimpleNamespace(get=_get)

    def _entity_registry(self):
        """The object er.async_get answers with, which only removes."""
        harness = self

        class _FakeEntityRegistry:
            """Records each removal against the row's own entity id."""

            def async_remove(self, entity_id):
                """Record one removal call."""
                harness.removed.append(entity_id)

        return _FakeEntityRegistry()

    def _device_registry(self):
        """The object dr.async_get answers with, which only releases rows."""
        harness = self

        class _FakeDeviceRegistry:
            """Records each device-row release against that row's own id."""

            def async_update_device(self, device_id, *, remove_config_entry_id=None):
                """Record one release call."""
                harness.released.append((device_id, remove_config_entry_id))

        return _FakeDeviceRegistry()

    @contextmanager
    def patched(self):
        """Route both registry accessors in __init__.py at this harness."""

        def _entity_get(hass):
            """Answer the fake entity registry, or raise to drive the guard."""
            if self.entity_get_raises:
                raise RuntimeError("entity registry unavailable")
            return self._entity_registry()

        def _entity_entries(registry, entry_id):
            """Return this config entry's surviving rows, re-derived per call."""
            return [
                SimpleNamespace(**vars(row))
                for row in self.entity_rows
                if row.config_entry_id == entry_id and row.entity_id not in self.removed
            ]

        def _device_get(hass):
            """Answer the fake device registry, or raise to drive the guard."""
            if self.device_get_raises:
                raise RuntimeError("device registry unavailable")
            return self._device_registry()

        def _device_entries(registry, entry_id):
            """Return this config entry's device rows, re-derived per call."""
            return [
                SimpleNamespace(**{k: v for k, v in vars(row).items() if k != "config_entries"})
                for row in self.device_rows
                if entry_id in getattr(row, "config_entries", frozenset())
            ]

        with (
            patch("custom_components.rainpoint.er.async_get", side_effect=_entity_get),
            patch("custom_components.rainpoint.er.async_entries_for_config_entry", side_effect=_entity_entries),
            patch("custom_components.rainpoint.dr.async_get", side_effect=_device_get),
            patch("custom_components.rainpoint.dr.async_entries_for_config_entry", side_effect=_device_entries),
        ):
            yield


async def _armed_install(harness: _Harness):
    """Drive construct, first refresh, sweep armed, then both platform setups.

    The shared preamble of every timeline here, in the order a real install
    runs it. The caller drives its own refreshes off the returned coordinator
    and asserts between them.
    """
    coordinator, hass, entry, client = _build_timeline()
    hass.states = harness.state_machine()

    await coordinator.async_config_entry_first_refresh()
    _sync_orphaned_entity_issues_on_updates(hass, entry, coordinator)
    await valve_async_setup_entry(hass, entry, harness.make_add_entities("valve"))
    await sensor_async_setup_entry(hass, entry, harness.make_add_entities("sensor"))
    return coordinator, hass, entry, client


def _ledger_snapshot(hass) -> list[tuple[str, frozenset[str]]]:
    """Every adder's ledger contents, flattened so it can be compared later."""
    snapshot = []
    for adder in hass.data[DOMAIN][ENTRY_ID][LATE_ADDER_STORE_KEY]:
        for key in sorted(adder.ledger.keys()):
            snapshot.append((key, adder.ledger.unique_ids_for(key)))
    return snapshot


class TestLeftoverRowEndToEnd:
    """One dead row on a present device becomes one card that removes it."""

    @pytest.mark.asyncio
    async def test_a_dead_row_on_a_present_device_is_offered_and_removed(self):
        """The whole path, driven in the order a real install runs it.

        The device never stops reporting, so nothing here is reachable through
        the departed-key derivation at any point.
        """
        harness = _Harness()

        with _patched_issue_registry() as (create, delete):
            coordinator, hass, _entry, _client = await _armed_install(harness)
            harness.add_leftover_row()
            ledger_before = _ledger_snapshot(hass)

            with harness.patched():
                # One update short of the threshold, nothing is offered.
                for _ in range(LEFTOVER_ROW_DEBOUNCE_UPDATES - 1):
                    await coordinator.async_refresh()
                assert create.call_count == 0

                await coordinator.async_refresh()
                assert create.call_count == 1

                issue_id = create.call_args.args[2]
                kwargs = create.call_args.kwargs
                assert kwargs["is_fixable"] is True
                assert kwargs["translation_key"] == LEFTOVER_ENTITIES_TRANSLATION_KEY
                assert kwargs["data"]["sensor_key"] == SENSOR_KEY
                assert kwargs["data"]["leftover"] is True
                assert kwargs["translation_placeholders"]["entity_count"] == "1"
                assert kwargs["translation_placeholders"]["missed_polls"] == str(LEFTOVER_ROW_DEBOUNCE_UPDATES)

                flow = await async_create_fix_flow(hass, issue_id, kwargs["data"])
                flow.hass = hass

                shown = await flow.async_step_init()
                assert shown["step_id"] == "confirm"
                # Opening the card removes nothing.
                assert harness.removed == []

                registered = sorted(row.entity_id for row in harness.entity_rows)
                result = await flow.async_step_confirm({})
                assert result["type"] == "create_entry"

                # Exactly the dead row, and every live row still standing.
                assert harness.removed == [LEFTOVER_ENTITY_ID]
                surviving = sorted(row.entity_id for row in harness.entity_rows if row.entity_id not in harness.removed)
                assert surviving == [entity_id for entity_id in registered if entity_id != LEFTOVER_ENTITY_ID]
                # The device is still present, so its device row stays.
                assert harness.released == []
                # No adder's ledger moved: none of these pairs was ever in one.
                assert _ledger_snapshot(hass) == ledger_before

                # With the row gone the key reports nothing left over, so its
                # card clears rather than being raised a second time.
                await coordinator.async_refresh()
                assert create.call_count == 1
                assert any(call.args[2] == issue_id for call in delete.call_args_list)
