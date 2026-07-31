"""Tests for the shared sub-device entity plumbing (entity.py)."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

from custom_components.rainpoint.entity import LateEntityAdder, RainPointSubDeviceEntity, sub_device_attributes


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
    """Tests for the hub_connected marker sub_device_attributes adds (D-03)."""

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
        hiding the reading itself (D-01 stays untouched)."""
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
        """Regression pin for D-01: RainPointSubDeviceEntity.available must
        NOT be touched by hub cloud connectivity.

        This is the boundary the phase's plan review flagged as the one
        thing that actually closes D-01 out. A future reader tempted to
        "finish the job" by propagating hub_connected into availability
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
        adder = LateEntityAdder(coordinator, added.extend, build)
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
        adder = LateEntityAdder(coordinator, added.extend, lambda k, i: [_FakeEntity(k)])

        adder.async_on_coordinator_update()

        assert added == []
