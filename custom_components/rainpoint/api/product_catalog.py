"""Import-time, failure-tolerant loader for the committed RainPoint product catalog.

The catalog is a trimmed snapshot of RainPoint's product-model metadata,
shipped inside the package and never fetched from RainPoint at runtime. It is
loaded once, in this module, during component import - which Home Assistant
already runs in the executor thread, so this read never blocks the event
loop. A missing, corrupt, oversized, or wrong-shape catalog file degrades to
an empty catalog: every lookup then returns None and callers fall back to
today's unenriched behavior. Nothing in this module ever raises.

On-disk shape is model -> modelCode -> variant record::

    {"HIC801W": {"278": {"portNumber": 0, "dp": [...]},
                 "279": {"portNumber": 8, "dp": [...]}}}

A model string is not a unique key: the RainPoint catalog maps some models to
more than one modelCode, and those variants can differ in port count. Keying
on the model alone would let one variant's zone metadata be attached to the
other's payload. HIC801W above is a real example - the same model name is 0
ports under code 278 and 8 ports under 279. Entries whose modelCode RainPoint
did not supply live under the "*" bucket and act as the model-level default.

portNumber is a per-model property in the RainPoint catalog, not a per-dp one, so
it lives on the variant record rather than being repeated on every dp entry.
"""

import hashlib
import json
import logging
from pathlib import Path

_LOGGER = logging.getLogger(__name__)

_CATALOG_PATH = Path(__file__).parent / "data" / "product_catalog.json"

# Bucket key for catalog entries RainPoint supplied without a modelCode.
UNCODED_VARIANT = "*"

# Upper bound on the committed catalog file size. The real trimmed snapshot is
# expected to be well under a megabyte; anything far larger is treated as
# corrupt (or hostile) and is rejected before its contents are read.
_CATALOG_MAX_BYTES = 5 * 1024 * 1024


def _normalize_variant_record(value: object) -> dict | None:
    """Return value as a {"portNumber": ..., "dp": [...]} record, or None if unusable.

    Accepts the current record shape and, for tolerance against a catalog file
    written before portNumber was hoisted off the dp entries, a bare dp list -
    which becomes a record with an unknown port count. Anything else is
    rejected, so one malformed variant degrades to "no catalog data" instead of
    raising during import.
    """
    if isinstance(value, list):
        return {"portNumber": None, "dp": value}
    if isinstance(value, dict):
        dp_list = value.get("dp")
        if not isinstance(dp_list, list):
            return None
        port_number = value.get("portNumber")
        if not isinstance(port_number, int) or isinstance(port_number, bool):
            port_number = None
        return {"portNumber": port_number, "dp": dp_list}
    return None


def _normalize_model_variants(value: object) -> dict | None:
    """Return value as a modelCode -> variant-record mapping, or None if unusable.

    Accepts the current nested shape and, for tolerance against a catalog file
    written before the modelCode split, a bare dp list - which is treated as
    the model-level uncoded bucket. Any other shape is rejected so a malformed
    entry degrades to "no catalog data for this model" rather than raising.
    """
    if isinstance(value, list):
        return {UNCODED_VARIANT: {"portNumber": None, "dp": value}}
    if isinstance(value, dict):
        variants = {}
        for code, record in value.items():
            normalized = _normalize_variant_record(record)
            if normalized is not None:
                variants[str(code)] = normalized
        return variants or None
    return None


def _load_catalog(path: Path) -> dict:
    """Load the catalog JSON at path, degrading to {} on any failure.

    Reads the file and parses it. Import-time loading goes through
    _read_catalog_bytes and _parse_catalog instead, so the snapshot
    fingerprint can be taken from the same bytes the catalog was parsed from;
    this wrapper is the standalone path-in form.
    """
    return _parse_catalog(_read_catalog_bytes(path))


def _parse_catalog(raw: bytes | None) -> dict:
    """Parse catalog bytes into the model -> modelCode -> record mapping, or {}.

    Rejects any parsed value that is not a JSON object. Never raises: every
    failure mode (no bytes, invalid JSON, wrong top-level shape) returns an
    empty dict.

    json.JSONDecodeError is a ValueError subclass, so the ValueError arm
    below covers malformed JSON as well as a non-str/bytes payload.
    """
    if raw is None:
        return {}
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        _LOGGER.debug("product_catalog.json is invalid, degrading to empty catalog: %s", exc)
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


