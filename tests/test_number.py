"""Tests for number entity platform (number.py)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from homeassistant.exceptions import HomeAssistantError

from custom_components.rainpoint.const import (
    CONF_GENERIC_CONTROL_ENABLED,
    DOMAIN,
    MODEL_HTV210B,
    MODEL_VALVE_245,
    MODEL_VALVE_345,
)
from custom_components.rainpoint.coordinator import SILENT_DATA_TYPE, SILENT_DEBOUNCE_POLLS
from custom_components.rainpoint.entity import LateEntityAdder, late_adders, register_late_adder
from custom_components.rainpoint.generic_control import RUN_STATE_IDENTITY
from custom_components.rainpoint.number import (
    DURATION_DEFAULT_MINUTES,
    DURATION_MAX_MINUTES,
    DURATION_MIN_MINUTES,
    DURATION_STEP_MINUTES,
    RainPointGenericZoneDurationNumber,
    RainPointZoneDurationNumber,
    _RainPointDurationNumberBase,
    build_generic_duration_entities,
)
from tests.helpers import (
    htv210b_hub_devices,
    htv210b_silent_status,
    htv210b_status,
    make_coordinator_data,
    make_sensor_coordinator,
    make_sensor_entry,
    make_valve_zone_status,
    make_valve_zone_status_open,
)

# Real, non-hand-written catalog variants reused from tests/test_generic_control.py's
# own fixtures, so the companion duration builder is proven against the same
# ground truth rather than a synthetic one.
ANCHOR_MODEL = "HTV103FRF"  # single-zone: portNumber 1, CTL_WATER on dpPort 1
ANCHOR_MODEL_CODE = 31
TWO_ZONE_MODEL = "HTV214FRF"  # two-zone: portNumber 2, CTL_WATER on dpPort 1 and 2
TWO_ZONE_MODEL_CODE = 288
SOCKET_MODEL = "HWG004WRF"  # CTL_SOCK only -- no valve zone, so no duration companion
SOCKET_MODEL_CODE = 34


def _unknown_data(model: str, fields: list[dict] | None = None) -> dict:
    """Build the {"type": "unknown", ...} decoded-payload shape the control gate requires.

    fields defaults to [] (the common case: a variant declaring no run-state
    identity at all), and accepts a caller-supplied list so the mid-run
    refusal tests can drive the same run-state field shape
    tests/test_generic_control.py's own _run_state_field builds.
    """
    fields = fields if fields is not None else []
    return {
        "type": "unknown",
        "model": model,
        "raw_value": "11#00",
        "generic": {"fields": fields, "field_names": [f["name"] for f in fields]},
    }


def _generic_control_sensor_info(
    model: str, model_code: int, sub_name: str = "Valve Hub 1", fields: list[dict] | None = None
) -> dict:
    entry = make_sensor_entry(hid=100, mid=200, addr=1, model=model, sub_name=sub_name, data=_unknown_data(model, fields))
    entry["model_code"] = model_code
    entry["device_name"] = "dev1"
    entry["product_key"] = "pk1"
    return entry


def _run_state_field(dp_port: int, value) -> dict:
    """Build one decode_generic field entry for STA_WKSTATE, catalog-annotated.

    Mirrors tests/test_generic_control.py's own _run_state_field so the
    companion duration entity's refusal cases are driven through the same
    field shape its sibling generic valve is proven against, rather than a
    module-local variant that could quietly drift from it.
    """
    return {
        "name": RUN_STATE_IDENTITY,
        "index": 30,
        "dp_id": 30,
        "raw": f"{value:02x}" if isinstance(value, int) and not isinstance(value, bool) else str(value),
        "value": value,
        "catalog": {
            "dp_port": dp_port,
            "data_type": "U8",
            "declared_width": 1,
            "signed": False,
            "port_number": 1,
            "width_mismatch": False,
        },
    }


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
    num._attr_name = "Zone 1 Duration"
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
        assert info["via_device"] == (DOMAIN, "hub_100_200")

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


class TestZoneDurationNumberHubConnectivityTolerance:
    """A hub_connectivity record's presence, absence, or disconnected state
    never changes this entity's own value or identity; only the hub_connected
    key extra_state_attributes already carries through sub_device_attributes
    changes.
    """

    def test_native_value_and_identity_unaffected_by_a_disconnected_hub(self):
        num = _make_number(current_value=15.0)
        num.coordinator.data["hub_connectivity"] = {200: {"state": "disconnected", "changed_at": None, "state_raw": None}}
        assert num.native_value == 15.0
        assert num._attr_unique_id == "rainpoint_100_200_1_zone1_duration"

    def test_extra_state_attributes_carries_hub_connected_false_when_disconnected(self):
        num = _make_number()
        num.coordinator.data["hub_connectivity"] = {200: {"state": "disconnected", "changed_at": None, "state_raw": None}}
        assert num.extra_state_attributes["hub_connected"] is False

    def test_extra_state_attributes_tolerates_a_coordinator_snapshot_with_no_hub_connectivity_key(self):
        num = _make_number()
        assert "hub_connectivity" not in num.coordinator.data
        attrs = num.extra_state_attributes
        assert attrs["hub_connected"] is None
        assert num.native_value == DURATION_DEFAULT_MINUTES


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
        assert inst._attr_name == "Zone 2 Duration"

    def test_constructor_fallback_sub_name(self):
        """Missing sub_name leaves the entity name unaffected.

        The device page falls back to '{model} {addr}' instead, since the
        display name is now composed by Home Assistant from device_info
        rather than interpolated here.
        """
        import custom_components.rainpoint.number as num_mod

        real_init = num_mod.RainPointZoneDurationNumber.__dict__["__init__"]

        sensor_info = {"hid": 9, "mid": 8, "addr": 7, "model": "M"}  # no sub_name
        inst = object.__new__(num_mod.RainPointZoneDurationNumber)
        coord = MagicMock()
        real_init(inst, coord, "9_8_7", sensor_info, 1)

        assert inst._attr_name == "Zone 1 Duration"
        assert inst.device_info["name"] == "M 7"


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

        assert entities[0]._attr_name == "CTL_WATER Duration (unverified)"

    def test_two_zone_names_include_the_zone_segment(self):
        sensor_info = _generic_control_sensor_info(TWO_ZONE_MODEL, TWO_ZONE_MODEL_CODE, sub_name="Yard")
        coordinator = _make_generic_coordinator("1_2_1", sensor_info)

        entities = build_generic_duration_entities(coordinator, "1_2_1", sensor_info, "1_2_1")

        names = sorted(e._attr_name for e in entities)
        assert names == [
            "Zone 1 CTL_WATER Duration (unverified)",
            "Zone 2 CTL_WATER Duration (unverified)",
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


class TestGenericZoneDurationNumberHubConnectivityTolerance:
    """The generic companion entity shares its base class with the trusted
    duration entity, so a disconnected hub or an absent hub_connectivity key
    must leave its value and identity exactly as unaffected.
    """

    def _build(self, hub_connectivity=None):
        sensor_info = _generic_control_sensor_info(ANCHOR_MODEL, ANCHOR_MODEL_CODE)
        coordinator = _make_generic_coordinator("100_200_1", sensor_info)
        if hub_connectivity is not None:
            coordinator.data["hub_connectivity"] = hub_connectivity
        entities = build_generic_duration_entities(coordinator, "100_200_1", sensor_info, "100_200_1")
        entity = entities[0]
        entity.async_write_ha_state = MagicMock()
        return entity

    def test_native_value_unaffected_by_a_disconnected_hub(self):
        entity = self._build(hub_connectivity={200: {"state": "disconnected", "changed_at": None, "state_raw": None}})
        assert entity.native_value == DURATION_DEFAULT_MINUTES

    def test_extra_state_attributes_carries_hub_connected_false_when_disconnected(self):
        entity = self._build(hub_connectivity={200: {"state": "disconnected", "changed_at": None, "state_raw": None}})
        assert entity.extra_state_attributes["hub_connected"] is False


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


class TestNumberAdderRegistration:
    """The number platform publishes its adder where the removal sweep reads."""

    @staticmethod
    def _hass_and_entry():
        """A hass/entry pair whose entry store is a real dict, not a mock."""
        coord = MagicMock()
        coord.data = {"sensors": {}}
        hass = MagicMock()
        entry = MagicMock()
        entry.entry_id = "e"
        entry.options = {}
        hass.data = {DOMAIN: {"e": {"coordinator": coord}}}
        return hass, entry

    @pytest.mark.asyncio
    async def test_setup_registers_exactly_the_adder_it_built(self):
        """The sweep reaches every platform's adder through one entry slot."""
        from custom_components.rainpoint.number import async_setup_entry

        hass, entry = self._hass_and_entry()

        await async_setup_entry(hass, entry, MagicMock())

        registered = late_adders(hass.data[DOMAIN]["e"])
        assert len(registered) == 1
        assert isinstance(registered[0], LateEntityAdder)

    @pytest.mark.asyncio
    async def test_a_second_platforms_registration_appends(self):
        """Three platforms share one slot, and the sweep needs all three."""
        from custom_components.rainpoint.number import async_setup_entry

        hass, entry = self._hass_and_entry()
        store = hass.data[DOMAIN]["e"]
        register_late_adder(store, "an earlier platform")

        await async_setup_entry(hass, entry, MagicMock())

        registered = late_adders(store)
        assert registered[0] == "an earlier platform"
        assert isinstance(registered[1], LateEntityAdder)


