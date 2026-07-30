"""
Decoder functions for RainPoint API.

This module contains all device-specific decoder functions for different
RainPoint device types.
"""

import logging
import re
from datetime import datetime

from .utils import (
    _base_decoder_dict,
    _extract_report_time,
    _f10_to_c,
    _le16,
    _parse_rainpoint_payload,
    _parse_tlv_payload,
)
from .validators import (
    _battery_flag_to_percent,
    _extract_battery_flag,
    _extract_rssi,
    _validate_payload,
    _validate_tag,
)

_LOGGER = logging.getLogger(__name__)


def _attach_report_time(result: dict, b: bytes, *, dp_id_prefixed: bool = False) -> None:
    """Add report_time / report_time_raw to result when the frame carries STA_REPTIME.

    Both keys stay absent when the record is missing or unpacks to an
    impossible date, so a consumer never has to distinguish a real reading from
    a fabricated fallback.
    """
    report = _extract_report_time(b, dp_id_prefixed=dp_id_prefixed)
    if report is not None:
        result["report_time"], result["report_time_raw"] = report


# Type byte → value byte count for HTV213FRF/HTV245FRF.
# Subset of types relevant to these models; see _TYPE_WIDTHS in utils.py for the full set.
_HTV213_TYPE_LENGTHS = {0xDC: 1, 0xD8: 1, 0x20: 2, 0xAD: 2, 0xB7: 4, 0x9F: 4}

# dp_id block bases for the HTV213FRF/HTV245FRF family. Each per-zone reading
# owns a block of consecutive dp_ids, so zone N is <base> + N: state 0x19..,
# event time 0x21.., duration 0x25.., usage 0x29.. on a 2-zone hub. The blocks
# are four wide, so a fifth zone's dp_id would land on the next block's first
# record; the type-byte check on every read below is what keeps that from being
# misread as the zone's own value rather than an assumption that it cannot
# happen.
_HTV213_DP_BASE_STATE = 0x18
_HTV213_DP_BASE_EVENT_TIME = 0x20
_HTV213_DP_BASE_DURATION = 0x24
_HTV213_DP_BASE_USAGE = 0x28

# Gallons per raw usage count. Calibrated against a single maintainer reading:
# a run the frame reported as 421 counts showed as 0.8 gal in the vendor app,
# which fits a 500-count-per-gallon flow sensor to well inside the one decimal
# the app displays. Two other candidate factors (1/512 gal, 7.5 mL) also round
# to that same 0.8, so the raw count is preserved next to the converted value:
# a finer calibration from a larger run can be applied later without
# re-capturing anything, and without invalidating stored history, since the
# usage entity deliberately stays out of long-term statistics.
_USAGE_GALLONS_PER_COUNT = 1 / 500

# Year offset for the packed timestamps carried by the 4-byte 0xB7 records.
_TIMESTAMP_YEAR_BASE = 2020

# Type byte → value byte count for the HTV145FRF single-outlet timer.
# Unlike HTV213FRF (11# dp_id/type/value stream), this model ships a 10#-prefixed
# marker/value stream where each record is [type_byte][value...]. The 0x20 record
# is a 5-byte compound (a 0xB7 sub-marker plus a 4-byte schedule/timestamp field),
# and a 0xFF byte terminates the decodable stream (a trailing device timestamp follows).
_HTV145_TYPE_WIDTHS = {0xE1: 2, 0xDC: 1, 0xD8: 1, 0x20: 5, 0xB7: 4, 0xAD: 2, 0x9F: 4}


def decode_htv213frf_valve(raw: str) -> dict:
    """
    Decode HTV213FRF/HTV245FRF valve hub payload.

    These devices support two formats:
    1. Hex format (11#...) - flat [dp_id][type_byte][value...] stream; value
       length is inferred from the type byte (not a TLV with explicit length)
    2. ASCII format (1,-84,1;...) - uses comma-separated values
    """
    try:
        # Check payload format and route to appropriate decoder
        if raw.startswith("11#"):
            return _decode_htv213frf_hex(raw)
        elif "," in raw and (";" in raw or "|" in raw):
            return _decode_htv213frf_ascii(raw)
        else:
            raise ValueError(f"Unexpected payload format: {raw}")

    except Exception as e:
        _LOGGER.exception("HTV213FRF router error for payload %r", raw)
        return {
            "type": "valve_hub",
            "rssi_dbm": 0,
            "raw_bytes": [],
            "zones": {},
            "tlv_raw": {},
            "decoder": "htv213frf_error",
            "error": str(e),
        }


def _decode_htv213frf_ascii(raw: str) -> dict:
    """
    Decode HTV213FRF ASCII format payload.

    Format: 1,-84,1;0,149,0,0,0,0|0,6,0,0,0,0
    Structure: [flags],[rssi],[flags];[zone1_data]|[zone2_data]
    """
    from ..const import debug_with_version

    _LOGGER.info(debug_with_version("HTV213FRF ASCII payload: %s"), raw)

    zones = {}
    hub_online = False

    try:
        # Parse the ASCII format
        # Example: 1,-84,1;0,149,0,0,0,0|0,6,0,0,0,0

        # Split on semicolon to separate header from zone data
        if ";" not in raw:
            raise ValueError("Invalid ASCII format: missing semicolon")

        header_part, zone_part = raw.split(";", 1)

        # Parse header: 1,-84,1
        header_parts = header_part.split(",")
        if len(header_parts) < 3:
            raise ValueError("Invalid ASCII header format")

        _flags1 = int(header_parts[0])
        rssi_raw = int(header_parts[1])  # RSSI in dBm (negative number)
        _flags2 = int(header_parts[2])

        # Extract RSSI; positive values indicate a malformed payload.
        if rssi_raw >= 0:
            _LOGGER.warning("ASCII RSSI value %d is non-negative; expected negative dBm", rssi_raw)
        rssi_dbm = rssi_raw if rssi_raw < 0 else None

        # Parse zone data: 0,149,0,0,0,0|0,6,0,0,0,0
        zone_sections = zone_part.split("|")
        zone_mapping = {}
        sequential_zone = 1

        for zone_data in zone_sections:
            if not zone_data.strip():
                continue

            zone_parts = zone_data.split(",")
            if len(zone_parts) < 6:
                _LOGGER.warning("Invalid zone data format: %s", zone_data)
                continue

            # Parse zone data: [zone_id?, state, duration?, ?, ?, ?]
            # Based on observed patterns:
            # Zone 1: 0,149,0,0,0,0
            # Zone 2: 0,6,0,0,0,0

            zone_id_raw = int(zone_parts[0])
            state = int(zone_parts[1])
            duration = int(zone_parts[2]) if len(zone_parts) > 2 else 0

            # Map to sequential zone number
            zone_mapping[sequential_zone] = {
                "raw_zone_id": zone_id_raw,
                "open": state != 0x00,
                "duration_seconds": duration,
                "raw_ascii_data": zone_data,
            }

            _LOGGER.info(
                "HTV213FRF ASCII Zone %d (raw ID %d): state=%d, duration=%d", sequential_zone, zone_id_raw, state, duration
            )
            sequential_zone += 1

        zones = zone_mapping

        # For ASCII format, assume hub is online if we got valid data
        hub_online = True
        _LOGGER.info("HTV213FRF ASCII hub state: online (valid ASCII data received)")

        result = {
            "type": "valve_hub",
            "rssi_dbm": rssi_dbm,
            "raw_bytes": raw.encode("ascii"),
            "zones": zones,
            "tlv_raw": {},
            "hub_online": hub_online,
            "hub_state_raw": "ascii_format",
            "decoder": "htv213frf_ascii",
            "debug_info": {
                "payload_format": "ascii",
                "raw_payload": raw,
                "header_parts": header_parts,
                "zone_sections": zone_sections,
                "zones_found": len(zones),
                "rssi_raw": rssi_raw,
            },
        }

        _LOGGER.info(
            debug_with_version("HTV213FRF ASCII decoded: %d zones, hub_online=%s, rssi=%s"), len(zones), hub_online, rssi_dbm
        )
        return result

    except Exception:
        _LOGGER.exception("HTV213FRF ASCII decoder error for payload %r", raw)
        raise


