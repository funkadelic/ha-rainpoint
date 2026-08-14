"""Tests for the diagnostics platform (diagnostics.py).

Two properties are worth separating, because they fail in different ways.

The *shape* property is that a value reaches the dump only by being named in an
allow-list, so a field the vendor adds tomorrow contributes its name and not its
contents. That is what `TestAllowList` and `TestUnlistedKeys` pin, and they read
the builders directly rather than the redacted payload, because a redactor
cannot be the thing that saves an unreviewed field.

The *disclosure* property is that no credential and no account identity survives
into the finished payload. `TestNothingSensitiveSurvives` walks the whole
returned structure for literal values rather than checking named keys, so a
value that reaches the dump through a key nobody anticipated still fails the
test.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from custom_components.rainpoint.const import DOMAIN, VERSION
from custom_components.rainpoint.diagnostics import (
    TO_REDACT,
    _select_allowed,
    async_get_config_entry_diagnostics,
    async_get_device_diagnostics,
)

REDACTED = "**REDACTED**"

# Values chosen to be findable: every one is a literal that must not appear
# anywhere in a finished dump, and each is distinctive enough that a substring
# walk cannot match it by accident.
SECRET_PASSWORD = "correct-horse-battery-staple"
SECRET_TOKEN = "tok_ABCDEFGHIJKLMNOP"
ACCOUNT_EMAIL = "someone@example.invalid"
HUB_MAC = "A8:46:74:BB:91:F0"
PRODUCT_KEY = "a3QrDxYPTM2"
IOT_ID = "jDQNeV92iFixCU42PDtUk0k0d4"

# The names a user chose, which the dump keeps on purpose. Distinctive for the
# same reason the secrets above are: a survival assertion made by walking every
# scalar is only worth something if the literal cannot match by accident.
HOME_NAME = "Richmond"
HUB_LABEL = "Garden Hub"
SUB_LABEL = "Front Lawn Valve"


def _hub_record(hid=182509, mid=236547, extra=None):
    """Return a top-level hub record shaped like a real getDeviceByHid entry."""
    record = {
        "hid": hid,
        "mid": mid,
        "brand": "RainPoint",
        "name": HUB_LABEL,
        "model": "HWG023WBRF-V2",
        "modelCode": "34",
        "softVer": "1.2.3",
        "hardwareVersion": "1.0",
        "mac": HUB_MAC,
        "deviceName": "MAC-A84674BB91F0",
        "productKey": PRODUCT_KEY,
        "iotId": IOT_ID,
        "homeName": HOME_NAME,
        "param": "0|1||",
        "state": "1,-52",
        "subDevices": [
            {
                "addr": 1,
                "mid": mid,
                "sid": 341550,
                "model": "HTV245FRF",
                "name": SUB_LABEL,
                "mac": "A4:C1:38:FF:88:E7",
                "softVer": "2.0.1",
                "param": "5=02,11=58020a001e000000000000000000",
            }
        ],
    }
    if extra:
        record.update(extra)
    return record


def _sensor_entry(hid=182509, mid=236547, addr=1):
    """Return a coordinator sensors entry carrying a decoded payload."""
    return {
        "hid": hid,
        "mid": mid,
        "addr": addr,
        "home_name": HOME_NAME,
        "hub_name": HUB_LABEL,
        "sub_name": SUB_LABEL,
        "model": "HTV245FRF",
        "model_code": "303",
        "firmware_version": "2.0.1",
        "device_name": "MAC-A84674BB91F0",
        "product_key": PRODUCT_KEY,
        "hub_paired": True,
        "raw_status": {"id": "D01", "value": "11#0100...", "time": 1785420002247},
        "data": {"type": "valve", "zones": {1: {"open": True, "duration_seconds": 120}}},
    }


def _make_hass(coordinator=None, mqtt_client=None, entry_id="entry-1"):
    """Return (hass, entry) wired the way async_setup_entry leaves them."""
    entry = MagicMock()
    entry.entry_id = entry_id
    entry.data = {
        "email": ACCOUNT_EMAIL,
        "password": SECRET_PASSWORD,
        "token": SECRET_TOKEN,
        "refresh_token": SECRET_TOKEN + "-r",
        "area_code": "1",
        "hids": [182509],
    }
    entry.options = {"push_enabled": True, "generic_entities_enabled": False}

    store = {}
    if coordinator is not None:
        store["coordinator"] = coordinator
    if mqtt_client is not None:
        store["mqtt_client"] = mqtt_client

    hass = MagicMock()
    hass.data = {DOMAIN: {entry_id: store}}
    return hass, entry


def _make_coordinator(hubs=None, sensors=None, connectivity=None):
    """Return a coordinator stand-in holding one poll's data."""
    coordinator = MagicMock()
    coordinator.data = {
        "hubs": hubs if hubs is not None else [_hub_record()],
        "sensors": sensors if sensors is not None else {"182509_236547_1": _sensor_entry()},
        "status": {},
        "hub_connectivity": connectivity if connectivity is not None else {236547: {"state": "connected", "changed_at": None}},
    }
    coordinator.last_update_success = True
    coordinator.update_interval = None
    return coordinator


