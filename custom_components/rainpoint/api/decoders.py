"""
Decoder functions for RainPoint API.

This module contains all device-specific decoder functions for different
RainPoint device types.
"""

import logging

from .utils import _base_decoder_dict, _f10_to_c, _le16, _parse_rainpoint_payload, _parse_tlv_payload
from .validators import _battery_status_to_percent, _extract_rssi, _extract_status_code, _validate_payload, _validate_tag

_LOGGER = logging.getLogger(__name__)

# Type byte → value byte count for HTV213FRF/HTV245FRF.
# Subset of types relevant to these models; see _TYPE_WIDTHS in utils.py for the full set.
_HTV213_TYPE_LENGTHS = {0xDC: 1, 0xD8: 1, 0x20: 2, 0xAD: 2, 0xB7: 4, 0x9F: 4}


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
        _LOGGER.error("HTV213FRF router error for payload %r: %s", raw, e, exc_info=True)
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

    except Exception as e:
        _LOGGER.error("HTV213FRF ASCII decoder error for payload %r: %s", raw, e, exc_info=True)
        raise


def _scan_htv213_dp_map(b: bytes) -> dict[int, tuple[int, int]]:
    """Scan a flat dp_id/type/value byte stream into {dp_id: (type_byte, value_int)}.

    Unknown type bytes cause a 1-byte advance so parsing can re-align on the
    next potential DP record. A misaligned multi-byte-value skip can still
    bypass trailing records; re-alignment is best-effort only. Duplicate
    dp_ids are last-write-wins (intentional, not an oversight). Endianness
    depends on type (0xAD=LE, others=BE).
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
            endian = "little" if type_byte == 0xAD else "big"
            dp_map[dp_id] = (type_byte, int.from_bytes(val_bytes, endian))
            i += 2 + val_len
    return dp_map


def _extract_htv213_hub_state(dp_map: dict[int, tuple[int, int]], raw: str) -> tuple[bool, int | None]:
    """Pull (hub_online, hub_state_raw) from the dp_map.

    Hub online DP is 0x18 with type 0xDC enforced; value 0x01 means online.
    """
    if 0x18 not in dp_map:
        _LOGGER.warning(
            "HTV213FRF: hub online DP (0x18) absent from payload %r; defaulting hub_online=False",
            raw,
        )
        return False, None

    hub_type, hub_state_raw = dp_map[0x18]
    if hub_type != 0xDC:
        _LOGGER.warning(
            "HTV213FRF: hub DP 0x18 has unexpected type 0x%02X (expected 0xDC); ignoring hub state",
            hub_type,
        )
        return False, hub_state_raw
    return hub_state_raw == 0x01, hub_state_raw


def _extract_htv213_zones(dp_map: dict[int, tuple[int, int]]) -> dict[int, dict]:
    """Pull per-zone open state and duration from the dp_map.

    Zone states are DP 0x18+N with type 0xD8 only; other types on zone-range
    IDs are schedule/timer fields, not zone states. Zone durations are DP
    0x24+N with type 0xAD (2-byte little-endian seconds).
    """
    zones: dict[int, dict] = {}
    for zone_num in range(1, 9):
        state_dp = 0x18 + zone_num
        dur_dp = 0x24 + zone_num
        if state_dp not in dp_map:
            continue
        state_type, state_val = dp_map[state_dp]
        if state_type != 0xD8:
            continue
        # Duration only populated for the documented 0xAD DP type; any other type
        # at this DP (or a missing DP) defaults to 0 rather than misinterpreting a
        # differently-typed value as seconds.
        duration_seconds = 0
        dur_entry = dp_map.get(dur_dp)
        if dur_entry is not None and dur_entry[0] == 0xAD:
            duration_seconds = dur_entry[1]
        is_open = bool(state_val & 0x01)  # LSB: 1=open, 0=closed (device uses 0x21/0x20, not 0x01/0x00)
        zones[zone_num] = {
            "open": is_open,
            "duration_seconds": duration_seconds,
            "state_raw": state_val,
        }
        _LOGGER.info(
            "HTV213FRF Zone %d: open=%s duration=%ds state_raw=0x%02X",
            zone_num,
            is_open,
            duration_seconds,
            state_val,
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

        _LOGGER.debug(
            debug_with_version("HTV213FRF hex decoded: %d zones, hub_online=%s"),
            len(zones),
            hub_online,
        )
        return {
            "type": "valve_hub",
            "rssi_dbm": _extract_rssi(b) if len(b) > 1 else 0,
            "raw_bytes": b,
            "zones": zones,
            "tlv_raw": {},
            "hub_online": hub_online,
            "hub_state_raw": hub_state_raw,
            "decoder": "htv213frf_hex",
        }

    except Exception as e:
        _LOGGER.error("HTV213FRF hex decoder error for payload %r: %s", raw, e, exc_info=True)
        raise


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
        _LOGGER.error("HCS021FRF decoder error: %s", e)
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

    except Exception as e:
        _LOGGER.error("HCS021FRF ASCII decoder error: %s", e)
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
    b14,b15= 0xFF,0x0F (status/battery)

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

    status_code = _extract_status_code(b, 14, 15)

    result = _base_decoder_dict("moisture_full", rssi, b)
    result.update(
        {
            "moisture_percent": moisture,
            "temperature_c": temp_c,
            "temperature_f10": temp_raw_f10,
            "illuminance_lux": lux,
            "illuminance_raw10": lux_raw10,
            "battery_status_code": status_code,
            "battery_percent": _battery_status_to_percent(status_code),
            "decoder": "hcs021frf_hex",
        }
    )
    return result


def decode_hws019wrf_v2(raw: str) -> dict:
    """
    Decode HWS019WRF-V2 (Display Hub) CSV/semicolon payload.
    Example: '1,0,1;707(707/694/1),42(42/39/1),P=9709(9709/9701/1),'

    Format: current_value(current/min_or_max/count)
    - 707 = current temperature (70.7°F)
    - 42 = current humidity (42%)
    - P=9709 = current pressure (970.9 mb)
    """
    _LOGGER.debug("decode_hws019wrf_v2 called with raw: %r", raw)
    try:
        parts = raw.split(";")
        # First part: status flags (e.g., '1,0,1')
        flags = [int(x) for x in parts[0].split(",") if x.strip().isdigit()]
        readings = {}
        if len(parts) > 1:
            for item in parts[1].split(","):
                item = item.strip()
                if not item:
                    continue
                if "=" in item:
                    # Pressure format: P=9709(9709/9701/1)
                    key, rest = item.split("=", 1)
                    key = key.strip()
                    if "(" in rest:
                        readings[key] = rest.split("(")[0].strip()
                    else:
                        readings[key] = rest.strip()
                elif "(" in item:
                    # Temperature/Humidity: 707(707/694/1) — extract current value
                    current_value = item.split("(")[0].strip()
                    if "temp" not in readings:
                        readings["temp"] = current_value
                    elif "humidity" not in readings:
                        readings["humidity"] = current_value
        result = {
            "type": "hws019wrf_v2",
            "flags": flags,
            "readings": readings,
            "raw": raw,
        }
        _LOGGER.debug("decode_hws019wrf_v2 result: %r", result)
        return result
    except (ValueError, IndexError) as ex:
        _LOGGER.error("Failed to decode HWS019WRF-V2 payload: %s (raw: %r)", ex, raw)
        return {"type": "hws019wrf_v2", "raw": raw, "error": str(ex)}


def decode_valve_hub(raw: str) -> dict:
    """
    Decode an irrigation valve hub TLV payload (e.g. HTV0540FRF).

    Confirmed DP map (derived from live payload capture):
    - Zone N state DP   = _DP_HUB_STATE + N  (0x19 = zone 1, 0x1A = zone 2, ...)
    - Zone N duration DP = _DP_BASE_DURATION + N (0x25 = zone 1, 0x26 = zone 2, ...)
    """
    from ..const import debug_with_version

    # DP IDs for valve hub zone state and duration (confirmed via payload capture)
    _DP_HUB_STATE = 0x18
    _DP_BASE_DURATION = 0x24  # zone N duration DP = 0x24 + N

    try:
        b = _parse_rainpoint_payload(raw)
        _LOGGER.debug(debug_with_version("Valve hub raw bytes: %s"), b)

        tlv = _parse_tlv_payload(raw)
        _LOGGER.debug(
            debug_with_version("Valve hub TLV entries: %s"),
            {
                f"0x{dp:02X}": (f"0x{type_byte:02X}", f"0x{value_int:02X}" if value_int < 256 else value_int, raw_bytes.hex())
                for dp, (type_byte, value_int, raw_bytes) in tlv.items()
            },
        )

        zones = {}
        hub_online = False

        # Extract hub online state from DP 0x18
        if _DP_HUB_STATE in tlv:
            _, hub_state_raw, _ = tlv[_DP_HUB_STATE]
            hub_online = hub_state_raw == 0x01
            _LOGGER.info(debug_with_version("Valve hub state: %s (raw: 0x%02X)"), hub_online, hub_state_raw)

        # Extract zone states and durations
        for zone_num in range(1, 9):  # Support up to 8 zones
            state_dp = _DP_HUB_STATE + zone_num
            duration_dp = _DP_BASE_DURATION + zone_num

            zone_state = None
            zone_duration = 0

            if state_dp in tlv:
                _, state_raw, _ = tlv[state_dp]
                zone_state = state_raw == 0x01

            if duration_dp in tlv:
                _, duration_raw, _ = tlv[duration_dp]
                # Duration appears to be in seconds (little-endian)
                zone_duration = duration_raw

            if zone_state is not None:
                zones[zone_num] = {
                    "open": zone_state,
                    "duration_seconds": zone_duration,
                    "state_raw": tlv[state_dp][1] if state_dp in tlv else None,
                    "duration_raw": tlv[duration_dp][1] if duration_dp in tlv else None,
                }

        result = {
            "type": "valve_hub",
            "rssi_dbm": _extract_rssi(b) if len(b) > 1 else 0,
            "raw_bytes": b,
            "zones": zones,
            "tlv_raw": tlv,
            "hub_online": hub_online,
            "hub_state_raw": tlv.get(_DP_HUB_STATE, (None, None, None))[1],
            "decoder": "valve_hub_tlv",
        }

        _LOGGER.info(debug_with_version("Valve hub decoded: %d zones, hub_online=%s"), len(zones), hub_online)
        return result

    except Exception as e:
        _LOGGER.error("Valve hub decoder error: %s", e)
        return {
            "type": "valve_hub",
            "rssi_dbm": 0,
            "raw_bytes": [],
            "zones": {},
            "tlv_raw": {},
            "decoder": "valve_hub_error",
            "error": str(e),
        }


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
    b22,b23 = 0xFF,0x0F (status/battery)
    b24..b27 = tail

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

    status_code = _extract_status_code(b, 22, 23)

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
            "battery_status_code": status_code,
            "battery_percent": _battery_status_to_percent(status_code),
        }
    )
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
    b7,b8 = status/battery field

    Based on actual payload: 10#E1C600DC01881AFF0F5E21F718
    E1 C6 00 DC 01 88 1A FF 0F 5E 21 F7 18
    b[1]=0xC6=198-256=-58 RSSI
    b[6]=0x1A=26% moisture
    """
    b = _validate_payload(raw, 9)
    _validate_tag(b, 5, 0x88, "HCS026FRF")

    rssi = _extract_rssi(b)
    moisture = b[6]
    status_code = _extract_status_code(b, 7, 8)

    result = _base_decoder_dict("moisture_simple", rssi, b)
    result.update(
        {
            "moisture_percent": moisture,
            "battery_status_code": status_code,
            "battery_percent": _battery_status_to_percent(status_code),
        }
    )
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

    except Exception as e:
        _LOGGER.error(debug_with_version("Error in HCS008FRF decoder: %s"), e)

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

    except Exception as e:
        _LOGGER.error(debug_with_version("Error in HCS0530THO decoder: %s"), e)

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

    except Exception as e:
        _LOGGER.error(debug_with_version("Error in soil decoder: %s"), e)

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

    except Exception as e:
        _LOGGER.error(debug_with_version("Error in temp/hum decoder: %s"), e)

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

    except Exception as e:
        _LOGGER.error(debug_with_version("Error in full temp/hum decoder: %s"), e)

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

    except Exception as e:
        _LOGGER.error(debug_with_version("Error in CO2 decoder: %s"), e)

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

    except Exception as e:
        _LOGGER.error(debug_with_version("Error in display decoder: %s"), e)

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

    except Exception as e:
        _LOGGER.error(debug_with_version("Error in unknown decoder: %s"), e)

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

    except Exception as e:
        _LOGGER.error(debug_with_version("Error in HCS014ARF decoder: %s"), e)

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

    except Exception as e:
        _LOGGER.error(debug_with_version("Error in HCS0528ARF decoder: %s"), e)

    return result


