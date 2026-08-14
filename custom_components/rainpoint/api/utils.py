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

# The hub record's own `param` field is a separate pipe-delimited wire shape
# from the structural indices above -- it is not a datapoint dpCode, it is the
# raw string `POST /app/device/main/update` reads and writes. Index 1 is the
# broadcast flag, "1" on and "0" off. Polarity comes from a bracketed pair of
# writes whose paramVersion moved in opposite directions across two calls, not
# from a single write: the first capture recorded the polarity backwards, and
# only the second, bracketing write settled it.
_HUB_PARAM_DELIMITER = "|"
_HUB_BROADCAST_FIELD_INDEX = 1

# STA_REPTIME packs a wall-clock date into 32 bits with the year counted from
# this base. Confirmed against captures whose decoded value matched the moment
# they were pulled; a base of 2000 would put those same frames in 2006.
_REPORT_TIME_BASE_YEAR = 2020

# A sub-device record's own `param` field is a third wire shape, distinct from
# both the structural indices above and the hub's pipe-delimited `param`: a
# comma-separated list of `key=value` tokens, of unknown total key count. Key
# 5 is the only one this integration understands -- its transmission-power
# setting, confirmed twice by independent experiment. Keys 11, 12, 50 and 51
# are observed but unidentified, and must survive a splice byte-for-byte.
_SUB_PARAM_DELIMITER = ","
_SUB_PARAM_ASSIGNMENT = "="
_SUB_POWER_MODE_KEY = "5"
# Every key-5 wire token this integration has been asked to accept, mapped to
# its canonical one-character mode digit. Both an unpadded and a zero-padded
# width are accepted -- the semantic mapping is the set {"0", "1", "2"},
# but every blob captured from the only known target device uses the
# zero-padded form ("01", "02"), so the unpadded set alone would ship the
# entity dead on the one device this control targets. Nothing outside these six
# literal tokens is ever accepted; widening this set is a captured-evidence
# decision, never a guess.
_SUB_POWER_MODE_WIRE_VALUES = {
    "0": "0",
    "00": "0",
    "1": "1",
    "01": "1",
    "2": "2",
    "02": "2",
}

# Longest cloud-supplied key rendered into a log summary before it is cut. A
# key is metadata rather than a value, but it still arrives from a payload
# nobody here controls, so it gets a bound like any other untrusted string.
_SUMMARY_KEY_MAX_LEN = 40

# Keys are rendered through this whitelist rather than escaped, because the
# set of characters a legitimate JSON key needs here is small and known. A
# newline or an ANSI escape smuggled into a key name would otherwise forge log
# lines in the file a user is about to paste into a public issue.
_SUMMARY_KEY_SAFE_CHARS = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-.")

# Caps on what one summary may render. A cloud response is not size-bounded by
# anything this integration controls, and the summary is a string built into a
# log record, so both the number of keys named and the number of list items
# walked to collect them are held to a limit. Exceeding either is reported in
# the output rather than hidden, so a truncated summary never reads as a
# complete one.
_SUMMARY_MAX_KEYS = 32
_SUMMARY_MAX_ITEMS = 50

# Rendered in place of a value that is absent or empty. One constant so the
# three renderers cannot drift onto different spellings of the same idea.
_EMPTY_MARKER = "<empty>"


def _redact_secret(value: str | None) -> str:
    """Render a secret as length + last-4 only -- never the raw value."""
    if not value:
        return _EMPTY_MARKER
    if len(value) <= 4:
        return f"len={len(value)} <short>"
    return f"len={len(value)} last4={value[-4:]}"


def _redact_identifier(value: str | None) -> str:
    """Render an identifier as its length alone -- no suffix, no source characters.

    Deliberately not _redact_secret. That helper keeps the last four characters
    so a reader can tell which credential is in play, which is a fair trade for
    a secret nobody can act on without the rest of it. It is the wrong trade for
    an identifier: the MQTT username is "<deviceName>&<productKey>", so its last
    four characters are the tail of a cloud device identifier that this
    integration otherwise keeps out of logs entirely.
    """
    if not value:
        return _EMPTY_MARKER
    return f"len={len(value)}"


def _safe_key(key: object) -> str:
    """Render one cloud-supplied key name, bounded and stripped to a safe charset."""
    text = str(key)
    cleaned = "".join(c if c in _SUMMARY_KEY_SAFE_CHARS else "?" for c in text[:_SUMMARY_KEY_MAX_LEN])
    if len(text) > _SUMMARY_KEY_MAX_LEN:
        cleaned += "~"
    return cleaned or _EMPTY_MARKER


