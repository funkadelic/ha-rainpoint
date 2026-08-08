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

from unittest.mock import MagicMock

import pytest
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.rainpoint.const import DOMAIN
from custom_components.rainpoint.diagnostic_sensors import RainPointBatterySensor
from custom_components.rainpoint.number import RainPointZoneDurationNumber
from custom_components.rainpoint.sensor import RainPointMoisturePercentSensor, RainPointZoneStateSensor
from custom_components.rainpoint.valve import RainPointValveEntity

assert not isinstance(er.async_get, MagicMock), "entity_registry is stubbed; every proof here would be a no-op"
assert not isinstance(dr.async_get, MagicMock), "device_registry is stubbed; every proof here would be a no-op"

HID = 100
MID = 200
ADDR = 1


def _make_entry(hass):
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"area_code": "1", "email": "a@b.c", "password": "pw", "hids": [HID], "token": "tok"},
        options={},
    )
    entry.add_to_hass(hass)
    return entry


def _make_renamed_device(hass, device_registry, entry, *, sub_name, display_name):
    """Build a device registry row whose display name may diverge from sub_name.

    ``sub_name`` is accepted for readability at the call site (it is what the
    entity being tested was built from) even though only ``display_name`` feeds
    the created row; the two are compared by the caller, not by this helper.
    """
    del sub_name
    return device_registry.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, f"{HID}_{MID}_{ADDR}")},
        name=display_name,
    )


def _compose(hass, entity, device):
    """Wrap entity_registry._async_get_full_entity_name, reading only the code's contribution.

    ``name=None`` is passed deliberately: that argument is the user's own
    registry override, and passing it None is what makes the composed string
    reflect only what the code supplies (``original_name`` and
    ``has_entity_name``), not a user's own override.
    """
    return er._async_get_full_entity_name(
        hass,
        device_id=device.id,
        fallback="unnamed",
        has_entity_name=getattr(entity, "_attr_has_entity_name", False),
        name=None,
        original_name=entity._attr_name,
    )


def _sensor_info(sub_name):
    return {
        "hid": HID,
        "mid": MID,
        "addr": ADDR,
        "sub_name": sub_name,
        "model": "HTV210B",
    }


def _mock_coordinator():
    coordinator = MagicMock()
    coordinator.data = {"sensors": {}}
    return coordinator


class TestRenamedDeviceComposesShortName:
    """The behavioural proof that a renamed device still composes correctly.

    The maintainer owned two HTV210Bs and renamed one to "HTV210B (Hub paired)"
    to tell them apart -- an ordinary user action the integration cannot
    prevent. Before this phase, composing against that renamed device still
    surfaced the un-stripped "HTV210B Zone 1" because has_entity_name was False
    and the device was never consulted.
    """

    @pytest.mark.asyncio
    async def test_valve_composes_against_renamed_device(self, hass, device_registry):
        entry = _make_entry(hass)
        device = _make_renamed_device(hass, device_registry, entry, sub_name="HTV210B", display_name="HTV210B (Hub paired)")
        valve = RainPointValveEntity(_mock_coordinator(), "100_200_1", _sensor_info("HTV210B"), 1)

        assert _compose(hass, valve, device) == "HTV210B (Hub paired) Zone 1"

    @pytest.mark.asyncio
    async def test_duration_composes_against_renamed_device(self, hass, device_registry):
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
        entry = _make_entry(hass)
        device = _make_renamed_device(
            hass, device_registry, entry, sub_name="HCS026FRF", display_name="HCS026FRF Moisture Sensor"
        )
        sensor_info = _sensor_info("HCS026FRF")
        sensor = RainPointMoisturePercentSensor(_mock_coordinator(), "100_200_1", sensor_info, "100_200_1", simple=True)

        assert _compose(hass, sensor, device) == "HCS026FRF Moisture Sensor Moisture Percent"

    @pytest.mark.asyncio
    async def test_battery_composes_against_renamed_device(self, hass, device_registry):
        entry = _make_entry(hass)
        device = _make_renamed_device(
            hass, device_registry, entry, sub_name="HCS026FRF", display_name="HCS026FRF Moisture Sensor"
        )
        sensor_info = _sensor_info("HCS026FRF")
        sensor = RainPointBatterySensor(_mock_coordinator(), "100_200_1", sensor_info, "100_200_1")

        assert _compose(hass, sensor, device) == "HCS026FRF Moisture Sensor Battery"


class TestEveryConvertedPlatformSetsHasEntityName:
    def test_valve_and_duration_carry_the_flag(self):
        valve = RainPointValveEntity(_mock_coordinator(), "100_200_1", _sensor_info("HTV210B"), 1)
        number = RainPointZoneDurationNumber(_mock_coordinator(), "100_200_1", _sensor_info("HTV210B"), 1)

        assert valve._attr_has_entity_name is True
        assert number._attr_has_entity_name is True

    def test_sensor_tree_platforms_carry_the_flag_by_inheritance(self):
        """None of these three declares the flag on its own class (checked by

        source grep in this plan's acceptance criteria); each inherits it from
        RainPointSubDeviceEntity or RainPointSensorBase.
        """
        sensor_info = _sensor_info("HCS026FRF")
        coordinator = _mock_coordinator()
        moisture = RainPointMoisturePercentSensor(coordinator, "100_200_1", sensor_info, "100_200_1", simple=True)
        battery = RainPointBatterySensor(coordinator, "100_200_1", sensor_info, "100_200_1")
        zone_state = RainPointZoneStateSensor(coordinator, "100_200_1", sensor_info, "100_200_1", 1)

        assert moisture._attr_has_entity_name is True
        assert battery._attr_has_entity_name is True
        assert zone_state._attr_has_entity_name is True
        assert "_attr_has_entity_name" not in RainPointMoisturePercentSensor.__dict__
        assert "_attr_has_entity_name" not in RainPointBatterySensor.__dict__
        assert "_attr_has_entity_name" not in RainPointZoneStateSensor.__dict__


class TestUserOverrideWins:
    @pytest.mark.asyncio
    async def test_user_set_name_override_composes_unchanged(self, hass, device_registry):
        """An entity registry row that already carries a user override keeps it."""
        entry = _make_entry(hass)
        device = _make_renamed_device(hass, device_registry, entry, sub_name="HTV210B", display_name="HTV210B (Hub paired)")
        valve = RainPointValveEntity(_mock_coordinator(), "100_200_1", _sensor_info("HTV210B"), 1)

        composed = er._async_get_full_entity_name(
            hass,
            device_id=device.id,
            fallback="unnamed",
            has_entity_name=getattr(valve, "_attr_has_entity_name", False),
            name="My Custom Zone Name",
            original_name=valve._attr_name,
        )

        assert composed == "My Custom Zone Name"


class TestUniqueIdUnchanged:
    def test_valve_and_duration_unique_ids_are_byte_identical(self):
        valve = RainPointValveEntity(_mock_coordinator(), "100_200_1", _sensor_info("HTV210B"), 1)
        number = RainPointZoneDurationNumber(_mock_coordinator(), "100_200_1", _sensor_info("HTV210B"), 1)

        assert valve._attr_unique_id == "rainpoint_100_200_1_zone1"
        assert number._attr_unique_id == "rainpoint_100_200_1_zone1_duration"