class TestSilentUnitGuardRealTimeline:
    """The explicit silent-type guard in number.py's build(), proven through
    the real coordinator-then-setup-then-refresh sequence rather than an
    injected coordinator.data snapshot. Companion to the same-named class in
    tests/test_valve.py.
    """

    @staticmethod
    async def _build_silent_timeline():
        """Construct -> first refresh -> platform setup for an HTV210B that
        never reports. Returns (coordinator, client, hass, entry, captured)."""
        from custom_components.rainpoint.const import CONF_HIDS
        from custom_components.rainpoint.coordinator import RainPointCoordinator
        from custom_components.rainpoint.number import async_setup_entry

        client = AsyncMock()
        client.get_devices_by_hid.return_value = htv210b_hub_devices()
        client.get_multiple_device_status.return_value = htv210b_silent_status()

        entry = MagicMock()
        entry.entry_id = "e1"
        entry.data = {CONF_HIDS: [10]}
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
    async def test_a_silent_from_the_start_htv210b_never_offers_a_duration_entity(self):
        """No duration entity across the whole silence timeline, even once the
        debounce elapses and the entry's model alone would have admitted it."""
        coordinator, _client, _hass, _entry, captured = await self._build_silent_timeline()
        key = "10_20_1"
        assert captured == []

        # Every refresh short of the debounce: the key has no trace at all yet.
        for _ in range(SILENT_DEBOUNCE_POLLS - 2):
            await coordinator.async_refresh()
            assert key not in coordinator.data["sensors"]
            assert captured == []

        # The debounce elapses: a silent entry appears, proving the model-set
        # gate alone would have admitted it, and still no entity is offered.
        await coordinator.async_refresh()
        entry = coordinator.data["sensors"][key]
        assert entry["data"]["type"] == SILENT_DATA_TYPE
        assert entry["model"] == MODEL_HTV210B
        assert captured == []

    @pytest.mark.asyncio
    async def test_duration_companions_appear_on_the_same_poll_as_the_valves(self):
        """A valve can never exist without its duration entity: both platforms'
        late adders promote the same debounced entry on the same refresh, with
        no reload and no second async_setup_entry call."""
        from custom_components.rainpoint.const import CONF_HIDS
        from custom_components.rainpoint.coordinator import RainPointCoordinator
        from custom_components.rainpoint.number import async_setup_entry as number_setup_entry
        from custom_components.rainpoint.valve import async_setup_entry as valve_setup_entry

        client = AsyncMock()
        client.get_devices_by_hid.return_value = htv210b_hub_devices()
        client.get_multiple_device_status.return_value = htv210b_silent_status()

        entry = MagicMock()
        entry.entry_id = "e1"
        entry.data = {CONF_HIDS: [10]}
        entry.options = {}
        hass = MagicMock()
        hass.data = {DOMAIN: {"e1": {}}}

        coordinator = RainPointCoordinator(hass, client, entry)
        hass.data[DOMAIN]["e1"]["coordinator"] = coordinator

        await coordinator.async_config_entry_first_refresh()

        valve_captured = []
        number_captured = []
        await valve_setup_entry(hass, entry, MagicMock(side_effect=lambda ents, **kw: valve_captured.extend(ents)))
        await number_setup_entry(hass, entry, MagicMock(side_effect=lambda ents, **kw: number_captured.extend(ents)))

        for _ in range(SILENT_DEBOUNCE_POLLS - 1):
            await coordinator.async_refresh()
        assert valve_captured == []
        assert number_captured == []

        client.get_multiple_device_status.return_value = htv210b_status()
        await coordinator.async_refresh()

        assert [e._zone_num for e in valve_captured] == [1, 2]
        assert [e._zone_num for e in number_captured] == [1, 2]


