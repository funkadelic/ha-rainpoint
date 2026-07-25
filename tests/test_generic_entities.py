"""Tests for generic_entities.py (opt-in, catalog-driven generic sensor factory)."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from custom_components.rainpoint import generic_entities as generic_entities_module
from custom_components.rainpoint.const import (
    CONF_GENERIC_ENTITIES_ENABLED,
    DOMAIN,
    GENERIC_UNIQUE_ID_MARKER,
    MODEL_MOISTURE_SIMPLE,
    MODEL_VALVE_245,
)
from custom_components.rainpoint.generic_entities import (
    _IDENTITY_SPECS,
    GENERIC_MARKER_ICON,
    GenericSensorSpec,
    RainPointGenericSensor,
    _declared_status_datapoints,
    _matching_field,
    build_generic_entities,
)
from custom_components.rainpoint.sensor import _MODEL_FACTORIES, async_setup_entry
from tests.helpers import make_coordinator_data, make_sensor_entry

FAKE_MODEL = "FAKE_GENERIC_MODEL"

_SENTINEL = object()


def _dp(identity: str, dp_port=0, dp_code: int = 10, data_type: str = "U8") -> dict:
    """Build one catalog dp entry."""
    return {"dpCode": dp_code, "identity": identity, "dpPort": dp_port, "dpDataType": data_type, "dpLen": 1}


def _decoded_field(name: str, value, dp_port, width_mismatch: bool = False) -> dict:
    """Build one decode_generic field entry, catalog-annotated."""
    return {
        "name": name,
        "index": 0,
        "dp_id": 0,
        "raw": "00",
        "value": value,
        "catalog": {"dp_port": dp_port, "width_mismatch": width_mismatch},
    }


def _unknown_data(fields: list[dict] | None = None, model: str = FAKE_MODEL) -> dict:
    """Build the {"type": "unknown", ...} decoded-payload shape build_generic_entities requires."""
    fields = fields or []
    return {
        "type": "unknown",
        "model": model,
        "raw_value": "10#00",
        "generic": {"decoder": "generic-tlv", "fields": fields, "field_names": [f["name"] for f in fields]},
    }


def _make_generic_sensor(
    dp_entry: dict,
    port_number=1,
    data=_SENTINEL,
    sensor_info_overrides: dict | None = None,
    sensor_key: str = "100_200_1",
) -> RainPointGenericSensor:
    """Build a real RainPointGenericSensor instance with a mock coordinator."""
    resolved_data = _unknown_data() if data is _SENTINEL else data
    sensor_info = make_sensor_entry(model=FAKE_MODEL, sub_name="Garden Sensor", data=resolved_data)
    if sensor_info_overrides:
        sensor_info.update(sensor_info_overrides)
    coordinator = MagicMock()
    coordinator.data = make_coordinator_data(sensors={sensor_key: sensor_info})
    return RainPointGenericSensor(coordinator, sensor_key, sensor_info, sensor_key, dp_entry, port_number)


def _make_hass_and_entry(coordinator, options: dict):
    """Build a MagicMock hass/entry pair matching the sensor platform's async_setup_entry contract."""
    hass = MagicMock()
    entry = MagicMock()
    entry.entry_id = "test_entry"
    entry.options = options
    hass.data = {DOMAIN: {"test_entry": {"coordinator": coordinator}}}
    return hass, entry


# ---------------------------------------------------------------------------
# _declared_status_datapoints
# ---------------------------------------------------------------------------


