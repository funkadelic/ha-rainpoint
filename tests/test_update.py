"""Tests for the hub firmware update platform (update.py) and its client call.

Feature-scoped rather than module-scoped: the behaviour is one round trip that
spans ``api/client.py``'s ``get_hub_firmware_info`` and the entity that renders
its two possible shapes, and splitting it would put the payload fixtures in one
file and the assertions about them in another.

The two payloads below are the real captured responses, 2026-08-25, scrubbed of
the account's addressing identifiers. They are the whole point of the tests: the
envelope is byte-identical whether or not an upgrade exists, so ``info`` being
null is the only thing separating "up to date" from "update available", and a
test that invented its own payload could not demonstrate that.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import ClientError

from custom_components.rainpoint.api import RainPointApiError
from custom_components.rainpoint.const import DOMAIN
from custom_components.rainpoint.coordinator import SILENT_DATA_TYPE, SILENT_DEBOUNCE_POLLS
from custom_components.rainpoint.diagnostic_sensors import RainPointFirmwareVersionSensor
from custom_components.rainpoint.entity import late_adders
from custom_components.rainpoint.hub_entities import RainPointHubFirmwareSensor
from custom_components.rainpoint.update import (
    RainPointFirmwareUpdate,
    RainPointHubFirmwareUpdate,
    RainPointSubFirmwareUpdate,
    async_setup_entry,
)
from tests.helpers import (
    htv210b_silent_status,
    make_hub_info,
    make_mock_session_client,
    make_sensor_coordinator,
    mock_json_response,
)


def _logged_in_client():
    """Return a client whose token is valid, so the call under test is the only request."""
    client = make_mock_session_client()
    client._token_expires_at = datetime.now(UTC) + timedelta(hours=1)
    return client


# The hub the check says is current. "info" is null; there is no other difference.
CURRENT_PAYLOAD = {"info": None, "softVer": "1.1.1041"}

# The hub with an upgrade waiting, every field as captured.
UPGRADE_PAYLOAD = {
    "info": {
        "modelCode": 289,
        "versionName": "1.1.1041",
        "releaseTime": 0,
        "fileUrl": "https://oss3.homgarus.com/us/config/2/firmware/202606/redacted.bin",
        "updateMode": 0,
        "autoUpdate": 1,
        "remark": "",
        "mark": "Release version, changes: 1, 2, 3",
        "lastestFlag": 1,
        "block": 0,
        "hasCondition": 0,
        "conditionScript": None,
    },
    "softVer": "1.1.1032",
}


def _make_entity(payload=None, error=None):
    """Return an entity wired to a client that answers with payload (or raises)."""
    client = MagicMock()
    client.get_hub_firmware_info = AsyncMock(
        side_effect=error if error is not None else None,
        return_value=payload,
    )
    return RainPointHubFirmwareUpdate(client, make_hub_info(mid=361277)), client


def _make_hass(hubs=None, with_client=True, sensors=None):
    """Return a mock hass carrying coordinator data and, unless told otherwise, a client."""
    coord = MagicMock()
    coord.data = {"hubs": hubs if hubs is not None else [], "sensors": {} if sensors is None else sensors}
    hass = MagicMock()
    entry = MagicMock()
    entry.entry_id = "test_entry"
    store = {"coordinator": coord}
    if with_client:
        store["client"] = MagicMock()
    hass.data = {DOMAIN: {entry.entry_id: store}}
    return hass, entry


class TestFirmwareUpdateEntity:
    """The entity's two rendered states, and the states around them."""

    @pytest.mark.asyncio
    async def test_upgrade_available_reports_both_versions(self):
        """A populated "info" makes latest diverge from installed, which is the update."""
        entity, client = _make_entity(UPGRADE_PAYLOAD)

        await entity.async_update()

        assert entity.installed_version == "1.1.1032"
        assert entity.latest_version == "1.1.1041"
        assert entity.latest_version != entity.installed_version
        # The changelog is not surfaced at all: it is Chinese-only whatever locale
        # is asked for, so there is nothing an English-reading user could do with it.
        assert entity.release_summary is None
        assert entity.available is True
        client.get_hub_firmware_info.assert_awaited_once_with(361277)

    @pytest.mark.asyncio
    async def test_current_hub_reports_latest_equal_to_installed(self):
        """A null "info" must render as up to date, not as an unknown latest version.

        Leaving latest_version None would show the entity as "unknown" forever on
        a hub that is simply current, which is the failure this fallback prevents.
        """
        entity, _client = _make_entity(CURRENT_PAYLOAD)

        await entity.async_update()

        assert entity.installed_version == "1.1.1041"
        assert entity.latest_version == "1.1.1041"
        assert entity.release_summary is None
        assert entity.available is True

    @pytest.mark.asyncio
    async def test_unavailable_until_the_first_check_answers(self):
        """The entity starts unavailable and only becomes available once polled.

        Order matters here rather than the end state: constructing an entity that
        claimed availability before its only data source had answered would show a
        blank version pair as live.
        """
        entity, _client = _make_entity(CURRENT_PAYLOAD)

        assert entity.available is False
        assert entity.installed_version is None

        await entity.async_update()

        assert entity.available is True

    @pytest.mark.asyncio
    async def test_api_error_goes_unavailable_rather_than_stale(self):
        """A failed check must not keep presenting the last known versions as live."""
        entity, _client = _make_entity(UPGRADE_PAYLOAD)
        await entity.async_update()
        assert entity.available is True

        entity._client.get_hub_firmware_info = AsyncMock(side_effect=RainPointApiError("boom"))
        await entity.async_update()

        assert entity.available is False

    @pytest.mark.asyncio
    async def test_missing_soft_ver_is_not_a_usable_answer(self):
        """A 200 with no installed version leaves the entity unavailable.

        The call succeeded, so nothing raised, but an update entity with no
        installed version has nothing to compare and nothing to show.
        """
        entity, _client = _make_entity({"info": None})

        await entity.async_update()

        assert entity.available is False

    @pytest.mark.asyncio
    async def test_transport_failure_is_swallowed_rather_than_raised(self):
        """Nothing may escape async_update, or the entity is destroyed at setup.

        The platform adds entities with update_before_add, and Home Assistant
        aborts an entity whose first update raises rather than retrying it, so an
        escaping ClientError would remove the entity for the rest of the run. The
        earlier version of this method caught RainPointApiError alone and would
        have let every one of these through.
        """
        for error in (ClientError("connection reset"), TimeoutError(), ValueError("not json")):
            entity, _client = _make_entity(error=error)

            await entity.async_update()

            assert entity.available is False

    @pytest.mark.asyncio
    async def test_offer_without_a_version_does_not_read_as_up_to_date(self):
        """A malformed offer must show unknown, never "up to date".

        Detection keys on "info" being present, not on versionName being truthy.
        Falling back to the installed version here would hide a real upgrade
        behind a green tick, which is the worst available outcome.
        """
        entity, _client = _make_entity({"info": {"mark": ""}, "softVer": "1.1.1032"})

        await entity.async_update()

        assert entity.installed_version == "1.1.1032"
        assert entity.latest_version is None

    @pytest.mark.asyncio
    async def test_malformed_info_is_not_read_as_up_to_date(self):
        """A non-null "info" that is not an object must never render as current.

        Non-null is the contract's way of saying an upgrade exists. Falling back
        to the installed version for a shape nobody can parse would put a green
        tick on a hub that has an upgrade waiting, which is the one wrong answer
        worse than saying nothing.
        """
        entity, _client = _make_entity({"info": "unexpected", "softVer": "1.1.1032"})

        await entity.async_update()

        assert entity.installed_version == "1.1.1032"
        assert entity.latest_version is None

    @pytest.mark.asyncio
    async def test_empty_soft_ver_is_treated_as_no_version(self):
        """This cloud sends "" rather than omitting a key, so "" is not a version.

        The Bluetooth wrapper record is the documented case: every identity field
        arrives as an empty string, which is why is_hub_record tests truthiness.
        """
        entity, _client = _make_entity({"info": None, "softVer": ""})

        await entity.async_update()

        assert entity.available is False
        assert entity.installed_version is None

    def test_unique_id_does_not_collide_with_the_firmware_sensor(self):
        """The hub already owns the plain _firmware suffix for its version sensor.

        Both are persisted in the entity registry, so a collision would silently
        drop one of them rather than fail loudly.
        """
        hub_info = make_hub_info(mid=361277)
        update_entity = RainPointHubFirmwareUpdate(MagicMock(), hub_info)
        sensor = RainPointHubFirmwareSensor(MagicMock(), hub_info)

        assert update_entity.unique_id != sensor.unique_id
        assert update_entity.unique_id.endswith("_firmware_update")

    def test_entity_polls_itself(self):
        """This platform fetches its own data, unlike the coordinator-driven hub entities.

        RainPointHubDevice.__init__ sets should_poll False as an instance
        attribute, so a subclass that only set a class attribute would be
        silently overridden and never refresh.
        """
        entity = RainPointHubFirmwareUpdate(MagicMock(), make_hub_info())

        assert entity.should_poll is True