# ---------------------------------------------------------------------------
# Mid-run duration refusal: _RainPointDurationNumberBase.async_set_native_value
# ---------------------------------------------------------------------------


def _set_zone_missing(num) -> None:
    """The zones mapping exists but carries no record for this entity's zone."""
    num.coordinator.data["sensors"][num._sensor_key]["data"] = {"zones": {}}


def _set_zone(num, **zone_fields) -> None:
    """Give this entity's own zone the supplied fields, replacing any prior zone data."""
    num.coordinator.data["sensors"][num._sensor_key]["data"] = {"zones": {num._zone_num: dict(zone_fields)}}


class TestDurationRefusalGuard:
    """Every accept and refuse case the mid-run refusal guard must answer.

    Drives the guard directly against an injected coordinator.data snapshot,
    which is appropriate here because each case is a pure function of the
    snapshot with no timing or debounce involved; the state-dependent
    closed-then-open-then-closed sequence itself is proven separately by
    TestDurationRefusalRealTimeline against a real coordinator.
    """

    @pytest.mark.asyncio
    async def test_explicit_open_refuses(self):
        num = _make_number(current_value=10.0)
        _set_zone(num, open=True)

        with pytest.raises(HomeAssistantError):
            await num.async_set_native_value(1.0)

        assert num.native_value == 10.0
        num.async_write_ha_state.assert_not_called()

    @pytest.mark.asyncio
    async def test_explicit_closed_accepts(self):
        num = _make_number(current_value=10.0)
        _set_zone(num, open=False)

        await num.async_set_native_value(1.0)

        assert num.native_value == 1.0
        num.async_write_ha_state.assert_called_once()

    @pytest.mark.asyncio
    async def test_open_none_accepts(self):
        num = _make_number(current_value=10.0)
        _set_zone(num, open=None)

        await num.async_set_native_value(1.0)

        assert num.native_value == 1.0
        num.async_write_ha_state.assert_called_once()

    @pytest.mark.asyncio
    async def test_zone_record_missing_accepts(self):
        num = _make_number(current_value=10.0)
        _set_zone_missing(num)

        await num.async_set_native_value(1.0)

        assert num.native_value == 1.0
        num.async_write_ha_state.assert_called_once()

    @pytest.mark.asyncio
    async def test_sensor_key_absent_accepts(self):
        num = _make_number(current_value=10.0)
        del num.coordinator.data["sensors"][num._sensor_key]

        await num.async_set_native_value(1.0)

        assert num.native_value == 1.0
        num.async_write_ha_state.assert_called_once()

    @pytest.mark.asyncio
    async def test_falsy_data_accepts(self):
        num = _make_number(current_value=10.0)
        num.coordinator.data["sensors"][num._sensor_key]["data"] = None

        await num.async_set_native_value(1.0)

        assert num.native_value == 1.0
        num.async_write_ha_state.assert_called_once()

    @pytest.mark.asyncio
    async def test_silent_entry_accepts(self):
        num = _make_number(current_value=10.0)
        num.coordinator.data["sensors"][num._sensor_key]["data"] = {
            "type": SILENT_DATA_TYPE,
            "silent_state": "never_reported",
        }

        await num.async_set_native_value(1.0)

        assert num.native_value == 1.0
        num.async_write_ha_state.assert_called_once()

    @pytest.mark.asyncio
    async def test_refusal_message_names_the_zone_the_rule_and_the_promise(self):
        num = _make_number(current_value=10.0)
        _set_zone(num, open=True)

        with pytest.raises(HomeAssistantError) as excinfo:
            await num.async_set_native_value(1.0)

        message = str(excinfo.value)
        assert "Zone 1" in message
        assert "watering" in message
        assert "closed" in message
        assert "next run" in message

    @pytest.mark.asyncio
    async def test_refusal_message_carries_no_end_time_or_clock_value(self):
        num = _make_number(current_value=10.0)
        _set_zone(num, open=True, duration_seconds=600, event_time="2026-08-13T21:05:00")

        with pytest.raises(HomeAssistantError) as excinfo:
            await num.async_set_native_value(1.0)

        message = str(excinfo.value)
        assert ":" not in message
        assert "2026-08-13T21:05:00" not in message
        assert "600" not in message

    @pytest.mark.asyncio
    async def test_two_identical_attempts_against_an_open_zone_both_refuse(self):
        num = _make_number(current_value=10.0)
        _set_zone(num, open=True)

        with pytest.raises(HomeAssistantError):
            await num.async_set_native_value(1.0)
        with pytest.raises(HomeAssistantError):
            await num.async_set_native_value(1.0)

        assert num.native_value == 10.0
        num.async_write_ha_state.assert_not_called()
        assert num.coordinator._client.mock_calls == []

    def test_base_class_default_run_state_open_is_none(self):
        base = _RainPointDurationNumberBase.__new__(_RainPointDurationNumberBase)
        assert base._run_state_open is None

    def test_base_class_default_zone_label_names_no_number(self):
        base = _RainPointDurationNumberBase.__new__(_RainPointDurationNumberBase)
        label = base._zone_label
        assert isinstance(label, str)
        assert label
        assert not any(char.isdigit() for char in label)

    def test_base_class_default_open_run_attributes_is_empty(self):
        base = _RainPointDurationNumberBase.__new__(_RainPointDurationNumberBase)
        assert base._open_run_attributes == {}


