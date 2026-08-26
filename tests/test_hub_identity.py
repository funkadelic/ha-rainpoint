"""Hub identity as a contract: two hubs in one home, and the shape that keeps them apart.

The defect this phase fixes is that a second real hub in one home produced
duplicate unique ids, which Home Assistant resolves by dropping the loser's
entities. Nothing in the migration's own tests exercises that, because the
migration is about installs that already exist. This module supplies the other
half: two real hub records polled together, built by the real platforms, and
accepted by the real registries.

Every registry assertion here depends on `entity_registry` and `device_registry`
resolving to the real Home Assistant classes. The repository conftest installs
package-wide MagicMock stubs and only skips these two because the pytest plugin
imported them first. A mock registry accepts every call and hands back a mock,
so without the guard below the whole two-hub proof would pass against nothing.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.rainpoint import _HUB_MIGRATABLE_SUFFIXES, _domain_sensor_key
from custom_components.rainpoint.const import DOMAIN, HUB_IDENTIFIER_PREFIX, HUB_UNIQUE_ID_PREFIX
from custom_components.rainpoint.device import RainPointHubDevice
from tests.helpers import VALVE_ZONES_TLV_PAYLOAD

assert not isinstance(er.async_get, MagicMock), "entity_registry is stubbed; every proof here would be a no-op"
assert not isinstance(dr.async_get, MagicMock), "device_registry is stubbed; every proof here would be a no-op"

HID = 100
MID_A = 200
MID_B = 201
MID_WRAPPER = 202

# Distinct, easily distinguishable and non-substring, so an id that happens to
# contain one segment cannot be mistaken for one carrying both.
CONVERGENCE_HID = 4242
CONVERGENCE_MID = 777

HUB_PLATFORMS = ("sensor", "select", "switch", "binary_sensor", "update")

# What each hub-owning platform contributes per real hub. Stated per platform
# rather than only as a total, so a platform that kept a singular id cannot hide
# inside a union that is large enough overall.
PER_HUB_COUNTS = {"sensor": 4, "select": 1, "switch": 1, "binary_sensor": 1, "update": 1}


def _hub_record(mid, *, real=True, sub_devices=()):
    """One top-level record as getDeviceByHid returns it.

    real=False is the Bluetooth wrapper shape: every identity field is an empty
    string rather than a missing key, which is why is_hub_record tests
    truthiness rather than presence.
    """
    identity = (
        {"did": f"did-{mid}", "mac": f"AA:BB:{mid}", "productKey": "pk1", "model": "HWG0358WRF", "deviceName": "d"}
        if real
        else {"did": "", "mac": "", "productKey": "", "model": "", "deviceName": ""}
    )
    return {
        "mid": mid,
        "name": f"Hub {mid}" if real else "",
        "homeName": "Home",
        "softVer": "1.2.3",
        "subDevices": list(sub_devices),
        **identity,
    }


def _make_entry(hass):
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"area_code": "1", "email": "a@b.c", "password": "pw", "hids": [HID], "token": "tok"},
        options={},
        version=2,
    )
    entry.add_to_hass(hass)
    return entry


async def _two_hub_coordinator(hass, entry):
    """Construct and first-refresh a real coordinator over a two-hub home.

    One home id, two real hub records at different mids, and a third top-level
    record that is not a hub at all, so the is_hub_record filter is exercised by
    the same fixture that counts the entities. Driven through the real construct
    then first-refresh sequence rather than by assigning coordinator.data: entity
    creation is one-shot off that single snapshot, so an injected end state would
    prove the ids are right without proving a live platform ever builds them.
    """
    from custom_components.rainpoint.coordinator import RainPointCoordinator

    client = MagicMock()
    client.list_homes = AsyncMock(return_value=[{"hid": HID, "name": "Home"}])
    client.get_devices_by_hid = AsyncMock(
        return_value=[
            _hub_record(MID_A, sub_devices=[{"addr": 1, "name": "Valve", "model": "HTV245FRF", "softVer": "127"}]),
            _hub_record(MID_B),
            _hub_record(MID_WRAPPER, real=False),
        ]
    )
    client.get_multiple_device_status = AsyncMock(
        return_value=[{"mid": MID_A, "subDeviceStatus": [{"id": "D01", "value": VALVE_ZONES_TLV_PAYLOAD, "time": 1785420002247}]}]
    )
    client.get_device_status = AsyncMock(return_value={})

    coordinator = RainPointCoordinator(hass, client, entry)
    entry_store = hass.data.setdefault(DOMAIN, {}).setdefault(entry.entry_id, {})
    entry_store["coordinator"] = coordinator
    # The update platform reads the client straight from the entry store rather
    # than through the coordinator, so a harness that stored only the coordinator
    # would build zero update entities and read as a platform that adds nothing.
    entry_store["client"] = client
    await coordinator.async_config_entry_first_refresh()
    return coordinator


async def _build_hub_entities(hass, entry, *, with_push=False):
    """Run each hub-owning platform's own async_setup_entry and capture its entities.

    Returns {platform: [entities]} holding only the hub-level entities, since
    the sensor platform also builds sub-device entities from the same call.

    with_push adds the MQTT client the two push diagnostics read their state
    from. They are off by default here so the per-platform counts stay about the
    per-hub loops, and on for the suffix-set invariant, which needs every hub
    entity the integration can build.
    """
    import importlib

    await _two_hub_coordinator(hass, entry)
    if with_push:
        mqtt_client = MagicMock()
        mqtt_client.hub_mid = MID_A
        hass.data[DOMAIN][entry.entry_id]["mqtt_client"] = mqtt_client

    built = {}
    for platform in HUB_PLATFORMS:
        module = importlib.import_module(f"custom_components.rainpoint.{platform}")
        captured = []

        def add(entities, update_before_add=False, _c=captured):
            _c.extend(entities)

        await module.async_setup_entry(hass, entry, add)
        built[platform] = [entity for entity in captured if isinstance(entity, RainPointHubDevice)]
    return built


def _hub_suffix(unique_id, hid, mid):
    """Strip the hub prefix and both identity segments, leaving the suffix."""
    prefix = f"{HUB_UNIQUE_ID_PREFIX}{hid}_{mid}_"
    assert unique_id.startswith(prefix), f"{unique_id} is not keyed on {hid} and {mid}"
    return unique_id[len(prefix) :]


class TestTwoHubsInOneHome:
    """The defect itself: two real hubs in one home, kept apart end to end."""

    @pytest.mark.asyncio
    async def test_each_platform_builds_one_set_per_hub(self, hass):
        """Asserted per platform, not only on the union.

        A platform that kept a singular id would still leave the union large
        enough if another platform happened to build extra entities, so the
        count that matters is the per-platform one.
        """
        entry = _make_entry(hass)
        built = await _build_hub_entities(hass, entry)

        for platform, per_hub in PER_HUB_COUNTS.items():
            assert len(built[platform]) == per_hub * 2, f"{platform} did not build one set per hub"

    @pytest.mark.asyncio
    async def test_no_hub_entity_id_is_shared_between_the_two_hubs(self, hass):
        """The union carries no duplicate, which is what the old shape could not manage."""
        entry = _make_entry(hass)
        built = await _build_hub_entities(hass, entry)

        unique_ids = [entity._attr_unique_id for entities in built.values() for entity in entities]

        assert len(unique_ids) == len(set(unique_ids))
        assert len(unique_ids) == sum(PER_HUB_COUNTS.values()) * 2

    @pytest.mark.asyncio
    async def test_the_wrapper_record_contributes_nothing(self, hass):
        """A top-level record that is not a hub yields no hub entity anywhere.

        Without this the two-hub count above could be satisfied by the wrong
        pair of records.
        """
        entry = _make_entry(hass)
        built = await _build_hub_entities(hass, entry)

        mids = {entity._hub_info["mid"] for entities in built.values() for entity in entities}
        assert mids == {MID_A, MID_B}

    @pytest.mark.asyncio
    async def test_the_push_diagnostics_cover_both_hubs(self, hass):
        """One push entity of each kind per hub, and the ids stay distinct.

        A property of how the push hub is resolved rather than of the id shape.
        These used to be single-instance because push reached one hub; both
        hubs are covered now, so this is the case where the re-key has to hold:
        two hubs each carrying a pair means four ids that must not collide.
        """
        import importlib

        from custom_components.rainpoint.hub_entities import (
            RainPointPushConnectedBinarySensor,
            RainPointPushLastMessageSensor,
        )

        entry = _make_entry(hass)
        await _two_hub_coordinator(hass, entry)
        mqtt_client = MagicMock()
        mqtt_client.hub_mid = MID_A
        hass.data[DOMAIN][entry.entry_id]["mqtt_client"] = mqtt_client

        captured = []
        for platform in ("sensor", "binary_sensor"):
            module = importlib.import_module(f"custom_components.rainpoint.{platform}")
            await module.async_setup_entry(hass, entry, lambda entities, **kw: captured.extend(entities))

        connected = [e for e in captured if isinstance(e, RainPointPushConnectedBinarySensor)]
        last_message = [e for e in captured if isinstance(e, RainPointPushLastMessageSensor)]
        assert sorted(e._hub_info["mid"] for e in connected) == sorted([MID_A, MID_B])
        assert sorted(e._hub_info["mid"] for e in last_message) == sorted([MID_A, MID_B])
        ids = [e._attr_unique_id for e in connected + last_message]
        assert len(set(ids)) == len(ids)

    @pytest.mark.asyncio
    async def test_both_entity_sets_survive_real_registration(self, hass, entity_registry):
        """The actual defect, asserted on what the registry accepted.

        On the retired shape the second hub's rows collide with the first's and
        Home Assistant drops them, so asserting on the built objects alone would
        miss the failure entirely.
        """
        entry = _make_entry(hass)
        built = await _build_hub_entities(hass, entry)

        registered = []
        for platform, entities in built.items():
            for entity in entities:
                row = entity_registry.async_get_or_create(platform, DOMAIN, entity._attr_unique_id, config_entry=entry)
                registered.append((platform, entity._attr_unique_id, row.entity_id))

        assert len({row_id for _p, _u, row_id in registered}) == len(registered)
        for platform, unique_id, row_id in registered:
            assert entity_registry.async_get_entity_id(platform, DOMAIN, unique_id) == row_id

    @pytest.mark.asyncio
    async def test_two_hubs_yield_two_device_pages(self, hass, device_registry):
        """Two rows, two identifiers, two device ids."""
        entry = _make_entry(hass)
        built = await _build_hub_entities(hass, entry)

        rows = {}
        for entity in built["sensor"]:
            row = device_registry.async_get_or_create(config_entry_id=entry.entry_id, **entity.device_info)
            rows[row.id] = row

        assert len(rows) == 2
        assert {_domain_sensor_key(row) for row in rows.values()} == {
            f"hub_{HID}_{MID_A}",
            f"hub_{HID}_{MID_B}",
        }

    @pytest.mark.asyncio
    async def test_each_hub_keeps_its_own_connectivity_id(self, hass):
        """The pairing that already shipped, and the reason row selection is a closed set.

        Two connectivity rows in one home share the hub unique-id prefix and do
        not collide with each other, so a selection rule keyed on the prefix
        alone would rewrite the sibling's id and orphan its history.
        """
        entry = _make_entry(hass)
        built = await _build_hub_entities(hass, entry)

        ids = sorted(entity._attr_unique_id for entity in built["binary_sensor"])
        assert ids == [
            f"{DOMAIN}_hub_{HID}_{MID_A}_connectivity",
            f"{DOMAIN}_hub_{HID}_{MID_B}_connectivity",
        ]


class TestHubIdentityConvergence:
    """The promotion, made enforceable: one spelling of hub identity, everywhere."""

    @pytest.mark.asyncio
    async def test_every_hub_entity_carries_both_segments_in_order(self, hass):
        """Derived from what the platforms build, so a future hub entity is covered
        without anyone remembering to extend a list."""
        entry = _make_entry(hass)
        built = await _build_hub_entities(hass, entry)

        for platform, entities in built.items():
            for entity in entities:
                mid = entity._hub_info["mid"]
                assert f"{HID}_{mid}" in entity._attr_unique_id, (
                    f"{platform}:{type(entity).__name__} is not keyed on the home id and the hub mid"
                )

    @pytest.mark.asyncio
    async def test_no_hub_entity_is_keyed_on_the_home_id_alone(self, hass):
        """The explicit negative: the retired shape, spelled out.

        This is the assertion that goes red the instant a future change
        reintroduces the singular assumption, which is otherwise invisible until
        someone owns a second hub.
        """
        import re

        entry = _make_entry(hass)
        built = await _build_hub_entities(hass, entry)

        retired = re.compile(rf"^{DOMAIN}_hub_{HID}_[a-z]")
        for platform, entities in built.items():
            for entity in entities:
                assert not retired.match(entity._attr_unique_id), (
                    f"{platform}:{type(entity).__name__} reintroduced the home-id-only hub key"
                )

    def test_the_three_derived_ids_ride_on_the_base(self):
        """Changing the base changes all three, which is why they were not edited.

        Proven against the same hub record rather than assumed, so the decision
        to leave those three lines alone is a property rather than a claim.
        """
        from custom_components.rainpoint.const import (
            PUSH_CONNECTED_UNIQUE_ID_SUFFIX,
            PUSH_LAST_MESSAGE_UNIQUE_ID_SUFFIX,
        )
        from custom_components.rainpoint.hub_entities import (
            RainPointHubRSSISensor,
            RainPointPushConnectedBinarySensor,
            RainPointPushLastMessageSensor,
        )

        hub_info = {"hid": CONVERGENCE_HID, "mid": CONVERGENCE_MID, "name": "Hub", "model": "HWG0358WRF"}
        base = RainPointHubDevice(dict(hub_info))._attr_unique_id
        assert base == f"{DOMAIN}_hub_{CONVERGENCE_HID}_{CONVERGENCE_MID}"

        coordinator = MagicMock()
        coordinator.data = {"hubs": [], "sensors": {}, "status": {}}
        mqtt_client = MagicMock()

        derived = {
            "rssi": RainPointHubRSSISensor(coordinator, dict(hub_info))._attr_unique_id,
            PUSH_CONNECTED_UNIQUE_ID_SUFFIX: RainPointPushConnectedBinarySensor(mqtt_client, dict(hub_info))._attr_unique_id,
            PUSH_LAST_MESSAGE_UNIQUE_ID_SUFFIX: RainPointPushLastMessageSensor(mqtt_client, dict(hub_info))._attr_unique_id,
        }
        for suffix, unique_id in derived.items():
            assert unique_id == f"{base}_{suffix}"

    def test_the_connectivity_id_did_not_move(self):
        """It already carried both segments, so any change here means an
        unnecessary migration was introduced."""
        from custom_components.rainpoint.hub_entities import RainPointHubConnectivityBinarySensor

        coordinator = MagicMock()
        coordinator.data = {"hubs": [], "sensors": {}, "status": {}}
        entity = RainPointHubConnectivityBinarySensor(coordinator, {"hid": 100, "mid": 200, "name": "Hub"})

        assert entity._attr_unique_id == "rainpoint_hub_100_200_connectivity"


class TestMigratableSuffixSet:
    """The one place the suffix list is spelled twice, pinned to what is built."""

    @pytest.mark.asyncio
    async def test_the_closed_set_equals_what_the_platforms_build(self, hass):
        """Equality in both directions, derived rather than restated.

        A member the platforms build and the set omits is an old-shape row the
        migration leaves behind forever, since the version boundary only fires
        once. A member in the set that nothing builds is dead weight that hides
        the first kind of drift. Restating the members here would make this a
        third spelling and prove nothing, so the set is imported.
        """
        entry = _make_entry(hass)
        built = await _build_hub_entities(hass, entry, with_push=True)

        observed = {
            _hub_suffix(entity._attr_unique_id, HID, entity._hub_info["mid"])
            for entities in built.values()
            for entity in entities
        }

        assert "connectivity" in observed, "the connectivity entity must still be built"
        assert observed - {"connectivity"} == _HUB_MIGRATABLE_SUFFIXES, (
            "connectivity is deliberately excluded: it already carries both segments, so it is the "
            "one hub entity that must never be migrated"
        )


class TestHubIdentifierPrefixSingleSource:
    """The hub identifier prefix, pinned the same way the suffix set is above.

    Unlike _HUB_MIGRATABLE_SUFFIXES, which is imported and compared directly,
    the prefix used to be an independent literal on each side: device.py wrote
    "hub_" / "rainpoint_hub_" verbatim, and __init__.py's migration matcher
    spelled its own "hub_" / f"{DOMAIN}_hub_" copies. Both writers now build
    from const.HUB_IDENTIFIER_PREFIX and const.HUB_UNIQUE_ID_PREFIX, and the
    migration's private aliases are assigned from the same constants rather
    than restated, so this test pins both that the values never change (a
    breaking migration if they did, since they are persisted in the entity and
    device registries) and that every writer and the matcher still agree.
    """

    def test_the_constants_are_the_literal_values_already_persisted(self):
        """The values themselves are frozen, not just internally consistent."""
        assert HUB_IDENTIFIER_PREFIX == "hub_"
        assert HUB_UNIQUE_ID_PREFIX == f"{DOMAIN}_hub_" == "rainpoint_hub_"

    def test_device_py_builds_its_identifiers_from_the_shared_constants(self):
        """RainPointHubDevice's device identifier and unique id both start here."""
        hub_info = {"hid": HID, "mid": MID_A, "name": "Hub"}
        device = RainPointHubDevice(hub_info)

        assert device._attr_unique_id.startswith(HUB_UNIQUE_ID_PREFIX)
        (identifier,) = device.device_info["identifiers"]
        assert identifier[1].startswith(HUB_IDENTIFIER_PREFIX)

    @pytest.mark.asyncio
    async def test_hub_entities_py_builds_every_inline_unique_id_from_the_same_prefix(self, hass):
        """The five inline hub_entities.py sites, plus RSSI's appended suffix.

        Reuses the real per-hub build so this fails the moment any hub-owning
        platform reintroduces an independently spelled prefix, rather than
        only when a hand-picked subset of classes is checked.
        """
        entry = _make_entry(hass)
        built = await _build_hub_entities(hass, entry, with_push=True)

        for entities in built.values():
            for entity in entities:
                assert entity._attr_unique_id.startswith(HUB_UNIQUE_ID_PREFIX), (
                    f"{entity._attr_unique_id} does not start with the shared hub unique-id prefix"
                )

    def test_the_migrations_private_aliases_equal_the_shared_constants(self):
        """__init__.py's matcher builds its aliases from const.py, not a second literal."""
        import custom_components.rainpoint as rp

        assert rp._HUB_IDENTIFIER_PREFIX == HUB_IDENTIFIER_PREFIX
        assert rp._HUB_UNIQUE_ID_PREFIX == HUB_UNIQUE_ID_PREFIX


