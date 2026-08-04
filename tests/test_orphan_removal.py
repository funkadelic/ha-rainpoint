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

import logging
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.rainpoint import (
    _build_orphaned_entity_records,
    _device_row_for_sensor_key,
    _device_row_is_empty,
    _read_aged_out_keys,
    _remove_orphaned_key_rows,
    _sync_orphaned_entity_issues,
    _sync_orphaned_entity_issues_on_updates,
    repairs,
)
from custom_components.rainpoint import coordinator as coordinator_module
from custom_components.rainpoint.const import (
    CONF_HIDS,
    DOMAIN,
    MODEL_MOISTURE_SIMPLE,
    MODEL_VALVE_245,
)
from custom_components.rainpoint.coordinator import (
    ORPHANED_KEY_DEBOUNCE_POLLS,
    RainPointCoordinator,
)
from custom_components.rainpoint.entity import (
    LATE_ADDER_STORE_KEY,
    EmittedEntityLedger,
    LateEntityAdder,
)
from custom_components.rainpoint.repairs import (
    async_create_fix_flow,
    orphaned_entities_issue_id,
)
from custom_components.rainpoint.sensor import async_setup_entry as sensor_async_setup_entry
from custom_components.rainpoint.valve import async_setup_entry as valve_async_setup_entry
from tests.helpers import VALVE_ZONES_TLV_PAYLOAD, make_valve_zone_status

HID = 100
MID = 200
ADDR = 1
SENSOR_KEY = f"{HID}_{MID}_{ADDR}"
ENTRY_ID = "e1"
FOREIGN_ENTRY_ID = "other_entry"

ZONE_1_UNIQUE_ID = f"rainpoint_{SENSOR_KEY}_zone1"
ZONE_2_UNIQUE_ID = f"rainpoint_{SENSOR_KEY}_zone2"

# (config entry id, entity_id, unique_id). Two rows this session's valve adder
# really emits, one same-entry row for a different sensor key, one row on this
# same entry in a different domain carrying the very same unique_id, and one
# row on a foreign config entry carrying it too. The last three are the
# blast-radius assertion: none may ever be removed.
#
# The sensor-domain row is the one registry uniqueness actually permits.
# Uniqueness is per (domain, platform, unique_id), so an id is only ever a
# partial identifier, and a sweep matching on the id alone would take this row
# as readily as the valve one that shares it.
_REGISTRY_ROWS = [
    (ENTRY_ID, "valve.zone1", ZONE_1_UNIQUE_ID),
    (ENTRY_ID, "valve.zone2", ZONE_2_UNIQUE_ID),
    (ENTRY_ID, "valve.unrelated", f"rainpoint_{HID}_{MID}_9_zone1"),
    (ENTRY_ID, "sensor.same_id_other_domain", ZONE_1_UNIQUE_ID),
    (FOREIGN_ENTRY_ID, "valve.foreign", ZONE_1_UNIQUE_ID),
]


def _hub_record(*, with_child: bool, model: str = MODEL_VALVE_245) -> list[dict]:
    """One real valve hub whose subDevices either lists its child or does not."""
    sub_devices = [{"addr": ADDR, "name": "Hub A", "model": model, "softVer": "127"}]
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


def _build_timeline(*, zones_reported: bool = True, model: str = MODEL_VALVE_245):
    """Return (coordinator, hass, entry, client) for one real valve hub."""
    client = AsyncMock()
    client.get_devices_by_hid.return_value = _hub_record(with_child=True, model=model)
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


def _capturing_add_entities_by_domain():
    """Return (captured, domains, make_add) tagging each id with its platform.

    Home Assistant builds an entity_id as <domain>.<object_id>, where the
    domain is the emitting platform's. A test double that cannot say which
    platform produced an entity cannot build a faithful entity_id, and the
    removal path matches on (domain, unique_id).
    """
    captured: list = []
    domains: dict[str, str] = {}

    def make_add(domain):
        """Return an async_add_entities that tags what it captures."""

        def _add(ents, **kw):
            for entity in ents:
                unique_id = getattr(entity, "_attr_unique_id", None)
                if unique_id is not None:
                    domains[unique_id] = domain
                captured.append(entity)

        return MagicMock(side_effect=_add)

    return captured, domains, make_add


def _ledger_entity(unique_id):
    """A stand-in carrying only the attribute the ledger records."""
    return SimpleNamespace(_attr_unique_id=unique_id)


@contextmanager
def _patched_issue_registry():
    """Patch the issue registry so create and delete really change what it holds.

    The three functions are one mechanism. Mocking the two writers while
    leaving the reader answering an empty registry would make every raise look
    like a card Home Assistant had already deleted, and the manager reconciles
    its dedup against that reader precisely because Home Assistant deletes a
    fixable issue itself when its flow finishes. So the double has to model the
    registry rather than only record calls.

    Yields the (create, delete) mocks, which still record every call.
    """
    held: dict[tuple[str, str], object] = {}

    def _create(hass, domain, issue_id, **kwargs):
        """Record the raised card the way the registry would hold it."""
        held[(domain, issue_id)] = SimpleNamespace(translation_placeholders=kwargs.get("translation_placeholders"))

    def _delete(hass, domain, issue_id):
        """Drop a card, and stay a no-op for an id the registry never held."""
        held.pop((domain, issue_id), None)

    registry = SimpleNamespace(async_get_issue=lambda domain, issue_id: held.get((domain, issue_id)))

    with (
        patch.object(repairs.ir, "async_create_issue", side_effect=_create) as create,
        patch.object(repairs.ir, "async_delete_issue", side_effect=_delete) as delete,
        patch.object(repairs.ir, "async_get", return_value=registry),
    ):
        yield create, delete


class TestOrphanedKeyEndToEnd:
    """A vanished key becomes one confirmable card that removes its own rows."""

    @pytest.mark.asyncio
    async def test_vanished_key_ages_out_into_one_card_whose_confirm_removes_its_rows(self):
        """The whole path, driven in the order a real install runs it."""
        coordinator, hass, entry, client = _build_timeline()
        removed, async_get, async_entries = _make_entity_registry()
        captured, async_add_entities = _capturing_add_entities()

        with _patched_issue_registry() as (create, _delete):
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
    async def test_a_confirm_that_removed_nothing_gets_its_card_back_on_the_next_poll(self):
        """A confirm can reach the executor and remove nothing: the entity
        registry lookup can fail, and the entry store can be unreadable. Home
        Assistant deletes a fixable issue itself whenever its flow finishes, so
        the card goes either way, and the user is left with the same stranded
        entities and no surface to act on unless the next sweep raises again.

        Driven rather than asserted on an end state, because the whole property
        is an ordering one: raise, submit, no-op, delete, and the very next
        poll has to produce the card again."""
        coordinator, hass, entry, client = _build_timeline()
        _captured, async_add_entities = _capturing_add_entities()

        with _patched_issue_registry() as (create, _delete):
            await coordinator.async_config_entry_first_refresh()
            _sync_orphaned_entity_issues_on_updates(hass, entry, coordinator)
            await valve_async_setup_entry(hass, entry, async_add_entities)

            client.get_devices_by_hid.return_value = _hub_record(with_child=False)
            for _ in range(ORPHANED_KEY_DEBOUNCE_POLLS):
                await coordinator.async_refresh()
            assert create.call_count == 1
            issue_id = create.call_args.args[2]

            flow = await async_create_fix_flow(hass, issue_id, create.call_args.kwargs["data"])
            flow.hass = hass

            # The confirm reaches the executor, whose entity registry lookup
            # fails, so it removes nothing and forgets nothing.
            with patch("custom_components.rainpoint.er.async_get", side_effect=RuntimeError("registry down")):
                result = await flow.async_step_confirm({})
            assert result["type"] == "create_entry"

            # Home Assistant's own repairs flow manager deletes the issue on
            # any non-abort result, which this integration never sees and
            # cannot prevent. Modelled here because that deletion is the whole
            # premise of the defect.
            repairs.ir.async_delete_issue(hass, DOMAIN, issue_id)

            await coordinator.async_refresh()

            assert create.call_count == 2
            assert create.call_args.args[2] == issue_id

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


