"""
Generic, model-agnostic payload decoder for diagnostics.

The per-model decoders in ``decoders.py`` are the trusted path: each is
reverse-engineered against captured hardware payloads and validated by tests.
This module is deliberately the opposite - it decodes the *structure* of a
payload without knowing the model, so an unsupported device still surfaces
named fields instead of an opaque hex string.

It is used only to enrich the unknown-device diagnostic sensor and the
pre-filled bug report. Values here are best-effort and explicitly unverified;
nothing in the entity or control path consumes them.
"""

import logging
import re

from .product_catalog import get_catalog_entry

_LOGGER = logging.getLogger(__name__)


# Field index -> status identity, harvested from the RainPoint/HomGar cloud
# product catalog's dp[].dpCode / dp[].identity pairs. The catalog is served
# by the vendor API at https://region3.homgarus.com/app/common/core/productModel
# and the STA_* names are RainPoint/HomGar's own identities. Only status (STA_*)
# fields that surface in status frames are kept; provisioning dpCodes that
# happen to share an index are ignored.
_STATUS_FIELDS: dict[int, str] = {
    0: "STA_CHG",
    1: "STA_RAIN",
    2: "STA_ALARM",
    4: "STA_OTHER",
    9: "STA_TEM",
    10: "STA_RH",
    13: "STA_TOTAL_RAIN",
    14: "STA_VFLOW",
    15: "STA_LASTUSAGE",
    17: "STA_POWER",
    18: "STA_ENERGY",
    19: "STA_DURATION",
    20: "STA_WATER_TOTAL",
    21: "STA_EVTIME",
    22: "STA_TREND",
    25: "STA_ILLUMINANCE",
    26: "STA_TOTAL_TODAY",
    27: "STA_CO2",
    30: "STA_WKSTATE",
    31: "STA_BAT",
    32: "STA_RSSI",
    33: "STA_WATER_ZONES1",
    34: "STA_WATER_ZONES2",
    35: "STA_WATER_ZONES3",
    36: "STA_WATER_ZONES4",
    37: "STA_WATER_ZONES",
    38: "STA_TS_DET",
    43: "STA_HOUR_RAIN",
    44: "STA_DAY_RAIN",
    45: "STA_7DAY_RAIN",
    46: "STA_CUR_FLOW",
    47: "STA_MAX_CO2",
    49: "STA_LAST_DURATION",
    50: "STA_OTHER_TOTAL",
    51: "STA_RSSI2",
    52: "STA_EVTIME2",
    54: "STA_REPTIME",
    55: "STA_LIGHT_LEVEL",
    58: "STA_ALARM_EX",
    60: "STA_OTHER_TOTAL2",
}

# Duration values are little-endian on the wire; everything else is big-endian.
# This is the same quirk our valve decoders guard against (type byte 0xAD,
# field index 19 = STA_DURATION).
_LITTLE_ENDIAN_FIELDS = frozenset({19})  # STA_DURATION


def _hex_to_bytes(hex_str: str) -> list[int]:
    """Return a list of byte values from an even-length hex string."""
    n = len(hex_str) // 2
    return [int(hex_str[i * 2 : i * 2 + 2], 16) & 0xFF for i in range(n)]


def _split_prefix(raw: str) -> tuple[str, bool]:
    """Return (hex_body, dp_id_prefixed) from a ``NN#...`` payload.

    ``dp_id_prefixed`` is true for the ``11#`` framing, where each entry starts
    with a one-byte dp_id. The ``10#`` framing has no per-entry dp_id.
    """
    dp_id_prefixed = False
    body = raw
    if "#" in raw:
        dp_id_prefixed = raw[1:2] == "1"
        body = raw.split("#", 1)[1]
    # Some firmwares append a comma-separated ASCII tail after the hex block.
    comma = body.find(",")
    if comma != -1:
        body = body[:comma]
    return body.strip().upper(), dp_id_prefixed


