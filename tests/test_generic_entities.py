"""Tests for generic_entities.py (opt-in, catalog-driven generic sensor factory)."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from custom_components.rainpoint import generic_entities as generic_entities_module
from custom_components.rainpoint.api import product_catalog as product_catalog_module
from custom_components.rainpoint.api.trust import is_hand_written_model
from custom_components.rainpoint.const import (
    CONF_GENERIC_ENTITIES_ENABLED,
    DOMAIN,
    GENERIC_UNIQUE_ID_MARKER,
    HAND_WRITTEN_MODELS,
    MODEL_MOISTURE_SIMPLE,
    MODEL_VALVE_245,
)
from custom_components.rainpoint.generic_entities import (
    _IDENTITY_SPECS,
    GENERIC_MARKER_ICON,
    GenericGateResult,
    GenericSensorSpec,
    RainPointGenericSensor,
    _filter_status_entries,
    _matching_field,
    build_generic_entities,
    count_generic_eligible_devices,
    describe_generic_gate,
    evaluate_generic_gate,
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
# _filter_status_entries
# ---------------------------------------------------------------------------


class TestFilterStatusEntries:
    """Tests for _filter_status_entries."""

    def test_skips_non_dict_entries_and_non_status_identities(self):
        dp_list = ["not-a-dict", {"identity": "CTL_WATER", "dpPort": 0}, _dp("STA_RH")]
        assert _filter_status_entries(dp_list) == [_dp("STA_RH")]

    def test_skips_entry_with_non_string_identity(self):
        dp_list = [{"identity": 123, "dpPort": 0}, _dp("STA_TEM")]
        assert _filter_status_entries(dp_list) == [_dp("STA_TEM")]


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
        dp_entries = [_dp("STA_TEM", dp_code=9)]
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
        dp_entries = [_dp("STA_TEM", dp_port=0, dp_code=9), _dp("STA_BAT", dp_port=0, dp_code=11)]
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

    def test_entity_construction_failure_after_gate_pass_is_caught(self, monkeypatch):
        """Exercises build_generic_entities' own broad except, downstream of a passing gate."""
        bad_result = GenericGateResult(
            datapoints=[{"identity": "STA_NOT_CURATED", "dpPort": 0}],
            unmapped_identities=(),
            blocked_by=(),
            port_number=1,
        )
        monkeypatch.setattr(generic_entities_module, "evaluate_generic_gate", lambda model, model_code=None: bad_result)
        sensor_info = make_sensor_entry(model=FAKE_MODEL, data=_unknown_data())
        coordinator = self._coordinator_for("100_200_1", sensor_info)

        assert build_generic_entities(coordinator, "100_200_1", sensor_info, "100_200_1") == []

    def test_port_number_lookup_raising_never_propagates(self, monkeypatch):
        """Exercises evaluate_generic_gate's own broad except, surfaced through build_generic_entities."""
        dp_entries = [_dp("STA_RH", dp_port=0)]
        monkeypatch.setattr(generic_entities_module, "get_catalog_entry", lambda model, model_code=None: dp_entries)

        def _boom(model, model_code=None):
            raise RuntimeError("boom")

        monkeypatch.setattr(generic_entities_module, "get_catalog_port_number", _boom)
        sensor_info = make_sensor_entry(model=FAKE_MODEL, data=_unknown_data())
        coordinator = self._coordinator_for("100_200_1", sensor_info)

        assert build_generic_entities(coordinator, "100_200_1", sensor_info, "100_200_1") == []

    def test_fully_curated_variant_yields_one_sensor_per_declared_datapoint(self, monkeypatch):
        dp_entries = [_dp("STA_RSSI", dp_port=0, dp_code=10), _dp("STA_TEM", dp_port=0, dp_code=9)]
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
            _dp("STA_RSSI", dp_port=0, dp_code=2),
            _dp("STA_TEM", dp_port=0, dp_code=3),
        ]
        monkeypatch.setattr(generic_entities_module, "get_catalog_entry", lambda model, model_code=None: dp_entries)
        monkeypatch.setattr(generic_entities_module, "get_catalog_port_number", lambda model, model_code=None: 4)
        sensor_info = make_sensor_entry(model=FAKE_MODEL, data=_unknown_data())
        coordinator = self._coordinator_for("100_200_1", sensor_info)

        entities = build_generic_entities(coordinator, "100_200_1", sensor_info, "100_200_1")

        identities = [e._identity for e in entities]
        assert identities == ["STA_RSSI", "STA_TEM", "STA_TEM"]

    def test_repeated_setup_over_identical_data_yields_identical_unique_id_sets(self, monkeypatch):
        dp_entries = [_dp("STA_RSSI", dp_port=0, dp_code=10), _dp("STA_TEM", dp_port=0, dp_code=9)]
        monkeypatch.setattr(generic_entities_module, "get_catalog_entry", lambda model, model_code=None: dp_entries)
        monkeypatch.setattr(generic_entities_module, "get_catalog_port_number", lambda model, model_code=None: 1)
        sensor_info = make_sensor_entry(model=FAKE_MODEL, data=_unknown_data())
        coordinator = self._coordinator_for("100_200_1", sensor_info)

        first = {e._attr_unique_id for e in build_generic_entities(coordinator, "100_200_1", sensor_info, "100_200_1")}
        second = {e._attr_unique_id for e in build_generic_entities(coordinator, "100_200_1", sensor_info, "100_200_1")}

        assert first == second
        assert len(first) == 2