def _walk_values(payload):
    """Yield every scalar value reachable in a nested dict/list structure."""
    if isinstance(payload, dict):
        for key, value in payload.items():
            yield key
            yield from _walk_values(value)
    elif isinstance(payload, list):
        for item in payload:
            yield from _walk_values(item)
    else:
        yield payload


def _device(identifier, name="HTV245FRF", name_by_user=None, row_id="device-row-1"):
    """Return a device registry row carrying one DOMAIN identifier.

    `name_by_user` defaults to None, which is the registry's own default for a
    device nobody has renamed, so a test opts in to the renamed case rather than
    inheriting it. `row_id` sets `.id`, the only stable handle a row carrying no
    DOMAIN identifier has, and is defaulted so existing call sites are untouched.
    """
    device = MagicMock()
    device.identifiers = {(DOMAIN, identifier)}
    device.name = name
    device.name_by_user = name_by_user
    device.id = row_id
    return device


@pytest.fixture(autouse=True)
def _registry_rows(monkeypatch):
    """Patch the device registry accessors `diagnostics.py` calls, file-wide.

    Yields the mutable list `dr.async_entries_for_config_entry` will return a
    copy of, so a test appends `_device(...)` rows to make itself visible to
    the registry walk. Default is an empty list, so all existing `_make_hass()`
    call sites, none of which know this fixture exists, keep passing unchanged
    and no test elsewhere gains a stub of its own.

    A fixture rather than a mutation inside `_make_hass()`: the device registry
    module is a shared conftest stub registered once at import time, so
    patching its two accessors here, through `monkeypatch`, restores them after
    each test instead of leaking a return value into another test in this file
    or into another test module that imports the same stub.
    """
    rows: list = []
    monkeypatch.setattr("custom_components.rainpoint.diagnostics.dr.async_get", lambda hass: object())
    monkeypatch.setattr(
        "custom_components.rainpoint.diagnostics.dr.async_entries_for_config_entry",
        lambda registry, entry_id: list(rows),
    )
    return rows


class TestAllowList:
    """The builders carry named fields and nothing else."""

    def test_a_field_outside_the_allow_list_contributes_only_its_name(self):
        """This is the property the whole module is built around."""
        record = {"model": "HTV245FRF", "somethingVendorAddedLater": "a secret-ish value"}

        selected = _select_allowed(record, frozenset({"model"}))

        assert selected["model"] == "HTV245FRF"
        assert selected["unlisted_keys"] == ["somethingVendorAddedLater"]
        assert "a secret-ish value" not in list(_walk_values(selected))

    def test_no_unlisted_keys_entry_when_every_field_is_named(self):
        """The list is a signal, so it must not appear when there is nothing to signal."""
        selected = _select_allowed({"model": "X"}, frozenset({"model", "mid"}))

        assert selected == {"model": "X"}

    def test_a_record_that_is_not_a_dict_is_typed_rather_than_raising(self):
        """Diagnostics is reached when something is already wrong; it must not add to it."""
        assert _select_allowed(["not", "a", "record"], frozenset({"model"})) == {"unexpected_type": "list"}


