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

import ast
import inspect
import logging
import textwrap
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from homeassistant.const import ATTR_RESTORED

from custom_components.rainpoint import (
    _build_leftover_row_pairs,
    _debounced_leftover_pairs,
    _fetch_registry_rows,
    _ledger_pairs_by_key,
    _leftover_pairs_now,
    _remove_orphaned_key_rows,
    _resolve_device_names,
    _row_is_unbacked,
    _sync_orphaned_entity_issues_on_updates,
)
from custom_components.rainpoint import dr as rainpoint_dr
from custom_components.rainpoint.const import (
    DOMAIN,
    GENERIC_CONTROL_UNIQUE_ID_MARKER,
    GENERIC_UNIQUE_ID_MARKER,
    HUB_IDENTIFIER_PREFIX,
    LEFTOVER_ENTITIES_TRANSLATION_KEY,
    LEFTOVER_ROW_DEBOUNCE_UPDATES,
    ORPHANED_ENTITIES_ISSUE_ID_PREFIX,
)
from custom_components.rainpoint.coordinator import ORPHANED_KEY_DEBOUNCE_POLLS
from custom_components.rainpoint.entity import LATE_ADDER_STORE_KEY, LateEntityAdder
from custom_components.rainpoint.repairs import async_create_fix_flow
from custom_components.rainpoint.sensor import async_setup_entry as sensor_async_setup_entry
from custom_components.rainpoint.valve import async_setup_entry as valve_async_setup_entry
from tests.test_orphan_removal import (
    ENTRY_ID,
    HID,
    SENSOR_KEY,
    _build_timeline,
    _hub_record,
    _ledger_entity,
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
        # Counts every call into the patched dr.async_get, which is how a test
        # proves the device registry is fetched once per sync rather than once
        # per consumer.
        self.device_get_calls = 0
        # Entity ids whose removal the registry refuses, which is how a row
        # that raises on the way out is driven into the per-row guard.
        self.remove_raises_for: set[str] = set()

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
                """Record one removal call, or refuse it."""
                if entity_id in harness.remove_raises_for:
                    raise RuntimeError(f"{entity_id} is busy")
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
            """Return this config entry's surviving rows, re-derived per call.

            The rows themselves rather than clones of them, because a guard
            test needs a row shape that raises when one of its fields is read,
            and cloning through vars() would evaluate that field first.
            """
            return [
                row
                for row in self.entity_rows
                if getattr(row, "config_entry_id", None) == entry_id and getattr(row, "entity_id", None) not in self.removed
            ]

        def _device_get(hass):
            """Answer the fake device registry, or raise to drive the guard."""
            self.device_get_calls += 1
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


def _adder_with(domain: str, unique_ids, key: str = SENSOR_KEY) -> LateEntityAdder:
    """A real adder whose ledger holds these unique_ids for one sensor key.

    A real one rather than a stub, because the derivation reads the ledger
    through the same two accessors the removal path does, and a stub that
    answered them without ever having recorded anything would prove neither.
    """
    coordinator = SimpleNamespace(data={"sensors": {}})
    adder = LateEntityAdder(
        coordinator,
        lambda entities: None,
        lambda k, i: [_ledger_entity(unique_id) for unique_id in unique_ids],
        domain,
    )
    adder.collect(key, {})
    return adder


def _seed_adder() -> LateEntityAdder:
    """One adder recording a single sensor row, so the key is a candidate at all.

    A sensor key reaches the derivation only when some adder recorded it this
    session, so every scan test needs one ledger entry that is not the row
    under test.
    """
    return _adder_with("sensor", [f"rainpoint_{SENSOR_KEY}_battery"])


def _derive(harness: _Harness, *, adders=None, live_keys=frozenset({SENSOR_KEY})) -> dict:
    """Run the leftover derivation once over one seeded install.

    Fetches device_rows itself, through the same patched accessors the
    harness routes _sync_orphaned_entity_issues at, mirroring how that
    function now supplies _build_leftover_row_pairs with an already-fetched
    list rather than letting it fetch its own.
    """
    hass = SimpleNamespace(data={}, states=harness.state_machine())
    entry = SimpleNamespace(entry_id=ENTRY_ID)
    entry_store = {LATE_ADDER_STORE_KEY: list(adders if adders is not None else [_seed_adder()])}
    with harness.patched():
        _, device_rows = _fetch_registry_rows(
            rainpoint_dr.async_get, rainpoint_dr.async_entries_for_config_entry, hass, entry, "test"
        )
        return _build_leftover_row_pairs(hass, entry, entry_store, live_keys, device_rows)


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


class TestLivenessIsTheGate:
    """Nothing without an exact restored marker behind it is ever a candidate.

    This is the guard the whole shape rests on. Every case here fails against
    a derivation that reads the marker loosely, and every one of them errs
    towards leaving a row alone, because the only thing downstream of a True
    verdict is an offer to delete recorder history that cannot be restored.
    """

    def test_a_row_whose_state_carries_no_restored_marker_is_never_a_candidate(self):
        """The ordinary live row: in no ledger, on a live key, and still safe."""
        harness = _Harness()
        harness.add_row(LEFTOVER_ENTITY_ID, LEFTOVER_UNIQUE_ID, state=_live_state())

        assert _derive(harness) == {}

    def test_a_row_with_no_state_at_all_is_never_a_candidate(self):
        """An absent state establishes nothing, so it cannot establish death."""
        harness = _Harness()
        harness.add_row(LEFTOVER_ENTITY_ID, LEFTOVER_UNIQUE_ID)

        assert _derive(harness) == {}

    def test_a_truthy_restored_value_that_is_not_the_boolean_is_never_a_candidate(self):
        """The identity comparison, as the guard it exists to be.

        A state stand-in can answer a truthy object for any attribute, so a
        truthiness test here would read a whole harness as dead.
        """
        harness = _Harness()
        harness.add_row(LEFTOVER_ENTITY_ID, LEFTOVER_UNIQUE_ID, state=_restored_state("yes"))

        assert _derive(harness) == {}

    def test_a_state_whose_attributes_are_not_a_mapping_is_never_a_candidate(self):
        """The row shape a MagicMock state machine hands back."""
        harness = _Harness()
        harness.add_row(LEFTOVER_ENTITY_ID, LEFTOVER_UNIQUE_ID, state=SimpleNamespace(attributes=MagicMock()))

        assert _derive(harness) == {}

    def test_an_unreadable_state_machine_yields_no_candidates_rather_than_every_row(self):
        """The failure direction that matters: unreadable means "leave alone"."""
        harness = _Harness()
        harness.add_leftover_row()
        harness.states_raise = True

        assert _derive(harness) == {}

    def test_the_gate_in_isolation_answers_true_only_for_an_exact_marker(self):
        """The predicate on its own, including the shape that raises."""
        hass = SimpleNamespace(states=SimpleNamespace(get=lambda entity_id: None))
        assert _row_is_unbacked(hass, "sensor.absent") is False

        dead = SimpleNamespace(states=SimpleNamespace(get=lambda entity_id: _restored_state()))
        assert _row_is_unbacked(dead, "sensor.dead") is True


class TestWhatTheScanMayNotReach:
    """Four populations the scan is prohibited from offering, and one it may."""

    def test_a_disabled_row_is_never_a_candidate(self):
        """A row the user disabled is still the user's. It is not leftover."""
        harness = _Harness()
        harness.add_leftover_row(disabled_by="user")

        assert _derive(harness) == {}

    def test_a_generic_namespace_row_is_never_a_candidate_in_either_namespace(self):
        """The generic sweep owns that namespace and governs it by its own
        toggles, so a second path deciding on those rows would let one of them
        remove what the other means to keep."""
        harness = _Harness()
        harness.add_leftover_row(
            entity_id=f"sensor.rainpoint_{SENSOR_KEY}{GENERIC_UNIQUE_ID_MARKER}humidity",
            unique_id=f"rainpoint_{SENSOR_KEY}{GENERIC_UNIQUE_ID_MARKER}humidity",
        )
        harness.add_leftover_row(
            entity_id=f"switch.rainpoint_{SENSOR_KEY}{GENERIC_CONTROL_UNIQUE_ID_MARKER}1",
            unique_id=f"rainpoint_{SENSOR_KEY}{GENERIC_CONTROL_UNIQUE_ID_MARKER}1",
        )

        assert _derive(harness) == {}

    def test_a_per_zone_row_is_never_a_candidate_in_any_of_its_spellings(self):
        """The narrowing guard, in the only direction it may ever apply.

        A zone produces its entities the first time that zone reports, so a
        zone nobody has watered since the last restart reads exactly like a
        zone that is gone for good. All four spellings this integration writes
        are covered, not just the bare one.
        """
        harness = _Harness()
        for suffix in ("zone1", "zone1_duration", "zone2_water_used", "zone3_state"):
            unique_id = f"rainpoint_{SENSOR_KEY}_{suffix}"
            harness.add_leftover_row(entity_id=f"sensor.{unique_id}", unique_id=unique_id)

        assert _derive(harness) == {}

    def test_a_row_carrying_no_string_unique_id_is_never_a_candidate(self):
        """A row shape the scan cannot name a pair for cannot be removed by one."""
        harness = _Harness()
        harness.add_leftover_row(entity_id="sensor.no_unique_id", unique_id=None)

        assert _derive(harness) == {}

    def test_a_row_whose_entity_id_carries_no_domain_is_never_a_candidate(self):
        """The domain is half the pair, and there is nowhere else to read it
        from: an entity has no domain until Home Assistant registers it."""
        harness = _Harness()
        harness.add_leftover_row(entity_id="no_domain_here", unique_id=LEFTOVER_UNIQUE_ID)

        assert _derive(harness) == {}

    def test_a_row_whose_pair_an_adder_recorded_is_never_a_candidate(self):
        """That row belongs to the departed-key shape, whose scope is the
        ledger. Two shapes claiming one row is how a live row gets deleted."""
        harness = _Harness()
        recorded = f"rainpoint_{SENSOR_KEY}_battery"
        harness.add_leftover_row(entity_id=f"sensor.{recorded}", unique_id=recorded)

        assert _derive(harness) == {}

    def test_the_same_unique_id_in_another_domain_is_a_separate_candidate(self):
        """Registry uniqueness is per domain, so a unique_id on its own is a
        partial identifier. The recorded pair is spared and the unrecorded one
        in the other domain is offered, from one and the same id."""
        harness = _Harness()
        recorded = f"rainpoint_{SENSOR_KEY}_battery"
        harness.add_leftover_row(entity_id=f"sensor.{recorded}", unique_id=recorded)
        harness.add_leftover_row(entity_id=f"binary_sensor.{recorded}", unique_id=recorded)

        assert _derive(harness) == {SENSOR_KEY: frozenset({("binary_sensor", recorded)})}


class TestHowARowReachesASensorKey:
    """Only through its device row's identifier, never through its own id."""

    def test_a_previous_unique_id_shape_stays_out_of_reach(self):
        """The hub identifier shape that predates the hub identity re-key
        resolves to something no adder recorded, so a row sitting on it fails
        the candidate-key test at the same gate that excludes a foreign row.
        Driven with the real old spelling rather than an invented string."""
        harness = _Harness(device_rows=[_sub_device_row(sensor_key=f"{HUB_IDENTIFIER_PREFIX}{HID}")])
        harness.add_leftover_row()

        assert _derive(harness) == {}

    def test_a_row_whose_device_row_cannot_be_resolved_is_never_a_candidate(self):
        """No device row means no sensor key, and no sensor key means no card."""
        harness = _Harness(device_rows=[])
        harness.add_leftover_row()

        assert _derive(harness) == {}

    def test_a_key_absent_from_the_current_poll_yields_nothing(self):
        """The mutual-exclusion half: a key the poll does not list belongs to
        the departed shape, whatever its rows look like."""
        harness = _Harness()
        harness.add_leftover_row()

        assert _derive(harness, live_keys=frozenset()) == {}


class TestResolveDeviceNames:
    """Each sensor key's Home Assistant name, resolved once from device rows.

    Unit-level, over _resolve_device_names in isolation, because the fallback
    order and the guard behaviour it protects hold regardless of which caller
    supplies the rows.
    """

    def test_a_renamed_device_yields_its_name_by_user(self):
        """The fallback's first rung: a rename always wins over the row's own
        name, which build_sub_device_info always stamps."""
        row = SimpleNamespace(id="d1", identifiers={(DOMAIN, SENSOR_KEY)}, name_by_user="Front Lawn", name="HTV210B 1")

        assert _resolve_device_names([row]) == {SENSOR_KEY: "Front Lawn"}

    def test_an_unrenamed_device_falls_back_to_its_own_name(self):
        """A device the owner has never renamed still resolves to something,
        rather than to nothing at all."""
        row = SimpleNamespace(id="d1", identifiers={(DOMAIN, SENSOR_KEY)}, name_by_user=None, name="HTV210B 1")

        assert _resolve_device_names([row]) == {SENSOR_KEY: "HTV210B 1"}

    def test_no_device_row_yields_no_entry_for_that_key(self):
        """The card falls back further still, to the record's own cloud
        sub_name, entirely outside this function -- it never reads that
        field."""
        assert _resolve_device_names([]) == {}

    def test_a_row_with_no_domain_identifier_contributes_nothing(self):
        """A row belonging to another integration, or carrying a malformed
        identifiers value, resolves to no sensor key and therefore names
        nothing. _domain_sensor_key already owns both shapes."""
        foreign = SimpleNamespace(id="d1", identifiers={("other_integration", SENSOR_KEY)}, name_by_user=None, name="X")
        malformed = SimpleNamespace(id="d2", identifiers="not-a-set-of-tuples", name_by_user=None, name="Y")

        assert _resolve_device_names([foreign, malformed]) == {}

    def test_an_unreadable_registry_yields_an_empty_map(self):
        """The caller degrades to an empty device_rows list on a registry
        failure, and this function's job is only to answer {} for that,
        exactly as it does for any other empty input."""
        assert _resolve_device_names([]) == {}

    def test_a_malformed_device_row_does_not_abort_the_rest(self):
        """One row whose identifiers attribute is entirely absent -- the shape
        that raises inside _domain_sensor_key rather than answering None --
        costs only that row."""
        malformed = SimpleNamespace(id="d_bad")
        good = SimpleNamespace(id="d1", identifiers={(DOMAIN, SENSOR_KEY)}, name_by_user="Front Lawn", name="X")

        assert _resolve_device_names([malformed, good]) == {SENSOR_KEY: "Front Lawn"}

    def test_two_devices_of_one_model_resolve_to_two_different_names(self):
        """The 2026-08-04 observation, at the resolution layer: two HTV210Bs
        under one model produce two names once each carries its own
        name_by_user."""
        other_key = f"{HID}_300_1"
        first = SimpleNamespace(
            id="d1", identifiers={(DOMAIN, SENSOR_KEY)}, name_by_user="HTV210B (Hub paired)", name="HTV210B 1"
        )
        second = SimpleNamespace(id="d2", identifiers={(DOMAIN, other_key)}, name_by_user="HTV210B (BT)", name="HTV210B 1")

        names = _resolve_device_names([first, second])

        assert names[SENSOR_KEY] != names[other_key]


class TestTheDeviceRegistryIsFetchedOncePerSync:
    """The name lookup reuses the leftover derivation's own fetch.

    Driven end to end rather than by reading source, because a shared fetch
    is a runtime property: two functions can each hold their own call to
    dr.async_get and still read as "no second fetch" to a source-only check.
    """

    @pytest.mark.asyncio
    async def test_one_coordinator_update_calls_the_device_registry_once(self):
        """One update, one dr.async_get call, covering both the leftover
        derivation and the device-name resolution it now shares a fetch with."""
        harness = _Harness()

        with _patched_issue_registry():
            coordinator, _hass, _entry, _client = await _armed_install(harness)

            with harness.patched():
                harness.device_get_calls = 0
                await coordinator.async_refresh()

                assert harness.device_get_calls == 1


class TestTheScanDegradesRatherThanRaising:
    """It runs inside a coordinator listener, so nothing here may propagate."""

    def test_an_unreadable_entity_registry_yields_no_candidates(self):
        """Offering nothing is the safe answer for a surface that deletes."""
        harness = _Harness()
        harness.add_leftover_row()
        harness.entity_get_raises = True

        assert _derive(harness) == {}

    def test_an_unreadable_device_registry_yields_no_candidates(self):
        """Without it no row can be resolved to a key at all."""
        harness = _Harness()
        harness.add_leftover_row()
        harness.device_get_raises = True

        assert _derive(harness) == {}

    def test_a_malformed_device_row_does_not_abort_the_scan(self):
        """A row carrying no identifiers attribute at all is the shape that
        raises rather than answering None, and the row beside it must still
        resolve."""
        harness = _Harness(
            device_rows=[
                SimpleNamespace(id="device_missing_identifiers", config_entries=frozenset({ENTRY_ID})),
                _sub_device_row(),
            ]
        )
        harness.add_leftover_row()

        assert _derive(harness) == {SENSOR_KEY: frozenset({("sensor", LEFTOVER_UNIQUE_ID)})}

    def test_a_malformed_entity_row_does_not_abort_the_scan(self):
        """One row whose fields cannot be read must cost only that row."""

        class _ExplodingRow:
            """A registry row whose unique_id cannot be read at all."""

            entity_id = "sensor.exploding"
            device_id = SUB_DEVICE_ROW_ID
            disabled_by = None
            config_entry_id = ENTRY_ID

            @property
            def unique_id(self):
                """Raise the way an unexpected row shape would."""
                raise RuntimeError("unreadable row")

        harness = _Harness()
        harness.entity_rows.append(_ExplodingRow())
        harness.add_leftover_row()

        assert _derive(harness) == {SENSOR_KEY: frozenset({("sensor", LEFTOVER_UNIQUE_ID)})}

    def test_a_malformed_adder_does_not_abort_the_pair_index(self):
        """The other adders' recorded pairs must still be known, or their live
        rows would read as unrecorded and become candidates."""
        broken = SimpleNamespace()
        harness = _Harness()
        recorded = f"rainpoint_{SENSOR_KEY}_battery"
        harness.add_leftover_row(entity_id=f"sensor.{recorded}", unique_id=recorded)
        harness.add_leftover_row()

        assert _derive(harness, adders=[broken, _seed_adder()]) == {SENSOR_KEY: frozenset({("sensor", LEFTOVER_UNIQUE_ID)})}

    def test_the_pair_index_skips_only_the_adder_it_could_not_read(self):
        """The index in isolation, which is what the claim above rests on."""
        pairs = _ledger_pairs_by_key([SimpleNamespace(), _seed_adder()])

        assert pairs == {SENSOR_KEY: {("sensor", f"rainpoint_{SENSOR_KEY}_battery")}}


class TestTheOrderRowsArriveInDoesNotMatter:
    """The match is set membership, so the registry's own order is irrelevant."""

    def test_reversing_the_registry_rows_produces_identical_candidates(self):
        """Driven by reversing the row list rather than by asserting a sort,
        because a sort would prove only that this test sorted something."""
        forward = _Harness()
        for index in range(4):
            unique_id = f"rainpoint_{SENSOR_KEY}_dead{index}"
            forward.add_leftover_row(entity_id=f"sensor.{unique_id}", unique_id=unique_id)
        forward.add_row(f"sensor.rainpoint_{SENSOR_KEY}_battery", f"rainpoint_{SENSOR_KEY}_battery", state=_live_state())

        reverse = _Harness()
        reverse.entity_rows = list(reversed(forward.entity_rows))
        reverse.states = dict(forward.states)

        assert _derive(forward) == _derive(reverse)


class TestThePairWindow:
    """Each pair serves its own window, and a broken run starts a fresh one."""

    def test_a_pair_is_offered_only_once_it_has_served_the_whole_window(self):
        """The boundary, counted rather than assumed."""
        counts: dict = {}
        pairs = {SENSOR_KEY: frozenset({("sensor", LEFTOVER_UNIQUE_ID)})}

        for _ in range(LEFTOVER_ROW_DEBOUNCE_UPDATES - 1):
            assert _debounced_leftover_pairs(counts, pairs) == {}

        assert _debounced_leftover_pairs(counts, pairs) == pairs

    def test_a_pair_that_stops_qualifying_serves_a_fresh_window_when_it_returns(self):
        """A row that came back to life and died again is a new observation,
        not a resumption. Decrementing, or leaving the count in place, would
        offer it on the very next update after it died a second time."""
        counts: dict = {}
        pairs = {SENSOR_KEY: frozenset({("sensor", LEFTOVER_UNIQUE_ID)})}

        for _ in range(LEFTOVER_ROW_DEBOUNCE_UPDATES - 1):
            _debounced_leftover_pairs(counts, pairs)
        # One update in which the row is backed again drops the entry outright.
        assert _debounced_leftover_pairs(counts, {}) == {}
        assert counts == {}

        assert _debounced_leftover_pairs(counts, pairs) == {}

    def test_a_second_dead_row_on_the_same_key_serves_its_own_window(self):
        """Per pair rather than per key: a sibling's accumulated count must not
        carry a pair that first qualified today straight to the card."""
        counts: dict = {}
        first = ("sensor", LEFTOVER_UNIQUE_ID)
        second = ("sensor", f"rainpoint_{SENSOR_KEY}_stale")

        for _ in range(LEFTOVER_ROW_DEBOUNCE_UPDATES):
            _debounced_leftover_pairs(counts, {SENSOR_KEY: frozenset({first})})

        assert _debounced_leftover_pairs(counts, {SENSOR_KEY: frozenset({first, second})}) == {SENSOR_KEY: frozenset({first})}


class TestWhatTheConfirmMayTake:
    """The removal scope on this shape, and everything it must leave alone."""

    @staticmethod
    def _confirm(harness: _Harness, pairs, *, adders=None):
        """Run the removal executor for one key over a seeded install.

        Always in the still-present shape, which is the shape the card that
        produced these pairs carries. The shape is passed rather than left to
        be read off the pairs, exactly as the fix flow passes it.
        """
        adder = adders[0] if adders else _seed_adder()
        hass = SimpleNamespace(data={DOMAIN: {ENTRY_ID: {LATE_ADDER_STORE_KEY: [adder]}}}, states=harness.state_machine())
        entry = SimpleNamespace(entry_id=ENTRY_ID)
        with harness.patched():
            count = _remove_orphaned_key_rows(hass, entry, SENSOR_KEY, leftover_pairs=pairs, leftover_shape=True)
        return count, adder

    def test_exactly_the_named_pair_goes_and_the_device_row_stays(self):
        """The device is still on the account and still reporting, so releasing
        its device row would take a live device's page."""
        harness = _Harness()
        harness.add_leftover_row()
        harness.add_row(f"sensor.rainpoint_{SENSOR_KEY}_battery", f"rainpoint_{SENSOR_KEY}_battery", state=_live_state())

        count, adder = self._confirm(harness, frozenset({("sensor", LEFTOVER_UNIQUE_ID)}))

        assert count == 1
        assert harness.removed == [LEFTOVER_ENTITY_ID]
        assert harness.released == []
        assert adder.ledger.unique_ids_for(SENSOR_KEY) == frozenset({f"rainpoint_{SENSOR_KEY}_battery"})

    def test_only_the_exactly_matching_pair_goes_when_two_domains_share_one_id(self):
        """The adjacency case as a removal rather than as a derivation: naming
        one pair may never take the row in the other domain."""
        harness = _Harness()
        shared = f"rainpoint_{SENSOR_KEY}_shared"
        harness.add_leftover_row(entity_id=f"sensor.{shared}", unique_id=shared)
        harness.add_leftover_row(entity_id=f"binary_sensor.{shared}", unique_id=shared)

        count, _adder = self._confirm(harness, frozenset({("binary_sensor", shared)}))

        assert count == 1
        assert harness.removed == [f"binary_sensor.{shared}"]

    def test_an_empty_pair_set_removes_nothing_and_leaves_one_breadcrumb(self, caplog):
        """Home Assistant deletes a fixable issue once its flow finishes, so a
        confirm that resolved to nothing looks to the user exactly like a
        successful removal. The log line is the only trace left.

        On this shape an empty scope is the ordinary recovery outcome rather
        than a fault, so the ledger-derived scope is never consulted and the
        breadcrumb says nothing about removing rows by hand.
        """
        harness = _Harness()
        harness.add_leftover_row()
        seeded = _seed_adder()

        with caplog.at_level(logging.INFO, logger="custom_components.rainpoint"):
            count, adder = self._confirm(harness, frozenset(), adders=[seeded])

        assert count == 0
        assert harness.removed == []
        assert harness.released == []
        # The ledger is untouched: this shape never resolves through it.
        assert adder.ledger.unique_ids_for(SENSOR_KEY) == frozenset({f"rainpoint_{SENSOR_KEY}_battery"})
        breadcrumbs = [r.getMessage() for r in caplog.records if "Nothing is left over for sensor key" in r.getMessage()]
        assert len(breadcrumbs) == 1
        assert SENSOR_KEY in breadcrumbs[0]
        assert not [r.getMessage() for r in caplog.records if "Nothing in scope for sensor key" in r.getMessage()]

    def test_a_row_that_refuses_to_go_does_not_break_the_confirm(self):
        """This runs inside a Repairs flow step, so nothing may propagate, and
        the row that did go still counts."""
        harness = _Harness()
        harness.add_leftover_row()
        second = f"rainpoint_{SENSOR_KEY}_stale"
        harness.add_leftover_row(entity_id=f"sensor.{second}", unique_id=second)
        harness.remove_raises_for = {LEFTOVER_ENTITY_ID}

        count, adder = self._confirm(harness, frozenset({("sensor", LEFTOVER_UNIQUE_ID), ("sensor", second)}))

        assert count == 1
        assert harness.removed == [f"sensor.{second}"]
        assert adder.ledger.unique_ids_for(SENSOR_KEY) == frozenset({f"rainpoint_{SENSOR_KEY}_battery"})


class TestARecoveredCardTakesNothing:
    """The seam between the card and the confirm, driven end to end.

    The confirm re-derives its scope, so a row that came back to life between
    the raise and the Submit drops out of it. When every offered row comes
    back, the re-derived scope is empty, and the whole question is what an
    empty scope means on a device that is present and reporting. It means
    there is nothing left to take. Answering it any other way -- by reading
    the emptiness as the departed-key shape, whose empty scope resolves
    through the session's ledgers instead -- deletes every entity of a live
    device and releases its device row.
    """

    @pytest.mark.asyncio
    async def test_a_card_whose_rows_all_recovered_removes_nothing_at_all(self):
        """Raise, recover, confirm, in that order, on a device that never left.

        Driven as a timeline rather than by handing the executor an empty pair
        set, because the recovery is the whole point: the card has to be
        genuinely raised against a genuinely dead row, and that row has to come
        back through the same liveness gate the derivation reads, before the
        confirm is allowed to answer.
        """
        harness = _Harness()

        with _patched_issue_registry() as (create, _delete):
            coordinator, hass, _entry, _client = await _armed_install(harness)
            harness.add_leftover_row()
            ledger_before = _ledger_snapshot(hass)

            with harness.patched():
                for _ in range(LEFTOVER_ROW_DEBOUNCE_UPDATES):
                    await coordinator.async_refresh()
                assert create.call_count == 1
                kwargs = create.call_args.kwargs
                assert kwargs["data"]["leftover"] is True

                flow = await async_create_fix_flow(hass, create.call_args.args[2], kwargs["data"])
                flow.hass = hass

                # The one row the card offered is backed again by the time the
                # user presses Submit.
                harness.states[LEFTOVER_ENTITY_ID] = _live_state()
                registered = sorted(row.entity_id for row in harness.entity_rows)

                result = await flow.async_step_confirm({})

                assert result["type"] == "create_entry"
                # Nothing removed, including the row that was offered.
                assert harness.removed == []
                assert sorted(row.entity_id for row in harness.entity_rows) == registered
                # The device is present and reporting, so its row is not released.
                assert harness.released == []
                # No adder's bookkeeping moved either.
                assert _ledger_snapshot(hass) == ledger_before

    @pytest.mark.asyncio
    async def test_only_the_row_that_stayed_dead_goes_when_its_sibling_recovers(self):
        """The partial case, which is what keeps the empty case honest.

        Two rows are offered on one card and one of them comes back before the
        confirm. The survivor proves the re-derivation is still doing its work
        rather than the shape having been switched off wholesale, and the row
        that did go proves the scope is narrowed to it alone.
        """
        harness = _Harness()
        second_unique_id = f"rainpoint_{SENSOR_KEY}_stale"
        second_entity_id = f"sensor.{second_unique_id}"

        with _patched_issue_registry() as (create, _delete):
            coordinator, hass, _entry, _client = await _armed_install(harness)
            harness.add_leftover_row()
            harness.add_leftover_row(entity_id=second_entity_id, unique_id=second_unique_id)

            with harness.patched():
                for _ in range(LEFTOVER_ROW_DEBOUNCE_UPDATES):
                    await coordinator.async_refresh()
                kwargs = create.call_args.kwargs
                assert kwargs["translation_placeholders"]["entity_count"] == "2"

                flow = await async_create_fix_flow(hass, create.call_args.args[2], kwargs["data"])
                flow.hass = hass
                harness.states[LEFTOVER_ENTITY_ID] = _live_state()

                await flow.async_step_confirm({})

                assert harness.removed == [second_entity_id]
                assert harness.released == []


class TestTheTwoShapesAreMutuallyExclusive:
    """One key holds exactly one shape at every step of its whole life."""

    @pytest.mark.asyncio
    async def test_a_key_with_a_dead_row_that_later_departs_never_holds_both(self):
        """Driven from present-with-a-dead-row through departure to aged out.

        The leftover derivation requires the key to be in the current poll and
        the aged-out verdict requires it to have been absent for a whole window
        of them, so the two can never coincide. That is what lets one issue id
        serve both without a live card ever changing its body underneath the
        user.
        """
        harness = _Harness()

        with _patched_issue_registry() as (create, delete):
            coordinator, _hass, _entry, client = await _armed_install(harness)
            harness.add_leftover_row()

            with harness.patched():
                # Present, reporting, and carrying one dead row.
                for _ in range(LEFTOVER_ROW_DEBOUNCE_UPDATES):
                    await coordinator.async_refresh()
                assert create.call_count == 1
                assert create.call_args.kwargs["translation_key"] == LEFTOVER_ENTITIES_TRANSLATION_KEY
                issue_id = create.call_args.args[2]

                # The child leaves the enumeration. The key is no longer in the
                # poll, so it is no longer leftover, and it has not aged out
                # yet either: the card clears and nothing replaces it.
                client.get_devices_by_hid.return_value = _hub_record(with_child=False)
                await coordinator.async_refresh()
                assert create.call_count == 1
                assert any(call.args[2] == issue_id for call in delete.call_args_list)

                # It ages out, and the same id comes back carrying the other
                # shape's copy.
                for _ in range(ORPHANED_KEY_DEBOUNCE_POLLS):
                    await coordinator.async_refresh()

                assert create.call_count == 2
                assert create.call_args.args[2] == issue_id
                assert create.call_args.kwargs["translation_key"] == ORPHANED_ENTITIES_ISSUE_ID_PREFIX
                assert "leftover" not in create.call_args.kwargs["data"]


class TestTheNarrowedPropertiesHoldInSource:
    """The three properties this path was narrowed to, pinned as source facts.

    A behavioural suite proves each function still does what it does; it
    cannot prove that none of them grew a second reason nobody has exercised
    yet. These assertions fail in review rather than in production if one of
    the three is ever loosened.
    """

    # Every function this widening added or rewrote. The narrowing claims below
    # are asserted over exactly these and nothing else, so an unrelated helper
    # elsewhere in the module cannot satisfy or break them by accident.
    SCAN_FUNCTIONS = (
        _row_is_unbacked,
        _ledger_pairs_by_key,
        _build_leftover_row_pairs,
        _debounced_leftover_pairs,
        _leftover_pairs_now,
    )

    @staticmethod
    def _tree(func):
        """Parse one function's own source into an AST."""
        return ast.parse(textwrap.dedent(inspect.getsource(func)))

    def test_the_executor_has_exactly_one_entity_removal_call_site(self):
        """The widening reuses the existing removal loop rather than adding a
        second one. Two call sites is how one of them acquires a guard the
        other does not have."""
        source = inspect.getsource(_remove_orphaned_key_rows)

        assert source.count("registry.async_remove(") == 1

    def test_the_leftover_branch_reaches_neither_the_device_release_nor_a_forget(self):
        """Both would be wrong rather than merely unnecessary here: the device
        row still represents a device the current poll lists, and no pair on
        this branch is in any ledger, so a forget would release unique ids that
        live entities still hold.

        The guard is on the shape the caller named, never on whether the pair
        set it handed in happens to be empty."""
        function = self._tree(_remove_orphaned_key_rows).body[0]
        guards = [
            node for node in ast.walk(function) if isinstance(node, ast.If) and ast.unparse(node.test) == "not leftover_shape"
        ]
        assert len(guards) == 1

        guarded_nodes = set(map(id, ast.walk(guards[0])))
        outside = {
            ast.unparse(node.func) for node in ast.walk(function) if isinstance(node, ast.Call) and id(node) not in guarded_nodes
        }

        assert "_release_emptied_device_row" not in outside
        assert not [name for name in outside if name.endswith(".forget")]

    @pytest.mark.parametrize("func", SCAN_FUNCTIONS, ids=lambda f: f.__name__)
    def test_no_function_here_tests_a_unique_id_for_a_prefix_or_a_suffix(self, func):
        """Removal stays an exact pair list. The only two string tests allowed
        anywhere on this path are the generic-namespace containment check, which
        keeps this scan out of a namespace another sweep owns, and the zone
        exclusion, which can only ever remove a candidate."""
        tree = self._tree(func)
        attribute_calls = {
            ast.unparse(node.func)
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        }

        assert not [name for name in attribute_calls if name.endswith((".startswith", ".endswith"))]
        matches = {name for name in attribute_calls if name.endswith((".search", ".match", ".fullmatch"))}
        assert matches <= {"_ZONE_UNIQUE_ID_RE.search"}

        containments = {
            ast.unparse(node)
            for node in ast.walk(tree)
            if isinstance(node, ast.Compare) and any(isinstance(op, ast.In | ast.NotIn) for op in node.ops)
        }
        assert {test for test in containments if "unique_id" in test} <= {"GENERIC_UNIQUE_ID_MARKER in unique_id"}