# ---------------------------------------------------------------------------
# evaluate_generic_gate / describe_generic_gate
# ---------------------------------------------------------------------------


class TestEvaluateGenericGate:
    """The full edge-case battery for the single shared gate evaluation."""

    def test_model_absent_from_catalog(self, monkeypatch):
        monkeypatch.setattr(generic_entities_module, "get_catalog_entry", lambda model, model_code=None: None)

        result = evaluate_generic_gate(FAKE_MODEL, None)

        assert result.passed is False
        assert result.datapoints == []
        assert result.unmapped_identities == ()
        assert len(result.blocked_by) == 1
        assert "not in" in result.blocked_by[0]

    def test_model_present_in_catalog_but_declaring_nothing_is_not_reported_as_absent(self, monkeypatch):
        """An empty dp list means the catalog carries the model but describes no readings.

        Roughly a third of the committed catalog is this shape, so reporting it
        as "not in the product catalog" would misdirect most reports about
        those models: extending the snapshot cannot help a model the vendor
        already describes with nothing.
        """
        monkeypatch.setattr(generic_entities_module, "get_catalog_entry", lambda model, model_code=None: [])

        result = evaluate_generic_gate(FAKE_MODEL, None)

        assert result.passed is False
        assert result.unmapped_identities == ()
        assert len(result.blocked_by) == 1
        assert "is in the product catalog" in result.blocked_by[0]
        assert "no readings" in result.blocked_by[0]

    def test_model_absent_from_catalog_is_reported_as_absent(self, monkeypatch):
        """A model the catalog does not carry at all reports exactly that."""
        monkeypatch.setattr(generic_entities_module, "get_catalog_entry", lambda model, model_code=None: None)
        monkeypatch.setattr(generic_entities_module, "get_catalog_variant_codes", lambda model: ())

        result = evaluate_generic_gate(FAKE_MODEL, None)

        assert result.blocked_by == (f"{FAKE_MODEL} is not in the product catalog, so nothing is known about what it reports",)

    def test_unresolved_variant_names_the_codes_rather_than_claiming_absence(self, monkeypatch):
        """A model with several variants and no reported code asks for the modelCode, not the catalog.

        The lookup misses for a reason the reporter can actually fix, so the
        reason has to distinguish it from a genuinely uncatalogued model.
        """
        monkeypatch.setattr(generic_entities_module, "get_catalog_entry", lambda model, model_code=None: None)
        monkeypatch.setattr(generic_entities_module, "get_catalog_variant_codes", lambda model: ("278", "279"))

        result = evaluate_generic_gate(FAKE_MODEL, None)

        assert len(result.blocked_by) == 1
        reason = result.blocked_by[0]
        assert "more than one hardware variant" in reason
        assert "278" in reason and "279" in reason
        assert "not in the product catalog" not in reason

    def test_variant_declaring_only_control_identities(self, monkeypatch):
        dp_entries = [{"identity": "CTL_WATER", "dpPort": 0}, {"identity": "CTL_SOCK", "dpPort": 1}]
        monkeypatch.setattr(generic_entities_module, "get_catalog_entry", lambda model, model_code=None: dp_entries)

        result = evaluate_generic_gate(FAKE_MODEL, None)

        assert result.passed is False
        assert result.unmapped_identities == ()
        assert len(result.blocked_by) == 1
        assert "does not report any readings" in result.blocked_by[0]

    def test_exactly_one_curated_status_datapoint_passes(self, monkeypatch):
        dp_entries = [_dp("STA_TEM", dp_port=0, dp_code=9)]
        monkeypatch.setattr(generic_entities_module, "get_catalog_entry", lambda model, model_code=None: dp_entries)
        monkeypatch.setattr(generic_entities_module, "get_catalog_port_number", lambda model, model_code=None: 1)

        result = evaluate_generic_gate(FAKE_MODEL, None)

        assert result.passed is True
        assert len(result.datapoints) == 1
        assert result.blocked_by == ()
        assert result.unmapped_identities == ()
        assert result.port_number == 1

    def test_one_curated_and_one_uncurated_identity_fails_naming_the_gap(self, monkeypatch):
        dp_entries = [_dp("STA_TEM", dp_port=0, dp_code=9), _dp("STA_BAT", dp_port=0, dp_code=11)]
        monkeypatch.setattr(generic_entities_module, "get_catalog_entry", lambda model, model_code=None: dp_entries)

        result = evaluate_generic_gate(FAKE_MODEL, None)

        assert result.passed is False
        assert result.datapoints == []
        assert result.unmapped_identities == ("STA_BAT",)
        assert len(result.blocked_by) == 1
        assert "1 of this device's 2 status readings" in result.blocked_by[0]

    def test_two_uncurated_identities_are_reported_sorted_and_deduped(self, monkeypatch):
        dp_entries = [
            _dp("STA_ZZZ", dp_port=0, dp_code=1),
            _dp("STA_AAA", dp_port=1, dp_code=2),
            _dp("STA_ZZZ", dp_port=2, dp_code=3),
        ]
        monkeypatch.setattr(generic_entities_module, "get_catalog_entry", lambda model, model_code=None: dp_entries)

        result = evaluate_generic_gate(FAKE_MODEL, None)

        assert result.passed is False
        assert result.unmapped_identities == ("STA_AAA", "STA_ZZZ")

    def test_duplicate_identity_and_dp_port_fails_naming_both(self, monkeypatch):
        dp_entries = [_dp("STA_RH", dp_port=0, dp_code=10), _dp("STA_RH", dp_port=0, dp_code=11)]
        monkeypatch.setattr(generic_entities_module, "get_catalog_entry", lambda model, model_code=None: dp_entries)

        result = evaluate_generic_gate(FAKE_MODEL, None)

        assert result.passed is False
        assert result.datapoints == []
        assert result.blocked_by
        duplicate_reason = result.blocked_by[0]
        assert "STA_RH" in duplicate_reason
        assert "0" in duplicate_reason

    def test_same_identity_different_ports_both_curated_yields_two_entities(self, monkeypatch):
        dp_entries = [_dp("STA_TEM", dp_port=0, dp_code=9), _dp("STA_TEM", dp_port=1, dp_code=8)]
        monkeypatch.setattr(generic_entities_module, "get_catalog_entry", lambda model, model_code=None: dp_entries)
        monkeypatch.setattr(generic_entities_module, "get_catalog_port_number", lambda model, model_code=None: 2)

        result = evaluate_generic_gate(FAKE_MODEL, None)

        assert result.passed is True
        assert len(result.datapoints) == 2
        ports = sorted(entry.get("dpPort") for entry in result.datapoints)
        assert ports == [0, 1]

    @pytest.mark.parametrize("bad_port", [None, "0", True, False])
    def test_unusable_dp_port_fails_whole_model(self, monkeypatch, bad_port):
        dp_entries = [_dp("STA_RH", dp_port=bad_port)]
        monkeypatch.setattr(generic_entities_module, "get_catalog_entry", lambda model, model_code=None: dp_entries)

        result = evaluate_generic_gate(FAKE_MODEL, None)

        assert result.passed is False
        assert result.blocked_by
        assert any("STA_RH" in reason for reason in result.blocked_by)

    def test_missing_dp_port_key_fails_whole_model(self, monkeypatch):
        dp_entries = [{"identity": "STA_RH", "dpCode": 1}]
        monkeypatch.setattr(generic_entities_module, "get_catalog_entry", lambda model, model_code=None: dp_entries)

        result = evaluate_generic_gate(FAKE_MODEL, None)

        assert result.passed is False
        assert result.blocked_by

    def test_hand_written_model_states_hand_written_reason(self):
        model = sorted(HAND_WRITTEN_MODELS)[0]

        result = evaluate_generic_gate(model, None)

        assert result.passed is False
        assert len(result.blocked_by) == 1
        assert "hand-written" in result.blocked_by[0]

    def test_emission_order_is_ascending_port_then_identity(self, monkeypatch):
        dp_entries = [
            _dp("STA_TEM", dp_port=1, dp_code=1),
            _dp("STA_RSSI", dp_port=0, dp_code=2),
            _dp("STA_TEM", dp_port=0, dp_code=3),
        ]
        monkeypatch.setattr(generic_entities_module, "get_catalog_entry", lambda model, model_code=None: dp_entries)
        monkeypatch.setattr(generic_entities_module, "get_catalog_port_number", lambda model, model_code=None: 4)

        result = evaluate_generic_gate(FAKE_MODEL, None)

        identities = [entry.get("identity") for entry in result.datapoints]
        assert identities == ["STA_RSSI", "STA_TEM", "STA_TEM"]

    def test_decoded_field_not_in_declared_list_does_not_change_verdict(self, monkeypatch):
        """The gate is evaluated against the static catalog list, never the decoded payload."""
        dp_entries = [_dp("STA_TEM", dp_port=0, dp_code=9)]
        monkeypatch.setattr(generic_entities_module, "get_catalog_entry", lambda model, model_code=None: dp_entries)
        monkeypatch.setattr(generic_entities_module, "get_catalog_port_number", lambda model, model_code=None: 1)
        fields = [
            _decoded_field("STA_TEM", 683, 0),
            _decoded_field("STA_RSSI", 42, 0),  # not declared - must not surface an entity or affect the gate
        ]
        sensor_info = make_sensor_entry(model=FAKE_MODEL, data=_unknown_data(fields))
        coordinator = MagicMock()
        coordinator.data = make_coordinator_data(sensors={"100_200_1": sensor_info})

        result = evaluate_generic_gate(FAKE_MODEL, None)
        entities = build_generic_entities(coordinator, "100_200_1", sensor_info, "100_200_1")

        assert result.passed is True
        assert len(entities) == 1
        assert entities[0]._identity == "STA_TEM"

    def test_catalog_lookup_raising_yields_fail_closed_result(self, monkeypatch):
        def _boom(model, model_code=None):
            raise RuntimeError("boom")

        monkeypatch.setattr(generic_entities_module, "get_catalog_entry", _boom)

        result = evaluate_generic_gate(FAKE_MODEL, None)

        assert result.passed is False
        assert result.datapoints == []
        assert result.blocked_by

    def test_port_number_lookup_raising_yields_fail_closed_result(self, monkeypatch):
        dp_entries = [_dp("STA_RH", dp_port=0)]
        monkeypatch.setattr(generic_entities_module, "get_catalog_entry", lambda model, model_code=None: dp_entries)

        def _boom(model, model_code=None):
            raise RuntimeError("boom")

        monkeypatch.setattr(generic_entities_module, "get_catalog_port_number", _boom)

        result = evaluate_generic_gate(FAKE_MODEL, None)

        assert result.passed is False
        assert result.blocked_by

    def test_real_committed_catalog_never_raises_and_never_disagrees_with_itself(self):
        """Consistency property over real vendor data: no model both passes and reports unmapped identities."""
        checked = 0
        for model, variants in product_catalog_module._CATALOG.items():
            if is_hand_written_model(model):
                continue
            for model_code in variants:
                result = evaluate_generic_gate(model, model_code)
                assert isinstance(result, GenericGateResult)
                assert not (result.passed and result.unmapped_identities)
                checked += 1
        assert checked > 0

        # Empirical baseline over the full committed catalog (including
        # hand-written models, whose variants still carry a dp list even
        # though the gate short-circuits before ever reaching the dpCode
        # check for them): 18 of the catalog's 90 variants declare the same
        # dpCode more than once anywhere in their dp list. This is the
        # ordinary multi-zone encoding (the same identity repeated on the
        # same dpCode across dpPort 1 and 2), not a rare quirk, so a future
        # catalog refresh that changes either number should force a
        # deliberate look rather than a silent pass.
        total_variants = 0
        duplicate_dp_code_variants = 0
        for variants in product_catalog_module._CATALOG.values():
            for record in variants.values():
                total_variants += 1
                codes = [entry.get("dpCode") for entry in record["dp"] if isinstance(entry, dict)]
                if len(codes) != len(set(codes)):
                    duplicate_dp_code_variants += 1
        assert total_variants == 90
        assert duplicate_dp_code_variants == 18


