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
from .product_catalog import get_catalog_entry, get_catalog_variant_codes

# The catalog datapoint identity a Bluetooth-backed valve declares in place of
# the RF CTL_WATER identity. A variant carrying this identity commands through
# controlWorkModeDP rather than controlWorkMode.
BLUETOOTH_CONTROL_IDENTITY = "CTL_BT_WATER"


def is_hand_written_model(model: str | None) -> bool:
    """Return True when model already has a hand-written, trusted decoder."""
    return model in HAND_WRITTEN_MODELS


def _declares_bluetooth_control(dp_entries) -> bool:
    """Return True when a variant's dp list carries BLUETOOTH_CONTROL_IDENTITY."""
    return any(isinstance(entry, dict) and entry.get("identity") == BLUETOOTH_CONTROL_IDENTITY for entry in dp_entries)


def has_bluetooth_control_identity(model: str | None, model_code: int | str | None = None) -> bool:
    """Return True when model's catalog variant declares BLUETOOTH_CONTROL_IDENTITY.

    A resolved variant answers for itself, including a variant that declares
    no datapoints at all: the catalog saying nothing about control is a
    negative, not an absence of an answer.

    An unresolved (model, model_code) pair is the one that used to be
    collapsed into the RF negative, and it is the failure this reads for: a
    catalog refresh that gives an already-supported Bluetooth valve a new
    model code would drop it onto the RF endpoint, which rejects that model
    outright, so the zone would get a valve entity that errors on every
    command. The fallback asks the model's other variants instead, and routes
    to the datapoint endpoint only when every one of them declares the
    identity. No model in the committed catalog mixes the two endpoints
    across its variants, so unanimity is the model's answer rather than a
    guess at the variant's.

    A model absent from the catalog still returns False, which is what keeps
    a degraded snapshot (product_catalog loads to an empty catalog rather
    than raising) from withdrawing the RF routing every install depends on.
    This is the only place a call site should ask this question -- never an
    ad-hoc comparison against a model string or list.
    """
    dp_entries = get_catalog_entry(model, model_code)
    if dp_entries is not None:
        return _declares_bluetooth_control(dp_entries)

    codes = get_catalog_variant_codes(model)
    return bool(codes) and all(_declares_bluetooth_control(get_catalog_entry(model, code) or []) for code in codes)