def _extract_htv213_rssi(b: bytes) -> int | None:
    """Find the signed-dBm RSSI in an HTV213/245 hex (11#) frame, or None.

    The 10# frames put the 0xE1 header at offset 0, so _extract_rssi reads its
    RSSI from b[1]. The 11# frame prefixes every record with a dp_id, so the
    header appears as [dp_id 0x17][type 0xE1][signed dBm][phy] somewhere in the
    stream, not at a fixed offset (dp records can be reordered). Locate that
    record and return the signed dBm byte. Reading b[1] here would instead
    return the constant 0xE1 header byte (a bogus -31), which was the bug this
    replaces.

    The fourth byte is the PHY the reading was taken on, not padding. It used to
    be matched against 0x00 to stop a 0x17/0xE1 pair inside another record's
    value bytes being read as the header, which silently voided the RSSI on any
    frame reporting a non-zero PHY: a captured HTV210B frame carries 17e1b401,
    which the vendor app shows as -76 dBm at 1M PHY. The catalog declares this
    field two bytes wide on HTV213FRF and HTV405FRF but one byte on HTV245FRF
    and HTV345FRF, so the width cannot be trusted to disambiguate either.

    Two constraints replace it. The dBm byte must be negative, which is the one
    that carries meaning: a real RSSI is always negative and this family already
    discards non-negative readings. The PHY byte must be one of the values any
    capture has actually shown, 0x00 on the RF frames and 0x01 on the HTV210B.
    That keeps the collision this used to catch out (the false pair in a 0x9F
    value ends 0x42) while letting a real non-zero PHY through. If a capture ever
    shows a higher PHY, this bound is the thing to widen.
    """
    for i in range(len(b) - 3):
        if b[i] == 0x17 and b[i + 1] == 0xE1 and b[i + 2] >= 0x80 and b[i + 3] <= 0x01:
            return b[i + 2] - 256
    return None


def _extract_htv213_battery(b: bytes) -> tuple[int | None, int | None]:
    """Return (raw STA_BAT flag, battery percentage) for an HTV213/245 hex frame.

    Both may be None: no STA_BAT record yields no flag, and a flag no capture
    pairs with a charge level yields no percentage. The flag is returned
    alongside so this family reports it like every other decoder does; it is
    the only thing left to look at when the percentage cannot be derived.

    The frame's STA_BAT record is located structurally, so no tail marker is
    needed. What the previous version read as a [0xFE marker][battery:2] tail
    is a dp_id of 0xFE followed by the two-byte extended-type header of the
    trailing STA_REPTIME record, whose four-byte value is the "timestamp:4"
    that same comment described.
    """
    flag = _extract_battery_flag(b, dp_id_prefixed=True)
    return flag, _battery_flag_to_percent(flag)


def _scan_htv213_dp_map(b: bytes) -> dict[int, tuple[int, int]]:
    """Scan a flat dp_id/type/value byte stream into {dp_id: (type_byte, value_int)}.

    Unknown type bytes cause a 1-byte advance so parsing can re-align on the
    next potential DP record. A misaligned multi-byte-value skip can still
    bypass trailing records; re-alignment is best-effort only. Duplicate
    dp_ids are last-write-wins (intentional, not an oversight). Every
    multi-byte value is little-endian; see _parse_tlv_payload in utils.py for
    why that is now unconditional.
    """
    dp_map: dict[int, tuple[int, int]] = {}
    i = 0
    while i < len(b) - 2:  # need at least 3 bytes: dp_id + type_byte + 1 value byte
        dp_id = b[i]
        type_byte = b[i + 1]
        val_len = _HTV213_TYPE_LENGTHS.get(type_byte)
        if val_len is None:
            _LOGGER.debug(
                "HTV213FRF: unknown type byte 0x%02X at offset %d; advancing 1 byte for re-alignment",
                type_byte,
                i,
            )
            i += 1
        elif i + 2 + val_len > len(b):
            _LOGGER.debug(
                "HTV213FRF: truncated record for type 0x%02X at offset %d: need %d value bytes but have %d; advancing 1 byte",
                type_byte,
                i,
                val_len,
                len(b) - (i + 2),
            )
            i += 1
        else:
            val_bytes = b[i + 2 : i + 2 + val_len]
            dp_map[dp_id] = (type_byte, int.from_bytes(val_bytes, "little"))
            i += 2 + val_len
    return dp_map


def _extract_htv213_hub_state(dp_map: dict[int, tuple[int, int]], raw: str) -> tuple[bool, int | None]:
    """Pull (hub_online, hub_state_raw) from the dp_map.

    Hub online DP is 0x18 with type 0xDC enforced; value 0x01 means online.
    Some HTV345FRF payloads omit that DP but include zone 1 state at 0x19; in
    that case, the payload itself is evidence that the hub is online.
    """
    zone_1_present = 0x19 in dp_map
    if 0x18 not in dp_map:
        if zone_1_present:
            _LOGGER.debug(
                "HTV213FRF: hub online DP (0x18) absent from payload %r; using zone 1 DP (0x19) presence as online",
                raw,
            )
            return True, None
        _LOGGER.debug("HTV213FRF: hub online DP (0x18) and zone 1 DP (0x19) absent from payload %r", raw)
        return False, None

    hub_type, hub_state_raw = dp_map[0x18]
    if hub_type != 0xDC:
        if zone_1_present:
            _LOGGER.warning(
                "HTV213FRF: hub DP 0x18 has unexpected type 0x%02X (expected 0xDC); using zone 1 DP (0x19) presence as online",
                hub_type,
            )
            return True, hub_state_raw
        _LOGGER.warning(
            "HTV213FRF: hub DP 0x18 has unexpected type 0x%02X (expected 0xDC); zone 1 DP (0x19) absent",
            hub_type,
        )
        return False, hub_state_raw
    return hub_state_raw == 0x01, hub_state_raw


