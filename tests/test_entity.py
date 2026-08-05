"""Tests for the shared sub-device entity plumbing (entity.py)."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from custom_components.rainpoint.entity import (
    LATE_ADDER_STORE_KEY,
    EmittedEntityLedger,
    LateEntityAdder,
    RainPointSubDeviceEntity,
    late_adders,
    register_late_adder,
    sub_device_attributes,
)
from tests.helpers import make_coordinator_data, make_sensor_entry


def _coordinator(entry):
    """Return a coordinator stub whose sensors map holds one entry under "k"."""
    return SimpleNamespace(data={"sensors": {"k": entry}} if entry is not None else {"sensors": {}})


class TestSubDeviceAttributes:
    """Tests for sub_device_attributes."""

    def test_firmware_and_device_timestamp(self):
        """A populated entry yields the firmware and the device timestamp trio."""
        coordinator = _coordinator(
            {
                "firmware_version": "1.4",
                "data": {
                    "device_timestamp": "2026-07-29T12:19:33+00:00",
                    "timestamp_method": "rtc",
                    "timestamp_source": "device",
                },
            }
        )
        assert sub_device_attributes(coordinator, "k") == {
            "firmware_version": "1.4",
            "device_timestamp": "2026-07-29T12:19:33+00:00",
            "timestamp_method": "rtc",
            "timestamp_source": "device",
            "hub_connected": None,
        }

    def test_server_timestamp_fills_in_for_device_timestamp(self):
        """With only a server timestamp, it is reported as the device timestamp."""
        coordinator = _coordinator({"data": {"server_timestamp": "2026-07-29T12:00:00+00:00"}})
        attrs = sub_device_attributes(coordinator, "k")
        assert attrs["device_timestamp"] == "2026-07-29T12:00:00+00:00"
        assert attrs["timestamp_source"] == "server"
        assert "timestamp_method" not in attrs

    def test_none_reading_yields_firmware_alone(self):
        """A sub-device with no reading yet must not raise.

        The per-platform copies in valve.py and number.py fed this None
        straight into a membership test, so a device that had not reported
        raised while its attributes were being built.
        """
        coordinator = _coordinator({"firmware_version": "1.4", "data": None})
        assert sub_device_attributes(coordinator, "k") == {"firmware_version": "1.4", "hub_connected": None}

    def test_missing_entry_yields_nothing(self):
        """A sensor key the coordinator does not know yields the hub_connected
        marker alone: the key is never conditionally omitted, so a template
        can test it without first testing for its existence."""
        assert sub_device_attributes(_coordinator(None), "k") == {"hub_connected": None}

    def test_coordinator_without_data_yields_nothing(self):
        """A coordinator that has not completed its first poll still yields
        the hub_connected marker, and must not raise."""
        assert sub_device_attributes(SimpleNamespace(data=None), "k") == {"hub_connected": None}

    def test_absent_firmware_is_omitted(self):
        """An empty firmware string is treated as absent rather than reported."""
        coordinator = _coordinator({"firmware_version": "", "data": {}})
        assert sub_device_attributes(coordinator, "k") == {"hub_connected": None}

    def test_silent_entry_yields_firmware_alone(self):
        """A silent entry (D-09/D-11) carries neither a device nor a server
        timestamp key, so it must not raise and must yield the firmware
        attribute alone, same as a bare None reading."""
        from custom_components.rainpoint.coordinator import SILENT_DATA_TYPE

        coordinator = _coordinator(
            {
                "firmware_version": "1.4",
                "raw_status": {},
                "data": {"type": SILENT_DATA_TYPE, "silent_state": "never_reported"},
            }
        )
        assert sub_device_attributes(coordinator, "k") == {"firmware_version": "1.4", "hub_connected": None}


class TestHubConnectedAttribute:
    """Tests for the hub_connected marker sub_device_attributes adds."""

    def test_hub_connected_true_when_hub_reports_connected(self):
        """A connected hub's cloud state yields hub_connected True."""
        coordinator = SimpleNamespace(
            data={
                "sensors": {"k": {"mid": 200, "data": {}}},
                "hub_connectivity": {200: {"state": "connected"}},
            }
        )
        assert sub_device_attributes(coordinator, "k")["hub_connected"] is True

    def test_hub_connected_false_when_hub_reports_disconnected(self):
        """A disconnected hub's cloud state yields hub_connected False -- the
        stale-reading marker a template can gate on without the integration
        hiding the reading itself, which stays untouched."""
        coordinator = SimpleNamespace(
            data={
                "sensors": {"k": {"mid": 200, "data": {}}},
                "hub_connectivity": {200: {"state": "disconnected"}},
            }
        )
        assert sub_device_attributes(coordinator, "k")["hub_connected"] is False

    def test_hub_connected_none_when_hub_reports_unknown(self):
        """An unknown tri-state yields hub_connected None, distinct from both
        a definite connected and disconnected answer."""
        coordinator = SimpleNamespace(
            data={
                "sensors": {"k": {"mid": 200, "data": {}}},
                "hub_connectivity": {200: {"state": "unknown"}},
            }
        )
        assert sub_device_attributes(coordinator, "k")["hub_connected"] is None

    def test_hub_connected_none_when_no_hub_connectivity_key_at_all(self):
        """A coordinator snapshot with no hub_connectivity key at all must not
        raise, and answers None rather than a false positive or negative."""
        coordinator = _coordinator({"mid": 200, "data": {}})
        assert sub_device_attributes(coordinator, "k")["hub_connected"] is None

    def test_hub_connected_none_when_sensor_entry_has_no_mid(self):
        """A sensor entry with no mid key yields None rather than raising."""
        coordinator = SimpleNamespace(
            data={
                "sensors": {"k": {"data": {}}},
                "hub_connectivity": {200: {"state": "connected"}},
            }
        )
        assert sub_device_attributes(coordinator, "k")["hub_connected"] is None


