"""Tests for device base classes (device.py)."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from homeassistant.helpers import device_registry as dr
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.rainpoint import _register_hub_devices
from custom_components.rainpoint.const import CONF_HIDS, DOMAIN, MODEL_VALVE_245
from custom_components.rainpoint.device import (
    _VIA_DEVICE_ID_SUPPORTED,
    RainPointHubDevice,
    build_sub_device_info,
    hub_display_name,
)
from tests.helpers import VALVE_ZONES_TLV_PAYLOAD, platform_with_hub, registered_parent_id

_LINK_KEYS = {"via_device", "via_device_id"}


class TestRainPointHubDevice:
    """Tests for RainPointHubDevice."""

    def _make_hub(self, hid=100, name="My Hub", model="HTV0540FRF", mid=1001, mac="AA:BB:CC:DD:EE:FF"):
        """Create a RainPointHubDevice with a mock coordinator via __new__.

        mac defaults to a well-formed address; pass None to build a record
        that carries no mac key at all, which is how a cloud record missing
        the field arrives.
        """
        hub_info = {
            "hid": hid,
            "mid": mid,
            "name": name,
            "model": model,
            "softVer": "2.0",
            "hardwareVersion": "1.0",
        }
        if mac is not None:
            hub_info["mac"] = mac
        # RainPointHubDevice inherits from Entity stub, so use __new__ to bypass
        # any super().__init__ that might call into MagicMock internals.
        hub = RainPointHubDevice.__new__(RainPointHubDevice)
        RainPointHubDevice.__init__(hub, hub_info)
        return hub

    def test_hub_device_info_identifiers(self):
        """device_info should contain the expected identifier tuple."""
        hub = self._make_hub(hid=100)
        info = hub.device_info
        assert (DOMAIN, "hub_100_1001") in info["identifiers"]

    def test_hub_device_info_name(self):
        """device_info name should match hub_info name."""
        hub = self._make_hub(name="My Hub")
        assert hub.device_info["name"] == "My Hub"

    def test_hub_device_info_manufacturer(self):
        """device_info manufacturer should be 'RainPoint'."""
        hub = self._make_hub()
        assert hub.device_info["manufacturer"] == "RainPoint"

    def test_hub_device_info_model(self):
        """device_info model should match hub_info model."""
        hub = self._make_hub(model="HTV0540FRF")
        assert hub.device_info["model"] == "HTV0540FRF"

    def test_hub_mac_is_a_connection_and_not_a_serial_number(self):
        """The MAC reaches the registry as a connection, leaving no serial.

        Both halves are asserted together because the point of the pair is
        that one replaced the other: asserting only the connection would
        still pass while a MAC sat behind the "Serial number" label as well.
        The cloud carries no manufacturer serial for a hub, so the key is
        absent rather than holding some other value.
        """
        info = self._make_hub(mac="A0:A3:B3:7C:AF:FC").device_info
        assert info["connections"] == {("mac", "a0:a3:b3:7c:af:fc")}
        assert "serial_number" not in info

    def test_hub_mac_is_normalised_to_the_spelling_the_registry_matches_on(self):
        """An uppercase cloud mac is stored lowercase.

        Home Assistant merges devices by comparing connection tuples
        literally, so a hub registered under the cloud's uppercase spelling
        would never match the same hardware another integration registered
        in lowercase.
        """
        info = self._make_hub(mac="AA:BB:CC:DD:EE:FF").device_info
        assert info["connections"] == {("mac", "aa:bb:cc:dd:ee:ff")}

    @pytest.mark.parametrize(
        "mac",
        [
            None,
            "",
            "   ",
            "N/A",
            "not-a-mac",
            "unknown",
            "AA:BB:CC:DD:EE",
            "zz:zz:zz:zz:zz:zz",
            "00:00:00:00:00:00",
            "FF:FF:FF:FF:FF:FF",
            "01:00:5e:00:00:fb",
        ],
    )
    def test_hub_without_a_usable_mac_declares_no_connections(self, mac):
        """Anything that is not a real address yields no connection at all.

        The nonempty junk matters more than the missing key. format_mac hands
        back whatever it cannot parse, so "N/A" survives the call looking
        exactly like a formatted address to a truthiness check, and the
        truncated and non-hex spellings are here because format_mac's own
        recognition is by length and separator count rather than by content.

        The last three are the ones a syntax check alone lets through. The
        all-zero address is a placeholder, and the broadcast and multicast
        addresses are addressed to a group rather than to an interface, so
        none of them belongs to a single hub either.
        """
        info = self._make_hub(mac=mac).device_info
        assert "connections" not in info

    @pytest.mark.parametrize("placeholder", ["", "N/A", "unknown", "00:00:00:00:00:00"])
    def test_two_hubs_sharing_a_placeholder_mac_are_not_merged(self, placeholder):
        """The same placeholder on two hubs produces no shared connection.

        This is the consequence the guard above exists for, asserted on the
        real pair rather than on one record at a time. A connection tuple is
        Home Assistant's instruction to treat two device entries as one
        device, so two hubs registering ("mac", "N/A") would collapse into a
        single row and one of them would lose its entities. Their
        identifiers still differ, which is what keeps them apart once no
        connection is emitted.
        """
        first = self._make_hub(hid=100, mid=1001, mac=placeholder).device_info
        second = self._make_hub(hid=100, mid=2002, mac=placeholder).device_info
        assert "connections" not in first
        assert "connections" not in second
        assert first["identifiers"] != second["identifiers"]

    @pytest.mark.parametrize(
        ("mac", "expected"),
        [
            ("a0:a3:b3:7c:af:fc", "a0:a3:b3:7c:af:fc"),
            ("A0:A3:B3:7C:AF:FC", "a0:a3:b3:7c:af:fc"),
            ("A0-A3-B3-7C-AF-FC", "a0:a3:b3:7c:af:fc"),
            ("A0A3B37CAFFC", "a0:a3:b3:7c:af:fc"),
            ("02:1a:2b:3c:4d:5e", "02:1a:2b:3c:4d:5e"),
            ("06:1a:2b:3c:4d:5e", "06:1a:2b:3c:4d:5e"),
        ],
    )
    def test_every_spelling_of_one_address_reaches_the_registry_identically(self, mac, expected):
        """Every acceptable address reaches the registry unchanged in meaning.

        Guarding the tightened check against over-rejection: the separator
        and case variants are real cloud spellings, and each has to survive
        as a connection rather than be discarded alongside the junk.

        The last two are locally administered addresses, which an interface
        can legitimately carry. They sit one bit away from the multicast
        addresses the guard rejects, so they are here to hold that line.
        """
        info = self._make_hub(mac=mac).device_info
        assert info["connections"] == {("mac", expected)}

    def test_hub_registry_identity_does_not_depend_on_the_mac(self):
        """identifiers is the same with and without a mac.

        identifiers alone keys the device registry entry, so an install
        upgrading into the connections change keeps its existing device row,
        its entities and their history.
        """
        with_mac = self._make_hub(hid=100, mid=1001).device_info
        without_mac = self._make_hub(hid=100, mid=1001, mac=None).device_info
        assert with_mac["identifiers"] == without_mac["identifiers"] == {(DOMAIN, "hub_100_1001")}

    def test_hub_available_always_true(self):
        """Hub is always available if config exists."""
        hub = self._make_hub()
        assert hub.available is True

    def test_hub_unique_id_format(self):
        """unique_id should be domain_hub_{hid}_{mid}."""
        hub = self._make_hub(hid=42, mid=77)
        assert hub._attr_unique_id == f"{DOMAIN}_hub_42_77"

    def test_base_sets_no_attr_name(self):
        """RainPointHubDevice leaves no _attr_name in the instance dict.

        Written against the instance dict rather than hasattr: the test
        conftest's stand-in for the Home Assistant base declares
        ``_attr_name = None`` as a real class attribute, while the real base
        only annotates it, so hasattr would answer differently under test
        than in production. The base must supply no name at all, so no
        subclass composing against it can double up on one.
        """
        hub = self._make_hub(name="Test Hub")
        assert "_attr_name" not in hub.__dict__

    def test_base_sets_has_entity_name(self):
        """The one flag site covering every hub entity family."""
        hub = self._make_hub(name="Test Hub")
        assert hub._attr_has_entity_name is True

    def test_should_poll_is_false(self):
        """Hub entities are coordinator-driven; a poll of their own would be redundant."""
        hub = self._make_hub()
        assert hub._attr_should_poll is False


class TestHubDisplayNameFallback:
    """The literal fallback name a cloud-unnamed hub's device row carries."""

    def test_unnamed_hub_falls_back_to_the_exact_literal(self):
        """Pinned exactly: this string reaches the device page verbatim."""
        assert hub_display_name({}) == "RainPoint Hub"

    def test_a_named_hub_keeps_its_own_name(self):
        """The fallback applies only when the cloud supplied no name."""
        assert hub_display_name({"name": "My Hub"}) == "My Hub"


