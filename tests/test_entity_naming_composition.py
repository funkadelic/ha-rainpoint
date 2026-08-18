"""Proves the entity-naming composition rule against a device the user has renamed.

Every proof here depends on `entity_registry` and `device_registry` resolving to
the real Home Assistant classes rather than this repo's own MagicMock stubs. The
repository conftest installs package-wide stubs and only skips these two because
the pytest plugin imports them first; if that ordering ever changed, every
assertion below would pass against a mock instead of against Home Assistant's own
name composition. The module-level guard makes that fail loudly instead of
silently, mirroring tests/test_migration.py's own guard.

The fixture built here is deliberately a device whose display name diverges from
the sub-device name the entity was constructed from. A fixture where the two
match would pass both before and after any fix, which is the same class of
mistake this project's testing rules record as "a silent device is not a
reporting one": substituting the easy subject for the real one removes the
failure mode being tested. A device rename is what breaks the device page's
exact-prefix strip, and that divergence is the only fixture shape that can prove
it is fixed.
"""

from __future__ import annotations

import inspect
from unittest.mock import MagicMock

import pytest
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.rainpoint.const import DOMAIN, HUB_IDENTIFIER_PREFIX
from custom_components.rainpoint.diagnostic_sensors import RainPointBatterySensor
from custom_components.rainpoint.generic_control import RUN_STATE_IDENTITY, build_generic_switch_entities
from custom_components.rainpoint.hub_entities import (
    RainPointHubBroadcastButton,
    RainPointHubBroadcastSwitch,
    RainPointHubChannelSelect,
    RainPointHubConnectivityBinarySensor,
    RainPointHubDeviceIDSensor,
    RainPointHubRSSISensor,
    RainPointPushConnectedBinarySensor,
)
from custom_components.rainpoint.number import RainPointZoneDurationNumber
from custom_components.rainpoint.select import RainPointSubDevicePowerSelect
from custom_components.rainpoint.sensor import RainPointMoisturePercentSensor, RainPointZoneStateSensor
from custom_components.rainpoint.valve import RainPointValveEntity
from tests.helpers import make_sensor_entry

# Home Assistant 2026.3 added two required keyword-only arguments to this private
# helper, area_id and parts, alongside an optional use_legacy_naming. This repo
# supports HA back to 2026.2, whose signature predates all three, so they are
# supplied only when the installed version accepts them.
#
# 2026.2 is also the release that introduced the helper at all, which is what
# sets the supported floor: there is nothing to assert against on an older one.
#
# The values mirror what HA's own async_get_full_entity_name wrapper passes, so
# what is asserted here stays the composition users actually get. area_id is None
# and parts omits AREA and FLOOR, which keeps the result device name plus entity
# name as before. use_legacy_naming matters only where a name is passed: without
# it a user override would be composed against the device name instead of
# standing alone.
_FULL_NAME_EXTRA_KWARGS: dict[str, object] = {}
if "parts" in inspect.signature(er._async_get_full_entity_name).parameters:
    _FULL_NAME_EXTRA_KWARGS = {
        "area_id": None,
        "parts": (er.EntityNamePart.DEVICE, er.EntityNamePart.ENTITY),
        "use_legacy_naming": True,
    }

# The one real CTL_SOCK candidate in the committed catalog with no
# hand-written decoder, reused from tests/test_generic_control.py's own
# anchor so this module's generic-control fixture rests on the same ground
# truth rather than a synthetic one.
SOCKET_MODEL = "HWG004WRF"
SOCKET_MODEL_CODE = 34

assert not isinstance(er.async_get, MagicMock), "entity_registry is stubbed; every proof here would be a no-op"
assert not isinstance(dr.async_get, MagicMock), "device_registry is stubbed; every proof here would be a no-op"

HID = 100
MID = 200
ADDR = 1
HUB_HID = 100
HUB_MID = 900