class TestHubConnectedCrossPlatform:
    """The hub_connected marker rides through every platform via the one
    shared helper, with no per-platform change of its own."""

    @staticmethod
    def _disconnected_hub_coordinator(entry):
        """Return a coordinator stub whose one hub reports disconnected."""
        return MagicMock(
            data={
                "sensors": {"100_200_1": entry},
                "hub_connectivity": {200: {"state": "disconnected"}},
            }
        )

    def test_valve_entity_surfaces_hub_connected_false(self):
        """RainPointValveEntity inherits hub_connected via sub_device_attributes."""
        from custom_components.rainpoint.valve import RainPointValveEntity

        sensor_info = {
            "hid": 100,
            "mid": 200,
            "addr": 1,
            "sub_name": "Valve Hub 1",
            "model": "HTV245FRF",
            "firmware_version": "1.0",
        }
        entry = {**sensor_info, "data": {"hub_online": True, "zones": {1: {"open": True}}}}
        coordinator = self._disconnected_hub_coordinator(entry)

        valve = RainPointValveEntity.__new__(RainPointValveEntity)
        valve.coordinator = coordinator
        valve._sensor_key = "100_200_1"
        valve._sensor_info = sensor_info
        valve._zone_num = 1

        assert valve.extra_state_attributes["hub_connected"] is False

    def test_sensor_entity_surfaces_hub_connected_false(self):
        """RainPointSensorBase subclasses inherit hub_connected the same way."""
        from custom_components.rainpoint.sensor import RainPointMoisturePercentSensor

        sensor_info = {
            "hid": 100,
            "mid": 200,
            "addr": 1,
            "sub_name": "Test Sensor",
            "model": "HCS026FRF",
            "firmware_version": "1.0.0",
            "raw_status": {},
        }
        entry = {**sensor_info, "data": {"type": "moisture_simple", "moisture_percent": 42}}
        coordinator = self._disconnected_hub_coordinator(entry)

        sensor = RainPointMoisturePercentSensor.__new__(RainPointMoisturePercentSensor)
        sensor.coordinator = coordinator
        sensor._sensor_key = "100_200_1"
        sensor._sensor_info = sensor_info
        sensor._base_slug = "100_200_1"
        sensor._simple = True

        assert sensor.extra_state_attributes["hub_connected"] is False