class TestOrphanedCardLifecycle:
    """A card only ever appears for a sustained absence, and clears itself."""

    @staticmethod
    async def _armed_timeline(model: str = MODEL_VALVE_245):
        """Drive construct -> first refresh -> sweep armed -> platform setup.

        The shared preamble of every lifecycle test, in the order a real
        install runs it. Callers drive their own polls off the returned client
        and coordinator and assert between them.
        """
        coordinator, hass, entry, client = _build_timeline(model=model)
        _captured, async_add_entities = _capturing_add_entities()

        await coordinator.async_config_entry_first_refresh()
        _sync_orphaned_entity_issues_on_updates(hass, entry, coordinator)
        await valve_async_setup_entry(hass, entry, async_add_entities)
        return coordinator, hass, entry, client

    @pytest.mark.asyncio
    async def test_a_hub_that_enumerates_nothing_says_so_on_the_poll_it_starts_counting(self, caplog):
        """A hub that is present but lists no sub-devices is trusted from the
        first poll: it is in neither the missing nor the provisional hub set,
        so the hub-outage freeze never reaches it and its children all start
        counting at once. That shape is a real unpair-everything and a real
        partial-degradation response from the device list, and the integration
        cannot tell them apart, so the case has to be visible in the log well
        before the cards appear."""
        coordinator, _hass, _entry, client = await self._armed_timeline()

        client.get_devices_by_hid.return_value = _hub_record(with_child=False)
        with caplog.at_level(logging.WARNING, logger="custom_components.rainpoint.coordinator"):
            await coordinator.async_refresh()

        assert coordinator._orphaned_key_poll_counts[SENSOR_KEY] == 1
        assert [r.getMessage() for r in caplog.records if "is listed but enumerates no sub-devices" in r.getMessage()]

    @pytest.mark.asyncio
    async def test_a_hub_that_keeps_enumerating_nothing_is_warned_about_once(self, caplog):
        """The warning is a breadcrumb for an edge, not a per-poll readout.

        The state it reports is permanent once it holds: the enumeration
        memory only ever grows and a present hub is never frozen, so every one
        of its keys stays counted on every later poll, including long after the
        user confirmed the removal. Warning on the state rather than on the
        transition put one line per hub in the log every two minutes for the
        life of the session, which a one-poll assertion cannot see.
        """
        coordinator, _hass, _entry, client = await self._armed_timeline()

        client.get_devices_by_hid.return_value = _hub_record(with_child=False)
        with caplog.at_level(logging.WARNING, logger="custom_components.rainpoint.coordinator"):
            for _ in range(ORPHANED_KEY_DEBOUNCE_POLLS + 5):
                await coordinator.async_refresh()

        # The key really was counted on every one of those polls, so the single
        # line is a gate rather than the counting having stopped.
        assert coordinator._orphaned_key_poll_counts[SENSOR_KEY] == ORPHANED_KEY_DEBOUNCE_POLLS + 5
        assert len([r for r in caplog.records if "enumerates no sub-devices" in r.getMessage()]) == 1

    @pytest.mark.asyncio
    async def test_a_hub_that_recovers_and_degrades_again_is_warned_about_again(self):
        """The gate re-arms, so a second degradation gets its own breadcrumb
        rather than being swallowed by the first one's mark."""
        coordinator, _hass, _entry, client = await self._armed_timeline()

        with patch.object(coordinator_module._LOGGER, "warning") as warned:
            client.get_devices_by_hid.return_value = _hub_record(with_child=False)
            await coordinator.async_refresh()
            await coordinator.async_refresh()
            assert len([call for call in warned.call_args_list if "enumerates no sub-devices" in call.args[0]]) == 1

            client.get_devices_by_hid.return_value = _hub_record(with_child=True)
            await coordinator.async_refresh()

            client.get_devices_by_hid.return_value = _hub_record(with_child=False)
            await coordinator.async_refresh()
            await coordinator.async_refresh()

        assert len([call for call in warned.call_args_list if "enumerates no sub-devices" in call.args[0]]) == 2

    @pytest.mark.asyncio
    async def test_a_hub_that_still_enumerates_its_children_stays_quiet(self):
        """The control: the warning must not fire on an ordinary poll, or it
        would be one line per hub per two minutes on every healthy install."""
        coordinator, _hass, _entry, _client = await self._armed_timeline()

        with patch.object(coordinator_module._LOGGER, "warning") as warned:
            await coordinator.async_refresh()

        assert not [call for call in warned.call_args_list if "enumerates no sub-devices" in call.args[0]]
        assert coordinator._orphaned_key_poll_counts == {}

    @pytest.mark.asyncio
    async def test_a_hub_that_enumerates_nothing_and_then_recovers_costs_the_key_nothing(self):
        """This is why the empty enumeration is counted rather than frozen, and
        it is the whole defence, so it is pinned rather than left implicit.

        A guard refusing to ever trust an empty enumeration was considered and
        declined. It would have cost the surface every hub whose only
        sub-device is unpaired, which for a one-valve install is the entire
        feature, and it would have bought protection this reset already
        provides: a degraded device-list response has to stay empty for
        ORPHANED_KEY_DEBOUNCE_POLLS consecutive polls, around an hour, before
        any card can appear. The transient partial response that motivated the
        hub-level absence window is a one-poll shrink, and one poll costs
        nothing here.
        """
        coordinator, _hass, _entry, client = await self._armed_timeline()

        client.get_devices_by_hid.return_value = _hub_record(with_child=False)
        for _ in range(ORPHANED_KEY_DEBOUNCE_POLLS - 1):
            await coordinator.async_refresh()
        assert coordinator._orphaned_key_poll_counts[SENSOR_KEY] == ORPHANED_KEY_DEBOUNCE_POLLS - 1

        client.get_devices_by_hid.return_value = _hub_record(with_child=True)
        await coordinator.async_refresh()

        assert SENSOR_KEY not in coordinator._orphaned_key_poll_counts
        assert coordinator.aged_out_sensor_keys() == frozenset()

        # And the count really restarts from zero rather than resuming, so a
        # second degradation gets the full window again.
        client.get_devices_by_hid.return_value = _hub_record(with_child=False)
        await coordinator.async_refresh()
        assert coordinator._orphaned_key_poll_counts[SENSOR_KEY] == 1

    @pytest.mark.asyncio
    async def test_a_key_that_returns_below_the_threshold_never_raises_a_card(self):
        """A shrunken poll that reverses must not be visible to the user at
        all, which is the whole reason the window is polls rather than one."""
        coordinator, _hass, _entry, client = await self._armed_timeline()
        removed, async_get, async_entries = _make_entity_registry()

        with (
            _patched_issue_registry() as (create, _delete),
            patch("custom_components.rainpoint.er.async_get", side_effect=async_get),
            patch("custom_components.rainpoint.er.async_entries_for_config_entry", side_effect=async_entries),
        ):
            client.get_devices_by_hid.return_value = _hub_record(with_child=False)
            for _ in range(ORPHANED_KEY_DEBOUNCE_POLLS - 1):
                await coordinator.async_refresh()
            assert create.call_count == 0

            client.get_devices_by_hid.return_value = _hub_record(with_child=True)
            await coordinator.async_refresh()

            # The counter restarts from zero on any reappearance, so the next
            # absence starts a fresh window rather than resuming this one.
            assert coordinator._orphaned_key_poll_counts == {}
            assert create.call_count == 0

        assert removed == []

    @pytest.mark.asyncio
    async def test_a_key_that_returns_after_the_card_clears_it_without_deleting(self):
        """A user who has not acted yet must not be left holding a card that
        claims a currently-reporting device is orphaned."""
        coordinator, _hass, _entry, client = await self._armed_timeline()
        removed, async_get, async_entries = _make_entity_registry()

        with (
            _patched_issue_registry() as (create, delete),
            patch("custom_components.rainpoint.er.async_get", side_effect=async_get),
            patch("custom_components.rainpoint.er.async_entries_for_config_entry", side_effect=async_entries),
        ):
            client.get_devices_by_hid.return_value = _hub_record(with_child=False)
            for _ in range(ORPHANED_KEY_DEBOUNCE_POLLS):
                await coordinator.async_refresh()
            assert create.call_count == 1
            raised_id = create.call_args.args[2]

            client.get_devices_by_hid.return_value = _hub_record(with_child=True)
            delete.reset_mock()
            await coordinator.async_refresh()

            # Filtered on this card's own id: the not-reporting manager shares
            # this patched delete and clears its own issue on the same poll.
            cleared = [call.args[2] for call in delete.call_args_list if call.args[2] == raised_id]
            assert cleared == [raised_id]
            assert create.call_count == 1

        assert removed == []

    @pytest.mark.asyncio
    async def test_a_key_no_adder_recorded_anything_for_never_gets_a_card(self):
        """The blast-radius limit as an observable: the moisture sensor's key
        ages out exactly the same way, but the valve adder emitted nothing for
        it, so there is no row to offer and therefore no card."""
        coordinator, _hass, _entry, client = await self._armed_timeline(model=MODEL_MOISTURE_SIMPLE)

        with _patched_issue_registry() as (create, _delete):
            client.get_devices_by_hid.return_value = _hub_record(with_child=False, model=MODEL_MOISTURE_SIMPLE)
            for _ in range(ORPHANED_KEY_DEBOUNCE_POLLS + 2):
                await coordinator.async_refresh()

            # The coordinator's own verdict is unchanged: it counts keys, not
            # entities, and knows nothing about what any adder emitted.
            assert SENSOR_KEY in coordinator.aged_out_sensor_keys()
            assert create.call_count == 0