class TestBuildSubDeviceInfoIdentity:
    """Identity fields the builder must keep stable for every platform."""

    def test_manufacturer_is_rainpoint(self):
        """All supported hardware is RainPoint, hub and sub-device alike."""
        info = build_sub_device_info(
            {"hid": 100, "mid": 200, "addr": 1, "sub_name": "Soil Sensor", "model": "HCS026FRF"},
        )
        assert info["manufacturer"] == "RainPoint"

    def test_model_is_passed_through(self):
        """The cloud's model string reaches the device page verbatim."""
        info = build_sub_device_info(
            {"hid": 100, "mid": 200, "addr": 1, "sub_name": "Soil Sensor", "model": "HCS026FRF"},
        )
        assert info["model"] == "HCS026FRF"
        assert info["model_id"] is None

    def test_retail_name_shown_with_reported_model_as_model_id(self):
        """An HCS0565ARF reports as HCS0528ARF; the page shows the name on the box."""
        info = build_sub_device_info(
            {"hid": 100, "mid": 200, "addr": 1, "model": "HCS0528ARF", "display_model": "HCS0565ARF"},
        )
        assert info["model"] == "HCS0565ARF"
        assert info["model_id"] == "HCS0528ARF"

    def test_truncated_display_model_is_ignored(self):
        """A displayModel that only drops the suffix leaves the page unchanged."""
        info = build_sub_device_info(
            {"hid": 100, "mid": 200, "addr": 1, "model": "HCS026FRF", "display_model": "HCS026"},
        )
        assert info["model"] == "HCS026FRF"
        assert info["model_id"] is None

    def test_unnamed_device_is_named_from_the_retail_name(self):
        """The fallback name matches the model line rather than the reported model."""
        info = build_sub_device_info(
            {"hid": 100, "mid": 200, "addr": 1, "model": "HCS0528ARF", "display_model": "HCS0565ARF"},
        )
        assert info["name"] == "HCS0565ARF 1"

    def test_registry_sweep_reports_the_retail_model(self):
        """The poll-time sweep writes the same model, model_id and name the DeviceInfo did."""
        from custom_components.rainpoint import _reported_device_fields

        record = {"hid": 100, "mid": 200, "addr": 1, "model": "HCS0528ARF", "display_model": "HCS0565ARF"}
        fields = _reported_device_fields("100_200_1", {"100_200_1": record}, {})
        assert fields["model"] == "HCS0565ARF"
        assert fields["model_id"] == "HCS0528ARF"
        assert fields["name"] == "HCS0565ARF 1"

    def test_non_string_display_model_is_ignored(self):
        """A cloud value of the wrong type must not raise inside entity setup."""
        info = build_sub_device_info({"hid": 100, "mid": 200, "addr": 1, "model": "HCS0528ARF", "display_model": 565})
        assert info["model"] == "HCS0528ARF"
        assert info["model_id"] is None

    def test_display_model_without_model_is_ignored(self):
        """No reported model means nothing to keep as model_id, so the fallback stands."""
        info = build_sub_device_info({"hid": 100, "mid": 200, "addr": 1, "display_model": "HCS0565ARF"})
        assert info["model"] == "Unknown"


