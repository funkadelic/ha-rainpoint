"""
Utility functions for HomGar API.

This module contains helper functions for payload parsing, data conversion,
and common operations used across the API.
"""

import logging

_LOGGER = logging.getLogger(__name__)


def _parse_homgar_payload(raw: str) -> bytes:
    """Parse a HomGar hex payload and return bytes."""
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
        0xD8: 1,   # zone state
        0xDC: 1,   # hub state
        0xAD: 2,   # zone duration (seconds, little-endian)
        0x20: 2,   # timer/schedule config
        0xE1: 2,
        0xB7: 4,   # schedule/timer extended
        0x9F: 4,   # schedule/timer extended
        0xC4: 1,
        0xC5: 1,
        0xC6: 1,
    }

    b = _parse_homgar_payload(raw)
    tlv = {}
    i = 0

    while i < len(b):
        if i + 1 >= len(b):
            break

        dp_id = b[i]
        type_byte = b[i + 1]

        width = _TYPE_WIDTHS.get(type_byte)
        if width is None:
            # Unknown type — advance 1 byte to attempt re-sync
            i += 1
            continue

        if i + 2 + width > len(b):
            break

        raw_bytes = bytes(b[i + 2 : i + 2 + width])
        # Duration DPs (0xAD type) are little-endian; all others big-endian
        if width > 0:
            endian = "little" if type_byte == 0xAD else "big"
            value_int = int.from_bytes(raw_bytes, endian)
        else:
            value_int = None
        tlv[dp_id] = (type_byte, value_int, raw_bytes)
        i += 2 + width

    return tlv


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
