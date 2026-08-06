"""Tests for the shared trust-boundary predicates."""

import custom_components.rainpoint.api.product_catalog as product_catalog_module
from custom_components.rainpoint.api import decode_generic, has_bluetooth_control_identity, is_hand_written_model
from custom_components.rainpoint.api.trust import BLUETOOTH_CONTROL_IDENTITY
from custom_components.rainpoint.const import (
    HAND_WRITTEN_MODELS,
    MODEL_DISPLAY_HUB,
    VALVE_MODELS,
)
from custom_components.rainpoint.coordinator import DECODER_REGISTRY


class TestIsHandWrittenModel:
    """is_hand_written_model must recognize every trusted model and no others."""

    def test_true_for_every_decoder_registry_key(self):
        """Every registered decoder marks its model as hand-written."""
        for model in DECODER_REGISTRY:
            assert is_hand_written_model(model) is True

    def test_true_for_display_hub(self):
        """The display hub is decoded by hand despite not being in the registry."""
        assert is_hand_written_model(MODEL_DISPLAY_HUB) is True

    def test_true_for_every_valve_model(self):
        """No valve model may ever reach the generic decode path."""
        for model in VALVE_MODELS:
            assert is_hand_written_model(model) is True

    def test_false_for_unsupported_model(self):
        """An unsupported model is exactly what the generic path is for."""
        assert is_hand_written_model("SOME_UNSUPPORTED_MODEL") is False

    def test_false_for_none(self):
        """A missing model string is not trusted by default."""
        assert is_hand_written_model(None) is False


class TestHandWrittenModelsDriftGuard:
    """HAND_WRITTEN_MODELS must stay in lockstep with DECODER_REGISTRY."""

    def test_matches_decoder_registry_plus_display_hub(self):
        """Adding a decoder without updating the constant would open the boundary."""
        assert set(DECODER_REGISTRY) | {MODEL_DISPLAY_HUB} == HAND_WRITTEN_MODELS


class TestDecodeGenericSkipsHandWrittenModels:
    """decode_generic must never attach a catalog annotation for a trusted model."""

    def test_hand_written_model_never_annotated_even_with_catalog_entry(self, monkeypatch):
        """Defense in depth: a hand-written model gets no annotation, even if it
        happened to have a matching catalog entry."""
        import custom_components.rainpoint.api.generic_decoder as generic_decoder_module

        fake_catalog = [{"dpCode": 31, "identity": "STA_BAT", "dpPort": 1, "dpDataType": "uint8", "portNumber": 1}]
        # The stub must mirror get_catalog_entry's real (model, model_code)
        # signature. A single-argument stub raises TypeError inside the
        # annotation step, which decode_generic swallows by design, so the
        # assertion below would hold even with the trust guard removed.
        monkeypatch.setattr(generic_decoder_module, "get_catalog_entry", lambda model, model_code=None: fake_catalog)

        hand_written_model = next(iter(DECODER_REGISTRY))
        result = decode_generic("10#DC", model=hand_written_model)

        for field in result["fields"]:
            assert "catalog" not in field


class TestHasBluetoothControlIdentity:
    """has_bluetooth_control_identity must recognize every committed
    CTL_BT_WATER variant and no others, mirroring TestIsHandWrittenModel's
    per-branch shape."""

    def test_true_for_htv210b_with_its_real_model_code(self):
        """The committed variant, resolved the way a real poll resolves it."""
        assert has_bluetooth_control_identity("HTV210B", 41) is True

    def test_true_for_htv210b_with_no_model_code(self):
        """A single-variant model still resolves without a model_code."""
        assert has_bluetooth_control_identity("HTV210B", None) is True

    def test_false_for_none_model(self):
        """Routing to the RF endpoint is the conservative default with no model."""
        assert has_bluetooth_control_identity(None) is False

    def test_false_for_a_model_absent_from_the_catalog(self):
        assert has_bluetooth_control_identity("SOME_UNSUPPORTED_MODEL") is False

    def test_false_for_a_catalog_model_with_no_ctl_bt_water_entry(self):
        """HTV245FRF's single variant declares CTL_WATER, never CTL_BT_WATER."""
        assert has_bluetooth_control_identity("HTV245FRF", None) is False

    def test_false_for_an_ambiguous_pair_the_catalog_cannot_resolve(self, monkeypatch):
        """A synthetic two-variant model with no matching code and no uncoded
        bucket resolves to None, following TestEvaluateControlGateSynthetic's
        precedent (tests/test_generic_control.py) for shapes no committed
        variant exhibits: an unresolvable pair must route to the RF path
        rather than guess."""
        catalog = {
            "SYNTH_AMBIGUOUS": {
                "1": {"portNumber": 1, "dp": [{"dpCode": 1, "identity": "CTL_BT_WATER", "dpPort": 1}]},
                "2": {"portNumber": 2, "dp": [{"dpCode": 1, "identity": "CTL_BT_WATER", "dpPort": 1}]},
            }
        }
        monkeypatch.setattr(product_catalog_module, "_CATALOG", catalog)

        assert has_bluetooth_control_identity("SYNTH_AMBIGUOUS", None) is False

    def test_true_regardless_of_where_the_identity_sits_in_the_dp_list(self, monkeypatch):
        """The walk does not depend on entry order, and a non-dict entry in
        the list is skipped by the isinstance guard rather than raising."""
        catalog = {
            "SYNTH_TRAILING": {
                "1": {
                    "portNumber": 1,
                    "dp": [
                        {"dpCode": 30, "identity": "STA_WKSTATE", "dpPort": 1},
                        "not-a-dict-entry",
                        {"dpCode": 1, "identity": "CTL_BT_WATER", "dpPort": 1},
                    ],
                }
            }
        }
        monkeypatch.setattr(product_catalog_module, "_CATALOG", catalog)

        assert has_bluetooth_control_identity("SYNTH_TRAILING", 1) is True


class TestBluetoothControlIdentityRoutingInvariant:
    """The claim this phase's endpoint selection rests on: every CTL_BT_WATER
    variant in the committed catalog routes to the DP endpoint, proven over
    the catalog itself rather than asserted in prose. If a future catalog
    refresh adds a sixth model carrying the identity, this test covers it by
    construction and only the count assertion below needs updating.
    """

    def test_every_committed_bluetooth_variant_routes_to_the_dp_endpoint(self):
        bt_variants = [
            (model, code)
            for model, variants in product_catalog_module._CATALOG.items()
            for code, record in variants.items()
            if any(isinstance(e, dict) and e.get("identity") == BLUETOOTH_CONTROL_IDENTITY for e in record["dp"])
        ]

        assert bt_variants, "catalog no longer carries a CTL_BT_WATER model; this guard needs rewriting"
        # Five models today: HTV102B, HTV107B, HTV124LT, HTV210B, HTV224B. A
        # sixth model showing up here is a real event worth noticing in the
        # diff, not a failure to fix blindly -- update this count once the
        # new model is confirmed to belong.
        assert len(bt_variants) == 5

        for model, code in bt_variants:
            model_code = None if code == product_catalog_module.UNCODED_VARIANT else int(code)
            assert has_bluetooth_control_identity(model, model_code) is True

            record = product_catalog_module._CATALOG[model][code]
            bt_entries = [e for e in record["dp"] if isinstance(e, dict) and e.get("identity") == BLUETOOTH_CONTROL_IDENTITY]
            for entry in bt_entries:
                # Pins the literal the client sends: a future catalog refresh
                # declaring the identity at a different code must fail this
                # test instead of the hardware silently ignoring the command.
                assert entry.get("dpCode") == 1