class _BrokenAdder:
    """An adder whose ledger raises, standing in for a malformed platform."""

    @property
    def ledger(self):
        """Fail the way a half-constructed adder would."""
        raise RuntimeError("no ledger")

    def forget(self, key):
        """Fail on the forget half too."""
        raise RuntimeError("cannot forget")


class _UnresolvableAdder:
    """An adder the resolve half cannot read, but whose forget would succeed.

    The asymmetry _BrokenAdder cannot express. That one raises on both halves,
    so a forget loop carrying no memory of which adders the resolve loop
    skipped still looks correct against it: its forget raises anyway and the
    per-adder guard swallows it. Here the forget would succeed, which is the
    only shape that can show ids being released for rows that were never even
    candidates for removal.
    """

    def __init__(self):
        """Hold one recorded id and a log of the forgets that reached it."""
        self.ledger = EmittedEntityLedger()
        self.ledger.record(SENSOR_KEY, {}, [_ledger_entity(ZONE_2_UNIQUE_ID)])
        self.forgotten: list[str] = []

    @property
    def domain(self):
        """Fail the way a half-constructed adder would."""
        raise RuntimeError("no domain")

    def forget(self, key):
        """Record that the sweep released this adder's bookkeeping."""
        self.forgotten.append(key)


class _UnforgettableAdder:
    """An adder that resolves cleanly and then raises on the forget.

    The other half of the per-adder discipline: a failure to release one
    adder's bookkeeping must not stop the adder beside it releasing its own.
    """

    def __init__(self):
        """Hold one recorded id under a real domain, so the resolve succeeds."""
        self.ledger = EmittedEntityLedger()
        self.ledger.record(SENSOR_KEY, {}, [_ledger_entity(ZONE_2_UNIQUE_ID)])
        self.domain = "valve"

    def forget(self, key):
        """Fail the way a half-torn-down adder would."""
        raise RuntimeError("cannot forget")