class TestBuildSubDeviceInfo:
    """Tests for the shared sub-device DeviceInfo builder."""

    def _info(self, **overrides):
        base = {
            "hid": 100,
            "mid": 200,
            "addr": 1,
            "sub_name": "Soil Sensor",
            "model": "HCS026FRF",
            "firmware_version": "1.4",
            "hub_paired": True,
        }
        base.update(overrides)
        return base

    def test_firmware_becomes_sw_version(self):
        """The firmware the coordinator already carries reaches the device page."""
        info = build_sub_device_info(self._info())
        assert info["sw_version"] == "1.4"

    def test_serial_number_is_the_mid_addr_pair(self):
        """Sub-devices have no manufacturer serial, so the stable pair stands in for one."""
        info = build_sub_device_info(self._info(mid=200, addr=7))
        assert info["serial_number"] == "200_7"

    async def test_links_to_the_parent_hub_when_hub_paired(self, hass):
        """A sub-device carried by a real hub links to the hub's registry row."""
        platform, hub_id = platform_with_hub(hass)
        info = build_sub_device_info(self._info(hid=100, hub_paired=True), platform)
        assert registered_parent_id(platform, info) == hub_id

    @pytest.mark.skipif(not _VIA_DEVICE_ID_SUPPORTED, reason="Home Assistant before 2026.9 has no via_device_id")
    async def test_the_link_is_the_registry_id_not_the_deprecated_identifier(self, hass):
        """The hub is named by device id, so Home Assistant has nothing to deprecate."""
        platform, hub_id = platform_with_hub(hass)
        info = build_sub_device_info(self._info(), platform)
        assert info["via_device_id"] == hub_id
        assert "via_device" not in info

    def test_no_link_without_a_platform(self):
        """An entity not yet added has no registry to resolve the hub in."""
        assert not _LINK_KEYS & build_sub_device_info(self._info()).keys()

    async def test_no_link_when_the_platform_has_no_config_entry(self, hass):
        """A platform outside a config entry cannot own the hub row."""
        platform = SimpleNamespace(hass=hass, config_entry=None)
        assert not _LINK_KEYS & build_sub_device_info(self._info(), platform).keys()

    async def test_no_link_while_the_hub_row_is_unregistered(self, hass):
        """A hub this entry has not registered yet leaves the device parentless, not broken."""
        platform, _ = platform_with_hub(hass, mid=999)
        info = build_sub_device_info(self._info(), platform)
        assert not _LINK_KEYS & info.keys()
        assert registered_parent_id(platform, info) is None

    async def test_no_via_device_when_not_hub_paired(self, hass):
        """A sub-device carried by the Bluetooth wrapper record gets no parent.

        Both product_key and device_name are populated here to prove the
        builder reads only the stamped hub_paired field and never re-derives
        the verdict from the raw hub fields it supersedes.
        """
        platform, _ = platform_with_hub(hass)
        info = build_sub_device_info(
            self._info(hub_paired=False, product_key="pk", device_name="dev"),
            platform,
        )
        assert not _LINK_KEYS & info.keys()

    async def test_via_device_present_when_hub_paired_key_absent(self, hass):
        """A sensor_info with no hub_paired key defaults to hub-linked."""
        platform, hub_id = platform_with_hub(hass)
        info = self._info()
        del info["hub_paired"]
        result = build_sub_device_info(info, platform)
        assert registered_parent_id(platform, result) == hub_id

    async def test_only_via_device_differs_between_the_two_polarities(self, hass):
        """Parenting is the only thing that changes; every other field agrees."""
        platform, _ = platform_with_hub(hass)
        linked = build_sub_device_info(self._info(hub_paired=True), platform)
        parentless = build_sub_device_info(self._info(hub_paired=False), platform)
        linked_without_parent = {k: v for k, v in linked.items() if k not in _LINK_KEYS}
        parentless_without_parent = {k: v for k, v in parentless.items() if k not in _LINK_KEYS}
        assert linked_without_parent == parentless_without_parent
        assert not _LINK_KEYS & parentless.keys()
        assert _LINK_KEYS & linked.keys()

    def test_identifiers_are_unchanged(self):
        """The registry key keeps its existing shape so no migration is needed."""
        info = build_sub_device_info(self._info())
        assert (DOMAIN, "100_200_1") in info["identifiers"]

    def test_missing_firmware_leaves_sw_version_unset(self):
        """A device the cloud reports no firmware for gets no fabricated version."""
        info = build_sub_device_info(self._info(firmware_version=None))
        assert info["sw_version"] is None

    def test_name_falls_back_only_when_unnamed(self):
        """The fallback applies only to a device the cloud did not name."""
        named = build_sub_device_info(self._info())
        unnamed = build_sub_device_info(self._info(sub_name=None))
        assert named["name"] == "Soil Sensor"
        assert unnamed["name"] == "HCS026FRF 1"

    def test_missing_model_reads_unknown(self):
        """An absent model is reported as Unknown rather than left blank."""
        info = build_sub_device_info(self._info(model=None))
        assert info["model"] == "Unknown"

    def test_unnamed_fallback_uses_the_model(self):
        """A cloud-unnamed device's name is '{model} {addr}', one spelling
        regardless of which of the four platforms reaches the device
        registry first."""
        info = build_sub_device_info(self._info(sub_name=None, model="HTV245FRF", addr=1))
        assert info["name"] == "HTV245FRF 1"

    def test_unnamed_fallback_with_no_model_reads_device_not_unknown(self):
        """A cloud-unnamed device with no model reads 'Device {addr}', never
        'Unknown {addr}': the display fallback and the model field default
        independently, so one cannot leak into the other."""
        info = build_sub_device_info(self._info(sub_name=None, model=None, addr=12))
        assert info["name"] == "Device 12"
        assert info["model"] == "Unknown"


