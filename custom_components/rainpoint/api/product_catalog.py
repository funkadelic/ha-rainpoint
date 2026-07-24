"""Import-time, failure-tolerant loader for the committed RainPoint product catalog.

The catalog is a trimmed snapshot of the vendor's product-model metadata,
shipped inside the package and never fetched from the vendor at runtime. It is
loaded once, in this module, during component import - which Home Assistant
already runs in the executor thread, so this read never blocks the event
loop. A missing, corrupt, oversized, or wrong-shape catalog file degrades to
an empty catalog: every lookup then returns None and callers fall back to
today's unenriched behavior. Nothing in this module ever raises.

On-disk shape is model -> modelCode -> dp list::

    {"HIC801W": {"278": [...], "279": [...]}}

A model string is not a unique key: the vendor catalog maps some models to
more than one modelCode, and those variants can differ in port count. Keying
on the model alone would let one variant's zone metadata be attached to the
other's payload. Entries whose modelCode the vendor did not supply live under
the "*" bucket and act as the model-level default.
"""

import json
import logging
from pathlib import Path

_LOGGER = logging.getLogger(__name__)

_CATALOG_PATH = Path(__file__).parent / "data" / "product_catalog.json"

# Bucket key for catalog entries the vendor supplied without a modelCode.
UNCODED_VARIANT = "*"

# Upper bound on the committed catalog file size. The real trimmed snapshot is
# expected to be well under a megabyte; anything far larger is treated as
# corrupt (or hostile) and is rejected before its contents are read.
_CATALOG_MAX_BYTES = 5 * 1024 * 1024


def _normalize_model_variants(value: object) -> dict | None:
    """Return value as a modelCode -> dp-list mapping, or None if unusable.

    Accepts the current nested shape and, for tolerance against a catalog file
    written before the modelCode split, a bare dp list - which is treated as
    the model-level uncoded bucket. Any other shape is rejected so a malformed
    entry degrades to "no catalog data for this model" rather than raising.
    """
    if isinstance(value, list):
        return {UNCODED_VARIANT: value}
    if isinstance(value, dict):
        variants = {str(code): dp_list for code, dp_list in value.items() if isinstance(dp_list, list)}
        return variants or None
    return None


def _load_catalog(path: Path) -> dict:
    """Load the catalog JSON at path, degrading to {} on any failure.

    Rejects the file outright, without reading its contents, when it exceeds
    _CATALOG_MAX_BYTES. Rejects any parsed value that is not a JSON object.
    Never raises: every failure mode (missing file, unreadable file, invalid
    JSON, oversized file, wrong top-level shape) returns an empty dict.

    json.JSONDecodeError is a ValueError subclass, so the ValueError arm
    below covers malformed JSON as well as a non-str/bytes payload.
    """
    try:
        if path.stat().st_size > _CATALOG_MAX_BYTES:
            _LOGGER.debug("product_catalog.json exceeds size cap, skipping load: %s", path)
            return {}
        with path.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError) as exc:
        _LOGGER.debug("product_catalog.json missing or invalid, degrading to empty catalog: %s", exc)
        return {}

    if not isinstance(data, dict):
        _LOGGER.debug("product_catalog.json is not a JSON object, degrading to empty catalog")
        return {}

    catalog = {}
    for model, value in data.items():
        variants = _normalize_model_variants(value)
        if variants is None:
            _LOGGER.debug("product_catalog.json entry for %s has an unusable shape, skipping it", model)
            continue
        catalog[model] = variants
    return catalog


_CATALOG: dict = _load_catalog(_CATALOG_PATH)


def get_catalog_entry(model: str | None, model_code: int | str | None = None) -> list | None:
    """Return the catalog's trimmed dp list for a model variant, or None on a miss.

    Resolution order, given a model_code:

    1. the variant recorded under that exact code
    2. the model-level uncoded bucket

    and, without a model_code:

    1. the only variant, when the model has exactly one
    2. the model-level uncoded bucket

    A known model_code that the catalog does not list never falls through to a
    different code's metadata, and an ambiguous model (several coded variants,
    no code supplied by the device) resolves to None rather than a guess. Both
    cases mean the caller keeps its unenriched behavior, which is the point:
    wrong zone metadata is worse than none.
    """
    if model is None:
        return None
    variants = _CATALOG.get(model)
    if not variants:
        return None
    if model_code is not None:
        entry = variants.get(str(model_code))
        if entry is not None:
            return entry
        return variants.get(UNCODED_VARIANT)
    if len(variants) == 1:
        return next(iter(variants.values()))
    return variants.get(UNCODED_VARIANT)