class TestUnlistedKeys:
    """A new vendor field surfaces as a question, through the real dump path."""

    @pytest.mark.asyncio
    async def test_a_new_hub_field_is_named_and_its_value_is_absent(self):
        """Asserted as the whole list, not as membership.

        Membership was what let `subDevices` sit in here unnoticed through a
        real dump: the field the test added was present, so the assertion
        passed, and the permanent false entry beside it was invisible.
        """
        hubs = [_hub_record(extra={"newVendorField": "unreviewed-payload-value"})]
        hass, entry = _make_hass(coordinator=_make_coordinator(hubs=hubs))

        result = await async_get_config_entry_diagnostics(hass, entry)

        assert result["hubs"][0]["unlisted_keys"] == ["newVendorField"]
        assert "unreviewed-payload-value" not in list(_walk_values(result))

    @pytest.mark.asyncio
    async def test_a_hub_carrying_only_known_fields_reports_nothing_unlisted(self):
        """The signal is worthless if every dump raises it.

        `subDevices` is deliberately outside the hub allow-list because
        `_hub_dump` walks each child through its own; that omission must not
        read as a field nobody reviewed.
        """
        hass, entry = _make_hass(coordinator=_make_coordinator())

        result = await async_get_config_entry_diagnostics(hass, entry)

        assert "unlisted_keys" not in result["hubs"][0]
        assert len(result["hubs"][0]["subDevices"]) == 1

    @pytest.mark.asyncio
    async def test_a_new_status_entry_field_is_named_and_its_value_is_absent(self):
        """The nested cloud mapping needs its own pass, not its container's.

        `raw_status` is held whole by the sensor entry, so allow-listing the
        entry says nothing about what is inside it. Without a second pass a
        field the vendor adds to a status entry ships its value on the first
        poll after they add it.
        """
        entry = _sensor_entry()
        entry["raw_status"]["newStatusField"] = "unreviewed-status-value"
        hass, hass_entry = _make_hass(coordinator=_make_coordinator(sensors={"182509_236547_1": entry}))

        result = await async_get_config_entry_diagnostics(hass, hass_entry)

        raw_status = result["sensors"]["182509_236547_1"]["raw_status"]
        assert raw_status["unlisted_keys"] == ["newStatusField"]
        assert raw_status["value"] == "11#0100..."
        assert "unreviewed-status-value" not in list(_walk_values(result))

    @pytest.mark.asyncio
    async def test_an_option_no_longer_written_is_named_and_its_value_is_absent(self):
        """`entry.options` holds whatever any past version persisted."""
        hass, entry = _make_hass(coordinator=_make_coordinator())
        entry.options = {"push_enabled": True, "debug_last_submission": "some-stale-value"}

        result = await async_get_config_entry_diagnostics(hass, entry)

        assert result["entry"]["options"]["push_enabled"] is True
        assert result["entry"]["options"]["unlisted_keys"] == ["debug_last_submission"]
        assert "some-stale-value" not in list(_walk_values(result))

    @pytest.mark.asyncio
    async def test_a_new_sub_device_field_is_named_and_its_value_is_absent(self):
        """The sub-device walk has its own allow-list, so it needs its own proof."""
        hub = _hub_record()
        hub["subDevices"][0]["newSubField"] = "unreviewed-sub-value"
        hass, entry = _make_hass(coordinator=_make_coordinator(hubs=[hub]))

        result = await async_get_config_entry_diagnostics(hass, entry)

        assert "newSubField" in result["hubs"][0]["subDevices"][0]["unlisted_keys"]
        assert "unreviewed-sub-value" not in list(_walk_values(result))


class TestNothingSensitiveSurvives:
    """No credential and no account identity survives, asserted by value."""

    @pytest.mark.asyncio
    async def test_no_credential_reaches_the_config_entry_dump(self):
        hass, entry = _make_hass(coordinator=_make_coordinator())

        values = list(_walk_values(await async_get_config_entry_diagnostics(hass, entry)))

        assert SECRET_PASSWORD not in values
        assert SECRET_TOKEN not in values
        assert SECRET_TOKEN + "-r" not in values

    @pytest.mark.asyncio
    async def test_no_account_identity_reaches_the_config_entry_dump(self):
        """The email is the single most harmful field here, per the logging review.

        The MAC, productKey and iotId are the identity half of the same rule:
        they address a device rather than describe it. What a user *named* their
        hardware is a separate question, settled the other way and pinned by
        `TestUserChosenNamesSurvive`.
        """
        hass, entry = _make_hass(coordinator=_make_coordinator())

        values = list(_walk_values(await async_get_config_entry_diagnostics(hass, entry)))

        assert ACCOUNT_EMAIL not in values
        assert HUB_MAC not in values
        assert PRODUCT_KEY not in values
        assert IOT_ID not in values

    @pytest.mark.asyncio
    async def test_entry_data_contributes_key_names_and_no_values(self):
        """Stronger than redaction: the values are never read into the structure."""
        hass, entry = _make_hass(coordinator=_make_coordinator())

        result = await async_get_config_entry_diagnostics(hass, entry)

        assert result["entry"]["data_keys"] == [
            "area_code",
            "email",
            "hids",
            "password",
            "refresh_token",
            "token",
        ]
        assert result["entry"]["home_count"] == 1
        assert "data" not in result["entry"]

    @pytest.mark.asyncio
    async def test_no_credential_reaches_a_device_dump(self):
        """The device path builds its own payload, so it needs its own proof."""
        hass, entry = _make_hass(coordinator=_make_coordinator())

        values = list(_walk_values(await async_get_device_diagnostics(hass, entry, _device("182509_236547_1"))))

        assert SECRET_PASSWORD not in values
        assert ACCOUNT_EMAIL not in values
        assert HUB_MAC not in values


