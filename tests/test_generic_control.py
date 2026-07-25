"""Tests for generic_control.py (opt-in, catalog-driven generic control write path)."""

from __future__ import annotations

import copy
from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.rainpoint import generic_control as generic_control_module
from custom_components.rainpoint.api import get_catalog_variant_codes
from custom_components.rainpoint.api import product_catalog as product_catalog_module
from custom_components.rainpoint.api.product_catalog import UNCODED_VARIANT
from custom_components.rainpoint.const import (
    CONF_GENERIC_CONTROL_ENABLED,
    DOMAIN,
    GENERIC_CONTROL_REFRESH_DELAY_SECONDS,
    GENERIC_CONTROL_UNIQUE_ID_MARKER,
    GENERIC_UNIQUE_ID_MARKER,
    HAND_WRITTEN_MODELS,
    MODEL_VALVE_245,
)
from custom_components.rainpoint.generic_control import (
    CONTROL_IDENTITY_ALLOWLIST,
    DEFAULT_CONTROL_DURATION_SECONDS,
    RUN_STATE_IDENTITY,
    ControlDatapoint,
    ControlGateResult,
    RainPointGenericValve,
    build_generic_control_entities,
    count_generic_control_eligible_devices,
    describe_control_gate,
    evaluate_control_gate,
    resolve_control_port,
)
from tests.helpers import make_coordinator_data, make_sensor_entry

ANCHOR_MODEL = "HTV103FRF"
ANCHOR_MODEL_CODE = 31

_SENTINEL = object()