class TestDurationRefusalRealTimeline:
    """The refusal proven against a real closed-then-open-then-closed
    coordinator timeline, against the same already-constructed entity object
    throughout, rather than an injected coordinator.data snapshot.
    """

    @staticmethod
    async def _build_timeline():
        """Construct -> first refresh (closed) -> platform setup for a real HTV245FRF valve hub."""
        from custom_components.rainpoint.const import CONF_HIDS
        from custom_components.rainpoint.coordinator import RainPointCoordinator
        from custom_components.rainpoint.number import async_setup_entry

        client = AsyncMock()
        client.get_devices_by_hid.return_value = [
            {
                "mid": 20,
                "name": "Hub A",
                "deviceName": "d",
                "productKey": "pk",
                "homeName": "H",
                "subDevices": [{"addr": 1, "name": "Hub A", "model": MODEL_VALVE_245, "softVer": "127"}],
            }
        ]
        client.get_multiple_device_status.return_value = make_valve_zone_status(zones_reported=True)

        entry = MagicMock()
        entry.entry_id = "e1"
        entry.data = {CONF_HIDS: [10]}
        entry.options = {}
        hass = MagicMock()
        hass.data = {DOMAIN: {"e1": {}}}

        coordinator = RainPointCoordinator(hass, client, entry)
        hass.data[DOMAIN]["e1"]["coordinator"] = coordinator

        await coordinator.async_config_entry_first_refresh()

        captured = []
        async_add_entities = MagicMock(side_effect=lambda ents, **kw: captured.extend(ents))
        await async_setup_entry(hass, entry, async_add_entities)

        return coordinator, client, captured

    @pytest.mark.asyncio
    async def test_a_real_zone_refuses_only_while_its_own_status_reports_open(self):
        """One entity object, driven through closed, open, closed refreshes."""
        coordinator, client, captured = await self._build_timeline()
        zone1 = next(e for e in captured if e._zone_num == 1)
        zone1.hass = MagicMock()
        zone1.async_write_ha_state = MagicMock()

        # Closed at setup: the set succeeds.
        await zone1.async_set_native_value(5.0)
        assert zone1.native_value == 5.0
        zone1.async_write_ha_state.assert_called_once()

        # A refresh reports zone 1 open: the same entity object now refuses,
        # and its displayed value does not move.
        client.get_multiple_device_status.return_value = make_valve_zone_status_open()
        await coordinator.async_refresh()

        with pytest.raises(HomeAssistantError):
            await zone1.async_set_native_value(1.0)
        assert zone1.native_value == 5.0
        zone1.async_write_ha_state.assert_called_once()

        # A further refresh reports zone 1 closed again: the same entity
        # object accepts once more.
        client.get_multiple_device_status.return_value = make_valve_zone_status(zones_reported=True)
        await coordinator.async_refresh()

        await zone1.async_set_native_value(8.0)
        assert zone1.native_value == 8.0
        assert zone1.async_write_ha_state.call_count == 2