class TestDeclaredStatusDatapoints:
    """Tests for _declared_status_datapoints."""

    def test_returns_empty_when_catalog_lookup_raises(self, monkeypatch):
        def _boom(model, model_code=None):
            raise RuntimeError("boom")

        monkeypatch.setattr(generic_entities_module, "get_catalog_entry", _boom)
        assert _declared_status_datapoints(FAKE_MODEL, None) == []

    def test_returns_empty_when_catalog_has_no_entry(self, monkeypatch):
        monkeypatch.setattr(generic_entities_module, "get_catalog_entry", lambda model, model_code=None: None)
        assert _declared_status_datapoints(FAKE_MODEL, None) == []

    def test_skips_non_dict_entries_and_non_status_identities(self, monkeypatch):
        dp_list = ["not-a-dict", {"identity": "CTL_WATER", "dpPort": 0}, _dp("STA_RH")]
        monkeypatch.setattr(generic_entities_module, "get_catalog_entry", lambda model, model_code=None: dp_list)
        assert _declared_status_datapoints(FAKE_MODEL, None) == [_dp("STA_RH")]

    def test_skips_entry_with_non_string_identity(self, monkeypatch):
        dp_list = [{"identity": 123, "dpPort": 0}, _dp("STA_TEM")]
        monkeypatch.setattr(generic_entities_module, "get_catalog_entry", lambda model, model_code=None: dp_list)
        assert _declared_status_datapoints(FAKE_MODEL, None) == [_dp("STA_TEM")]


# ---------------------------------------------------------------------------
# _matching_field
# ---------------------------------------------------------------------------


class TestMatchingField:
    """Tests for _matching_field."""

    def test_no_match_returns_none(self):
        fields = [_decoded_field("STA_TEM", 100, 0)]
        assert _matching_field(fields, "STA_RH", 0) is None

    def test_single_match_returns_field(self):
        target = _decoded_field("STA_RH", 42, 0)
        fields = [_decoded_field("STA_TEM", 100, 0), target]
        assert _matching_field(fields, "STA_RH", 0) is target

    def test_ambiguous_match_returns_none(self):
        fields = [_decoded_field("STA_RH", 42, 0), _decoded_field("STA_RH", 43, 0)]
        assert _matching_field(fields, "STA_RH", 0) is None

    def test_field_missing_catalog_key_does_not_match_a_port(self):
        fields = [{"name": "STA_RH", "value": 42}]
        assert _matching_field(fields, "STA_RH", 0) is None


# ---------------------------------------------------------------------------
# build_generic_entities gate
# ---------------------------------------------------------------------------


