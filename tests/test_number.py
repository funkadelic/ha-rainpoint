"""Tests for number entity platform (number.py)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.rainpoint.const import CONF_GENERIC_CONTROL_ENABLED, DOMAIN, MODEL_VALVE_345
from custom_components.rainpoint.coordinator import SILENT_DATA_TYPE
from custom_components.rainpoint.number import (
    DURATION_DEFAULT_MINUTES,
    DURATION_MAX_MINUTES,
    DURATION_MIN_MINUTES,
    DURATION_STEP_MINUTES,
    RainPointGenericZoneDurationNumber,
    RainPointZoneDurationNumber,
    build_generic_duration_entities,
)
from tests.helpers import make_coordinator_data, make_sensor_coordinator, make_sensor_entry

# Real, non-hand-written catalog variants reused from tests/test_generic_control.py's
# own fixtures, so the companion duration builder is proven against the same
# ground truth rather than a synthetic one.
ANCHOR_MODEL = "HTV103FRF"  # single-zone: portNumber 1, CTL_WATER on dpPort 1
ANCHOR_MODEL_CODE = 31
TWO_ZONE_MODEL = "HTV214FRF"  # two-zone: portNumber 2, CTL_WATER on dpPort 1 and 2
TWO_ZONE_MODEL_CODE = 288
SOCKET_MODEL = "HWG004WRF"  # CTL_SOCK only -- no valve zone, so no duration companion
SOCKET_MODEL_CODE = 34


def _unknown_data(model: str) -> dict:
    """Build the {"type": "unknown", ...} decoded-payload shape the control gate requires."""
    return {"type": "unknown", "model": model, "raw_value": "11#00", "generic": {"fields": [], "field_names": []}}


def _generic_control_sensor_info(model: str, model_code: int, sub_name: str = "Valve Hub 1") -> dict:
    entry = make_sensor_entry(hid=100, mid=200, addr=1, model=model, sub_name=sub_name, data=_unknown_data(model))
    entry["model_code"] = model_code
    entry["device_name"] = "dev1"
    entry["product_key"] = "pk1"
    return entry


def _make_number(current_value=10.0, firmware_version="1.0"):
    """Create a RainPointZoneDurationNumber with mock coordinator, bypassing __init__."""
    sensor_key = "100_200_1"
    sensor_info = {
        "hid": 100,
        "mid": 200,
        "addr": 1,
        "sub_name": "Valve Hub 1",
        "model": "HTV245FRF",
    }
    mock_coordinator = make_sensor_coordinator(
        model="HTV245FRF",
        data={},
        sub_name="Valve Hub 1",
        firmware_version=firmware_version,
    )

    num = RainPointZoneDurationNumber.__new__(RainPointZoneDurationNumber)
    num.coordinator = mock_coordinator
    num._sensor_key = sensor_key
    num._sensor_info = sensor_info
    num._zone_num = 1
    num._current_value = current_value
    num._attr_unique_id = "rainpoint_100_200_1_zone1_duration"
    num._attr_name = "Valve Hub 1 Zone 1 Duration"
    num.hass = MagicMock()
    num.async_write_ha_state = MagicMock()
    return num


class TestNumberEntity:
    """Tests for RainPointZoneDurationNumber."""

    def test_device_info_carries_identity_and_hub_link(self):
        """The duration entity resolves to the same device card as its valve."""
        number = _make_number()
        info = number.device_info
        assert info["serial_number"] == "200_1"
        assert info["via_device"] == (DOMAIN, "hub_100")

    def test_native_value_returns_current(self):
        """native_value should return _current_value."""
        num = _make_number(current_value=10.0)
        assert num.native_value == 10.0

    @pytest.mark.asyncio
    async def test_set_native_value_updates(self):
        """async_set_native_value should update _current_value and write state."""
        num = _make_number(current_value=10.0)
        await num.async_set_native_value(30.0)
        assert num._current_value == 30.0
        num.async_write_ha_state.assert_called_once()

    def test_unique_id_format(self):
        """unique_id should end with '_duration'."""
        num = _make_number()
        assert num._attr_unique_id.endswith("_duration")

    def test_device_info_manufacturer(self):
        """device_info should have manufacturer == 'RainPoint'."""
        num = _make_number()
        assert num.device_info["manufacturer"] == "RainPoint"

    def test_device_info_identifiers(self):
        """device_info should contain the correct identifier tuple."""
        num = _make_number()
        identifiers = num.device_info["identifiers"]
        assert (DOMAIN, "100_200_1") in identifiers

    def test_extra_state_attributes_firmware(self):
        """extra_state_attributes should contain firmware_version when set."""
        num = _make_number(firmware_version="2.0")
        attrs = num.extra_state_attributes
        assert attrs["firmware_version"] == "2.0"

    def test_extra_state_attributes_no_firmware_when_missing(self):
        """extra_state_attributes should not contain firmware_version when not set."""
        num = _make_number(firmware_version=None)
        # Firmware version is None, so it should not appear
        # (the code checks `if firmware_version:`)
        attrs = num.extra_state_attributes
        assert "firmware_version" not in attrs

    def test_min_max_step(self):
        """Number entity class attributes should have correct min/max/step."""
        assert RainPointZoneDurationNumber._attr_native_min_value == 1
        assert RainPointZoneDurationNumber._attr_native_max_value == 60
        assert RainPointZoneDurationNumber._attr_native_step == 1

    def test_duration_constants(self):
        """Module-level constants should have expected values."""
        assert DURATION_MIN_MINUTES == 1
        assert DURATION_MAX_MINUTES == 60
        assert DURATION_STEP_MINUTES == 1
        assert DURATION_DEFAULT_MINUTES == 10

    @pytest.mark.asyncio
    async def test_set_native_value_stores_float(self):
        """async_set_native_value should store value as given.

        Note: the implementation does not coerce int -> float; HA's number
        platform is expected to pass a float. Passing an int here exercises
        the direct-assignment path and documents that no coercion occurs.
        """
        num = _make_number(current_value=10.0)
        await num.async_set_native_value(15)  # int
        assert num._current_value == 15

    def test_extra_state_attributes_device_timestamp_present(self):
        """device_timestamp in decoded data flows through to attrs."""
        num = _make_number()
        # Inject a data dict with device_timestamp
        num.coordinator.data = {
            "sensors": {
                num._sensor_key: {
                    "firmware_version": "1.0",
                    "data": {
                        "device_timestamp": "2024-01-01T00:00:00+00:00",
                        "timestamp_method": "rtc",
                        "timestamp_source": "device",
                    },
                }
            }
        }
        attrs = num.extra_state_attributes
        assert attrs["device_timestamp"] == "2024-01-01T00:00:00+00:00"
        assert attrs["timestamp_method"] == "rtc"
        assert attrs["timestamp_source"] == "device"

    def test_extra_state_attributes_server_timestamp_fallback(self):
        """When only server_timestamp present, it is copied into device_timestamp."""
        num = _make_number()
        num.coordinator.data = {
            "sensors": {
                num._sensor_key: {
                    "firmware_version": "1.0",
                    "data": {
                        "server_timestamp": "2024-06-01T00:00:00+00:00",
                        "timestamp_source": "server",
                    },
                }
            }
        }
        attrs = num.extra_state_attributes
        assert attrs["device_timestamp"] == "2024-06-01T00:00:00+00:00"
        assert attrs["timestamp_source"] == "server"


class TestNumberConstructor:
    """Direct constructor coverage for __init__ (lines 78-90)."""

    def test_constructor_builds_unique_id_and_name(self):
        """__init__ assembles unique_id + name from sensor_info + zone_num."""
        import custom_components.rainpoint.number as num_mod

        real_init = num_mod.RainPointZoneDurationNumber.__dict__["__init__"]

        sensor_info = {
            "hid": 100,
            "mid": 200,
            "addr": 1,
            "sub_name": "Front Yard",
            "model": "HTV245FRF",
        }
        inst = object.__new__(num_mod.RainPointZoneDurationNumber)
        coord = MagicMock()

        real_init(inst, coord, "100_200_1", sensor_info, 2)

        assert inst._sensor_key == "100_200_1"
        assert inst._zone_num == 2
        assert inst._current_value == num_mod.DURATION_DEFAULT_MINUTES
        assert inst._attr_unique_id == "rainpoint_100_200_1_zone2_duration"
        assert inst._attr_name == "Front Yard Zone 2 Duration"

    def test_constructor_fallback_sub_name(self):
        """Missing sub_name falls back to 'Valve Hub {addr}'."""
        import custom_components.rainpoint.number as num_mod

        real_init = num_mod.RainPointZoneDurationNumber.__dict__["__init__"]

        sensor_info = {"hid": 9, "mid": 8, "addr": 7, "model": "M"}  # no sub_name
        inst = object.__new__(num_mod.RainPointZoneDurationNumber)
        coord = MagicMock()
        real_init(inst, coord, "9_8_7", sensor_info, 1)

        assert "Valve Hub 7" in inst._attr_name


class TestNumberAsyncAddedToHass:
    """Cover async_added_to_hass restore logic (lines 93-106)."""

    @pytest.mark.asyncio
    async def test_restore_valid_value(self):
        """A valid last_state within bounds restores into _current_value."""
        from unittest.mock import AsyncMock

        num = _make_number(current_value=10.0)
        last_state = MagicMock()
        last_state.state = "25.0"
        num.async_get_last_state = AsyncMock(return_value=last_state)

        import custom_components.rainpoint.number as num_mod

        real_fn = num_mod.RainPointZoneDurationNumber.async_added_to_hass
        await real_fn(num)

        assert num._current_value == 25.0

    @pytest.mark.asyncio
    async def test_restore_out_of_range_ignored(self):
        """A last_state outside bounds is discarded; default stays."""
        from unittest.mock import AsyncMock

        num = _make_number(current_value=10.0)
        last_state = MagicMock()
        last_state.state = "999.0"  # way above max
        num.async_get_last_state = AsyncMock(return_value=last_state)

        import custom_components.rainpoint.number as num_mod

        real_fn = num_mod.RainPointZoneDurationNumber.async_added_to_hass
        await real_fn(num)

        assert num._current_value == 10.0  # unchanged

    @pytest.mark.asyncio
    async def test_restore_non_numeric_swallowed(self):
        """A non-numeric last_state.state is caught by ValueError/TypeError."""
        from unittest.mock import AsyncMock

        num = _make_number(current_value=10.0)
        last_state = MagicMock()
        last_state.state = "not-a-number"
        num.async_get_last_state = AsyncMock(return_value=last_state)

        import custom_components.rainpoint.number as num_mod

        real_fn = num_mod.RainPointZoneDurationNumber.async_added_to_hass
        await real_fn(num)

        assert num._current_value == 10.0

    @pytest.mark.asyncio
    async def test_restore_no_last_state(self):
        """When async_get_last_state returns None, default value is kept."""
        from unittest.mock import AsyncMock

        num = _make_number(current_value=10.0)
        num.async_get_last_state = AsyncMock(return_value=None)

        import custom_components.rainpoint.number as num_mod

        real_fn = num_mod.RainPointZoneDurationNumber.async_added_to_hass
        await real_fn(num)

        assert num._current_value == 10.0


class TestNumberSetupEntry:
    """Cover async_setup_entry (lines 30-53)."""

    @pytest.mark.asyncio
    async def test_setup_entry_creates_one_number_per_zone(self):
        """One RainPointZoneDurationNumber entity is added per zone per valve sensor."""
        from custom_components.rainpoint.number import async_setup_entry

        coord = MagicMock()
        coord.data = {
            "sensors": {
                "1_2_3": {
                    "hid": 1,
                    "mid": 2,
                    "addr": 3,
                    "sub_name": "Hub",
                    "model": "HTV245FRF",
                    "data": {"zones": {1: {}, 2: {}, 3: {}}},
                }
            }
        }
        hass = MagicMock()
        entry = MagicMock()
        entry.entry_id = "e"
        hass.data = {DOMAIN: {"e": {"coordinator": coord}}}

        added = MagicMock()
        await async_setup_entry(hass, entry, added)

        added.assert_called_once()
        entities = added.call_args[0][0]
        assert len(entities) == 3

    @pytest.mark.asyncio
    async def test_setup_entry_creates_numbers_for_valve_345(self):
        """HTV345FRF creates one duration number per reported zone."""
        from custom_components.rainpoint.number import async_setup_entry

        coord = MagicMock()
        coord.data = {
            "sensors": {
                "1_2_3": {
                    "hid": 1,
                    "mid": 2,
                    "addr": 3,
                    "sub_name": "HTV345",
                    "model": MODEL_VALVE_345,
                    "data": {"zones": {1: {}, 2: {}, 3: {}}},
                }
            }
        }
        hass = MagicMock()
        entry = MagicMock()
        entry.entry_id = "e"
        hass.data = {DOMAIN: {"e": {"coordinator": coord}}}

        added = MagicMock()
        await async_setup_entry(hass, entry, added)

        added.assert_called_once()
        entities = added.call_args[0][0]
        assert [entity._zone_num for entity in entities] == [1, 2, 3]
        assert all(entity._sensor_info["model"] == MODEL_VALVE_345 for entity in entities)

    @pytest.mark.asyncio
    async def test_setup_entry_skips_non_valve_models(self):
        """Non-valve models are skipped and produce no number entities."""
        from custom_components.rainpoint.number import async_setup_entry

        coord = MagicMock()
        coord.data = {
            "sensors": {
                "k": {
                    "hid": 1,
                    "mid": 2,
                    "addr": 3,
                    "model": "HCS021FRF",  # not a valve
                    "data": {"zones": {1: {}}},
                }
            }
        }
        hass = MagicMock()
        entry = MagicMock()
        entry.entry_id = "e"
        hass.data = {DOMAIN: {"e": {"coordinator": coord}}}

        added = MagicMock()
        await async_setup_entry(hass, entry, added)

        # async_add_entities not called when entities list is empty
        added.assert_not_called()

    @pytest.mark.asyncio
    async def test_setup_entry_no_zones_skips(self):
        """A valve sensor with no zones produces no entities."""
        from custom_components.rainpoint.number import async_setup_entry

        coord = MagicMock()
        coord.data = {
            "sensors": {
                "k": {
                    "hid": 1,
                    "mid": 2,
                    "addr": 3,
                    "model": "HTV245FRF",
                    "data": {"zones": {}},
                }
            }
        }
        hass = MagicMock()
        entry = MagicMock()
        entry.entry_id = "e"
        hass.data = {DOMAIN: {"e": {"coordinator": coord}}}

        added = MagicMock()
        await async_setup_entry(hass, entry, added)

        added.assert_not_called()

    @pytest.mark.asyncio
    async def test_setup_entry_creates_no_number_for_a_silent_entry(self):
        """A valve model with no status at all (raw_status={}, D-11/D-12) has no
        zones to walk, so it produces no duration number entity."""
        from custom_components.rainpoint.number import async_setup_entry

        coord = MagicMock()
        coord.data = {
            "sensors": {
                "1_2_3": {
                    "hid": 1,
                    "mid": 2,
                    "addr": 3,
                    "sub_name": "Hub",
                    "model": "HTV245FRF",
                    "raw_status": {},
                    "data": {"type": SILENT_DATA_TYPE, "silent_state": "never_reported"},
                }
            }
        }
        hass = MagicMock()
        entry = MagicMock()
        entry.entry_id = "e"
        entry.options = {}
        hass.data = {DOMAIN: {"e": {"coordinator": coord}}}

        added = MagicMock()
        await async_setup_entry(hass, entry, added)

        added.assert_not_called()


# ---------------------------------------------------------------------------
# build_generic_duration_entities (companion duration entities for generic
# control valve zones)
# ---------------------------------------------------------------------------


def _make_generic_coordinator(sensor_key: str, sensor_info: dict):
    coord = MagicMock()
    coord.data = make_coordinator_data(sensors={sensor_key: sensor_info})
    return coord


class TestBuildGenericDurationEntities:
    def test_single_zone_anchor_yields_one_companion(self):
        sensor_info = _generic_control_sensor_info(ANCHOR_MODEL, ANCHOR_MODEL_CODE)
        coordinator = _make_generic_coordinator("100_200_1", sensor_info)

        entities = build_generic_duration_entities(coordinator, "100_200_1", sensor_info, "100_200_1")

        assert len(entities) == 1
        assert isinstance(entities[0], RainPointGenericZoneDurationNumber)
        assert entities[0]._attr_unique_id == "rainpoint_100_200_1_generic_ctl_ctl_water_p1_duration"

    def test_two_zone_anchor_yields_two_companions_matching_the_valves_unique_ids(self):
        """A two-zone anchor variant's companions are exactly its two valve unique_ids, each plus the duration suffix."""
        from custom_components.rainpoint.generic_control import build_generic_valve_entities

        sensor_info = _generic_control_sensor_info(TWO_ZONE_MODEL, TWO_ZONE_MODEL_CODE, sub_name="Yard")
        coordinator = _make_generic_coordinator("1_2_1", sensor_info)

        valves = build_generic_valve_entities(coordinator, "1_2_1", sensor_info, "1_2_1")
        durations = build_generic_duration_entities(coordinator, "1_2_1", sensor_info, "1_2_1")

        assert len(valves) == 2
        assert len(durations) == 2
        assert {d._attr_unique_id for d in durations} == {f"{v._attr_unique_id}_duration" for v in valves}

    def test_socket_only_variant_yields_no_companions(self):
        """CTL_SOCK carries no run duration -- only valve zones get a companion."""
        sensor_info = _generic_control_sensor_info(SOCKET_MODEL, SOCKET_MODEL_CODE, sub_name="Outlet 1")
        coordinator = _make_generic_coordinator("300_400_1", sensor_info)

        assert build_generic_duration_entities(coordinator, "300_400_1", sensor_info, "300_400_1") == []

    def test_non_unknown_payload_yields_nothing(self):
        sensor_info = _generic_control_sensor_info(ANCHOR_MODEL, ANCHOR_MODEL_CODE)
        sensor_info["data"] = {"type": "valve"}
        coordinator = _make_generic_coordinator("100_200_1", sensor_info)

        assert build_generic_duration_entities(coordinator, "100_200_1", sensor_info, "100_200_1") == []