def _read_catalog_bytes(path: Path) -> bytes | None:
    """Return the catalog file's raw bytes, or None when it cannot be used.

    One read serves both the parse and the fingerprint, so the fingerprint
    provably describes the bytes that produced _CATALOG rather than a second,
    independent read of the same path.

    Never raises. ValueError is caught alongside OSError for the same reason
    _load_catalog catches it: this runs at import time, where anything
    escaping fails the component import outright, and Path.stat can raise
    ValueError on a path with an embedded null.
    """
    try:
        if path.stat().st_size > _CATALOG_MAX_BYTES:
            _LOGGER.debug("product_catalog.json exceeds size cap, skipping load: %s", path)
            return None
        return path.read_bytes()
    except (OSError, ValueError) as exc:
        _LOGGER.debug("product_catalog.json missing or unreadable, degrading to empty catalog: %s", exc)
        return None


def _fingerprint_catalog(raw: bytes | None) -> str | None:
    """Return a short content hash of the catalog bytes, or None when there are none.

    Identifies which snapshot produced a reading, which the integration
    version cannot: the catalog is refreshed by its own script and can change
    in a PR that ships no code, and a hand-edited or partially-refreshed file
    reads as the release it sits in.

    Hashes the raw bytes rather than the parsed catalog, so a file that
    _parse_catalog degraded to {} still fingerprints as itself and a report
    against a corrupt catalog can be told apart from one against no catalog.
    The cost is that a whitespace-only reformat changes the fingerprint for
    identical content, which is the right trade for a label whose job is to
    identify a file.

    Never raises: hashing bytes already in memory has no failure mode this
    module can degrade around, and there is no I/O left to fail.
    """
    if raw is None:
        return None
    return hashlib.sha256(raw).hexdigest()[:12]


def _load_catalog_and_fingerprint(path: Path) -> tuple[dict, str | None]:
    """Return (catalog, fingerprint) from a single read of the file.

    One read serves both, so the fingerprint provably labels the bytes the
    catalog was parsed from rather than a second, independent read of the same
    path. The bytes are local to this call and are released when it returns:
    retaining them at module level would pin the whole file, up to
    _CATALOG_MAX_BYTES, for the process lifetime to produce twelve characters.
    """
    raw = _read_catalog_bytes(path)
    return _parse_catalog(raw), _fingerprint_catalog(raw)


_CATALOG, _CATALOG_FINGERPRINT = _load_catalog_and_fingerprint(_CATALOG_PATH)


def get_catalog_fingerprint() -> str | None:
    """Return the committed catalog snapshot's short content hash, or None."""
    return _CATALOG_FINGERPRINT


def _resolve_variant(model: str | None, model_code: int | str | None = None) -> dict | None:
    """Return the variant record for a model, or None on a miss.

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
        record = variants.get(str(model_code))
        if record is not None:
            return record
        return variants.get(UNCODED_VARIANT)
    if len(variants) == 1:
        return next(iter(variants.values()))
    return variants.get(UNCODED_VARIANT)


def get_catalog_entry(model: str | None, model_code: int | str | None = None) -> list | None:
    """Return the catalog's trimmed dp list for a model variant, or None on a miss.

    See _resolve_variant for how a (model, model_code) pair resolves to one
    variant, and why an ambiguous pair deliberately returns None.
    """
    record = _resolve_variant(model, model_code)
    if record is None:
        return None
    return record["dp"]


def get_catalog_variant_codes(model: str | None) -> tuple[str, ...]:
    """Return the modelCodes the catalog lists for a model, sorted; empty when unknown.

    Lets a caller tell "this model is absent from the catalog" apart from
    "this model is present but the device did not say which variant it is",
    which are different problems with different fixes and would otherwise both
    surface as a plain lookup miss. The uncoded bucket is reported under its
    own sentinel rather than omitted, so a caller never sees an empty tuple
    for a model the catalog does carry.
    """
    if model is None:
        return ()
    return tuple(sorted(_CATALOG.get(model) or {}))


def get_catalog_port_number(model: str | None, model_code: int | str | None = None) -> int | None:
    """Return the declared port (zone) count for a model variant, or None on a miss.

    None means "the catalog does not say", which is distinct from 0 ("the
    RainPoint declares no ports"): several models are 0 ports under one modelCode
    and many under another, so a caller must not read a missing value as zero.
    """
    record = _resolve_variant(model, model_code)
    if record is None:
        return None
    return record["portNumber"]