class TestDpCodeAmbiguityRule:
    """Tests for the "same dpCode declared more than once" gate rule.

    The runtime catalog matcher (_match_catalog_dp in api/generic_decoder.py)
    keys on dpCode alone and refuses to annotate a field whose dpCode is
    ambiguous, so an entity built over one of those entries would never
    resolve a value and would sit at None forever.
    """

    def test_duplicate_dp_code_across_status_entries_fails_whole_model(self, monkeypatch):
        dp_entries = [_dp("STA_TEM", dp_port=0, dp_code=9), _dp("STA_RSSI", dp_port=1, dp_code=9)]
        monkeypatch.setattr(generic_entities_module, "get_catalog_entry", lambda model, model_code=None: dp_entries)

        result = evaluate_generic_gate(FAKE_MODEL, None)

        assert result.passed is False
        assert result.datapoints == []
        assert len(result.blocked_by) == 1
        assert "dpCode" in result.blocked_by[0]
        assert "9" in result.blocked_by[0]

    def test_duplicate_dp_code_between_status_and_control_entry_fails_whole_model(self, monkeypatch):
        """Proves the check spans the full dp list, not just the status entries."""
        dp_entries = [_dp("STA_TEM", dp_port=0, dp_code=9), {"identity": "CTL_WATER", "dpPort": 0, "dpCode": 9}]
        monkeypatch.setattr(generic_entities_module, "get_catalog_entry", lambda model, model_code=None: dp_entries)

        result = evaluate_generic_gate(FAKE_MODEL, None)

        assert result.passed is False
        assert result.datapoints == []
        assert len(result.blocked_by) == 1
        assert "dpCode" in result.blocked_by[0]
        assert "9" in result.blocked_by[0]

    def test_distinct_dp_codes_still_passes(self, monkeypatch):
        dp_entries = [_dp("STA_TEM", dp_port=0, dp_code=9), _dp("STA_RSSI", dp_port=0, dp_code=10)]
        monkeypatch.setattr(generic_entities_module, "get_catalog_entry", lambda model, model_code=None: dp_entries)
        monkeypatch.setattr(generic_entities_module, "get_catalog_port_number", lambda model, model_code=None: 1)

        result = evaluate_generic_gate(FAKE_MODEL, None)

        assert result.passed is True
        assert len(result.datapoints) == 2
        assert result.blocked_by == ()

    def test_non_dict_entries_in_full_dp_list_are_skipped(self, monkeypatch):
        """The dpCode scan walks the raw catalog list directly, so it must tolerate malformed entries too."""
        dp_entries = ["not-a-dict", _dp("STA_TEM", dp_port=0, dp_code=9), _dp("STA_RSSI", dp_port=0, dp_code=10)]
        monkeypatch.setattr(generic_entities_module, "get_catalog_entry", lambda model, model_code=None: dp_entries)
        monkeypatch.setattr(generic_entities_module, "get_catalog_port_number", lambda model, model_code=None: 1)

        result = evaluate_generic_gate(FAKE_MODEL, None)

        assert result.passed is True
        assert len(result.datapoints) == 2

    def test_multiple_independent_rule_violations_are_all_reported_together(self, monkeypatch):
        """The core promise of the change: every independent rejection reason is surfaced, not just the first.

        STA_TEM has an unusable dpPort (rule 1) *and* shares dpCode 9 with
        STA_RSSI (rule 3). Both are independent grounds for rejection and
        fixing only one would still leave the variant blocked, so both must
        appear - in fixed rule order (dpPort problems before dpCode
        problems).
        """
        dp_entries = [_dp("STA_TEM", dp_port="bad", dp_code=9), _dp("STA_RSSI", dp_port=0, dp_code=9)]
        monkeypatch.setattr(generic_entities_module, "get_catalog_entry", lambda model, model_code=None: dp_entries)

        result = evaluate_generic_gate(FAKE_MODEL, None)

        assert result.passed is False
        assert len(result.blocked_by) == 2
        assert "dpPort" not in result.blocked_by[0]  # jargon rewritten to plain language
        assert "STA_TEM" in result.blocked_by[0]
        assert "usable port number" in result.blocked_by[0]
        assert "dpCode" in result.blocked_by[1]
        assert "9" in result.blocked_by[1]

    def test_two_dp_codes_each_reused_twice_produce_one_message_naming_both(self, monkeypatch):
        """Two separate dpCode collisions in one variant collapse into a single aggregated reason, not two."""
        dp_entries = [
            _dp("STA_TEM", dp_port=0, dp_code=9),
            _dp("STA_RSSI", dp_port=1, dp_code=9),
            _dp("STA_RSSI", dp_port=0, dp_code=20),
            {"identity": "CTL_WATER", "dpPort": 0, "dpCode": 20},
        ]
        monkeypatch.setattr(generic_entities_module, "get_catalog_entry", lambda model, model_code=None: dp_entries)

        result = evaluate_generic_gate(FAKE_MODEL, None)

        assert result.passed is False
        dp_code_reasons = [reason for reason in result.blocked_by if "dpCode" in reason]
        assert len(dp_code_reasons) == 1
        assert "9" in dp_code_reasons[0]
        assert "20" in dp_code_reasons[0]

    def test_duplicate_dp_codes_are_listed_in_numeric_order(self, monkeypatch):
        """Codes read 2, 15, 100 rather than the 100, 15, 2 a plain string sort would produce."""
        dp_entries = [
            _dp("STA_TEM", dp_port=0, dp_code=100),
            _dp("STA_TEM", dp_port=1, dp_code=100),
            _dp("STA_RSSI", dp_port=0, dp_code=15),
            _dp("STA_RSSI", dp_port=1, dp_code=15),
            _dp("STA_ALARM", dp_port=0, dp_code=2),
            _dp("STA_ALARM", dp_port=1, dp_code=2),
        ]
        monkeypatch.setattr(generic_entities_module, "get_catalog_entry", lambda model, model_code=None: dp_entries)

        result = evaluate_generic_gate(FAKE_MODEL, None)

        reason = next(r for r in result.blocked_by if "dpCode" in r)
        assert reason.index("dpCode 2") < reason.index("dpCode 15") < reason.index("dpCode 100")

    def test_non_integer_dp_codes_sort_after_integers_without_raising(self, monkeypatch):
        """A catalog carrying a non-integer dpCode still yields a totally ordered message rather than a TypeError."""
        dp_entries = [
            _dp("STA_TEM", dp_port=0, dp_code=None),
            _dp("STA_TEM", dp_port=1, dp_code=None),
            _dp("STA_RSSI", dp_port=0, dp_code=7),
            _dp("STA_RSSI", dp_port=1, dp_code=7),
        ]
        monkeypatch.setattr(generic_entities_module, "get_catalog_entry", lambda model, model_code=None: dp_entries)

        result = evaluate_generic_gate(FAKE_MODEL, None)

        reason = next(r for r in result.blocked_by if "dpCode" in r)
        assert reason.index("dpCode 7") < reason.index("dpCode None")