class TestOrphanedSweepGuards:
    """Every read on this path degrades rather than raising.

    The sweep runs inside a coordinator listener and the remover runs inside a
    Repairs flow step, so an exception in either breaks something much larger
    than the read that produced it.
    """

    def test_a_coordinator_without_the_accessor_offers_nothing(self):
        """Coordinator stand-ins are common on this path, and one predating
        the accessor must not raise into a listener."""
        assert _read_aged_out_keys(SimpleNamespace()) == frozenset()

    def test_a_coordinator_answering_with_a_non_iterable_offers_nothing(self):
        """The other shape of the same failure, caught at the same place
        rather than at some later and less obvious point."""
        assert _read_aged_out_keys(SimpleNamespace(aged_out_sensor_keys=lambda: 1)) == frozenset()

    def test_no_coordinator_at_all_offers_nothing(self):
        """A setup that never got as far as building one."""
        assert _read_aged_out_keys(None) == frozenset()

    def test_one_malformed_adder_does_not_abort_the_record_build(self):
        """The other platforms' keys must still be reconciled."""
        coordinator = SimpleNamespace(data={"sensors": {}})
        good = LateEntityAdder(coordinator, lambda ents: None, lambda k, i: [_ledger_entity("z1")], "valve")
        good.collect(SENSOR_KEY, {"addr": ADDR, "model": "M", "sub_name": "S", "hub_name": "H"})
        store = {LATE_ADDER_STORE_KEY: [_BrokenAdder(), good]}

        records = _build_orphaned_entity_records(store, ENTRY_ID, frozenset({SENSOR_KEY}))

        assert [(r.sensor_key, r.entity_count, r.orphaned) for r in records] == [(SENSOR_KEY, 1, True)]

    def test_a_manager_that_raises_leaves_every_card_alone(self):
        """The outer guard, which is what keeps this off the update path's
        error surface entirely."""
        manager = MagicMock()
        manager.async_sync.side_effect = RuntimeError("registry down")
        hass = SimpleNamespace(data={DOMAIN: {ENTRY_ID: {}}})
        entry = SimpleNamespace(entry_id=ENTRY_ID)

        _sync_orphaned_entity_issues(hass, entry, None, manager)

    def test_an_unwritable_entry_store_still_arms_the_listener(self):
        """A config entry whose store cannot be written loses its removal
        executor, and that is all: the cards still reconcile."""
        coordinator = MagicMock()
        hass = SimpleNamespace(data={})
        entry = MagicMock()
        entry.entry_id = ENTRY_ID

        _sync_orphaned_entity_issues_on_updates(hass, entry, coordinator)

        coordinator.async_add_listener.assert_called_once()

    def test_the_listener_reconciles_on_every_update(self):
        """Both the raise and the clear are idempotent, so there is no arming
        gate and no update this sweep deliberately sits out."""
        coordinator = MagicMock()
        hass = SimpleNamespace(data={DOMAIN: {ENTRY_ID: {}}})
        entry = MagicMock()
        entry.entry_id = ENTRY_ID

        with patch("custom_components.rainpoint.RainPointOrphanedEntityIssues") as manager_cls:
            _sync_orphaned_entity_issues_on_updates(hass, entry, coordinator)
            listener = coordinator.async_add_listener.call_args.args[0]
            listener()
            listener()

        assert manager_cls.return_value.async_sync.call_count == 3

    def test_the_cards_are_withdrawn_when_the_config_entry_unloads(self):
        """A card that outlives a reload can never be cleared again, because
        every structure that could clear it is rebuilt empty and a departed key
        can never be mentioned by a fresh record."""
        coordinator = MagicMock()
        hass = SimpleNamespace(data={DOMAIN: {ENTRY_ID: {}}})
        entry = MagicMock()
        entry.entry_id = ENTRY_ID

        with patch("custom_components.rainpoint.RainPointOrphanedEntityIssues") as manager_cls:
            _sync_orphaned_entity_issues_on_updates(hass, entry, coordinator)
            manager_cls.return_value.async_clear_all.assert_not_called()

            # The unload hook is the second thing registered, after the
            # listener's own remover.
            for call in entry.async_on_unload.call_args_list:
                call.args[0]()

        manager_cls.return_value.async_clear_all.assert_called_once()

    def test_a_withdrawal_that_raises_does_not_block_the_unload(self, caplog):
        """Everything registered after this hook still has to be torn down."""
        coordinator = MagicMock()
        hass = SimpleNamespace(data={DOMAIN: {ENTRY_ID: {}}})
        entry = MagicMock()
        entry.entry_id = ENTRY_ID

        with patch("custom_components.rainpoint.RainPointOrphanedEntityIssues") as manager_cls:
            manager_cls.return_value.async_clear_all.side_effect = RuntimeError("registry down")
            _sync_orphaned_entity_issues_on_updates(hass, entry, coordinator)

            with caplog.at_level(logging.DEBUG, logger="custom_components.rainpoint"):
                for call in entry.async_on_unload.call_args_list:
                    call.args[0]()

        assert [r.getMessage() for r in caplog.records if "Could not withdraw the orphaned entity cards" in r.getMessage()]

    def test_an_unreadable_entry_store_removes_nothing(self):
        """The remover's first guard, reached by a flow submitted after its
        config entry unloaded."""
        hass = SimpleNamespace(data={})
        entry = SimpleNamespace(entry_id=ENTRY_ID)

        assert _remove_orphaned_key_rows(hass, entry, SENSOR_KEY) == 0

    def test_a_card_with_nothing_in_scope_says_so_rather_than_returning_silently(self, caplog):
        """Home Assistant deletes a fixable issue once its flow finishes, so a
        confirm with no ledger entry behind it looks to the user exactly like a
        successful removal. The log line is the only breadcrumb left."""
        coordinator = SimpleNamespace(data={"sensors": {}})
        adder = LateEntityAdder(coordinator, lambda ents: None, lambda k, i: [], "valve")
        hass = SimpleNamespace(data={DOMAIN: {ENTRY_ID: {LATE_ADDER_STORE_KEY: [adder]}}})
        entry = SimpleNamespace(entry_id=ENTRY_ID)

        with caplog.at_level(logging.WARNING, logger="custom_components.rainpoint"):
            assert _remove_orphaned_key_rows(hass, entry, SENSOR_KEY) == 0

        assert [r.getMessage() for r in caplog.records if "Nothing in scope for sensor key" in r.getMessage()]

    def test_one_malformed_adder_does_not_stop_the_others_being_resolved(self):
        """The per-adder guard on the resolve half, and on the forget half.

        _BrokenAdder fails the resolve and is therefore skipped by both loops.
        _UnforgettableAdder resolves cleanly and raises only on the forget,
        which must leave the adder beside it releasing its own bookkeeping.
        """
        removed, async_get, async_entries = _make_entity_registry()
        coordinator = SimpleNamespace(data={"sensors": {}})
        good = LateEntityAdder(coordinator, lambda ents: None, lambda k, i: [_ledger_entity(ZONE_1_UNIQUE_ID)], "valve")
        good.collect(SENSOR_KEY, {})
        stubborn = _UnforgettableAdder()
        hass = SimpleNamespace(data={DOMAIN: {ENTRY_ID: {LATE_ADDER_STORE_KEY: [_BrokenAdder(), stubborn, good]}}})
        entry = SimpleNamespace(entry_id=ENTRY_ID)

        with (
            patch("custom_components.rainpoint.er.async_get", side_effect=async_get),
            patch("custom_components.rainpoint.er.async_entries_for_config_entry", side_effect=async_entries),
        ):
            assert _remove_orphaned_key_rows(hass, entry, SENSOR_KEY) == 2

        assert removed == ["valve.zone1", "valve.zone2"]
        assert good.ledger.unique_ids_for(SENSOR_KEY) == frozenset()

    def test_an_adder_the_resolve_half_skipped_is_never_forgotten(self):
        """The two loops have to agree about which adders they skipped.

        An adder that could not be read while resolving contributed nothing to
        the doomed set, so not one of its rows was ever a candidate and every
        one of them is still registered holding its unique_id. Forgetting it
        anyway would release those ids, which is the very state the removal
        guard exists to prevent, arriving through the resolve guard instead.
        """
        removed, async_get, async_entries = _make_entity_registry()
        coordinator = SimpleNamespace(data={"sensors": {}})
        good = LateEntityAdder(coordinator, lambda ents: None, lambda k, i: [_ledger_entity(ZONE_1_UNIQUE_ID)], "valve")
        good.collect(SENSOR_KEY, {})
        unresolvable = _UnresolvableAdder()
        hass = SimpleNamespace(data={DOMAIN: {ENTRY_ID: {LATE_ADDER_STORE_KEY: [unresolvable, good]}}})
        entry = SimpleNamespace(entry_id=ENTRY_ID)

        with (
            patch("custom_components.rainpoint.er.async_get", side_effect=async_get),
            patch("custom_components.rainpoint.er.async_entries_for_config_entry", side_effect=async_entries),
        ):
            assert _remove_orphaned_key_rows(hass, entry, SENSOR_KEY) == 1

        # Its id was never in scope, so its row survives untouched.
        assert removed == ["valve.zone1"]
        assert unresolvable.forgotten == []
        assert unresolvable.ledger.unique_ids_for(SENSOR_KEY) == frozenset({ZONE_2_UNIQUE_ID})
        # The readable adder is unaffected by its neighbour's failure.
        assert good.ledger.unique_ids_for(SENSOR_KEY) == frozenset()

    def test_a_row_in_another_domain_sharing_the_unique_id_is_left_alone(self):
        """Entity registry uniqueness is per (domain, platform, unique_id), so
        the same unique_id may legitimately exist in two domains and an id on
        its own does not identify a row.

        The valve adder recorded this id, so a sweep matching on the id alone
        would take the sensor-domain row carrying it as readily as the valve
        one, destroying an entity no adder ever emitted along with its recorder
        history. The seeded rows include exactly that pair.
        """
        coordinator = SimpleNamespace(data={"sensors": {}})
        adder = LateEntityAdder(coordinator, lambda ents: None, lambda k, i: [_ledger_entity(ZONE_1_UNIQUE_ID)], "valve")
        adder.collect(SENSOR_KEY, {})
        hass = SimpleNamespace(data={DOMAIN: {ENTRY_ID: {LATE_ADDER_STORE_KEY: [adder]}}})
        entry = SimpleNamespace(entry_id=ENTRY_ID)
        removed, async_get, async_entries = _make_entity_registry()

        with (
            patch("custom_components.rainpoint.er.async_get", side_effect=async_get),
            patch("custom_components.rainpoint.er.async_entries_for_config_entry", side_effect=async_entries),
        ):
            assert _remove_orphaned_key_rows(hass, entry, SENSOR_KEY) == 1

        assert removed == ["valve.zone1"]
        assert "sensor.same_id_other_domain" not in removed

    def test_an_unreadable_registry_removes_nothing_and_forgets_nothing(self):
        """Forgetting without removing would leave a key able to re-offer a
        unique_id whose row still exists."""
        coordinator = SimpleNamespace(data={"sensors": {}})
        adder = LateEntityAdder(coordinator, lambda ents: None, lambda k, i: [_ledger_entity(ZONE_1_UNIQUE_ID)], "valve")
        adder.collect(SENSOR_KEY, {})
        hass = SimpleNamespace(data={DOMAIN: {ENTRY_ID: {LATE_ADDER_STORE_KEY: [adder]}}})
        entry = SimpleNamespace(entry_id=ENTRY_ID)

        with patch("custom_components.rainpoint.er.async_get", side_effect=RuntimeError("no registry")):
            assert _remove_orphaned_key_rows(hass, entry, SENSOR_KEY) == 0

        assert adder.ledger.unique_ids_for(SENSOR_KEY) == frozenset({ZONE_1_UNIQUE_ID})

    def test_a_row_that_cannot_be_removed_is_skipped_and_keeps_the_bookkeeping(self, caplog):
        """Per-row guarding, matching the generic sweep's shape, plus the
        condition that guarding puts on the forget.

        A row whose removal raised is still registered and still holds its
        unique_id. Both adders' add-once bookkeeping is per key, not per id, so
        there is no partial forget to make: releasing the key would let a
        returning device offer that live unique_id a second time, which Home
        Assistant rejects and which the never-offer-twice property exists
        precisely to prevent."""
        coordinator = SimpleNamespace(data={"sensors": {}})
        adder = LateEntityAdder(
            coordinator,
            lambda ents: None,
            lambda k, i: [_ledger_entity(ZONE_1_UNIQUE_ID), _ledger_entity(ZONE_2_UNIQUE_ID)],
            "valve",
        )
        adder.collect(SENSOR_KEY, {})
        hass = SimpleNamespace(data={DOMAIN: {ENTRY_ID: {LATE_ADDER_STORE_KEY: [adder]}}})
        entry = SimpleNamespace(entry_id=ENTRY_ID)

        class _StubbornRegistry:
            """Refuses one row and accepts the other."""

            def __init__(self):
                self.removed = []

            def async_remove(self, entity_id):
                """Fail on the first row only."""
                if entity_id == "valve.zone1":
                    raise RuntimeError("row is busy")
                self.removed.append(entity_id)

        registry = _StubbornRegistry()
        rows = [
            SimpleNamespace(entity_id="valve.zone1", unique_id=ZONE_1_UNIQUE_ID),
            SimpleNamespace(entity_id="valve.zone2", unique_id=ZONE_2_UNIQUE_ID),
        ]

        with (
            patch("custom_components.rainpoint.er.async_get", return_value=registry),
            patch("custom_components.rainpoint.er.async_entries_for_config_entry", return_value=rows),
            caplog.at_level(logging.WARNING, logger="custom_components.rainpoint"),
        ):
            assert _remove_orphaned_key_rows(hass, entry, SENSOR_KEY) == 1

        assert registry.removed == ["valve.zone2"]
        # Held, whole. The cost is that a returning key gains nothing until a
        # reload, which is recoverable; the cost of releasing is a unique_id
        # collision Home Assistant answers by dropping the new entity, which is
        # not recoverable short of a restart.
        assert adder.ledger.unique_ids_for(SENSOR_KEY) == frozenset({ZONE_1_UNIQUE_ID, ZONE_2_UNIQUE_ID})
        assert ZONE_1_UNIQUE_ID in adder._emitted
        assert ZONE_2_UNIQUE_ID in adder._emitted
        assert [r.getMessage() for r in caplog.records if "Kept the bookkeeping for sensor key" in r.getMessage()]