def _parse_entries(data: list[int], dp_id_prefixed: bool) -> list[dict]:
    """Walk the self-describing byte stream into structural entries.

    Header byte layout, derived from our captured payloads:
      - bit 7 selects the form.
      - Compact form (bit 7 clear): the whole field is this one byte; the field
        index is ``(byte >> 4) & 7`` and the byte is its own value.
      - Wide form (bit 7 set): bits 0-1 give ``extra_len`` so the value spans
        ``extra_len + 1`` bytes after the header; bits 2-6 give ``index5``.
        ``index5 <= 30`` means field index ``index5 + 8``; ``index5 == 31`` is
        the extended escape where the real index lives in the next byte.

    Returns ``{"dp_id", "field", "value_bytes"}`` dicts (value_bytes excludes
    the header byte).
    """
    entries: list[dict] = []
    i = 0
    n = len(data)
    while i < n:
        dp_id = 0
        if dp_id_prefixed:
            dp_id = data[i]
            i += 1
            if i >= n:
                break

        header = data[i]
        if not header & 0x80:
            # Compact form: header is both index and value.
            entries.append({"dp_id": dp_id, "field": (header >> 4) & 7, "value_bytes": [header]})
            i += 1
            continue

        extra_len = header & 3
        span = extra_len + 2  # header byte + (extra_len + 1) value bytes
        index5 = (header >> 2) & 31
        if index5 <= 30:
            field = index5 + 8
            chunk = data[i : i + span]
            i += span
        else:
            # Extended escape: the field index is carried in the following byte.
            i += 1
            if i >= n:
                break
            field = (data[i] & 0xFF) + 39
            chunk = data[i : i + span]
            i += span
        entries.append({"dp_id": dp_id, "field": field, "value_bytes": chunk[1:]})
    return entries


def _int_from_bytes(value_bytes: list[int], field: int) -> int | None:
    """Interpret value bytes as an int, honouring per-field endianness."""
    if not value_bytes:
        return None
    order = "little" if field in _LITTLE_ENDIAN_FIELDS else "big"
    return int.from_bytes(bytes(value_bytes), order)


_DATA_TYPE_WIDTH_RE = re.compile(r"^u?int(\d+)$")


def _declared_byte_width(data_type) -> int | None:
    """Parse a declared byte width out of a catalog dpDataType string.

    Only matches strings shaped exactly like "uint8" / "int16" (an optional
    "u" prefix, "int", then a digit run with nothing else). This is
    deliberately anchored rather than a bare digit search: a catalog
    dpDataType like "enum8" embeds a digit that means "8 possible states",
    not "8 bits", and a loose search would silently misparse it as a byte
    width. Returns None when no width can be determined (non-string,
    non-matching shape, or a bit count that is not a whole number of bytes),
    in which case the caller treats the field as "cannot compare" rather
    than guessing at a mismatch.
    """
    if not isinstance(data_type, str):
        return None
    match = _DATA_TYPE_WIDTH_RE.match(data_type)
    if not match:
        return None
    bits = int(match.group(1))
    if bits <= 0 or bits % 8 != 0:
        return None
    return bits // 8


def _match_catalog_dp(dp_list: list, index: int, dp_id: int, dp_id_prefixed: bool) -> dict | None:
    """Return the catalog dp entry for a decoded field, or None on no match.

    The ``11#`` (TLV) framing carries the vendor's real per-instance dp_id on
    each entry, so it is matched first - this also disambiguates duplicate
    structural indices, such as two STA_DURATION fields on different zones.
    The ``10#`` (flat) framing has no per-entry dp_id, so it falls back to
    matching on the structural field index, the same numbering the STA_*
    names in _STATUS_FIELDS were originally harvested from.
    """
    key = dp_id if dp_id_prefixed else index
    for dp in dp_list:
        if isinstance(dp, dict) and dp.get("dpCode") == key:
            return dp
    return None


