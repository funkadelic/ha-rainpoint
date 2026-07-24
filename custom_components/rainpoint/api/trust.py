"""Shared trust-boundary predicate for the model-agnostic decode path.

Every hand-written, fixture-validated decoder is the trusted path; the
model-agnostic generic decoder is the lower-trust fallback for unsupported
devices. ``is_hand_written_model`` is the single authoritative check that
keeps any model with a hand-written decoder out of the generic path, so a
trusted model can never be shadowed or mixed with catalog-driven output.
"""

from ..const import HAND_WRITTEN_MODELS


def is_hand_written_model(model: str | None) -> bool:
    """Return True when model already has a hand-written, trusted decoder."""
    return model in HAND_WRITTEN_MODELS
