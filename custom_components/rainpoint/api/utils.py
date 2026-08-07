"""
Utility functions for RainPoint API.

This module contains helper functions for payload parsing, data conversion,
and common operations used across the API.
"""

import logging
from datetime import datetime

_LOGGER = logging.getLogger(__name__)

# Structural field indices, equal to the catalog's dpCode for the same
# datapoint. Only the ones a hand-written decoder reads live here.
STA_BAT_FIELD = 31
STA_REPTIME_FIELD = 54

# STA_REPTIME packs a wall-clock date into 32 bits with the year counted from
# this base. Confirmed against captures whose decoded value matched the moment
# they were pulled; a base of 2000 would put those same frames in 2006.
_REPORT_TIME_BASE_YEAR = 2020


def _parse_rainpoint_payload(raw: str) -> bytes:
    """Parse a RainPoint hex payload and return bytes."""
    if "#" not in raw:
        raise ValueError("Payload missing '#' separator")

    prefix, hex_data = raw.split("#", 1)

    # Handle different formats
    if prefix == "10":
        # Standard format: 10#ABCDEF...
        return bytes.fromhex(hex_data)
    elif prefix == "11":
        # TLV format: 11#ABCDEF...
        return bytes.fromhex(hex_data)
    else:
        raise ValueError(f"Unknown payload prefix: {prefix}")


def _parse_tlv_payload(raw: str) -> dict:
    """
    Parse TLV payload for valve hub (11# prefix).

    Format: DP_ID (1 byte) + TYPE (1 byte) + VALUE (variable length based on type).
    There is no explicit length byte; the type byte determines the value width.

    Returns a dictionary mapping DP IDs to (type_byte, value_int, raw_bytes).
    """
    # Type byte → value width in bytes
    _TYPE_WIDTHS = {
        0xD8: 1,  # zone state
        0xDC: 1,  # hub state
        0xAD: 2,  # zone duration (seconds, little-endian)
        0x20: 2,  # timer/schedule config
        0xE1: 2,
        0xB7: 4,  # schedule/timer extended
        0x9F: 4,  # schedule/timer extended
        0xC4: 1,
        0xC5: 1,
        0xC6: 1,
    }

    b = _parse_rainpoint_payload(raw)
    tlv = {}
    i = 0

    while i < len(b):
        if i + 1 >= len(b):
            break

        dp_id = b[i]
        type_byte = b[i + 1]

        width = _TYPE_WIDTHS.get(type_byte)
        if width is None:
            # Unknown type: skip dp_id + type byte pair to attempt re-sync
            _LOGGER.debug("_parse_tlv_payload: unknown type 0x%02X at offset %d (dp_id=0x%02X), skipping", type_byte, i, dp_id)
            i += 2
            continue

        if i + 2 + width > len(b):
            break

        raw_bytes = bytes(b[i + 2 : i + 2 + width])
        # Every multi-byte value in this framing is little-endian. The rule
        # used to single out the 0xAD duration DP, which was the only
        # multi-byte value any decoder read at the time; the wider set now
        # decoded from captured frames (0x9F usage counts, 0xB7 packed
        # timestamps, and the 0xE1 header whose low byte is the signed RSSI)
        # is little-endian too, and no captured record reads correctly as
        # big-endian.
        value_int = int.from_bytes(raw_bytes, "little")
        tlv[dp_id] = (type_byte, value_int, raw_bytes)
        i += 2 + width

    if i < len(b):
        _LOGGER.debug("_parse_tlv_payload: %d unparsed trailing bytes at offset %d: %s", len(b) - i, i, b[i:].hex())

    return tlv


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


def _is_ascii_payload(raw: str) -> bool:
    """True when raw is the comma-and-semicolon ASCII framing, not a hex body.

    Three clauses, all required: ``"#"`` is absent (a ``NN#`` prefix always
    wins and routes to the hex path first -- every payload
    ``_split_prefix``'s tail-truncation branch was written for carries that
    prefix, so it is routed away before this test is ever reached); ``";"``
    is present, separating the header from the body;
    and a ``","`` appears in the pre-semicolon header, matching the
    ``[flags],[rssi],[flags];...`` shape both hand-written ASCII decoders in
    ``decoders.py`` already parse. Deliberately not requiring the full
    three-part header here: an ASCII-shaped payload with a short or malformed
    header must still route here and be declined by the ASCII branch, never
    fall through to the hex path to be misreported as a hex parse failure
    (the fail-closed rule this module's caller depends on). The stricter
    three-part check belongs to ``_parse_ascii_rssi``, which reads the header
    once it has already been routed here. Mirrors the ordering the two
    routers in ``decoders.py`` use: hex prefix checked first, ASCII shape
    checked as the fallback.
    """
    if "#" in raw:
        return False
    if ";" not in raw:
        return False
    header = raw.split(";", 1)[0]
    return "," in header