class TestRealCatalogMultiReasonRegression:
    """Regression coverage for the motivating bug: a variant blocked by multiple independent rules.

    HTV245FRF (model code 303) has a hand-written decoder in this repo, so it
    never actually reaches the generic gate at runtime - is_hand_written_model
    is patched here purely to exercise the gate against its real, committed
    catalog entry, which is exactly the shape a brand-new undecoded model
    would have: it reuses dpCode 2 (STA_ALARM) and several other codes across
    its two zones, AND most of its declared status identities have no
    curated row. Before this change, only the first of those two problems
    was ever reported.
    """

    def test_htv245frf_reports_both_the_dp_code_collision_and_the_uncurated_identities(self, monkeypatch):
        monkeypatch.setattr(generic_entities_module, "is_hand_written_model", lambda model: False)

        result = evaluate_generic_gate(MODEL_VALVE_245, 303)

        assert result.passed is False
        assert any("dpCode" in reason for reason in result.blocked_by)
        assert any("status readings have no verified definition" in reason for reason in result.blocked_by)
        assert result.unmapped_identities == (
            "STA_ALARM",
            "STA_BAT",
            "STA_DURATION",
            "STA_EVTIME",
            "STA_EVTIME2",
            "STA_LASTUSAGE",
            "STA_RSRP",
            "STA_WKSTATE",
        )