def _make_entry(hass):
    """Create and register a minimal RainPoint config entry that the fixture
    device rows in this module attach to."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"area_code": "1", "email": "a@b.c", "password": "pw", "hids": [HID], "token": "tok"},
        options={},
    )
    entry.add_to_hass(hass)
    return entry


def _make_renamed_device(hass, device_registry, entry, *, sub_name, display_name):
    """Build a sub-device registry row, then rename it the way a real user does.

    Created first with ``sub_name``, the name the integration itself supplies,
    then updated with ``name_by_user``: that is the field a real Home Assistant
    rename writes, and the field ``_async_get_full_entity_name`` prefers over
    the plain ``name``. Exercising this field rather than ``name`` directly is
    what proves composition against a user-owned field the code never writes,
    and it is the shape a renamed device actually has in a live registry.

    Mirrors ``_make_renamed_hub_device``. Setting ``name`` to the display name
    instead would still compose correctly, because Home Assistant falls back to
    ``name`` when ``name_by_user`` is unset, so the assertions would pass while
    never touching the user-rename path that motivated this whole module.
    """
    device = device_registry.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, f"{HID}_{MID}_{ADDR}")},
        name=sub_name,
    )
    return device_registry.async_update_device(device.id, name_by_user=display_name)


def _compose(hass, entity, device):
    """Wrap entity_registry._async_get_full_entity_name, reading only the code's contribution.

    ``name=None`` is passed deliberately: that argument is the user's own
    registry override, and passing it None is what makes the composed string
    reflect only what the code supplies (``original_name`` and
    ``has_entity_name``), not a user's own override.

    The flag is read through ``entity.has_entity_name``, the property Home
    Assistant itself consults, rather than through the ``_attr_`` backing
    attribute, so a class that satisfied one and not the other would not
    compose correctly here.

    ``_async_get_full_entity_name`` is private to Home Assistant and carries
    no compatibility guarantee, but it is the function that does this
    composition and there is no public equivalent to assert against. If a
    future release moves or renames it, this module breaks at the call rather
    than silently proving nothing; re-point this one wrapper at whatever
    replaced it rather than weakening the assertions to a string built here.
    """
    return er._async_get_full_entity_name(
        hass,
        device_id=device.id,
        fallback="unnamed",
        has_entity_name=entity.has_entity_name,
        name=None,
        original_name=entity._attr_name,
        **_FULL_NAME_EXTRA_KWARGS,
    )


def _sensor_info(sub_name):
    """Build a sub-device sensor_info dict for the shared HID/MID/ADDR
    identity, model fixed to HTV210B. sub_name is left to the caller so the
    device registry's display name can be made to diverge from it."""
    return {
        "hid": HID,
        "mid": MID,
        "addr": ADDR,
        "sub_name": sub_name,
        "model": "HTV210B",
    }


def _mock_coordinator():
    """A bare coordinator stand-in with an empty sensors map, enough for
    entity constructors that never read past their own sensor_info
    argument."""
    coordinator = MagicMock()
    coordinator.data = {"sensors": {}}
    return coordinator


def _hub_info(name=None):
    """Build a hub_info dict for the shared HUB_HID/HUB_MID identity.

    The name key is omitted entirely rather than set to None when name is
    None, so the unnamed-hub tests exercise a record that carries no name
    field at all, not one carrying an explicit null.
    """
    info = {"hid": HUB_HID, "mid": HUB_MID, "model": "HWG023WBRF-V2"}
    if name is not None:
        info["name"] = name
    return info


def _mock_hub_coordinator():
    """A bare hub coordinator stand-in with an empty hubs list, enough for
    hub entity constructors that never read past their own hub_info
    argument."""
    coordinator = MagicMock()
    coordinator.data = {"hubs": []}
    return coordinator


def _make_renamed_hub_device(hass, device_registry, entry, *, code_name, display_name):
    """Build a hub device registry row, then rename it the way a real user does.

    Created first with the name the integration itself supplies, then updated
    with ``name_by_user``: that is the field a real Home Assistant rename
    writes, and the field ``_async_get_full_entity_name`` prefers over the
    plain ``name``. Exercising this field rather than ``name`` directly is
    what proves composition against a user-owned field the code never writes.
    """
    device = device_registry.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, f"{HUB_IDENTIFIER_PREFIX}{HUB_HID}_{HUB_MID}")},
        name=code_name,
    )
    return device_registry.async_update_device(device.id, name_by_user=display_name)


