"""The hub identity re-key, driven end to end through the real registries.

Every proof here depends on `entity_registry` and `device_registry` resolving
to the real Home Assistant classes. The repository conftest installs package
wide MagicMock stubs and only skips these two because the pytest plugin
imported them first; if that ordering ever changes, each assertion below would
pass against a mock while the migration did nothing at all. The module-level
guard makes that fail loudly instead.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.rainpoint import async_migrate_entry
from custom_components.rainpoint.const import DOMAIN

assert not isinstance(er.async_get, MagicMock), "entity_registry is stubbed; every proof here would be a no-op"
assert not isinstance(dr.async_get, MagicMock), "device_registry is stubbed; every proof here would be a no-op"

HID = 100
MID = 200
ADDR = 1


def _make_entry(hass, version=1):
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"area_code": "1", "email": "a@b.c", "password": "pw", "hids": [HID], "token": "tok"},
        options={},
        version=version,
    )
    entry.add_to_hass(hass)
    return entry


def _seed_old_shape_install(entry, entity_registry, device_registry):
    """Seed the registries as a pre-migration install holds them.

    One hub device row keyed on the home id alone, one sub-device parented to
    it, hub entity rows covering both a base-derived id (rssi) and an inline
    one (mac), each carrying a user-set name so the preservation claim has
    something to be false about, and one connectivity row already in the
    target shape.
    """
    hub = device_registry.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, f"hub_{HID}")},
        name="RainPoint Hub",
    )
    device_registry.async_update_device(hub.id, name_by_user="Kitchen Hub")
    child = device_registry.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, f"{HID}_{MID}_{ADDR}")},
        via_device=(DOMAIN, f"hub_{HID}"),
        name="Zone Valve",
    )

    rows = {}
    for suffix, platform in (("rssi", "sensor"), ("mac", "sensor")):
        row = entity_registry.async_get_or_create(
            platform,
            DOMAIN,
            f"{DOMAIN}_hub_{HID}_{suffix}",
            config_entry=entry,
            suggested_object_id=f"rainpoint_hub_{suffix}",
        )
        entity_registry.async_update_entity(row.entity_id, name=f"My Hub {suffix.upper()}")
        rows[suffix] = entity_registry.async_get(row.entity_id)

    connectivity = entity_registry.async_get_or_create(
        "binary_sensor",
        DOMAIN,
        f"{DOMAIN}_hub_{HID}_{MID}_connectivity",
        config_entry=entry,
        suggested_object_id="rainpoint_hub_cloud_connection",
    )
    rows["connectivity"] = entity_registry.async_get(connectivity.entity_id)

    return hub, child, rows


class TestHubIdentityMigration:
    """One seeded install, migrated in place through the real registries."""

    @pytest.mark.asyncio
    async def test_seeded_install_migrates_in_place(self, hass, entity_registry, device_registry):
        """The whole tracer: both halves move, and nothing loses its identity.

        Asserted against a registry the migration actually wrote, not against a
        return value or an injected snapshot. The hub device row keeps its
        device.id, which is what lets the already-parented sub-device keep
        resolving without any via_device sweep anywhere in this phase.
        """
        entry = _make_entry(hass)
        hub, child, rows = _seed_old_shape_install(entry, entity_registry, device_registry)
        hub_device_id = hub.id

        assert await async_migrate_entry(hass, entry) is True
        assert entry.version == 2

        migrated_hub = device_registry.async_get(hub_device_id)
        assert migrated_hub.identifiers == {(DOMAIN, f"hub_{HID}_{MID}")}

        # The row moved rather than being recreated, so every child's parent
        # link survives untouched.
        assert migrated_hub.id == hub_device_id
        assert device_registry.async_get(child.id).via_device_id == hub_device_id

        for suffix in ("rssi", "mac"):
            seeded = rows[suffix]
            moved = entity_registry.async_get(seeded.entity_id)
            assert moved is not None, f"the {suffix} row lost its entity_id"
            assert moved.unique_id == f"{DOMAIN}_hub_{HID}_{MID}_{suffix}"
            assert moved.name == f"My Hub {suffix.upper()}"

        # Already in the target shape, so it is not a member of the closed
        # suffix set and is never passed to async_update_entity at all.
        connectivity = entity_registry.async_get(rows["connectivity"].entity_id)
        assert connectivity.unique_id == f"{DOMAIN}_hub_{HID}_{MID}_connectivity"

    @pytest.mark.asyncio
    async def test_an_already_correct_row_is_never_rewritten(self, hass, entity_registry, device_registry):
        """The connectivity row is not merely unchanged in value; it is untouched.

        Asserting its unique_id is what it was would also pass if the migration
        wrote the identical string back over it, which would be a real defect
        on a two-hub home, where the sibling hub's row is the one at risk.
        """
        entry = _make_entry(hass)
        _hub, _child, rows = _seed_old_shape_install(entry, entity_registry, device_registry)
        connectivity_id = rows["connectivity"].entity_id

        seen = []
        real_update = entity_registry.async_update_entity

        def recording_update(entity_id, **kwargs):
            seen.append(entity_id)
            return real_update(entity_id, **kwargs)

        entity_registry.async_update_entity = recording_update
        try:
            assert await async_migrate_entry(hass, entry) is True
        finally:
            entity_registry.async_update_entity = real_update

        assert connectivity_id not in seen


class TestMidResolutionSources:
    """The two ordered sources, their filter, and their tie-break."""

    @pytest.mark.asyncio
    async def test_a_parented_sub_device_names_the_hub_that_keeps_the_row(self, hass, entity_registry, device_registry):
        """Source 1 wins over source 2, and it wins on evidence.

        Two connectivity rows are seeded, one of them naming a lower mid than
        the sub-device does. Source 2 alone would take the lower one; source 1
        decides it by which hub the sub-devices actually hang off, which is the
        hub that should keep this row and its customizations.
        """
        entry = _make_entry(hass)
        hub = device_registry.async_get_or_create(
            config_entry_id=entry.entry_id, identifiers={(DOMAIN, f"hub_{HID}")}, name="Hub"
        )
        device_registry.async_get_or_create(
            config_entry_id=entry.entry_id,
            identifiers={(DOMAIN, f"{HID}_{MID}_{ADDR}")},
            via_device=(DOMAIN, f"hub_{HID}"),
            name="Child",
        )
        for mid in (9, MID):
            entity_registry.async_get_or_create(
                "binary_sensor",
                DOMAIN,
                f"{DOMAIN}_hub_{HID}_{mid}_connectivity",
                config_entry=entry,
                suggested_object_id=f"conn_{mid}",
            )

        assert await async_migrate_entry(hass, entry) is True
        assert device_registry.async_get(hub.id).identifiers == {(DOMAIN, f"hub_{HID}_{MID}")}

    @pytest.mark.asyncio
    async def test_source_two_ties_numerically_not_lexically(self, hass, entity_registry, device_registry):
        """With no parented sub-device, the lowest mid wins, sorted as a number.

        "10" sorts before "9" as text, so a bare minimum would pick the wrong
        hub here and disagree with the residual sweep's tie-break over the same
        home. Deterministic-but-inconsistent is worse than either alone,
        because the two paths share one device row.
        """
        entry = _make_entry(hass)
        hub = device_registry.async_get_or_create(
            config_entry_id=entry.entry_id, identifiers={(DOMAIN, f"hub_{HID}")}, name="Hub"
        )
        for mid in (10, 9):
            entity_registry.async_get_or_create(
                "binary_sensor",
                DOMAIN,
                f"{DOMAIN}_hub_{HID}_{mid}_connectivity",
                config_entry=entry,
                suggested_object_id=f"conn_{mid}",
            )

        assert await async_migrate_entry(hass, entry) is True
        assert device_registry.async_get(hub.id).identifiers == {(DOMAIN, f"hub_{HID}_9")}

    @pytest.mark.asyncio
    async def test_a_non_numeric_segment_defers_rather_than_raising(self, hass, entity_registry, device_registry, caplog):
        """The steady route, from the migration's side.

        The connectivity row's middle segment is not a decimal integer, so the
        filter drops it before the numeric tie-break can raise on it. A raise
        here would land in ConfigEntry.async_migrate as MIGRATION_ERROR and the
        whole integration would fail to load, which is a far worse outcome than
        one hub left on its old identity.
        """
        entry = _make_entry(hass)
        hub = device_registry.async_get_or_create(
            config_entry_id=entry.entry_id, identifiers={(DOMAIN, f"hub_{HID}")}, name="Hub"
        )
        entity_registry.async_get_or_create(
            "binary_sensor",
            DOMAIN,
            f"{DOMAIN}_hub_{HID}_abc_connectivity",
            config_entry=entry,
            suggested_object_id="conn_bad",
        )

        assert await async_migrate_entry(hass, entry) is True
        assert entry.version == 2
        assert device_registry.async_get(hub.id).identifiers == {(DOMAIN, f"hub_{HID}")}
        assert "Could not resolve the mid" in caplog.text

    @pytest.mark.asyncio
    async def test_a_sibling_hubs_connectivity_row_is_never_rewritten(self, hass, entity_registry, device_registry):
        """The two-hub home this whole selection rule exists for.

        Both hubs share the hid and both wrote a connectivity row, so both rows
        match the hub unique-id prefix. A rule of the form "migrate any
        remainder that does not already start with this hub's mid" would
        rewrite the sibling's row into one carrying a foreign mid segment,
        destroying that entity's identity and orphaning its recorder history.
        """
        entry = _make_entry(hass)
        device_registry.async_get_or_create(config_entry_id=entry.entry_id, identifiers={(DOMAIN, f"hub_{HID}")}, name="Hub")
        seeded = {}
        for mid in (MID, 201):
            row = entity_registry.async_get_or_create(
                "binary_sensor",
                DOMAIN,
                f"{DOMAIN}_hub_{HID}_{mid}_connectivity",
                config_entry=entry,
                suggested_object_id=f"conn_{mid}",
            )
            seeded[mid] = entity_registry.async_get(row.entity_id)

        assert await async_migrate_entry(hass, entry) is True

        for mid, before in seeded.items():
            after = entity_registry.async_get(before.entity_id)
            assert after.unique_id == f"{DOMAIN}_hub_{HID}_{mid}_connectivity"

    @pytest.mark.asyncio
    async def test_a_sub_device_entity_row_is_left_alone(self, hass, entity_registry, device_registry):
        """A row outside the hub namespace is never a migration candidate.

        Every other row in these fixtures shares the hid, including a sibling
        hub's, so without a row from a different namespace the prefix test's
        reject arm has no driver at all.
        """
        entry = _make_entry(hass)
        device_registry.async_get_or_create(config_entry_id=entry.entry_id, identifiers={(DOMAIN, f"hub_{HID}")}, name="Hub")
        entity_registry.async_get_or_create(
            "binary_sensor",
            DOMAIN,
            f"{DOMAIN}_hub_{HID}_{MID}_connectivity",
            config_entry=entry,
            suggested_object_id="conn",
        )
        sub = entity_registry.async_get_or_create(
            "sensor",
            DOMAIN,
            f"{DOMAIN}_{HID}_{MID}_{ADDR}_moisture",
            config_entry=entry,
            suggested_object_id="soil_moisture",
        )

        assert await async_migrate_entry(hass, entry) is True
        assert entity_registry.async_get(sub.entity_id).unique_id == f"{DOMAIN}_{HID}_{MID}_{ADDR}_moisture"


class TestMigrationFailureBranches:
    """What the migration does when a half cannot complete."""

    @pytest.mark.asyncio
    async def test_an_unreadable_registry_returns_false_and_leaves_the_version(self, hass, monkeypatch):
        """False is reserved for the one case where nothing at all was read.

        It is expensive: Home Assistant sets MIGRATION_ERROR and never calls
        async_setup_entry, so the integration does not load that session at
        all. It is correct here anyway, because the entry stays at version 1
        and the next start genuinely re-runs the migration.
        """
        import custom_components.rainpoint as rp

        entry = _make_entry(hass)

        def boom(_hass):
            raise RuntimeError("registry unavailable")

        monkeypatch.setattr(rp.er, "async_get", boom)
        assert await async_migrate_entry(hass, entry) is False
        assert entry.version == 1

    @pytest.mark.asyncio
    async def test_a_failed_device_move_leaves_that_hubs_entity_rows_alone(
        self, hass, entity_registry, device_registry, caplog, monkeypatch
    ):
        """A hub whose device row did not move must not move its entity halves.

        Entity rows carrying an identity whose device row does not exist would
        describe a hub nothing writes to. The failure is injected rather than
        provoked with a competing row, deliberately: the handler has one arm and
        logs one way regardless of why the move failed, and a competing row
        would also put a second, new-shape row in the working set, which has an
        entity pass of its own. That state is the next test.
        """
        entry = _make_entry(hass)
        hub, _child, rows = _seed_old_shape_install(entry, entity_registry, device_registry)

        def boom(device_id, **kwargs):
            raise RuntimeError("registry write rejected")

        monkeypatch.setattr(device_registry, "async_update_device", boom)
        with caplog.at_level("WARNING"):
            assert await async_migrate_entry(hass, entry) is True

        assert device_registry.async_get(hub.id).identifiers == {(DOMAIN, f"hub_{HID}")}
        assert entity_registry.async_get(rows["mac"].entity_id).unique_id == f"{DOMAIN}_hub_{HID}_mac"
        assert entity_registry.async_get(rows["rssi"].entity_id).unique_id == f"{DOMAIN}_hub_{HID}_rssi"
        assert "Could not re-key hub device row" in caplog.text

    @pytest.mark.asyncio
    async def test_a_competing_row_splits_the_hub_and_says_so(self, hass, entity_registry, device_registry, caplog):
        """What the user is actually left with when the target is already taken.

        This is the permanent state the warning exists to make visible, so it is
        asserted rather than described. The original row keeps its user-set name
        and its parented sub-device while carrying an identifier nothing writes
        any more; the competing row is new-shape, so it is in the working set
        too and its own entity pass moves the hub entity rows onto it. The
        device registry offers no in-place merge, so nothing here recovers the
        abandoned row.
        """
        entry = _make_entry(hass)
        old, child, rows = _seed_old_shape_install(entry, entity_registry, device_registry)
        competing = device_registry.async_get_or_create(
            config_entry_id=entry.entry_id,
            identifiers={(DOMAIN, f"hub_{HID}_{MID}")},
            name="Competing hub",
        )

        with caplog.at_level("WARNING"):
            assert await async_migrate_entry(hass, entry) is True

        abandoned = device_registry.async_get(old.id)
        assert abandoned.identifiers == {(DOMAIN, f"hub_{HID}")}
        assert abandoned.name_by_user == "Kitchen Hub"
        assert device_registry.async_get(child.id).via_device_id == old.id
        assert device_registry.async_get(competing.id) is not None
        assert entity_registry.async_get(rows["mac"].entity_id).unique_id == f"{DOMAIN}_hub_{HID}_{MID}_mac"
        assert "Could not re-key hub device row" in caplog.text

    @pytest.mark.asyncio
    async def test_an_entity_collision_skips_that_row_and_continues(self, hass, entity_registry, device_registry, caplog):
        """One taken unique_id must not cost the rest of the loop.

        The later row moving is what proves the loop continued rather than
        aborting on the first ValueError, and the losing row surviving is what
        proves the collision branch skips rather than deletes.
        """
        entry = _make_entry(hass)
        _hub, _child, rows = _seed_old_shape_install(entry, entity_registry, device_registry)
        squatter = entity_registry.async_get_or_create(
            "sensor",
            DOMAIN,
            f"{DOMAIN}_hub_{HID}_{MID}_mac",
            config_entry=entry,
            suggested_object_id="squatter",
        )

        with caplog.at_level("WARNING"):
            assert await async_migrate_entry(hass, entry) is True

        assert entity_registry.async_get(rows["mac"].entity_id).unique_id == f"{DOMAIN}_hub_{HID}_mac"
        assert entity_registry.async_get(squatter.entity_id) is not None
        assert entity_registry.async_get(rows["rssi"].entity_id).unique_id == f"{DOMAIN}_hub_{HID}_{MID}_rssi"
        assert "Could not re-key hub entity" in caplog.text

    @pytest.mark.asyncio
    async def test_a_half_completed_run_finishes_on_retry(self, hass, entity_registry, device_registry):
        """The two registries save on independent debounced timers.

        A crash can therefore flush the device half without the entity half.
        The retry has to read the mid off the already-migrated device row; a
        helper that only recognised the old shape would skip this hub forever,
        with the version already burned.
        """
        entry = _make_entry(hass)
        hub, _child, rows = _seed_old_shape_install(entry, entity_registry, device_registry)
        device_registry.async_update_device(hub.id, new_identifiers={(DOMAIN, f"hub_{HID}_{MID}")})

        assert await async_migrate_entry(hass, entry) is True
        assert entity_registry.async_get(rows["mac"].entity_id).unique_id == f"{DOMAIN}_hub_{HID}_{MID}_mac"

    @pytest.mark.asyncio
    async def test_a_second_run_changes_nothing(self, hass, entity_registry, device_registry):
        """Idempotence, asserted on the registry rather than on a return value."""
        entry = _make_entry(hass)
        hub, _child, rows = _seed_old_shape_install(entry, entity_registry, device_registry)

        assert await async_migrate_entry(hass, entry) is True
        after_first = {
            "hub": device_registry.async_get(hub.id).identifiers,
            "mac": entity_registry.async_get(rows["mac"].entity_id).unique_id,
        }

        assert await async_migrate_entry(hass, entry) is True
        assert device_registry.async_get(hub.id).identifiers == after_first["hub"]
        assert entity_registry.async_get(rows["mac"].entity_id).unique_id == after_first["mac"]

    @pytest.mark.asyncio
    async def test_a_malformed_hub_identifier_is_not_a_migration_candidate(self, hass, entity_registry, device_registry):
        """A hub_-prefixed value that is neither shape is rejected outright.

        Folding this arm into the prefix test to avoid writing a test for it
        would make any future three-segment hub identifier silently
        migratable, which is the class of silent misread this selection rule
        exists to prevent.
        """
        entry = _make_entry(hass)
        odd = device_registry.async_get_or_create(
            config_entry_id=entry.entry_id,
            identifiers={(DOMAIN, f"hub_{HID}_{MID}_extra")},
            name="Odd row",
        )

        assert await async_migrate_entry(hass, entry) is True
        assert device_registry.async_get(odd.id).identifiers == {(DOMAIN, f"hub_{HID}_{MID}_extra")}

    @pytest.mark.asyncio
    async def test_another_config_entry_is_untouched(self, hass, entity_registry, device_registry):
        """Both registry fetches are config-entry scoped; there is no whole-registry scan."""
        entry = _make_entry(hass)
        other = MockConfigEntry(domain=DOMAIN, data={"email": "z@z.z"}, options={}, version=1)
        other.add_to_hass(hass)
        _seed_old_shape_install(entry, entity_registry, device_registry)
        foreign = device_registry.async_get_or_create(
            config_entry_id=other.entry_id,
            identifiers={(DOMAIN, "hub_999")},
            name="Someone else's hub",
        )

        assert await async_migrate_entry(hass, entry) is True
        assert device_registry.async_get(foreign.id).identifiers == {(DOMAIN, "hub_999")}


def _hub_record(mid=MID, hid=HID, real=True):
    """A coordinator hub record. real=False is the Bluetooth wrapper shape.

    The wrapper's identity fields are empty strings rather than missing keys,
    which is exactly why is_hub_record tests truthiness rather than presence,
    and it still carries a mid, which is why filtering it out matters.
    """
    identity = (
        {"did": "did-1", "mac": "AA:BB:CC", "productKey": "pk1", "model": "HWG0358WRF"}
        if real
        else {"did": "", "mac": "", "productKey": "", "model": ""}
    )
    return {"hid": hid, "mid": mid, "name": "Hub", **identity}


def _coordinator(hubs):
    coordinator = MagicMock()
    coordinator.data = {"hubs": list(hubs)}
    return coordinator


class TestResidualSweep:
    """The coordinator-backed pass that finishes what the migration could not."""

    @pytest.mark.asyncio
    async def test_it_resolves_from_the_coordinators_own_hub_record(self, hass, entity_registry, device_registry):
        """The authoritative source, and the one the migration cannot reach.

        The migration runs before any coordinator exists, so a hub with no
        connectivity row and no parented sub-device is unresolvable there. This
        pass reads the mid off the hub record itself.
        """
        from custom_components.rainpoint import _complete_hub_identity_rekey

        entry = _make_entry(hass)
        hub = device_registry.async_get_or_create(
            config_entry_id=entry.entry_id, identifiers={(DOMAIN, f"hub_{HID}")}, name="Hub"
        )
        row = entity_registry.async_get_or_create(
            "sensor", DOMAIN, f"{DOMAIN}_hub_{HID}_mac", config_entry=entry, suggested_object_id="hub_mac"
        )

        assert await async_migrate_entry(hass, entry) is True
        assert device_registry.async_get(hub.id).identifiers == {(DOMAIN, f"hub_{HID}")}

        residual = _complete_hub_identity_rekey(hass, entry, _coordinator([_hub_record()]))

        assert residual == frozenset()
        assert device_registry.async_get(hub.id).identifiers == {(DOMAIN, f"hub_{HID}_{MID}")}
        assert entity_registry.async_get(row.entity_id).unique_id == f"{DOMAIN}_hub_{HID}_{MID}_mac"

    @pytest.mark.asyncio
    async def test_a_wrapper_record_never_supplies_the_mid(self, hass, device_registry):
        """The Bluetooth wrapper record is kept in the hub list on purpose.

        It carries a mid, and here a lower one than the real hub, so the
        lowest-mid tie-break would take it deterministically rather than
        rarely. Re-keying the hub device row to a wrapper's mid would make the
        row new-shape, silence this sweep forever, and let the platforms create
        a second correctly-keyed row on the same start.
        """
        from custom_components.rainpoint import _complete_hub_identity_rekey

        entry = _make_entry(hass)
        hub = device_registry.async_get_or_create(
            config_entry_id=entry.entry_id, identifiers={(DOMAIN, f"hub_{HID}")}, name="Hub"
        )
        coordinator = _coordinator([_hub_record(mid=5, real=False), _hub_record(mid=MID)])

        assert _complete_hub_identity_rekey(hass, entry, coordinator) == frozenset()
        assert device_registry.async_get(hub.id).identifiers == {(DOMAIN, f"hub_{HID}_{MID}")}

    @pytest.mark.asyncio
    async def test_a_wrapper_only_home_is_left_untouched_and_stays_residual(self, hass, device_registry):
        """The is_hub_record filter is what gives the no-mid branch a route.

        Without it the wrapper's mid would satisfy the branch and leave it
        unreachable, so the filter closes the data-loss hole and makes the skip
        testable at the same time.
        """
        from custom_components.rainpoint import _complete_hub_identity_rekey

        entry = _make_entry(hass)
        hub = device_registry.async_get_or_create(
            config_entry_id=entry.entry_id, identifiers={(DOMAIN, f"hub_{HID}")}, name="Hub"
        )

        residual = _complete_hub_identity_rekey(hass, entry, _coordinator([_hub_record(real=False)]))

        assert residual == frozenset({str(HID)})
        assert device_registry.async_get(hub.id).identifiers == {(DOMAIN, f"hub_{HID}")}

    @pytest.mark.asyncio
    async def test_a_mismatched_hid_supplies_nothing(self, hass, device_registry):
        """hid is an int on a coordinator record and a str off an identifier.

        An unnormalized comparison is 100 == "100", which is always False, and
        would leave this whole path inert while looking wired up. The record
        below is a real hub for a different home, so it must be rejected on the
        hid rather than on its shape.
        """
        from custom_components.rainpoint import _complete_hub_identity_rekey

        entry = _make_entry(hass)
        device_registry.async_get_or_create(config_entry_id=entry.entry_id, identifiers={(DOMAIN, f"hub_{HID}")}, name="Hub")

        assert _complete_hub_identity_rekey(hass, entry, _coordinator([_hub_record(hid=999)])) == frozenset({str(HID)})

    @pytest.mark.asyncio
    async def test_the_lowest_mid_wins_numerically(self, hass, device_registry):
        """Same tie-break and same sort key the migration uses, over the same home."""
        from custom_components.rainpoint import _complete_hub_identity_rekey

        entry = _make_entry(hass)
        hub = device_registry.async_get_or_create(
            config_entry_id=entry.entry_id, identifiers={(DOMAIN, f"hub_{HID}")}, name="Hub"
        )
        coordinator = _coordinator([_hub_record(mid=10), _hub_record(mid=9)])

        assert _complete_hub_identity_rekey(hass, entry, coordinator) == frozenset()
        assert device_registry.async_get(hub.id).identifiers == {(DOMAIN, f"hub_{HID}_9")}

    @pytest.mark.asyncio
    async def test_a_non_numeric_mid_defers_without_raising(self, hass, device_registry):
        """The steady route from the sweep's side, and it must not raise.

        The value is dropped by the filter on every pass, so the row stays
        old-shape and residual indefinitely. That is accepted; taking the
        integration down on a defensive branch would not be.
        """
        from custom_components.rainpoint import _complete_hub_identity_rekey

        entry = _make_entry(hass)
        hub = device_registry.async_get_or_create(
            config_entry_id=entry.entry_id, identifiers={(DOMAIN, f"hub_{HID}")}, name="Hub"
        )

        residual = _complete_hub_identity_rekey(hass, entry, _coordinator([_hub_record(mid=-5)]))

        assert residual == frozenset({str(HID)})
        assert device_registry.async_get(hub.id).identifiers == {(DOMAIN, f"hub_{HID}")}

    @pytest.mark.asyncio
    async def test_an_unreadable_registry_returns_none_rather_than_empty(self, hass, monkeypatch):
        """None and an empty frozenset must never be collapsed.

        An empty frozenset claims this pass looked and found nothing left to
        do. A pass that could not read a registry is in no position to claim
        that, and the caller's latch reads the difference.
        """
        import custom_components.rainpoint as rp

        entry = _make_entry(hass)

        def boom(_hass):
            raise RuntimeError("registry unavailable")

        monkeypatch.setattr(rp.dr, "async_get", boom)
        assert rp._complete_hub_identity_rekey(hass, entry, _coordinator([_hub_record()])) is None

    @pytest.mark.asyncio
    async def test_an_unreadable_hub_list_returns_none(self, hass, device_registry):
        """A read that raises is could-not-look; an empty list is not."""
        from custom_components.rainpoint import _complete_hub_identity_rekey

        entry = _make_entry(hass)
        device_registry.async_get_or_create(config_entry_id=entry.entry_id, identifiers={(DOMAIN, f"hub_{HID}")}, name="Hub")
        coordinator = MagicMock()
        type(coordinator).data = property(lambda _self: (_ for _ in ()).throw(RuntimeError("no data")))

        assert _complete_hub_identity_rekey(hass, entry, coordinator) is None

    @pytest.mark.asyncio
    async def test_an_empty_hub_list_is_a_successful_read(self, hass, device_registry):
        """A device-list outage empties this list, and that is not a failure.

        Returning None here instead would make the caller re-run a full
        two-registry sweep on every poll and every pushed frame, indefinitely,
        on exactly the installs that already hold a residual.
        """
        from custom_components.rainpoint import _complete_hub_identity_rekey

        entry = _make_entry(hass)
        device_registry.async_get_or_create(config_entry_id=entry.entry_id, identifiers={(DOMAIN, f"hub_{HID}")}, name="Hub")

        assert _complete_hub_identity_rekey(hass, entry, _coordinator([])) == frozenset({str(HID)})

    @pytest.mark.asyncio
    async def test_a_hub_whose_move_raised_is_still_reported_residual(self, hass, device_registry, monkeypatch, caplog):
        """The residual set is re-read from the registry, not tallied from intent.

        A hub whose move raised was attempted and did not move. A set built from
        "hids I decided to move" would omit it, report nothing outstanding, and
        let the caller latch shut over a hub whose identity is permanently split
        across two device rows.
        """
        from custom_components.rainpoint import _complete_hub_identity_rekey

        entry = _make_entry(hass)
        device_registry.async_get_or_create(config_entry_id=entry.entry_id, identifiers={(DOMAIN, f"hub_{HID}")}, name="Hub")

        def boom(device_id, **kwargs):
            raise RuntimeError("registry write rejected")

        monkeypatch.setattr(device_registry, "async_update_device", boom)
        with caplog.at_level("WARNING"):
            residual = _complete_hub_identity_rekey(hass, entry, _coordinator([_hub_record()]))

        assert residual == frozenset({str(HID)})
        assert "Could not re-key hub device row" in caplog.text

    @pytest.mark.asyncio
    async def test_a_cleanly_migrated_install_is_a_no_op(self, hass, entity_registry, device_registry):
        """Every hub row already new-shape means nothing to do and nothing touched."""
        from custom_components.rainpoint import _complete_hub_identity_rekey

        entry = _make_entry(hass)
        hub, _child, rows = _seed_old_shape_install(entry, entity_registry, device_registry)
        assert await async_migrate_entry(hass, entry) is True

        assert _complete_hub_identity_rekey(hass, entry, _coordinator([_hub_record()])) == frozenset()
        assert device_registry.async_get(hub.id).identifiers == {(DOMAIN, f"hub_{HID}_{MID}")}
        assert entity_registry.async_get(rows["mac"].entity_id).unique_id == f"{DOMAIN}_hub_{HID}_{MID}_mac"


class TestResidualSweepRetryCadence:
    """The wrapper: when it arms, when it re-runs, and when it stops."""

    @staticmethod
    def _armed_listener(coordinator):
        assert coordinator.async_add_listener.call_count == 1
        return coordinator.async_add_listener.call_args[0][0]

    @pytest.mark.asyncio
    async def test_a_clean_install_arms_nothing(self, hass, entity_registry, device_registry):
        """Steady-state cost on the overwhelming majority of installs is zero."""
        from custom_components.rainpoint import _complete_hub_identity_rekey_on_updates

        entry = _make_entry(hass)
        _seed_old_shape_install(entry, entity_registry, device_registry)
        assert await async_migrate_entry(hass, entry) is True

        coordinator = _coordinator([_hub_record()])
        _complete_hub_identity_rekey_on_updates(hass, entry, coordinator)

        coordinator.async_add_listener.assert_not_called()

    @pytest.mark.asyncio
    async def test_a_residual_completes_on_the_poll_that_returns_the_hub(self, hass, device_registry):
        """The device-list outage route, driven end to end.

        The first refresh lands inside an outage, so the setup pass has no
        record to read and the hub waits. On a real Home Assistant install the
        next restart can be months away, which is why this arms a listener
        rather than accepting a setup-only pass.
        """
        from custom_components.rainpoint import _complete_hub_identity_rekey_on_updates

        entry = _make_entry(hass)
        hub = device_registry.async_get_or_create(
            config_entry_id=entry.entry_id, identifiers={(DOMAIN, f"hub_{HID}")}, name="Hub"
        )
        coordinator = _coordinator([])

        _complete_hub_identity_rekey_on_updates(hass, entry, coordinator)
        assert device_registry.async_get(hub.id).identifiers == {(DOMAIN, f"hub_{HID}")}

        listener = self._armed_listener(coordinator)
        coordinator.data = {"hubs": [_hub_record()]}
        listener()

        assert device_registry.async_get(hub.id).identifiers == {(DOMAIN, f"hub_{HID}_{MID}")}

    @pytest.mark.asyncio
    async def test_an_unchanged_hub_mapping_costs_no_registry_reads(self, hass, device_registry, monkeypatch):
        """The gate, asserted on calls rather than on end state.

        The end state is identical whether or not the gate exists, so counting
        registry fetches across several notifications is the only thing that
        distinguishes them. Both halves are asserted together: a gate that never
        opens would satisfy the closing half alone, and that is the exact bug
        the gate risks introducing.
        """
        import custom_components.rainpoint as rp

        entry = _make_entry(hass)
        hub = device_registry.async_get_or_create(
            config_entry_id=entry.entry_id, identifiers={(DOMAIN, f"hub_{HID}")}, name="Hub"
        )
        # A wrapper-only list is a successful read yielding no candidate, so the
        # sweep stays residual without the read itself having failed.
        coordinator = _coordinator([_hub_record(real=False)])
        rp._complete_hub_identity_rekey_on_updates(hass, entry, coordinator)
        listener = self._armed_listener(coordinator)

        fetches = []
        real_fetch = rp._fetch_registry_rows
        monkeypatch.setattr(
            rp,
            "_fetch_registry_rows",
            lambda *args, **kwargs: (fetches.append(args[-1]), real_fetch(*args, **kwargs))[1],
        )

        for _ in range(3):
            listener()
        assert fetches == [], "an unchanged hub mapping must not re-read either registry"

        coordinator.data = {"hubs": [_hub_record(real=False), _hub_record()]}
        listener()
        assert fetches, "the poll that changed the mapping must re-run the sweep"
        assert device_registry.async_get(hub.id).identifiers == {(DOMAIN, f"hub_{HID}_{MID}")}

    @pytest.mark.asyncio
    async def test_a_pass_that_could_not_look_stays_armed_and_retries(self, hass, device_registry, monkeypatch):
        """The arm-never hole, closed.

        A pass that could not read a registry observes zero unresolved rows. On
        a bare truthiness test it would arm nothing and latch shut, with the
        version boundary already burned, stranding the hub until the next
        restart. It also retries unconditionally rather than waiting for the
        hub mapping to change, because the thing that failed was not the hub
        list.
        """
        import custom_components.rainpoint as rp

        entry = _make_entry(hass)
        hub = device_registry.async_get_or_create(
            config_entry_id=entry.entry_id, identifiers={(DOMAIN, f"hub_{HID}")}, name="Hub"
        )
        coordinator = _coordinator([_hub_record()])

        real_get = rp.dr.async_get
        monkeypatch.setattr(rp.dr, "async_get", lambda _hass: (_ for _ in ()).throw(RuntimeError("unreadable")))
        rp._complete_hub_identity_rekey_on_updates(hass, entry, coordinator)
        listener = self._armed_listener(coordinator)
        assert device_registry.async_get(hub.id).identifiers == {(DOMAIN, f"hub_{HID}")}

        monkeypatch.setattr(rp.dr, "async_get", real_get)
        # The hub mapping is unchanged since the setup pass, so only the
        # could-not-look arm can drive this.
        listener()

        assert device_registry.async_get(hub.id).identifiers == {(DOMAIN, f"hub_{HID}_{MID}")}

    @pytest.mark.asyncio
    async def test_the_latch_closes_once_a_pass_finds_nothing_outstanding(self, hass, device_registry, monkeypatch):
        """Only a positively empty residual may close it, and then it stays closed."""
        import custom_components.rainpoint as rp

        entry = _make_entry(hass)
        device_registry.async_get_or_create(config_entry_id=entry.entry_id, identifiers={(DOMAIN, f"hub_{HID}")}, name="Hub")
        coordinator = _coordinator([])
        rp._complete_hub_identity_rekey_on_updates(hass, entry, coordinator)
        listener = self._armed_listener(coordinator)

        coordinator.data = {"hubs": [_hub_record()]}
        listener()

        fetches = []
        real_fetch = rp._fetch_registry_rows
        monkeypatch.setattr(
            rp,
            "_fetch_registry_rows",
            lambda *args, **kwargs: (fetches.append(args[-1]), real_fetch(*args, **kwargs))[1],
        )
        coordinator.data = {"hubs": [_hub_record(mid=999)]}
        listener()

        assert fetches == [], "a settled install must not sweep again on a later mapping change"

    @pytest.mark.asyncio
    async def test_the_first_decline_for_a_hid_is_loud_and_later_ones_are_quiet(self, hass, device_registry, caplog):
        """A persistently unresolvable mid warns once, then goes quiet.

        Before this fix, every pass after the version-boundary migration logged
        this decline at DEBUG only, so a hub stuck on the non-numeric/negative-mid
        steady route (device.py writes the mid verbatim; this sweep's isdigit
        filter drops it on every pass) had no durable signal past the single
        WARNING async_migrate_entry logs once at the version boundary. Now the
        first residual-sweep decline for a given hid is WARNING, and a later
        decline for that same hid, on the same entry, drops back to DEBUG so a
        hub that never resolves does not warn on every poll forever.
        """
        import logging

        import custom_components.rainpoint as rp

        entry = _make_entry(hass)
        device_registry.async_get_or_create(config_entry_id=entry.entry_id, identifiers={(DOMAIN, f"hub_{HID}")}, name="Hub")

        caplog.set_level(logging.DEBUG, logger="custom_components.rainpoint")
        coordinator = _coordinator([_hub_record(mid=-5)])
        rp._complete_hub_identity_rekey_on_updates(hass, entry, coordinator)
        listener = self._armed_listener(coordinator)

        first_pass_warnings = [r for r in caplog.records if r.levelno == logging.WARNING and "No mid available" in r.message]
        assert len(first_pass_warnings) == 1, "the first decline for this hid must be loud"
        # The reason separates the two conditions behind a decline, which call
        # for opposite responses. This one is permanent until the cloud changes
        # it, so the line must not read as something a later poll will fix.
        assert "mid that is not a decimal integer" in first_pass_warnings[0].getMessage()

        caplog.clear()
        # A second, unrelated real hub changes the mapping so the gate reopens,
        # while hid 100's mid stays -5 (still isdigit-false) and declines again.
        coordinator.data = {"hubs": [_hub_record(mid=-5), _hub_record(hid=HID + 1, mid=300)]}
        listener()

        second_pass_warnings = [r for r in caplog.records if r.levelno == logging.WARNING and "No mid available" in r.message]
        second_pass_debugs = [r for r in caplog.records if r.levelno == logging.DEBUG and "No mid available" in r.message]
        assert second_pass_warnings == [], "a repeat decline for the same hid must not warn a second time"
        assert len(second_pass_debugs) == 1
        # The repeat line carries the reason too, because once a hid has been
        # announced this is the only place a change in it is visible.
        assert "mid that is not a decimal integer" in second_pass_debugs[0].getMessage()

    @pytest.mark.asyncio
    async def test_a_decline_says_which_of_the_two_conditions_produced_it(self, hass, device_registry, caplog):
        """A hub absent from this poll and one whose record is unreadable both
        decline, and until the reason rode along they read identically.

        They call for opposite responses. Absence is transient by construction:
        a getDeviceByHid response can omit a hub the previous poll listed, which
        is why this sweep re-runs on updates at all, so the deferral the line
        promises is real. An unreadable mid is permanent until the cloud changes
        it, because device.py writes it verbatim while this sweep filters on
        isdigit, so waiting for a later poll achieves nothing.
        """
        import logging

        import custom_components.rainpoint as rp

        entry = _make_entry(hass)
        device_registry.async_get_or_create(config_entry_id=entry.entry_id, identifiers={(DOMAIN, f"hub_{HID}")}, name="Hub")

        caplog.set_level(logging.DEBUG, logger="custom_components.rainpoint")
        # A poll that carries a real hub, but not this hid's.
        coordinator = _coordinator([_hub_record(hid=HID + 1, mid=300)])
        rp._complete_hub_identity_rekey_on_updates(hass, entry, coordinator)

        warnings = [r for r in caplog.records if r.levelno == logging.WARNING and "No mid available" in r.message]
        assert len(warnings) == 1
        assert "no record for it in this poll" in warnings[0].getMessage()

    def test_the_reason_reads_the_wrapper_record_as_no_record_at_all(self):
        """The reason is derived from the same is_hub_record-filtered list the
        sweep resolves candidates from, so a hid whose only top-level record is
        the Bluetooth wrapper is reported as absent rather than as unreadable.

        Called directly: the phrase is what production hands the log line, and
        deriving it a second way here would only assert this test's own copy.
        """
        from custom_components.rainpoint import _residual_mid_decline_reason

        # An unreadable mid on the record that is present, because a readable
        # one resolves and never reaches the decline this phrase describes.
        real_hubs = [_hub_record(hid=HID + 1, mid=-5)]

        assert _residual_mid_decline_reason(str(HID), real_hubs) == "no record for it in this poll"
        assert _residual_mid_decline_reason(str(HID + 1), real_hubs) == "its record carries a mid that is not a decimal integer"


# ---------------------------------------------------------------------------
# Real-coordinator scaffolding
#
# Everything below drives a real RainPointCoordinator through the real
# construct -> first refresh -> (optionally) platform setup -> async_refresh
# sequence, rather than assigning coordinator.data. The sweep's whole point is
# that it reads a freshly polled hub record from the window in which that
# record exists, and a snapshot handed to it directly answers a different
# question. It also matters for typing: _collect_hubs re-injects hid as the
# int the config entry carries, so a hand-built record with a str hid would
# pass against a broken hub["hid"] == hid comparison and prove nothing.
# ---------------------------------------------------------------------------


def _poll_record(mid=MID, real=True, sub_devices=()):
    """One top-level record as getDeviceByHid returns it.

    real=False is the Bluetooth wrapper shape, whose identity fields are empty
    strings rather than missing keys. It still carries a mid, which is why
    is_hub_record has to filter it out before any candidate is taken.
    """
    identity = (
        {"did": "did-1", "mac": "AA:BB:CC", "productKey": "pk1", "model": "HWG0358WRF", "deviceName": "d"}
        if real
        else {"did": "", "mac": "", "productKey": "", "model": "", "deviceName": ""}
    )
    return {
        "mid": mid,
        "name": "Hub" if real else "",
        "homeName": "Home",
        "softVer": "1.2.3",
        "subDevices": list(sub_devices),
        **identity,
    }


def _make_client(*polls):
    """A client whose successive get_devices_by_hid calls return successive polls.

    The last poll repeats for every later call, so a test can drive as many
    async_refresh() calls as it likes after the sequence it cares about.
    """
    from unittest.mock import AsyncMock

    remaining = [list(poll) for poll in polls] or [[]]
    client = MagicMock()
    client.restore_tokens = MagicMock()
    client.export_tokens = MagicMock(return_value={})
    client.register_relogin_listener = MagicMock()
    client.list_homes = AsyncMock(return_value=[{"hid": HID, "name": "Home"}])

    async def _by_hid(_hid):
        return remaining.pop(0) if len(remaining) > 1 else remaining[0]

    client.get_devices_by_hid = AsyncMock(side_effect=_by_hid)
    client.get_multiple_device_status = AsyncMock(return_value=[])
    client.get_device_status = AsyncMock(return_value={})
    return client


async def _real_coordinator(hass, entry, client):
    """Construct and first-refresh a real coordinator, as async_setup_entry does."""
    from custom_components.rainpoint.coordinator import RainPointCoordinator

    coordinator = RainPointCoordinator(hass, client, entry)
    await coordinator.async_config_entry_first_refresh()
    return coordinator


async def _setup_with_patched_forward(hass, entry, client, device_registry, monkeypatch, built=None):
    """Drive this integration's own async_setup_entry, forward included.

    Home Assistant's entity-platform layer is not reachable in this repository's
    test setup, so the forward is supplied by the harness: each platform
    module's own async_setup_entry runs with a capturing callback, and each
    built entity's device_info is registered exactly as entity_platform would
    register it. What that proves is what this integration's setup path does and
    what the registries end up holding; it does not prove that Home Assistant
    would have added those entities. Nothing asserted on it depends on the
    untested layer: the identifier written below is the one device.py computed.

    The client is injected by pre-seeding hass.data rather than by patching
    RainPointClient, because async_setup_entry reads the stored client before
    constructing one, so this exercises more of the real path.
    """
    import importlib

    import custom_components.rainpoint as rp

    built = {} if built is None else built
    hass.data.setdefault(DOMAIN, {}).setdefault(entry.entry_id, {})["client"] = client

    async def fake_forward(cfg_entry, platforms):
        for platform in platforms:
            module = importlib.import_module(f"custom_components.rainpoint.{platform}")
            captured = []

            def add(entities, update_before_add=False, _c=captured):
                _c.extend(entities)

            await module.async_setup_entry(hass, cfg_entry, add)
            built[platform] = captured
            for entity in captured:
                info = getattr(entity, "device_info", None)
                if info:
                    device_registry.async_get_or_create(config_entry_id=cfg_entry.entry_id, **info)

    monkeypatch.setattr(hass.config_entries, "async_forward_entry_setups", fake_forward)
    result = await rp.async_setup_entry(hass, entry)
    return result, built


class TestResidualSweepAgainstARealCoordinator:
    """The sweep driven through real polls, in the real order."""

    @pytest.mark.asyncio
    async def test_a_hub_absent_from_the_first_poll_is_finished_on_a_later_one(self, hass, entity_registry, device_registry):
        """The device-list outage route, end to end and in the real order.

        The first poll omits the hub entirely, so the setup pass has no record
        to read and the row must still be old-shape at that point. The poll that
        returns the hub finishes it. A setup-only call leaves the row old-shape
        at the end of this test, which on a real install means waiting for the
        next restart.
        """
        from custom_components.rainpoint import _complete_hub_identity_rekey_on_updates

        entry = _make_entry(hass)
        hub = device_registry.async_get_or_create(
            config_entry_id=entry.entry_id, identifiers={(DOMAIN, f"hub_{HID}")}, name="Hub"
        )
        row = entity_registry.async_get_or_create(
            "sensor", DOMAIN, f"{DOMAIN}_hub_{HID}_mac", config_entry=entry, suggested_object_id="hub_mac"
        )
        entity_registry.async_update_entity(row.entity_id, name="My Hub MAC")

        coordinator = await _real_coordinator(hass, entry, _make_client([], [_poll_record()]))
        _complete_hub_identity_rekey_on_updates(hass, entry, coordinator)
        assert device_registry.async_get(hub.id).identifiers == {(DOMAIN, f"hub_{HID}")}

        await coordinator.async_refresh()

        migrated = device_registry.async_get(hub.id)
        assert migrated.identifiers == {(DOMAIN, f"hub_{HID}_{MID}")}
        assert migrated.id == hub.id
        moved = entity_registry.async_get(row.entity_id)
        assert moved.unique_id == f"{DOMAIN}_hub_{HID}_{MID}_mac"
        assert moved.name == "My Hub MAC"

    @pytest.mark.asyncio
    async def test_the_hid_comparison_survives_the_int_versus_str_boundary(self, hass, device_registry):
        """hid is an int on a coordinator record and a str off an identifier.

        This record is built by the real _collect_hubs, which re-injects the
        int the config entry carries, so an unnormalized comparison here is
        100 == "100" and the sweep would resolve nothing at all while looking
        wired up. A fixture that handed the sweep a stringified hid would pass
        against exactly that bug.
        """
        from custom_components.rainpoint import _complete_hub_identity_rekey

        entry = _make_entry(hass)
        hub = device_registry.async_get_or_create(
            config_entry_id=entry.entry_id, identifiers={(DOMAIN, f"hub_{HID}")}, name="Hub"
        )
        coordinator = await _real_coordinator(hass, entry, _make_client([_poll_record()]))
        assert isinstance(coordinator.data["hubs"][0]["hid"], int)

        assert _complete_hub_identity_rekey(hass, entry, coordinator) == frozenset()
        assert device_registry.async_get(hub.id).identifiers == {(DOMAIN, f"hub_{HID}_{MID}")}

    @pytest.mark.asyncio
    async def test_an_unchanged_poll_costs_no_registry_reads_and_a_changed_one_sweeps(self, hass, device_registry, monkeypatch):
        """The gate, across real refreshes, with both halves in one test.

        A gate that never opens satisfies the closing half alone, which is the
        exact bug the gate risks introducing, so the poll that returns the hub
        has to be asserted in the same place as the polls that do not.
        """
        import custom_components.rainpoint as rp

        entry = _make_entry(hass)
        hub = device_registry.async_get_or_create(
            config_entry_id=entry.entry_id, identifiers={(DOMAIN, f"hub_{HID}")}, name="Hub"
        )
        client = _make_client([], [], [], [_poll_record()])
        coordinator = await _real_coordinator(hass, entry, client)
        rp._complete_hub_identity_rekey_on_updates(hass, entry, coordinator)

        fetches = []
        real_fetch = rp._fetch_registry_rows
        monkeypatch.setattr(
            rp,
            "_fetch_registry_rows",
            lambda *args, **kwargs: (fetches.append(args[-1]), real_fetch(*args, **kwargs))[1],
        )

        await coordinator.async_refresh()
        await coordinator.async_refresh()
        assert fetches == [], "an unchanged hub mapping must not re-read either registry"

        await coordinator.async_refresh()
        assert fetches, "the poll that returned the hub must re-run the sweep"
        assert device_registry.async_get(hub.id).identifiers == {(DOMAIN, f"hub_{HID}_{MID}")}

    @pytest.mark.asyncio
    async def test_the_latch_closes_through_the_armed_listener(self, hass, device_registry, monkeypatch):
        """Driven through the listener, because the latch lives in its closure.

        A direct second call to the sweep re-enters nothing and would prove
        nothing about the latch. Patching the registry fetch to raise before the
        third refresh is what proves the listener returned on the latch without
        touching a registry at all.
        """
        import custom_components.rainpoint as rp

        entry = _make_entry(hass)
        hub = device_registry.async_get_or_create(
            config_entry_id=entry.entry_id, identifiers={(DOMAIN, f"hub_{HID}")}, name="Hub"
        )
        coordinator = await _real_coordinator(hass, entry, _make_client([], [_poll_record()], [_poll_record(mid=999)]))
        rp._complete_hub_identity_rekey_on_updates(hass, entry, coordinator)

        await coordinator.async_refresh()
        assert device_registry.async_get(hub.id).identifiers == {(DOMAIN, f"hub_{HID}_{MID}")}

        def boom(*args, **kwargs):
            raise AssertionError("a latched listener must not reach the registries")

        monkeypatch.setattr(rp, "_fetch_registry_rows", boom)
        await coordinator.async_refresh()

    @pytest.mark.asyncio
    async def test_a_coordinator_read_that_raises_declines_and_the_next_poll_completes(self, hass, device_registry, monkeypatch):
        """The other half of could-not-look, and the only driver for its second return.

        Every other case fails a registry read; nothing else makes the
        coordinator read fail. The read itself is made to raise rather than the
        snapshot being swapped for a different one, because only a raise reaches
        the guard. Declining is then shown to have been useful: the next real
        poll completes the re-key.
        """
        import custom_components.rainpoint as rp

        entry = _make_entry(hass)
        hub = device_registry.async_get_or_create(
            config_entry_id=entry.entry_id, identifiers={(DOMAIN, f"hub_{HID}")}, name="Hub"
        )
        coordinator = await _real_coordinator(hass, entry, _make_client([_poll_record()]))

        real_data = coordinator.data

        class _Unreadable:
            def get(self, *args, **kwargs):
                raise RuntimeError("coordinator data unreadable")

            def __bool__(self):
                return True

        coordinator.data = _Unreadable()
        assert rp._complete_hub_identity_rekey(hass, entry, coordinator) is None
        assert device_registry.async_get(hub.id).identifiers == {(DOMAIN, f"hub_{HID}")}

        rp._complete_hub_identity_rekey_on_updates(hass, entry, coordinator)
        assert coordinator._listeners, "a pass that could not look must stay armed"

        coordinator.data = real_data
        await coordinator.async_refresh()
        migrated = device_registry.async_get(hub.id)
        assert migrated.identifiers == {(DOMAIN, f"hub_{HID}_{MID}")}
        assert migrated.id == hub.id

    @pytest.mark.asyncio
    async def test_a_malformed_hubs_value_is_zero_candidates_not_an_unreadable_pass(self, hass, device_registry):
        """A hubs value that is not a list is a successful read of nothing.

        The distinction matters because None re-arms the sweep unconditionally
        on every poll and every pushed frame. A malformed value is not a failed
        observation, it is an observation that yielded no candidate mids, so it
        must fall through to the ordinary residual path like an empty list does.
        """
        import custom_components.rainpoint as rp

        entry = _make_entry(hass)
        hub = device_registry.async_get_or_create(
            config_entry_id=entry.entry_id, identifiers={(DOMAIN, f"hub_{HID}")}, name="Hub"
        )
        coordinator = await _real_coordinator(hass, entry, _make_client([_poll_record()]))

        coordinator.data = {"hubs": "not a list"}
        assert rp._read_current_hubs(coordinator) == []
        # Residual, not None: the pass looked and found no candidate.
        assert rp._complete_hub_identity_rekey(hass, entry, coordinator) == frozenset({str(HID)})
        assert device_registry.async_get(hub.id).identifiers == {(DOMAIN, f"hub_{HID}")}

    @pytest.mark.asyncio
    async def test_a_non_dict_hub_record_is_dropped_rather_than_raising(self, hass, device_registry):
        """Records come from cloud JSON that nothing validates on the way in.

        Every consumer reaches straight for hub.get(...), so a record that is
        not a dict would raise AttributeError inside the caller rather than
        inside the guard, escaping a sweep that documents that it never raises
        and aborting config entry setup.
        """
        import custom_components.rainpoint as rp

        entry = _make_entry(hass)
        hub = device_registry.async_get_or_create(
            config_entry_id=entry.entry_id, identifiers={(DOMAIN, f"hub_{HID}")}, name="Hub"
        )
        coordinator = await _real_coordinator(hass, entry, _make_client([_poll_record()]))

        # The real record the coordinator built, hid injected, not a raw fixture.
        good = coordinator.data["hubs"][0]
        coordinator.data = {"hubs": ["a bare string", None, 42, good]}
        assert rp._read_current_hubs(coordinator) == [good]
        # The one well-formed record still drives the re-key to completion.
        assert rp._complete_hub_identity_rekey(hass, entry, coordinator) == frozenset()
        assert device_registry.async_get(hub.id).identifiers == {(DOMAIN, f"hub_{HID}_{MID}")}

    @pytest.mark.asyncio
    async def test_a_cleanly_migrated_install_writes_nothing_at_all(self, hass, entity_registry, device_registry, monkeypatch):
        """No-op asserted on calls, not on end state.

        An unchanged end state is also what a sweep that rewrote every row with
        its existing value would leave behind, and that would be a real defect
        on a two-hub home.
        """
        from custom_components.rainpoint import _complete_hub_identity_rekey

        entry = _make_entry(hass)
        _seed_old_shape_install(entry, entity_registry, device_registry)
        assert await async_migrate_entry(hass, entry) is True
        coordinator = await _real_coordinator(hass, entry, _make_client([_poll_record()]))

        calls = []
        monkeypatch.setattr(device_registry, "async_update_device", lambda *a, **k: calls.append(a))
        monkeypatch.setattr(entity_registry, "async_update_entity", lambda *a, **k: calls.append(a))

        assert _complete_hub_identity_rekey(hass, entry, coordinator) == frozenset()
        assert calls == []


class TestOrderingProperties:
    """Two orderings the design rests on, recorded rather than argued."""

    @pytest.mark.asyncio
    async def test_every_hub_device_move_precedes_that_hubs_entity_moves(
        self, hass, entity_registry, device_registry, monkeypatch
    ):
        """Real call indices across both registries, so a reorder goes red.

        Home Assistant already guarantees the platform-setup half of this
        hazard by running the migration before setup, and both halves here are
        in-place updates with no get-or-create between them, so swapping the two
        loops cannot by itself create a second device row. What this pins is the
        ordering as a standing property, so a future edit that emits a
        DeviceInfo from inside either path, or moves this work after the
        platform forward, fails rather than ships.
        """
        entry = _make_entry(hass)
        _seed_old_shape_install(entry, entity_registry, device_registry)

        order = []
        real_device_update = device_registry.async_update_device
        real_entity_update = entity_registry.async_update_entity
        monkeypatch.setattr(
            device_registry,
            "async_update_device",
            lambda *a, **k: (order.append("device"), real_device_update(*a, **k))[1],
        )
        monkeypatch.setattr(
            entity_registry,
            "async_update_entity",
            lambda *a, **k: (order.append("entity"), real_entity_update(*a, **k))[1],
        )

        assert await async_migrate_entry(hass, entry) is True

        assert "device" in order
        assert "entity" in order
        assert order.index("device") < order.index("entity")

    @pytest.mark.asyncio
    async def test_the_rekey_listener_runs_before_the_late_entity_adders(self, hass, device_registry, monkeypatch):
        """Registration order is fire order, and this integration sets it.

        The wrapper registers inside async_setup_entry and sensor.py's adder
        registers inside the platform setup the forward invokes, so the re-key
        runs ahead of every late adder on every update. That is what makes a
        post-forward listener run safe: a poll that both returns a previously
        absent hub and surfaces a new sub-device re-keys the hub row first, and
        the adder writes its DeviceInfo against an already-migrated parent.

        Home Assistant's entity-platform layer is not exercised here, but it
        contributes nothing to this ordering, which is set entirely by this
        integration's own code.
        """

        entry = _make_entry(hass)
        device_registry.async_get_or_create(config_entry_id=entry.entry_id, identifiers={(DOMAIN, f"hub_{HID}")}, name="Hub")
        client = _make_client([], [_poll_record()])

        # Instrument registration rather than registering anything: each
        # listener is wrapped in place, so the order below is the order this
        # integration's own code chose, not one the test picked.
        from custom_components.rainpoint.coordinator import RainPointCoordinator

        fired = []
        real_add_listener = RainPointCoordinator.async_add_listener

        def instrumented(self, update_callback, context=None):
            label = getattr(update_callback, "__qualname__", repr(update_callback))

            def recording(*args, **kwargs):
                fired.append(label)
                return update_callback(*args, **kwargs)

            return real_add_listener(self, recording, context)

        monkeypatch.setattr(RainPointCoordinator, "async_add_listener", instrumented)

        assert (await _setup_with_patched_forward(hass, entry, client, device_registry, monkeypatch))[0] is True
        coordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]
        fired.clear()

        await coordinator.async_refresh()

        rekey = next(i for i, label in enumerate(fired) if "_complete_hub_identity_rekey_on_updates" in label)
        adder = next(i for i, label in enumerate(fired) if "async_on_coordinator_update" in label)
        assert rekey < adder


class TestTheCompetingRowWindow:
    """The one route that leaves a hub permanently split across two device rows."""

    @pytest.mark.asyncio
    async def test_a_setup_pass_that_could_not_look_leaves_two_rows_and_says_so(
        self, hass, entity_registry, device_registry, monkeypatch, caplog
    ):
        """The headline data-loss window, driven through this integration's own setup.

        With the re-key sweep unable to read a registry on its setup pass,
        async_setup_entry continues to the platform forward, the hub platforms
        write the migrated identifier, and a fresh device row appears beside the
        old-shape one. Every later sweep then collides on that identifier and
        the original row stays old-shape permanently, keeping its area, its
        user-set name and its sub-devices while nothing writes to it any more.

        The patch is scoped by caller, not by call index: three sweeps call
        _fetch_registry_rows during one setup, so failing the first call or the
        first N calls would starve the wrong one and produce a green test about
        nothing. Only the residual re-key's own fetches are failed here.

        Home Assistant's entity-platform layer is not exercised, so what this
        proves is what this integration's setup path does and what the
        registries end up holding, not that HA would have added those entities.
        The competing identifier under assertion is the one device.py computed.
        """
        import custom_components.rainpoint as rp

        entry = _make_entry(hass, version=2)
        old = device_registry.async_get_or_create(
            config_entry_id=entry.entry_id, identifiers={(DOMAIN, f"hub_{HID}")}, name="Hub"
        )
        device_registry.async_update_device(old.id, name_by_user="Kitchen Hub")
        child = device_registry.async_get_or_create(
            config_entry_id=entry.entry_id,
            identifiers={(DOMAIN, f"{HID}_{MID}_{ADDR}")},
            via_device=(DOMAIN, f"hub_{HID}"),
            name="Child",
        )
        row = entity_registry.async_get_or_create(
            "sensor", DOMAIN, f"{DOMAIN}_hub_{HID}_mac", config_entry=entry, suggested_object_id="hub_mac"
        )

        real_fetch = rp._fetch_registry_rows
        starve = {"on": True}

        def scoped_fetch(get_registry, entries_for, hass_arg, entry_arg, sweep):
            if starve["on"] and sweep == "the residual hub identity re-key":
                return None, []
            return real_fetch(get_registry, entries_for, hass_arg, entry_arg, sweep)

        monkeypatch.setattr(rp, "_fetch_registry_rows", scoped_fetch)

        client = _make_client([_poll_record()])
        result, _built = await _setup_with_patched_forward(hass, entry, client, device_registry, monkeypatch)
        assert result is True

        competing = device_registry.async_get_device(identifiers={(DOMAIN, f"hub_{HID}_{MID}")})
        assert competing is not None, "the platform forward must have written the migrated identifier"
        assert competing.id != old.id

        starve["on"] = False
        caplog.clear()
        coordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]
        with caplog.at_level("DEBUG"):
            await coordinator.async_refresh()

        abandoned = device_registry.async_get(old.id)
        assert abandoned.identifiers == {(DOMAIN, f"hub_{HID}")}
        assert abandoned.name_by_user == "Kitchen Hub"
        assert device_registry.async_get(child.id).via_device_id == old.id
        assert entity_registry.async_get(row.entity_id).unique_id == f"{DOMAIN}_hub_{HID}_mac"

        # The level is the whole difference between a permanent state a user can
        # see and one that is invisible on a default install, so it is asserted
        # on the record rather than by grepping caplog.text.
        warnings = [
            record
            for record in caplog.records
            if record.levelno >= 30 and "Could not re-key hub device row" in record.getMessage()
        ]
        assert warnings, "the abandoned row must be named at warning level"

        # This test drives async_setup_entry, which starts the push channel, but
        # never unloads the entry, so the disconnect registered on unload never
        # runs. Home Assistant's test cleanup fails a test that leaves the
        # supervisor task running.
        mqtt_client = hass.data[DOMAIN][entry.entry_id].get("mqtt_client")
        if mqtt_client is not None:
            await mqtt_client.async_disconnect()

        assert rp._complete_hub_identity_rekey(hass, entry, coordinator) == frozenset({str(HID)})

    @pytest.mark.asyncio
    async def test_the_sweep_reports_a_collided_hub_residual_and_stays_armed(self, hass, device_registry, caplog):
        """The residual set is a re-read of row shape, not a tally of intent.

        An implementation that built the set from the hids it decided to move
        returns an empty frozenset here, which would latch the wrapper shut over
        a hub that is still old-shape and still needs a pass.
        """
        import custom_components.rainpoint as rp

        entry = _make_entry(hass)
        old = device_registry.async_get_or_create(
            config_entry_id=entry.entry_id, identifiers={(DOMAIN, f"hub_{HID}")}, name="Hub"
        )
        competing = device_registry.async_get_or_create(
            config_entry_id=entry.entry_id, identifiers={(DOMAIN, f"hub_{HID}_{MID}")}, name="Competing"
        )
        coordinator = await _real_coordinator(hass, entry, _make_client([_poll_record()]))

        with caplog.at_level("DEBUG"):
            residual = rp._complete_hub_identity_rekey(hass, entry, coordinator)

        assert residual == frozenset({str(HID)})
        assert device_registry.async_get(old.id).identifiers == {(DOMAIN, f"hub_{HID}")}
        assert device_registry.async_get(competing.id).id == competing.id
        assert [r for r in caplog.records if r.levelno >= 30 and "Could not re-key hub device row" in r.getMessage()]

        rp._complete_hub_identity_rekey_on_updates(hass, entry, coordinator)
        assert coordinator._listeners, "a hub left old-shape must keep the listener armed"

    @pytest.mark.asyncio
    async def test_the_migration_continues_past_a_collided_hub(self, hass, entity_registry, device_registry, caplog):
        """A caught device move must not cost the rest of the loop.

        The second hub migrating on both halves is what proves the loop
        continued rather than stopping at the caught row.
        """
        entry = _make_entry(hass)
        hid2 = 101
        old = device_registry.async_get_or_create(
            config_entry_id=entry.entry_id, identifiers={(DOMAIN, f"hub_{HID}")}, name="Hub A"
        )
        device_registry.async_get_or_create(
            config_entry_id=entry.entry_id, identifiers={(DOMAIN, f"hub_{HID}_{MID}")}, name="Competing"
        )
        second = device_registry.async_get_or_create(
            config_entry_id=entry.entry_id, identifiers={(DOMAIN, f"hub_{hid2}")}, name="Hub B"
        )
        colliding_row = entity_registry.async_get_or_create(
            "sensor", DOMAIN, f"{DOMAIN}_hub_{HID}_mac", config_entry=entry, suggested_object_id="hub_a_mac"
        )
        second_row = entity_registry.async_get_or_create(
            "sensor", DOMAIN, f"{DOMAIN}_hub_{hid2}_mac", config_entry=entry, suggested_object_id="hub_b_mac"
        )
        for hid, mid in ((HID, MID), (hid2, 301)):
            entity_registry.async_get_or_create(
                "binary_sensor",
                DOMAIN,
                f"{DOMAIN}_hub_{hid}_{mid}_connectivity",
                config_entry=entry,
                suggested_object_id=f"conn_{hid}",
            )

        with caplog.at_level("WARNING"):
            assert await async_migrate_entry(hass, entry) is True
        assert entry.version == 2

        assert device_registry.async_get(old.id).identifiers == {(DOMAIN, f"hub_{HID}")}
        # Hub A's entity row does move, and by the competing row's own entity
        # pass rather than by the caught one's: that row is already new-shape,
        # so it is in the working set with nothing to do on the device half.
        # The caught hub's own pass touched neither half, which is what the
        # injected-failure case above isolates.
        assert entity_registry.async_get(colliding_row.entity_id).unique_id == f"{DOMAIN}_hub_{HID}_{MID}_mac"
        assert device_registry.async_get(second.id).identifiers == {(DOMAIN, f"hub_{hid2}_301")}
        assert entity_registry.async_get(second_row.entity_id).unique_id == f"{DOMAIN}_hub_{hid2}_301_mac"
        assert [r for r in caplog.records if r.levelno >= 30 and "Could not re-key hub device row" in r.getMessage()]


class TestSelectionScope:
    """What the migration must never touch."""

    @pytest.mark.asyncio
    async def test_a_sub_device_device_row_is_never_passed_to_the_device_registry(
        self, hass, entity_registry, device_registry, monkeypatch
    ):
        """A sub-device identifier carries no hub prefix, so it is not a candidate."""
        entry = _make_entry(hass)
        _hub, child, _rows = _seed_old_shape_install(entry, entity_registry, device_registry)

        moved = []
        real_update = device_registry.async_update_device
        monkeypatch.setattr(
            device_registry,
            "async_update_device",
            lambda device_id, **kwargs: (moved.append(device_id), real_update(device_id, **kwargs))[1],
        )

        assert await async_migrate_entry(hass, entry) is True
        assert child.id not in moved
        assert device_registry.async_get(child.id).identifiers == {(DOMAIN, f"{HID}_{MID}_{ADDR}")}

    @pytest.mark.asyncio
    async def test_the_losing_row_of_a_collision_keeps_its_whole_identity(self, hass, entity_registry, device_registry):
        """Skipped, never deleted: the recorder history behind it is the point."""
        entry = _make_entry(hass)
        _hub, _child, rows = _seed_old_shape_install(entry, entity_registry, device_registry)
        entity_registry.async_get_or_create(
            "sensor", DOMAIN, f"{DOMAIN}_hub_{HID}_{MID}_mac", config_entry=entry, suggested_object_id="squatter"
        )
        before = entity_registry.async_get(rows["mac"].entity_id)

        assert await async_migrate_entry(hass, entry) is True

        after = entity_registry.async_get(before.entity_id)
        assert after is not None
        assert after.unique_id == before.unique_id
        assert after.entity_id == before.entity_id
        assert after.name == before.name


class TestMidResolutionOrdering:
    """Each source alone, and the tie-break's independence from creation order."""

    @pytest.mark.asyncio
    async def test_source_one_alone_resolves_the_mid(self, hass, device_registry):
        """A parented sub-device and no connectivity entity at all."""
        entry = _make_entry(hass)
        hub = device_registry.async_get_or_create(
            config_entry_id=entry.entry_id, identifiers={(DOMAIN, f"hub_{HID}")}, name="Hub"
        )
        device_registry.async_get_or_create(
            config_entry_id=entry.entry_id,
            identifiers={(DOMAIN, f"{HID}_{MID}_{ADDR}")},
            via_device=(DOMAIN, f"hub_{HID}"),
            name="Child",
        )

        assert await async_migrate_entry(hass, entry) is True
        assert device_registry.async_get(hub.id).identifiers == {(DOMAIN, f"hub_{HID}_{MID}")}

    @pytest.mark.asyncio
    async def test_source_two_alone_resolves_the_mid(self, hass, entity_registry, device_registry):
        """A connectivity entity and no parented sub-device at all.

        This is the population that has run 1.12.0, where the connectivity row
        has shipped and is the only thing on disk carrying the mid.
        """
        entry = _make_entry(hass)
        hub = device_registry.async_get_or_create(
            config_entry_id=entry.entry_id, identifiers={(DOMAIN, f"hub_{HID}")}, name="Hub"
        )
        entity_registry.async_get_or_create(
            "binary_sensor",
            DOMAIN,
            f"{DOMAIN}_hub_{HID}_{MID}_connectivity",
            config_entry=entry,
            suggested_object_id="conn",
        )

        assert await async_migrate_entry(hass, entry) is True
        assert device_registry.async_get(hub.id).identifiers == {(DOMAIN, f"hub_{HID}_{MID}")}

    @pytest.mark.asyncio
    @pytest.mark.parametrize("creation_order", [(9, 10), (10, 9)])
    async def test_the_tie_break_ignores_registry_iteration_order(self, hass, entity_registry, device_registry, creation_order):
        """Same outcome whichever row was created first.

        Without a tie-break the surviving device row, with its area, its
        user-set name and every sub-device parented to it, would be assigned to
        one of the two hubs by luck of iteration.
        """
        entry = _make_entry(hass)
        hub = device_registry.async_get_or_create(
            config_entry_id=entry.entry_id, identifiers={(DOMAIN, f"hub_{HID}")}, name="Hub"
        )
        for mid in creation_order:
            entity_registry.async_get_or_create(
                "binary_sensor",
                DOMAIN,
                f"{DOMAIN}_hub_{HID}_{mid}_connectivity",
                config_entry=entry,
                suggested_object_id=f"conn_{mid}",
            )

        assert await async_migrate_entry(hass, entry) is True
        assert device_registry.async_get(hub.id).identifiers == {(DOMAIN, f"hub_{HID}_9")}