class TestDescribeGenericGate:
    """Tests for describe_generic_gate's two-key projection."""

    def test_returns_exactly_two_keys(self, monkeypatch):
        monkeypatch.setattr(generic_entities_module, "get_catalog_entry", lambda model, model_code=None: None)

        described = describe_generic_gate(FAKE_MODEL, None)

        assert set(described.keys()) == {"unmapped_generic_identities", "generic_gate_blocked_by"}

    def test_unmapped_identities_and_blocked_by_are_always_lists_never_none(self, monkeypatch):
        dp_entries = [_dp("STA_TEM", dp_port=0, dp_code=9)]
        monkeypatch.setattr(generic_entities_module, "get_catalog_entry", lambda model, model_code=None: dp_entries)
        monkeypatch.setattr(generic_entities_module, "get_catalog_port_number", lambda model, model_code=None: 1)

        described = describe_generic_gate(FAKE_MODEL, None)

        assert described["unmapped_generic_identities"] == []
        assert described["generic_gate_blocked_by"] == []

    def test_projects_the_evaluation_reason(self, monkeypatch):
        monkeypatch.setattr(generic_entities_module, "get_catalog_entry", lambda model, model_code=None: None)

        described = describe_generic_gate(FAKE_MODEL, None)

        assert described["generic_gate_blocked_by"]
        assert isinstance(described["generic_gate_blocked_by"], list)
        assert described["unmapped_generic_identities"] == []

    def test_blocked_by_carries_every_reason_as_a_list(self, monkeypatch):
        dp_entries = [_dp("STA_TEM", dp_port="bad", dp_code=9), _dp("STA_RSSI", dp_port=0, dp_code=9)]
        monkeypatch.setattr(generic_entities_module, "get_catalog_entry", lambda model, model_code=None: dp_entries)

        described = describe_generic_gate(FAKE_MODEL, None)

        assert len(described["generic_gate_blocked_by"]) == 2