def _annotate_fields_with_catalog(
    fields: list[dict], model: str, dp_id_prefixed: bool, model_code: int | str | None = None
) -> None:
    """Attach catalog zone/type annotation to fields in place.

    Annotate-never-override: looks up the (model, model_code) variant in the
    committed product catalog and, for each field that maps to a catalog dp
    entry, attaches a "catalog" sub-dict carrying the declared zone (dpPort),
    data type (dpDataType), port number, and a width_mismatch flag. Fields
    with no catalog match are left exactly as built by the caller - no
    "catalog" key is added. This never modifies a field's existing "value" or
    "raw".
    """
    dp_list = get_catalog_entry(model, model_code)
    if not dp_list:
        return

    for field in fields:
        dp_entry = _match_catalog_dp(dp_list, field["index"], field["dp_id"], dp_id_prefixed)
        if dp_entry is None:
            continue
        declared_width = _declared_byte_width(dp_entry.get("dpDataType"))
        actual_width = len(field["raw"]) // 2
        width_mismatch = declared_width is not None and declared_width != actual_width
        field["catalog"] = {
            "dp_port": dp_entry.get("dpPort"),
            "data_type": dp_entry.get("dpDataType"),
            "port_number": dp_entry.get("portNumber"),
            "width_mismatch": width_mismatch,
        }


def decode_generic(raw: str, model: str | None = None, model_code: int | str | None = None) -> dict:
    """Best-effort, model-agnostic decode of a payload for diagnostics.

    Returns a dict shaped as::

        {
            "decoder": "generic-tlv",
            "dp_id_prefixed": bool,
            "fields": [
                {"name": "STA_BAT", "index": 31, "dp_id": 24,
                 "raw": "01", "value": 1},
                ...
            ],
            "field_names": ["STA_BAT", "STA_WKSTATE", "STA_DURATION", ...],
        }

    On any parse failure it returns ``{"decoder": "generic-tlv", "error": ...}``
    - it never raises, so the unknown-device path stays robust.

    When ``model`` is given, each field whose position matches an entry in the
    committed product catalog for that model additionally carries a "catalog"
    sub-dict: ``{"dp_port": ..., "data_type": ..., "port_number": ...,
    "width_mismatch": bool}``. The catalog only annotates - a field's "value"
    and "raw" are never modified by this step. A field with no catalog match,
    a model with no catalog entry, or a model of None all leave the field
    dict exactly as it is without ``model`` (no "catalog" key at all).

    ``model_code`` disambiguates models the vendor maps to several codes whose
    port counts differ. Passing it is what lets the lookup pick the right
    variant; omitting it for such a model yields no annotation rather than a
    coin-flip between variants.
    """
    result: dict = {"decoder": "generic-tlv"}
    try:
        body, dp_id_prefixed = _split_prefix(raw)
        if not body or len(body) % 2 != 0:
            result["error"] = "empty or odd-length hex body"
            return result
        data = _hex_to_bytes(body)
        entries = _parse_entries(data, dp_id_prefixed)
    except Exception as exc:  # diagnostics must not break polling
        _LOGGER.debug("decode_generic failed for %r: %s", raw, exc)
        result["error"] = str(exc)
        return result

    fields: list[dict] = []
    for e in entries:
        index = e["field"]
        value_bytes = e["value_bytes"]
        fields.append(
            {
                "name": _STATUS_FIELDS.get(index, f"UNKNOWN_{index}"),
                "index": index,
                "dp_id": e["dp_id"],
                "raw": bytes(value_bytes).hex(),
                "value": _int_from_bytes(value_bytes, index),
            }
        )

    if model:
        # Defense in depth: decode_generic is normally only reached for
        # unregistered models (see coordinator._decode_subdevice_payload), but
        # guard here too so a hand-written model can never receive catalog
        # annotation even if this function is reached directly. Imported
        # locally to avoid a circular import (trust.py imports from const,
        # not from this module).
        from .trust import is_hand_written_model

        if not is_hand_written_model(model):
            try:
                _annotate_fields_with_catalog(fields, model, dp_id_prefixed, model_code)
            except Exception as exc:  # annotation must never break the diagnostic decode
                _LOGGER.debug("Catalog annotation failed for model=%s: %s", model, exc)

    result["dp_id_prefixed"] = dp_id_prefixed
    result["fields"] = fields
    result["field_names"] = [f["name"] for f in fields]
    return result