SUB_DEVICE_ROW_ID = "device_sub_1"

# Four unique_ids one adder emitted for SENSOR_KEY this session, all landing on
# the same sub-device row. Four rather than two so "removed every row this key
# names" cannot be satisfied by a partial sweep.
EMITTED_UNIQUE_IDS = tuple(f"rainpoint_{SENSOR_KEY}_zone{zone}" for zone in (1, 2, 3, 4))

EMITTED_ENTITY_ROWS = tuple(
    SimpleNamespace(
        config_entry_id=ENTRY_ID,
        entity_id=f"valve.emitted_zone{zone}",
        unique_id=f"rainpoint_{SENSOR_KEY}_zone{zone}",
        device_id=SUB_DEVICE_ROW_ID,
    )
    for zone in (1, 2, 3, 4)
)

# A row on the same device that no adder emitted: a manually created template
# entity, a row from a previous unique_id shape, anything at all. Its presence
# is what must keep the device row intact.
UNEMITTED_SAME_ENTRY_ROW = SimpleNamespace(
    config_entry_id=ENTRY_ID,
    entity_id="sensor.left_behind",
    unique_id="rainpoint_something_this_session_never_emitted",
    device_id=SUB_DEVICE_ROW_ID,
)

# The same row, on a different config entry. The device-row emptiness test is
# scoped to this entry, so this one is not in the candidate set at all.
UNEMITTED_FOREIGN_ENTRY_ROW = SimpleNamespace(
    config_entry_id=FOREIGN_ENTRY_ID,
    entity_id="sensor.left_behind_elsewhere",
    unique_id="rainpoint_something_this_session_never_emitted",
    device_id=SUB_DEVICE_ROW_ID,
)

SUB_DEVICE_ROW = SimpleNamespace(
    id=SUB_DEVICE_ROW_ID,
    identifiers={(DOMAIN, SENSOR_KEY)},
    config_entries=frozenset({ENTRY_ID}),
)
UNRELATED_DEVICE_ROW = SimpleNamespace(
    id="device_sub_9",
    identifiers={(DOMAIN, f"{HID}_{MID}_9")},
    config_entries=frozenset({ENTRY_ID}),
)
MALFORMED_DEVICE_ROW = SimpleNamespace(
    id="device_malformed",
    identifiers="not-a-set-of-tuples",
    config_entries=frozenset({ENTRY_ID}),
)
NO_DOMAIN_DEVICE_ROW = SimpleNamespace(
    id="device_no_domain",
    identifiers={("other_integration", SENSOR_KEY)},
    config_entries=frozenset({ENTRY_ID}),
)
# No identifiers attribute at all, which is the one shape that raises inside
# _domain_sensor_key rather than answering None.
MISSING_IDENTIFIERS_DEVICE_ROW = SimpleNamespace(
    id="device_missing_identifiers",
    config_entries=frozenset({ENTRY_ID}),
)
# Carries the very same DOMAIN identifier on a foreign config entry. The
# device-registry fetch is entry scoped, so it is never a candidate.
FOREIGN_DEVICE_ROW = SimpleNamespace(
    id="device_foreign",
    identifiers={(DOMAIN, SENSOR_KEY)},
    config_entries=frozenset({FOREIGN_ENTRY_ID}),
)
# One row carrying both config entries, which is what Home Assistant builds
# when two RainPoint entries -- two accounts resolving the same invited home --
# claim the same (DOMAIN, sensor_key) identifier. This is the shape the
# unscoped device removal would have cascaded through.
SHARED_SUB_DEVICE_ROW = SimpleNamespace(
    id=SUB_DEVICE_ROW_ID,
    identifiers={(DOMAIN, SENSOR_KEY)},
    config_entries=frozenset({ENTRY_ID, FOREIGN_ENTRY_ID}),
)

# Ordered so every skip-and-continue guard is exercised before the matching row
# is reached, rather than the match being the first thing the search sees.
DEFAULT_DEVICE_ROWS = (
    MALFORMED_DEVICE_ROW,
    NO_DOMAIN_DEVICE_ROW,
    MISSING_IDENTIFIERS_DEVICE_ROW,
    UNRELATED_DEVICE_ROW,
    SUB_DEVICE_ROW,
    FOREIGN_DEVICE_ROW,
)