def _decode_packed_timestamp(value: int) -> str | None:
    """Decode a packed wall-clock stamp into an ISO string, or None if unusable.

    The 4-byte 0xB7 records hold a little-endian word whose bit fields are,
    from the top: year (offset from 2020), month, day, hour, minute, second.
    Two independent captures confirm the layout - one frame's trailing stamp
    decodes to the day that capture was taken, and a mid-run frame's zone
    event time is exactly that frame's report time plus the zone's remaining
    duration.

    The stamp carries no timezone and the device appears to report local wall
    time, so the returned string is deliberately naive: labelling it UTC would
    shift every reading by the viewer's offset. A zero value means "no event"
    and yields None, as does any word whose fields do not form a real date.
    """
    if not value:
        return None
    year = _TIMESTAMP_YEAR_BASE + ((value >> 26) & 0x3F)
    month = (value >> 22) & 0x0F
    day = (value >> 17) & 0x1F
    hour = (value >> 12) & 0x1F
    minute = (value >> 6) & 0x3F
    second = value & 0x3F
    try:
        return datetime(year, month, day, hour, minute, second).isoformat()
    except ValueError:
        _LOGGER.debug("HTV213FRF: packed timestamp 0x%08X is not a valid date", value)
        return None


def _extract_htv213_zones(dp_map: dict[int, tuple[int, int]]) -> dict[int, dict]:
    """Pull per-zone open state, duration, event time, and water usage from the dp_map.

    Zone states are DP 0x18+N with type 0xD8 only; other types on zone-range
    IDs are schedule/timer fields, not zone states. Zone durations are DP
    0x24+N with type 0xAD (2-byte seconds), event times are DP 0x20+N with
    type 0xB7 (4-byte packed stamp), and water usage is DP 0x28+N with type
    0x9F (4-byte raw count).

    Every one of those reads is guarded on its own type byte, so a record that
    is absent, or that belongs to a neighbouring dp_id block, leaves the field
    empty instead of contributing a plausible wrong number.
    """
    zones: dict[int, dict] = {}
    for zone_num in range(1, 9):
        state_dp = _HTV213_DP_BASE_STATE + zone_num
        if state_dp not in dp_map:
            continue
        state_type, state_val = dp_map[state_dp]
        if state_type != 0xD8:
            continue
        # Duration only populated for the documented 0xAD DP type; any other type
        # at this DP (or a missing DP) defaults to 0 rather than misinterpreting a
        # differently-typed value as seconds.
        duration_seconds = 0
        dur_entry = dp_map.get(_HTV213_DP_BASE_DURATION + zone_num)
        if dur_entry is not None and dur_entry[0] == 0xAD:
            duration_seconds = dur_entry[1]

        # On every frame captured so far this is the moment the zone's current
        # run ends (the frame's own report time plus the duration above), and
        # it reads zero for an idle zone. It is named for the vendor's own
        # STA_EVTIME identity rather than for that observed meaning, so a
        # firmware that later populates it while idle does not make the name a
        # lie.
        event_time = None
        event_entry = dp_map.get(_HTV213_DP_BASE_EVENT_TIME + zone_num)
        if event_entry is not None and event_entry[0] == 0xB7:
            event_time = _decode_packed_timestamp(event_entry[1])

        # Raw flow count for the zone's last completed run; it reads zero
        # while that zone is running. None (rather than 0) when the frame
        # carries no usable record, so "not reported" stays distinguishable
        # from "reported as none used".
        usage_counts = None
        usage_gallons = None
        usage_entry = dp_map.get(_HTV213_DP_BASE_USAGE + zone_num)
        if usage_entry is not None and usage_entry[0] == 0x9F:
            usage_counts = usage_entry[1]
            usage_gallons = round(usage_counts * _USAGE_GALLONS_PER_COUNT, 3)

        is_open = bool(state_val & 0x01)  # LSB: 1=open, 0=closed (device uses 0x21/0x20, not 0x01/0x00)
        zones[zone_num] = {
            "open": is_open,
            "duration_seconds": duration_seconds,
            "state_raw": state_val,
            "event_time": event_time,
            "last_usage_counts": usage_counts,
            "last_usage_gallons": usage_gallons,
        }
        _LOGGER.info(
            "HTV213FRF Zone %d: open=%s duration=%ds state_raw=0x%02X event_time=%s usage=%s counts",
            zone_num,
            is_open,
            duration_seconds,
            state_val,
            event_time,
            usage_counts,
        )
    return zones


def _decode_htv213frf_hex(raw: str) -> dict:
    """
    Decode HTV213FRF/HTV245FRF hex format payload (11# prefix).

    The payload is a flat sequence of [dp_id][type_byte][value_bytes...] records.
    The type byte determines value length:
      0xDC, 0xD8 → 1 byte   (hub state, zone open/close state)
      0x20, 0xAD → 2 bytes  (timer config, zone duration in seconds)
      0xB7, 0x9F → 4 bytes  (schedule/timer extended fields)

    Known DP IDs:
      0x18              → hub online state (type 0xDC enforced, value 0x01=online)
      0x18+N (1≤N≤8)   → zone N open state (type 0xD8, value 0x01=open, 0x00=closed)
      0x24+N (1≤N≤8)   → zone N duration in seconds (type 0xAD, 2-byte little-endian)
    """
    from ..const import debug_with_version

    try:
        b = _parse_rainpoint_payload(raw)
        _LOGGER.debug(debug_with_version("HTV213FRF hex raw bytes: %s"), b)

        dp_map = _scan_htv213_dp_map(b)
        hub_online, hub_state_raw = _extract_htv213_hub_state(dp_map, raw)
        zones = _extract_htv213_zones(dp_map)

        battery_flag, battery_percent = _extract_htv213_battery(b)

        _LOGGER.debug(
            debug_with_version("HTV213FRF hex decoded: %d zones, hub_online=%s, battery=%s (flag %s)"),
            len(zones),
            hub_online,
            battery_percent,
            battery_flag,
        )
        result = {
            "type": "valve_hub",
            "rssi_dbm": _extract_htv213_rssi(b),
            "raw_bytes": b,
            "zones": zones,
            "tlv_raw": {},
            "hub_online": hub_online,
            "hub_state_raw": hub_state_raw,
            "battery_flag": battery_flag,
            "decoder": "htv213frf_hex",
        }
        if battery_percent is not None:
            result["battery_percent"] = battery_percent
        _attach_report_time(result, b, dp_id_prefixed=True)
        return result

    except Exception:
        _LOGGER.exception("HTV213FRF hex decoder error for payload %r", raw)
        raise


