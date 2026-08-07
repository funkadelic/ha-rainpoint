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

from .product_catalog import get_catalog_entry, get_catalog_port_number
from .utils import _is_ascii_payload, _parse_ascii_rssi, _parse_entries, _split_prefix

_LOGGER = logging.getLogger(__name__)

# The result-dict key marking a decode that went through the ASCII branch.
# Present only on that branch's result (never False on a hex result, only
# absent) so is_ascii_declined's truthiness read fails closed on every hex
# path and on a dict that never went through decode_generic at all.
_ASCII_FRAMED_KEY = "ascii_framed"

# ASCII-framed payloads carry no ordering rule the body can be recovered
# under. The header holds in 3 of 3 committed samples across 3 unrelated
# device families (HTV213FRF/245, HCS021FRF, HWS019WRF-V2), while the body
# agrees across none of them: pipe-separated six-field zone groups; a flat
# triple with a G= sub-encoding; parenthesised current/min/flag triples with
# a P= prefix and a trailing comma. The catalog is keyed by dpCode, not by
# wire position, so it carries no positional meaning to derive an ordering
# from either. A per-family position table and a catalog-derived ordering
# were both considered and rejected: a table with zero populated rows is a
# code path nothing exercises, and asserting a dpCode-to-wire-position
# correspondence no sample supports would be inventing evidence. The body is
# therefore declined outright rather than guessed at.
_ASCII_DECLINED_ERROR = "ASCII-framed payload: header read, body declined (field ordering is positional and not recoverable)"


# Field index -> status identity, harvested from the RainPoint/HomGar cloud
# product catalog's dp[].dpCode / dp[].identity pairs. The catalog is served
# by the RainPoint API at https://region3.homgarus.com/app/common/core/productModel
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

# Every multi-byte value in these framings is little-endian. This used to
# single out STA_DURATION (field index 19), the one multi-byte field a
# hand-written decoder read at the time; captured frames since then decode
# correctly as little-endian for STA_LASTUSAGE (index 15, a raw flow count)
# and STA_EVTIME (index 21, a packed timestamp) as well, and big-endian reads
# of those same records yield nine- and ten-digit nonsense. No captured record
# is known to be big-endian.


def _hex_to_bytes(hex_str: str) -> list[int]:
    """Return a list of byte values from an even-length hex string."""
    n = len(hex_str) // 2
    return [int(hex_str[i * 2 : i * 2 + 2], 16) & 0xFF for i in range(n)]


def _int_from_bytes(value_bytes: list[int]) -> int | None:
    """Interpret value bytes as a little-endian int, or None when there are none."""
    if not value_bytes:
        return None
    return int.from_bytes(bytes(value_bytes), "little")


# RainPoint's dpDataType vocabulary is "U8" / "S16" / "U32" style: a
# signedness letter then a bit count. Anchored rather than a bare digit
# search, so a future type name that merely embeds a digit (an "ENUM8" whose
# 8 means "8 possible states", not "8 bits") is not misread as a width.
_DATA_TYPE_RE = re.compile(r"^([US])(\d+)$")


def _parse_data_type(dp_entry: dict) -> re.Match | None:
    """Return a dp entry's parsed dpDataType, or None when it is absent or unparseable."""
    data_type = dp_entry.get("dpDataType")
    if not isinstance(data_type, str):
        return None
    return _DATA_TYPE_RE.match(data_type)