def _make_device_registry(rows, *, get_raises=False, remove_raises=False):
    """Return (events, async_get, async_entries) over seeded device rows.

    Rows are re-derived per call and the config-entry scope is honoured, so a
    foreign-entry row carrying the identical DOMAIN identifier is never in the
    candidate set rather than being filtered out by luck.

    async_update_device models the branch Home Assistant's own device registry
    takes for remove_config_entry_id: it removes the row outright only when
    this entry was its last one, and otherwise drops the link and leaves the
    row, and every other entry's entities on it, in place. Recording those two
    outcomes separately is what lets a test tell "the row went" apart from
    "this entry let go of a row it shares".
    """
    events = SimpleNamespace(removed=[], unlinked=[])

    class _FakeDeviceRegistry:
        """Records each device-row release against that row's own id."""

        def async_update_device(self, device_id, *, remove_config_entry_id=None):
            """Drop one config entry's link, removing the row if it was the last."""
            if remove_raises:
                raise RuntimeError(f"device row {device_id} is busy")
            row = next((candidate for candidate in rows if candidate.id == device_id), None)
            entries = getattr(row, "config_entries", frozenset()) if row is not None else frozenset()
            if entries == {remove_config_entry_id}:
                events.removed.append(device_id)
            else:
                events.unlinked.append((device_id, remove_config_entry_id))

    registry = _FakeDeviceRegistry()

    def _async_get(hass):
        """Return the seeded fake registry, or raise to drive the lookup guard."""
        if get_raises:
            raise RuntimeError("device registry unavailable")
        return registry

    def _async_entries_for_config_entry(reg, entry_id):
        """Return this config entry's device rows, re-derived per call."""
        return [
            SimpleNamespace(**{k: v for k, v in vars(row).items() if k != "config_entries"})
            for row in rows
            if entry_id in row.config_entries and row.id not in events.removed
        ]

    return events, _async_get, _async_entries_for_config_entry


def _make_device_aware_entity_registry(rows, *, get_raises_after=None):
    """Return (removed, async_get, async_entries) over rows carrying a device_id.

    Sibling of _make_entity_registry with the one field the device-row half
    reads. `get_raises_after` arms the Nth-and-later lookup to fail, which is
    how the post-removal re-fetch is driven into its own guard while the first
    fetch still succeeds and still removes the entity rows.
    """
    removed: list[str] = []
    lookups: list[int] = []

    class _FakeEntityRegistry:
        """Records each entity removal against the row's own entity id."""

        def async_remove(self, entity_id):
            """Record one removal call."""
            removed.append(entity_id)

    registry = _FakeEntityRegistry()

    def _async_get(hass):
        """Return the seeded fake registry, or raise once the arm point passes."""
        lookups.append(1)
        if get_raises_after is not None and len(lookups) > get_raises_after:
            raise RuntimeError("entity registry unavailable")
        return registry

    def _async_entries_for_config_entry(reg, entry_id):
        """Return this config entry's surviving rows, re-derived per call."""
        return [
            SimpleNamespace(entity_id=row.entity_id, unique_id=row.unique_id, device_id=row.device_id)
            for row in rows
            if row.config_entry_id == entry_id and row.entity_id not in removed
        ]

    return removed, _async_get, _async_entries_for_config_entry


class TestOrphanedDeviceRowRemoval:
    """The confirmed removal takes the emptied sub-device row too.

    Removing the entity rows alone leaves an empty device card on the user's
    device page, which trades one cosmetic defect for another. The emptiness
    test is what makes taking the row safe: a row still carrying any entity for
    this config entry is left completely alone, whatever that entity is and
    whichever session or integration created it.
    """

    @staticmethod
    def _drive(
        *,
        entity_rows=EMITTED_ENTITY_ROWS,
        device_rows=DEFAULT_DEVICE_ROWS,
        device_get_raises=False,
        device_remove_raises=False,
        entity_get_raises_after=None,
    ):
        """Run the confirmed removal for SENSOR_KEY over one seeded pair of registries.

        Returns (count, removed_entity_ids, device_events, adder) so each case
        can assert on the entity half, the device half and the adder's
        bookkeeping independently. device_events carries both device-registry
        outcomes: .removed for rows that went, .unlinked for rows this entry
        merely let go of.
        """
        coordinator = SimpleNamespace(data={"sensors": {}})
        adder = LateEntityAdder(
            coordinator,
            lambda ents: None,
            lambda k, i: [_ledger_entity(unique_id) for unique_id in EMITTED_UNIQUE_IDS],
            "valve",
        )
        adder.collect(SENSOR_KEY, {})
        hass = SimpleNamespace(data={DOMAIN: {ENTRY_ID: {LATE_ADDER_STORE_KEY: [adder]}}})
        entry = SimpleNamespace(entry_id=ENTRY_ID)

        removed_entities, entity_get, entity_entries = _make_device_aware_entity_registry(
            entity_rows, get_raises_after=entity_get_raises_after
        )
        device_events, device_get, device_entries = _make_device_registry(
            device_rows, get_raises=device_get_raises, remove_raises=device_remove_raises
        )

        with (
            patch("custom_components.rainpoint.er.async_get", side_effect=entity_get),
            patch("custom_components.rainpoint.er.async_entries_for_config_entry", side_effect=entity_entries),
            patch("custom_components.rainpoint.dr.async_get", side_effect=device_get),
            patch("custom_components.rainpoint.dr.async_entries_for_config_entry", side_effect=device_entries),
        ):
            count = _remove_orphaned_key_rows(hass, entry, SENSOR_KEY)

        return count, removed_entities, device_events, adder

    def test_the_emptied_sub_device_row_goes_with_its_entities(self):
        """The whole point of the device half: after a confirm the physical
        device has neither a leftover entity set nor a leftover device page."""
        count, removed_entities, device_events, adder = self._drive()

        assert count == 4
        assert sorted(removed_entities) == [f"valve.emitted_zone{zone}" for zone in (1, 2, 3, 4)]
        # Exactly one row released, naming that row and no other. The unrelated
        # row, the malformed row and the foreign-entry row all survive. This
        # entry was the row's only one, so the release removes it outright.
        assert device_events.removed == [SUB_DEVICE_ROW_ID]
        assert device_events.unlinked == []
        assert adder.ledger.unique_ids_for(SENSOR_KEY) == frozenset()

    def test_a_row_still_carrying_an_entity_this_session_did_not_emit_is_left_alone(self):
        """The emptiness test, as the guard it exists to be. A cascade removal
        would have taken this row and the entity on it; the entity removals are
        individual and by unique_id precisely so it cannot."""
        count, removed_entities, device_events, _adder = self._drive(
            entity_rows=(*EMITTED_ENTITY_ROWS, UNEMITTED_SAME_ENTRY_ROW),
        )

        assert count == 4
        assert "sensor.left_behind" not in removed_entities
        assert device_events.removed == []
        assert device_events.unlinked == []

    def test_a_row_shared_with_another_config_entry_is_released_not_removed(self):
        """The one case where "empty for this config entry" and "empty" differ.

        Two RainPoint entries resolving the same invited home claim the same
        (DOMAIN, sensor_key) identifier, so Home Assistant merges them into one
        row carrying both. The emptiness test is entry scoped and reads that
        row as empty, which is exactly why the release has to be entry scoped
        too: an unscoped device removal cascades into every entity whose config
        entry is on the row, so the foreign entry would silently lose its
        entities and their recorder history to a card that never named them.
        Dropping only this entry's link leaves the row, and that entry's
        entities, standing."""
        count, removed_entities, device_events, _adder = self._drive(
            entity_rows=(*EMITTED_ENTITY_ROWS, UNEMITTED_FOREIGN_ENTRY_ROW),
            device_rows=(SHARED_SUB_DEVICE_ROW,),
        )

        assert count == 4
        assert device_events.removed == []
        assert device_events.unlinked == [(SUB_DEVICE_ROW_ID, ENTRY_ID)]
        assert "sensor.left_behind_elsewhere" not in removed_entities

    def test_no_device_row_carrying_the_key_removes_the_entities_and_nothing_else(self):
        """A sub-device whose device row was already removed, or was never
        created, must not turn the confirm step into an error."""
        count, removed_entities, device_events, adder = self._drive(
            device_rows=(UNRELATED_DEVICE_ROW, FOREIGN_DEVICE_ROW),
        )

        assert count == 4
        assert len(removed_entities) == 4
        assert device_events.removed == []
        assert device_events.unlinked == []
        assert adder.ledger.unique_ids_for(SENSOR_KEY) == frozenset()

    def test_an_unreadable_device_registry_skips_the_device_half_only(self):
        """The entity removals are already done by then and must stand. The
        device row is a second, weaker claim, and failing to make it is not a
        reason to undo the first."""
        count, removed_entities, device_events, adder = self._drive(device_get_raises=True)

        assert count == 4
        assert len(removed_entities) == 4
        assert device_events.removed == []
        assert device_events.unlinked == []
        assert adder.ledger.unique_ids_for(SENSOR_KEY) == frozenset()

    def test_an_unreadable_post_removal_entity_fetch_leaves_the_device_row_in_place(self):
        """Emptiness is a claim that has to be positively established. A fetch
        that could not answer establishes nothing, so the row stays: reading an
        unreadable registry as "no rows, therefore empty" would remove a device
        row on the strength of a failed lookup."""
        count, removed_entities, device_events, adder = self._drive(entity_get_raises_after=1)

        assert count == 4
        assert len(removed_entities) == 4
        assert device_events.removed == []
        assert device_events.unlinked == []
        assert adder.ledger.unique_ids_for(SENSOR_KEY) == frozenset()

    def test_a_device_row_that_refuses_to_be_released_does_not_break_the_confirm(self):
        """The removal runs inside a Repairs flow step, so nothing here may
        propagate. The entity removals and the lockstep forget both stand."""
        count, removed_entities, device_events, adder = self._drive(device_remove_raises=True)

        assert count == 4
        assert len(removed_entities) == 4
        assert device_events.removed == []
        assert device_events.unlinked == []
        assert adder.ledger.unique_ids_for(SENSOR_KEY) == frozenset()

    def test_malformed_and_foreign_device_rows_are_never_the_candidate(self):
        """The resolve half in isolation: a row with no identifiers attribute,
        a row whose identifiers are not tuples, and a row carrying another
        integration's identifier are all skipped rather than matched, and one
        of them cannot abort the search for the rest."""
        rows = [
            MISSING_IDENTIFIERS_DEVICE_ROW,
            MALFORMED_DEVICE_ROW,
            NO_DOMAIN_DEVICE_ROW,
            UNRELATED_DEVICE_ROW,
            SUB_DEVICE_ROW,
        ]

        assert _device_row_for_sensor_key(rows, SENSOR_KEY) is SUB_DEVICE_ROW
        assert _device_row_for_sensor_key(rows, "no_such_key") is None
        assert _device_row_for_sensor_key([], SENSOR_KEY) is None

    def test_emptiness_is_answered_for_this_entry_and_survives_a_row_without_a_device_id(self):
        """The predicate in isolation, including the row shape that carries no
        device_id at all, which must read as "not on this device" rather than
        raising."""
        on_the_row = SimpleNamespace(device_id=SUB_DEVICE_ROW_ID)
        on_another_row = SimpleNamespace(device_id="device_sub_9")
        no_device_id = SimpleNamespace()

        assert _device_row_is_empty([], SUB_DEVICE_ROW_ID) is True
        assert _device_row_is_empty([on_another_row, no_device_id], SUB_DEVICE_ROW_ID) is True
        assert _device_row_is_empty([on_another_row, on_the_row], SUB_DEVICE_ROW_ID) is False