class TestSupportPayload:
    """The dump answers a decode question without a follow-up request."""

    @pytest.mark.asyncio
    async def test_the_raw_payload_and_the_decode_of_it_both_survive(self):
        hass, entry = _make_hass(coordinator=_make_coordinator())

        result = await async_get_config_entry_diagnostics(hass, entry)

        sensor = result["sensors"]["182509_236547_1"]
        assert sensor["raw_status"]["value"] == "11#0100..."
        assert sensor["data"]["zones"][1]["duration_seconds"] == 120
        assert sensor["model"] == "HTV245FRF"

    @pytest.mark.asyncio
    async def test_the_hub_param_blob_survives(self):
        """The field the throwaway probe script was written to read."""
        hass, entry = _make_hass(coordinator=_make_coordinator())

        result = await async_get_config_entry_diagnostics(hass, entry)

        assert result["hubs"][0]["param"] == "0|1||"

    @pytest.mark.asyncio
    async def test_the_integration_version_is_stamped(self):
        """A support dump is worthless without knowing which build produced it."""
        hass, entry = _make_hass(coordinator=_make_coordinator())

        result = await async_get_config_entry_diagnostics(hass, entry)

        assert result["integration"] == {"domain": DOMAIN, "version": VERSION}


class TestPushSection:
    """The push channel reports liveness and carries no session credential."""

    @pytest.mark.asyncio
    async def test_absent_client_reports_that_and_nothing_else(self):
        hass, entry = _make_hass(coordinator=_make_coordinator())

        result = await async_get_config_entry_diagnostics(hass, entry)

        assert result["push"] == {"client_built": False}

    @pytest.mark.asyncio
    async def test_present_client_reports_the_four_liveness_fields(self):
        mqtt_client = MagicMock()
        mqtt_client.connected = True
        mqtt_client.message_count = 17
        mqtt_client.last_message_at = 1785420002.5
        mqtt_client.hub_mid = 236547
        mqtt_client.device_secret = "SECRET-SHOULD-NEVER-APPEAR"
        hass, entry = _make_hass(coordinator=_make_coordinator(), mqtt_client=mqtt_client)

        result = await async_get_config_entry_diagnostics(hass, entry)

        assert result["push"] == {
            "client_built": True,
            "connected": True,
            "message_count": 17,
            "last_message_at": 1785420002.5,
            "hub_mid": 236547,
        }
        assert "SECRET-SHOULD-NEVER-APPEAR" not in list(_walk_values(result))