def _declared_byte_width(dp_entry: dict) -> int | None:
    """Return a catalog dp entry's declared byte width, or None if it has none.

    dpLen is RainPoint's own byte count and is authoritative: it is present on
    every entry, including the ones whose dpDataType is blank, and it disagrees
    with the type name where the two conflict (the "TD2" timestamp type appears
    at both 1 and 2 bytes). The dpDataType parse is only a fallback for an entry
    that somehow lacks a usable dpLen.

    A dpLen of 0 means variable-length (the STRING types) and yields None, as
    does any non-integer value. None means "cannot compare", which the caller
    treats as no mismatch rather than guessing at one.

    dp_entry is always a dict: _match_catalog_dp only ever returns entries it
    has already type-checked.
    """
    dp_len = dp_entry.get("dpLen")
    if isinstance(dp_len, int) and not isinstance(dp_len, bool) and dp_len > 0:
        return dp_len

    match = _parse_data_type(dp_entry)
    if not match:
        return None
    bits = int(match.group(2))
    if bits <= 0 or bits % 8 != 0:
        return None
    return bits // 8


def _declared_signedness(dp_entry: dict) -> bool | None:
    """Return True/False for a signed/unsigned dp entry, or None if undeclared.

    Signedness comes from the dpDataType letter ("S16" is signed, "U8" is
    not). Types that carry no signedness at all (STRING, the timestamp types
    T4/TD2, and blank values) return None rather than defaulting to unsigned,
    so a caller can tell "RainPoint says unsigned" apart from "RainPoint does
    not say".

    dp_entry is always a dict, for the same reason as _declared_byte_width.
    """
    match = _parse_data_type(dp_entry)
    if not match:
        return None
    return match.group(1) == "S"


def _match_catalog_dp(dp_list: list, index: int) -> dict | None:
    """Return the catalog dp entry for a flat (10#) field's structural index, or None.

    The ``10#`` framing has no per-entry dp_id, so the structural field index
    is the only candidate key - the same numbering the STA_* names in
    _STATUS_FIELDS were originally harvested from, and the numbering the
    catalog's dpCode lines up with on both framings.

    dpCode is RainPoint's per-instance identifier and is therefore expected
    to be unique within a model for a single-field key. The catalog is
    regenerated from an external API though, so that is an assumption rather
    than a guarantee: an ambiguous key (two or more entries sharing it)
    yields None instead of whichever entry happened to sort first, since a
    wrong zone number is worse than an absent one.
    """
    matches = [dp for dp in dp_list if isinstance(dp, dict) and dp.get("dpCode") == index]
    if len(matches) == 1:
        return matches[0]
    if matches:
        _LOGGER.debug("Catalog has %d entries for dpCode %s; leaving the field unannotated", len(matches), index)
    return None


def _usable_dp_port(dp_port) -> bool:
    """True when a declared dpPort is a plain int, usable to order a group.

    Mirrors generic_entities._usable_port's shape test: a bool is technically
    an int subtype in Python but is never a real port number, and anything
    else (a JSON list, a string, None) cannot be sorted against its peers.
    """
    return isinstance(dp_port, int) and not isinstance(dp_port, bool)


def _pair_group_by_dp_id_and_port(group_fields: list[dict], candidates: list[dict]) -> list[tuple[dict, dict]]:
    """Pair one dpCode's worth of 11# fields to their catalog candidates, or refuse the whole group.

    Several decoded fields can share one structural index - the ordinary
    multi-zone shape, such as two STA_WKSTATE fields on a two-zone valve.
    Disambiguation pairs the fields in ascending dp_id order (RainPoint's
    real per-instance ordering handle, only present on ``11#`` TLV framing)
    against the catalog's own candidates in ascending dpPort order. This is
    the pairing validated in tests/api/test_tlv_catalog_alignment.py against
    the trusted hand-written valve decoder's exact per-zone assignment.

    The whole group is refused - never a prefix of it - when there are no
    candidates, when the candidate count disagrees with the field count, or
    when the group has more than one member and any candidate's dpPort is
    not a plain int (a group of one keeps the flat path's simple behaviour
    and does not require a usable dpPort, so a variant whose dpPort the
    RainPoint left unusable still gets its data-type and width annotation).
    """
    if not candidates or len(candidates) != len(group_fields):
        return []
    if len(group_fields) > 1 and not all(_usable_dp_port(dp.get("dpPort")) for dp in candidates):
        return []
    ordered_fields = sorted(group_fields, key=lambda f: f["dp_id"])
    ordered_candidates = sorted(candidates, key=lambda dp: dp.get("dpPort"))
    if len(ordered_fields) > 1:
        # A group of one has nothing to disambiguate. A group of more than
        # one is paired purely by position (ascending dp_id zipped against
        # ascending dpPort) rather than by an unambiguous per-entry key, so
        # log the pairing at debug level -- this is the one place a future
        # catalog-driven variant whose dp_id numbering does not track its
        # dpPort numbering would be discoverable, rather than silently
        # mis-paired.
        _LOGGER.debug(
            "Pairing %d fields by positional dp_id/dpPort ordering: dp_ids=%s dpPorts=%s",
            len(ordered_fields),
            [f["dp_id"] for f in ordered_fields],
            [dp.get("dpPort") for dp in ordered_candidates],
        )
    return list(zip(ordered_fields, ordered_candidates, strict=True))