def _socket_sensor_info(sub_name):
    """Build a sensor_info entry for SOCKET_MODEL, the one real CTL_SOCK
    candidate in the committed catalog with no hand-written decoder, carrying
    a generic-decoded run-state field so build_generic_switch_entities has
    something to build a switch from."""
    entry = make_sensor_entry(
        hid=HID,
        mid=MID,
        addr=ADDR,
        model=SOCKET_MODEL,
        sub_name=sub_name,
        data={
            "type": "unknown",
            "model": SOCKET_MODEL,
            "raw_value": "11#00",
            "generic": {
                "decoder": "generic-tlv",
                "fields": [{"name": RUN_STATE_IDENTITY, "index": 30, "dp_id": 30, "raw": "01", "value": 1}],
                "field_names": [RUN_STATE_IDENTITY],
            },
        },
    )
    entry["model_code"] = SOCKET_MODEL_CODE
    return entry


class TestRenamedDeviceComposesShortName:
    """The behavioural proof that a renamed device still composes correctly.

    The maintainer owned two HTV210Bs and renamed one to "HTV210B (Hub paired)"
    to tell them apart -- an ordinary user action the integration cannot
    prevent. Composing against that renamed device previously surfaced the
    un-stripped "HTV210B Zone 1", because has_entity_name was False and the
    device registry was never consulted.
    """

    @pytest.mark.asyncio
    async def test_valve_composes_against_renamed_device(self, hass, device_registry):
        """Compose the valve's short name against a device row renamed away
        from its sub_name and assert the renamed display name, not the
        original sub_name, appears in the composed entity name."""
        entry = _make_entry(hass)
        device = _make_renamed_device(hass, device_registry, entry, sub_name="HTV210B", display_name="HTV210B (Hub paired)")
        valve = RainPointValveEntity(_mock_coordinator(), "100_200_1", _sensor_info("HTV210B"), 1)

        assert _compose(hass, valve, device) == "HTV210B (Hub paired) Zone 1"

    @pytest.mark.asyncio
    async def test_duration_composes_against_renamed_device(self, hass, device_registry):
        """Compose the duration number's short name against the same renamed
        device row and assert the renamed display name carries through,
        mirroring the valve case above for the sibling platform."""
        entry = _make_entry(hass)
        device = _make_renamed_device(hass, device_registry, entry, sub_name="HTV210B", display_name="HTV210B (Hub paired)")
        number = RainPointZoneDurationNumber(_mock_coordinator(), "100_200_1", _sensor_info("HTV210B"), 1)

        assert _compose(hass, number, device) == "HTV210B (Hub paired) Zone 1 Duration"

    @pytest.mark.asyncio
    async def test_matching_name_device_is_the_control_and_proves_nothing(self, hass, device_registry):
        """Control case, labelled as such: display name equals the sub-device name.

        This passes both before and after the fix, and is kept here so the two
        diverging-name tests above cannot later be quietly swapped for this one
        without the loss of coverage being visible.
        """
        entry = _make_entry(hass)
        device = _make_renamed_device(hass, device_registry, entry, sub_name="HTV210B", display_name="HTV210B")
        valve = RainPointValveEntity(_mock_coordinator(), "100_200_1", _sensor_info("HTV210B"), 1)
        number = RainPointZoneDurationNumber(_mock_coordinator(), "100_200_1", _sensor_info("HTV210B"), 1)

        assert _compose(hass, valve, device) == "HTV210B Zone 1"
        assert _compose(hass, number, device) == "HTV210B Zone 1 Duration"