def _summarize_record(record: object) -> str:
    """Render a cloud record as its shape and key names, never its values.

    This is the shape every log line on a cloud-record path uses instead of
    dumping the record itself. Summarising rather than redacting field by
    field is deliberate: a redaction list has to be maintained against a
    payload nobody here controls, and it fails silently the first time the
    vendor adds a field. A key-and-count summary has the opposite failure
    mode, because a new field shows up as a name with no value attached.

    Nested values are counted, not walked. A list reports its length and the
    union of its items' keys, so a status response reads as "n=4" plus the
    field set rather than four device records in full.

    Output is bounded on both axes. The reported n is always the true length,
    but at most _SUMMARY_MAX_ITEMS items are walked to collect keys and at most
    _SUMMARY_MAX_KEYS names are rendered, because nothing here controls how
    large a cloud response can be.
    """
    if record is None:
        return "<none>"
    if isinstance(record, dict):
        return f"dict(n={len(record)}) keys=[{_render_keys(record)}]"
    if isinstance(record, (list, tuple)):
        walked = record[:_SUMMARY_MAX_ITEMS]
        union: set[object] = set()
        for item in walked:
            if isinstance(item, dict):
                union.update(item)
        # Only stated when it is true, so an ordinary summary is not cluttered
        # by a marker that always reads the same.
        scanned = f" scanned={len(walked)}" if len(record) > len(walked) else ""
        return f"list(n={len(record)}) keys=[{_render_keys(union)}]{scanned}"
    return f"<{type(record).__name__}>"


def _render_keys(keys) -> str:
    """Render a key collection as a sorted, deduplicated, count-capped name list."""
    safe = sorted({_safe_key(k) for k in keys})
    if len(safe) <= _SUMMARY_MAX_KEYS:
        return ",".join(safe)
    kept = safe[:_SUMMARY_MAX_KEYS]
    return ",".join(kept) + f",+{len(safe) - _SUMMARY_MAX_KEYS} more"


class _RecordSummary:
    """A _summarize_record call deferred until something actually formats it.

    Log call arguments are evaluated whether or not the record survives level
    filtering, so a bare _summarize_record(...) inside a _LOGGER.debug(...) does
    its work on every poll even with debug logging off. Wrapping it means the
    string is built only when a handler formats the record, which is the whole
    difference between a debug aid and a cost every user pays.

    Use this at log call sites. Call _summarize_record directly only when the
    string is wanted right now.
    """

    __slots__ = ("_record",)

    def __init__(self, record: object) -> None:
        self._record = record

    def __str__(self) -> str:
        """Render the summary, at format time rather than at call time."""
        return _summarize_record(self._record)

    def __repr__(self) -> str:
        """Match __str__ so %r and %s agree in a log line."""
        return _summarize_record(self._record)


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


def _parse_hub_broadcast_flag(param: object) -> bool | None:
    """Read the hub record's `param` index-1 broadcast flag, or None when it
    cannot be trusted.

    `param` arrives from a cloud JSON document with no guaranteed type, so
    anything other than a `str` is rejected outright -- `bool` is included in
    that rejection deliberately, since it is an `int` subclass and would
    otherwise pass a naive `isinstance(param, str)`-adjacent check by
    accident on a caller that forgot the order.

    The gate is a minimum-index test (`len(fields) > _HUB_BROADCAST_FIELD_INDEX`),
    never an exact-field-count test: a hub whose `param` carries three or five
    pipe-delimited fields still has a recoverable index 1, and an exact-count
    gate would put such a hub permanently unknown even though nothing here
    needs to understand its other fields. Do not tighten this to match the
    four fields the one observed hub happens to produce.

    Returns True for the token "1" at that index, False for "0", and None for
    anything else -- a missing/empty/short param, a non-str param, or an
    unrecognised token. Like `_parse_ascii_rssi`, this never raises and never
    logs at any level: it runs on every poll for an affected hub, so a log
    line here would be per-poll spam rather than a one-off diagnostic.
    """
    if not isinstance(param, str):
        return None
    fields = param.split(_HUB_PARAM_DELIMITER)
    if len(fields) <= _HUB_BROADCAST_FIELD_INDEX:
        return None
    token = fields[_HUB_BROADCAST_FIELD_INDEX]
    if token == "1":
        return True
    if token == "0":
        return False
    return None