class TestUpdateSetupEntry:
    """Platform setup: which hubs get an entity, and which failures are survivable."""

    @pytest.mark.asyncio
    async def test_one_entity_per_real_hub(self):
        """A real hub plus a Bluetooth wrapper record yields exactly one entity."""
        real_hub = {"hid": 182509, "mid": 236547, "name": "Hub", "did": "17053410", "mac": "A8:46:74:BB:91:F0"}
        wrapper = {"hid": 182509, "mid": 346965, "name": "", "did": "", "mac": "", "model": "", "productKey": ""}
        hass, entry = _make_hass(hubs=[real_hub, wrapper])

        add_entities = MagicMock()
        await async_setup_entry(hass, entry, add_entities)

        entities = add_entities.call_args[0][0]
        assert len(entities) == 1
        assert isinstance(entities[0], RainPointHubFirmwareUpdate)

    @pytest.mark.asyncio
    async def test_first_check_runs_before_the_entity_is_added(self):
        """Without update_before_add the entity would sit unavailable for hours.

        SCAN_INTERVAL is measured in hours, so the first poll has to be the one
        that happens at setup rather than the one that happens on the next tick.
        """
        hub = {"hid": 182509, "mid": 236547, "name": "Hub", "did": "17053410", "mac": "A8:46:74:BB:91:F0"}
        hass, entry = _make_hass(hubs=[hub])

        add_entities = MagicMock()
        await async_setup_entry(hass, entry, add_entities)

        assert add_entities.call_args.kwargs["update_before_add"] is True

    @pytest.mark.asyncio
    async def test_no_hubs_adds_nothing_without_raising(self):
        """An empty snapshot is a normal state, not a setup failure."""
        hass, entry = _make_hass(hubs=[])

        add_entities = MagicMock()
        await async_setup_entry(hass, entry, add_entities)

        assert add_entities.call_args[0][0] == []

    @pytest.mark.asyncio
    async def test_non_list_hubs_snapshot_is_rejected(self):
        """A dict snapshot skips setup rather than crashing the platform."""
        hass, entry = _make_hass(hubs={"236547": {}})

        add_entities = MagicMock()
        await async_setup_entry(hass, entry, add_entities)

        add_entities.assert_not_called()

    @pytest.mark.asyncio
    async def test_missing_client_skips_setup(self):
        """Every value this platform shows comes from the client, so there is nothing to build without one."""
        hub = {"hid": 1, "mid": 2, "name": "Hub", "did": "d", "mac": "A8:46:74:BB:91:F0"}
        hass, entry = _make_hass(hubs=[hub], with_client=False)

        add_entities = MagicMock()
        await async_setup_entry(hass, entry, add_entities)

        add_entities.assert_not_called()