def _apply_catalog_annotation(field: dict, dp_entry: dict, port_number: int | None) -> None:
    """Attach one catalog dp entry's zone/type annotation to a field, in place.

    Annotate-never-override: this never modifies a field's existing "value"
    or "raw". port_number is a property of the model variant, not of the
    individual dp entry, so callers resolve it once for the whole model
    rather than read it off each dp entry.
    """
    declared_width = _declared_byte_width(dp_entry)
    actual_width = len(field["raw"]) // 2
    width_mismatch = declared_width is not None and declared_width != actual_width
    field["catalog"] = {
        "dp_port": dp_entry.get("dpPort"),
        "data_type": dp_entry.get("dpDataType"),
        "declared_width": declared_width,
        "signed": _declared_signedness(dp_entry),
        "port_number": port_number,
        "width_mismatch": width_mismatch,
    }


def _annotate_fields_with_catalog(
    fields: list[dict], model: str, dp_id_prefixed: bool, model_code: int | str | None = None
) -> None:
    """Attach catalog zone/type annotation to fields in place.

    Looks up the (model, model_code) variant in the committed product
    catalog and, for each field that maps to a catalog dp entry, attaches a
    "catalog" sub-dict via _apply_catalog_annotation. Fields with no catalog
    match are left exactly as built by the caller - no "catalog" key is
    added.

    Both framings key on the field's structural index against the catalog's
    dpCode - that is the numbering space the catalog actually uses, on both
    the ``10#`` (flat) and ``11#`` (TLV) framings. The ``11#`` framing
    additionally carries a per-entry dp_id, RainPoint's per-instance
    ordering handle; it is not in the catalog's dpCode space, so it is never
    compared against dpCode, but it disambiguates several fields sharing one
    index by fixing the order they pair against that index's candidate
    catalog entries (see _pair_group_by_dp_id_and_port). The ``10#`` framing
    has no per-entry dp_id, so a shared index there is simply ambiguous and
    is refused by _match_catalog_dp.
    """
    dp_list = get_catalog_entry(model, model_code)
    if not dp_list:
        return

    port_number = get_catalog_port_number(model, model_code)

    if dp_id_prefixed:
        _annotate_tlv_fields(fields, dp_list, port_number)
    else:
        _annotate_flat_fields(fields, dp_list, port_number)


def _annotate_tlv_fields(fields: list[dict], dp_list: list, port_number: int | None) -> None:
    """Annotate ``11#`` fields, disambiguating a shared index by dp_id then port.

    Fields sharing one structural index pair against that index's catalog
    candidates in _pair_group_by_dp_id_and_port order; see the framing note
    in _annotate_fields_with_catalog.
    """
    groups: dict[int, list[dict]] = {}
    for field in fields:
        groups.setdefault(field["index"], []).append(field)
    for index, group_fields in groups.items():
        candidates = [dp for dp in dp_list if isinstance(dp, dict) and dp.get("dpCode") == index]
        for field, dp_entry in _pair_group_by_dp_id_and_port(group_fields, candidates):
            _apply_catalog_annotation(field, dp_entry, port_number)