# ---------------------------------------------------------------------------
# The mid-run refusal extended to the generic control family, reading its
# run state through generic_control.generic_run_state_open -- the same body
# the companion generic valve now calls.
# ---------------------------------------------------------------------------


class TestGenericDurationRefusal:
    """Every case in the generic family's own behaviour list.

    Driven through the real build_generic_duration_entities factory, so
    these cases run against the same entity the platform ships rather than
    a hand-constructed one.
    """

    @staticmethod
    def _build(fields=None):
        sensor_info = _generic_control_sensor_info(ANCHOR_MODEL, ANCHOR_MODEL_CODE, fields=fields)
        coordinator = _make_generic_coordinator("100_200_1", sensor_info)
        entities = build_generic_duration_entities(coordinator, "100_200_1", sensor_info, "100_200_1")
        entity = entities[0]
        entity.hass = MagicMock()
        entity.async_write_ha_state = MagicMock()
        return entity, coordinator

    @pytest.mark.asyncio
    async def test_explicit_open_refuses_with_the_same_message_shape(self):
        entity, _ = self._build(fields=[_run_state_field(1, 1)])

        with pytest.raises(HomeAssistantError) as excinfo:
            await entity.async_set_native_value(1.0)

        message = str(excinfo.value)
        assert "Zone 1" in message
        assert "watering" in message
        assert "closed" in message
        assert "next run" in message
        entity.async_write_ha_state.assert_not_called()

    @pytest.mark.asyncio
    async def test_ascii_declined_payload_accepts(self):
        entity, coordinator = self._build(fields=[_run_state_field(1, 1)])
        coordinator.data["sensors"]["100_200_1"]["data"]["generic"]["ascii_framed"] = True

        await entity.async_set_native_value(1.0)

        assert entity.native_value == 1.0
        entity.async_write_ha_state.assert_called_once()

    @pytest.mark.asyncio
    async def test_no_matching_run_state_record_accepts(self):
        entity, _ = self._build(fields=[])

        await entity.async_set_native_value(1.0)

        assert entity.native_value == 1.0
        entity.async_write_ha_state.assert_called_once()

    @pytest.mark.asyncio
    async def test_non_integer_value_accepts(self):
        entity, _ = self._build(fields=[_run_state_field(1, "1")])

        await entity.async_set_native_value(1.0)

        assert entity.native_value == 1.0
        entity.async_write_ha_state.assert_called_once()

    @pytest.mark.asyncio
    async def test_bool_value_accepts(self):
        entity, _ = self._build(fields=[_run_state_field(1, True)])

        await entity.async_set_native_value(1.0)

        assert entity.native_value == 1.0
        entity.async_write_ha_state.assert_called_once()

    @pytest.mark.asyncio
    async def test_a_record_at_an_unproven_width_accepts(self):
        field = dict(_run_state_field(1, 1), raw="0100")
        entity, _ = self._build(fields=[field])

        await entity.async_set_native_value(1.0)

        assert entity.native_value == 1.0
        entity.async_write_ha_state.assert_called_once()

    @pytest.mark.asyncio
    async def test_run_state_closed_accepts(self):
        entity, _ = self._build(fields=[_run_state_field(1, 0)])

        await entity.async_set_native_value(1.0)

        assert entity.native_value == 1.0
        entity.async_write_ha_state.assert_called_once()

    @pytest.mark.asyncio
    async def test_the_common_case_of_no_run_state_identity_at_all_accepts_every_write(self):
        """The practical outcome of the same rule meeting thinner data, asserted directly.

        A generic duration entity built for a model whose catalog data
        supplies no run-state identity at all is, in practice, the common
        case for this family -- and this must not be read later as a defect.
        """
        entity, _ = self._build()  # fields=None -> []

        await entity.async_set_native_value(1.0)
        await entity.async_set_native_value(2.0)

        assert entity.native_value == 2.0
        assert entity.async_write_ha_state.call_count == 2

    def test_zone_label_is_zone_plus_the_datapoint_port(self):
        entity, _ = self._build()
        assert entity._zone_label == "Zone 1"

    def test_both_duration_families_reach_refusal_through_the_same_inherited_method(self):
        """Neither family can carry its own rule: they resolve to one method object."""
        assert RainPointZoneDurationNumber.async_set_native_value is RainPointGenericZoneDurationNumber.async_set_native_value