class TestUnnamedSubDeviceNameAgreesAcrossPlatforms:
    """Closes the first-platform-wins race: whichever of the four platforms
    reaches the device registry first for the same cloud-unnamed sub-device,
    the name it registers is identical, because all four now compute it
    through the same build_sub_device_info call rather than four different
    inline literals."""

    def test_all_four_platforms_agree_on_the_unnamed_device_name(self):
        """Build one entity from each of the four converted platforms against
        the same cloud-unnamed sub-device and assert their device_info names
        agree, proving the platforms delegate to the shared
        build_sub_device_info call rather than each carrying its own inline
        fallback literal."""
        from custom_components.rainpoint.generic_control import ControlDatapoint, RainPointGenericControlBase
        from custom_components.rainpoint.number import RainPointZoneDurationNumber
        from custom_components.rainpoint.sensor import RainPointMoisturePercentSensor
        from custom_components.rainpoint.valve import RainPointValveEntity

        coordinator = MagicMock()
        coordinator.data = {"sensors": {}}
        sensor_info = {"hid": 100, "mid": 200, "addr": 1, "model": "HTV245FRF"}

        valve = RainPointValveEntity(coordinator, "100_200_1", sensor_info, 1)
        number = RainPointZoneDurationNumber(coordinator, "100_200_1", sensor_info, 1)
        sensor = RainPointMoisturePercentSensor(coordinator, "100_200_1", sensor_info, "100_200_1", simple=True)
        datapoint = ControlDatapoint(identity="CTL_SOCK", dp_port=1, command_port=1, dp_code=1, dp_data_type=1)
        control = RainPointGenericControlBase(coordinator, "100_200_1", sensor_info, "100_200_1", datapoint, None)

        names = {
            valve.device_info["name"],
            number.device_info["name"],
            sensor.device_info["name"],
            control.device_info["name"],
        }
        assert names == {"HTV245FRF 1"}