class TestDeviceRouting:
    """The DOMAIN identifier decides which of the three dumps a device page gets."""

    @pytest.mark.asyncio
    async def test_a_sub_device_row_yields_only_its_own_sensor_entry(self):
        sensors = {
            "182509_236547_1": _sensor_entry(addr=1),
            "182509_236547_3": _sensor_entry(addr=3),
        }
        hass, entry = _make_hass(coordinator=_make_coordinator(sensors=sensors))

        result = await async_get_device_diagnostics(hass, entry, _device("182509_236547_3"))

        assert result["device"]["kind"] == "sub_device"
        assert list(result["sensors"]) == ["182509_236547_3"]
        assert result["sensors"]["182509_236547_3"]["addr"] == 3

    @pytest.mark.asyncio
    async def test_a_hub_row_yields_its_record_its_connectivity_and_its_children(self):
        other_hub = _hub_record(mid=999999)
        sensors = {
            "182509_236547_1": _sensor_entry(mid=236547),
            "182509_999999_1": _sensor_entry(mid=999999),
        }
        connectivity = {236547: {"state": "connected"}, 999999: {"state": "disconnected"}}
        coordinator = _make_coordinator(hubs=[_hub_record(), other_hub], sensors=sensors, connectivity=connectivity)
        hass, entry = _make_hass(coordinator=coordinator)

        result = await async_get_device_diagnostics(hass, entry, _device("hub_182509_236547"))

        assert result["device"]["kind"] == "hub"
        assert [hub["mid"] for hub in result["hubs"]] == [236547]
        assert list(result["hub_connectivity"]) == [236547]
        assert list(result["sensors"]) == ["182509_236547_1"]

    @pytest.mark.asyncio
    async def test_a_hub_row_still_on_the_older_hid_only_identity_still_resolves(self):
        """A row the identity migration could not finish keeps the `hub_{hid}` shape.

        Matching the identifier as a string would hand that row an empty dump on
        the one device page most likely to be opened when something is wrong
        with it, so the identity helper's both-shapes reading is what routes it.
        """
        coordinator = _make_coordinator()
        hass, entry = _make_hass(coordinator=coordinator)

        result = await async_get_device_diagnostics(hass, entry, _device("hub_182509"))

        assert result["device"]["kind"] == "hub"
        assert [hub["mid"] for hub in result["hubs"]] == [236547]
        assert list(result["hub_connectivity"]) == [236547]
        assert list(result["sensors"]) == ["182509_236547_1"]

    @pytest.mark.asyncio
    async def test_a_hid_only_identity_in_a_two_hub_home_returns_both_rather_than_neither(self):
        """The identifier is genuinely ambiguous there, and both beats an empty dump."""
        hubs = [_hub_record(), _hub_record(mid=999999)]
        sensors = {
            "182509_236547_1": _sensor_entry(mid=236547),
            "182509_999999_1": _sensor_entry(mid=999999),
        }
        coordinator = _make_coordinator(hubs=hubs, sensors=sensors)
        hass, entry = _make_hass(coordinator=coordinator)

        result = await async_get_device_diagnostics(hass, entry, _device("hub_182509"))

        assert sorted(hub["mid"] for hub in result["hubs"]) == [236547, 999999]
        assert sorted(result["sensors"]) == ["182509_236547_1", "182509_999999_1"]

    @pytest.mark.asyncio
    async def test_a_hub_prefixed_identifier_of_neither_shape_yields_empty_sections(self):
        """`_hub_identity` returns None for it, and an empty dump is then correct."""
        hass, entry = _make_hass(coordinator=_make_coordinator())

        result = await async_get_device_diagnostics(hass, entry, _device("hub_a_b_c"))

        assert result["device"]["kind"] == "hub"
        assert result["hubs"] == []
        assert result["hub_connectivity"] == {}
        assert result["sensors"] == {}

    @pytest.mark.asyncio
    async def test_a_hub_row_from_another_home_is_not_matched_by_mid_alone(self):
        """hid is checked first, so two homes reusing a mid do not cross over."""
        hubs = [_hub_record(hid=182710, mid=236547)]
        sensors = {"182710_236547_1": _sensor_entry(hid=182710)}
        coordinator = _make_coordinator(hubs=hubs, sensors=sensors)
        hass, entry = _make_hass(coordinator=coordinator)

        result = await async_get_device_diagnostics(hass, entry, _device("hub_182509_236547"))

        assert result["hubs"] == []
        assert result["sensors"] == {}

    @pytest.mark.asyncio
    async def test_a_row_with_no_domain_identifier_is_named_rather_than_returning_an_empty_dump(self):
        """An empty dump would read as 'nothing wrong here', which is a different claim."""
        device = MagicMock()
        device.identifiers = {("other_integration", "whatever")}
        device.name = "Something Else"
        device.name_by_user = None
        hass, entry = _make_hass(coordinator=_make_coordinator())

        result = await async_get_device_diagnostics(hass, entry, device)

        assert result["device"] == {
            "identifier": None,
            "kind": "unrecognised",
            "name": "Something Else",
            "name_by_user": None,
        }
        assert "sensors" not in result

    @pytest.mark.asyncio
    async def test_a_sub_device_key_absent_from_this_poll_yields_an_empty_sensor_map(self):
        """A silent device still has a device page, and downloading from it must not raise."""
        hass, entry = _make_hass(coordinator=_make_coordinator())

        result = await async_get_device_diagnostics(hass, entry, _device("182509_236547_99"))

        assert result["sensors"] == {}