class TestSubDeviceEntity:
    """Tests for RainPointSubDeviceEntity."""

    @staticmethod
    def _entity(data):
        """Return an entity bound to a coordinator holding ``data``."""
        coordinator = MagicMock()
        coordinator.data = data
        return RainPointSubDeviceEntity(coordinator, "k", {"addr": 1}, "slug")

    def test_sensor_data_without_coordinator_data(self):
        """A coordinator with no data yet reads as no reading, not a crash.

        Matches the guard sub_device_attributes already applies, so both halves
        of this module agree on what an empty coordinator means.
        """
        entity = self._entity(None)
        assert entity._sensor_data is None
        assert entity.available is False

    def test_sensor_data_reads_through_to_the_entry(self):
        """A populated entry yields its decoded reading and reads as available."""
        entity = self._entity({"sensors": {"k": {"data": {"type": "valve"}}}})
        assert entity._sensor_data == {"type": "valve"}
        assert entity.available is True

    def test_silent_entry_reads_as_unavailable(self):
        """A silent entry's data is truthy, but must still read as unavailable:
        a battery/RSSI/generic entity bound to this key must not look wired up
        while reading nothing (D-02/D-12)."""
        from custom_components.rainpoint.coordinator import SILENT_DATA_TYPE

        entity = self._entity({"sensors": {"k": {"data": {"type": SILENT_DATA_TYPE, "silent_state": "never_reported"}}}})
        assert entity.available is False

    def test_available_is_unchanged_by_a_disconnected_hub(self):
        """Regression pin: RainPointSubDeviceEntity.available must NOT be
        touched by hub cloud connectivity.

        A future reader tempted to "finish the job" by propagating
        hub_connected into availability
        would silently reverse a decision made on hardware evidence: the
        data self-heals within seconds of the hub reattaching, so flipping
        every reading entity to unavailable during an outage would cost
        every existing user history gaps and template errors for a
        self-healing condition. The read path deliberately keeps its last
        value throughout a hub outage; only the mid key on the sensor entry,
        not the hub_connectivity record, is what this property ever reads.
        """
        entity = self._entity(
            {
                "sensors": {"k": {"mid": 200, "data": {"type": "valve"}}},
                "hub_connectivity": {200: {"state": "disconnected"}},
            }
        )
        assert entity.available is True


class _FakeEntity:
    """Minimal stand-in carrying only the attribute the adder dedupes on."""

    def __init__(self, unique_id):
        """Record the unique id the adder will read."""
        self._attr_unique_id = unique_id


class TestLateEntityAdder:
    """Add-once bookkeeping and the coordinator listener behind late creation."""

    @staticmethod
    def _adder(sensors, build):
        """Wire an adder over a coordinator stub, returning it with the sink."""
        coordinator = SimpleNamespace(data={"sensors": sensors})
        added = []
        adder = LateEntityAdder(coordinator, added.extend, build, "valve")
        return coordinator, adder, added

    def test_collect_returns_entities_once_and_never_again(self):
        """A repeated unique_id is an error in Home Assistant, so it cannot recur."""
        _c, adder, _added = self._adder({}, lambda k, i: [_FakeEntity("a"), _FakeEntity("b")])

        first = adder.collect("k", {})
        second = adder.collect("k", {})

        assert [e._attr_unique_id for e in first] == ["a", "b"]
        assert second == []

    def test_a_key_gaining_a_second_zone_gets_only_the_new_entity(self):
        """The per-zone case the sensor platform's key-based bookkeeping cannot express."""
        zones = ["z1"]
        _c, adder, _added = self._adder({}, lambda k, i: [_FakeEntity(z) for z in zones])

        adder.collect("k", {})
        zones.append("z2")
        second = adder.collect("k", {})

        assert [e._attr_unique_id for e in second] == ["z2"]

    def test_an_entity_without_a_unique_id_is_never_deduped(self):
        """Nothing to key on means nothing to suppress; letting it through is safer."""
        _c, adder, _added = self._adder({}, lambda k, i: [_FakeEntity(None)])

        assert len(adder.collect("k", {})) == 1
        assert len(adder.collect("k", {})) == 1

    def test_listener_adds_a_key_that_became_eligible_after_setup(self):
        """The whole point: entity creation is otherwise frozen at first refresh."""
        sensors = {}
        _coordinator, adder, added = self._adder(sensors, lambda k, i: [_FakeEntity(k)])

        adder.async_on_coordinator_update()
        assert added == []

        sensors["late"] = {}
        adder.async_on_coordinator_update()

        assert [e._attr_unique_id for e in added] == ["late"]

    def test_listener_skips_a_malformed_record_and_keeps_the_rest(self):
        """One bad record must not break late creation for every other device."""
        sensors = {"bad": "not a dict", "good": {}}
        _c, adder, added = self._adder(sensors, lambda k, i: [_FakeEntity(k)])

        adder.async_on_coordinator_update()

        assert [e._attr_unique_id for e in added] == ["good"]

    def test_listener_tolerates_a_coordinator_with_no_data(self):
        """A refresh that failed leaves data None; the listener still runs."""
        coordinator = SimpleNamespace(data=None)
        added = []
        adder = LateEntityAdder(coordinator, added.extend, lambda k, i: [_FakeEntity(k)], "valve")

        adder.async_on_coordinator_update()

        assert added == []