class TestHubIdentityIsNotASensorKey:
    """Why the parenting sweep still needs no hub-row guard."""

    @pytest.mark.asyncio
    async def test_the_hub_identifier_cannot_collide_with_a_sensor_key(self, hass):
        """Structural, and it survives the re-key.

        The parenting sweep is scoped to rows whose identifier is a key in the
        current poll's sensors mapping. A hub identifier carries the literal hub
        prefix as its first segment and no address segment, and a numeric home
        id can never equal that prefix, so a hub row simply never matches. Adding
        an explicit guard would encode hub identifier shape in a second place,
        which is the thing that was deliberately avoided.
        """
        entry = _make_entry(hass)
        coordinator = await _two_hub_coordinator(hass, entry)
        hub_info = next(hub for hub in coordinator.data["hubs"] if hub["mid"] == MID_A)
        identifier = f"hub_{hub_info['hid']}_{hub_info['mid']}"

        assert coordinator.data["sensors"], "the fixture must poll at least one sub-device"
        assert identifier not in coordinator.data["sensors"]
        assert identifier.split("_")[0] == "hub"

    @pytest.mark.asyncio
    async def test_the_parenting_sweep_leaves_a_rekeyed_hub_row_alone(self, hass, device_registry):
        """Driven through the real sweep against a real registry holding both rows."""
        from custom_components.rainpoint import _reconcile_sub_device_parents

        entry = _make_entry(hass)
        coordinator = await _two_hub_coordinator(hass, entry)
        sensor_key = next(iter(coordinator.data["sensors"]))

        hub_row = device_registry.async_get_or_create(
            config_entry_id=entry.entry_id,
            identifiers={(DOMAIN, f"hub_{HID}_{MID_A}")},
            name="Hub",
        )
        child = device_registry.async_get_or_create(
            config_entry_id=entry.entry_id,
            identifiers={(DOMAIN, sensor_key)},
            via_device=(DOMAIN, f"hub_{HID}_{MID_A}"),
            name="Child",
        )
        assert device_registry.async_get(child.id).via_device_id == hub_row.id

        _reconcile_sub_device_parents(hass, entry, coordinator)

        after = device_registry.async_get(hub_row.id)
        assert after.identifiers == {(DOMAIN, f"hub_{HID}_{MID_A}")}
        assert after.via_device_id is None
        assert device_registry.async_get(child.id).via_device_id == hub_row.id