# ---------------------------------------------------------------------------
# RainPointGenericZoneDurationNumber construction and behavior
# ---------------------------------------------------------------------------


class TestGenericZoneDurationNumberConstruction:
    def test_single_zone_name_omits_zone_segment(self):
        sensor_info = _generic_control_sensor_info(ANCHOR_MODEL, ANCHOR_MODEL_CODE, sub_name="Garden Valve")
        coordinator = _make_generic_coordinator("100_200_1", sensor_info)

        entities = build_generic_duration_entities(coordinator, "100_200_1", sensor_info, "100_200_1")

        assert entities[0]._attr_name == "Garden Valve CTL_WATER Duration (unverified)"

    def test_two_zone_names_include_the_zone_segment(self):
        sensor_info = _generic_control_sensor_info(TWO_ZONE_MODEL, TWO_ZONE_MODEL_CODE, sub_name="Yard")
        coordinator = _make_generic_coordinator("1_2_1", sensor_info)

        entities = build_generic_duration_entities(coordinator, "1_2_1", sensor_info, "1_2_1")

        names = sorted(e._attr_name for e in entities)
        assert names == [
            "Yard Zone 1 CTL_WATER Duration (unverified)",
            "Yard Zone 2 CTL_WATER Duration (unverified)",
        ]

    def test_icon_is_the_generic_control_marker_icon(self):
        from custom_components.rainpoint.const import GENERIC_CONTROL_MARKER_ICON

        sensor_info = _generic_control_sensor_info(ANCHOR_MODEL, ANCHOR_MODEL_CODE)
        coordinator = _make_generic_coordinator("100_200_1", sensor_info)

        entities = build_generic_duration_entities(coordinator, "100_200_1", sensor_info, "100_200_1")

        assert entities[0]._attr_icon == GENERIC_CONTROL_MARKER_ICON

    def test_bounds_match_the_trusted_duration_entity(self):
        assert RainPointGenericZoneDurationNumber._attr_native_min_value == DURATION_MIN_MINUTES
        assert RainPointGenericZoneDurationNumber._attr_native_max_value == DURATION_MAX_MINUTES
        assert RainPointGenericZoneDurationNumber._attr_native_step == DURATION_STEP_MINUTES

    def test_default_value_matches_the_trusted_duration_entity(self):
        sensor_info = _generic_control_sensor_info(ANCHOR_MODEL, ANCHOR_MODEL_CODE)
        coordinator = _make_generic_coordinator("100_200_1", sensor_info)

        entities = build_generic_duration_entities(coordinator, "100_200_1", sensor_info, "100_200_1")

        assert entities[0].native_value == DURATION_DEFAULT_MINUTES

    def test_device_info_matches_the_sub_device_card(self):
        sensor_info = _generic_control_sensor_info(ANCHOR_MODEL, ANCHOR_MODEL_CODE)
        coordinator = _make_generic_coordinator("100_200_1", sensor_info)

        entities = build_generic_duration_entities(coordinator, "100_200_1", sensor_info, "100_200_1")

        info = entities[0].device_info
        assert info["identifiers"] == {(DOMAIN, "100_200_1")}
        assert info["manufacturer"] == "RainPoint"
        assert info["model"] == ANCHOR_MODEL