class TestEmittedEntityLedger:
    """What an adder emitted, indexed by the key that produced it."""

    def test_a_key_gaining_entities_across_polls_ends_with_all_of_them(self):
        """The append rule: a per-zone platform emits for one key over several
        polls, so replacing rather than appending would leave the first zone's
        row unreachable and therefore unremovable."""
        zones = ["z1"]
        coordinator = SimpleNamespace(data={"sensors": {}})
        adder = LateEntityAdder(coordinator, lambda ents: None, lambda k, i: [_FakeEntity(z) for z in zones], "valve")

        adder.collect("k", {})
        zones.append("z2")
        adder.collect("k", {})

        assert adder.ledger.unique_ids_for("k") == frozenset({"z1", "z2"})

    def test_only_what_was_handed_to_home_assistant_is_recorded(self):
        """An entity suppressed as already emitted was recorded on the poll
        that did emit it, so recording the builder's full output again would
        be indistinguishable until the builder stopped being deterministic."""
        coordinator = SimpleNamespace(data={"sensors": {}})
        adder = LateEntityAdder(coordinator, lambda ents: None, lambda k, i: [_FakeEntity("z1")], "valve")

        adder.collect("k", {})
        adder.collect("k", {})

        assert adder.ledger.unique_ids_for("k") == frozenset({"z1"})

    def test_an_entity_with_no_unique_id_leaves_the_key_invisible(self):
        """No unique_id means no registry row, so the key is out of scope for
        removal entirely rather than recorded with nothing to remove."""
        coordinator = SimpleNamespace(data={"sensors": {}})
        adder = LateEntityAdder(coordinator, lambda ents: None, lambda k, i: [_FakeEntity(None)], "valve")

        adder.collect("k", {})

        assert adder.ledger.unique_ids_for("k") == frozenset()
        assert "k" not in adder.ledger.keys()  # noqa: SIM118 -- a named accessor, not a mapping

    def test_the_descriptor_carries_the_last_listing_seen(self):
        """The card has to name a device whose key has left the poll entirely,
        so the descriptor is what survives it."""
        coordinator = SimpleNamespace(data={"sensors": {}})
        adder = LateEntityAdder(coordinator, lambda ents: None, lambda k, i: [_FakeEntity("z1")], "valve")

        adder.collect("k", {"addr": 1, "model": "HTV245FRF", "sub_name": "Old", "hub_name": "Hub A", "hub_paired": True})
        adder.collect("k", {"addr": 1, "model": "HTV245FRF", "sub_name": "New", "hub_name": "Hub A", "hub_paired": True})

        assert adder.ledger.descriptor_for("k") == {
            "addr": 1,
            "model": "HTV245FRF",
            "sub_name": "New",
            "hub_name": "Hub A",
            "hub_paired": True,
        }

    def test_the_descriptor_carries_the_hub_pairing_verdict(self):
        """The card names no hub for a Bluetooth-paired device, so the verdict
        has to survive the key leaving the poll alongside the names it
        qualifies. A sensor entry that predates the stamp yields None, which
        the record builder reads as the hub-paired default rather than as
        evidence of a Bluetooth pairing."""
        coordinator = SimpleNamespace(data={"sensors": {}})
        # One unique_id per key: a shared id would be suppressed by the
        # add-once gate on the second key, and record writes no descriptor for
        # a key it holds no ids for.
        adder = LateEntityAdder(coordinator, lambda ents: None, lambda k, i: [_FakeEntity(f"{k}_z1")], "valve")

        adder.collect("bt", {"addr": 1, "model": "HTV210B", "sub_name": "BT", "hub_name": "", "hub_paired": False})
        adder.collect("old", {"addr": 2, "model": "HTV210B", "sub_name": "Old", "hub_name": "Hub A"})

        assert adder.ledger.descriptor_for("bt")["hub_paired"] is False
        assert adder.ledger.descriptor_for("old")["hub_paired"] is None

    def test_an_unknown_key_has_no_descriptor(self):
        """Reading a key nothing was recorded for must not raise."""
        assert EmittedEntityLedger().descriptor_for("nope") == {}

    def test_a_key_this_adder_builds_nothing_for_is_not_described(self):
        """collect runs for every sensor key in the account on every update,
        including the ones a given platform builds no entity for. Describing
        those would leave one entry per key on every adder that forget can
        never reach, since it only runs for keys with recorded unique_ids."""
        coordinator = SimpleNamespace(data={"sensors": {}})
        adder = LateEntityAdder(coordinator, lambda ents: None, lambda k, i: [], "valve")

        adder.collect("k", {"addr": 1, "model": "HCS026FRF", "sub_name": "Soil", "hub_name": "Hub A"})

        assert adder.ledger.descriptor_for("k") == {}
        assert adder.ledger._descriptors == {}

    def test_forget_drops_the_key_from_both_structures_at_once(self):
        """The lockstep half: the ids only stop being remembered when the rows
        they name have actually been removed, and then in both places."""
        coordinator = SimpleNamespace(data={"sensors": {}})
        adder = LateEntityAdder(coordinator, lambda ents: None, lambda k, i: [_FakeEntity("z1")], "valve")
        adder.collect("k", {})

        adder.forget("k")

        assert adder.ledger.unique_ids_for("k") == frozenset()
        assert adder.ledger.descriptor_for("k") == {}
        assert "z1" not in adder._emitted

    def test_forgetting_a_key_lets_a_later_reappearance_emit_again(self):
        """Without this, a removed key that returns gains no entities until a
        reload, which would be a new silent failure mode of its own."""
        coordinator = SimpleNamespace(data={"sensors": {}})
        adder = LateEntityAdder(coordinator, lambda ents: None, lambda k, i: [_FakeEntity("z1")], "valve")
        adder.collect("k", {})

        adder.forget("k")

        assert [e._attr_unique_id for e in adder.collect("k", {})] == ["z1"]

    def test_forgetting_an_unrecorded_key_is_a_no_op(self):
        """The remover calls forget on every adder, including ones that never
        emitted for that key."""
        coordinator = SimpleNamespace(data={"sensors": {}})
        adder = LateEntityAdder(coordinator, lambda ents: None, lambda k, i: [_FakeEntity("z1")], "valve")
        adder.collect("k", {})

        adder.forget("other")

        assert adder.ledger.unique_ids_for("k") == frozenset({"z1"})

    def test_a_held_id_keeps_its_key_described_while_the_rest_are_released(self):
        """The partial forget, which this adder can express because both halves
        of its bookkeeping are indexed by id.

        The held id names a row that is still registered, so releasing it would
        let a returning key offer a live unique_id a second time. The key keeps
        its descriptor alongside it, because a record still has to be buildable
        for it or the card could never be offered again for a retry.
        """
        coordinator = SimpleNamespace(data={"sensors": {}})
        adder = LateEntityAdder(coordinator, lambda ents: None, lambda k, i: [_FakeEntity("z1"), _FakeEntity("z2")], "valve")
        adder.collect("k", {"addr": 1, "model": "HTV245FRF", "sub_name": "Front", "hub_name": "Hub A"})

        adder.forget("k", frozenset({"z1"}))

        assert adder.ledger.unique_ids_for("k") == frozenset({"z1"})
        assert adder.ledger.descriptor_for("k")["sub_name"] == "Front"
        assert adder._emitted == {"z1"}

    def test_holding_an_id_this_key_never_held_still_drops_the_key(self):
        """The held set is intersected against the key's own ids, so a caller
        that over-approximates cannot resurrect an id from nowhere or gate a
        key whose rows all went."""
        coordinator = SimpleNamespace(data={"sensors": {}})
        adder = LateEntityAdder(coordinator, lambda ents: None, lambda k, i: [_FakeEntity("z1")], "valve")
        adder.collect("k", {"addr": 1, "model": "HTV245FRF", "sub_name": "Front", "hub_name": "Hub A"})

        adder.forget("k", frozenset({"elsewhere"}))

        assert adder.ledger.unique_ids_for("k") == frozenset()
        assert adder.ledger.descriptor_for("k") == {}
        assert adder._emitted == set()