class TestGetHubFirmwareInfo:
    """The client call behind the entity."""

    @pytest.mark.asyncio
    async def test_returns_the_data_object_for_both_shapes(self):
        """Both captured payloads come back as-is; the branching belongs to the caller."""
        for payload in (CURRENT_PAYLOAD, UPGRADE_PAYLOAD):
            client = _logged_in_client()
            client._session.get = MagicMock(return_value=mock_json_response({"code": 0, "data": payload}))

            assert await client.get_hub_firmware_info(361277) == payload

    @pytest.mark.asyncio
    async def test_mid_is_sent_as_a_query_param(self):
        """The endpoint is keyed by mid alone."""
        client = _logged_in_client()
        client._session.get = MagicMock(return_value=mock_json_response({"code": 0, "data": CURRENT_PAYLOAD}))

        await client.get_hub_firmware_info(361277)

        assert client._session.get.call_args.kwargs["params"] == {"mid": 361277}
        assert client._session.get.call_args[0][0].endswith("/app/device/firmware/upgrade/info/v2")

    @pytest.mark.asyncio
    async def test_non_object_body_raises_rather_than_attribute_error(self):
        """A JSON body that is not an object must not reach .get().

        An AttributeError here would escape async_update, which catches the
        transport family and not that, and an exception escaping the first update
        destroys the entity for the rest of the run.
        """
        client = _logged_in_client()
        client._session.get = MagicMock(return_value=mock_json_response(["unexpected"]))

        with pytest.raises(RainPointApiError, match="not an object"):
            await client.get_hub_firmware_info(361277)

    @pytest.mark.asyncio
    async def test_http_error_raises(self):
        client = _logged_in_client()
        client._session.get = MagicMock(return_value=mock_json_response({}, status=500))

        with pytest.raises(RainPointApiError, match="get_hub_firmware_info HTTP 500"):
            await client.get_hub_firmware_info(361277)

    @pytest.mark.asyncio
    async def test_non_zero_code_raises(self):
        client = _logged_in_client()
        client._session.get = MagicMock(return_value=mock_json_response({"code": 3, "msg": "nope"}))

        with pytest.raises(RainPointApiError, match="get_hub_firmware_info failed: code 3"):
            await client.get_hub_firmware_info(361277)

    @pytest.mark.asyncio
    async def test_unusable_data_shape_returns_empty(self):
        """A list where an object belongs degrades to {} rather than raising into the poll.

        The list is non-empty on purpose: an empty one is already absorbed by the
        `or {}` fallback above the type check, so it would leave the guard untested.
        """
        client = _logged_in_client()
        client._session.get = MagicMock(return_value=mock_json_response({"code": 0, "data": [CURRENT_PAYLOAD]}))

        assert await client.get_hub_firmware_info(361277) == {}