# ---------------------------------------------------------------------------
# RainPointGenericSensor construction
# ---------------------------------------------------------------------------


class TestRainPointGenericSensorConstruction:
    """Tests for unique_id / name / icon / device_class / state_class construction."""

    def test_unique_id_exact_shape(self):
        dp_entry = _dp("STA_TEM", dp_port=0, dp_code=9)
        sensor = _make_generic_sensor(dp_entry, port_number=1)
        assert sensor._attr_unique_id == "rainpoint_100_200_1_generic_sta_tem_p0"

    def test_name_single_port_variant_omits_zone(self):
        dp_entry = _dp("STA_RSSI", dp_port=0)
        sensor = _make_generic_sensor(dp_entry, port_number=1)
        assert sensor._attr_name == "Garden Sensor Signal Strength (unverified)"

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
        dp_entry = _dp("STA_RSSI", dp_port=0)
        sensor = _make_generic_sensor(dp_entry, port_number=1)
        assert sensor._attr_state_class is None


# ---------------------------------------------------------------------------
# RainPointGenericSensor.native_value
# ---------------------------------------------------------------------------


class TestRainPointGenericSensorNativeValue:
    """Tests for native_value transform/range/validity handling."""

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
        dp_entry = _dp("STA_RSSI", dp_port=0, dp_code=10, data_type="U8")
        fields = [_decoded_field("STA_RSSI", 42, 0, width_mismatch=False)]
        sensor = _make_generic_sensor(dp_entry, port_number=1, data=_unknown_data(fields))

        attrs = sensor.extra_state_attributes

        assert attrs["catalog_derived"] is True
        assert attrs["identity"] == "STA_RSSI"
        assert attrs["dp_code"] == 10
        assert attrs["dp_port"] == 0
        assert attrs["dp_data_type"] == "U8"
        assert attrs["width_mismatch"] is False

    def test_width_mismatch_is_none_when_datapoint_absent_from_poll(self):
        dp_entry = _dp("STA_TEM", dp_port=0, dp_code=9)
        sensor = _make_generic_sensor(dp_entry, port_number=1, data=_unknown_data([]))

        attrs = sensor.extra_state_attributes

        assert attrs["width_mismatch"] is None

    def test_no_account_identifying_fields_leak_into_attributes(self):
        dp_entry = _dp("STA_RSSI", dp_port=0)
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
        dp_entries = [_dp("STA_TEM", dp_port=0, dp_code=9)]
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
        dp_entries = [_dp("STA_RSSI", dp_port=0, dp_code=10), _dp("STA_TEM", dp_port=0, dp_code=9)]
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
        dp_entries = [_dp("STA_TEM", dp_port=0, dp_code=9), _dp("STA_BAT", dp_port=0, dp_code=11)]
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