class TestGenericZoneDurationNumberBehavior:
    def _build(self):
        sensor_info = _generic_control_sensor_info(ANCHOR_MODEL, ANCHOR_MODEL_CODE)
        coordinator = _make_generic_coordinator("100_200_1", sensor_info)
        entities = build_generic_duration_entities(coordinator, "100_200_1", sensor_info, "100_200_1")
        entity = entities[0]
        entity.async_write_ha_state = MagicMock()
        return entity

    @pytest.mark.asyncio
    async def test_set_native_value_updates_and_writes_state(self):
        entity = self._build()
        await entity.async_set_native_value(45.0)
        assert entity.native_value == 45.0
        entity.async_write_ha_state.assert_called_once()

    @pytest.mark.asyncio
    async def test_restore_valid_value_within_bounds(self):
        entity = self._build()
        last_state = MagicMock()
        last_state.state = "25.0"
        entity.async_get_last_state = AsyncMock(return_value=last_state)

        await entity.async_added_to_hass()

        assert entity.native_value == 25.0

    @pytest.mark.asyncio
    async def test_restore_out_of_range_is_discarded(self):
        entity = self._build()
        last_state = MagicMock()
        last_state.state = "999.0"
        entity.async_get_last_state = AsyncMock(return_value=last_state)

        await entity.async_added_to_hass()

        assert entity.native_value == DURATION_DEFAULT_MINUTES

    @pytest.mark.asyncio
    async def test_restore_unparseable_value_is_discarded(self):
        entity = self._build()
        last_state = MagicMock()
        last_state.state = "not-a-number"
        entity.async_get_last_state = AsyncMock(return_value=last_state)

        await entity.async_added_to_hass()

        assert entity.native_value == DURATION_DEFAULT_MINUTES

    @pytest.mark.asyncio
    async def test_restore_no_last_state_keeps_default(self):
        entity = self._build()
        entity.async_get_last_state = AsyncMock(return_value=None)

        await entity.async_added_to_hass()

        assert entity.native_value == DURATION_DEFAULT_MINUTES

    def test_extra_state_attributes_firmware(self):
        entity = self._build()
        assert entity.extra_state_attributes["firmware_version"] == "1.0.0"

    def test_extra_state_attributes_no_firmware_when_missing(self):
        entity = self._build()
        entity.coordinator.data["sensors"]["100_200_1"]["firmware_version"] = None
        assert "firmware_version" not in entity.extra_state_attributes

    def test_extra_state_attributes_device_timestamp(self):
        entity = self._build()
        entity.coordinator.data["sensors"]["100_200_1"]["data"]["device_timestamp"] = "2024-01-01T00:00:00+00:00"
        entity.coordinator.data["sensors"]["100_200_1"]["data"]["timestamp_method"] = "rtc"
        entity.coordinator.data["sensors"]["100_200_1"]["data"]["timestamp_source"] = "device"

        attrs = entity.extra_state_attributes

        assert attrs["device_timestamp"] == "2024-01-01T00:00:00+00:00"
        assert attrs["timestamp_method"] == "rtc"
        assert attrs["timestamp_source"] == "device"

    def test_extra_state_attributes_server_timestamp_fallback(self):
        entity = self._build()
        entity.coordinator.data["sensors"]["100_200_1"]["data"]["server_timestamp"] = "2024-06-01T00:00:00+00:00"
        entity.coordinator.data["sensors"]["100_200_1"]["data"]["timestamp_source"] = "server"

        attrs = entity.extra_state_attributes

        assert attrs["device_timestamp"] == "2024-06-01T00:00:00+00:00"
        assert attrs["timestamp_source"] == "server"