def _splice_hub_broadcast_param(param: object, enabled: bool) -> str | None:
    """Return `param` with only index 1 replaced by the requested flag, or
    None when the same gate that blocks the read blocks the write.

    Calls `_parse_hub_broadcast_flag` itself as the gate -- not a
    re-implementation of it -- so the two functions cannot drift apart on
    what counts as readable: a `param` this module could not itself parse to
    a `bool` (wrong type, too few fields, or an index-1 token that is neither
    "0" nor "1") never reaches the split-and-replace below. On success the
    string is split on the delimiter, the element at
    `_HUB_BROADCAST_FIELD_INDEX` is replaced with the literal "1" or "0", and
    the result is rejoined on the same delimiter. Every other element is
    carried across untouched and unparsed: no `strip`, no case change, no
    re-encode, no normalisation, no defaulting of an empty field. The output
    field count always equals the input field count, and adjacent delimiters
    -- which denote empty fields -- survive as the same empty fields. The
    replacement token is always one of these two local literals; it is never
    derived from the input, so no cloud-supplied text can reach the field
    this function writes. Never raises, never logs at any level, for the same
    reason `_parse_hub_broadcast_flag` does not.
    """
    if _parse_hub_broadcast_flag(param) is None:
        return None
    # param is guaranteed a str with a recoverable index 1 at this point --
    # the gate above already proved it.
    fields = param.split(_HUB_PARAM_DELIMITER)
    fields[_HUB_BROADCAST_FIELD_INDEX] = "1" if enabled else "0"
    return _HUB_PARAM_DELIMITER.join(fields)


def _parse_sub_power_mode(param: object) -> str | None:
    """Read a sub-device record's `param` key-5 transmission-power mode, or None.

    `param` arrives from a cloud JSON document with no guaranteed type, so
    anything other than a `str` is rejected outright -- `bool` is included in
    that rejection deliberately, for the same reason `_parse_hub_broadcast_flag`
    excludes it.

    The blob is split on the comma delimiter into an ordered list of raw
    tokens. Every token must contain exactly one `=`; a token with none or with
    two returns None immediately. No key may repeat -- a duplicated key
    anywhere in the blob, key 5 or otherwise, returns None rather than letting
    a later occurrence silently win, which is the dict-coalescing key loss
    this gate exists to prevent. Key 5 must be present, and its value must be a
    member of `_SUB_POWER_MODE_WIRE_VALUES`; any other key and any other
    value are read as opaque and simply carried past unvalidated.

    The key count is deliberately not fixed to the four other keys (11, 12,
    50, 51) the one observed device happens to produce: a blob carrying a key
    nobody has yet identified, alongside a valid key 5, still parses. Do not
    tighten this to an exact key set.

    Returns the canonical one-character mode digit ("0", "1", or "2") on
    success, None on any failure. Like `_parse_hub_broadcast_flag`, this never
    raises and never logs at any level: it runs on every poll for an affected
    sub-device, so a log line here would be per-poll spam rather than a
    one-off diagnostic.
    """
    if not isinstance(param, str):
        return None
    seen_keys: set[str] = set()
    mode: str | None = None
    for token in param.split(_SUB_PARAM_DELIMITER):
        parts = token.split(_SUB_PARAM_ASSIGNMENT)
        if len(parts) != 2:
            return None
        key, value = parts
        if key in seen_keys:
            return None
        seen_keys.add(key)
        if key == _SUB_POWER_MODE_KEY:
            mode = _SUB_POWER_MODE_WIRE_VALUES.get(value)
            if mode is None:
                return None
    if _SUB_POWER_MODE_KEY not in seen_keys:
        return None
    return mode


def _splice_sub_power_mode(param: object, mode: str) -> str | None:
    """Return `param` with only key 5's value token replaced, or None when the
    same gate that blocks the read blocks the write.

    Calls `_parse_sub_power_mode` itself as the gate -- not a re-implementation
    of it -- so the two functions cannot drift apart on what counts as
    readable: a `param` this module could not itself parse (wrong type, a
    malformed token, a duplicate key, an absent key 5, or an unrecognised
    key-5 value) never reaches the splice below. `mode` must itself be one of
    the three canonical digits; anything else returns None.

    On success, the blob is split on the delimiter and rejoined; only the
    token whose parsed key is the key-5 key has its value replaced, and the
    replacement keeps the character width of the value it replaced (a
    two-character value in yields a two-character value out, matching the
    only write shape the cloud has been observed to accept). Every other
    token -- keys 11, 12, 50, 51 and anything not yet identified -- is carried
    across byte for byte, unparsed, unstripped, un-normalised, in its original
    position. This never rebuilds the string from parsed keys and never
    routes it through a dict, so a duplicate key cannot silently coalesce.
    Never raises, never logs at any level, for the same reason
    `_parse_sub_power_mode` does not.
    """
    if _parse_sub_power_mode(param) is None:
        return None
    if mode not in ("0", "1", "2"):
        return None
    # param is guaranteed a str with a well-formed, uniquely-keyed token list
    # and a recoverable key-5 value at this point -- the gate above already
    # proved it.
    spliced_tokens = []
    for token in param.split(_SUB_PARAM_DELIMITER):
        key, sep, value = token.partition(_SUB_PARAM_ASSIGNMENT)
        if key == _SUB_POWER_MODE_KEY:
            spliced_tokens.append(f"{key}{sep}{mode.rjust(len(value), '0')}")
        else:
            spliced_tokens.append(token)
    return _SUB_PARAM_DELIMITER.join(spliced_tokens)


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
