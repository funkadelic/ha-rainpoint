"""Tests for the binary_sensor platform setup, the connectivity entity, the push
connection entity, and the HIC801W per-station watering entities."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from homeassistant.components.binary_sensor import BinarySensorDeviceClass

from custom_components.rainpoint.api import decode_hic801w
from custom_components.rainpoint.binary_sensor import (
    RainPointHicStationWateringBinarySensor,
    _build_hic801w_station_entities,
    async_setup_entry,
)
from custom_components.rainpoint.const import CONF_HIDS, DOMAIN, MODEL_HIC801W, PUSH_CONNECTED_UNIQUE_ID_SUFFIX
from custom_components.rainpoint.coordinator import SILENT_DATA_TYPE, SILENT_DEBOUNCE_POLLS, RainPointCoordinator
from custom_components.rainpoint.entity import late_adders
from custom_components.rainpoint.hub_entities import (
    RainPointHubConnectivityBinarySensor,
    RainPointPushConnectedBinarySensor,
)
from tests.helpers import make_coordinator_data, make_sensor_entry
from tests.payload_samples import SAMPLE_HIC801W_IDLE_PAYLOAD, SAMPLE_HIC801W_STATION3_PAYLOAD

_UNSET = object()


def _make_hass(hubs=None, mqtt_client=_UNSET):
    """Return a mock hass whose entry object graph mirrors __init__.async_setup_entry.

    A caller-supplied mqtt_client is used as-is (MagicMock instances are callable,
    so it must not be invoked); the default builds a fresh mock, and None means
    push is disabled.
    """
    coord = MagicMock()
    coord.data = {"hubs": hubs if hubs is not None else [], "sensors": {}, "hub_connectivity": {}}
    hass = MagicMock()
    entry = MagicMock()
    entry.entry_id = "test_entry"
    data = {"coordinator": coord}
    client = MagicMock() if mqtt_client is _UNSET else mqtt_client
    if client is not None:
        data["mqtt_client"] = client
    hass.data = {DOMAIN: {entry.entry_id: data}}
    return hass, entry, coord


def _hub(hid=100, name="Hub 1", mid=None):
    return {"hid": hid, "mid": mid if mid is not None else hid, "name": name, "model": "HTV0540FRF"}


def _bt_wrapper_hub(mid=999):
    """A Bluetooth wrapper record: every identity field present as an empty string."""
    return {"hid": 100, "mid": mid, "did": "", "mac": "", "productKey": "", "model": "", "name": ""}


class TestBinarySensorSetupEntry:
    """Tests for binary_sensor async_setup_entry."""

    @pytest.mark.asyncio
    async def test_no_mqtt_client_yields_only_the_connectivity_entity(self):
        """With push disabled (no mqtt_client), a push-disabled install with one
        real hub still yields exactly one entity: the cloud-connectivity sensor."""
        hass, entry, _coord = _make_hass(hubs=[_hub()], mqtt_client=None)
        add = MagicMock()

        await async_setup_entry(hass, entry, add)

        add.assert_called_once()
        entities = add.call_args[0][0]
        assert len(entities) == 1
        assert isinstance(entities[0], RainPointHubConnectivityBinarySensor)

    @pytest.mark.asyncio
    async def test_one_connected_entity_bound_to_the_clients_hub(self):
        """Exactly one push-connected sensor is created, for the hub the single
        MQTT client is bound to -- not one per configured hub (which would show
        unrelated hubs the shared client's state). Connectivity entities exist
        for every hub alongside it, so the assertion is by type, not by count."""
        hubs = [_hub(100, "Hub 1", mid=111), _hub(200, "Hub 2", mid=222)]
        client = MagicMock()
        client.hub_mid = 222  # client is bound to the second hub
        hass, entry, _coord = _make_hass(hubs=hubs, mqtt_client=client)
        add = MagicMock()

        await async_setup_entry(hass, entry, add)

        add.assert_called_once()
        entities = add.call_args[0][0]
        push_entities = [e for e in entities if isinstance(e, RainPointPushConnectedBinarySensor)]
        assert len(push_entities) == 1
        # Bound to the second hub (mid 222), not the first (mid 111).
        assert push_entities[0]._hub_info["mid"] == 222

    @pytest.mark.asyncio
    async def test_no_hubs_adds_no_entities(self):
        """No hubs at all -> no entities and no add call, push enabled or not."""
        hass, entry, _coord = _make_hass(hubs=[], mqtt_client=MagicMock())
        add = MagicMock()

        await async_setup_entry(hass, entry, add)

        add.assert_not_called()

    @pytest.mark.asyncio
    async def test_bluetooth_wrapper_record_yields_no_extra_connectivity_entity(self):
        """One real hub plus one Bluetooth wrapper record yields exactly one
        connectivity entity, not two."""
        hass, entry, _coord = _make_hass(hubs=[_hub(), _bt_wrapper_hub()], mqtt_client=None)
        add = MagicMock()

        await async_setup_entry(hass, entry, add)

        entities = add.call_args[0][0]
        connectivity_entities = [e for e in entities if isinstance(e, RainPointHubConnectivityBinarySensor)]
        assert len(connectivity_entities) == 1

    @pytest.mark.asyncio
    async def test_setup_entry_skips_non_dict_sensor_records(self):
        """A malformed sub-device record must not abort setup and drop the
        connectivity entity built from real hubs, matching the defensive
        filter valve.py and sensor.py already apply at setup."""
        hass, entry, coord = _make_hass(hubs=[_hub()], mqtt_client=None)
        coord.data["sensors"] = {"bad": "not-a-dict"}
        add = MagicMock()

        await async_setup_entry(hass, entry, add)

        entities = add.call_args[0][0]
        assert [e for e in entities if isinstance(e, RainPointHicStationWateringBinarySensor)] == []
        assert len([e for e in entities if isinstance(e, RainPointHubConnectivityBinarySensor)]) == 1


class TestConnectivityRealTimeline:
    """Drives the real coordinator/platform-setup sequence rather than an injected snapshot."""

    @staticmethod
    def _build(connected_value="1"):
        """Return (coordinator, hass, entry, client) wired the way __init__.py wires them."""
        client = AsyncMock()
        client.get_devices_by_hid.return_value = [_hub(hid=100, mid=200)]
        client.get_multiple_device_status.return_value = [
            {"mid": 200, "subDeviceStatus": [{"id": "connected", "value": connected_value}]}
        ]

        entry = MagicMock()
        entry.entry_id = "test_entry"
        entry.data = {CONF_HIDS: [100]}

        hass = MagicMock()
        hass.data = {DOMAIN: {"test_entry": {}}}

        coordinator = RainPointCoordinator(hass, client, entry)
        hass.data[DOMAIN]["test_entry"]["coordinator"] = coordinator
        hass.data[DOMAIN]["test_entry"]["mqtt_client"] = None

        return coordinator, hass, entry, client

    @pytest.mark.asyncio
    async def test_connected_to_disconnected_transition_moves_is_on(self):
        """Construct, first refresh, platform setup, then a further refresh whose
        connected value has flipped -- asserted between each step, not from an
        injected coordinator.data snapshot."""
        coordinator, hass, entry, client = self._build(connected_value="1")

        await coordinator.async_config_entry_first_refresh()

        captured = []
        add = MagicMock(side_effect=lambda ents, **kw: captured.extend(ents))
        await async_setup_entry(hass, entry, add)

        connectivity_entities = [e for e in captured if isinstance(e, RainPointHubConnectivityBinarySensor)]
        assert len(connectivity_entities) == 1
        entity = connectivity_entities[0]
        assert entity.is_on is True

        client.get_multiple_device_status.return_value = [{"mid": 200, "subDeviceStatus": [{"id": "connected", "value": "0"}]}]
        await coordinator.async_refresh()

        # Same entity object, no second setup call, no reload.
        assert entity.is_on is False


class TestRainPointPushConnectedBinarySensor:
    """Tests for the push connection-state entity."""

    def _make(self, connected=True):
        mqtt_client = MagicMock()
        mqtt_client.connected = connected
        return RainPointPushConnectedBinarySensor(mqtt_client, _hub()), mqtt_client

    def test_is_on_tracks_connected(self):
        entity, mqtt_client = self._make(connected=True)
        assert entity.is_on is True
        mqtt_client.connected = False
        assert entity.is_on is False

    def test_unique_id_and_category_and_enabled_by_default(self):
        entity, _ = self._make()
        assert entity._attr_unique_id.endswith(f"_{PUSH_CONNECTED_UNIQUE_ID_SUFFIX}")
        assert entity._attr_entity_category == "diagnostic"
        # Enabled by default: the entity never opts out of the registry.
        assert getattr(entity, "_attr_entity_registry_enabled_default", True) is True

    def test_available_true_when_client_present(self):
        entity, _ = self._make()
        assert entity.available is True

    @pytest.mark.asyncio
    async def test_registers_and_unregisters_state_listener(self):
        """The entity subscribes to client state changes for its lifetime."""
        entity, mqtt_client = self._make()

        await entity.async_added_to_hass()
        mqtt_client.add_state_listener.assert_called_once_with(entity._handle_client_state)

        await entity.async_will_remove_from_hass()
        mqtt_client.remove_state_listener.assert_called_once_with(entity._handle_client_state)

    def test_handle_client_state_writes_ha_state(self):
        entity, _ = self._make()
        entity.async_write_ha_state = MagicMock()
        entity._handle_client_state()
        entity.async_write_ha_state.assert_called_once_with()


class TestHicStationWateringEntities:
    """Per-entity behaviour for RainPointHicStationWateringBinarySensor,
    driven through decode_hic801w on the real committed frames rather than a
    hand-built data dict."""

    @staticmethod
    def _stations(raw_payload):
        """Decode one raw HIC801W frame and build all eight station entities for it."""
        sensor_key = "100_200_3"
        entry = make_sensor_entry(
            hid=100,
            mid=200,
            addr=3,
            model=MODEL_HIC801W,
            sub_name="Irrigation Controller",
            data=decode_hic801w(raw_payload),
        )
        coordinator = MagicMock()
        coordinator.data = make_coordinator_data(sensors={sensor_key: entry})
        return _build_hic801w_station_entities(coordinator, sensor_key, entry)

    def test_exactly_eight_entities_with_the_locked_unique_ids_and_names(self):
        stations = self._stations(SAMPLE_HIC801W_STATION3_PAYLOAD)
        assert len(stations) == 8
        assert [e._attr_unique_id for e in stations] == [f"rainpoint_100_200_3_station{n}_watering" for n in range(1, 9)]
        assert [e._attr_name for e in stations] == [f"Station {n} Watering" for n in range(1, 9)]

    def test_all_eight_carry_the_running_device_class(self):
        stations = self._stations(SAMPLE_HIC801W_STATION3_PAYLOAD)
        assert all(e._attr_device_class is BinarySensorDeviceClass.RUNNING for e in stations)

    def test_running_frame_is_on_for_exactly_the_running_station(self):
        """The reporter's station-3 capture: exactly one of the 8 is on, and
        it is the _station3_watering entity."""
        stations = self._stations(SAMPLE_HIC801W_STATION3_PAYLOAD)
        on_ids = [e._attr_unique_id for e in stations if e.is_on is True]
        assert on_ids == ["rainpoint_100_200_3_station3_watering"]
        assert all(e.is_on is False for e in stations if e._attr_unique_id != "rainpoint_100_200_3_station3_watering")

    def test_idle_frame_all_eight_are_off(self):
        stations = self._stations(SAMPLE_HIC801W_IDLE_PAYLOAD)
        assert all(e.is_on is False for e in stations)

    def test_rejected_frame_all_eight_read_none_but_stay_available(self):
        """A b3-mutated frame decodes to the error envelope (D-10): every
        station reads no state, and available stays True because the device
        is reachable and it is the payload that did not parse."""
        mutated = SAMPLE_HIC801W_STATION3_PAYLOAD.replace("F703FF0300F9", "F703FF0301F9")
        assert mutated != SAMPLE_HIC801W_STATION3_PAYLOAD
        stations = self._stations(mutated)
        assert all(e.is_on is None for e in stations)
        assert all(e.available is True for e in stations)

    def test_missing_reading_reads_no_state(self):
        """A sensor key with no reading at all (data is None) is the other
        falsy-data branch is_on guards, distinct from a parsed envelope whose
        current_station is None."""
        sensor_key = "100_200_3"
        entry = make_sensor_entry(hid=100, mid=200, addr=3, model=MODEL_HIC801W, data=None)
        coordinator = MagicMock()
        coordinator.data = make_coordinator_data(sensors={sensor_key: entry})
        station = RainPointHicStationWateringBinarySensor(coordinator, sensor_key, entry, "100_200_3", 1)
        assert station.is_on is None

    def test_out_of_range_station_reads_no_state_on_all_eight(self):
        """A frame that clears the shape check but carries a b0 outside 0
        through 8 yields no state on every station, never a confident False.

        The shape check rejects only on a non-zero b3, so an out-of-range b0
        reaches the entity inside an otherwise-valid envelope. Reporting
        False for all eight there would let an automation read `not is_on` as
        evidence that a station is off on the strength of a corrupt byte,
        which is the wrong-state-instead-of-no-state failure HIC-05 forbids.
        RainPointHicCurrentStationSensor guards the same case through its
        closed option list (D-05); this is the binary_sensor half of it.
        """
        # SAMPLE_HIC801W_STATION3_PAYLOAD's STA_WATER_ZONES value is
        # 03FF0300; raise b0 from 03 to 09 while leaving b3 at 00, so the
        # frame still passes the shape check.
        mutated = SAMPLE_HIC801W_STATION3_PAYLOAD.replace("F703FF0300F9", "F709FF0300F9")
        assert mutated != SAMPLE_HIC801W_STATION3_PAYLOAD

        decoded = decode_hic801w(mutated)
        # The decoder itself reports the byte it read: the range judgement
        # belongs to the entities, not to the decode.
        assert decoded["decoder"] == "hic801w_hex"
        assert decoded["current_station"] == 9

        stations = self._stations(mutated)
        assert all(e.is_on is None for e in stations)
        assert all(e.available is True for e in stations)


class TestBuildHic801wStationEntitiesGuards:
    """The two guards in _build_hic801w_station_entities: silent entries and
    other models both yield zero entities from this platform."""

    def test_returns_empty_for_a_silent_entry(self):
        sensor_key = "100_200_3"
        entry = make_sensor_entry(
            hid=100,
            mid=200,
            addr=3,
            model=MODEL_HIC801W,
            data={"type": SILENT_DATA_TYPE, "silent_state": "never_reported"},
        )
        coordinator = MagicMock()
        coordinator.data = make_coordinator_data(sensors={sensor_key: entry})
        assert _build_hic801w_station_entities(coordinator, sensor_key, entry) == []

    def test_returns_empty_for_a_non_hic801w_model(self):
        """A sub-device of any other model produces zero station entities
        from this platform."""
        sensor_key = "100_200_3"
        entry = make_sensor_entry(hid=100, mid=200, addr=3, model="HTV210B", data={"type": "valve_hub"})
        coordinator = MagicMock()
        coordinator.data = make_coordinator_data(sensors={sensor_key: entry})
        assert _build_hic801w_station_entities(coordinator, sensor_key, entry) == []


def _hic801w_hub_devices(mid=200, addr=1):
    """A getDeviceByHid hub record carrying one HIC801W sub-device."""
    return [
        {
            "mid": mid,
            "name": "Hub A",
            "deviceName": "d",
            "productKey": "pk",
            "homeName": "H",
            "subDevices": [
                {
                    "addr": addr,
                    "name": "Irrigation Controller",
                    "model": MODEL_HIC801W,
                    "modelCode": 279,
                    "softVer": "1.1.1026",
                }
            ],
        }
    ]


def _hic801w_silent_status(mid=200):
    """A multipleDeviceStatus poll that carries no entry for the HIC801W.

    Matches htv210b_silent_status's shape: the hub itself is still
    enumerated, only its sub-device's status is missing.
    """
    return [{"mid": mid, "subDeviceStatus": []}]


def _hic801w_status(mid=200, payload=SAMPLE_HIC801W_STATION3_PAYLOAD):
    """A multipleDeviceStatus poll reporting the given raw HIC801W frame."""
    return [{"mid": mid, "subDeviceStatus": [{"id": "D01", "value": payload, "time": 1785420002247}]}]


class TestHicStationLateAddTimeline:
    """Drives the real construct -> first refresh -> platform setup ->
    refresh sequence for a genuinely silent HIC801W, proving the late-add
    path this plan gives binary_sensor.py for the first time.

    Mirrors tests/test_valve.py's TestSilentUnitGuardRealTimeline shape: the
    silent device must actually be silent (no status entry at all for its
    addr, not an empty or malformed one), built up through the real
    debounce rather than injected as an already-settled coordinator.data
    snapshot.
    """

    @staticmethod
    async def _build_silent_timeline():
        """Construct -> first refresh -> platform setup for an HIC801W that
        never reports. Returns (coordinator, client, hass, entry, captured)."""
        client = AsyncMock()
        client.get_devices_by_hid.return_value = _hic801w_hub_devices()
        client.get_multiple_device_status.return_value = _hic801w_silent_status()

        entry = MagicMock()
        entry.entry_id = "e1"
        entry.data = {CONF_HIDS: [100]}
        entry.options = {}
        hass = MagicMock()
        hass.data = {DOMAIN: {"e1": {}}}

        coordinator = RainPointCoordinator(hass, client, entry)
        hass.data[DOMAIN]["e1"]["coordinator"] = coordinator

        await coordinator.async_config_entry_first_refresh()

        captured = []
        async_add_entities = MagicMock(side_effect=lambda ents, **kw: captured.extend(ents))
        await async_setup_entry(hass, entry, async_add_entities)

        return coordinator, client, hass, entry, captured

    @pytest.mark.asyncio
    async def test_a_silent_from_the_start_hic801w_offers_no_station_entities(self):
        """No station entity across the whole silence timeline, even once the
        debounce elapses and the entry's model alone would have admitted it."""
        coordinator, _client, _hass, _entry, captured = await self._build_silent_timeline()
        key = "100_200_1"
        assert [e for e in captured if isinstance(e, RainPointHicStationWateringBinarySensor)] == []

        # Every refresh short of the debounce: the key has no trace at all yet.
        for _ in range(SILENT_DEBOUNCE_POLLS - 2):
            await coordinator.async_refresh()
            assert key not in coordinator.data["sensors"]
            assert [e for e in captured if isinstance(e, RainPointHicStationWateringBinarySensor)] == []

        # The debounce elapses: a silent entry appears, and still no station
        # entity is offered -- the build guard, not mere absence, is doing
        # the work from here on.
        await coordinator.async_refresh()
        silent_entry = coordinator.data["sensors"][key]
        assert silent_entry["data"]["type"] == SILENT_DATA_TYPE
        assert silent_entry["model"] == MODEL_HIC801W
        assert [e for e in captured if isinstance(e, RainPointHicStationWateringBinarySensor)] == []

    @pytest.mark.asyncio
    async def test_the_silent_hic801w_gains_all_eight_the_poll_it_starts_reporting(self):
        """The late adder promotes the entry the moment it stops being
        silent, through the same coordinator listener, with no reload and no
        second async_setup_entry call. A further poll on the same frame adds
        none, proving the add-once bookkeeping."""
        coordinator, client, _hass, _entry, captured = await self._build_silent_timeline()
        for _ in range(SILENT_DEBOUNCE_POLLS - 1):
            await coordinator.async_refresh()
        key = "100_200_1"
        assert coordinator.data["sensors"][key]["data"]["type"] == SILENT_DATA_TYPE
        assert [e for e in captured if isinstance(e, RainPointHicStationWateringBinarySensor)] == []

        client.get_multiple_device_status.return_value = _hic801w_status()
        await coordinator.async_refresh()

        station_entities = [e for e in captured if isinstance(e, RainPointHicStationWateringBinarySensor)]
        assert sorted(e._attr_unique_id for e in station_entities) == sorted(
            f"rainpoint_100_200_1_station{n}_watering" for n in range(1, 9)
        )

        client.get_multiple_device_status.return_value = _hic801w_status()
        before = len([e for e in captured if isinstance(e, RainPointHicStationWateringBinarySensor)])
        await coordinator.async_refresh()
        assert len([e for e in captured if isinstance(e, RainPointHicStationWateringBinarySensor)]) == before

    @pytest.mark.asyncio
    async def test_the_adder_is_published_with_the_binary_sensor_domain(self):
        """The link the Repairs removal path depends on: the adder this
        setup registers carries domain "binary_sensor", invisible from the
        entity assertions alone."""
        _coordinator, _client, hass, entry, _captured = await self._build_silent_timeline()
        adders = late_adders(hass.data[DOMAIN][entry.entry_id])
        assert any(a.domain == "binary_sensor" for a in adders)
