"""D-02 evidence sweep for the TLV (11#) catalog annotation key selection.

Measurement only, not production change: nothing here touches
``custom_components/``. It exists to widen the evidence behind the finding
in ``.planning/todos/pending/2026-07-24-tlv-catalog-annotation-keys-on-the-wrong-field.md``
before the annotation key is changed - a decoded field's structural
``index`` lines up with the catalog's ``dpCode``, while its per-entry
``dp_id`` (the vendor's per-instance ordering handle, only present on ``11#``
TLV framing) does not - and to prove, against the trusted hand-written valve
decoder as ground truth, that ascending ``dp_id`` order maps to ascending
``dpPort`` order for the two identities the fix's multi-zone disambiguation
depends on (``STA_WKSTATE`` and ``STA_DURATION``).

Both counting passes below measure the two candidate numbering spaces
structurally - comparing ``index`` and ``dp_id`` directly against the
catalog's ``dpCode`` - independently of whatever ``_match_catalog_dp``
currently does, so a change to that function cannot make this sweep agree
with itself by construction.

A full-catalog cross-product (every TLV sample against every one of the
~90 models' catalog variants, not just each sample's own model) does turn
up a handful of dp_id/dpCode coincidences - low integers are common on both
sides of a catalog with ~90 models, so a per-instance dp_id occasionally
lands on some unrelated model's unrelated dpCode by chance (observed: an
HTV405FRF ``STA_ALARM`` field's dp_id 30/31/32 happens to equal that same
model's own ``STA_WKSTATE``/``STA_BAT``/``STA_RSSI`` dpCodes). That is
noise from an unrelated identity, not a genuine numbering-space alignment -
it never touches ``STA_WKSTATE`` or ``STA_DURATION``, the identities this
fix's port-disambiguation depends on, and if anything it strengthens the
case against dp_id-keying: the old key does not just fail silently, it can
occasionally produce a plausible-looking wrong answer. The sweep below
therefore keeps the full-catalog cross-product for the "index dominates"
and "more than one model" claims, and scopes the "dp_id doesn't work"
claim to the three identities the folded todo's original single-payload
measurement covered (``STA_BAT``, ``STA_WKSTATE``, ``STA_DURATION``),
evaluated against each sample's own catalog variant - which is exactly the
todo's reported zero, reproduced across every TLV sample now available
rather than the one payload it was taken from.
"""

import pytest

from custom_components.rainpoint.api import product_catalog as product_catalog_module
from custom_components.rainpoint.api.decoders import decode_htv213frf_valve
from custom_components.rainpoint.api.generic_decoder import decode_generic
from custom_components.rainpoint.api.product_catalog import get_catalog_entry, get_catalog_variant_codes
from custom_components.rainpoint.generic_control import CONTROL_IDENTITY_ALLOWLIST, RUN_STATE_IDENTITY
from tests.payload_samples import (
    SAMPLE_HTV245_TLV_PAYLOAD,
    SAMPLE_HTV405_TLV_PAYLOAD,
    VALVE_HUB_APPLY_TLV_PAYLOAD,
    VALVE_HUB_TLV_PAYLOAD,
)

# Every 11# TLV sample constant available in the shared payload fixtures.
TLV_SAMPLES: dict[str, str] = {
    "SAMPLE_HTV245_TLV_PAYLOAD": SAMPLE_HTV245_TLV_PAYLOAD,
    "SAMPLE_HTV405_TLV_PAYLOAD": SAMPLE_HTV405_TLV_PAYLOAD,
    "VALVE_HUB_TLV_PAYLOAD": VALVE_HUB_TLV_PAYLOAD,
    "VALVE_HUB_APPLY_TLV_PAYLOAD": VALVE_HUB_APPLY_TLV_PAYLOAD,
}

# The catalog model each sample's fields were captured from, or synthesized
# against - exactly the model payload_samples.py's own docstring names for
# each constant. Used to scope the own-model checks and to source the
# ground-truth zone assignment; not a test-only invention.
SAMPLE_MODEL: dict[str, str] = {
    "SAMPLE_HTV245_TLV_PAYLOAD": "HTV245FRF",
    "SAMPLE_HTV405_TLV_PAYLOAD": "HTV405FRF",
    "VALVE_HUB_TLV_PAYLOAD": "HTV0540FRF",
    "VALVE_HUB_APPLY_TLV_PAYLOAD": "HTV0540FRF",
}

