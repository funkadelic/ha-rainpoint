"""End-to-end HIC801W timeline: a real coordinator poll through the real
sensor platform, proving the unsupported-device notification stops firing
and that a rejected frame yields no state, both on a live entity object
rather than on a constructed envelope or an injected coordinator.data
snapshot.

This behaviour spans the decoder, the coordinator's registry dispatch and
unknown-model notification, and the sensor platform's factory and entity
lifecycle, matching the tests/test_hub_identity.py and
tests/test_orphan_removal.py precedent of a feature-scoped module rather
than living inside tests/test_sensor.py.

This file also carries the phase's whole-set proof (TestHic801wWholeEntitySet):
one HIC801W sub-device driven through both sensor.async_setup_entry and
binary_sensor.async_setup_entry off a single coordinator first refresh,
asserting the emitted unique-ID set as an equality against the locked
table, one device-registry identity across both platforms, and the whole
set clearing together on a rejected frame and recovering together on the
next good one -- the assertion no per-plan test could make on its own.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.rainpoint import binary_sensor as binary_sensor_module
from custom_components.rainpoint import coordinator as coordinator_module
from custom_components.rainpoint.const import CONF_HIDS, DOMAIN, MODEL_HIC801W
from custom_components.rainpoint.coordinator import RainPointCoordinator
from custom_components.rainpoint.diagnostic_sensors import (
    RainPointBatterySensor,
    RainPointFirmwareVersionSensor,
    RainPointLastUpdatedSensor,
    RainPointRSSISensor,
)
from custom_components.rainpoint.sensor import (
    RainPointHicCurrentStationSensor,
    RainPointUnknownSensor,
    async_setup_entry,
)
from tests.payload_samples import SAMPLE_HIC801W_IDLE_PAYLOAD, SAMPLE_HIC801W_STATION3_PAYLOAD

_HUB_MID = 200
_SUB_ADDR = 1
_SENSOR_KEY = f"100_{_HUB_MID}_{_SUB_ADDR}"

# The station-3 capture's STA_WATER_ZONES b3 mutated from 00 to a non-zero
# byte, so the shape check rejects it without needing a second real
# capture.
_B3_MUTATED_PAYLOAD = SAMPLE_HIC801W_STATION3_PAYLOAD.replace("F703FF0300F9", "F703FF0301F9")
assert _B3_MUTATED_PAYLOAD != SAMPLE_HIC801W_STATION3_PAYLOAD


def _hub_devices():
    """One home, one hub, one HIC801W sub-device at addr 1, modelCode 279."""
    return [
        {
            "mid": _HUB_MID,
            "name": "Hub A",
            "deviceName": "d",
            "productKey": "pk",
            "homeName": "H",
            "subDevices": [
                {
                    "addr": _SUB_ADDR,
                    "name": "Irrigation Controller",
                    "model": MODEL_HIC801W,
                    "modelCode": 279,
                    "softVer": "1.1.1026",
                }
            ],
        }
    ]


def _status(payload: str):
    """One subDeviceStatus entry carrying the given raw HIC801W payload."""
    return [
        {
            "mid": _HUB_MID,
            "subDeviceStatus": [{"id": "D01", "value": payload, "time": 1785420002247}],
        }
    ]


def _build_coordinator(payload: str = SAMPLE_HIC801W_STATION3_PAYLOAD):
    """Construct the client, entry, hass and coordinator for one HIC801W poll.

    Shared by the two timelines below, which differ only in which platforms
    they set up after the first refresh. Nothing is started here: the caller
    owns the async_create patch and the first refresh, because both timelines
    assert on that seam and the patch has to span it.

    Returns (coordinator, client, hass, entry).
    """
    client = AsyncMock()
    client.get_devices_by_hid.return_value = _hub_devices()
    client.get_multiple_device_status.return_value = _status(payload)

    entry = MagicMock()
    entry.entry_id = "e1"
    entry.data = {CONF_HIDS: [100]}
    entry.options = {}
    hass = MagicMock()
    hass.data = {DOMAIN: {"e1": {}}}

    coordinator = RainPointCoordinator(hass, client, entry)
    hass.data[DOMAIN]["e1"]["coordinator"] = coordinator
    return coordinator, client, hass, entry


async def _build_hic801w_timeline():
    """Drive construct -> first refresh -> platform setup with the station-3
    capture, patching persistent_notification.async_create for the whole
    sequence so "no notification fires" is asserted rather than
    assumed.

    Returns (coordinator, client, entity, mock_notify).
    """
    coordinator, client, hass, entry = _build_coordinator()

    with patch.object(coordinator_module, "async_create") as mock_notify:
        await coordinator.async_config_entry_first_refresh()

        captured = []
        async_add_entities = MagicMock(side_effect=lambda ents, **kw: captured.extend(ents))
        await async_setup_entry(hass, entry, async_add_entities)

    stations = [e for e in captured if isinstance(e, RainPointHicCurrentStationSensor)]
    assert len(stations) == 1
    return coordinator, client, stations[0], mock_notify


class TestHic801wRealTimeline:
    """Construct -> first refresh -> platform setup -> refresh, driven on a
    real coordinator and real entity object throughout."""

    @pytest.mark.asyncio
    async def test_registration_stops_the_unknown_model_notification(self):
        """No unsupported-device notification, proven on the async_create
        seam coordinator.py imports,
        not inferred from registry membership alone."""
        coordinator, _client, _entity, mock_notify = await _build_hic801w_timeline()

        assert coordinator.data["sensors"][_SENSOR_KEY]["data"]["type"] == "irrigation_controller"
        assert coordinator.data["sensors"][_SENSOR_KEY]["data"]["type"] != "unknown"
        mock_notify.assert_not_called()

    @pytest.mark.asyncio
    async def test_station_3_capture_reads_3_and_stays_available(self):
        """The real platform, off a real first refresh, builds one Current
        Station sensor reading the station-3 capture's settled value."""
        _coordinator, _client, entity, _mock_notify = await _build_hic801w_timeline()

        assert entity.native_value == "3"
        assert entity.available is True

    @pytest.mark.asyncio
    async def test_idle_refresh_moves_the_same_entity_object_to_none(self):
        """Swapping the polled payload to the idle capture and refreshing
        moves the same entity object to "none" -- no reload, no second
        setup."""
        coordinator, client, entity, _mock_notify = await _build_hic801w_timeline()

        client.get_multiple_device_status.return_value = _status(SAMPLE_HIC801W_IDLE_PAYLOAD)
        await coordinator.async_refresh()

        assert entity.native_value == "none"
        assert entity.available is True

    @pytest.mark.asyncio
    async def test_rejected_frame_yields_no_state_but_stays_available(self):
        """On a live object: a b3-mutated frame decodes to the
        error envelope, the same entity object reads no state, and it stays
        available because the device is reachable and still polling -- it is
        the payload that did not parse."""
        coordinator, client, entity, _mock_notify = await _build_hic801w_timeline()

        client.get_multiple_device_status.return_value = _status(_B3_MUTATED_PAYLOAD)
        await coordinator.async_refresh()

        assert entity.native_value is None
        assert entity.available is True