def _scan_htv145_markers(b: bytes) -> dict[int, int]:
    """Scan the HTV145FRF [type_byte][value...] stream into {type_byte: value_int}.

    Value width comes from _HTV145_TYPE_WIDTHS; every multi-byte value is
    little-endian, as in the other framings. Parsing stops at the first 0xFF byte
    (stream terminator followed by a trailing device timestamp). Unknown type
    bytes advance 1 byte to attempt re-alignment. Duplicate type bytes are
    last-write-wins; the single-outlet payload carries one of each.
    """
    markers: dict[int, int] = {}
    i = 0
    while i < len(b):
        type_byte = b[i]
        if type_byte == 0xFF:
            break
        width = _HTV145_TYPE_WIDTHS.get(type_byte)
        if width is None:
            _LOGGER.debug(
                "HTV145FRF: unknown type byte 0x%02X at offset %d; advancing 1 byte for re-alignment",
                type_byte,
                i,
            )
            i += 1
            continue
        if i + 1 + width > len(b):
            _LOGGER.debug(
                "HTV145FRF: truncated record for type 0x%02X at offset %d; stopping",
                type_byte,
                i,
            )
            break
        val_bytes = b[i + 1 : i + 1 + width]
        markers[type_byte] = int.from_bytes(val_bytes, "little")
        i += 1 + width
    return markers


def decode_htv145frf(raw: str) -> dict:
    """
    Decode HTV145FRF single-outlet WiFi water timer payload (10# prefix).

    The payload is a flat [type_byte][value...] marker stream (not the HTV213FRF
    dp_id/type/value layout), so it needs its own scan. Known markers:
      0xDC (1 byte)  → hub online state (bit 0 set = online; HTV145 reports 0x01,
                       HTV113 reports 0x03, both online)
      0xD8 (1 byte)  → zone open state  (bit 0 set = open; device uses 0x21/0x20)
      0xAD (2 bytes) → zone run duration in seconds (little-endian)
      0x20 (5 bytes) → schedule/timestamp compound (captured but not interpreted)
      0x9F (4 bytes) → schedule/counter field (captured but not interpreted)
      0xE1 (2 bytes) → header field; byte[1] doubles as the signed-dBm RSSI

    This is a single-outlet timer, so the one 0xD8 marker maps to zone 1. Output
    shape matches the other valve decoders (type "valve_hub" with a zones dict) so
    valve.py and number.py consume it unchanged.
    """
    try:
        # This decoder only understands the flat 10# marker stream. A 11# TLV
        # payload would still parse, and its value bytes could coincide with
        # 0xDC/0xD8 markers, fabricating false hub-online or valve state -- so
        # reject anything that is not 10# before scanning.
        if not raw.startswith("10#"):
            raise ValueError("HTV145FRF payload must use the 10# format")
        b = _parse_rainpoint_payload(raw)
        markers = _scan_htv145_markers(b)

        hub_state_raw = markers.get(0xDC)
        # Bit 0 is the online flag. HTV145 reports 0x01 and HTV113 reports 0x03;
        # both are online, so an exact 0x01 match would wrongly mark the HTV113
        # valve entity unavailable (valve.py gates availability on hub_online).
        hub_online = hub_state_raw is not None and bool(hub_state_raw & 0x01)

        zones: dict[int, dict] = {}
        if 0xD8 in markers:
            state_val = markers[0xD8]
            zones[1] = {
                "open": bool(state_val & 0x01),
                "duration_seconds": markers.get(0xAD, 0),
                "state_raw": state_val,
            }
            _LOGGER.info(
                "HTV145FRF Zone 1: open=%s duration=%ds state_raw=0x%02X",
                zones[1]["open"],
                zones[1]["duration_seconds"],
                state_val,
            )

        return {
            "type": "valve_hub",
            "rssi_dbm": _extract_rssi(b) if len(b) > 1 else 0,
            "raw_bytes": b,
            "zones": zones,
            "tlv_raw": {},
            "hub_online": hub_online,
            "hub_state_raw": hub_state_raw,
            "decoder": "htv145frf_hex",
        }

    except Exception as e:
        _LOGGER.exception("HTV145FRF decoder error for payload %r", raw)
        return {
            "type": "valve_hub",
            "rssi_dbm": 0,
            "raw_bytes": [],
            "zones": {},
            "tlv_raw": {},
            "decoder": "htv145frf_error",
            "error": str(e),
        }


def decode_moisture_full(raw: str) -> dict:
    """
    Decode HCS021FRF (moisture + temp + lux).

    Supports two formats:
    1. Hex format (10#...) - standard TLV structure
    2. ASCII format (1,-73,1;694,70,G=292478) - comma-separated values
    """
    try:
        # Check payload format and route to appropriate decoder
        if raw.startswith("10#"):
            return _decode_moisture_full_hex(raw)
        elif "," in raw and (";" in raw or "=" in raw):
            return _decode_moisture_full_ascii(raw)
        else:
            raise ValueError(f"Unexpected payload format: {raw}")

    except Exception as e:
        _LOGGER.exception("HCS021FRF decoder error")
        return {"type": "moisture_full", "rssi_dbm": 0, "raw_bytes": [], "decoder": "hcs021frf_error", "error": str(e)}


def _decode_moisture_full_ascii(raw: str) -> dict:
    """
    Decode HCS021FRF ASCII format payload.

    Format: 1,-73,1;694,70,G=292478
    Structure: [flags],[rssi],[flags];[temp_raw],[moisture],[lux_data]
    """
    from ..const import debug_with_version

    _LOGGER.info(debug_with_version("HCS021FRF ASCII payload: %s"), raw)

    try:
        # Parse the ASCII format
        # Example: 1,-73,1;694,70,G=292478

        # Split on semicolon to separate header from sensor data
        if ";" not in raw:
            raise ValueError("Invalid ASCII format: missing semicolon")

        header_part, sensor_part = raw.split(";", 1)

        # Parse header: 1,-73,1
        header_parts = header_part.split(",")
        if len(header_parts) < 3:
            raise ValueError("Invalid ASCII header format")

        _flags1 = int(header_parts[0])
        rssi_raw = int(header_parts[1])  # RSSI in dBm (negative number)
        _flags2 = int(header_parts[2])

        # Extract RSSI; positive values indicate a malformed payload.
        if rssi_raw >= 0:
            _LOGGER.warning("ASCII RSSI value %d is non-negative; expected negative dBm", rssi_raw)
        rssi_dbm = rssi_raw if rssi_raw < 0 else None

        # Parse sensor data: 694,70,G=292478
        sensor_parts = sensor_part.split(",")

        if len(sensor_parts) < 3:
            raise ValueError("Invalid ASCII sensor data format")

        # Parse sensor values
        temp_raw = int(sensor_parts[0])  # Temperature raw value (Fahrenheit * 10)
        moisture = int(sensor_parts[1])  # Moisture percentage
        lux_data = sensor_parts[2]  # Lux data (may contain =)

        # Parse temperature - ASCII format provides Fahrenheit * 10
        # Example: 685 = 68.5°F
        temp_f = temp_raw / 10.0 if temp_raw else 0
        # Convert Fahrenheit to Celsius: (F - 32) * 5/9
        temp_c = (temp_f - 32) * 5 / 9

        # Parse lux data if it contains = (e.g., "G=292478")
        if "=" in lux_data:
            lux_parts = lux_data.split("=")
            if len(lux_parts) == 2:
                lux_raw = int(lux_parts[1])
                lux = lux_raw / 10.0  # Assuming similar scaling as hex format
            else:
                lux = 0
        else:
            # Try to parse as direct lux value
            try:
                lux = int(lux_data) / 10.0
            except ValueError:
                lux = 0

        result = {
            "type": "moisture_full",
            "rssi_dbm": rssi_dbm,
            "raw_bytes": raw.encode("ascii"),
            "moisture_percent": moisture,
            "temperature_c": temp_c,
            "temperature_f10": temp_raw,
            "illuminance_lux": lux,
            "illuminance_raw10": int(lux * 10) if lux else 0,
            "decoder": "hcs021frf_ascii",
            "debug_info": {
                "payload_format": "ascii",
                "raw_payload": raw,
                "header_parts": header_parts,
                "sensor_parts": sensor_parts,
                "rssi_raw": rssi_raw,
                "lux_data_parsed": lux_data,
            },
        }

        _LOGGER.info(
            debug_with_version("HCS021FRF ASCII decoded: temp=%.1f°C, moisture=%d%%, lux=%.1f, rssi=%s"),
            temp_c,
            moisture,
            lux,
            rssi_dbm,
        )
        return result

    except Exception:
        _LOGGER.exception("HCS021FRF ASCII decoder error")
        raise