# HCS variant decoders - basic implementations
def decode_hcs005frf(raw: str) -> dict:
    """Decode HCS005FRF (moisture-only sensor)."""
    return decode_moisture_simple(raw)  # pragma: no cover - stub passthrough - decode_moisture_simple covered separately


def decode_hcs003frf(raw: str) -> dict:
    """Decode HCS003FRF (moisture-only sensor)."""
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
def decode_hcs027arf(raw: str) -> dict:
    """Decode HCS027ARF (unknown sensor type)."""
    return decode_unknown(raw)  # pragma: no cover - stub passthrough - decode_unknown covered separately


def decode_hcs016arf(raw: str) -> dict:
    """Decode HCS016ARF (unknown sensor type)."""
    return decode_unknown(raw)  # pragma: no cover - stub passthrough - decode_unknown covered separately


def decode_hcs044frf(raw: str) -> dict:
    """Decode HCS044FRF (unknown sensor type)."""
    return decode_unknown(raw)  # pragma: no cover - stub passthrough - decode_unknown covered separately


def decode_hcs666frf(raw: str) -> dict:
    """Decode HCS666FRF (unknown sensor variant)."""
    return decode_unknown(raw)  # pragma: no cover - stub passthrough - decode_unknown covered separately


def decode_hcs666rfr_p(raw: str) -> dict:
    """Decode HCS666RFR-P (unknown sensor variant)."""
    return decode_unknown(raw)  # pragma: no cover - stub passthrough - decode_unknown covered separately