class TestRenamedDeviceComposesShortNameForTheSensorTree:
    """The behavioural proof for the shared RainPointSubDeviceEntity base.

    The maintainer's second reproduction device is the HCS026FRF soil sensor,
    named "HCS026FRF Moisture Sensor" -- diverging from the sub-device's own
    "HCS026FRF" name the same way the HTV210B does, so it proves the base
    class's composition rather than only the two platforms with their own
    constructors.
    """

    @pytest.mark.asyncio
    async def test_moisture_percent_composes_against_renamed_device(self, hass, device_registry):
        """Compose the moisture percent sensor's short name against the
        renamed soil-sensor device row, proving the shared
        RainPointSubDeviceEntity base consults the device registry rather
        than reading the sensor_info's own sub_name."""
        entry = _make_entry(hass)
        device = _make_renamed_device(
            hass, device_registry, entry, sub_name="HCS026FRF", display_name="HCS026FRF Moisture Sensor"
        )
        sensor_info = _sensor_info("HCS026FRF")
        sensor = RainPointMoisturePercentSensor(_mock_coordinator(), "100_200_1", sensor_info, "100_200_1", simple=True)

        assert _compose(hass, sensor, device) == "HCS026FRF Moisture Sensor Moisture Percent"

    @pytest.mark.asyncio
    async def test_battery_composes_against_renamed_device(self, hass, device_registry):
        """Compose the battery sensor's short name against the same renamed
        device row, the second of two sensor-tree platforms proving the
        shared base's composition rather than trusting the moisture case to
        stand in for it."""
        entry = _make_entry(hass)
        device = _make_renamed_device(
            hass, device_registry, entry, sub_name="HCS026FRF", display_name="HCS026FRF Moisture Sensor"
        )
        sensor_info = _sensor_info("HCS026FRF")
        sensor = RainPointBatterySensor(_mock_coordinator(), "100_200_1", sensor_info, "100_200_1")

        assert _compose(hass, sensor, device) == "HCS026FRF Moisture Sensor Battery"


class TestRenamedDeviceComposesShortNameForGenericAndSelect:
    """The behavioural proof for the two remaining converted platforms.

    RainPointGenericSwitch inherits the flag from RainPointGenericControlBase,
    which shares no base with RainPointSubDeviceEntity;
    RainPointSubDevicePowerSelect needed no source change at all, since it
    inherits the flag from RainPointSubDeviceEntity and already carried a
    short name -- this is what proves both inheritance claims rather than
    trusting them.
    """

    @pytest.mark.asyncio
    async def test_generic_switch_composes_against_renamed_device(self, hass, device_registry):
        """Compose the generic switch's short name against a renamed outlet
        device row, proving RainPointGenericSwitch's inherited flag (from
        RainPointGenericControlBase, which shares no base with
        RainPointSubDeviceEntity) actually drives correct composition rather
        than merely resolving to True in isolation."""
        entry = _make_entry(hass)
        device = _make_renamed_device(hass, device_registry, entry, sub_name="Outlet 1", display_name="Outlet 1 (Garage)")
        sensor_info = _socket_sensor_info("Outlet 1")
        coordinator = _mock_coordinator()
        coordinator.data = {"sensors": {"100_200_1": sensor_info}}

        entities = build_generic_switch_entities(coordinator, "100_200_1", sensor_info, "100_200_1")

        assert len(entities) == 1
        switch = entities[0]
        assert _compose(hass, switch, device) == "Outlet 1 (Garage) CTL_SOCK (unverified)"

    @pytest.mark.asyncio
    async def test_select_composes_against_renamed_device(self, hass, device_registry):
        """Compose the power select's short name against the renamed HTV210B
        device row, the second inheritance path (RainPointSubDeviceEntity)
        proven the same way as the generic switch above."""
        entry = _make_entry(hass)
        device = _make_renamed_device(hass, device_registry, entry, sub_name="HTV210B", display_name="HTV210B (Hub paired)")
        select = RainPointSubDevicePowerSelect(_mock_coordinator(), "100_200_1", _sensor_info("HTV210B"))

        assert _compose(hass, select, device) == "HTV210B (Hub paired) Transmission Power"