def _decode_moisture_full_hex(raw: str) -> dict:
    """
    Decode HCS021FRF hex format payload.

    Layout after '10#':
    b0 = 0xE1
    b1 = RSSI (signed)
    b2 = 0x00
    b3 = 0xDC
    b4 = 0x01
    b5 = 0x85
    b6,b7 = temp_raw F*10 LE
    b8     = 0x88  (moisture tag)
    b9     = moisture %
    b10    = 0xC6  (lux tag)
    b11,b12= lux_raw * 10 LE
    b13    = 0x00
    b14..  = trailing STA_REPTIME record (0xFF 0x0F + 4-byte packed wall clock)

    Battery is the STA_BAT record at b3,b4, read structurally rather than at
    these offsets; the 0xFF 0x0F pair is that trailing record's extended-type
    header, not a battery word.

    Based on actual payload: 10#E1A200DC0185AB02881FC6600600FF0FFA28F718
    E1 A2 00 DC 01 85 AB 02 88 1F C6 60 06 00 FF 0F FA 28 F7 18
    b[1]=0xA2=162-256=-94 RSSI
    b[6:7]=0x02AB=683°F*10 → 68.3°F → 20.2°C
    b[9]=0x1F=31% moisture
    b[11:12]=0x0660=1632 lux*10 → 163.2 lux

    Note: Some payloads are 20 bytes instead of 16
    """
    # Handle both 16-byte and 20-byte payloads
    b = _validate_payload(raw, 16)  # Minimum 16 bytes
    if len(b) > 20:
        raise ValueError(f"HCS021FRF payload too long: {len(b)} bytes")

    _validate_tag(b, 5, 0x85, "HCS021FRF")

    rssi = _extract_rssi(b)
    temp_raw_f10 = _le16(b, 6)
    temp_c = _f10_to_c(temp_raw_f10)

    _validate_tag(b, 8, 0x88, "HCS021FRF")
    moisture = b[9]

    _validate_tag(b, 10, 0xC6, "HCS021FRF")
    lux_raw10 = _le16(b, 11)
    lux = lux_raw10 / 10.0

    battery_flag = _extract_battery_flag(b)

    result = _base_decoder_dict("moisture_full", rssi, b)
    result.update(
        {
            "moisture_percent": moisture,
            "temperature_c": temp_c,
            "temperature_f10": temp_raw_f10,
            "illuminance_lux": lux,
            "illuminance_raw10": lux_raw10,
            "battery_flag": battery_flag,
            "battery_percent": _battery_flag_to_percent(battery_flag),
            "decoder": "hcs021frf_hex",
        }
    )
    _attach_report_time(result, b)
    return result


def _parse_hws019_flags(flags_part: str) -> list[int]:
    """Parse the leading status-flags segment (e.g. '1,0,1') into a list of ints.

    Raises ValueError if any non-empty token is not a digit string, so malformed
    payloads surface to the caller's error path instead of producing a partial list.
    """
    flags: list[int] = []
    for raw_token in flags_part.split(","):
        token = raw_token.strip()
        if not token:
            continue
        if not token.isdigit():
            raise ValueError(f"invalid flag token {token!r} in flags segment {flags_part!r}")
        flags.append(int(token))
    return flags


# Matched with fullmatch against the whole reading, so the trailer must be the
# only bracketed group and nothing may follow it. Searching for the triple
# anywhere in the string would accept malformed readings such as
# '707(abc)(798/750/1)' or '707(798/750/1)junk'.
#
# Each field accepts an optional leading '-': temperature is reported in tenths
# of a degree Fahrenheit, so a daily minimum below 0F arrives as e.g.
# '20(50/-50/1)'. Matching only digits would drop the whole trailer for those
# readings.
_HWS019_STATS_RE = re.compile(r"[^()]*\((-?\d+)/(-?\d+)/(-?\d+)\)")

# Keys whose '(a/b/c)' trailer is NOT a day-max/day-min pair. The rain sensor's
# 'R=' field reuses the same syntax for cumulative totals per time window
# (e.g. 'R=4870(10/20/430)'), where a < b and the values do not bracket the
# current reading. Parsing those as max/min would silently invert them.
_HWS019_NON_MINMAX_KEYS = frozenset({"R"})


def _parse_hws019_stats(rest: str) -> dict[str, str] | None:
    """
    Extract the '(day-max/day-min/unknown)' trailer from a reading.

    The bracketed triple holds the running daily maximum and minimum for the
    reading, in the same units and scaling as the current value. The third
    field is 1 in every captured sample; its meaning is not established, so it
    is preserved verbatim rather than named.

    Returns None when the reading is not exactly one value followed by one
    complete trailer, so a malformed token contributes no stats rather than
    silently yielding a partial reading.
    """
    match = _HWS019_STATS_RE.fullmatch(rest.strip())
    if match is None:
        return None
    return {"max": match.group(1), "min": match.group(2), "unknown": match.group(3)}


def _apply_hws019_keyed_item(item: str, readings: dict, stats: dict) -> None:
    """Apply a 'KEY=VALUE(...)' style reading (e.g. 'P=9709(9709/9701/1)') to readings."""
    key, rest = item.split("=", 1)
    key = key.strip()
    if "(" in rest:
        readings[key] = rest.split("(")[0].strip()
        if key not in _HWS019_NON_MINMAX_KEYS:
            parsed = _parse_hws019_stats(rest)
            if parsed is not None:
                stats[key] = parsed
    else:
        readings[key] = rest.strip()