# ---------------------------------------------------------------------------
# The running run's own numbers, carried as extra state attributes only
# while the entity's own zone reads explicitly open.
# ---------------------------------------------------------------------------


class TestOpenRunAttributes:
    """Every case in the open-run-attributes behaviour list."""

    @pytest.mark.asyncio
    async def test_open_zone_carries_duration_and_event_time(self):
        num = _make_number()
        _set_zone(num, open=True, duration_seconds=600, event_time="2026-08-13T21:05:00")

        attrs = num.extra_state_attributes

        assert attrs["duration_seconds"] == 600
        assert attrs["event_time"] == "2026-08-13T21:05:00"
        assert attrs["firmware_version"] == "1.0"

    def test_closed_zone_carries_neither_key(self):
        num = _make_number()
        _set_zone(num, open=False, duration_seconds=600, event_time="2026-08-13T21:05:00")

        attrs = num.extra_state_attributes

        assert "duration_seconds" not in attrs
        assert "event_time" not in attrs

    def test_open_none_carries_neither_key(self):
        num = _make_number()
        _set_zone(num, open=None)

        attrs = num.extra_state_attributes

        assert "duration_seconds" not in attrs
        assert "event_time" not in attrs

    def test_no_zone_record_carries_neither_key_but_still_carries_sub_device_attributes(self):
        num = _make_number()
        _set_zone_missing(num)

        attrs = num.extra_state_attributes

        assert "duration_seconds" not in attrs
        assert "event_time" not in attrs
        assert attrs["firmware_version"] == "1.0"

    def test_open_zone_with_no_duration_omits_the_key(self):
        num = _make_number()
        _set_zone(num, open=True, duration_seconds=None, event_time="2026-08-13T21:05:00")

        attrs = num.extra_state_attributes

        assert "duration_seconds" not in attrs
        assert attrs["event_time"] == "2026-08-13T21:05:00"

    def test_open_zone_with_no_event_time_omits_the_key(self):
        num = _make_number()
        _set_zone(num, open=True, duration_seconds=600, event_time=None)

        attrs = num.extra_state_attributes

        assert attrs["duration_seconds"] == 600
        assert "event_time" not in attrs

    def test_state_raw_is_not_added(self):
        num = _make_number()
        _set_zone(num, open=True, duration_seconds=600, event_time="2026-08-13T21:05:00", state_raw=1)

        attrs = num.extra_state_attributes

        assert "state_raw" not in attrs

    def test_generic_duration_entity_open_carries_neither_key(self):
        """No curated identity supplies either value on the generic path."""
        entity, _ = TestGenericDurationRefusal._build(fields=[_run_state_field(1, 1)])

        attrs = entity.extra_state_attributes

        assert "duration_seconds" not in attrs
        assert "event_time" not in attrs
        assert attrs["firmware_version"] == "1.0.0"

    @pytest.mark.asyncio
    async def test_real_timeline_attributes_appear_and_disappear_with_the_zone(self):
        """The same entity object gains the two keys on the refresh that opens
        the zone and loses them on the refresh that closes it again."""
        coordinator, client, captured = await TestDurationRefusalRealTimeline._build_timeline()
        zone1 = next(e for e in captured if e._zone_num == 1)

        assert "duration_seconds" not in zone1.extra_state_attributes

        client.get_multiple_device_status.return_value = make_valve_zone_status_open()
        await coordinator.async_refresh()

        attrs = zone1.extra_state_attributes
        assert attrs["duration_seconds"] == 600
        assert attrs["event_time"] == "2026-08-13T21:05:00"

        client.get_multiple_device_status.return_value = make_valve_zone_status(zones_reported=True)
        await coordinator.async_refresh()

        attrs = zone1.extra_state_attributes
        assert "duration_seconds" not in attrs
        assert "event_time" not in attrs
