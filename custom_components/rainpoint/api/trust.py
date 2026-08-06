"""Shared trust-boundary predicates for the model-agnostic decode path and
for write-endpoint routing.

Every hand-written, fixture-validated decoder is the trusted path; the
model-agnostic generic decoder is the lower-trust fallback for unsupported
devices. ``is_hand_written_model`` is the single authoritative check that
keeps any model with a hand-written decoder out of the generic path, so a
trusted model can never be shadowed or mixed with catalog-driven output.

``has_bluetooth_control_identity`` answers a different question over the same
committed catalog: which of two write endpoints a valve zone commands
through. It never inspects a model string or model list -- only the
catalog's own datapoint identity -- so a model whose catalog variant declares
the Bluetooth-backed control identity routes to the datapoint endpoint by
construction, with no per-model code change.
"""

from ..const import HAND_WRITTEN_MODELS
from .product_catalog import get_catalog_entry

# The catalog datapoint identity a Bluetooth-backed valve declares in place of
# the RF CTL_WATER identity. A variant carrying this identity commands through
# controlWorkModeDP rather than controlWorkMode.
BLUETOOTH_CONTROL_IDENTITY = "CTL_BT_WATER"


def is_hand_written_model(model: str | None) -> bool:
    """Return True when model already has a hand-written, trusted decoder."""
    return model in HAND_WRITTEN_MODELS


def has_bluetooth_control_identity(model: str | None, model_code: int | str | None = None) -> bool:
    """Return True when the resolved catalog variant declares BLUETOOTH_CONTROL_IDENTITY.

    A None model, a model absent from the catalog, or a (model, model_code)
    pair the catalog cannot resolve to one variant all return False: routing
    to the RF endpoint is the conservative default when the catalog cannot
    say. This is the only place a call site should ask this question -- never
    an ad-hoc comparison against a model string or list.
    """
    dp_entries = get_catalog_entry(model, model_code)
    if not dp_entries:
        return False
    return any(isinstance(entry, dict) and entry.get("identity") == BLUETOOTH_CONTROL_IDENTITY for entry in dp_entries)