def _apply_hws019_positional_item(item: str, readings: dict, stats: dict) -> None:
    """Apply a positional 'CURRENT(...)' reading; first slot is temp, second is humidity."""
    current_value = item.split("(")[0].strip()
    if "temp" not in readings:
        key = "temp"
    elif "humidity" not in readings:
        key = "humidity"
    else:
        return
    readings[key] = current_value
    parsed = _parse_hws019_stats(item)
    if parsed is not None:
        stats[key] = parsed


def _parse_hws019_readings(readings_part: str) -> tuple[dict[str, str], dict[str, dict[str, str]]]:
    """
    Parse the readings segment (e.g. '707(...),42(...),P=9709(...)').

    Returns (readings, stats): current values keyed by reading name, and the
    per-reading daily max/min trailer for those readings that carry one.
    """
    readings: dict[str, str] = {}
    stats: dict[str, dict[str, str]] = {}
    for raw_item in readings_part.split(","):
        item = raw_item.strip()
        if not item:
            continue
        if "=" in item:
            _apply_hws019_keyed_item(item, readings, stats)
        elif "(" in item:
            _apply_hws019_positional_item(item, readings, stats)
    return readings, stats


def decode_hws019wrf_v2(raw: str) -> dict:
    """
    Decode HWS019WRF-V2 (Display Hub) CSV/semicolon payload.
    Example: '1,0,1;707(707/694/1),42(42/39/1),P=9709(9709/9701/1),'

    Format: current_value(day-max/day-min/unknown)
    - 707 = current temperature (70.7°F)
    - 42 = current humidity (42%)
    - P=9709 = current pressure (970.9 mb)

    The bracketed triple is the running daily max and min, in the same units as
    the current value. In the sample above the max equals the current value for
    all three readings, which is what a reading sitting at its daily high looks
    like, so that sample alone cannot distinguish max/min from a repeat of the
    current value.

    The ordering is fixed by captures where the pair straddles the current
    value. Those captures come from outside this repo, so they are cited here
    rather than left as an unexplained assertion:

      brettmeyerowitz/homeassistant-homgar
        tests/fixtures/payloads/HWS019WRF-V2.json
        '1,0,1;758(798/750/1),54(54/46/1),P=8569(8569/8540/1),'   750 < 758 < 798
      Remboooo/homgarapi
        homgarapi/devices.py
        '755(1020/588/1),54(91/24/1),'                            588 < 755 < 1020

    Across 28 hub and air-sensor samples from those sources, no value violates
    min <= current <= max and 7 straddle outright. This supersedes the earlier
    'current/min_or_max/count' reading, which was a deliberate hedge recorded
    when the ordering was still unverified.

    The third field is 1 in every sample seen so far and its meaning is
    unknown, so it is preserved verbatim rather than named.
    """
    _LOGGER.debug("decode_hws019wrf_v2 called with raw: %r", raw)
    try:
        parts = raw.split(";")
        if len(parts) < 2:
            raise ValueError(f"expected ';' separator between flags and readings in HWS019 payload: {raw!r}")
        flags = _parse_hws019_flags(parts[0])
        readings, reading_stats = _parse_hws019_readings(parts[1])
        result = {
            "type": "hws019wrf_v2",
            "flags": flags,
            "readings": readings,
            "reading_stats": reading_stats,
            "raw": raw,
        }
        _LOGGER.debug("decode_hws019wrf_v2 result: %r", result)
        return result
    except (ValueError, IndexError) as ex:
        _LOGGER.exception("Failed to decode HWS019WRF-V2 payload (raw: %r)", raw)
        return {"type": "hws019wrf_v2", "raw": raw, "error": str(ex)}


# DP IDs for valve hub zone state and duration (confirmed via payload capture).
# Zone N state DP   = _VALVE_HUB_DP_HUB_STATE + N (0x19 = zone 1, 0x1A = zone 2, ...)
# Zone N duration DP = _VALVE_HUB_DP_BASE_DURATION + N (0x25 = zone 1, 0x26 = zone 2, ...)
_VALVE_HUB_DP_HUB_STATE = 0x18
_VALVE_HUB_DP_BASE_DURATION = 0x24


def _format_valve_hub_tlv_log(tlv: dict) -> dict:
    """Format the valve hub TLV map for diagnostic logging."""
    return {
        f"0x{dp:02X}": (
            f"0x{type_byte:02X}",
            f"0x{value_int:02X}" if value_int < 256 else value_int,
            raw_bytes.hex(),
        )
        for dp, (type_byte, value_int, raw_bytes) in tlv.items()
    }


def _extract_valve_hub_state(tlv: dict) -> bool:
    """Return hub online flag derived from DP 0x18; logs at INFO when present."""
    from ..const import debug_with_version

    if _VALVE_HUB_DP_HUB_STATE not in tlv:
        return False
    _, hub_state_raw, _ = tlv[_VALVE_HUB_DP_HUB_STATE]
    hub_online = hub_state_raw == 0x01
    _LOGGER.info(debug_with_version("Valve hub state: %s (raw: 0x%02X)"), hub_online, hub_state_raw)
    return hub_online


def _extract_valve_hub_zone(zone_num: int, tlv: dict) -> dict | None:
    """Build a single zone dict from TLV, or None when no state DP is present."""
    state_dp = _VALVE_HUB_DP_HUB_STATE + zone_num
    if state_dp not in tlv:
        return None

    _, state_raw, _ = tlv[state_dp]
    zone_state = state_raw == 0x01

    duration_dp = _VALVE_HUB_DP_BASE_DURATION + zone_num
    duration_entry = tlv.get(duration_dp)
    # Duration appears to be in seconds (little-endian).
    zone_duration = duration_entry[1] if duration_entry is not None else 0
    duration_raw = duration_entry[1] if duration_entry is not None else None

    return {
        "open": zone_state,
        "duration_seconds": zone_duration,
        "state_raw": state_raw,
        "duration_raw": duration_raw,
    }


def _extract_valve_hub_zones(tlv: dict) -> dict:
    """Walk zones 1-8 and return the populated zone map."""
    zones: dict = {}
    for zone_num in range(1, 9):
        zone = _extract_valve_hub_zone(zone_num, tlv)
        if zone is not None:
            zones[zone_num] = zone
    return zones


def _valve_hub_error_result(error: str) -> dict:
    """Shape the error fallback dict returned when decoding fails."""
    return {
        "type": "valve_hub",
        "rssi_dbm": 0,
        "raw_bytes": [],
        "zones": {},
        "tlv_raw": {},
        "decoder": "valve_hub_error",
        "error": error,
    }