def _make_sub_entity(payload=None, error=None, sid=504942, data=None):
    """Return a sub-device entity wired to a client that answers with payload (or raises)."""
    coordinator = make_sensor_coordinator(
        model="HTV210B",
        data={} if data is None else data,
        extra_sensor_info={"sid": sid},
    )
    client = MagicMock()
    client.get_sub_firmware_info = AsyncMock(
        side_effect=error if error is not None else None,
        return_value=payload,
    )
    entity = RainPointSubFirmwareUpdate(coordinator, client, "100_200_1", coordinator.data["sensors"]["100_200_1"])
    return entity, client


class TestSharedFirmwareUpdateBase:
    """The contract the two device-specific subclasses fill in."""

    def test_base_class_cannot_be_built_without_an_endpoint(self):
        """A subclass that forgets one must fail at construction, not at its first check.

        The platform adds with update_before_add, and an exception escaping the
        first update drops the entity for the run of the config entry, so a
        NotImplementedError body would have hidden the mistake exactly where it
        is most expensive.
        """
        with pytest.raises(TypeError, match="abstract"):
            RainPointFirmwareUpdate()


class TestSubFirmwareUpdateEntity:
    """The sub-device half: same decode, different endpoint and addressing."""

    @pytest.mark.asyncio
    async def test_check_is_addressed_by_sid(self):
        """sid, not addr and not mid: the settings endpoints' scheme, not the control ones'."""
        entity, client = _make_sub_entity(payload=CURRENT_PAYLOAD)

        await entity.async_update()

        client.get_sub_firmware_info.assert_awaited_once_with(504942)

    @pytest.mark.asyncio
    async def test_a_re_keyed_sid_is_picked_up_without_a_reload(self):
        """The cloud re-keys a sub-device's sid when it is re-paired.

        The maintainer's HTV210B was 491657 before one and 504942 after. A device
        re-paired into the same hub and address keeps this entity, so an id read
        once at construction would address something the cloud has retired for
        the life of the config entry.
        """
        entity, client = _make_sub_entity(payload=CURRENT_PAYLOAD, sid=491657)
        entity._coordinator.data["sensors"]["100_200_1"]["sid"] = 504942

        await entity.async_update()

        client.get_sub_firmware_info.assert_awaited_once_with(504942)

    @pytest.mark.asyncio
    async def test_a_poll_that_has_not_carried_this_device_falls_back(self):
        """An entry missing from the snapshot must not send None as an address."""
        entity, client = _make_sub_entity(payload=CURRENT_PAYLOAD)
        entity._coordinator.data["sensors"].clear()

        await entity.async_update()

        client.get_sub_firmware_info.assert_awaited_once_with(504942)

    @pytest.mark.asyncio
    async def test_both_envelope_shapes_decode_as_they_do_for_a_hub(self):
        """The probe found the sub endpoint's envelope byte-identical to the hub's.

        That is the whole reason the two share a base, so the sub path is asserted
        against both captured shapes rather than trusted to inherit correctly.
        """
        entity, _ = _make_sub_entity(payload=UPGRADE_PAYLOAD)
        await entity.async_update()
        assert (entity.installed_version, entity.latest_version) == ("1.1.1032", "1.1.1041")

        entity, _ = _make_sub_entity(payload=CURRENT_PAYLOAD)
        await entity.async_update()
        assert entity.installed_version == entity.latest_version == "1.1.1041"
        assert entity.available is True

    @pytest.mark.asyncio
    async def test_api_error_goes_unavailable_rather_than_stale(self):
        """Same contract as the hub's: one failed call means no version pair, not an old one."""
        entity, _ = _make_sub_entity(error=RainPointApiError("boom"))

        await entity.async_update()

        assert entity.available is False

    @pytest.mark.asyncio
    async def test_a_silent_sub_device_still_reports_its_firmware_check(self):
        """The half of the no-silence-gate decision that only a silent device can show.

        A reporting device cannot distinguish this entity's availability rule
        from the one every other sub-device entity uses, because the two agree.
        They disagree here: the readings are gone and the version pair is not,
        because it came from a cloud call keyed on sid rather than from the poll.
        """
        entity, _ = _make_sub_entity(
            payload=CURRENT_PAYLOAD,
            data={"type": SILENT_DATA_TYPE, "silent_state": "stopped_reporting", "last_seen": None},
        )

        await entity.async_update()

        assert entity.available is True
        assert entity.installed_version == "1.1.1041"

    def test_unique_id_does_not_collide_with_the_firmware_version_sensor(self):
        """The sub-device already has a Firmware Version diagnostic sensor on its own suffix."""
        coordinator = make_sensor_coordinator(extra_sensor_info={"sid": 1})
        info = coordinator.data["sensors"]["100_200_1"]
        entity = RainPointSubFirmwareUpdate(coordinator, MagicMock(), "100_200_1", info)
        sensor = RainPointFirmwareVersionSensor(coordinator, "100_200_1", info, "100_200_1")

        assert entity.unique_id == "rainpoint_100_200_1_firmware_update"
        assert entity.unique_id != sensor.unique_id

    def test_entity_polls_itself_and_lands_on_the_sub_device_page(self):
        """Self-polling like its hub sibling, but parented to the sub-device, not the hub.

        should_poll is asserted through the property Home Assistant reads, and it
        is why this class is not a RainPointSubDeviceEntity: CoordinatorEntity
        defines should_poll to hard-return False from ahead of the _attr_-reading
        one, so mixing this onto that base silently disables SCAN_INTERVAL.
        """
        entity, _ = _make_sub_entity()

        assert entity.should_poll is True
        assert entity.available is False
        assert (DOMAIN, "100_200_1") in entity.device_info["identifiers"]

    def test_the_coordinator_base_would_have_disabled_polling(self):
        """Pins the trap this class exists to avoid.

        The same mistake is available to every future platform that wants a
        self-polling sub-device entity, and it is invisible to a test double
        that does not carry the property, which is why conftest's
        CoordinatorEntity stand-in mirrors the real class's hard-coded False.
        """
        from homeassistant.helpers.update_coordinator import CoordinatorEntity

        class _Mixed(RainPointFirmwareUpdate, CoordinatorEntity):
            async def _fetch_firmware_info(self):
                return {}

        entity = _Mixed()
        entity._attr_should_poll = True

        assert entity.should_poll is False