class TestBeforeSetupCompletes:
    """A dump taken while the entry is half up must not raise."""

    @pytest.mark.asyncio
    async def test_no_coordinator_reports_that_rather_than_raising(self):
        hass, entry = _make_hass(coordinator=None)

        result = await async_get_config_entry_diagnostics(hass, entry)

        assert result["coordinator"] == {"set_up": False}
        assert result["hubs"] == []
        assert result["sensors"] == {}

    @pytest.mark.asyncio
    async def test_a_coordinator_holding_no_data_yields_empty_sections(self):
        """`coordinator.data` is None before the first refresh returns."""
        coordinator = MagicMock()
        coordinator.data = None
        coordinator.last_update_success = False
        coordinator.update_interval = None
        hass, entry = _make_hass(coordinator=coordinator)

        result = await async_get_config_entry_diagnostics(hass, entry)

        assert result["coordinator"]["hub_count"] == 0
        assert result["coordinator"]["last_update_success"] is False
        assert result["sensors"] == {}

    @pytest.mark.asyncio
    async def test_the_domain_store_missing_entirely_does_not_raise(self):
        entry = MagicMock()
        entry.entry_id = "entry-1"
        entry.data = {}
        entry.options = {}
        hass = MagicMock()
        hass.data = {}

        result = await async_get_config_entry_diagnostics(hass, entry)

        assert result["coordinator"] == {"set_up": False}
        assert result["entry"]["home_count"] == 0

    @pytest.mark.asyncio
    async def test_a_hub_whose_sub_devices_are_not_a_list_is_typed_rather_than_raising(self):
        hubs = [_hub_record(extra={"subDevices": "unexpected"})]
        hass, entry = _make_hass(coordinator=_make_coordinator(hubs=hubs))

        result = await async_get_config_entry_diagnostics(hass, entry)

        assert result["hubs"][0]["subDevices"] == {"unexpected_type": "str"}

    @pytest.mark.asyncio
    async def test_a_sensor_entry_carrying_no_status_is_dumped_without_the_nested_pass(self):
        """The nested pass is conditional, so an entry without one must still dump."""
        entry = _sensor_entry()
        del entry["raw_status"]
        hass, hass_entry = _make_hass(coordinator=_make_coordinator(sensors={"182509_236547_1": entry}))

        result = await async_get_config_entry_diagnostics(hass, hass_entry)

        dumped = result["sensors"]["182509_236547_1"]
        assert "raw_status" not in dumped
        assert dumped["model"] == "HTV245FRF"

    @pytest.mark.asyncio
    async def test_a_hub_record_that_is_not_a_dict_is_typed_rather_than_raising(self):
        hass, entry = _make_hass(coordinator=_make_coordinator(hubs=["not-a-record"]))

        result = await async_get_config_entry_diagnostics(hass, entry)

        assert result["hubs"][0] == {"unexpected_type": "str"}

    @pytest.mark.asyncio
    async def test_update_interval_is_reported_in_seconds_when_one_is_set(self):
        coordinator = _make_coordinator()
        coordinator.update_interval = MagicMock()
        coordinator.update_interval.total_seconds.return_value = 120.0
        hass, entry = _make_hass(coordinator=coordinator)

        result = await async_get_config_entry_diagnostics(hass, entry)

        assert result["coordinator"]["update_interval_seconds"] == 120.0


class TestRedactionKeySet:
    """The key set itself, pinned so a rename on either side is visible."""

    def test_both_spellings_of_every_dual_spelled_field_are_covered(self):
        """A coordinator entry says device_name where a cloud record says deviceName."""
        for camel, snake in (
            ("deviceName", "device_name"),
            ("productKey", "product_key"),
            ("deviceSecret", "device_secret"),
        ):
            assert camel in TO_REDACT
            assert snake in TO_REDACT

    def test_no_user_chosen_name_key_is_in_the_redaction_set(self):
        """Adding one back is a policy change, so it has to fail here first.

        `deviceName` is the MAC-derived cloud identifier and stays redacted; the
        near-miss with `name` is exactly why this is pinned by key rather than
        left to a reader to infer from the set.
        """
        for key in ("name", "homeName", "home_name", "hub_name", "sub_name"):
            assert key not in TO_REDACT