class TestCountGenericEligibleDevices:
    """count_generic_eligible_devices reports what the options toggle would actually do.

    The options form states these numbers, so a user can see up front that
    enabling the toggle may add nothing rather than enabling it, seeing no new
    entities, and concluding the integration is broken.
    """

    @staticmethod
    def _entry(model: str, decoded_type: str = "unknown") -> dict:
        return {"model": model, "data": {"type": decoded_type, "model": model}}

    def test_no_data_reports_zero_of_zero(self):
        """Absent coordinator data reports zero without raising."""
        assert count_generic_eligible_devices(None) == (0, 0)

    def test_devices_with_a_working_decoder_are_not_counted_as_unsupported(self):
        """A decoded device is outside the generic path, so it is not in the denominator."""
        data = {"sensors": {"a": self._entry("HTV245FRF", decoded_type="valve")}}

        assert count_generic_eligible_devices(data) == (0, 0)

    def test_unsupported_but_ungated_device_counts_only_in_the_denominator(self, monkeypatch):
        """An unsupported device the gate rejects raises the total but not the eligible count."""
        monkeypatch.setattr(generic_entities_module, "is_hand_written_model", lambda model: False)
        monkeypatch.setattr(generic_entities_module, "get_catalog_entry", lambda model, model_code=None: None)
        data = {"sensors": {"a": self._entry(FAKE_MODEL)}}

        assert count_generic_eligible_devices(data) == (0, 1)

    def test_unsupported_and_fully_curated_device_counts_as_eligible(self, monkeypatch):
        """A device whose every declared reading is curated is reported as eligible."""
        dp_entries = [_dp("STA_TEM", dp_port=0, dp_code=9), _dp("STA_RSSI", dp_port=1, dp_code=10)]
        monkeypatch.setattr(generic_entities_module, "is_hand_written_model", lambda model: False)
        monkeypatch.setattr(generic_entities_module, "get_catalog_entry", lambda model, model_code=None: dp_entries)
        monkeypatch.setattr(generic_entities_module, "get_catalog_port_number", lambda model, model_code=None: 1)
        data = {"sensors": {"a": self._entry(FAKE_MODEL), "b": self._entry(FAKE_MODEL, decoded_type="valve")}}

        assert count_generic_eligible_devices(data) == (1, 1)

    def test_malformed_coordinator_data_degrades_to_zero_rather_than_raising(self):
        """A sensors value that is not a mapping degrades to zero instead of breaking the options form."""
        assert count_generic_eligible_devices({"sensors": 5}) == (0, 0)