class TestSubUpdateSetupEntry:
    """Which sub-devices get an entity."""

    @pytest.mark.asyncio
    async def test_one_entity_per_sub_device_carrying_a_sid(self):
        """A sub-device with no sid cannot be addressed, so it gets nothing rather than a broken entity.

        The non-dict member is the other skip: one malformed record must cost
        its own entity and no other, matching the listener's own guard.
        """
        sensors = {
            "1_2_1": {"hid": 1, "mid": 2, "addr": 1, "sid": 504942, "model": "HTV210B"},
            "1_2_2": {"hid": 1, "mid": 2, "addr": 2, "sid": None, "model": "HCS026FRF"},
            "1_2_3": "not a record",
        }
        hass, entry = _make_hass(hubs=[], sensors=sensors)

        add_entities = MagicMock()
        await async_setup_entry(hass, entry, add_entities)

        entities = add_entities.call_args[0][0]
        assert [e.unique_id for e in entities] == ["rainpoint_1_2_1_firmware_update"]

    @pytest.mark.asyncio
    async def test_no_model_gate(self):
        """The endpoint answers for the RF models too, so every addressable sub-device gets one."""
        sensors = {
            "1_2_1": {"hid": 1, "mid": 2, "addr": 1, "sid": 341550, "model": "HTV245FRF"},
            "1_2_2": {"hid": 1, "mid": 2, "addr": 2, "sid": 485351, "model": "HCS026FRF"},
        }
        hass, entry = _make_hass(hubs=[], sensors=sensors)

        add_entities = MagicMock()
        await async_setup_entry(hass, entry, add_entities)

        assert len(add_entities.call_args[0][0]) == 2

    @pytest.mark.asyncio
    async def test_non_dict_sensors_snapshot_still_yields_the_hub_entities(self):
        """A malformed sensors snapshot costs the sub-device entities, not the hub's."""
        hub = {"hid": 182509, "mid": 236547, "name": "Hub", "did": "17053410", "mac": "A8:46:74:BB:91:F0"}
        hass, entry = _make_hass(hubs=[hub], sensors=["not a dict"])

        add_entities = MagicMock()
        await async_setup_entry(hass, entry, add_entities)

        entities = add_entities.call_args[0][0]
        assert [type(e) for e in entities] == [RainPointHubFirmwareUpdate]

    @pytest.mark.asyncio
    async def test_the_adder_is_published_for_the_removal_sweep(self):
        """Without a published adder these rows are in no ledger, and a row in no
        ledger is one the departed-key removal cannot take, which leaves the
        sub-device's device registry row permanently non-empty."""
        sensors = {"1_2_1": {"hid": 1, "mid": 2, "addr": 1, "sid": 504942, "model": "HTV210B"}}
        hass, entry = _make_hass(hubs=[], sensors=sensors)

        await async_setup_entry(hass, entry, MagicMock())

        adders = late_adders(hass.data[DOMAIN][entry.entry_id])
        assert [a.domain for a in adders] == ["update"]
        assert adders[0].ledger.unique_ids_for("1_2_1") == frozenset({"rainpoint_1_2_1_firmware_update"})