class TestUserChosenNamesSurvive:
    """The names a user chose reach the dump, and that is the point of it.

    The mirror of `TestNothingSensitiveSurvives`: same walk, opposite verdict.
    Both halves are asserted from one dump in each test, because the property
    that matters is not "labels survive" or "identifiers do not" on their own,
    it is that the two are separated within the same payload.
    """

    @pytest.mark.asyncio
    async def test_the_config_entry_dump_keeps_every_name_a_user_reads(self):
        hass, entry = _make_hass(coordinator=_make_coordinator())

        values = list(_walk_values(await async_get_config_entry_diagnostics(hass, entry)))

        assert HUB_LABEL in values
        assert SUB_LABEL in values
        assert HOME_NAME in values
        assert HUB_MAC not in values

    @pytest.mark.asyncio
    async def test_a_device_dump_keeps_them_too(self):
        """The device path builds its own payload, so it needs its own proof."""
        hass, entry = _make_hass(coordinator=_make_coordinator())

        values = list(_walk_values(await async_get_device_diagnostics(hass, entry, _device("182509_236547_1"))))

        assert SUB_LABEL in values
        assert HUB_LABEL in values
        assert HUB_MAC not in values

    @pytest.mark.asyncio
    async def test_the_label_and_the_identifier_are_told_apart_on_one_record(self):
        """`name` and `deviceName` sit side by side on a hub record.

        Separating them is the whole decision: one is what the owner typed and
        the other is derived from the MAC, and a redactor matching key names has
        no way to know that. If a later edit collapses the two, this is where it
        shows up.
        """
        hass, entry = _make_hass(coordinator=_make_coordinator())

        hub = (await async_get_config_entry_diagnostics(hass, entry))["hubs"][0]

        assert hub["name"] == HUB_LABEL
        assert hub["deviceName"] == REDACTED
        assert hub["subDevices"][0]["name"] == SUB_LABEL


class TestTheDeviceIsNamedTheWayItsOwnerNamesIt:
    """The registry names, which are the dump's only tie to what is on screen.

    Measured on the maintainer's hardware at 1.15.0rc2: every cloud `name` in a
    real dump read as the model string ("HTV245FRF", "Hub"), so before this the
    only way to tell which device a record described was to already know the
    `{hid}_{mid}_{addr}` key.
    """

    @pytest.mark.asyncio
    async def test_an_unrenamed_device_carries_its_integration_name_and_a_null_override(self):
        hass, entry = _make_hass(coordinator=_make_coordinator())

        result = await async_get_device_diagnostics(hass, entry, _device("182509_236547_1"))

        assert result["device"]["name"] == "HTV245FRF"
        assert result["device"]["name_by_user"] is None

    @pytest.mark.asyncio
    async def test_a_renamed_device_carries_both_names(self):
        """Both, not the resolved one.

        Which of the two Home Assistant is showing is the question a rename
        raises, and a dump carrying only the winner cannot answer it.
        """
        hass, entry = _make_hass(coordinator=_make_coordinator())
        device = _device("182509_236547_1", name="HTV245FRF", name_by_user="Front Lawn Valve")

        result = await async_get_device_diagnostics(hass, entry, device)

        assert result["device"]["name"] == "HTV245FRF"
        assert result["device"]["name_by_user"] == "Front Lawn Valve"

    @pytest.mark.asyncio
    async def test_a_registry_name_is_not_redacted(self):
        """It is a user-chosen label, so it follows the same rule as the rest."""
        hass, entry = _make_hass(coordinator=_make_coordinator())
        device = _device("182509_236547_1", name_by_user=SUB_LABEL)

        result = await async_get_device_diagnostics(hass, entry, device)

        assert result["device"]["name_by_user"] == SUB_LABEL
        assert SUB_LABEL in list(_walk_values(result))