# The three identities the folded todo's original single-payload measurement
# covered (STA_BAT, STA_WKSTATE, STA_DURATION).
_PAIRING_IDENTITIES = {"STA_BAT", "STA_WKSTATE", "STA_DURATION"}


def _dp_codes(dp_list: list) -> set:
    """Return the set of dpCode values a catalog dp list declares."""
    return {dp.get("dpCode") for dp in dp_list if isinstance(dp, dict)}


def _hit_count(fields: list[dict], dp_codes: set, key: str) -> int:
    """Count decoded fields whose `key` value is one of dp_codes."""
    return sum(1 for f in fields if f[key] in dp_codes)


def _full_catalog_sweep():
    """Yield (sample_name, model, model_code, fields, dp_codes) for every sample x every catalog variant.

    Decodes each sample once with no model (so no annotation runs) and
    reuses that field list across every catalog variant, per the D-02
    requirement to sweep "every available TLV sample crossed with every
    catalog variant". The model universe is read from the loaded catalog
    module (there is no public accessor that lists every model - the
    catalog is keyed by model, not the other way around), matching the
    precedent already established in
    tests/test_generic_entities.py::test_real_committed_catalog_never_raises_and_never_disagrees_with_itself.
    Variant codes and dp lists come from the public accessors, sorted, so
    no JSON is parsed here.
    """
    for sample_name, payload in TLV_SAMPLES.items():
        fields = decode_generic(payload)["fields"]
        for model in sorted(product_catalog_module._CATALOG):
            for model_code in get_catalog_variant_codes(model):
                dp_list = get_catalog_entry(model, model_code) or []
                yield sample_name, model, model_code, fields, _dp_codes(dp_list)


def _pairing(fields: list[dict], dp_list: list, index: int) -> list[tuple[dict, dict]]:
    """Pair decoded fields at `index` to catalog entries at dpCode == index.

    Ascending dp_id on the field side, ascending dpPort on the catalog side -
    the exact pairing task 2 implements in production. strict=True so a
    group whose field count disagrees with its catalog candidate count
    raises immediately instead of silently zipping a truncated pairing.
    """
    group = sorted((f for f in fields if f["index"] == index), key=lambda f: f["dp_id"])
    candidates = sorted((dp for dp in dp_list if dp.get("dpCode") == index), key=lambda dp: dp.get("dpPort"))
    return list(zip(group, candidates, strict=True))


class TestIndexVsDpIdAlignmentSweep:
    """D-02's widened evidence: index vs dp_id, every TLV sample x every catalog variant."""

    def test_index_key_strictly_dominates_dp_id_key(self):
        """Summed over every (sample, variant) pair, index produces far more hits than dp_id."""
        total_index_hits = 0
        total_dp_id_hits = 0
        for _sample_name, _model, _model_code, fields, dp_codes in _full_catalog_sweep():
            total_index_hits += _hit_count(fields, dp_codes, "index")
            total_dp_id_hits += _hit_count(fields, dp_codes, "dp_id")

        assert total_index_hits > total_dp_id_hits

    def test_dp_id_key_produces_zero_hits_for_the_pairing_identities_within_their_own_model(self):
        """Reproduces the todo's exact single-payload finding, widened to every TLV sample.

        Scoped to each sample's own catalog variant and to the three
        identities the todo's table covered: dp_id never matches that
        variant's dpCode for any of them, on any of the four samples. See
        the module docstring for why a full-catalog cross-product is not
        used for this claim.
        """
        hits = 0
        for sample_name, payload in TLV_SAMPLES.items():
            model = SAMPLE_MODEL[sample_name]
            fields = [f for f in decode_generic(payload)["fields"] if f["name"] in _PAIRING_IDENTITIES]
            for model_code in get_catalog_variant_codes(model):
                dp_codes = _dp_codes(get_catalog_entry(model, model_code) or [])
                hits += _hit_count(fields, dp_codes, "dp_id")

        assert hits == 0

    def test_index_key_hits_span_more_than_one_model(self):
        """At least two distinct (sample, variant) pairs match on index, from more than one model.

        The finding must rest on more than one model, not the same model
        matched twice by two different samples.
        """
        nonzero_pairs = set()
        for sample_name, model, model_code, fields, dp_codes in _full_catalog_sweep():
            if _hit_count(fields, dp_codes, "index") > 0:
                nonzero_pairs.add((sample_name, model, model_code))

        assert len(nonzero_pairs) >= 2
        assert len({model for _sample, model, _code in nonzero_pairs}) >= 2