class TestHic801wWholeEntitySet:
    """The assertion no per-plan test could make: one HIC801W sub-device,
    driven through both sensor.async_setup_entry and
    binary_sensor.async_setup_entry off one coordinator's first refresh,
    yields exactly fourteen unique IDs (the thirteen entities this model
    publishes plus the sensor platform's unconditional
    disabled-by-default raw-payload diagnostic), all resolving to one
    device-registry identity, and the whole set clears to no state
    together on a rejected frame -- while every entity stays available --
    and recovers together on the next good poll.
    """

    _UID_PREFIX = f"rainpoint_100_{_HUB_MID}_{_SUB_ADDR}_"

    _EXPECTED_SENSOR_SUFFIXES = frozenset(
        {
            "current_station",
            "run_duration",
            "run_ends_at",
            "program_stations",
            "program_stations_completed",
            "raw_payload",
        }
    )
    _EXPECTED_BINARY_SUFFIXES = frozenset({f"station{n}_watering" for n in range(1, 9)})

    async def _build(self):
        """Drive construct -> first refresh -> both platforms' setup on the
        station-3 capture, patching persistent_notification.async_create for
        the whole sequence so "no notification fires" holds at the
        whole-set level too, not just for the sensor platform alone.

        Returns (coordinator, client, sensor_entities, binary_entities), the
        raw lists each platform's async_add_entities received -- filtering to
        just this sub-device's rows is the caller's job, since the hub itself
        also emits entities on both platforms and the set assertion must not
        silently pass because a hub entity happened to pad out a count.
        """
        coordinator, client, hass, entry = _build_coordinator()

        with patch.object(coordinator_module, "async_create") as mock_notify:
            await coordinator.async_config_entry_first_refresh()

            sensor_captured: list = []
            sensor_add = MagicMock(side_effect=lambda ents, **kw: sensor_captured.extend(ents))
            await async_setup_entry(hass, entry, sensor_add)

            binary_captured: list = []
            binary_add = MagicMock(side_effect=lambda ents, **kw: binary_captured.extend(ents))
            await binary_sensor_module.async_setup_entry(hass, entry, binary_add)

        mock_notify.assert_not_called()
        return coordinator, client, sensor_captured, binary_captured

    def _hic_sensor_entities(self, sensor_captured):
        return [e for e in sensor_captured if e._attr_unique_id.startswith(self._UID_PREFIX)]

    def _hic_binary_entities(self, binary_captured):
        return [e for e in binary_captured if e._attr_unique_id.startswith(self._UID_PREFIX)]

    @pytest.mark.asyncio
    async def test_the_union_is_exactly_the_locked_fourteen_ids_split_by_domain(self):
        """The locked set as an equality, not a superset or a bare count: an entity
        added later for an undefined reading fails here, and a suffix that
        drifted between plans fails here too. Registry uniqueness is per
        (domain, platform, unique_id), so the domain split is asserted
        alongside the union, not folded away by it."""
        _coordinator, _client, sensor_captured, binary_captured = await self._build()

        sensor_ids = {e._attr_unique_id for e in self._hic_sensor_entities(sensor_captured)}
        binary_ids = {e._attr_unique_id for e in self._hic_binary_entities(binary_captured)}

        expected_sensor_ids = {f"{self._UID_PREFIX}{suffix}" for suffix in self._EXPECTED_SENSOR_SUFFIXES}
        expected_binary_ids = {f"{self._UID_PREFIX}{suffix}" for suffix in self._EXPECTED_BINARY_SUFFIXES}

        assert sensor_ids == expected_sensor_ids
        assert binary_ids == expected_binary_ids
        assert sensor_ids.isdisjoint(binary_ids)
        assert sensor_ids | binary_ids == expected_sensor_ids | expected_binary_ids
        assert len(sensor_ids | binary_ids) == 14

    @pytest.mark.asyncio
    async def test_no_id_carries_a_substring_for_an_unverified_reading(self):
        """The whole-set half of the unverified-field guarantee: the decoder half is
        asserted on the envelope keys in test_decoders.py
        (test_neither_envelope_carries_a_key_for_an_unverified_reading), and
        neither implies the other, so both are kept rather than one deleted
        as redundant. Checked on the suffix after the unique-ID prefix, since
        every id starts with "rainpoint_" and a literal check on the full id
        would spuriously match "rain" in that prefix on every entity."""
        _coordinator, _client, sensor_captured, binary_captured = await self._build()
        all_ids = {e._attr_unique_id for e in self._hic_sensor_entities(sensor_captured)}
        all_ids |= {e._attr_unique_id for e in self._hic_binary_entities(binary_captured)}
        assert all_ids

        banned = ("rain", "humid", "ts_det", "b3", "wkstate", "work_state")
        for unique_id in all_ids:
            suffix = unique_id.removeprefix(self._UID_PREFIX)
            for term in banned:
                assert term not in suffix, f"{unique_id!r} unexpectedly carries {term!r}"

    @pytest.mark.asyncio
    async def test_no_battery_rssi_firmware_last_updated_or_unknown_fallback_entity_exists(self):
        """No per-sub-device battery, RSSI, firmware or last-updated
        diagnostic, and no generic-fallback Unsupported sensor, exists
        anywhere in the union. HIC801W is registered in HAND_WRITTEN_MODELS
        which locks it out of the generic and Unsupported-fallback
        paths entirely, and variant 279 declares neither STA_BAT nor
        STA_RSSI, so a diagnostic entity here would read available with no
        value while the real readings already exist on the 278 hub
        record."""
        _coordinator, _client, sensor_captured, binary_captured = await self._build()
        union = list(sensor_captured) + list(binary_captured)
        for cls in (
            RainPointBatterySensor,
            RainPointRSSISensor,
            RainPointFirmwareVersionSensor,
            RainPointLastUpdatedSensor,
            RainPointUnknownSensor,
        ):
            assert not any(isinstance(e, cls) for e in union), cls.__name__

    @pytest.mark.asyncio
    async def test_the_whole_set_resolves_to_one_device_identity(self):
        """Every entity in the union, across both platforms, points at the
        same device-registry identity: one device page carrying all
        thirteen, not two. No per-station device fan-out either, even
        though the catalog's portNumber is 8 -- the wire carries one
        aggregate record, not eight per-station ones."""
        _coordinator, _client, sensor_captured, binary_captured = await self._build()
        union = self._hic_sensor_entities(sensor_captured) + self._hic_binary_entities(binary_captured)
        assert len(union) == 14

        identities = {frozenset(e.device_info["identifiers"]) for e in union}
        assert len(identities) == 1

    @pytest.mark.asyncio
    async def test_the_set_clears_together_and_recovers_together(self):
        """One continuous timeline on the same fourteen entity objects: the
        station-3 capture's running state across the whole set, then a
        b3-mutated refresh that must clear every one of them to no state at
        the same moment while every one stays available (at
        the whole-set level -- compared individually against None so a
        single stale entity names itself in the failure rather than hiding
        inside a summary), then an idle refresh that must move the same
        objects to their idle values, proving the no-state condition is not
        sticky."""
        coordinator, client, sensor_captured, binary_captured = await self._build()
        hic_sensors = self._hic_sensor_entities(sensor_captured)
        hic_binaries = self._hic_binary_entities(binary_captured)
        assert len(hic_sensors) + len(hic_binaries) == 14
        by_suffix = {e._attr_unique_id.removeprefix(self._UID_PREFIX): e for e in hic_sensors}
        stations_by_num = {e._station_num: e for e in hic_binaries}
        assert set(stations_by_num) == set(range(1, 9))

        # Step 1: the station-3 capture's running state across the set.
        assert by_suffix["current_station"].native_value == "3"
        assert by_suffix["run_duration"].native_value == 60
        ends_at = by_suffix["run_ends_at"].native_value
        assert ends_at is not None
        assert ends_at.tzinfo is not None
        assert by_suffix["program_stations"].native_value == "1, 2, 3, 4, 5, 6, 7, 8"
        assert by_suffix["program_stations_completed"].native_value == "1, 2"
        assert stations_by_num[3].is_on is True
        for n in range(1, 9):
            if n != 3:
                assert stations_by_num[n].is_on is False

        # Step 2: a b3-mutated refresh clears the thirteen entities to
        # no state at once, on the same objects, while every one stays
        # available. The Raw Payload diagnostic is deliberately excluded
        # here: it is the platform's unconditional fourteenth entity, not
        # one of the thirteen, and its whole purpose is to keep showing the
        # last-received hex even when the decode failed, so a report is
        # diagnosable without a capture session.
        client.get_multiple_device_status.return_value = _status(_B3_MUTATED_PAYLOAD)
        await coordinator.async_refresh()

        thirteen_sensors = [suffix_entity for suffix, suffix_entity in by_suffix.items() if suffix != "raw_payload"]
        for entity in thirteen_sensors:
            assert entity.native_value is None, f"{entity._attr_unique_id!r} retained a value"
            assert entity.available is True, f"{entity._attr_unique_id!r} unexpectedly unavailable"
        for entity in hic_binaries:
            assert entity.is_on is None, f"{entity._attr_unique_id!r} retained a value"
            assert entity.available is True, f"{entity._attr_unique_id!r} unexpectedly unavailable"
        # The Raw Payload diagnostic itself is the deliberate exception:
        # still available, and still showing the raw hex it was handed even
        # though that hex failed to decode.
        assert by_suffix["raw_payload"].native_value is not None
        assert by_suffix["raw_payload"].available is True

        # Step 3: back to good. The idle capture moves the same objects to
        # their idle values, so the no-state condition above is not sticky.
        client.get_multiple_device_status.return_value = _status(SAMPLE_HIC801W_IDLE_PAYLOAD)
        await coordinator.async_refresh()

        assert by_suffix["current_station"].native_value == "none"
        assert by_suffix["run_duration"].native_value == 0
        assert by_suffix["run_ends_at"].native_value is None
        assert by_suffix["program_stations"].native_value == "none"
        assert by_suffix["program_stations_completed"].native_value == "none"
        assert all(entity.is_on is False for entity in hic_binaries)
        assert all(entity.available is True for entity in hic_sensors + hic_binaries)