class TestPushReachesTheHubItNamesThroughARealCoordinator:
    """The joint _frame_mid's int() crosses into the coordinator's mid comparison.

    Both sides are otherwise tested against a mock of the other: every test in
    tests/api/test_mqtt.py asserts against a MagicMock coordinator, and every
    push test in tests/test_coordinator.py hands apply_*_push_update a mid the
    test author picked. Before this branch the mid came from hub.get("mid")
    itself, so the two sides were type-identical by construction and could not
    disagree. Reading it out of the payload made that agreement load-bearing
    across a parse, and nothing asserted it. The cloud is already known to
    return mid as a string on one endpoint and an int on its sibling.
    """

    @staticmethod
    def _push_client(hass, coordinator):
        from custom_components.rainpoint.api.mqtt import RainPointMqttClient

        rainpoint_client = MagicMock()
        rainpoint_client.get_subscribe_status = AsyncMock(return_value={})
        return RainPointMqttClient(
            hass,
            rainpoint_client,
            entry=MagicMock(),
            hub_device_name="hub-device",
            hub_product_key="hub-pk",
            coordinator=coordinator,
            # The session is opened for hub A. Every assertion below is about a
            # frame naming a different hub still landing correctly, which is the
            # whole point of routing on the frame's own mid.
            hub_mid=MID_A,
            paho_client_factory=MagicMock(return_value=MagicMock()),
            time_source=lambda: 1000.0,
        )

    @staticmethod
    def _subdevice_frame(mid, raw_value, ts=1785420009999):
        """A captured-shape sub-device envelope naming a specific hub."""
        import json

        inner = {"D01": {"time": ts, "value": raw_value}}
        param = "|".join(["#P" + "0" * 24 + f"{mid:06d}", json.dumps(inner), str(ts), "abcdef012345#"])
        return json.dumps({"method": "thing.service.property.set", "params": {"param": param}, "version": "1.0.0"}).encode()

    @staticmethod
    def _hub_frame(mid, connected, ts=1785521850011):
        return f"#P{'0' * 24}{mid:06d}|{1 if connected else 0}|{ts}|112882164350#".encode()

    @pytest.mark.asyncio
    async def test_a_captured_sub_device_frame_reaches_the_hub_it_names(self, hass):
        """A real payload, parsed by the real client, applied by the real coordinator.

        Hub A is the only hub with a sub-device, so its reading is the one that
        can move. If _frame_mid's int() and the poll's hub["mid"] ever stop
        agreeing, this drops at "unknown mid" and goes red, where the two
        halves' own tests stay green.
        """
        entry = _make_entry(hass)
        coordinator = await _two_hub_coordinator(hass, entry)
        client = self._push_client(hass, coordinator)

        sensor_key = f"{HID}_{MID_A}_1"
        before = coordinator.data["sensors"][sensor_key]

        client._handle_message("topic", self._subdevice_frame(MID_A, VALVE_ZONES_TLV_PAYLOAD))

        assert coordinator.data["sensors"][sensor_key] is not before, "the frame never reached the hub it named"
        assert client.last_message_at_for(MID_A) == 1000.0

    @pytest.mark.asyncio
    async def test_a_captured_hub_frame_reaches_the_other_hub_and_leaves_the_session_hub_alone(self, hass):
        """The same joint on the connectivity entry point, for the hub the
        session was NOT opened for: that is what "one session serves every hub"
        has to mean, and a mid that failed to cross would silently fall back to
        touching nothing."""
        entry = _make_entry(hass)
        coordinator = await _two_hub_coordinator(hass, entry)
        client = self._push_client(hass, coordinator)

        client._handle_message("topic", self._hub_frame(MID_B, connected=False))

        from custom_components.rainpoint.coordinator import HUB_DISCONNECTED

        connectivity = coordinator.data["hub_connectivity"]
        assert connectivity[MID_B]["state"] == HUB_DISCONNECTED
        assert connectivity.get(MID_A, {}).get("state") != HUB_DISCONNECTED, "the edge landed on the wrong hub"
        assert client.last_message_at_for(MID_B) == 1000.0
        assert client.last_message_at_for(MID_A) is None

    @pytest.mark.asyncio
    async def test_a_frame_naming_an_unknown_mid_stamps_no_clock(self, hass):
        """The per-hub clock map is keyed on a payload field. Stamping before the
        coordinator resolves the mid let any mid a frame named take a permanent
        entry, for a hub no entity reads, in a class that caps its only other
        payload-keyed structure on purpose."""
        entry = _make_entry(hass)
        coordinator = await _two_hub_coordinator(hass, entry)
        client = self._push_client(hass, coordinator)

        unknown_mid = 999999
        client._handle_message("topic", self._hub_frame(unknown_mid, connected=True))

        assert client.last_message_at_for(unknown_mid) is None
        assert client._last_message_at_by_mid == {}
        # The session clock still advances: an unattributable frame is still
        # proof the pipe is alive, which is the distinction between the two.
        assert client.last_message_at == 1000.0

    @pytest.mark.asyncio
    async def test_a_hub_record_carrying_a_string_mid_still_receives_its_frames(self, hass):
        """The cloud is already known to be inconsistent about mid's type across
        endpoints, and this branch is what made the two sides have to agree
        across a parse rather than being the same object. A string mid must not
        silently kill push for that hub, which is what a bare == comparison did:
        the frame would be dropped as an unknown mid, logged at DEBUG, and be
        indistinguishable from a hub that really had left the account."""
        from custom_components.rainpoint.coordinator import RainPointCoordinator

        entry = _make_entry(hass)
        record = _hub_record(MID_A, sub_devices=[{"addr": 1, "name": "Valve", "model": "HTV245FRF", "softVer": "127"}])
        record["mid"] = str(MID_A)

        client = MagicMock()
        client.list_homes = AsyncMock(return_value=[{"hid": HID, "name": "Home"}])
        client.get_devices_by_hid = AsyncMock(return_value=[record])
        client.get_multiple_device_status = AsyncMock(
            return_value=[
                {"mid": MID_A, "subDeviceStatus": [{"id": "D01", "value": VALVE_ZONES_TLV_PAYLOAD, "time": 1785420002247}]}
            ]
        )
        client.get_device_status = AsyncMock(return_value={})

        coordinator = RainPointCoordinator(hass, client, entry)
        hass.data.setdefault(DOMAIN, {}).setdefault(entry.entry_id, {})["coordinator"] = coordinator
        await coordinator.async_config_entry_first_refresh()

        # Normalised at ingestion, so every downstream consumer sees one type.
        assert coordinator.data["hubs"][0]["mid"] == MID_A
        assert isinstance(coordinator.data["hubs"][0]["mid"], int)

        push_client = self._push_client(hass, coordinator)
        sensor_key = f"{HID}_{MID_A}_1"
        before = coordinator.data["sensors"][sensor_key]

        push_client._handle_message("topic", self._subdevice_frame(MID_A, VALVE_ZONES_TLV_PAYLOAD))

        assert coordinator.data["sensors"][sensor_key] is not before
        assert push_client.last_message_at_for(MID_A) == 1000.0