class TestBuildGenericEntitiesGate:
    """Tests for the build_generic_entities rejection order and success path."""

    def _coordinator_for(self, sensor_key, sensor_info):
        coordinator = MagicMock()
        coordinator.data = make_coordinator_data(sensors={sensor_key: sensor_info})
        return coordinator

    def test_hand_written_model_yields_nothing_even_when_catalog_is_curated(self, monkeypatch):
        """A hand-written valve model absent from _MODEL_FACTORIES still yields zero generic sensors."""
        assert MODEL_VALVE_245 not in _MODEL_FACTORIES
        dp_entries = [_dp("STA_RH")]
        monkeypatch.setattr(generic_entities_module, "get_catalog_entry", lambda model, model_code=None: dp_entries)
        sensor_info = make_sensor_entry(model=MODEL_VALVE_245, data=_unknown_data(model=MODEL_VALVE_245))
        coordinator = self._coordinator_for("100_200_1", sensor_info)

        assert build_generic_entities(coordinator, "100_200_1", sensor_info, "100_200_1") == []

    def test_non_unknown_payload_yields_nothing(self, monkeypatch):
        dp_entries = [_dp("STA_RH")]
        monkeypatch.setattr(generic_entities_module, "get_catalog_entry", lambda model, model_code=None: dp_entries)
        sensor_info = make_sensor_entry(model=FAKE_MODEL, data={"type": "moisture_simple"})
        coordinator = self._coordinator_for("100_200_1", sensor_info)

        assert build_generic_entities(coordinator, "100_200_1", sensor_info, "100_200_1") == []

    def test_no_data_yields_nothing(self, monkeypatch):
        dp_entries = [_dp("STA_RH")]
        monkeypatch.setattr(generic_entities_module, "get_catalog_entry", lambda model, model_code=None: dp_entries)
        sensor_info = make_sensor_entry(model=FAKE_MODEL, data=None)
        coordinator = self._coordinator_for("100_200_1", sensor_info)

        assert build_generic_entities(coordinator, "100_200_1", sensor_info, "100_200_1") == []

    def test_no_declared_datapoints_yields_nothing(self, monkeypatch):
        monkeypatch.setattr(generic_entities_module, "get_catalog_entry", lambda model, model_code=None: [])
        sensor_info = make_sensor_entry(model=FAKE_MODEL, data=_unknown_data())
        coordinator = self._coordinator_for("100_200_1", sensor_info)

        assert build_generic_entities(coordinator, "100_200_1", sensor_info, "100_200_1") == []

    def test_non_integer_dp_port_fails_whole_model(self, monkeypatch):
        dp_entries = [_dp("STA_RH", dp_port="0")]
        monkeypatch.setattr(generic_entities_module, "get_catalog_entry", lambda model, model_code=None: dp_entries)
        sensor_info = make_sensor_entry(model=FAKE_MODEL, data=_unknown_data())
        coordinator = self._coordinator_for("100_200_1", sensor_info)

        assert build_generic_entities(coordinator, "100_200_1", sensor_info, "100_200_1") == []

    def test_bool_dp_port_fails_whole_model(self, monkeypatch):
        """Booleans are technically ints in Python but must not be accepted as a dpPort."""
        dp_entries = [_dp("STA_RH", dp_port=True)]
        monkeypatch.setattr(generic_entities_module, "get_catalog_entry", lambda model, model_code=None: dp_entries)
        sensor_info = make_sensor_entry(model=FAKE_MODEL, data=_unknown_data())
        coordinator = self._coordinator_for("100_200_1", sensor_info)

        assert build_generic_entities(coordinator, "100_200_1", sensor_info, "100_200_1") == []

    def test_duplicate_identity_and_port_fails_whole_model(self, monkeypatch):
        dp_entries = [_dp("STA_RH", dp_port=0, dp_code=10), _dp("STA_RH", dp_port=0, dp_code=11)]
        monkeypatch.setattr(generic_entities_module, "get_catalog_entry", lambda model, model_code=None: dp_entries)
        sensor_info = make_sensor_entry(model=FAKE_MODEL, data=_unknown_data())
        coordinator = self._coordinator_for("100_200_1", sensor_info)

        assert build_generic_entities(coordinator, "100_200_1", sensor_info, "100_200_1") == []

    def test_uncurated_identity_fails_whole_model(self, monkeypatch):
        dp_entries = [_dp("STA_RH", dp_port=0), _dp("STA_BAT", dp_port=0, dp_code=11)]
        monkeypatch.setattr(generic_entities_module, "get_catalog_entry", lambda model, model_code=None: dp_entries)
        sensor_info = make_sensor_entry(model=FAKE_MODEL, data=_unknown_data())
        coordinator = self._coordinator_for("100_200_1", sensor_info)

        assert build_generic_entities(coordinator, "100_200_1", sensor_info, "100_200_1") == []

    def test_catalog_lookup_raising_never_propagates(self, monkeypatch):
        def _boom(model, model_code=None):
            raise RuntimeError("boom")

        monkeypatch.setattr(generic_entities_module, "get_catalog_entry", _boom)
        sensor_info = make_sensor_entry(model=FAKE_MODEL, data=_unknown_data())
        coordinator = self._coordinator_for("100_200_1", sensor_info)

        assert build_generic_entities(coordinator, "100_200_1", sensor_info, "100_200_1") == []

    def test_port_number_lookup_raising_never_propagates(self, monkeypatch):
        """Exercises build_generic_entities' own broad except, past the gate."""
        dp_entries = [_dp("STA_RH", dp_port=0)]
        monkeypatch.setattr(generic_entities_module, "get_catalog_entry", lambda model, model_code=None: dp_entries)

        def _boom(model, model_code=None):
            raise RuntimeError("boom")

        monkeypatch.setattr(generic_entities_module, "get_catalog_port_number", _boom)
        sensor_info = make_sensor_entry(model=FAKE_MODEL, data=_unknown_data())
        coordinator = self._coordinator_for("100_200_1", sensor_info)

        assert build_generic_entities(coordinator, "100_200_1", sensor_info, "100_200_1") == []

    def test_fully_curated_variant_yields_one_sensor_per_declared_datapoint(self, monkeypatch):
        dp_entries = [_dp("STA_RH", dp_port=0, dp_code=10), _dp("STA_TEM", dp_port=0, dp_code=9)]
        monkeypatch.setattr(generic_entities_module, "get_catalog_entry", lambda model, model_code=None: dp_entries)
        monkeypatch.setattr(generic_entities_module, "get_catalog_port_number", lambda model, model_code=None: 1)
        sensor_info = make_sensor_entry(model=FAKE_MODEL, data=_unknown_data())
        coordinator = self._coordinator_for("100_200_1", sensor_info)

        entities = build_generic_entities(coordinator, "100_200_1", sensor_info, "100_200_1")

        assert len(entities) == 2
        assert all(isinstance(e, RainPointGenericSensor) for e in entities)

    def test_entities_are_ordered_by_port_then_identity(self, monkeypatch):
        dp_entries = [
            _dp("STA_TEM", dp_port=1, dp_code=1),
            _dp("STA_RH", dp_port=0, dp_code=2),
            _dp("STA_RSSI", dp_port=1, dp_code=3),
        ]
        monkeypatch.setattr(generic_entities_module, "get_catalog_entry", lambda model, model_code=None: dp_entries)
        monkeypatch.setattr(generic_entities_module, "get_catalog_port_number", lambda model, model_code=None: 4)
        sensor_info = make_sensor_entry(model=FAKE_MODEL, data=_unknown_data())
        coordinator = self._coordinator_for("100_200_1", sensor_info)

        entities = build_generic_entities(coordinator, "100_200_1", sensor_info, "100_200_1")

        identities = [e._identity for e in entities]
        assert identities == ["STA_RH", "STA_RSSI", "STA_TEM"]

    def test_repeated_setup_over_identical_data_yields_identical_unique_id_sets(self, monkeypatch):
        dp_entries = [_dp("STA_RH", dp_port=0), _dp("STA_TEM", dp_port=0, dp_code=9)]
        monkeypatch.setattr(generic_entities_module, "get_catalog_entry", lambda model, model_code=None: dp_entries)
        monkeypatch.setattr(generic_entities_module, "get_catalog_port_number", lambda model, model_code=None: 1)
        sensor_info = make_sensor_entry(model=FAKE_MODEL, data=_unknown_data())
        coordinator = self._coordinator_for("100_200_1", sensor_info)

        first = {e._attr_unique_id for e in build_generic_entities(coordinator, "100_200_1", sensor_info, "100_200_1")}
        second = {e._attr_unique_id for e in build_generic_entities(coordinator, "100_200_1", sensor_info, "100_200_1")}

        assert first == second
        assert len(first) == 2