class TestSubUpdateSilentTimeline:
    """The real sequence, driven rather than injected.

    A silent sub-device is absent from coordinator.data["sensors"] for its first
    SILENT_DEBOUNCE_POLLS polls, so the setup pass cannot see it and a snapshot
    test cannot express it. This is the population the no-silence-gate decision
    exists to serve, so it is the one the platform has to actually reach.
    """

    _HID = 10
    _MID = 20
    _ADDR = 1
    _SID = 504942
    _KEY = "10_20_1"

    @classmethod
    def _hub_devices(cls):
        """A listing carrying the sub-device's sid, which is where the sid comes from.

        The listing answers whether or not the device's status does, which is
        exactly why a firmware check can outlive a device's readings.
        """
        return [
            {
                "mid": cls._MID,
                "name": "Hub A",
                "deviceName": "hub-mac",
                "productKey": "hub-pk",
                "homeName": "H",
                "model": "HWG023WBRF-V2",
                "subDevices": [
                    {
                        "addr": cls._ADDR,
                        "sid": cls._SID,
                        "name": "BT Valve",
                        "model": "HTV210B",
                        "modelCode": 41,
                        "softVer": "1.0",
                    }
                ],
            }
        ]

    @classmethod
    async def _build_timeline(cls):
        """Construct -> first refresh -> update platform setup, device silent throughout."""
        from custom_components.rainpoint.const import CONF_HIDS
        from custom_components.rainpoint.coordinator import RainPointCoordinator

        client = AsyncMock()
        client.get_devices_by_hid.return_value = cls._hub_devices()
        client.get_multiple_device_status.return_value = htv210b_silent_status(mid=cls._MID)
        client.get_sub_firmware_info.return_value = CURRENT_PAYLOAD
        client.get_hub_firmware_info.return_value = CURRENT_PAYLOAD

        entry = MagicMock()
        entry.entry_id = "e1"
        entry.data = {CONF_HIDS: [cls._HID]}
        entry.options = {}
        hass = MagicMock()
        hass.data = {DOMAIN: {"e1": {"client": client}}}

        coordinator = RainPointCoordinator(hass, client, entry)
        hass.data[DOMAIN]["e1"]["coordinator"] = coordinator

        await coordinator.async_config_entry_first_refresh()

        captured = []
        add_kwargs = []
        add_entities = MagicMock(
            side_effect=lambda ents, **kw: (captured.extend(ents), add_kwargs.append(kw)),
        )
        await async_setup_entry(hass, entry, add_entities)
        return coordinator, client, captured, add_kwargs

    @pytest.mark.asyncio
    async def test_a_device_silent_at_setup_gains_its_entity_once_the_debounce_elapses(self):
        """Setup cannot see it, and the late adder is what makes that survivable."""
        coordinator, _client, captured, _kwargs = await self._build_timeline()

        subs = [e for e in captured if isinstance(e, RainPointSubFirmwareUpdate)]
        assert subs == []

        for _ in range(SILENT_DEBOUNCE_POLLS - 2):
            await coordinator.async_refresh()
            assert self._KEY not in coordinator.data["sensors"]
            assert [e for e in captured if isinstance(e, RainPointSubFirmwareUpdate)] == []

        await coordinator.async_refresh()

        entry = coordinator.data["sensors"][self._KEY]
        assert entry["data"]["type"] == SILENT_DATA_TYPE
        assert entry["sid"] == self._SID
        subs = [e for e in captured if isinstance(e, RainPointSubFirmwareUpdate)]
        assert [e.unique_id for e in subs] == ["rainpoint_10_20_1_firmware_update"]

    @pytest.mark.asyncio
    async def test_the_late_added_entity_checks_immediately_rather_than_in_six_hours(self):
        """SCAN_INTERVAL is measured in hours, so an entity added without
        update_before_add would sit unavailable until the next tick."""
        coordinator, _client, _captured, add_kwargs = await self._build_timeline()

        for _ in range(SILENT_DEBOUNCE_POLLS - 1):
            await coordinator.async_refresh()

        assert add_kwargs, "the adder never added anything"
        assert all(kw.get("update_before_add") is True for kw in add_kwargs)

    @pytest.mark.asyncio
    async def test_the_entity_is_offered_once_across_the_whole_timeline(self):
        """A repeated unique_id is an error in Home Assistant, and the entry stays
        in the snapshot on every poll after the debounce."""
        coordinator, _client, captured, _kwargs = await self._build_timeline()

        for _ in range(SILENT_DEBOUNCE_POLLS + 3):
            await coordinator.async_refresh()

        subs = [e for e in captured if isinstance(e, RainPointSubFirmwareUpdate)]
        assert len(subs) == 1