def _annotate_flat_fields(fields: list[dict], dp_list: list, port_number: int | None) -> None:
    """Annotate ``10#`` fields, matching each field's index to a unique catalog dp.

    A shared index is ambiguous on this framing (no per-entry dp_id) and is
    refused by _match_catalog_dp, so the field is left unannotated.
    """
    for field in fields:
        dp_entry = _match_catalog_dp(dp_list, field["index"])
        if dp_entry is None:
            continue
        _apply_catalog_annotation(field, dp_entry, port_number)


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

    A payload using the comma-and-semicolon ASCII framing (``[flags],[rssi],
    [flags];...``) is read for its header only. The result carries both
    "error" and "fields" together - unlike the hex failure paths, which
    carry "error" alone - because the header rssi is a genuine reading even
    though the body is declined. "fields" holds at most one entry, a
    synthetic ``STA_RSSI`` field with "index", "dp_id" and "raw" all
    ``None`` (there is no byte-stream position behind a header-derived
    value) or is empty when the header rssi is non-negative or unparseable.
    "ascii_framed" is ``True`` on this result and absent - never ``False`` -
    on every hex result; see ``is_ascii_declined``. The catalog annotation
    step below never runs for an ASCII result: a header-derived field has no
    structural index for it to match against.

    When ``model`` is given, each field whose position matches an entry in the
    committed product catalog for that model additionally carries a "catalog"
    sub-dict: ``{"dp_port": ..., "data_type": ..., "declared_width": ...,
    "signed": ..., "port_number": ..., "width_mismatch": bool}``. Both
    "declared_width" and "signed" are None when RainPoint does not declare
    them. The catalog only annotates - a field's "value"
    and "raw" are never modified by this step. A field with no catalog match,
    a model with no catalog entry, or a model of None all leave the field
    dict exactly as it is without ``model`` (no "catalog" key at all).

    ``model_code`` disambiguates models RainPoint maps to several codes whose
    port counts differ. Passing it is what lets the lookup pick the right
    variant; omitting it for such a model yields no annotation rather than a
    coin-flip between variants.
    """
    result: dict = {"decoder": "generic-tlv"}

    try:
        ascii_framed = _is_ascii_payload(raw)
    except Exception as exc:  # diagnostics must not break polling
        _LOGGER.debug("decode_generic failed for %r: %s", raw, exc)
        result["error"] = str(exc)
        return result

    if ascii_framed:
        rssi = _parse_ascii_rssi(raw)
        fields = (
            [
                {
                    "name": "STA_RSSI",
                    "index": None,
                    "dp_id": None,
                    "raw": None,
                    "value": rssi,
                }
            ]
            if rssi is not None
            else []
        )
        result[_ASCII_FRAMED_KEY] = True
        result["error"] = _ASCII_DECLINED_ERROR
        result["dp_id_prefixed"] = False
        result["fields"] = fields
        result["field_names"] = [f["name"] for f in fields]
        return result

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
                "value": _int_from_bytes(value_bytes),
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


def is_ascii_declined(generic: dict | None) -> bool:
    """True when a decode_generic result went through the declined-ASCII branch.

    Public-named rather than underscore-prefixed to match has_declared_width,
    the existing guard in this codebase that crosses a module boundary for
    the same reason: generic_control's readback needs to refuse an
    ASCII-framed result explicitly rather than relying on its fields list
    happening to be run-state-free.

    Fails closed on None, on a non-dict, and on any hex result, because the
    marker is never set to False - only left absent - on those. A caller
    cannot distinguish "this was a hex result" from "this was never decoded"
    from this function's return value alone, and must not try.
    """
    return bool(isinstance(generic, dict) and generic.get(_ASCII_FRAMED_KEY))