class TestGroundTruthZoneOrdering:
    """D-01's core validation against the trusted hand-written valve decoder.

    Ascending dp_id paired to ascending dpPort must reproduce the trusted
    decoder's exact per-zone assignment. Getting the direction backwards
    would silently swap zone 1 and zone 2, so every assertion below checks
    exact per-zone values rather than merely that the sets match.
    """

    @pytest.mark.parametrize(
        "payload,model",
        [
            (SAMPLE_HTV245_TLV_PAYLOAD, "HTV245FRF"),
            (SAMPLE_HTV405_TLV_PAYLOAD, "HTV405FRF"),
        ],
    )
    def test_wkstate_pairing_reproduces_trusted_per_zone_open_state(self, payload, model):
        fields = decode_generic(payload)["fields"]
        dp_list = get_catalog_entry(model, get_catalog_variant_codes(model)[0])
        pairs = _pairing(fields, dp_list, 30)  # STA_WKSTATE
        assert pairs  # a group must exist, or this test proves nothing

        trusted_zones = decode_htv213frf_valve(payload)["zones"]
        for field, dp_entry in pairs:
            zone = dp_entry["dpPort"]
            expected_open = trusted_zones[zone]["open"]
            actual_open = bool(field["value"] & 0x01)
            assert actual_open == expected_open

    @pytest.mark.parametrize(
        "payload,model",
        [
            (SAMPLE_HTV245_TLV_PAYLOAD, "HTV245FRF"),
            (SAMPLE_HTV405_TLV_PAYLOAD, "HTV405FRF"),
        ],
    )
    def test_duration_pairing_reproduces_trusted_per_zone_duration(self, payload, model):
        fields = decode_generic(payload)["fields"]
        dp_list = get_catalog_entry(model, get_catalog_variant_codes(model)[0])
        pairs = _pairing(fields, dp_list, 19)  # STA_DURATION
        assert pairs

        trusted_zones = decode_htv213frf_valve(payload)["zones"]
        for field, dp_entry in pairs:
            zone = dp_entry["dpPort"]
            assert field["value"] == trusted_zones[zone]["duration_seconds"]


class TestControlEligibleVariantRunStatePortOrdering:
    """Lock in that every generic-control-allowlisted variant's run-state
    dpPort groups are dense and ascending, the structural precondition
    position-based dp_id pairing depends on.

    Real per-payload dp_id evidence exists only for the models with a
    captured TLV sample (see TestGroundTruthZoneOrdering above); this sweep
    checks a necessary, catalog-only precondition across every variant
    generic_control.evaluate_control_gate could ever admit. If a future
    catalog update ever declared a control-eligible variant's run-state ports
    out of the expected 1..N contiguous ascending shape, position-based
    pairing could not reproduce the vendor's real per-instance ordering,
    whatever it turns out to be - locking this in now means such a catalog
    change fails loudly here rather than silently mis-displaying a zone's
    state.
    """

    def test_every_control_eligible_variants_run_state_ports_are_contiguous_ascending(self):
        checked_groups = 0
        for model in sorted(product_catalog_module._CATALOG):
            for model_code in get_catalog_variant_codes(model):
                dp_list = get_catalog_entry(model, model_code) or []
                if not any(isinstance(dp, dict) and dp.get("identity") in CONTROL_IDENTITY_ALLOWLIST for dp in dp_list):
                    continue
                run_state = [dp for dp in dp_list if isinstance(dp, dict) and dp.get("identity") == RUN_STATE_IDENTITY]
                by_dp_code: dict = {}
                for dp in run_state:
                    by_dp_code.setdefault(dp.get("dpCode"), []).append(dp.get("dpPort"))
                for dp_code, ports in by_dp_code.items():
                    if len(ports) <= 1:
                        continue
                    checked_groups += 1
                    assert sorted(ports) == list(range(1, len(ports) + 1)), (
                        f"{model}/{model_code} dpCode {dp_code} run-state ports {ports} are not a contiguous ascending 1..N run"
                    )

        # A regression-proof floor: fails loudly if the catalog sweep above
        # stops finding any multi-port control-eligible variant at all (e.g.
        # a broken import), rather than passing vacuously on zero groups.
        assert checked_groups > 0