class TestRenamedHubComposesShortName:
    """The behavioural proof for the hub tree: a hub the user has renamed.

    Mirrors TestRenamedDeviceComposesShortName's reasoning for the sub-device
    side: a fixture whose device row name matches the hub record's own name
    would pass whether or not the hub base ever consults the device registry.
    """

    @pytest.mark.asyncio
    async def test_hub_rssi_composes_against_renamed_hub(self, hass, device_registry):
        """Compose the hub RSSI sensor's short name against a hub device row
        the user has renamed via name_by_user, asserting the renamed display
        name reaches the composed string rather than the hub record's own
        name."""
        entry = _make_entry(hass)
        device = _make_renamed_hub_device(hass, device_registry, entry, code_name="RainPoint Hub", display_name="Kitchen Hub")
        sensor = RainPointHubRSSISensor(_mock_hub_coordinator(), _hub_info(name="RainPoint Hub"))

        assert _compose(hass, sensor, device) == "Kitchen Hub Signal Strength"

    @pytest.mark.asyncio
    async def test_hub_device_id_composes_against_renamed_hub(self, hass, device_registry):
        """The second of the two distinct hub name shapes: rebuilt from the hub
        record rather than appended to the inherited name, and must be proven
        separately from the RSSI case above."""
        entry = _make_entry(hass)
        device = _make_renamed_hub_device(hass, device_registry, entry, code_name="RainPoint Hub", display_name="Kitchen Hub")
        sensor = RainPointHubDeviceIDSensor(_mock_hub_coordinator(), _hub_info(name="RainPoint Hub"))

        assert _compose(hass, sensor, device) == "Kitchen Hub Device ID"

    @pytest.mark.asyncio
    async def test_unnamed_hub_still_yields_short_names(self, hass, device_registry):
        """A hub record with no name at all still yields a short entity name,
        never 'None Signal Strength' and never 'RainPoint Hub Signal Strength'
        once the base stops appending anything to compose against."""
        entry = _make_entry(hass)
        device = device_registry.async_get_or_create(
            config_entry_id=entry.entry_id,
            identifiers={(DOMAIN, f"{HUB_IDENTIFIER_PREFIX}{HUB_HID}_{HUB_MID}")},
            name="RainPoint Hub",
        )
        sensor = RainPointHubRSSISensor(_mock_hub_coordinator(), _hub_info())

        assert _compose(hass, sensor, device) == "RainPoint Hub Signal Strength"
        assert device.name == "RainPoint Hub"


class TestEveryHubEntitySetsHasEntityName:
    """Covers the six hub entity families that root at RainPointHubDevice."""

    def test_all_six_hub_families_carry_the_flag(self):
        """Constructs one instance of each of the six hub entity families that
        root at RainPointHubDevice, rather than reading the flag off the base
        class, so a family that stopped inheriting the base would be caught."""
        coordinator = _mock_hub_coordinator()
        mqtt_client = MagicMock()
        hub_info = _hub_info(name="RainPoint Hub")

        rssi = RainPointHubRSSISensor(coordinator, hub_info)
        connectivity = RainPointHubConnectivityBinarySensor(coordinator, hub_info)
        channel_select = RainPointHubChannelSelect(coordinator, hub_info)
        push_connected = RainPointPushConnectedBinarySensor(mqtt_client, hub_info)
        broadcast_switch = RainPointHubBroadcastSwitch(coordinator, hub_info)
        broadcast_button = RainPointHubBroadcastButton(coordinator, hub_info)

        for entity in (rssi, connectivity, channel_select, push_connected, broadcast_switch, broadcast_button):
            assert entity.has_entity_name is True


class TestHubEntityUniqueIdsUnchanged:
    """The unique_id side of the hub naming conversion: proves the
    identifiers used for registry entries did not move even though the
    display-name composition did."""

    def test_hub_entity_unique_ids_are_byte_identical(self):
        """The identity half stays pinned while the name half moves."""
        rssi = RainPointHubRSSISensor(_mock_hub_coordinator(), _hub_info(name="RainPoint Hub"))
        device_id_sensor = RainPointHubDeviceIDSensor(_mock_hub_coordinator(), _hub_info(name="RainPoint Hub"))

        assert rssi._attr_unique_id == f"rainpoint_hub_{HUB_HID}_{HUB_MID}_rssi"
        assert device_id_sensor._attr_unique_id == f"rainpoint_hub_{HUB_HID}_{HUB_MID}_device_id"