# ---------------------------------------------------------------------------
# RainPointGenericSensor construction
# ---------------------------------------------------------------------------


class TestRainPointGenericSensorConstruction:
    """Tests for unique_id / name / icon / device_class / state_class construction."""

    def test_unique_id_exact_shape(self):
        dp_entry = _dp("STA_RH", dp_port=0, dp_code=10)
        sensor = _make_generic_sensor(dp_entry, port_number=1)
        assert sensor._attr_unique_id == "rainpoint_100_200_1_generic_sta_rh_p0"

    def test_name_single_port_variant_omits_zone(self):
        dp_entry = _dp("STA_RH", dp_port=0)
        sensor = _make_generic_sensor(dp_entry, port_number=1)
        assert sensor._attr_name == "Garden Sensor Humidity (unverified)"

    def test_name_multi_port_variant_includes_zone(self):
        dp_entry = _dp("STA_TEM", dp_port=2, dp_code=9)
        sensor = _make_generic_sensor(dp_entry, port_number=4)
        assert sensor._attr_name == "Garden Sensor Zone 2 Temperature (unverified)"

    def test_zone_segment_omitted_when_port_is_zero_even_on_multi_port_variant(self):
        dp_entry = _dp("STA_TEM", dp_port=0, dp_code=9)
        sensor = _make_generic_sensor(dp_entry, port_number=4)
        assert sensor._attr_name == "Garden Sensor Temperature (unverified)"

    def test_zone_segment_omitted_when_port_number_is_none(self):
        dp_entry = _dp("STA_TEM", dp_port=2, dp_code=9)
        sensor = _make_generic_sensor(dp_entry, port_number=None)
        assert sensor._attr_name == "Garden Sensor Temperature (unverified)"

    def test_icon_wins_over_device_class_default(self):
        dp_entry = _dp("STA_TEM", dp_port=0, dp_code=9)
        sensor = _make_generic_sensor(dp_entry, port_number=1)
        assert sensor._attr_icon == GENERIC_MARKER_ICON
        assert sensor._attr_device_class is not None

    def test_state_class_is_always_none(self):
        dp_entry = _dp("STA_RH", dp_port=0)
        sensor = _make_generic_sensor(dp_entry, port_number=1)
        assert sensor._attr_state_class is None