def _parse_ascii_rssi(raw: str) -> int | None:
    """Read the ASCII header's rssi token, or None when it cannot be trusted.

    Splits on the first ``";"``, then the header on ``","``. Returns None on
    a header with fewer than three parts, on a non-integer rssi token, and on
    a non-negative value -- non-negative already means "malformed" on this
    wire format (both hand-written ASCII decoders treat it the same way).
    Unlike those two decoders, this helper emits no log record on any path,
    at any level: it runs on every poll for an affected device, not once per
    manually-triggered decode, so the same WARNING here would be per-poll
    spam rather than a one-off diagnostic.
    """
    header = raw.split(";", 1)[0]
    parts = header.split(",")
    if len(parts) < 3:
        return None
    try:
        rssi = int(parts[1])
    except (ValueError, IndexError):
        return None
    return rssi if rssi < 0 else None


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


def _find_field_value(b: bytes, field: int, *, dp_id_prefixed: bool = False) -> list[int] | None:
    """Return the value bytes of the first ``field`` record in b, or None.

    Structural: the frame is walked record by record, so a byte that merely
    happens to equal a type byte cannot be mistaken for one. Reading these
    datapoints at fixed offsets is what made the previous battery extraction
    land on the trailing report-time header instead.
    """
    for entry in _parse_entries(list(b), dp_id_prefixed):
        if entry["field"] == field:
            return entry["value_bytes"]
    return None


def _decode_packed_report_time(raw: int) -> str | None:
    """Unpack a 32-bit STA_REPTIME word into an ISO-8601 local wall-clock string.

    Bit layout, most significant first: 6 bits year (from
    ``_REPORT_TIME_BASE_YEAR``), 4 month, 5 day, 5 hour, 6 minute, 6 second.
    The value carries no UTC offset, so the result is deliberately naive: it is
    the device's own wall clock, not a point on the timeline. Out-of-range
    fields (a malformed or misaligned frame) yield None rather than a
    fabricated date.
    """
    year = _REPORT_TIME_BASE_YEAR + ((raw >> 26) & 0x3F)
    month = (raw >> 22) & 0x0F
    day = (raw >> 17) & 0x1F
    hour = (raw >> 12) & 0x1F
    minute = (raw >> 6) & 0x3F
    second = raw & 0x3F
    try:
        return datetime(year, month, day, hour, minute, second).isoformat()
    except ValueError:
        return None


def _extract_report_time(b: bytes, *, dp_id_prefixed: bool = False) -> tuple[str, int] | None:
    """Return (iso_wall_clock, raw_word) for the frame's STA_REPTIME, or None."""
    value_bytes = _find_field_value(b, STA_REPTIME_FIELD, dp_id_prefixed=dp_id_prefixed)
    if not value_bytes or len(value_bytes) != 4:
        return None
    raw = int.from_bytes(bytes(value_bytes), "little")
    iso = _decode_packed_report_time(raw)
    return None if iso is None else (iso, raw)


def _encode_dp_duration_param(seconds: int) -> str:
    """Encode a duration in seconds as the ``param`` string ``controlWorkModeDP`` reads.

    The endpoint carries the run duration as an unsigned 4-byte little-endian
    hex string with no separator and no prefix, in place of a ``duration``
    field: 60 becomes ``"3C000000"``. Clamped rather than raised on an
    out-of-range input -- a negative value clamps to 0 and anything past the
    4-byte ceiling clamps to ``"FFFFFFFF"`` -- because a caller-side
    ``OverflowError`` here would surface to the user as a failed valve command
    with no diagnostic.
    """
    clamped = max(0, min(int(seconds), 0xFFFFFFFF))
    return clamped.to_bytes(4, "little").hex().upper()


def _le16(b: bytes, offset: int) -> int:
    """Extract little-endian 16-bit integer from bytes at offset."""
    return int.from_bytes(b[offset : offset + 2], "little")


def _f10_to_c(temp_raw_f10: int) -> float:
    """Convert temperature from F*10 to Celsius."""
    return (temp_raw_f10 / 10.0 - 32.0) * 5.0 / 9.0


def _base_decoder_dict(device_type: str, rssi: int, raw_bytes: bytes) -> dict:
    """Create base decoder dictionary with common fields."""
    return {
        "type": device_type,
        "rssi_dbm": rssi,
        "raw_bytes": raw_bytes,
    }