def decode_valve_hub(raw: str) -> dict:
    """
    Decode an irrigation valve hub TLV payload (e.g. HTV0540FRF).

    Confirmed DP map (derived from live payload capture):
    - Zone N state DP   = _VALVE_HUB_DP_HUB_STATE + N  (0x19 = zone 1, 0x1A = zone 2, ...)
    - Zone N duration DP = _VALVE_HUB_DP_BASE_DURATION + N (0x25 = zone 1, 0x26 = zone 2, ...)
    """
    from ..const import debug_with_version

    try:
        b = _parse_rainpoint_payload(raw)
        _LOGGER.debug(debug_with_version("Valve hub raw bytes: %s"), b)

        tlv = _parse_tlv_payload(raw)
        _LOGGER.debug(
            debug_with_version("Valve hub TLV entries: %s"),
            _format_valve_hub_tlv_log(tlv),
        )

        hub_online = _extract_valve_hub_state(tlv)
        zones = _extract_valve_hub_zones(tlv)

        result = {
            "type": "valve_hub",
            "rssi_dbm": _extract_rssi(b) if len(b) > 1 else 0,
            "raw_bytes": b,
            "zones": zones,
            "tlv_raw": tlv,
            "hub_online": hub_online,
            "hub_state_raw": tlv.get(_VALVE_HUB_DP_HUB_STATE, (None, None, None))[1],
            "decoder": "valve_hub_tlv",
        }

        _LOGGER.info(debug_with_version("Valve hub decoded: %d zones, hub_online=%s"), len(zones), hub_online)
        return result

    except Exception as e:
        _LOGGER.exception("Valve hub decoder error")
        return _valve_hub_error_result(str(e))


def decode_rain(raw: str) -> dict:
    """
    Decode HCS012ARF (rain gauge).
    Layout after '10#':
    b0 = 0xE1
    b1 = 0x00 (seems constant in your samples)
    b2 = 0x00
    b3,4 = FD,04 ; b5,b6 = lastHour raw*10 LE
    b7,8 = FD,05 ; b9,b10 = last24h raw*10 LE
    b11,12 = FD,06 ; b13,b14 = last7d raw*10 LE
    b15,16 = DC,01
    b17 = 0x97 ; b18,b19 = total raw*10 LE
    b20,b21 = 0x00,0x00
    b22..b27 = trailing STA_REPTIME record (0xFF 0x0F + 4-byte packed wall clock)

    Battery is the STA_BAT record at b15,b16, read structurally rather than at
    these offsets; the 0xFF 0x0F pair is that trailing record's extended-type
    header, not a battery word.

    Based on actual payload: 10#E10000FD040000FD054E07FD064E07DC01974E070000FF0F0410F718
    E1 00 00 FD 04 00 00 FD 05 4E 07 FD 06 4E 07 DC 01 97 4E 07 00 00 FF 0F 04 10 F7 18
    b[5:6]=0x0000=0.0mm last hour
    b[9:10]=0x074E=1870mm*10 → 187.0mm last 24h
    b[13:14]=0x074E=1870mm*10 → 187.0mm last 7d
    b[18:19]=0x074E=1870mm*10 → 187.0mm total
    """
    b = _validate_payload(raw, 24)

    # Validate rain-specific tags
    if not (b[3] == 0xFD and b[4] == 0x04):
        raise ValueError("HCS012ARF: Missing FD 04 at [3:5]")
    if not (b[7] == 0xFD and b[8] == 0x05):
        raise ValueError("HCS012ARF: Missing FD 05 at [7:9]")
    if not (b[11] == 0xFD and b[12] == 0x06):
        raise ValueError("HCS012ARF: Missing FD 06 at [11:13]")
    _validate_tag(b, 17, 0x97, "HCS012ARF")

    last_hour_raw10 = _le16(b, 5)
    last_24h_raw10 = _le16(b, 9)
    last_7d_raw10 = _le16(b, 13)
    total_raw10 = _le16(b, 18)

    battery_flag = _extract_battery_flag(b)

    result = _base_decoder_dict("rain", 0, b)  # Rain gauge doesn't have RSSI in standard position
    result.update(
        {
            "rain_last_hour_mm": last_hour_raw10 / 10.0,
            "rain_last_24h_mm": last_24h_raw10 / 10.0,
            "rain_last_7d_mm": last_7d_raw10 / 10.0,
            "rain_total_mm": total_raw10 / 10.0,
            "rain_last_hour_raw10": last_hour_raw10,
            "rain_last_24h_raw10": last_24h_raw10,
            "rain_last_7d_raw10": last_7d_raw10,
            "rain_total_raw10": total_raw10,
            "battery_flag": battery_flag,
            "battery_percent": _battery_flag_to_percent(battery_flag),
        }
    )
    _attach_report_time(result, b)
    return result


def decode_moisture_simple(raw: str) -> dict:
    """
    Decode HCS026FRF (moisture-only) payload.
    Layout after '10#':
    b0 = 0xE1
    b1 = RSSI (signed int8)
    b2 = 0x00
    b3 = 0xDC
    b4 = 0x01
    b5 = 0x88  (moisture tag)
    b6 = moisture % (0-100)
    b7.. = trailing STA_REPTIME record (0xFF 0x0F + 4-byte packed wall clock)

    Based on actual payload: 10#E1C600DC01881AFF0F5E21F718
    E1 C6 00 DC 01 88 1A FF 0F 5E 21 F7 18
    b[1]=0xC6=198-256=-58 RSSI
    b[4]=0x01 STA_BAT flag
    b[6]=0x1A=26% moisture

    The offsets above are the frame's record boundaries: this is the same
    self-describing encoding the 11# frames use, without the per-record dp_id.
    Battery and report time are read structurally rather than at these fixed
    offsets, so a firmware that reorders or omits a record cannot silently
    shift them onto another datapoint's bytes.
    """
    b = _validate_payload(raw, 9)
    _validate_tag(b, 5, 0x88, "HCS026FRF")

    rssi = _extract_rssi(b)
    moisture = b[6]
    battery_flag = _extract_battery_flag(b)

    result = _base_decoder_dict("moisture_simple", rssi, b)
    result.update(
        {
            "moisture_percent": moisture,
            "battery_flag": battery_flag,
            "battery_percent": _battery_flag_to_percent(battery_flag),
        }
    )
    _attach_report_time(result, b)
    return result


def decode_flow_meter(raw: str) -> dict:
    """Decode HCS008FRF (flow meter)."""
    from ..const import debug_with_version

    _LOGGER.debug(debug_with_version("Decoding HCS008FRF: %s"), raw)

    result = {
        "type": "flowmeter",
        "device_model": "HCS008FRF",
        "flowcurrentused": None,
        "flowcurrenduration": None,
        "flowtoday": None,
        "flowtotal": None,
        "flowbatt": None,
        "rssi": None,
        "decoder": "basic",
    }

    try:
        b = _parse_rainpoint_payload(raw)
        if b and len(b) > 1:
            result["rssi"] = _extract_rssi(b)

        # Basic flow parsing - can be enhanced with exact RainPoint logic later
        _LOGGER.debug(debug_with_version("HCS008FRF basic parsing completed"))

    except Exception:
        _LOGGER.exception(debug_with_version("Error in HCS008FRF decoder"))

    return result