class TestNumberSetupEntryGenericControl:
    """Cover the async_setup_entry generic-control branch added in this plan."""

    @pytest.mark.asyncio
    async def test_option_absent_creates_no_companion(self):
        from custom_components.rainpoint.number import async_setup_entry

        sensor_info = _generic_control_sensor_info(ANCHOR_MODEL, ANCHOR_MODEL_CODE)
        coord = _make_generic_coordinator("100_200_1", sensor_info)
        hass = MagicMock()
        entry = MagicMock()
        entry.entry_id = "e"
        entry.options = {}
        hass.data = {DOMAIN: {"e": {"coordinator": coord}}}

        added = MagicMock()
        await async_setup_entry(hass, entry, added)

        added.assert_not_called()

    @pytest.mark.asyncio
    async def test_option_false_creates_no_companion(self):
        from custom_components.rainpoint.number import async_setup_entry

        sensor_info = _generic_control_sensor_info(ANCHOR_MODEL, ANCHOR_MODEL_CODE)
        coord = _make_generic_coordinator("100_200_1", sensor_info)
        hass = MagicMock()
        entry = MagicMock()
        entry.entry_id = "e"
        entry.options = {CONF_GENERIC_CONTROL_ENABLED: False}
        hass.data = {DOMAIN: {"e": {"coordinator": coord}}}

        added = MagicMock()
        await async_setup_entry(hass, entry, added)

        added.assert_not_called()

    @pytest.mark.asyncio
    async def test_option_true_creates_one_companion_for_the_anchor(self):
        from custom_components.rainpoint.number import async_setup_entry

        sensor_info = _generic_control_sensor_info(ANCHOR_MODEL, ANCHOR_MODEL_CODE)
        coord = _make_generic_coordinator("100_200_1", sensor_info)
        hass = MagicMock()
        entry = MagicMock()
        entry.entry_id = "e"
        entry.options = {CONF_GENERIC_CONTROL_ENABLED: True}
        hass.data = {DOMAIN: {"e": {"coordinator": coord}}}

        added = MagicMock()
        await async_setup_entry(hass, entry, added)

        added.assert_called_once()
        entities = added.call_args[0][0]
        assert len(entities) == 1
        assert isinstance(entities[0], RainPointGenericZoneDurationNumber)
        assert entities[0]._attr_unique_id == "rainpoint_100_200_1_generic_ctl_ctl_water_p1_duration"