# ---------------------------------------------------------------------------
# RainPointGenericSensor.native_value
# ---------------------------------------------------------------------------


class TestRainPointGenericSensorNativeValue:
    """Tests for native_value transform/range/validity handling."""

    def test_humidity_in_range(self):
        dp_entry = _dp("STA_RH", dp_port=0)
        fields = [_decoded_field("STA_RH", 42, 0)]
        sensor = _make_generic_sensor(dp_entry, port_number=1, data=_unknown_data(fields))
        assert sensor.native_value == 42.0

    def test_humidity_out_of_range_is_none(self):
        dp_entry = _dp("STA_RH", dp_port=0)
        fields = [_decoded_field("STA_RH", 250, 0)]
        sensor = _make_generic_sensor(dp_entry, port_number=1, data=_unknown_data(fields))
        assert sensor.native_value is None

    def test_signal_strength_negative_reading(self):
        dp_entry = _dp("STA_RSSI", dp_port=0, dp_code=32)
        fields = [_decoded_field("STA_RSSI", 198, 0)]
        sensor = _make_generic_sensor(dp_entry, port_number=1, data=_unknown_data(fields))
        assert sensor.native_value == -58.0

    def test_signal_strength_non_negative_after_reinterpretation_is_none(self):
        dp_entry = _dp("STA_RSSI", dp_port=0, dp_code=32)
        fields = [_decoded_field("STA_RSSI", 12, 0)]
        sensor = _make_generic_sensor(dp_entry, port_number=1, data=_unknown_data(fields))
        assert sensor.native_value is None

    def test_temperature_scaling(self):
        dp_entry = _dp("STA_TEM", dp_port=0, dp_code=9)
        fields = [_decoded_field("STA_TEM", 683, 0)]
        sensor = _make_generic_sensor(dp_entry, port_number=1, data=_unknown_data(fields))
        assert sensor.native_value == 20.2

    def test_absent_datapoint_is_none(self):
        dp_entry = _dp("STA_TEM", dp_port=0, dp_code=9)
        sensor = _make_generic_sensor(dp_entry, port_number=1, data=_unknown_data([]))
        assert sensor.native_value is None

    def test_non_integer_raw_value_is_none(self):
        dp_entry = _dp("STA_TEM", dp_port=0, dp_code=9)
        fields = [_decoded_field("STA_TEM", "683", 0)]
        sensor = _make_generic_sensor(dp_entry, port_number=1, data=_unknown_data(fields))
        assert sensor.native_value is None

    def test_bool_raw_value_is_none(self):
        """Booleans are technically ints in Python but must not be accepted as a raw value."""
        dp_entry = _dp("STA_TEM", dp_port=0, dp_code=9)
        fields = [_decoded_field("STA_TEM", True, 0)]
        sensor = _make_generic_sensor(dp_entry, port_number=1, data=_unknown_data(fields))
        assert sensor.native_value is None

    def test_no_sensor_data_is_none(self):
        dp_entry = _dp("STA_TEM", dp_port=0, dp_code=9)
        sensor = _make_generic_sensor(dp_entry, port_number=1, data=None)
        assert sensor.native_value is None

    def test_transform_returning_none_is_surfaced_as_none(self, monkeypatch):
        """Exercises the defensive 'transform result is None' branch directly."""
        fake_spec = GenericSensorSpec(
            label="Fake",
            device_class=None,
            unit="unit",
            state_class=None,
            transform=lambda raw: None,
            valid_range=(0.0, 100.0),
            precision=0,
        )
        monkeypatch.setitem(_IDENTITY_SPECS, "STA_FAKE", fake_spec)
        dp_entry = _dp("STA_FAKE", dp_port=0, dp_code=99)
        fields = [_decoded_field("STA_FAKE", 5, 0)]
        sensor = _make_generic_sensor(dp_entry, port_number=1, data=_unknown_data(fields))
        assert sensor.native_value is None