class TestEveryConvertedPlatformSetsHasEntityName:
    """Covers has_entity_name across every platform this branch converted,
    split by which shared base each inherits the flag from."""

    def test_valve_and_duration_carry_the_flag(self):
        """Assert the valve and duration number, the two platforms with
        their own __init__ rather than a shared sensor-tree base, both
        resolve has_entity_name to True."""
        valve = RainPointValveEntity(_mock_coordinator(), "100_200_1", _sensor_info("HTV210B"), 1)
        number = RainPointZoneDurationNumber(_mock_coordinator(), "100_200_1", _sensor_info("HTV210B"), 1)

        assert valve.has_entity_name is True
        assert number.has_entity_name is True

    def test_generic_switch_and_select_carry_the_flag(self):
        """Neither declares the flag on its own class; the two inherit it from different roots.

        RainPointGenericSwitch inherits it from RainPointGenericControlBase,
        which shares no base with RainPointSubDeviceEntity; the select
        inherits it from RainPointSubDeviceEntity. Two roots is the reason
        both are constructed here rather than one standing in for the other.
        """
        sensor_info = _socket_sensor_info("Outlet 1")
        coordinator = _mock_coordinator()
        coordinator.data = {"sensors": {"100_200_1": sensor_info}}
        entities = build_generic_switch_entities(coordinator, "100_200_1", sensor_info, "100_200_1")
        select = RainPointSubDevicePowerSelect(_mock_coordinator(), "100_200_1", _sensor_info("HTV210B"))

        assert entities[0].has_entity_name is True
        assert select.has_entity_name is True
        assert "_attr_has_entity_name" not in type(entities[0]).__dict__
        assert "_attr_has_entity_name" not in type(select).__dict__

    def test_sensor_tree_platforms_carry_the_flag_by_inheritance(self):
        """None of these three declares the flag on its own class.

        Each inherits it from RainPointSubDeviceEntity or RainPointSensorBase,
        which the three __dict__ assertions below prove directly rather than
        deferring the claim anywhere else.
        """
        sensor_info = _sensor_info("HCS026FRF")
        coordinator = _mock_coordinator()
        moisture = RainPointMoisturePercentSensor(coordinator, "100_200_1", sensor_info, "100_200_1", simple=True)
        battery = RainPointBatterySensor(coordinator, "100_200_1", sensor_info, "100_200_1")
        zone_state = RainPointZoneStateSensor(coordinator, "100_200_1", sensor_info, "100_200_1", 1)

        assert moisture.has_entity_name is True
        assert battery.has_entity_name is True
        assert zone_state.has_entity_name is True
        assert "_attr_has_entity_name" not in RainPointMoisturePercentSensor.__dict__
        assert "_attr_has_entity_name" not in RainPointBatterySensor.__dict__
        assert "_attr_has_entity_name" not in RainPointZoneStateSensor.__dict__


class TestUserOverrideWins:
    """Covers the case _compose deliberately does not: a user-set name
    override in the entity registry must survive composition unchanged."""

    @pytest.mark.asyncio
    async def test_user_set_name_override_composes_unchanged(self, hass, device_registry):
        """An entity registry row that already carries a user override keeps it."""
        entry = _make_entry(hass)
        device = _make_renamed_device(hass, device_registry, entry, sub_name="HTV210B", display_name="HTV210B (Hub paired)")
        valve = RainPointValveEntity(_mock_coordinator(), "100_200_1", _sensor_info("HTV210B"), 1)

        # Not routed through _compose: the point of this case is the `name`
        # argument, which _compose deliberately pins to None.
        composed = er._async_get_full_entity_name(
            hass,
            device_id=device.id,
            fallback="unnamed",
            has_entity_name=valve.has_entity_name,
            name="My Custom Zone Name",
            original_name=valve._attr_name,
            **_FULL_NAME_EXTRA_KWARGS,
        )

        assert composed == "My Custom Zone Name"


class TestUniqueIdUnchanged:
    """The unique_id counterpart to TestEveryConvertedPlatformSetsHasEntityName:
    proves the naming conversion touched only the display name, not entity
    identity."""

    def test_valve_and_duration_unique_ids_are_byte_identical(self):
        """Assert the valve and duration number's unique_id strings are
        unchanged from before the naming conversion, byte for byte."""
        valve = RainPointValveEntity(_mock_coordinator(), "100_200_1", _sensor_info("HTV210B"), 1)
        number = RainPointZoneDurationNumber(_mock_coordinator(), "100_200_1", _sensor_info("HTV210B"), 1)

        assert valve._attr_unique_id == "rainpoint_100_200_1_zone1"
        assert number._attr_unique_id == "rainpoint_100_200_1_zone1_duration"