class TestSubDeviceParentingRealTimeline:
    """Drives the real construct -> first refresh -> platform setup sequence
    for two top-level records in one home, a real hub and the Bluetooth
    wrapper record, proving parenting on the DeviceInfo the valve platform's
    real async_setup_entry actually built, rather than on build_sub_device_info
    called directly with a hand-built dict or on an injected coordinator.data
    snapshot. Per the entity-lifecycle invariant (CLAUDE.md), entity creation
    is one-shot off the single coordinator.data snapshot built at
    async_config_entry_first_refresh, so asserting on the entity object here
    is what proves the feature reaches a live platform and is not merely true
    of the builder in isolation.

    The wrapper record's real-world child is an HTV210B that reports no
    status at all (silent). The reporting valve child used here is the
    structurally equivalent case chosen so both polarities exist at setup
    time with no debounce; the silent HTV210B shape is covered by
    TestSilentEntityAppearsWithinTheSession in tests/test_sensor.py.
    """

    @staticmethod
    def _zone_payload():
        """The shared captured TLV zone fixture, rather than a second copy."""
        return VALVE_ZONES_TLV_PAYLOAD

    async def _build(self, hass, entry=None, *, register_hubs=True) -> tuple[dict, MockConfigEntry]:
        """Construct, first-refresh, register the hub rows, then run
        valve.async_setup_entry over a two-record poll: a real hub at mid 200
        and the Bluetooth wrapper record at mid 201, each carrying one valve
        sub-device at addr 1. Each entity is then registered the way Home
        Assistant's entity platform does it. Returns the device rows keyed by
        _sensor_key, plus the entry."""
        from custom_components.rainpoint.coordinator import RainPointCoordinator
        from custom_components.rainpoint.valve import async_setup_entry

        zone_value = self._zone_payload()

        client = AsyncMock()
        client.get_devices_by_hid.return_value = [
            {
                "mid": 200,
                "name": "Hub A",
                "deviceName": "d",
                "productKey": "pk",
                "homeName": "H",
                "subDevices": [{"addr": 1, "name": "Valve", "model": MODEL_VALVE_245, "softVer": "127"}],
            },
            {
                # The Bluetooth wrapper record: every identity field is the
                # empty string and it carries neither did nor mac.
                "mid": 201,
                "name": "",
                "deviceName": "",
                "productKey": "",
                "model": "",
                "homeName": "H",
                "subDevices": [{"addr": 1, "name": "Valve", "model": MODEL_VALVE_245, "softVer": "127"}],
            },
        ]
        client.get_multiple_device_status.return_value = [
            {"mid": 200, "subDeviceStatus": [{"id": "D01", "value": zone_value, "time": 1785420002247}]},
            {"mid": 201, "subDeviceStatus": [{"id": "D01", "value": zone_value, "time": 1785420002247}]},
        ]

        if entry is None:
            entry = MockConfigEntry(domain=DOMAIN, data={CONF_HIDS: [100]}, options={})
            entry.add_to_hass(hass)
        hass.data.setdefault(DOMAIN, {})[entry.entry_id] = {}

        coordinator = RainPointCoordinator(hass, client, entry)
        hass.data[DOMAIN][entry.entry_id]["coordinator"] = coordinator

        await coordinator.async_config_entry_first_refresh()
        if register_hubs:
            _register_hub_devices(hass, entry, coordinator)

        captured = []
        async_add_entities = MagicMock(side_effect=lambda ents, **kw: captured.extend(ents))
        await async_setup_entry(hass, entry, async_add_entities)

        return self._register(hass, entry, captured), entry

    @staticmethod
    def _register(hass, entry, entities) -> dict:
        """Attach the platform and register each entity's device, as EntityPlatform does."""
        registry = dr.async_get(hass)
        platform = SimpleNamespace(hass=hass, config_entry=entry)
        rows = {}
        for entity in entities:
            entity.platform = platform
            rows[entity._sensor_key] = registry.async_get_or_create(config_entry_id=entry.entry_id, **entity.device_info)
        return rows

    @staticmethod
    def _hub_row(hass, entry, mid=200):
        rows = dr.async_entries_for_config_entry(dr.async_get(hass), entry.entry_id)
        return next(row for row in rows if (DOMAIN, f"hub_100_{mid}") in row.identifiers)

    @pytest.mark.asyncio
    async def test_hub_paired_child_keeps_its_link_and_wrapper_child_has_none(self, hass, caplog):
        """Parenting asserted on the device rows the registry built from the
        entities the valve platform's own async_setup_entry produced off a real
        coordinator refresh, not on a DeviceInfo assembled by the test."""
        rows, entry = await self._build(hass)

        assert rows["100_200_1"].via_device_id == self._hub_row(hass, entry).id
        assert rows["100_201_1"].via_device_id is None
        assert not [record for record in caplog.records if "via_device" in record.getMessage()]

    @pytest.mark.asyncio
    async def test_a_hub_registered_late_is_linked_on_the_next_setup(self, hass, caplog):
        """With no hub row yet the child is added parentless rather than
        dropped; once the hub row exists, the next setup links the same row."""
        rows, entry = await self._build(hass, register_hubs=False)
        child = rows["100_200_1"]
        assert child.via_device_id is None

        hub = RainPointHubDevice({"hid": 100, "mid": 200, "name": "Hub A"})
        hub_row = dr.async_get(hass).async_get_or_create(config_entry_id=entry.entry_id, **hub.device_info)
        relinked = await self._build(hass, entry)
        assert relinked[0]["100_200_1"].id == child.id
        assert relinked[0]["100_200_1"].via_device_id == hub_row.id
        assert not [record for record in caplog.records if "via_device" in record.getMessage()]

    @pytest.mark.asyncio
    async def test_an_existing_install_keeps_its_rows_and_link(self, hass):
        """A registry already holding the hub and its linked child is reused
        as is: same row ids, same link, no second device."""
        first, entry = await self._build(hass)
        hub_id = self._hub_row(hass, entry).id
        second = await self._build(hass, entry)

        assert second[0]["100_200_1"].id == first["100_200_1"].id
        assert second[0]["100_200_1"].via_device_id == hub_id
        assert self._hub_row(hass, entry).id == hub_id

    @pytest.mark.asyncio
    async def test_parenting_is_a_per_record_property_not_a_per_home_one(self, hass):
        """Guards the assumption-delta decision this plan is built on: within
        a single home, the parenting outcome is a property of the top-level
        record that carries a sub-device, not of the home itself. Asserts
        that two distinct top-level records in one home, off a single poll,
        produce two distinct parenting outcomes, and that their sub-device
        identifiers differ only by the mid segment, so no unique_id or device
        identifier shape changed. This goes red the moment a future
        change collapses the per-record question back onto the per-home key.

        The debt this test used to record is closed. Hub device identity and
        every hub entity unique_id now carry the carrying record's mid
        alongside the home id, so a second real hub in one home no longer
        produces duplicate unique_ids for Home Assistant to reject. The
        sibling assertions on hub identity live in tests/test_hub_identity.py;
        this test stays scoped to sub-device parenting.
        """
        rows, _ = await self._build(hass)

        linked = rows["100_200_1"]
        parentless = rows["100_201_1"]

        assert linked.via_device_id is not None
        assert parentless.via_device_id is None

        linked_id = next(t for t in linked.identifiers if t[0] == DOMAIN)
        parentless_id = next(t for t in parentless.identifiers if t[0] == DOMAIN)
        linked_hid, linked_mid, linked_addr = linked_id[1].split("_")
        parentless_hid, parentless_mid, parentless_addr = parentless_id[1].split("_")
        assert linked_hid == parentless_hid
        assert linked_addr == parentless_addr
        assert linked_mid != parentless_mid