class TestLateAdderStore:
    """The slot the removal sweep reaches the platforms' adders through."""

    def test_registering_appends_rather_than_replaces(self):
        """Three platforms publish into one entry store, and the sweep needs
        all of them, not the last one."""
        store: dict = {}
        register_late_adder(store, "first")
        register_late_adder(store, "second")

        assert store[LATE_ADDER_STORE_KEY] == ["first", "second"]
        assert late_adders(store) == ["first", "second"]

    def test_an_entry_store_with_no_adders_reads_as_none(self):
        """A config entry whose platforms have not set up yet is ordinary."""
        assert late_adders({}) == []

    def test_an_unreadable_entry_store_degrades_instead_of_raising(self):
        """Called from a coordinator listener and from a Repairs flow step,
        where raising breaks something much larger than this read."""
        assert late_adders(object()) == []


class _AddEntitiesSpy:
    """Every unique_id ever handed to Home Assistant, call by call.

    Accumulating rather than remembering the last call is the point: the
    property under test is about the whole session, and asserting on the last
    call alone would pass against an adder that offered the same id twice.
    """

    def __init__(self):
        """Start with no calls recorded."""
        self.calls: list[list] = []

    def __call__(self, entities, **kwargs):
        """Record one async_add_entities call's unique_ids."""
        self.calls.append([getattr(e, "_attr_unique_id", None) for e in entities])

    @property
    def all_ids(self) -> list:
        """Return every unique_id across every call, in order."""
        return [unique_id for call in self.calls for unique_id in call]

    def count(self, unique_id) -> int:
        """Return how many times one unique_id was offered in total."""
        return self.all_ids.count(unique_id)