class TestDeviceIdentityMap:
    """The entry dump's `devices` map ties a registry row to what its owner sees.

    Every assertion drives the real `async_get_config_entry_diagnostics`
    coroutine and reads its returned payload, never the map builder in
    isolation, so a wiring mistake between the builder and the payload
    literal cannot hide behind a passing unit-level test.
    """

    @pytest.mark.asyncio
    async def test_a_sub_device_row_in_the_current_poll_reads_true(self, _registry_rows):
        _registry_rows.append(_device("182509_236547_1", name="HTV245FRF", name_by_user="Front Lawn Valve"))
        hass, entry = _make_hass(coordinator=_make_coordinator())

        result = await async_get_config_entry_diagnostics(hass, entry)

        assert result["devices"]["182509_236547_1"] == {
            "kind": "sub_device",
            "name": "HTV245FRF",
            "name_by_user": "Front Lawn Valve",
            "in_current_poll": True,
        }

    @pytest.mark.asyncio
    async def test_a_sub_device_row_absent_from_the_poll_reads_false_and_still_appears(self, _registry_rows):
        _registry_rows.append(_device("182509_236547_1"))
        hass, entry = _make_hass(coordinator=_make_coordinator(sensors={}))

        result = await async_get_config_entry_diagnostics(hass, entry)

        assert "182509_236547_1" in result["devices"]
        assert result["devices"]["182509_236547_1"]["in_current_poll"] is False

    @pytest.mark.asyncio
    async def test_a_raising_registry_read_degrades_to_an_empty_map_not_an_exception(self, monkeypatch):
        """The coroutine still returns a full payload; it never propagates the error."""
        monkeypatch.setattr(
            "custom_components.rainpoint.diagnostics.dr.async_entries_for_config_entry",
            MagicMock(side_effect=RuntimeError("boom")),
        )
        hass, entry = _make_hass(coordinator=_make_coordinator())

        result = await async_get_config_entry_diagnostics(hass, entry)

        assert result["devices"] == {}
        assert result["hubs"]

    @pytest.mark.asyncio
    async def test_the_three_existing_sections_are_unchanged_by_the_new_key(self, _registry_rows):
        _registry_rows.append(_device("182509_236547_1"))
        hass, entry = _make_hass(coordinator=_make_coordinator())

        result = await async_get_config_entry_diagnostics(hass, entry)

        assert result["hubs"][0]["name"] == HUB_LABEL
        assert result["sensors"]["182509_236547_1"]["model"] == "HTV245FRF"
        assert result["hub_connectivity"] == {236547: {"state": "connected", "changed_at": None}}

    @pytest.mark.asyncio
    async def test_a_migrated_hub_row_matching_the_poll_reads_hub_and_true(self, _registry_rows):
        _registry_rows.append(_device("hub_182509_236547"))
        hass, entry = _make_hass(coordinator=_make_coordinator())

        result = await async_get_config_entry_diagnostics(hass, entry)

        assert result["devices"]["hub_182509_236547"]["kind"] == "hub"
        assert result["devices"]["hub_182509_236547"]["in_current_poll"] is True

    @pytest.mark.asyncio
    async def test_a_migrated_hub_row_against_a_different_mid_reads_false(self, _registry_rows):
        _registry_rows.append(_device("hub_182509_236547"))
        coordinator = _make_coordinator(hubs=[_hub_record(mid=999999)], sensors={"182509_999999_1": _sensor_entry(mid=999999)})
        hass, entry = _make_hass(coordinator=coordinator)

        result = await async_get_config_entry_diagnostics(hass, entry)

        assert result["devices"]["hub_182509_236547"]["in_current_poll"] is False

    @pytest.mark.asyncio
    async def test_a_legacy_hid_only_row_matches_any_hub_in_that_home(self, _registry_rows):
        _registry_rows.append(_device("hub_182509"))
        hubs = [_hub_record(mid=236547), _hub_record(mid=999999)]
        sensors = {"182509_236547_1": _sensor_entry(mid=236547), "182509_999999_1": _sensor_entry(mid=999999)}
        hass, entry = _make_hass(coordinator=_make_coordinator(hubs=hubs, sensors=sensors))

        result = await async_get_config_entry_diagnostics(hass, entry)

        assert result["devices"]["hub_182509"]["in_current_poll"] is True

    @pytest.mark.asyncio
    async def test_a_legacy_and_a_migrated_row_for_the_same_home_stay_two_distinct_entries(self, _registry_rows):
        _registry_rows.append(_device("hub_182509"))
        _registry_rows.append(_device("hub_182509_236547"))
        hass, entry = _make_hass(coordinator=_make_coordinator())

        result = await async_get_config_entry_diagnostics(hass, entry)

        assert "hub_182509" in result["devices"]
        assert "hub_182509_236547" in result["devices"]
        assert result["devices"]["hub_182509"] is not result["devices"]["hub_182509_236547"]

    @pytest.mark.asyncio
    async def test_a_hub_prefixed_identifier_of_neither_shape_reads_hub_and_false(self, _registry_rows):
        _registry_rows.append(_device("hub_a_b_c"))
        hass, entry = _make_hass(coordinator=_make_coordinator())

        result = await async_get_config_entry_diagnostics(hass, entry)

        assert result["devices"]["hub_a_b_c"]["kind"] == "hub"
        assert result["devices"]["hub_a_b_c"]["in_current_poll"] is False

    @pytest.mark.asyncio
    async def test_a_row_with_no_domain_identifier_gets_an_unrecognised_entry(self, _registry_rows):
        device = MagicMock()
        device.identifiers = {("other_integration", "whatever")}
        device.name = "Something Else"
        device.name_by_user = None
        device.id = "device-row-9"
        _registry_rows.append(device)
        hass, entry = _make_hass(coordinator=_make_coordinator())

        result = await async_get_config_entry_diagnostics(hass, entry)

        assert result["devices"]["unrecognised_device-row-9"] == {
            "kind": "unrecognised",
            "name": "Something Else",
            "name_by_user": None,
            "in_current_poll": False,
        }