# The identifiers observed on the maintainer's install when a valve moved
# between two polls. A mid change means a top-level record appeared and another
# stopped listing the addr, which is what makes this two independent events in
# one poll rather than one move.
REKEY_HID = 182509
REKEY_OLD_MID = 236547
REKEY_OLD_ADDR = 3
REKEY_NEW_MID = 346965
REKEY_NEW_ADDR = 1
REKEY_OLD_KEY = f"{REKEY_HID}_{REKEY_OLD_MID}_{REKEY_OLD_ADDR}"
REKEY_NEW_KEY = f"{REKEY_HID}_{REKEY_NEW_MID}_{REKEY_NEW_ADDR}"


def _rekey_hub_record(mid: int, addrs) -> dict:
    """One real hub record at this mid, listing these addrs and no others."""
    return {
        "mid": mid,
        "name": f"Hub {mid}",
        "deviceName": "d",
        "productKey": "pk",
        "homeName": "H",
        "subDevices": [{"addr": addr, "name": "Valve", "model": MODEL_VALVE_245, "softVer": "127"} for addr in addrs],
    }


def _rekey_status(entries) -> list[dict]:
    """A multipleDeviceStatus list from (mid, addr or None) pairs."""
    return [
        {
            "mid": mid,
            "subDeviceStatus": (
                [{"id": f"D{addr:02d}", "value": VALVE_ZONES_TLV_PAYLOAD, "time": 1785420002247}] if addr is not None else []
            ),
        }
        for mid, addr in entries
    ]


def _unique_ids_for(entities, sensor_key: str) -> set[str]:
    """Return the unique_ids among these entities that carry one sensor key."""
    return {
        entity._attr_unique_id
        for entity in entities
        if getattr(entity, "_attr_unique_id", None) and sensor_key in entity._attr_unique_id
    }


def _entities_for(entities, sensor_key: str) -> list:
    """Return the entities among these that carry one sensor key."""
    return [entity for entity in entities if getattr(entity, "_attr_unique_id", None) and sensor_key in entity._attr_unique_id]