# Alias for backward compatibility
decode_flowmeter = decode_flow_meter


def decode_pool_plus(raw: str) -> dict:
    """Decode HCS0530THO (pool plus with CO2)."""
    from ..const import debug_with_version

    _LOGGER.debug(debug_with_version("Decoding HCS0530THO: %s"), raw)

    result = {
        "type": "co2",
        "device_model": "HCS0530THO",
        "co2": None,
        "temperature_c": None,
        "humidity_percent": None,
        "rssi": None,
        "decoder": "basic",
    }

    try:
        b = _parse_rainpoint_payload(raw)
        if b and len(b) > 1:
            result["rssi"] = _extract_rssi(b)

        # Basic CO2 parsing - can be enhanced with exact RainPoint logic later
        _LOGGER.debug(debug_with_version("HCS0530THO basic parsing completed"))

    except Exception:
        _LOGGER.exception(debug_with_version("Error in HCS0530THO decoder"))

    return result


def decode_soil(raw: str) -> dict:
    """Decode soil sensor."""
    from ..const import debug_with_version

    _LOGGER.debug(debug_with_version("Decoding soil sensor: %s"), raw)

    result = {
        "type": "soil",
        "rssi": None,
        "decoder": "basic",
    }

    try:
        b = _parse_rainpoint_payload(raw)
        if b and len(b) > 1:
            result["rssi"] = _extract_rssi(b)
            result["raw_bytes"] = b

    except Exception:
        _LOGGER.exception(debug_with_version("Error in soil decoder"))

    return result


def decode_temp_hum(raw: str) -> dict:
    """Decode temperature/humidity sensor."""
    from ..const import debug_with_version

    _LOGGER.debug(debug_with_version("Decoding temp/hum sensor: %s"), raw)

    result = {
        "type": "temphum",
        "rssi": None,
        "decoder": "basic",
    }

    try:
        b = _parse_rainpoint_payload(raw)
        if b and len(b) > 1:
            result["rssi"] = _extract_rssi(b)
            result["raw_bytes"] = b

    except Exception:
        _LOGGER.exception(debug_with_version("Error in temp/hum decoder"))

    return result


def decode_temp_hum_full(raw: str) -> dict:
    """Decode full temperature/humidity sensor."""
    from ..const import debug_with_version

    _LOGGER.debug(debug_with_version("Decoding full temp/hum sensor: %s"), raw)

    result = {
        "type": "temphum_full",
        "rssi": None,
        "decoder": "basic",
    }

    try:
        b = _parse_rainpoint_payload(raw)
        if b and len(b) > 1:
            result["rssi"] = _extract_rssi(b)
            result["raw_bytes"] = b

    except Exception:
        _LOGGER.exception(debug_with_version("Error in full temp/hum decoder"))

    return result


def decode_co2(raw: str) -> dict:
    """Decode CO2 sensor."""
    from ..const import debug_with_version

    _LOGGER.debug(debug_with_version("Decoding CO2 sensor: %s"), raw)

    result = {
        "type": "co2",
        "rssi": None,
        "decoder": "basic",
    }

    try:
        b = _parse_rainpoint_payload(raw)
        if b and len(b) > 1:
            result["rssi"] = _extract_rssi(b)
            result["raw_bytes"] = b

    except Exception:
        _LOGGER.exception(debug_with_version("Error in CO2 decoder"))

    return result


def decode_display(raw: str) -> dict:
    """Decode display sensor."""
    from ..const import debug_with_version

    _LOGGER.debug(debug_with_version("Decoding display sensor: %s"), raw)

    result = {
        "type": "display",
        "rssi": None,
        "decoder": "basic",
    }

    try:
        b = _parse_rainpoint_payload(raw)
        if b and len(b) > 1:
            result["rssi"] = _extract_rssi(b)
            result["raw_bytes"] = b

    except Exception:
        _LOGGER.exception(debug_with_version("Error in display decoder"))

    return result


def decode_unknown(raw: str) -> dict:
    """Decode unknown device."""
    from ..const import debug_with_version

    _LOGGER.debug(debug_with_version("Decoding unknown device: %s"), raw)

    result = {
        "type": "unknown",
        "rssi": None,
        "decoder": "basic",
    }

    try:
        b = _parse_rainpoint_payload(raw)
        if b and len(b) > 1:
            result["rssi"] = _extract_rssi(b)
            result["raw_bytes"] = b

    except Exception:
        _LOGGER.exception(debug_with_version("Error in unknown decoder"))

    return result


# Additional HCS decoders - basic implementations
def decode_temphum(raw: str) -> dict:
    """Decode HCS014ARF (temperature/humidity) payload."""
    from ..const import debug_with_version

    _LOGGER.debug(debug_with_version("Decoding HCS014ARF: %s"), raw)

    result = {
        "type": "temphum",
        "rssi": None,
        "decoder": "basic",
    }

    try:
        b = _parse_rainpoint_payload(raw)
        if b and len(b) > 1:
            result["rssi"] = _extract_rssi(b)
            result["raw_bytes"] = b

    except Exception:
        _LOGGER.exception(debug_with_version("Error in HCS014ARF decoder"))

    return result


def decode_pool(raw: str) -> dict:
    """Decode HCS0528ARF (pool/temperature) payload."""
    from ..const import debug_with_version

    _LOGGER.debug(debug_with_version("Decoding HCS0528ARF: %s"), raw)

    result = {
        "type": "pool",
        "rssi": None,
        "decoder": "basic",
    }

    try:
        b = _parse_rainpoint_payload(raw)
        if b and len(b) > 1:
            result["rssi"] = _extract_rssi(b)
            result["raw_bytes"] = b

    except Exception:
        _LOGGER.exception(debug_with_version("Error in HCS0528ARF decoder"))

    return result


# HCS variant decoders - basic implementations
def decode_hcs005frf(raw: str) -> dict:
    """Decode HCS005FRF (moisture-only sensor)."""
    return decode_moisture_simple(raw)  # pragma: no cover - stub passthrough - decode_moisture_simple covered separately


def decode_hcs024frf_v1(raw: str) -> dict:
    """Decode HCS024FRF-V1 (multi-sensor)."""
    return decode_moisture_full(raw)  # pragma: no cover - stub passthrough - decode_moisture_full covered separately


def decode_hcs014arf(raw: str) -> dict:
    """Decode HCS014ARF (Temperature/Humidity)."""
    return decode_temphum(raw)  # pragma: no cover - stub passthrough - decode_temphum covered separately


def decode_hcs015arf(raw: str) -> dict:
    """Decode HCS015ARF (pool temperature sensor)."""
    return decode_pool(raw)  # pragma: no cover - stub passthrough - decode_pool covered separately


def decode_hcs0528arf(raw: str) -> dict:
    """Decode HCS0528ARF (pool temperature sensor)."""
    return decode_pool(raw)  # pragma: no cover - stub passthrough - decode_pool covered separately


# Additional HCS variant decoders - placeholder implementations
