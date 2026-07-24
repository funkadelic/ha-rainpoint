"""Import-time, failure-tolerant loader for the committed RainPoint product catalog.

The catalog is a trimmed snapshot of the vendor's product-model metadata,
shipped inside the package and never fetched from the vendor at runtime. It is
loaded once, in this module, during component import - which Home Assistant
already runs in the executor thread, so this read never blocks the event
loop. A missing, corrupt, oversized, or wrong-shape catalog file degrades to
an empty catalog: every lookup then returns None and callers fall back to
today's unenriched behavior. Nothing in this module ever raises.
"""

import json
import logging
from pathlib import Path

_LOGGER = logging.getLogger(__name__)

_CATALOG_PATH = Path(__file__).parent / "data" / "product_catalog.json"

# Upper bound on the committed catalog file size. The real trimmed snapshot is
# expected to be well under a megabyte; anything far larger is treated as
# corrupt (or hostile) and is rejected before its contents are read.
_CATALOG_MAX_BYTES = 5 * 1024 * 1024


def _load_catalog(path: Path) -> dict:
    """Load the catalog JSON at path, degrading to {} on any failure.

    Rejects the file outright, without reading its contents, when it exceeds
    _CATALOG_MAX_BYTES. Rejects any parsed value that is not a JSON object.
    Never raises: every failure mode (missing file, unreadable file, invalid
    JSON, oversized file, wrong top-level shape) returns an empty dict.
    """
    try:
        if path.stat().st_size > _CATALOG_MAX_BYTES:
            _LOGGER.debug("product_catalog.json exceeds size cap, skipping load: %s", path)
            return {}
        with path.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        _LOGGER.debug("product_catalog.json missing or invalid, degrading to empty catalog: %s", exc)
        return {}

    if not isinstance(data, dict):
        _LOGGER.debug("product_catalog.json is not a JSON object, degrading to empty catalog")
        return {}
    return data


_CATALOG: dict = _load_catalog(_CATALOG_PATH)


def get_catalog_entry(model: str | None) -> dict | None:
    """Return the catalog's trimmed dp list for model, or None on any miss.

    Returns None when model is None, when model has no catalog entry, or
    (via the fail-soft load above) when the catalog itself failed to load.
    """
    if model is None:
        return None
    return _CATALOG.get(model)