def _run_state_field(dp_port: int, value: int) -> dict:
    """Build one decode_generic field entry for STA_WKSTATE, catalog-annotated."""
    return {
        "name": RUN_STATE_IDENTITY,
        "index": 30,
        "dp_id": 30,
        "raw": f"{value:02x}",
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


def _unknown_data(model: str, fields: list[dict] | None = None) -> dict:
    """Build the {"type": "unknown", ...} decoded-payload shape the control path requires."""
    fields = fields if fields is not None else [_run_state_field(1, 1)]
    return {
        "type": "unknown",
        "model": model,
        "raw_value": "11#00",
        "generic": {"decoder": "generic-tlv", "fields": fields, "field_names": [f["name"] for f in fields]},
    }


def _anchor_sensor_info(sub_name: str = "Valve Hub 1", fields: list[dict] | None = None, model: str = ANCHOR_MODEL) -> dict:
    entry = make_sensor_entry(hid=100, mid=200, addr=1, model=model, sub_name=sub_name, data=_unknown_data(model, fields))
    entry["model_code"] = ANCHOR_MODEL_CODE
    entry["device_name"] = "dev1"
    entry["product_key"] = "pk1"
    return entry


def _make_coordinator(sensor_key: str, sensor_info: dict):
    coordinator = MagicMock()
    coordinator.data = make_coordinator_data(sensors={sensor_key: sensor_info})
    coordinator._client = MagicMock()
    coordinator._client.control_work_mode = AsyncMock(return_value=None)
    coordinator.async_request_refresh = AsyncMock()
    return coordinator


def _build_anchor_valve(sensor_key: str = "100_200_1", fields: list[dict] | None = None, sub_name: str = "Valve Hub 1"):
    sensor_info = _anchor_sensor_info(sub_name=sub_name, fields=fields)
    coordinator = _make_coordinator(sensor_key, sensor_info)
    entities = build_generic_control_entities(coordinator, sensor_key, sensor_info, sensor_key)
    assert len(entities) == 1
    entity = entities[0]
    entity.hass = MagicMock()
    return entity, coordinator, sensor_info


def _make_hass_and_entry(coordinator, options: dict):
    hass = MagicMock()
    entry = MagicMock()
    entry.entry_id = "test_entry"
    entry.options = options
    hass.data = {DOMAIN: {"test_entry": {"coordinator": coordinator}}}
    return hass, entry


# ---------------------------------------------------------------------------
# resolve_control_port
# ---------------------------------------------------------------------------


class TestResolveControlPort:
    def test_port_one_or_greater_resolves_to_itself(self):
        assert resolve_control_port(1, 1) == 1
        assert resolve_control_port(3, 4) == 3

    def test_port_zero_resolves_to_one_on_single_port_variant(self):
        assert resolve_control_port(0, 1) == 1

    def test_port_zero_refused_on_multi_port_variant(self):
        assert resolve_control_port(0, 4) is None

    def test_port_zero_refused_when_port_number_unknown(self):
        assert resolve_control_port(0, None) is None

    @pytest.mark.parametrize("bad_port", [None, "1", True, False, -1, [1]])
    def test_unusable_port_refused(self, bad_port):
        assert resolve_control_port(bad_port, 1) is None


# ---------------------------------------------------------------------------
# evaluate_control_gate: terminal / whole-model rules
# ---------------------------------------------------------------------------


class TestEvaluateControlGateTerminalRules:
    def test_hand_written_model_is_refused(self):
        model = sorted(HAND_WRITTEN_MODELS)[0]

        result = evaluate_control_gate(model, None)

        assert result.passed is False
        assert len(result.blocked_by) == 1
        assert "hand-written" in result.blocked_by[0]

    def test_every_hand_written_model_is_refused_across_every_modelcode(self):
        for model in HAND_WRITTEN_MODELS:
            for code in get_catalog_variant_codes(model) or (None,):
                result = evaluate_control_gate(model, code)
                assert result.passed is False

    def test_model_absent_from_catalog_is_refused(self, monkeypatch):
        monkeypatch.setattr(generic_control_module, "get_catalog_entry", lambda model, model_code=None: None)
        monkeypatch.setattr(generic_control_module, "get_catalog_variant_codes", lambda model: (), raising=False)

        result = evaluate_control_gate("FAKE_MODEL", None)

        assert result.passed is False
        assert len(result.blocked_by) == 1
        assert "not in the product catalog" in result.blocked_by[0]

    def test_model_present_with_empty_dp_list_is_refused_distinctly(self, monkeypatch):
        monkeypatch.setattr(generic_control_module, "get_catalog_entry", lambda model, model_code=None: [])

        result = evaluate_control_gate("FAKE_MODEL", None)

        assert result.passed is False
        assert "no readings" in result.blocked_by[0]

    def test_model_of_none_is_refused(self, monkeypatch):
        monkeypatch.setattr(generic_control_module, "get_catalog_entry", lambda model, model_code=None: None)
        monkeypatch.setattr(generic_control_module, "get_catalog_variant_codes", lambda model: (), raising=False)

        result = evaluate_control_gate(None, None)

        assert result.passed is False
        assert result.blocked_by

    def test_variant_declaring_no_allowlisted_identity_is_refused(self, monkeypatch):
        dp_entries = [{"dpCode": 1, "identity": "STA_TEM", "dpPort": 0}]
        monkeypatch.setattr(generic_control_module, "is_hand_written_model", lambda model: False)
        monkeypatch.setattr(generic_control_module, "get_catalog_entry", lambda model, model_code=None: dp_entries)
        monkeypatch.setattr(generic_control_module, "get_catalog_port_number", lambda model, model_code=None: 1)

        result = evaluate_control_gate("FAKE_MODEL", None)

        assert result.passed is False
        assert "no allowlisted control identity" in result.blocked_by[0]

    def test_catalog_lookup_raising_never_propagates(self, monkeypatch):
        def _boom(model, model_code=None):
            raise RuntimeError("boom")

        monkeypatch.setattr(generic_control_module, "get_catalog_entry", _boom)

        result = evaluate_control_gate("FAKE_MODEL", None)

        assert result.passed is False
        assert result.datapoints == ()
        assert result.blocked_by == ("the product catalog could not be read",)

    def test_two_consecutive_evaluations_of_the_same_variant_are_equal(self):
        first = evaluate_control_gate("HTV214FRF", 288)
        second = evaluate_control_gate("HTV214FRF", 288)

        assert first.blocked_by == second.blocked_by
        assert first.datapoints == second.datapoints


# ---------------------------------------------------------------------------
# evaluate_control_gate: per-datapoint port resolution, against real catalog data
# ---------------------------------------------------------------------------


class TestEvaluateControlGateRealCatalog:
    def test_anchor_single_port_variant_admits_one_datapoint_at_command_port_one(self):
        result = evaluate_control_gate(ANCHOR_MODEL, ANCHOR_MODEL_CODE)

        assert result.passed is True
        assert len(result.datapoints) == 1
        dp = result.datapoints[0]
        assert dp.identity == "CTL_WATER"
        assert dp.dp_port == 1
        assert dp.command_port == 1
        assert result.port_number == 1

    def test_two_zone_variant_admits_both_ports_in_order(self):
        result = evaluate_control_gate("HTV214FRF", 288)

        assert result.passed is True
        assert [dp.dp_port for dp in result.datapoints] == [1, 2]
        assert [dp.command_port for dp in result.datapoints] == [1, 2]
        assert all(dp.identity == "CTL_WATER" for dp in result.datapoints)

    def test_multi_zone_variant_declaring_port_zero_is_refused(self):
        result = evaluate_control_gate("HIC406B", 40)

        assert result.passed is False
        assert result.blocked_by
        assert "no resolvable command port" in result.blocked_by[0]

    def test_reusing_one_dp_code_across_zones_does_not_block_the_variant(self):
        """HTV214FRF reuses dpCode 1 for CTL_WATER across both its zones; the control
        path resolves ports by declared port, not dpCode uniqueness, so this must not
        block it -- unlike the sensor gate's dpCode rule, deliberately not carried over.
        """
        result = evaluate_control_gate("HTV214FRF", 288)

        assert result.passed is True

    def test_port_pairing_invariant_holds_across_the_full_committed_catalog(self):
        """For every one of the 34 allowlist-touching variants, the set of ports on its
        allowlisted control datapoints equals the set on its STA_WKSTATE datapoints --
        this is what makes port pairing exact rather than heuristic (D-05).
        """
        checked = 0
        for variants in product_catalog_module._CATALOG.values():
            for record in variants.values():
                dp_list = record["dp"]
                ctl_ports = {
                    e.get("dpPort") for e in dp_list if isinstance(e, dict) and e.get("identity") in CONTROL_IDENTITY_ALLOWLIST
                }
                if not ctl_ports:
                    continue
                checked += 1
                wk_ports = {e.get("dpPort") for e in dp_list if isinstance(e, dict) and e.get("identity") == RUN_STATE_IDENTITY}
                assert ctl_ports == wk_ports
        assert checked == 34


# ---------------------------------------------------------------------------
# evaluate_control_gate: synthetic edge shapes
# ---------------------------------------------------------------------------


class TestEvaluateControlGateSynthetic:
    def test_port_zero_admitted_at_command_port_one_on_synthetic_single_port_variant(self, monkeypatch):
        """No committed variant declares a control dp on port zero with portNumber == 1;
        this shape is covered synthetically per D-06.
        """
        dp_entries = [
            {"dpCode": 1, "identity": "CTL_WATER", "dpPort": 0},
            {"dpCode": 30, "identity": "STA_WKSTATE", "dpPort": 0},
        ]
        monkeypatch.setattr(generic_control_module, "is_hand_written_model", lambda model: False)
        monkeypatch.setattr(generic_control_module, "get_catalog_entry", lambda model, model_code=None: dp_entries)
        monkeypatch.setattr(generic_control_module, "get_catalog_port_number", lambda model, model_code=None: 1)

        result = evaluate_control_gate("FAKE_MODEL", None)

        assert result.passed is True
        dp = result.datapoints[0]
        assert dp.dp_port == 0
        assert dp.command_port == 1

    def test_list_valued_dp_port_produces_a_specific_reason_without_raising(self, monkeypatch):
        dp_entries = [
            {"dpCode": 1, "identity": "CTL_WATER", "dpPort": [1, 2]},
            {"dpCode": 30, "identity": "STA_WKSTATE", "dpPort": 1},
        ]
        monkeypatch.setattr(generic_control_module, "is_hand_written_model", lambda model: False)
        monkeypatch.setattr(generic_control_module, "get_catalog_entry", lambda model, model_code=None: dp_entries)
        monkeypatch.setattr(generic_control_module, "get_catalog_port_number", lambda model, model_code=None: 1)

        result = evaluate_control_gate("FAKE_MODEL", None)

        assert result.passed is False
        assert result.blocked_by
        assert "could not be read" not in result.blocked_by[0]

    def test_two_separately_unresolvable_datapoints_report_two_reasons(self, monkeypatch):
        dp_entries = [
            {"dpCode": 1, "identity": "CTL_WATER", "dpPort": 0},  # unresolved: portNumber=3
            {"dpCode": 2, "identity": "CTL_BT_WATER", "dpPort": True},  # unresolved: bool is never a usable port
        ]
        monkeypatch.setattr(generic_control_module, "is_hand_written_model", lambda model: False)
        monkeypatch.setattr(generic_control_module, "get_catalog_entry", lambda model, model_code=None: dp_entries)
        monkeypatch.setattr(generic_control_module, "get_catalog_port_number", lambda model, model_code=None: 3)

        result = evaluate_control_gate("FAKE_MODEL", None)

        assert result.passed is False
        assert len(result.blocked_by) == 2

    def test_non_dict_entries_in_dp_list_are_skipped(self, monkeypatch):
        dp_entries = [
            "not-a-dict",
            {"dpCode": 1, "identity": "CTL_WATER", "dpPort": 1},
            {"dpCode": 30, "identity": "STA_WKSTATE", "dpPort": 1},
        ]
        monkeypatch.setattr(generic_control_module, "is_hand_written_model", lambda model: False)
        monkeypatch.setattr(generic_control_module, "get_catalog_entry", lambda model, model_code=None: dp_entries)
        monkeypatch.setattr(generic_control_module, "get_catalog_port_number", lambda model, model_code=None: 1)

        result = evaluate_control_gate("FAKE_MODEL", None)

        assert result.passed is True
        assert len(result.datapoints) == 1

    def test_port_number_lookup_raising_yields_fail_closed_result(self, monkeypatch):
        dp_entries = [{"dpCode": 1, "identity": "CTL_WATER", "dpPort": 1}]
        monkeypatch.setattr(generic_control_module, "is_hand_written_model", lambda model: False)
        monkeypatch.setattr(generic_control_module, "get_catalog_entry", lambda model, model_code=None: dp_entries)

        def _boom(model, model_code=None):
            raise RuntimeError("boom")

        monkeypatch.setattr(generic_control_module, "get_catalog_port_number", _boom)

        result = evaluate_control_gate("FAKE_MODEL", None)

        assert result.passed is False
        assert result.blocked_by

    def test_control_datapoint_with_no_paired_run_state_datapoint_is_refused(self, monkeypatch):
        """A control dp whose port resolves fine but has no run-state datapoint declared
        at all on that port is still refused: a resolvable port is not enough on its
        own -- GCTL-04's confirm-by-re-poll needs a readable state to confirm against.
        """
        dp_entries = [{"dpCode": 1, "identity": "CTL_WATER", "dpPort": 1}]  # no STA_WKSTATE anywhere
        monkeypatch.setattr(generic_control_module, "is_hand_written_model", lambda model: False)
        monkeypatch.setattr(generic_control_module, "get_catalog_entry", lambda model, model_code=None: dp_entries)
        monkeypatch.setattr(generic_control_module, "get_catalog_port_number", lambda model, model_code=None: 1)

        result = evaluate_control_gate("FAKE_MODEL", None)

        assert result.passed is False
        assert "0 matching run-state readings" in result.blocked_by[0]

    def test_control_datapoint_with_two_ambiguous_run_state_matches_is_refused(self, monkeypatch):
        """Two run-state datapoints sharing one port make the state to confirm against
        ambiguous, so the control datapoint is refused rather than guessing which one.
        """
        dp_entries = [
            {"dpCode": 1, "identity": "CTL_WATER", "dpPort": 1},
            {"dpCode": 30, "identity": "STA_WKSTATE", "dpPort": 1},
            {"dpCode": 31, "identity": "STA_WKSTATE", "dpPort": 1},
        ]
        monkeypatch.setattr(generic_control_module, "is_hand_written_model", lambda model: False)
        monkeypatch.setattr(generic_control_module, "get_catalog_entry", lambda model, model_code=None: dp_entries)
        monkeypatch.setattr(generic_control_module, "get_catalog_port_number", lambda model, model_code=None: 1)

        result = evaluate_control_gate("FAKE_MODEL", None)

        assert result.passed is False
        assert "2 matching run-state readings" in result.blocked_by[0]


# ---------------------------------------------------------------------------
# evaluate_control_gate: override rule
# ---------------------------------------------------------------------------


class TestOverrideRule:
    @staticmethod
    def _synthetic_entry(dp_port: int = 1) -> list[dict]:
        return [
            {"dpCode": 1, "identity": "CTL_WATER", "dpPort": dp_port},
            {"dpCode": 30, "identity": "STA_WKSTATE", "dpPort": dp_port},
        ]

    def _patch_catalog(self, monkeypatch, override):
        monkeypatch.setattr(generic_control_module, "is_hand_written_model", lambda model: False)
        monkeypatch.setattr(generic_control_module, "get_catalog_entry", lambda model, model_code=None: self._synthetic_entry())
        monkeypatch.setattr(generic_control_module, "get_catalog_port_number", lambda model, model_code=None: 1)
        monkeypatch.setattr(generic_control_module, "GENERIC_CONTROL_OVERRIDE_DISABLED", override)

    def test_override_disables_exactly_that_variant(self, monkeypatch):
        self._patch_catalog(monkeypatch, frozenset({("FAKE_MODEL", "1")}))

        result = evaluate_control_gate("FAKE_MODEL", 1)

        assert result.passed is False
        assert any("force-disabled" in reason for reason in result.blocked_by)

    def test_sibling_variant_under_a_different_modelcode_is_unaffected(self, monkeypatch):
        self._patch_catalog(monkeypatch, frozenset({("FAKE_MODEL", "1")}))

        result = evaluate_control_gate("FAKE_MODEL", 2)

        assert result.passed is True

    def test_uncoded_bucket_override_disables_when_device_reports_no_model_code(self, monkeypatch):
        self._patch_catalog(monkeypatch, frozenset({("FAKE_MODEL", UNCODED_VARIANT)}))

        result = evaluate_control_gate("FAKE_MODEL", None)

        assert result.passed is False

    def test_empty_override_set_disables_nothing(self, monkeypatch):
        self._patch_catalog(monkeypatch, frozenset())

        result = evaluate_control_gate("FAKE_MODEL", 1)

        assert result.passed is True

    def test_override_check_happens_before_the_catalog_lookup(self, monkeypatch):
        """The override rule must be evaluated even when the catalog lookup would fail."""

        def _boom(model, model_code=None):
            raise AssertionError("get_catalog_entry must not be reached when the override rule refuses first")

        monkeypatch.setattr(generic_control_module, "is_hand_written_model", lambda model: False)
        monkeypatch.setattr(generic_control_module, "get_catalog_entry", _boom)
        monkeypatch.setattr(generic_control_module, "get_catalog_port_number", lambda model, model_code=None: None)
        monkeypatch.setattr(generic_control_module, "GENERIC_CONTROL_OVERRIDE_DISABLED", frozenset({("FAKE_MODEL", "1")}))

        result = evaluate_control_gate("FAKE_MODEL", 1)

        assert result.passed is False


# ---------------------------------------------------------------------------
# describe_control_gate
# ---------------------------------------------------------------------------


class TestDescribeControlGate:
    def test_returns_exactly_one_key(self, monkeypatch):
        monkeypatch.setattr(generic_control_module, "get_catalog_entry", lambda model, model_code=None: None)

        described = describe_control_gate("FAKE_MODEL", None)

        assert set(described.keys()) == {"generic_control_blocked_by"}

    def test_value_is_always_a_list_never_none(self):
        described = describe_control_gate("HTV214FRF", 288)

        assert described["generic_control_blocked_by"] == []

    def test_never_raises(self, monkeypatch):
        def _boom(model, model_code=None):
            raise RuntimeError("boom")

        monkeypatch.setattr(generic_control_module, "get_catalog_entry", _boom)

        described = describe_control_gate("FAKE_MODEL", None)

        assert described["generic_control_blocked_by"]

    def test_holds_no_logic_of_its_own(self):
        """describe_control_gate is a pure projection: it must agree with evaluate_control_gate."""
        result = evaluate_control_gate("HIC406B", 40)

        described = describe_control_gate("HIC406B", 40)

        assert described["generic_control_blocked_by"] == list(result.blocked_by)


# ---------------------------------------------------------------------------
# count_generic_control_eligible_devices
# ---------------------------------------------------------------------------


class TestCountGenericControlEligibleDevices:
    def test_no_data_reports_zero_of_zero(self):
        assert count_generic_control_eligible_devices(None) == (0, 0)

    def test_devices_with_a_working_decoder_are_not_counted_as_unsupported(self):
        data = {"sensors": {"a": {"model": MODEL_VALVE_245, "data": {"type": "valve"}}}}

        assert count_generic_control_eligible_devices(data) == (0, 0)

    def test_unsupported_but_ungated_device_counts_only_in_the_denominator(self, monkeypatch):
        monkeypatch.setattr(generic_control_module, "is_hand_written_model", lambda model: False)
        monkeypatch.setattr(generic_control_module, "get_catalog_entry", lambda model, model_code=None: None)
        data = {"sensors": {"a": {"model": "FAKE_MODEL", "data": {"type": "unknown"}}}}

        assert count_generic_control_eligible_devices(data) == (0, 1)

    def test_unsupported_and_eligible_device_counts_as_eligible(self):
        data = {"sensors": {"a": {"model": ANCHOR_MODEL, "model_code": ANCHOR_MODEL_CODE, "data": {"type": "unknown"}}}}

        assert count_generic_control_eligible_devices(data) == (1, 1)

    def test_malformed_coordinator_data_degrades_to_zero_rather_than_raising(self):
        assert count_generic_control_eligible_devices({"sensors": 5}) == (0, 0)


# ---------------------------------------------------------------------------
# build_generic_control_entities
# ---------------------------------------------------------------------------


class TestBuildGenericControlEntities:
    def test_non_unknown_payload_yields_nothing(self):
        sensor_info = make_sensor_entry(model=ANCHOR_MODEL, data={"type": "valve"})
        sensor_info["model_code"] = ANCHOR_MODEL_CODE
        coordinator = _make_coordinator("100_200_1", sensor_info)

        assert build_generic_control_entities(coordinator, "100_200_1", sensor_info, "100_200_1") == []

    def test_no_data_yields_nothing(self):
        sensor_info = make_sensor_entry(model=ANCHOR_MODEL, data=None)
        sensor_info["model_code"] = ANCHOR_MODEL_CODE
        coordinator = _make_coordinator("100_200_1", sensor_info)

        assert build_generic_control_entities(coordinator, "100_200_1", sensor_info, "100_200_1") == []

    def test_hand_written_model_yields_nothing_even_with_unknown_type_data(self):
        sensor_info = _anchor_sensor_info(model=MODEL_VALVE_245)
        coordinator = _make_coordinator("100_200_1", sensor_info)

        assert build_generic_control_entities(coordinator, "100_200_1", sensor_info, "100_200_1") == []

    def test_gate_failure_yields_nothing(self, monkeypatch):
        monkeypatch.setattr(generic_control_module, "is_hand_written_model", lambda model: False)
        monkeypatch.setattr(generic_control_module, "get_catalog_entry", lambda model, model_code=None: None)
        sensor_info = make_sensor_entry(model="FAKE_MODEL", data=_unknown_data("FAKE_MODEL"))
        coordinator = _make_coordinator("100_200_1", sensor_info)

        assert build_generic_control_entities(coordinator, "100_200_1", sensor_info, "100_200_1") == []

    def test_fully_eligible_anchor_yields_one_valve(self):
        sensor_info = _anchor_sensor_info()
        coordinator = _make_coordinator("100_200_1", sensor_info)

        entities = build_generic_control_entities(coordinator, "100_200_1", sensor_info, "100_200_1")

        assert len(entities) == 1
        assert isinstance(entities[0], RainPointGenericValve)

    def test_switch_identity_is_skipped_rather_than_built(self, monkeypatch):
        """CTL_SOCK admits a datapoint at the gate, but the switch branch is not
        implemented in this plan -- it must be silently skipped, not raise.
        """
        dp = ControlDatapoint(identity="CTL_SOCK", dp_port=1, command_port=1, dp_code=2, dp_data_type="")
        fake_result = ControlGateResult(datapoints=(dp,), blocked_by=(), port_number=1)
        monkeypatch.setattr(generic_control_module, "evaluate_control_gate", lambda model, model_code=None: fake_result)
        sensor_info = make_sensor_entry(model="FAKE_MODEL", data=_unknown_data("FAKE_MODEL"))
        coordinator = _make_coordinator("100_200_1", sensor_info)

        entities = build_generic_control_entities(coordinator, "100_200_1", sensor_info, "100_200_1")

        assert entities == []

    def test_unrecognized_identity_is_silently_skipped(self, monkeypatch):
        """Defensive branch: every admitted datapoint's identity is, by construction,
        in VALVE_CONTROL_IDENTITIES or SWITCH_CONTROL_IDENTITIES (their union is
        CONTROL_IDENTITY_ALLOWLIST, which is what the gate filters on). This proves
        an identity in neither set is simply skipped rather than raising.
        """
        dp = ControlDatapoint(identity="CTL_UNKNOWN", dp_port=1, command_port=1, dp_code=9, dp_data_type="")
        fake_result = ControlGateResult(datapoints=(dp,), blocked_by=(), port_number=1)
        monkeypatch.setattr(generic_control_module, "evaluate_control_gate", lambda model, model_code=None: fake_result)
        sensor_info = make_sensor_entry(model="FAKE_MODEL", data=_unknown_data("FAKE_MODEL"))
        coordinator = _make_coordinator("100_200_1", sensor_info)

        entities = build_generic_control_entities(coordinator, "100_200_1", sensor_info, "100_200_1")

        assert entities == []

    def test_entity_construction_failure_after_gate_pass_is_caught(self, monkeypatch):
        bad_result = ControlGateResult(
            datapoints=(ControlDatapoint(identity="CTL_WATER", dp_port=1, command_port=1, dp_code=1, dp_data_type=""),),
            blocked_by=(),
            port_number=1,
        )
        monkeypatch.setattr(generic_control_module, "evaluate_control_gate", lambda model, model_code=None: bad_result)
        monkeypatch.setattr(
            generic_control_module,
            "RainPointGenericValve",
            MagicMock(side_effect=RuntimeError("boom")),
        )
        sensor_info = make_sensor_entry(model="FAKE_MODEL", data=_unknown_data("FAKE_MODEL"))
        coordinator = _make_coordinator("100_200_1", sensor_info)

        assert build_generic_control_entities(coordinator, "100_200_1", sensor_info, "100_200_1") == []


# ---------------------------------------------------------------------------
# RainPointGenericControlBase / RainPointGenericValve construction
# ---------------------------------------------------------------------------


class TestRainPointGenericValveConstruction:
    def test_unique_id_contains_the_full_control_marker(self):
        entity, _, _ = _build_anchor_valve()

        assert entity._attr_unique_id == "rainpoint_100_200_1_generic_ctl_ctl_water_p1"
        assert GENERIC_CONTROL_UNIQUE_ID_MARKER in entity._attr_unique_id
        # The control marker nests the sensor marker (option-a); assert the
        # FULL control marker, never a prefix, so a sloppy substring test
        # cannot silently pass on a sensor-namespace unique_id too.
        assert GENERIC_UNIQUE_ID_MARKER in entity._attr_unique_id

    def test_sensor_namespace_unique_id_does_not_contain_the_control_marker(self):
        sensor_unique_id = "rainpoint_100_200_1_generic_sta_tem_p0"
        assert GENERIC_UNIQUE_ID_MARKER in sensor_unique_id
        assert GENERIC_CONTROL_UNIQUE_ID_MARKER not in sensor_unique_id

    def test_name_single_port_variant_omits_zone(self):
        entity, _, _ = _build_anchor_valve(sub_name="Garden Valve")

        assert entity._attr_name == "Garden Valve (unverified)"

    def test_name_multi_port_variant_includes_zone(self):
        sensor_info = make_sensor_entry(hid=1, mid=2, addr=1, model="HTV214FRF", sub_name="Yard", data=_unknown_data("HTV214FRF"))
        sensor_info["model_code"] = 288
        coordinator = _make_coordinator("1_2_1", sensor_info)

        entities = build_generic_control_entities(coordinator, "1_2_1", sensor_info, "1_2_1")

        names = sorted(e._attr_name for e in entities)
        assert names == ["Yard Zone 1 (unverified)", "Yard Zone 2 (unverified)"]

    def test_device_info_matches_the_sub_device_card(self):
        entity, _, _ = _build_anchor_valve()

        info = entity.device_info
        assert info["identifiers"] == {(DOMAIN, "100_200_1")}
        assert info["manufacturer"] == "RainPoint"
        assert info["model"] == ANCHOR_MODEL

    def test_icon_is_the_control_marker_icon(self):
        entity, _, _ = _build_anchor_valve()

        assert entity._attr_icon == generic_control_module.GENERIC_CONTROL_MARKER_ICON

    def test_reports_position_false_and_open_close_supported(self):
        entity, _, _ = _build_anchor_valve()

        assert entity._attr_reports_position is False


# ---------------------------------------------------------------------------
# RainPointGenericValve.is_closed / run-state reading
# ---------------------------------------------------------------------------


class TestRunStateReading:
    def test_is_closed_false_when_run_state_open(self):
        entity, _, _ = _build_anchor_valve(fields=[_run_state_field(1, 1)])
        assert entity.is_closed is False

    def test_is_closed_true_when_run_state_closed(self):
        entity, _, _ = _build_anchor_valve(fields=[_run_state_field(1, 0)])
        assert entity.is_closed is True

    def test_is_closed_none_when_no_matching_field(self):
        entity, _, _ = _build_anchor_valve(fields=[])
        assert entity.is_closed is None

    def test_is_closed_none_when_sensor_key_absent(self):
        entity, coordinator, _ = _build_anchor_valve()
        coordinator.data["sensors"].pop("100_200_1")
        assert entity.is_closed is None

    def test_is_closed_none_when_data_absent(self):
        entity, coordinator, _ = _build_anchor_valve()
        coordinator.data["sensors"]["100_200_1"]["data"] = None
        assert entity.is_closed is None

    def test_is_closed_none_when_raw_value_not_an_int(self):
        entity, coordinator, _ = _build_anchor_valve()
        field = coordinator.data["sensors"]["100_200_1"]["data"]["generic"]["fields"][0]
        field["value"] = "1"
        assert entity.is_closed is None

    def test_is_closed_none_when_raw_value_is_bool(self):
        entity, coordinator, _ = _build_anchor_valve()
        field = coordinator.data["sensors"]["100_200_1"]["data"]["generic"]["fields"][0]
        field["value"] = True
        assert entity.is_closed is None

    def test_run_state_none_when_transform_yields_neither_zero_nor_one(self, monkeypatch):
        """Defensive branch: STA_WKSTATE's real transform is a bit-zero mask, always
        0.0 or 1.0, so this can only be exercised by substituting the curated spec.
        """
        from dataclasses import replace

        from custom_components.rainpoint.generic_entities import _IDENTITY_SPECS

        original = _IDENTITY_SPECS[RUN_STATE_IDENTITY]
        monkeypatch.setitem(_IDENTITY_SPECS, RUN_STATE_IDENTITY, replace(original, transform=lambda raw: 0.5))

        entity, _, _ = _build_anchor_valve(fields=[_run_state_field(1, 1)])

        assert entity.is_closed is None

    def test_higher_bits_are_masked_off(self):
        """The device reports 0x21/0x20 on one hand-written decoder; bit zero alone decides."""
        entity, _, _ = _build_anchor_valve(fields=[_run_state_field(1, 0x21)])
        assert entity.is_closed is False

    def test_mutating_run_state_flips_reported_value(self):
        entity, coordinator, _ = _build_anchor_valve(fields=[_run_state_field(1, 1)])
        assert entity.is_closed is False

        coordinator.data["sensors"]["100_200_1"]["data"]["generic"]["fields"][0]["value"] = 0
        assert entity.is_closed is True


# ---------------------------------------------------------------------------
# RainPointGenericValve control (open/close, refresh scheduling, no optimism)
# ---------------------------------------------------------------------------


class TestRainPointGenericValveControl:
    @pytest.mark.asyncio
    async def test_open_calls_control_work_mode_with_resolved_port_and_default_duration(self, monkeypatch):
        entity, coordinator, _ = _build_anchor_valve()
        call_later = MagicMock(return_value=MagicMock())
        monkeypatch.setattr(generic_control_module, "async_call_later", call_later)

        await entity.async_open_valve()

        coordinator._client.control_work_mode.assert_awaited_once_with(
            mid=200,
            addr=1,
            device_name="dev1",
            product_key="pk1",
            port=1,
            mode=1,
            duration=DEFAULT_CONTROL_DURATION_SECONDS,
        )

    @pytest.mark.asyncio
    async def test_open_with_explicit_duration_uses_it(self, monkeypatch):
        entity, coordinator, _ = _build_anchor_valve()
        monkeypatch.setattr(generic_control_module, "async_call_later", MagicMock(return_value=MagicMock()))

        await entity.async_open_valve(duration=90)

        _, kwargs = coordinator._client.control_work_mode.call_args
        assert kwargs["duration"] == 90
        assert kwargs["mode"] == 1

    @pytest.mark.asyncio
    async def test_close_calls_control_work_mode_with_mode_zero_duration_zero(self, monkeypatch):
        entity, coordinator, _ = _build_anchor_valve()
        monkeypatch.setattr(generic_control_module, "async_call_later", MagicMock(return_value=MagicMock()))

        await entity.async_close_valve()

        coordinator._client.control_work_mode.assert_awaited_once_with(
            mid=200,
            addr=1,
            device_name="dev1",
            product_key="pk1",
            port=1,
            mode=0,
            duration=0,
        )

    @pytest.mark.asyncio
    async def test_open_leaves_coordinator_data_byte_for_byte_unchanged(self, monkeypatch):
        entity, coordinator, _ = _build_anchor_valve()
        monkeypatch.setattr(generic_control_module, "async_call_later", MagicMock(return_value=MagicMock()))
        before = copy.deepcopy(coordinator.data)

        await entity.async_open_valve()

        assert coordinator.data == before

    @pytest.mark.asyncio
    async def test_close_leaves_coordinator_data_byte_for_byte_unchanged(self, monkeypatch):
        entity, coordinator, _ = _build_anchor_valve()
        monkeypatch.setattr(generic_control_module, "async_call_later", MagicMock(return_value=MagicMock()))
        before = copy.deepcopy(coordinator.data)

        await entity.async_close_valve()

        assert coordinator.data == before

    @pytest.mark.asyncio
    async def test_command_never_calls_async_set_updated_data_or_record_valve_command(self, monkeypatch):
        entity, coordinator, _ = _build_anchor_valve()
        monkeypatch.setattr(generic_control_module, "async_call_later", MagicMock(return_value=MagicMock()))
        coordinator.async_set_updated_data = MagicMock()
        coordinator.record_valve_command = MagicMock()

        await entity.async_open_valve()

        coordinator.async_set_updated_data.assert_not_called()
        coordinator.record_valve_command.assert_not_called()

    @pytest.mark.asyncio
    async def test_a_delayed_refresh_is_scheduled_and_not_requested_synchronously(self, monkeypatch):
        entity, coordinator, _ = _build_anchor_valve()
        call_later = MagicMock(return_value=MagicMock())
        monkeypatch.setattr(generic_control_module, "async_call_later", call_later)

        await entity.async_open_valve()

        call_later.assert_called_once_with(entity.hass, GENERIC_CONTROL_REFRESH_DELAY_SECONDS, entity._handle_refresh)
        coordinator.async_request_refresh.assert_not_called()

    @pytest.mark.asyncio
    async def test_scheduled_refresh_callback_requests_the_coordinator_refresh(self, monkeypatch):
        entity, coordinator, _ = _build_anchor_valve()
        monkeypatch.setattr(generic_control_module, "async_call_later", MagicMock(return_value=MagicMock()))

        await entity.async_open_valve()
        await entity._handle_refresh(None)

        coordinator.async_request_refresh.assert_awaited_once()
        assert entity._refresh_cancel is None

    @pytest.mark.asyncio
    async def test_reported_state_unchanged_immediately_after_a_command(self, monkeypatch):
        entity, _, _ = _build_anchor_valve(fields=[_run_state_field(1, 1)])
        monkeypatch.setattr(generic_control_module, "async_call_later", MagicMock(return_value=MagicMock()))
        before = entity.is_closed

        await entity.async_open_valve()

        assert entity.is_closed == before

    @pytest.mark.asyncio
    async def test_second_command_cancels_the_first_pending_refresh(self, monkeypatch):
        entity, _, _ = _build_anchor_valve()
        first_cancel = MagicMock()
        second_cancel = MagicMock()
        call_later = MagicMock(side_effect=[first_cancel, second_cancel])
        monkeypatch.setattr(generic_control_module, "async_call_later", call_later)

        await entity.async_open_valve()
        await entity.async_close_valve()

        first_cancel.assert_called_once()
        second_cancel.assert_not_called()
        assert entity._refresh_cancel is second_cancel

    @pytest.mark.asyncio
    async def test_async_will_remove_from_hass_cancels_a_pending_refresh(self, monkeypatch):
        entity, _, _ = _build_anchor_valve()
        cancel = MagicMock()
        monkeypatch.setattr(generic_control_module, "async_call_later", MagicMock(return_value=cancel))

        await entity.async_open_valve()
        await entity.async_will_remove_from_hass()

        cancel.assert_called_once()
        assert entity._refresh_cancel is None

    @pytest.mark.asyncio
    async def test_async_will_remove_from_hass_is_a_no_op_with_no_pending_refresh(self):
        entity, _, _ = _build_anchor_valve()

        await entity.async_will_remove_from_hass()  # must not raise

        assert entity._refresh_cancel is None


# ---------------------------------------------------------------------------
# End-to-end: valve.async_setup_entry dispatch with the control toggle
# ---------------------------------------------------------------------------


class TestEndToEndValveSetupEntry:
    @pytest.mark.asyncio
    async def test_option_absent_creates_no_control_entity(self):
        from custom_components.rainpoint.valve import async_setup_entry

        sensor_key = "100_200_1"
        sensor_info = _anchor_sensor_info()
        coordinator = _make_coordinator(sensor_key, sensor_info)
        hass, entry = _make_hass_and_entry(coordinator, {})

        captured = []
        async_add_entities = MagicMock(side_effect=lambda ents, **kw: captured.extend(ents))
        await async_setup_entry(hass, entry, async_add_entities)

        assert captured == []

    @pytest.mark.asyncio
    async def test_option_false_creates_no_control_entity(self):
        from custom_components.rainpoint.valve import async_setup_entry

        sensor_key = "100_200_1"
        sensor_info = _anchor_sensor_info()
        coordinator = _make_coordinator(sensor_key, sensor_info)
        hass, entry = _make_hass_and_entry(coordinator, {CONF_GENERIC_CONTROL_ENABLED: False})

        captured = []
        async_add_entities = MagicMock(side_effect=lambda ents, **kw: captured.extend(ents))
        await async_setup_entry(hass, entry, async_add_entities)

        assert captured == []

    @pytest.mark.asyncio
    async def test_option_true_creates_exactly_one_generic_valve_for_the_anchor_variant(self):
        from custom_components.rainpoint.valve import async_setup_entry

        sensor_key = "100_200_1"
        sensor_info = _anchor_sensor_info()
        coordinator = _make_coordinator(sensor_key, sensor_info)
        hass, entry = _make_hass_and_entry(coordinator, {CONF_GENERIC_CONTROL_ENABLED: True})

        captured = []
        async_add_entities = MagicMock(side_effect=lambda ents, **kw: captured.extend(ents))
        await async_setup_entry(hass, entry, async_add_entities)

        assert len(captured) == 1
        assert isinstance(captured[0], RainPointGenericValve)
        assert captured[0]._attr_unique_id == "rainpoint_100_200_1_generic_ctl_ctl_water_p1"

    @pytest.mark.asyncio
    async def test_hand_written_model_creates_no_control_entity_even_with_option_on(self):
        from custom_components.rainpoint.valve import async_setup_entry

        sensor_key = "100_200_1"
        sensor_info = _anchor_sensor_info(model=MODEL_VALVE_245)
        coordinator = _make_coordinator(sensor_key, sensor_info)
        hass, entry = _make_hass_and_entry(coordinator, {CONF_GENERIC_CONTROL_ENABLED: True})

        captured = []
        async_add_entities = MagicMock(side_effect=lambda ents, **kw: captured.extend(ents))
        await async_setup_entry(hass, entry, async_add_entities)

        assert all(GENERIC_CONTROL_UNIQUE_ID_MARKER not in getattr(e, "_attr_unique_id", "") for e in captured)