# ---------------------------------------------------------------------------
# RainPointGenericSensor.extra_state_attributes
# ---------------------------------------------------------------------------


class TestRainPointGenericSensorAttributes:
    """Tests for the six-key provenance attribute allowlist."""

    def test_exactly_six_provenance_keys_present(self):
        dp_entry = _dp("STA_RH", dp_port=0, dp_code=10, data_type="U8")
        fields = [_decoded_field("STA_RH", 42, 0, width_mismatch=False)]
        sensor = _make_generic_sensor(dp_entry, port_number=1, data=_unknown_data(fields))

        attrs = sensor.extra_state_attributes

        assert attrs["catalog_derived"] is True
        assert attrs["identity"] == "STA_RH"
        assert attrs["dp_code"] == 10
        assert attrs["dp_port"] == 0
        assert attrs["dp_data_type"] == "U8"
        assert attrs["width_mismatch"] is False

    def test_width_mismatch_is_none_when_datapoint_absent_from_poll(self):
        dp_entry = _dp("STA_RH", dp_port=0, dp_code=10)
        sensor = _make_generic_sensor(dp_entry, port_number=1, data=_unknown_data([]))

        attrs = sensor.extra_state_attributes

        assert attrs["width_mismatch"] is None

    def test_no_account_identifying_fields_leak_into_attributes(self):
        dp_entry = _dp("STA_RH", dp_port=0)
        sensor = _make_generic_sensor(
            dp_entry,
            port_number=1,
            sensor_info_overrides={
                "home_name": "Casa",
                "hub_name": "Hub1",
                "device_name": "dev",
                "product_key": "pk",
            },
        )

        attrs = sensor.extra_state_attributes

        forbidden = {"home_name", "hub_name", "device_name", "product_key"}
        assert forbidden.isdisjoint(attrs.keys())


# ---------------------------------------------------------------------------
# End-to-end: async_setup_entry dispatch with the options toggle
# ---------------------------------------------------------------------------