class TestOrphanedKeyReKeyEndState:
    """The re-key this whole path exists for, driven end to end.

    The new key gains a full entity set in the same poll the old key vanishes,
    with no reload and no identity pairing, and the old key's rows and device
    row go only once a human confirms the card. Every assertion here sits
    between two real steps of the sequence a live install runs, because the
    defect is an ordering property: an end-state-only test passes against an
    implementation that never reaches this device at all.
    """

    @staticmethod
    def _build(hub_records, status):
        """Return (coordinator, hass, entry, client) over these hub records."""
        client = AsyncMock()
        client.get_devices_by_hid.return_value = hub_records
        client.get_multiple_device_status.return_value = status

        entry = MagicMock()
        entry.entry_id = ENTRY_ID
        entry.data = {CONF_HIDS: [REKEY_HID]}
        entry.options = {}

        hass = MagicMock()
        hass.data = {DOMAIN: {ENTRY_ID: {}}}

        coordinator = RainPointCoordinator(hass, client, entry)
        hass.data[DOMAIN][ENTRY_ID]["coordinator"] = coordinator
        return coordinator, hass, entry, client

    @classmethod
    async def _ids_a_fresh_install_would_build(cls):
        """Return the new key's unique_ids as built by an install that never re-keyed.

        Derived independently rather than by transforming the old key's ids, so
        an implementation that renamed a unique_id across the re-key could not
        satisfy it by construction.
        """
        coordinator, hass, entry, _client = cls._build(
            [_rekey_hub_record(REKEY_NEW_MID, [REKEY_NEW_ADDR])],
            _rekey_status([(REKEY_NEW_MID, REKEY_NEW_ADDR)]),
        )
        captured, _domains, make_add = _capturing_add_entities_by_domain()

        await coordinator.async_config_entry_first_refresh()
        await sensor_async_setup_entry(hass, entry, make_add("sensor"))
        await valve_async_setup_entry(hass, entry, make_add("valve"))

        return _unique_ids_for(captured, REKEY_NEW_KEY)

    @staticmethod
    def _registry_rows_from(captured, domains):
        """Mirror what Home Assistant would have registered for these entities.

        Each sub-device entity lands on its own key's device row, and the
        hub-level entities land on a hub row that must survive untouched.

        The entity_id is built as <domain>.<object_id> from the domain of the
        platform that emitted the entity, which is what Home Assistant does.
        Using the integration name there instead would make every row's domain
        the same string and quietly defeat the removal path's (domain,
        unique_id) match, which is the one thing keeping it from taking a row
        that shares an id across two domains.
        """
        entity_rows = []
        for entity in captured:
            unique_id = getattr(entity, "_attr_unique_id", None)
            if unique_id is None:
                continue
            if REKEY_OLD_KEY in unique_id:
                device_id = f"device_{REKEY_OLD_KEY}"
            elif REKEY_NEW_KEY in unique_id:
                device_id = f"device_{REKEY_NEW_KEY}"
            else:
                device_id = "device_hub"
            entity_rows.append(
                SimpleNamespace(
                    config_entry_id=ENTRY_ID,
                    entity_id=f"{domains[unique_id]}.{unique_id}",
                    unique_id=unique_id,
                    device_id=device_id,
                )
            )

        device_rows = [
            SimpleNamespace(
                id=f"device_{REKEY_OLD_KEY}",
                identifiers={(DOMAIN, REKEY_OLD_KEY)},
                config_entries=frozenset({ENTRY_ID}),
            ),
            SimpleNamespace(
                id=f"device_{REKEY_NEW_KEY}",
                identifiers={(DOMAIN, REKEY_NEW_KEY)},
                config_entries=frozenset({ENTRY_ID}),
            ),
            SimpleNamespace(
                id="device_hub",
                identifiers={(DOMAIN, f"hub_{REKEY_HID}_{REKEY_NEW_MID}")},
                config_entries=frozenset({ENTRY_ID}),
            ),
        ]
        return entity_rows, device_rows

    @pytest.mark.asyncio
    async def test_a_mid_change_ends_with_one_entity_set_and_one_device_row(self):
        """The whole timeline, in the order a live install runs it."""
        from_scratch_ids = await self._ids_a_fresh_install_would_build()

        coordinator, hass, entry, client = self._build(
            [_rekey_hub_record(REKEY_OLD_MID, [REKEY_OLD_ADDR])],
            _rekey_status([(REKEY_OLD_MID, REKEY_OLD_ADDR)]),
        )
        captured, domains, make_add = _capturing_add_entities_by_domain()

        with _patched_issue_registry() as (create, _delete):
            await coordinator.async_config_entry_first_refresh()
            _sync_orphaned_entity_issues_on_updates(hass, entry, coordinator)
            await sensor_async_setup_entry(hass, entry, make_add("sensor"))
            await valve_async_setup_entry(hass, entry, make_add("valve"))

            # Polls 1 and 2: one device at the old key, nothing counted.
            await coordinator.async_refresh()
            old_key_ids = _unique_ids_for(captured, REKEY_OLD_KEY)
            assert old_key_ids
            assert _unique_ids_for(captured, REKEY_NEW_KEY) == set()
            assert coordinator._orphaned_key_poll_counts == {}
            emitted_before_rekey = len(captured)

            # Poll 3: the re-key. Two independent events in one poll, not a
            # move: a record appears at the new mid carrying the device at a
            # new addr, and the old mid stops listing its addr.
            client.get_devices_by_hid.return_value = [
                _rekey_hub_record(REKEY_OLD_MID, []),
                _rekey_hub_record(REKEY_NEW_MID, [REKEY_NEW_ADDR]),
            ]
            client.get_multiple_device_status.return_value = _rekey_status(
                [(REKEY_OLD_MID, None), (REKEY_NEW_MID, REKEY_NEW_ADDR)]
            )
            await coordinator.async_refresh()

            # The new key gained its full entity set immediately, with no
            # reload, and those entities were added rather than renamed.
            assert len(captured) > emitted_before_rekey
            assert _unique_ids_for(captured, REKEY_NEW_KEY) == from_scratch_ids
            # Nothing compared the two keys: no id was carried across, so the
            # two sets are disjoint and the old ids are all still present.
            assert from_scratch_ids.isdisjoint(old_key_ids)
            assert _unique_ids_for(captured, REKEY_OLD_KEY) == old_key_ids

            # The control assertion that makes "exactly one set" meaningful
            # later: right now both sets exist and only the old one is dead.
            assert all(not entity.available for entity in _entities_for(captured, REKEY_OLD_KEY))
            assert any(entity.available for entity in _entities_for(captured, REKEY_NEW_KEY))

            # The old key is now being counted, one poll per absence.
            assert coordinator._orphaned_key_poll_counts[REKEY_OLD_KEY] == 1
            assert REKEY_NEW_KEY not in coordinator._orphaned_key_poll_counts

            for _ in range(ORPHANED_KEY_DEBOUNCE_POLLS - 2):
                await coordinator.async_refresh()

            raised = [call for call in create.call_args_list if call.args[2].startswith("orphaned_device_entities_")]
            assert coordinator._orphaned_key_poll_counts[REKEY_OLD_KEY] == ORPHANED_KEY_DEBOUNCE_POLLS - 1
            assert raised == []

            await coordinator.async_refresh()
            raised = [call for call in create.call_args_list if call.args[2].startswith("orphaned_device_entities_")]
            assert len(raised) == 1

        # Exactly one card, and it names the old key rather than the new one.
        issue_id = raised[0].args[2]
        flow_data = raised[0].kwargs["data"]
        assert issue_id == orphaned_entities_issue_id(REKEY_OLD_KEY)
        assert flow_data["sensor_key"] == REKEY_OLD_KEY

        entity_rows, device_rows = self._registry_rows_from(captured, domains)
        removed_entities, entity_get, entity_entries = _make_device_aware_entity_registry(entity_rows)
        device_events, device_get, device_entries = _make_device_registry(device_rows)

        flow = await async_create_fix_flow(hass, issue_id, flow_data)
        flow.hass = hass

        with (
            patch("custom_components.rainpoint.er.async_get", side_effect=entity_get),
            patch("custom_components.rainpoint.er.async_entries_for_config_entry", side_effect=entity_entries),
            patch("custom_components.rainpoint.dr.async_get", side_effect=device_get),
            patch("custom_components.rainpoint.dr.async_entries_for_config_entry", side_effect=device_entries),
        ):
            shown = await flow.async_step_init()
            assert shown["step_id"] == "confirm"
            # Opening the card removes nothing at all.
            assert removed_entities == []
            assert device_events.removed == []
            assert device_events.unlinked == []

            result = await flow.async_step_confirm({})

        assert result["type"] == "create_entry"

        # Exactly one entity set for the physical device, and it is the
        # current key's. The hub-level rows are untouched.
        surviving = [row for row in entity_rows if row.entity_id not in removed_entities]
        assert {row.unique_id for row in surviving if row.device_id != "device_hub"} == from_scratch_ids
        assert not [row for row in surviving if REKEY_OLD_KEY in row.unique_id]
        assert {row.device_id for row in surviving} == {f"device_{REKEY_NEW_KEY}", "device_hub"}

        # Exactly one device row for the physical device, and the hub row and
        # the new key's row both survive.
        assert device_events.removed == [f"device_{REKEY_OLD_KEY}"]
        assert device_events.unlinked == []
