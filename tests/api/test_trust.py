"""Tests for the shared hand-written-model trust-boundary predicate."""

from custom_components.rainpoint.api import decode_generic, is_hand_written_model
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
        monkeypatch.setattr(generic_decoder_module, "get_catalog_entry", lambda model: fake_catalog)

        hand_written_model = next(iter(DECODER_REGISTRY))
        result = decode_generic("10#DC", model=hand_written_model)

        for field in result["fields"]:
            assert "catalog" not in field
