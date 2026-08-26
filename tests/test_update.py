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

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import ClientError

from custom_components.rainpoint.api import RainPointApiError
from custom_components.rainpoint.const import DOMAIN
from custom_components.rainpoint.hub_entities import RainPointHubFirmwareSensor
from custom_components.rainpoint.update import (
    RainPointHubFirmwareUpdate,
    async_setup_entry,
)
from tests.helpers import make_hub_info, make_mock_session_client, mock_json_response


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


def _make_hass(hubs=None, with_client=True):
    """Return a mock hass carrying coordinator data and, unless told otherwise, a client."""
    coord = MagicMock()
    coord.data = {"hubs": hubs if hubs is not None else [], "sensors": {}}
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