class TestGetSubFirmwareInfo:
    """The client call behind the sub-device entity."""

    @pytest.mark.asyncio
    async def test_sid_is_sent_as_a_query_param(self):
        """A different endpoint and a different addressing scheme from the hub check."""
        client = _logged_in_client()
        client._session.get = MagicMock(return_value=mock_json_response({"code": 0, "data": CURRENT_PAYLOAD}))

        assert await client.get_sub_firmware_info(504942) == CURRENT_PAYLOAD

        assert client._session.get.call_args.kwargs["params"] == {"sid": 504942}
        assert client._session.get.call_args[0][0].endswith("/app/device/sub/firmware/upgrade/info")

    @pytest.mark.asyncio
    async def test_failures_are_named_for_the_sub_call(self):
        """The label travels with the call, so a log line names which check failed."""
        client = _logged_in_client()
        client._session.get = MagicMock(return_value=mock_json_response({}, status=500))

        with pytest.raises(RainPointApiError, match="get_sub_firmware_info HTTP 500"):
            await client.get_sub_firmware_info(504942)


# Runs in a child interpreter for the same reason test_entity_naming.py's
# has_entity_name sweep does: this process is under the repository conftest,
# whose stubbed entity bases are a description of the shipped hierarchy rather
# than the hierarchy itself. should_poll is the property that description got
# wrong once, and it is the one this platform's entire SCAN_INTERVAL rests on.
_REAL_HA_POLLING_CHECK = """
import json
import sys

from custom_components.rainpoint.update import RainPointHubFirmwareUpdate, RainPointSubFirmwareUpdate

hub = RainPointHubFirmwareUpdate(None, {"hid": 1, "mid": 2})
sub = RainPointSubFirmwareUpdate(None, None, "1_2_1", {"hid": 1, "mid": 2, "addr": 1, "sid": 3})
json.dump({"hub": hub.should_poll, "sub": sub.should_poll}, sys.stdout)
"""