class TestHubAndSubDeviceReadMidTheSameWay:
    """The two sides of the hub link must be structurally unable to disagree."""

    async def test_both_sides_direct_index_mid(self, hass):
        """A hub record with no mid raises here rather than emitting a parent
        identifier that no device row carries.

        The raise is the point, and so is its counterpart. is_hub_record tests
        did, mac, productKey and model, and never mid, so nothing guarantees a
        hub record carries one. If the hub side degraded to a placeholder while
        build_sub_device_info kept its direct index, every sub-device would
        point at a parent that does not exist and Home Assistant would orphan
        the lot. Both sides read mid the same way instead, so the sub-device's
        hub link below resolves to exactly the row a hub built from the same
        record registers.
        """
        hub_info = {"hid": 100, "name": "Hub", "model": "HTV0540FRF"}
        with pytest.raises(KeyError):
            RainPointHubDevice(hub_info)

        entry = MockConfigEntry(domain=DOMAIN)
        entry.add_to_hass(hass)
        hub = RainPointHubDevice({**hub_info, "mid": 200})
        hub_row = dr.async_get(hass).async_get_or_create(config_entry_id=entry.entry_id, **hub.device_info)

        platform = SimpleNamespace(hass=hass, config_entry=entry)
        sub_info = {"hid": 100, "mid": 200, "addr": 1, "sub_name": "Valve", "model": MODEL_VALVE_245}
        assert registered_parent_id(platform, build_sub_device_info(sub_info, platform)) == hub_row.id