def _valve_entry():
    """A reporting valve hub entry, the per-zone platform shape."""
    from custom_components.rainpoint.const import MODEL_VALVE_245

    return make_sensor_entry(
        hid=100,
        mid=200,
        addr=1,
        model=MODEL_VALVE_245,
        sub_name="Valve Hub",
        data={"type": "valve_hub", "zones": {1: {"open": False, "duration_seconds": 0}}},
    )


def _moisture_entry():
    """A reporting moisture entry, the per-key platform shape."""
    from custom_components.rainpoint.const import MODEL_MOISTURE_SIMPLE

    return make_sensor_entry(
        hid=100,
        mid=200,
        addr=1,
        model=MODEL_MOISTURE_SIMPLE,
        sub_name="Soil",
        data={"type": "moisture_simple", "moisture_percent": 42, "rssi_dbm": -70, "battery_percent": 80},
    )


async def _setup_platform_adder(module_name, entry_builder):
    """Run one platform's real async_setup_entry and return (adder, spy, key).

    The adder is fetched back out of the entry store rather than rebuilt, so
    the build closure under test is the platform's genuine one and the store
    wiring is exercised at the same time.
    """
    import importlib

    from custom_components.rainpoint.const import DOMAIN

    module = importlib.import_module(f"custom_components.rainpoint.{module_name}")
    key = "100_200_1"
    coordinator = MagicMock()
    coordinator.data = make_coordinator_data(sensors={key: entry_builder()})
    hass = MagicMock()
    entry = MagicMock()
    entry.entry_id = "e1"
    entry.options = {}
    hass.data = {DOMAIN: {"e1": {"coordinator": coordinator}}}

    spy = _AddEntitiesSpy()
    await module.async_setup_entry(hass, entry, spy)

    adder = late_adders(hass.data[DOMAIN]["e1"])[0]
    return adder, spy, key


class TestNoUniqueIdIsEverOfferedTwice:
    """The whole emit, gate, forget, re-emit cycle, on all three platforms' adders.

    Forgetting a key is coupled to an actual removal of its rows, so the
    add-once guarantee has to survive a repeat collect before the forget and
    has to release exactly once after it. Asserted with an accumulating spy
    rather than on the last call, because an adder that offered an id twice
    would still show a correct last call.
    """

    @pytest.mark.parametrize(
        ("module_name", "entry_builder"),
        [
            ("valve", _valve_entry),
            ("number", _valve_entry),
            ("sensor", _moisture_entry),
        ],
    )
    @pytest.mark.asyncio
    async def test_the_full_cycle_offers_each_id_once_per_genuine_emission(self, module_name, entry_builder):
        """Emit, gate, forget, re-emit: two emissions, never two offers of one."""
        adder, spy, key = await _setup_platform_adder(module_name, entry_builder)

        recorded = adder.ledger.unique_ids_for(key)
        assert recorded, "the platform emitted nothing, so the cycle proves nothing"
        assert all(spy.count(unique_id) == 1 for unique_id in recorded)

        # The gate still holds: a repeat while the ledger holds the ids offers
        # nothing, which is the half of the guarantee that must not regress.
        adder.async_on_coordinator_update()
        assert all(spy.count(unique_id) == 1 for unique_id in recorded)

        adder.forget(key)
        assert adder.ledger.unique_ids_for(key) == frozenset()

        adder.async_on_coordinator_update()
        assert all(spy.count(unique_id) == 2 for unique_id in recorded)
        assert adder.ledger.unique_ids_for(key) == recorded

        # No single call ever carried a duplicate, on any leg of the cycle.
        for call in spy.calls:
            assert len(call) == len(set(call))