class TestBothEntitiesPollUnderRealHomeAssistant:
    """The one assertion no stub can make on this platform's behalf."""

    def test_should_poll_resolves_true_against_the_shipped_base_classes(self):
        """Both entities fetch their own data, so both must actually be polled.

        A class whose should_poll resolves False gets exactly one check, from
        update_before_add, and is then frozen for the run of the config entry:
        a single failed check leaves it permanently unavailable and
        SCAN_INTERVAL governs nothing. That is what mixing onto a
        CoordinatorEntity does, silently, and no _attr_ assignment overrides it.
        """
        repo_root = Path(__file__).resolve().parent.parent
        env = dict(os.environ)
        env["PYTHONPATH"] = os.pathsep.join(filter(None, [str(repo_root), env.get("PYTHONPATH", "")]))
        completed = subprocess.run(
            [sys.executable, "-c", _REAL_HA_POLLING_CHECK],
            cwd=repo_root,
            env=env,
            capture_output=True,
            text=True,
            check=False,
            timeout=300,
        )
        if completed.returncode != 0:
            pytest.fail(
                "The polling check could not run against the real Home Assistant.\n"
                f"exit code: {completed.returncode}\nstdout:\n{completed.stdout}\nstderr:\n{completed.stderr}"
            )

        assert json.loads(completed.stdout) == {"hub": True, "sub": True}