def decode_hcs999frf(raw: str) -> dict:
    """Decode HCS999FRF (unknown sensor variant)."""
    return decode_unknown(raw)  # pragma: no cover - stub passthrough - decode_unknown covered separately


def decode_hcs999frf_p(raw: str) -> dict:
    """Decode HCS999FRF-P (unknown sensor variant)."""
    return decode_unknown(raw)  # pragma: no cover - stub passthrough - decode_unknown covered separately


def decode_hcs666frf_x(raw: str) -> dict:
    """Decode HCS666FRF-X (unknown sensor variant)."""
    return decode_unknown(raw)  # pragma: no cover - stub passthrough - decode_unknown covered separately


def decode_hcs701b(raw: str) -> dict:
    """Decode HCS701B (unknown sensor type)."""
    return decode_unknown(raw)  # pragma: no cover - stub passthrough - decode_unknown covered separately


def decode_hcs596wb(raw: str) -> dict:
    """Decode HCS596WB (unknown sensor type)."""
    return decode_unknown(raw)  # pragma: no cover - stub passthrough - decode_unknown covered separately


def decode_hcs596wb_v4(raw: str) -> dict:
    """Decode HCS596WB-V4 (unknown sensor type)."""
    return decode_unknown(raw)  # pragma: no cover - stub passthrough - decode_unknown covered separately


def decode_hcs706arf(raw: str) -> dict:
    """Decode HCS706ARF (unknown sensor type)."""
    return decode_unknown(raw)  # pragma: no cover - stub passthrough - decode_unknown covered separately


def decode_hcs802arf(raw: str) -> dict:
    """Decode HCS802ARF (unknown sensor type)."""
    return decode_unknown(raw)  # pragma: no cover - stub passthrough - decode_unknown covered separately


def decode_hcs048b(raw: str) -> dict:
    """Decode HCS048B (unknown sensor type)."""
    return decode_unknown(raw)  # pragma: no cover - stub passthrough - decode_unknown covered separately


def decode_hcs888arf_v1(raw: str) -> dict:
    """Decode HCS888ARF-V1 (unknown sensor type)."""
    return decode_unknown(raw)  # pragma: no cover - stub passthrough - decode_unknown covered separately


def decode_hcs0600arf(raw: str) -> dict:
    """Decode HCS0600ARF (unknown sensor type)."""
    return decode_unknown(raw)  # pragma: no cover - stub passthrough - decode_unknown covered separately