class TestGenericSensorDispatchEndToEnd:
    """Tests exercising the full sensor.py dispatch with both toggle states."""

    @pytest.mark.asyncio
    async def test_toggle_off_yields_no_generic_entities(self, monkeypatch):
        dp_entries = [_dp("STA_RH", dp_port=0)]
        monkeypatch.setattr(generic_entities_module, "get_catalog_entry", lambda model, model_code=None: dp_entries)
        monkeypatch.setattr(generic_entities_module, "get_catalog_port_number", lambda model, model_code=None: 1)

        sensor_key = "100_200_1"
        sensor_info = make_sensor_entry(model=FAKE_MODEL, data=_unknown_data())
        coordinator = MagicMock()
        coordinator.data = make_coordinator_data(sensors={sensor_key: sensor_info})
        hass, entry = _make_hass_and_entry(coordinator, {})

        captured = []
        async_add_entities = MagicMock(side_effect=lambda ents, **kw: captured.extend(ents))
        await async_setup_entry(hass, entry, async_add_entities)

        assert len(captured) == 2  # Unsupported + Raw Payload only
        assert all(GENERIC_UNIQUE_ID_MARKER not in getattr(e, "_attr_unique_id", "") for e in captured)

    @pytest.mark.asyncio
    async def test_toggle_on_fully_curated_variant_yields_generic_plus_unsupported_plus_raw(self, monkeypatch):
        dp_entries = [_dp("STA_RH", dp_port=0), _dp("STA_TEM", dp_port=0, dp_code=9)]
        monkeypatch.setattr(generic_entities_module, "get_catalog_entry", lambda model, model_code=None: dp_entries)
        monkeypatch.setattr(generic_entities_module, "get_catalog_port_number", lambda model, model_code=None: 1)

        sensor_key = "100_200_1"
        sensor_info = make_sensor_entry(model=FAKE_MODEL, data=_unknown_data())
        coordinator = MagicMock()
        coordinator.data = make_coordinator_data(sensors={sensor_key: sensor_info})
        hass, entry = _make_hass_and_entry(coordinator, {CONF_GENERIC_ENTITIES_ENABLED: True})

        captured = []
        async_add_entities = MagicMock(side_effect=lambda ents, **kw: captured.extend(ents))
        await async_setup_entry(hass, entry, async_add_entities)

        # 1 Unsupported + 2 generic + 1 Raw Payload = 4
        assert len(captured) == 4
        generic_uids = [e._attr_unique_id for e in captured if GENERIC_UNIQUE_ID_MARKER in e._attr_unique_id]
        assert len(generic_uids) == 2

    @pytest.mark.asyncio
    async def test_toggle_on_uncurated_identity_yields_zero_generic_sensors(self, monkeypatch):
        dp_entries = [_dp("STA_RH", dp_port=0), _dp("STA_BAT", dp_port=0, dp_code=11)]
        monkeypatch.setattr(generic_entities_module, "get_catalog_entry", lambda model, model_code=None: dp_entries)
        monkeypatch.setattr(generic_entities_module, "get_catalog_port_number", lambda model, model_code=None: 1)

        sensor_key = "100_200_1"
        sensor_info = make_sensor_entry(model=FAKE_MODEL, data=_unknown_data())
        coordinator = MagicMock()
        coordinator.data = make_coordinator_data(sensors={sensor_key: sensor_info})
        hass, entry = _make_hass_and_entry(coordinator, {CONF_GENERIC_ENTITIES_ENABLED: True})

        captured = []
        async_add_entities = MagicMock(side_effect=lambda ents, **kw: captured.extend(ents))
        await async_setup_entry(hass, entry, async_add_entities)

        assert len(captured) == 2  # Unsupported + Raw Payload only
        assert all(GENERIC_UNIQUE_ID_MARKER not in e._attr_unique_id for e in captured)

    @pytest.mark.asyncio
    async def test_toggle_on_hand_written_model_yields_zero_generic_sensors(self, monkeypatch):
        """A hand-written model dispatched through its own factory never reaches the generic path."""
        dp_entries = [_dp("STA_RH", dp_port=0)]
        monkeypatch.setattr(generic_entities_module, "get_catalog_entry", lambda model, model_code=None: dp_entries)
        monkeypatch.setattr(generic_entities_module, "get_catalog_port_number", lambda model, model_code=None: 1)

        sensor_key = "100_200_1"
        sensor_info = make_sensor_entry(
            model=MODEL_MOISTURE_SIMPLE,
            data={"type": "moisture_simple", "moisture_percent": 5, "rssi_dbm": -80, "battery_percent": 75},
        )
        coordinator = MagicMock()
        coordinator.data = make_coordinator_data(sensors={sensor_key: sensor_info})
        hass, entry = _make_hass_and_entry(coordinator, {CONF_GENERIC_ENTITIES_ENABLED: True})

        captured = []
        async_add_entities = MagicMock(side_effect=lambda ents, **kw: captured.extend(ents))
        await async_setup_entry(hass, entry, async_add_entities)

        assert all(GENERIC_UNIQUE_ID_MARKER not in getattr(e, "_attr_unique_id", "") for e in captured)
