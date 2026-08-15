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
    _ledger_pairs_and_descriptors,
    _read_aged_out_keys,
    _remove_orphaned_key_rows,
    _sync_orphaned_entity_issues,
    _sync_orphaned_entity_issues_on_updates,
    async_remove_entry,
    repairs,
)
from custom_components.rainpoint import coordinator as coordinator_module
from custom_components.rainpoint.const import (
    CONF_HIDS,
    DOMAIN,
    HUB_IDENTIFIER_PREFIX,
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
def _patched_issue_registry(restored: dict | None = None):
    """Patch the issue registry so create and delete really change what it holds.

    The three functions are one mechanism. Mocking the two writers while
    leaving the reader answering an empty registry would make every raise look
    like a card Home Assistant had already deleted, and the manager reconciles
    its dedup against that reader precisely because Home Assistant deletes a
    fixable issue itself when its flow finishes. So the double has to model the
    registry rather than only record calls.

    ``restored`` seeds the registry with cards that were already there when this
    session started, keyed by issue id and carrying the ``data`` dict Home
    Assistant would have reloaded for a persistent issue. That is what a restart
    looks like from inside the integration: no raise happened in this session,
    and the only thing the card still knows about itself is what was serialized.
    Left None, the registry starts empty, which is every other session here.

    Yields the (create, delete) mocks, which still record every call.
    """
    held: dict[tuple[str, str], object] = {
        (DOMAIN, issue_id): SimpleNamespace(translation_placeholders={}, data=data) for issue_id, data in (restored or {}).items()
    }

    def _create(hass, domain, issue_id, **kwargs):
        """Record the raised card the way the registry would hold it.

        The data dict is held alongside the placeholders because the confirm
        dialog reads both: the placeholders are the text it shows, and the data
        carries the offer that text describes, which the flow snapshots at the
        moment it shows it. A double holding only the text would leave every
        confirm with no ceiling and no test could tell.
        """
        held[(domain, issue_id)] = SimpleNamespace(
            translation_placeholders=kwargs.get("translation_placeholders"), data=kwargs.get("data")
        )

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
            assert issue_id == orphaned_entities_issue_id(SENSOR_KEY, ENTRY_ID)
            assert issue_id == f"orphaned_device_entities_{ENTRY_ID}_{SENSOR_KEY}"
            assert kwargs["is_fixable"] is True
            # Persistent, so the card and the offer inside it survive a restart.
            assert kwargs["is_persistent"] is True
            assert kwargs["translation_key"] == "orphaned_device_entities"
            # The offer rides in the data dict, keyed the way the removal is
            # keyed and sorted so an unchanged offer republishes an unchanged
            # value. It is what a restored card's confirm is held to.
            assert kwargs["data"] == {
                "entry_id": ENTRY_ID,
                "sensor_key": SENSOR_KEY,
                # Lists rather than tuples, because this rides in a persisted
                # issue whose storage schema this integration does not own.
                "orphaned_pairs": [["valve", ZONE_1_UNIQUE_ID], ["valve", ZONE_2_UNIQUE_ID]],
            }
            assert kwargs["translation_placeholders"]["entity_count"] == "2"
            assert kwargs["translation_placeholders"]["missed_polls"] == str(ORPHANED_KEY_DEBOUNCE_POLLS)

            adder = hass.data[DOMAIN][ENTRY_ID][LATE_ADDER_STORE_KEY][0]
            assert adder.ledger.unique_ids_for(SENSOR_KEY) == frozenset({ZONE_1_UNIQUE_ID, ZONE_2_UNIQUE_ID})

            flow = await async_create_fix_flow(hass, issue_id, kwargs["data"])
            flow.hass = hass

            # The issue registry patch stays up for the confirm, which is not
            # bookkeeping: the departed-key confirm now reads its own card back
            # to learn what it was offering, exactly as the still-present one
            # does, so a flow run against an unreadable registry is a flow with
            # an empty ceiling that removes nothing.
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
    async def test_a_card_restored_into_a_session_that_never_saw_the_device_still_removes_its_rows(self):
        """The timeline this whole change exists for, and the one no other test
        here reaches.

        Every other end-to-end confirm runs inside the session that raised the
        card, where the adder ledgers still hold the key and would carry the
        removal on their own. That is precisely the session a restored card does
        not have. Here the card is raised in one session and confirmed in a
        second that never listed the device, never emitted an entity for it and
        therefore holds no ledger entry for its key -- which is what a restart
        after the device departed actually looks like, and what the persisted
        offer is for.

        Driven as two real sessions rather than by injecting a card into one,
        because the property is entirely about what the second session does
        *not* have.
        """
        # Session one: the device is listed, its entities are emitted, then it
        # leaves the enumeration and ages out into a card.
        first, hass_one, entry_one, client = _build_timeline()
        _captured, async_add_entities = _capturing_add_entities()

        with _patched_issue_registry() as (create, _delete):
            await first.async_config_entry_first_refresh()
            _sync_orphaned_entity_issues_on_updates(hass_one, entry_one, first)
            await valve_async_setup_entry(hass_one, entry_one, async_add_entities)

            client.get_devices_by_hid.return_value = _hub_record(with_child=False)
            for _ in range(ORPHANED_KEY_DEBOUNCE_POLLS):
                await first.async_refresh()
            assert create.call_count == 1
            issue_id = create.call_args.args[2]
            # This is the whole of what survives the restart, and it is a plain
            # JSON structure because a persistent issue's data is serialized.
            persisted_data = create.call_args.kwargs["data"]

        assert persisted_data["orphaned_pairs"] == [["valve", ZONE_1_UNIQUE_ID], ["valve", ZONE_2_UNIQUE_ID]]

        # Session two: a fresh coordinator, hass and entry store, over a hub
        # that has never listed the child. Entity creation is one-shot from the
        # first refresh, so no adder emits for the departed key and no ledger
        # in this session mentions it.
        second, hass_two, entry_two, client_two = _build_timeline()
        client_two.get_devices_by_hid.return_value = _hub_record(with_child=False)
        removed, async_get, async_entries = _make_entity_registry()
        _captured_two, async_add_entities_two = _capturing_add_entities()

        # The card is already in the registry when this session starts, holding
        # nothing but what was serialized. Nothing here raises it.
        with _patched_issue_registry({issue_id: persisted_data}) as (create_two, _delete_two):
            await second.async_config_entry_first_refresh()
            _sync_orphaned_entity_issues_on_updates(hass_two, entry_two, second)
            await valve_async_setup_entry(hass_two, entry_two, async_add_entities_two)

            # The premise: this session knows nothing about the key. Asserted
            # rather than assumed, because a session that did hold it would let
            # the old ledger-derived path pass this test.
            assert _offer_of(hass_two) == frozenset()
            assert SENSOR_KEY not in second.data["sensors"]
            # And nothing in this session re-raised it, so the card the user is
            # looking at is the restored one and nothing else.
            assert create_two.call_count == 0

            flow = await async_create_fix_flow(hass_two, issue_id, persisted_data)
            flow.hass = hass_two

            with (
                patch("custom_components.rainpoint.er.async_get", side_effect=async_get),
                patch("custom_components.rainpoint.er.async_entries_for_config_entry", side_effect=async_entries),
            ):
                shown = await flow.async_step_init()
                assert shown["step_id"] == "confirm"
                assert removed == []
                result = await flow.async_step_confirm({})

        # The rows go, on the strength of the offer alone.
        assert result["type"] == "create_entry"
        assert sorted(removed) == ["valve.zone1", "valve.zone2"]
        # And only those: the unrelated same-entry row and the foreign-entry row
        # are as untouched here as they are inside the raising session.
        assert "valve.unrelated" not in removed
        assert "valve.foreign" not in removed

    @pytest.mark.asyncio
    async def test_a_device_that_returns_under_an_open_dialog_survives_the_confirm(self, caplog):
        """The guard that came with the persistent card, driven in order.

        A departed-key card can now outlive the session that raised it, so the
        premise it was raised on -- RainPoint no longer lists this device --
        has to be rechecked at the moment of deletion rather than assumed to
        still hold. The sweep does clear such a card on the update that first
        sees the key again, but a Submit can arrive before that update does,
        and the dialog in front of the user still says the device is gone.

        Driven rather than asserted on an end state, because the property is
        entirely an ordering one: age out, raise, open the dialog, let the
        device come back, then submit. Injecting a card and a live key at once
        would pass against an executor that never rechecks anything.
        """
        coordinator, hass, entry, client = _build_timeline()
        removed, async_get, async_entries = _make_entity_registry()
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
            # The offer is real, so an executor that skipped the guard would
            # have had two rows to take.
            assert len(create.call_args.kwargs["data"]["orphaned_pairs"]) == 2

            flow = await async_create_fix_flow(hass, issue_id, create.call_args.kwargs["data"])
            flow.hass = hass

            with (
                patch("custom_components.rainpoint.er.async_get", side_effect=async_get),
                patch("custom_components.rainpoint.er.async_entries_for_config_entry", side_effect=async_entries),
            ):
                shown = await flow.async_step_init()
                assert shown["step_id"] == "confirm"

                # The device comes back while the dialog sits open, listed by
                # its hub again but not yet reporting: the cloud returns no
                # status for it, which is what a device that has just been
                # re-paired or re-keyed looks like for its first few polls.
                #
                # This is the case a guard reading coordinator.data["sensors"]
                # gets wrong, and it is why the guard reads the enumeration
                # instead. A quiet device is absent from `sensors` for up to
                # SILENT_DEBOUNCE_POLLS polls while still being enumerated, so
                # that guard would read it as departed and clear the way to
                # deleting the live device's rows and their history.
                client.get_devices_by_hid.return_value = _hub_record(with_child=True)
                # A well-formed status response that simply carries nothing for
                # the addr, which is how a re-listed device presents before its
                # first reading arrives.
                client.get_multiple_device_status.return_value = [{"mid": MID, "subDeviceStatus": []}]
                await coordinator.async_refresh()
                assert SENSOR_KEY not in coordinator.data["sensors"]
                assert SENSOR_KEY in coordinator.enumerated_sensor_keys()

                with caplog.at_level(logging.INFO, logger="custom_components.rainpoint"):
                    result = await flow.async_step_confirm({})

        assert result["type"] == "create_entry"
        # Nothing taken, and the breadcrumb says why: Home Assistant deletes
        # the card on any non-abort result, so without this line a confirm that
        # correctly removed nothing is indistinguishable from one that worked.
        assert removed == []
        assert [r for r in caplog.records if "is listed by its hub again" in r.getMessage()]

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
            assert _remove_orphaned_key_rows(hass, entry, SENSOR_KEY, offered_pairs=_offer_of(hass)) == 0

        assert removed == []


class TestDepartedKeyRecordCarriesADeviceName:
    """The departed-key shape gets the same naming treatment as the leftover one.

    The 2026-08-04 observation was made on this shape, so naming only the
    still-present card would close nothing. Both draw on the same
    _resolve_device_names / device_name plumbing.
    """

    @pytest.mark.asyncio
    async def test_a_renamed_device_s_card_carries_its_home_assistant_name(self):
        """The device row survives an aged-out key right up to confirm, so its
        name_by_user is available to name the card."""
        coordinator, hass, entry, client = _build_timeline()
        _removed, entity_get, entity_entries = _make_entity_registry()
        device_row = SimpleNamespace(
            id="d1",
            identifiers={(DOMAIN, SENSOR_KEY)},
            config_entries=frozenset({ENTRY_ID}),
            name_by_user="Front Lawn Valve",
            name="HTV245FRF 1",
        )
        _events, device_get, device_entries = _make_device_registry([device_row])
        _captured, async_add_entities = _capturing_add_entities()

        with (
            _patched_issue_registry() as (create, _delete),
            patch("custom_components.rainpoint.er.async_get", side_effect=entity_get),
            patch("custom_components.rainpoint.er.async_entries_for_config_entry", side_effect=entity_entries),
            patch("custom_components.rainpoint.dr.async_get", side_effect=device_get),
            patch("custom_components.rainpoint.dr.async_entries_for_config_entry", side_effect=device_entries),
        ):
            await coordinator.async_config_entry_first_refresh()
            _sync_orphaned_entity_issues_on_updates(hass, entry, coordinator)
            await valve_async_setup_entry(hass, entry, async_add_entities)

            client.get_devices_by_hid.return_value = _hub_record(with_child=False)
            for _ in range(ORPHANED_KEY_DEBOUNCE_POLLS):
                await coordinator.async_refresh()

            assert create.call_count == 1
            assert create.call_args.kwargs["translation_placeholders"]["device_name"] == "Front Lawn Valve"

    @pytest.mark.asyncio
    async def test_no_device_row_falls_back_to_the_cloud_sub_name(self):
        """Today's behaviour for a departed key whose device row is already
        gone, or whose registry could not be read: the card still names
        something, drawn from the cloud record rather than a blank."""
        coordinator, hass, entry, client = _build_timeline()
        _removed, entity_get, entity_entries = _make_entity_registry()
        _captured, async_add_entities = _capturing_add_entities()

        with (
            _patched_issue_registry() as (create, _delete),
            patch("custom_components.rainpoint.er.async_get", side_effect=entity_get),
            patch("custom_components.rainpoint.er.async_entries_for_config_entry", side_effect=entity_entries),
            patch("custom_components.rainpoint.dr.async_get", side_effect=RuntimeError("device registry unavailable")),
        ):
            await coordinator.async_config_entry_first_refresh()
            _sync_orphaned_entity_issues_on_updates(hass, entry, coordinator)
            await valve_async_setup_entry(hass, entry, async_add_entities)

            client.get_devices_by_hid.return_value = _hub_record(with_child=False)
            for _ in range(ORPHANED_KEY_DEBOUNCE_POLLS):
                await coordinator.async_refresh()

            assert create.call_count == 1
            # "Hub A" is the sub-device name _hub_record stamps into the cloud
            # record, which build_sub_device_info reads as sub_name.
            assert create.call_args.kwargs["translation_placeholders"]["device_name"] == "Hub A"


class TestDepartedKeyRecordCarriesAHubName:
    """The Hub bullet resolves the way the Device bullet does, on both shapes.

    A card that named the device the way its owner does while naming the hub
    the way RainPoint does was describing one home in two vocabularies. The
    hub has its own device row on this config entry, so the sweep's single
    device-registry fetch already carries the name.
    """

    @staticmethod
    def _hub_device_row(name_by_user="HWG023WBRF-V2 Hub"):
        """One hub device row, identified the way this integration writes it."""
        return SimpleNamespace(
            id="d_hub",
            identifiers={(DOMAIN, f"{HUB_IDENTIFIER_PREFIX}{HID}_{MID}")},
            config_entries=frozenset({ENTRY_ID}),
            name_by_user=name_by_user,
            # Deliberately not the cloud's own string for this hub, which is
            # "Hub A". The two routes to a hub name have to be told apart: a
            # row whose registry name matched the cloud fallback would satisfy
            # the unrenamed-hub assertion below even if this function ignored
            # the registry entirely.
            name="Hub A Registry Name",
        )

    async def _card_for(self, device_rows):
        """Drive an aged-out key to its card over these device rows."""
        coordinator, hass, entry, client = _build_timeline()
        _removed, entity_get, entity_entries = _make_entity_registry()
        _events, device_get, device_entries = _make_device_registry(device_rows)
        _captured, async_add_entities = _capturing_add_entities()

        with (
            _patched_issue_registry() as (create, _delete),
            patch("custom_components.rainpoint.er.async_get", side_effect=entity_get),
            patch("custom_components.rainpoint.er.async_entries_for_config_entry", side_effect=entity_entries),
            patch("custom_components.rainpoint.dr.async_get", side_effect=device_get),
            patch("custom_components.rainpoint.dr.async_entries_for_config_entry", side_effect=device_entries),
        ):
            await coordinator.async_config_entry_first_refresh()
            _sync_orphaned_entity_issues_on_updates(hass, entry, coordinator)
            await valve_async_setup_entry(hass, entry, async_add_entities)

            client.get_devices_by_hid.return_value = _hub_record(with_child=False)
            for _ in range(ORPHANED_KEY_DEBOUNCE_POLLS):
                await coordinator.async_refresh()

            assert create.call_count == 1
            return create.call_args.kwargs["translation_placeholders"]

    @pytest.mark.asyncio
    async def test_a_renamed_hub_names_the_card_s_hub_bullet(self):
        """The maintainer's own hub, named the way it is named everywhere else
        in Home Assistant rather than the way RainPoint names it."""
        placeholders = await self._card_for([self._hub_device_row()])

        assert placeholders["hub_name"] == "HWG023WBRF-V2 Hub"

    @pytest.mark.asyncio
    async def test_an_unrenamed_hub_still_gets_a_name_from_its_own_row(self):
        """A hub the owner never renamed resolves to the registry's own name
        for it, which is a name rather than a blank."""
        placeholders = await self._card_for([self._hub_device_row(name_by_user=None)])

        assert placeholders["hub_name"] == "Hub A Registry Name"

    @pytest.mark.asyncio
    async def test_no_hub_row_falls_back_to_the_cloud_hub_name(self):
        """A hub whose row cannot be resolved is still named, from the cloud
        record the ledger's descriptor holds."""
        placeholders = await self._card_for([])

        assert placeholders["hub_name"] == "Hub A"


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


def _offer_of(hass, *pairs) -> frozenset[tuple[str, str]]:
    """Return the offer the caller states, having checked production agrees.

    The removal executor is held to what its card offered rather than to
    whatever the ledgers hold at confirm time, because a persisted card can
    outlive the session that raised it. So these tests have to hand it an offer,
    and where that offer comes from decides what they can catch.

    An earlier version derived it by calling the same function the record
    builder calls. That made every one of these assertions tautological about
    the offer: "the executor took what production would have offered" cannot
    fail if production computed both halves. The offer is stated by the caller
    here instead, so each test says which rows its card named.

    The derivation still runs, as a cross-check rather than as the source: if
    the stated offer and the one the record builder would publish ever diverge,
    that is either a test describing a card nobody would raise or a builder that
    has drifted, and both are worth failing on at the moment they appear rather
    than at the end-to-end tests much later.
    """
    stated = frozenset(pairs)
    store = (hass.data.get(DOMAIN) or {}).get(ENTRY_ID) or {}
    built, _descriptors = _ledger_pairs_and_descriptors(store)
    assert frozenset(built.get(SENSOR_KEY, frozenset())) == stated, (
        "this test states an offer the record builder would not publish for the same ledgers"
    )
    return stated


class _BrokenAdder:
    """An adder whose ledger raises, standing in for a malformed platform."""

    @property
    def ledger(self):
        """Fail the way a half-constructed adder would."""
        raise RuntimeError("no ledger")

    def forget(self, key, kept_ids=frozenset()):
        """Fail on the forget half too.

        Carries the production signature deliberately: a double that took the
        key alone would raise TypeError on a call the sweep really makes, and
        the per-adder guard would swallow that as if it were this adder's own
        declared failure.
        """
        raise RuntimeError("cannot forget")


class _PartiallyReadableAdder:
    """An adder whose ledger names a key and then refuses to describe it.

    The shape _BrokenAdder cannot express. That one raises before the gather
    loop reaches any key at all, so both halves of the gathered state stay
    empty and agree by accident. This one answers for a key and fails halfway
    through it, which is the only way a key can end up recorded in one half
    and missing from the other.
    """

    domain = "sensor"

    def __init__(self, key: str):
        """Hold one key whose ids read cleanly and whose descriptor does not."""
        self.ledger = SimpleNamespace(
            keys=lambda: frozenset({key}),
            unique_ids_for=lambda _key: frozenset({"half-recorded"}),
            descriptor_for=self._refuse,
        )

    def _refuse(self, key):
        """Fail on the descriptor half alone, once the ids have been read."""
        raise RuntimeError("no descriptor")

    def forget(self, key, kept_ids=frozenset()):
        """Never reached, and carries the production signature regardless."""
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
        self.forgotten: list[tuple[str, frozenset]] = []

    @property
    def domain(self):
        """Fail the way a half-constructed adder would."""
        raise RuntimeError("no domain")

    def forget(self, key, kept_ids=frozenset()):
        """Record that the sweep released this adder's bookkeeping.

        Records the pair rather than the key alone, and takes kept_ids at the
        production signature, because this double's whole job is an assertion
        that the sweep never calls it. Taking the key alone would make that
        call raise TypeError into the per-adder guard, leaving the log empty
        and the assertion green for the wrong reason.
        """
        self.forgotten.append((key, kept_ids))


class _NamelessAdder:
    """An adder whose domain reads cleanly and is not a usable string.

    Distinct from _UnresolvableAdder, whose domain raises. This one answers,
    and answers with something no (domain, unique_id) pair can be built from,
    which is the case a bare try/except never reaches.
    """

    def __init__(self, domain=None):
        """Hold one recorded id under a domain that cannot key a pair."""
        self.ledger = EmittedEntityLedger()
        self.ledger.record(SENSOR_KEY, {}, [_ledger_entity(ZONE_2_UNIQUE_ID)])
        self.domain = domain
        self.forgotten: list[tuple[str, frozenset]] = []

    def forget(self, key, kept_ids=frozenset()):
        """Record the forget, so a released id would be visible."""
        self.forgotten.append((key, kept_ids))


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

    def forget(self, key, kept_ids=frozenset()):
        """Fail the way a half-torn-down adder would, at the real signature."""
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

    def test_an_adder_that_fails_mid_key_does_not_abort_the_record_build(self):
        """The same promise at the one point it was not kept.

        The gather fills two dicts, and the record builder indexes the second
        by the keys it finds in the first, without a guard of its own. An
        adder that answers for a key and then raises describing it used to
        leave that key in the first dict alone, so the builder raised a
        KeyError and the whole sweep was lost rather than this adder's keys.
        """
        coordinator = SimpleNamespace(data={"sensors": {}})
        good = LateEntityAdder(coordinator, lambda ents: None, lambda k, i: [_ledger_entity("z1")], "valve")
        good.collect(SENSOR_KEY, {"addr": ADDR, "model": "M", "sub_name": "S", "hub_name": "H"})
        store = {LATE_ADDER_STORE_KEY: [_PartiallyReadableAdder("half_key"), good]}

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

    @pytest.mark.parametrize("domain", [None, ""])
    def test_an_adder_with_no_usable_domain_offers_none_of_its_rows(self, caplog, domain):
        """A pair needs a domain, so an adder that cannot supply one supplies
        no pairs.

        Entity registry uniqueness is per domain, so half a pair is not a
        partial identifier, it is a wrong one. Dropping the adder narrows the
        offer, which is the direction every uncertainty on this path takes: its
        rows go unoffered until a session that can read it, rather than being
        offered under a guessed domain that could name somebody else's row.
        """
        good = LateEntityAdder(
            SimpleNamespace(data={"sensors": {}}), lambda ents: None, lambda k, i: [_ledger_entity(ZONE_1_UNIQUE_ID)], "valve"
        )
        good.collect(SENSOR_KEY, {})
        nameless = _NamelessAdder(domain)
        hass = SimpleNamespace(data={DOMAIN: {ENTRY_ID: {LATE_ADDER_STORE_KEY: [nameless, good]}}})

        with caplog.at_level(logging.DEBUG, logger="custom_components.rainpoint"):
            offer = _offer_of(hass, ("valve", ZONE_1_UNIQUE_ID))

        # The readable adder's row is offered; its neighbour's is not, and the
        # neighbour costs it nothing.
        assert offer == frozenset({("valve", ZONE_1_UNIQUE_ID)})
        assert [r for r in caplog.records if "late adder with no usable domain" in r.getMessage()]

    @staticmethod
    def _remover_over(coordinator):
        """Publish the removal executor for a coordinator and hand it back."""
        hass = SimpleNamespace(data={DOMAIN: {ENTRY_ID: {}}})
        entry = MagicMock()
        entry.entry_id = ENTRY_ID
        with patch("custom_components.rainpoint.RainPointOrphanedEntityIssues"):
            _sync_orphaned_entity_issues_on_updates(hass, entry, coordinator)
        return hass.data[DOMAIN][ENTRY_ID]["orphan_entity_remover"]

    @pytest.mark.parametrize(
        ("answer", "why"),
        [
            (None, "no poll has carried a device list yet"),
            (["100_200_1"], "the answer is not a set"),
            ("100_200_1", "the answer is a bare string, whose membership test would pass by substring"),
        ],
    )
    def test_a_confirm_that_cannot_read_the_enumeration_removes_nothing(self, caplog, answer, why):
        """The direction this guard resolves in, on the reader it must not share.

        The two registry sweeps use a reader that answers {} for both "the poll
        listed nothing" and "I could not look", which is right where the worst
        case is a row left alone. Here the question is whether RainPoint still
        lists this addr, and a collapsed answer means "it does not", so an
        unreadable coordinator would read as confirmation that the device is
        gone and the deletion would proceed on it.
        """
        coordinator = MagicMock()
        coordinator.enumerated_sensor_keys.return_value = answer
        remover = self._remover_over(coordinator)

        with (
            caplog.at_level(logging.WARNING, logger="custom_components.rainpoint"),
            patch("custom_components.rainpoint._remove_orphaned_key_rows") as executor,
        ):
            taken = remover(SENSOR_KEY, leftover_shape=False, offered_pairs=frozenset({("valve", ZONE_1_UNIQUE_ID)}))

        assert taken == 0, why
        # The executor is never reached at all, so this cannot be satisfied by
        # an executor that happens to resolve an empty scope.
        executor.assert_not_called()
        assert [r for r in caplog.records if "Could not read the enumeration" in r.getMessage()]

    def test_a_coordinator_that_raises_on_the_enumeration_removes_nothing(self, caplog):
        """The same direction, reached by an exception rather than a value.

        A torn-down coordinator raises rather than answering None, and that
        route may not be the one that resolves toward deleting.
        """
        coordinator = MagicMock()
        coordinator.enumerated_sensor_keys.side_effect = RuntimeError("coordinator gone")
        remover = self._remover_over(coordinator)

        with (
            caplog.at_level(logging.DEBUG, logger="custom_components.rainpoint"),
            patch("custom_components.rainpoint._remove_orphaned_key_rows") as executor,
        ):
            assert remover(SENSOR_KEY, leftover_shape=False, offered_pairs=frozenset()) == 0

        executor.assert_not_called()
        assert [r for r in caplog.records if "Could not read the enumeration while confirming" in r.getMessage()]

    def test_an_empty_enumeration_is_an_observation_and_lets_the_removal_run(self, caplog):
        """The other side of the three-valued read, which is what stops it
        degrading into "never remove anything".

        A hub that genuinely lists no sub-devices is a real observation that
        this key is not among them, and it is the ordinary state of the account
        this card exists for. Collapsing it with the unreadable case would make
        the guard block the very removal it was added to permit.
        """
        coordinator = MagicMock()
        coordinator.enumerated_sensor_keys.return_value = frozenset()
        remover = self._remover_over(coordinator)

        with patch("custom_components.rainpoint._remove_orphaned_key_rows", return_value=2) as executor:
            assert remover(SENSOR_KEY, leftover_shape=False, offered_pairs=frozenset({("valve", ZONE_1_UNIQUE_ID)})) == 2

        executor.assert_called_once()

    def test_an_adder_with_no_usable_domain_keeps_its_bookkeeping_through_a_removal(self, caplog):
        """The resolve half of the same gate the offer half applies.

        The two build the same (domain, unique_id) pairs from the same ledgers,
        so an adder one of them skips and the other does not is a disagreement
        about what this key's rows even are. Both skip it, and skipping means
        holding: its ids stay in its ledger, so a returning device cannot be
        offered a unique_id whose row is still registered.
        """
        removed, async_get, async_entries = _make_entity_registry()
        good = LateEntityAdder(
            SimpleNamespace(data={"sensors": {}}),
            lambda ents: None,
            lambda k, i: [_ledger_entity(ZONE_1_UNIQUE_ID)],
            "valve",
        )
        good.collect(SENSOR_KEY, {})
        nameless = _NamelessAdder()
        hass = SimpleNamespace(data={DOMAIN: {ENTRY_ID: {LATE_ADDER_STORE_KEY: [nameless, good]}}})
        entry = SimpleNamespace(entry_id=ENTRY_ID)

        with (
            patch("custom_components.rainpoint.er.async_get", side_effect=async_get),
            patch("custom_components.rainpoint.er.async_entries_for_config_entry", side_effect=async_entries),
            caplog.at_level(logging.DEBUG, logger="custom_components.rainpoint"),
        ):
            assert (
                _remove_orphaned_key_rows(hass, entry, SENSOR_KEY, offered_pairs=_offer_of(hass, ("valve", ZONE_1_UNIQUE_ID)))
                == 1
            )

        assert removed == ["valve.zone1"]
        # Never resolved, so never forgotten, so its id is still held.
        assert nameless.forgotten == []
        assert nameless.ledger.unique_ids_for(SENSOR_KEY) == frozenset({ZONE_2_UNIQUE_ID})
        assert [r for r in caplog.records if "no usable domain while resolving" in r.getMessage()]

    def test_an_unreadable_manager_does_not_block_the_unload(self, caplog):
        """Everything registered after this hook still has to be torn down."""
        coordinator = MagicMock()
        hass = SimpleNamespace(data={DOMAIN: {ENTRY_ID: {}}})
        entry = MagicMock()
        entry.entry_id = ENTRY_ID

        with patch("custom_components.rainpoint.RainPointOrphanedEntityIssues") as manager_cls:
            manager_cls.return_value.async_withdraw_rebuildable_cards.side_effect = RuntimeError("registry down")
            _sync_orphaned_entity_issues_on_updates(hass, entry, coordinator)

            with caplog.at_level(logging.DEBUG, logger="custom_components.rainpoint"):
                for call in entry.async_on_unload.call_args_list:
                    call.args[0]()

        assert [r for r in caplog.records if "Could not withdraw the unused entity cards" in r.getMessage()]

    def test_the_unload_hook_asks_for_the_rebuildable_cards_only(self):
        """The wiring half of the shape-scoped withdrawal.

        Which cards actually go is the manager's decision and is asserted
        against a real one in test_repairs; what this pins is that the unload
        path asks the shape-scoped question at all. Calling the old
        withdraw-everything method here would take the departed-key cards with
        it and undo the persistence.
        """
        coordinator = MagicMock()
        hass = SimpleNamespace(data={DOMAIN: {ENTRY_ID: {}}})
        entry = MagicMock()
        entry.entry_id = ENTRY_ID

        with patch("custom_components.rainpoint.RainPointOrphanedEntityIssues") as manager_cls:
            _sync_orphaned_entity_issues_on_updates(hass, entry, coordinator)
            manager_cls.return_value.async_withdraw_rebuildable_cards.assert_not_called()

            # Every hook this registers, fired.
            for call in entry.async_on_unload.call_args_list:
                call.args[0]()

        manager_cls.return_value.async_withdraw_rebuildable_cards.assert_called_once()

    def test_an_unreadable_entry_store_removes_nothing(self):
        """The remover's first guard, reached by a flow submitted after its
        config entry unloaded."""
        hass = SimpleNamespace(data={})
        entry = SimpleNamespace(entry_id=ENTRY_ID)

        assert _remove_orphaned_key_rows(hass, entry, SENSOR_KEY, offered_pairs=_offer_of(hass)) == 0

    def test_a_card_with_nothing_in_scope_says_so_rather_than_returning_silently(self, caplog):
        """Home Assistant deletes a fixable issue once its flow finishes, so a
        confirm that took nothing looks to the user exactly like a successful
        removal. The log line is the only breadcrumb left.

        It no longer names a manual entity-registry step, and that is the
        change rather than a rewording: the card is persistent and carries its
        own offer, so an empty scope means this confirm found nothing, not that
        nothing will be offered again.
        """
        coordinator = SimpleNamespace(data={"sensors": {}})
        adder = LateEntityAdder(coordinator, lambda ents: None, lambda k, i: [], "valve")
        hass = SimpleNamespace(data={DOMAIN: {ENTRY_ID: {LATE_ADDER_STORE_KEY: [adder]}}})
        entry = SimpleNamespace(entry_id=ENTRY_ID)

        with caplog.at_level(logging.WARNING, logger="custom_components.rainpoint"):
            assert _remove_orphaned_key_rows(hass, entry, SENSOR_KEY, offered_pairs=_offer_of(hass)) == 0

        said = [r.getMessage() for r in caplog.records if "Nothing was in scope for sensor key" in r.getMessage()]
        assert said
        assert not [m for m in said if "by hand" in m]

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
            assert (
                _remove_orphaned_key_rows(
                    hass,
                    entry,
                    SENSOR_KEY,
                    offered_pairs=_offer_of(hass, ("valve", ZONE_1_UNIQUE_ID), ("valve", ZONE_2_UNIQUE_ID)),
                )
                == 2
            )

        assert removed == ["valve.zone1", "valve.zone2"]
        assert good.ledger.unique_ids_for(SENSOR_KEY) == frozenset()

    def test_a_ledger_row_this_card_never_offered_keeps_its_bookkeeping(self, caplog):
        """The hold that the offer-scoped confirm made necessary.

        The scope is now the card's offer rather than the whole of the ledger,
        so the two can differ: a ledger that gained a row after the card was
        raised holds an id this sweep never reached for. That row is still
        registered and still holds its unique_id, exactly like one whose
        removal was attempted and raised, so releasing it would let a returning
        device offer a live unique_id a second time. A forget keyed on the
        failures alone would release it.
        """
        removed, async_get, async_entries = _make_entity_registry()
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

        # The card named one of the two rows; the second joined the ledger
        # after it was raised.
        offer = frozenset({("valve", ZONE_1_UNIQUE_ID)})

        with (
            patch("custom_components.rainpoint.er.async_get", side_effect=async_get),
            patch("custom_components.rainpoint.er.async_entries_for_config_entry", side_effect=async_entries),
            caplog.at_level(logging.DEBUG, logger="custom_components.rainpoint"),
        ):
            assert _remove_orphaned_key_rows(hass, entry, SENSOR_KEY, offered_pairs=offer) == 1

        # Exactly the offered row went, and the unoffered one kept both its
        # registry row and its place in the ledger.
        assert removed == ["valve.zone1"]
        assert adder.ledger.unique_ids_for(SENSOR_KEY) == frozenset({ZONE_2_UNIQUE_ID})
        assert [r for r in caplog.records if "never offered" in r.getMessage()]

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
            assert (
                _remove_orphaned_key_rows(hass, entry, SENSOR_KEY, offered_pairs=_offer_of(hass, ("valve", ZONE_1_UNIQUE_ID)))
                == 1
            )

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
            assert (
                _remove_orphaned_key_rows(hass, entry, SENSOR_KEY, offered_pairs=_offer_of(hass, ("valve", ZONE_1_UNIQUE_ID)))
                == 1
            )

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
            assert (
                _remove_orphaned_key_rows(hass, entry, SENSOR_KEY, offered_pairs=_offer_of(hass, ("valve", ZONE_1_UNIQUE_ID)))
                == 0
            )

        assert adder.ledger.unique_ids_for(SENSOR_KEY) == frozenset({ZONE_1_UNIQUE_ID})

    def test_a_row_that_cannot_be_removed_keeps_its_own_id_and_only_its_own(self, caplog):
        """Per-row guarding, matching the generic sweep's shape, plus the
        condition that guarding puts on the forget.

        A row whose removal raised is still registered and still holds its
        unique_id, so that id is held: releasing it would let a returning
        device offer a live unique_id a second time, which Home Assistant
        rejects and which the never-offer-twice property exists precisely to
        prevent. The id beside it names a row that really did go, so holding
        that one too would leave a returning device missing exactly the
        entities the sweep succeeded in removing, with no recovery short of a
        reload. This adder's bookkeeping is id-indexed on both halves, so the
        held set is exactly the failures."""
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
            assert (
                _remove_orphaned_key_rows(
                    hass,
                    entry,
                    SENSOR_KEY,
                    offered_pairs=_offer_of(hass, ("valve", ZONE_1_UNIQUE_ID), ("valve", ZONE_2_UNIQUE_ID)),
                )
                == 1
            )

        assert registry.removed == ["valve.zone2"]
        # Exactly the failure is held. Releasing it would be a unique_id
        # collision Home Assistant answers by dropping the new entity; holding
        # its neighbour would leave a returning device short of the entity the
        # sweep did remove.
        assert adder.ledger.unique_ids_for(SENSOR_KEY) == frozenset({ZONE_1_UNIQUE_ID})
        assert adder._emitted == {ZONE_1_UNIQUE_ID}
        # The descriptor survives with the held id, so a record can still be
        # built for the key and the card offered again for a retry.
        assert adder.ledger.descriptor_for(SENSOR_KEY) != {}
        assert [r.getMessage() for r in caplog.records if "Kept the bookkeeping for" in r.getMessage()]

    def test_a_key_that_returns_after_a_partial_failure_regains_the_removed_rows(self):
        """The silent failure mode the per-id hold exists to close.

        Holding the whole key would leave the ledger naming ids whose rows are
        gone, so a key that returns before the user retries would have those
        entities suppressed as already-emitted and come back with an entity set
        missing exactly the rows the sweep did remove, with no recovery short
        of a reload.
        """
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
            """Refuses the first row and accepts the second."""

            def async_remove(self, entity_id):
                """Fail on the first row only."""
                if entity_id == "valve.zone1":
                    raise RuntimeError("row is busy")

        rows = [
            SimpleNamespace(entity_id="valve.zone1", unique_id=ZONE_1_UNIQUE_ID),
            SimpleNamespace(entity_id="valve.zone2", unique_id=ZONE_2_UNIQUE_ID),
        ]

        with (
            patch("custom_components.rainpoint.er.async_get", return_value=_StubbornRegistry()),
            patch("custom_components.rainpoint.er.async_entries_for_config_entry", return_value=rows),
        ):
            _remove_orphaned_key_rows(
                hass, entry, SENSOR_KEY, offered_pairs=_offer_of(hass, ("valve", ZONE_1_UNIQUE_ID), ("valve", ZONE_2_UNIQUE_ID))
            )

        # Only the row that really went is offered again. The one still holding
        # its unique_id stays suppressed, which is what stops the returning
        # device colliding with it.
        assert [e._attr_unique_id for e in adder.collect(SENSOR_KEY, {})] == [ZONE_2_UNIQUE_ID]


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
            count = _remove_orphaned_key_rows(
                hass, entry, SENSOR_KEY, offered_pairs=_offer_of(hass, *(("valve", uid) for uid in EMITTED_UNIQUE_IDS))
            )

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
            assert issue_id == orphaned_entities_issue_id(REKEY_OLD_KEY, ENTRY_ID)
            assert flow_data["sensor_key"] == REKEY_OLD_KEY
            # Every pair the card offers belongs to the old key, so a confirm
            # held to this offer cannot reach the new key's fresh entity set
            # even though both sets sit on the same config entry.
            assert flow_data["orphaned_pairs"]
            assert all(REKEY_OLD_KEY in unique_id for _domain, unique_id in flow_data["orphaned_pairs"])

            entity_rows, device_rows = self._registry_rows_from(captured, domains)
            removed_entities, entity_get, entity_entries = _make_device_aware_entity_registry(entity_rows)
            device_events, device_get, device_entries = _make_device_registry(device_rows)

            flow = await async_create_fix_flow(hass, issue_id, flow_data)
            flow.hass = hass

            # Inside the issue registry patch, because the confirm reads its own
            # card back for the offer it is held to.
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


class TestTheCardsGoWhenTheEntryDoes:
    """Removal is the one event that still withdraws these cards.

    A reload leaves them standing and a restart restores them, so this is the
    only path left that deletes one without a user confirming it. It has to
    reach them through the issue registry rather than through the manager,
    because Home Assistant unloads a config entry before it removes it and the
    manager is gone by then.
    """

    @staticmethod
    def _registry(*issue_ids):
        """An issue registry double holding the given ids under this domain."""
        held = {(DOMAIN, issue_id): SimpleNamespace(data={}) for issue_id in issue_ids}
        held[("other_integration", "orphaned_device_entities_e1_1_2_3")] = SimpleNamespace(data={})
        return held, SimpleNamespace(issues=held)

    @pytest.mark.asyncio
    async def test_removing_the_entry_withdraws_its_own_cards_and_no_others(self):
        """Scoped by the entry id the issue id already carries.

        Two RainPoint entries resolving the same invited home produce the same
        sensor keys, so an unscoped sweep here would delete a card the other
        entry raised and never consented to. An entry id carries no underscore
        of its own, which is what makes the prefix test unable to reach across.
        """
        mine = orphaned_entities_issue_id(SENSOR_KEY, ENTRY_ID)
        theirs = orphaned_entities_issue_id(SENSOR_KEY, "other_entry")
        held, registry = self._registry(mine, theirs, "device_not_reporting_100_200_1")

        with (
            patch.object(repairs.ir, "async_get", return_value=registry),
            patch.object(repairs.ir, "async_delete_issue", side_effect=lambda h, d, i: held.pop((d, i), None)),
        ):
            await async_remove_entry(SimpleNamespace(), SimpleNamespace(entry_id=ENTRY_ID))

        assert (DOMAIN, mine) not in held
        # The other entry's card, the not-reporting card and another
        # integration's identically shaped id are all untouched.
        assert (DOMAIN, theirs) in held
        assert (DOMAIN, "device_not_reporting_100_200_1") in held
        assert ("other_integration", "orphaned_device_entities_e1_1_2_3") in held

    @pytest.mark.asyncio
    async def test_a_sibling_entry_id_that_is_a_string_prefix_keeps_its_cards(self):
        """The case the prefix test alone cannot answer.

        Ids are f"{PREFIX}_{entry_id}_{sensor_key}", so entry "e1"'s prefix is a
        string prefix of entry "e1_2"'s whole id. Home Assistant's own entry ids
        are ULID or uuid4 hex and contain no underscore, which makes this
        unreachable in production -- but the code states the scoping as a safety
        property, so the property is what gets tested rather than the alphabet
        that currently saves it. The card's own published entry_id decides.
        """
        mine = orphaned_entities_issue_id(SENSOR_KEY, "e1")
        sibling = orphaned_entities_issue_id(SENSOR_KEY, "e1_2")
        held = {
            (DOMAIN, mine): SimpleNamespace(data={"entry_id": "e1", "sensor_key": SENSOR_KEY}),
            (DOMAIN, sibling): SimpleNamespace(data={"entry_id": "e1_2", "sensor_key": SENSOR_KEY}),
        }
        registry = SimpleNamespace(issues=held)

        with (
            patch.object(repairs.ir, "async_get", return_value=registry),
            patch.object(repairs.ir, "async_delete_issue", side_effect=lambda h, d, i: held.pop((d, i), None)),
        ):
            await async_remove_entry(SimpleNamespace(), SimpleNamespace(entry_id="e1"))

        assert (DOMAIN, mine) not in held
        # The sibling's id starts with entry e1's prefix and survives anyway.
        assert sibling.startswith("orphaned_device_entities_e1_")
        assert (DOMAIN, sibling) in held

    @pytest.mark.asyncio
    async def test_a_card_whose_entry_id_cannot_be_read_falls_back_to_the_prefix(self):
        """The fallback, which is the pre-existing behaviour.

        A card written before the entry id was published there, or one whose
        data cannot be read at all, still has to be withdrawable by the entry
        that raised it.
        """
        no_data = orphaned_entities_issue_id("100_200_1", ENTRY_ID)
        raises = orphaned_entities_issue_id("100_200_2", ENTRY_ID)

        class _ExplodingData:
            @property
            def data(self):
                raise RuntimeError("unreadable")

        held = {
            (DOMAIN, no_data): SimpleNamespace(data=None),
            (DOMAIN, raises): _ExplodingData(),
        }
        registry = SimpleNamespace(issues=held)

        with (
            patch.object(repairs.ir, "async_get", return_value=registry),
            patch.object(repairs.ir, "async_delete_issue", side_effect=lambda h, d, i: held.pop((d, i), None)),
        ):
            await async_remove_entry(SimpleNamespace(), SimpleNamespace(entry_id=ENTRY_ID))

        assert held == {}

    @pytest.mark.asyncio
    async def test_an_unreadable_registry_leaves_the_cards_and_does_not_raise(self, caplog):
        """This runs on Home Assistant's removal path, where raising would
        surface as a failed integration removal. A card left standing is the
        lesser outcome."""
        with (
            patch.object(repairs.ir, "async_get", side_effect=RuntimeError("registry down")),
            patch.object(repairs.ir, "async_delete_issue") as delete,
            caplog.at_level(logging.DEBUG, logger="custom_components.rainpoint"),
        ):
            await async_remove_entry(SimpleNamespace(), SimpleNamespace(entry_id=ENTRY_ID))

        delete.assert_not_called()
        assert [r.getMessage() for r in caplog.records if "Could not read the issue registry" in r.getMessage()]

    @pytest.mark.asyncio
    async def test_one_card_that_refuses_to_go_does_not_strand_the_rest(self, caplog):
        """Guarded per card, matching the per-row discipline of the removal
        executor: one failure costs its own card and no others."""
        first = orphaned_entities_issue_id("100_200_1", ENTRY_ID)
        second = orphaned_entities_issue_id("100_200_2", ENTRY_ID)
        held, registry = self._registry(first, second)

        def _delete(hass, domain, issue_id):
            """Fail on the first id only, so the second still has to go."""
            if issue_id == first:
                raise RuntimeError("cannot delete")
            held.pop((domain, issue_id), None)

        with (
            patch.object(repairs.ir, "async_get", return_value=registry),
            patch.object(repairs.ir, "async_delete_issue", side_effect=_delete),
            caplog.at_level(logging.DEBUG, logger="custom_components.rainpoint"),
        ):
            await async_remove_entry(SimpleNamespace(), SimpleNamespace(entry_id=ENTRY_ID))

        assert (DOMAIN, first) in held
        assert (DOMAIN, second) not in held
        assert [r.getMessage() for r in caplog.records if "Failed to withdraw the leftover entities" in r.getMessage()]
