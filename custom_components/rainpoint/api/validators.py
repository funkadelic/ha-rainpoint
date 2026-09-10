"""
Validation functions for RainPoint API.

This module contains functions for validating payloads, extracting data,
and performing common validation operations.
"""

import logging

from .utils import STA_BAT_FIELD, _find_field_value

_LOGGER = logging.getLogger(__name__)


def _validate_payload(raw: str, expected_length: int) -> bytes:
    """Validate and parse a hex payload."""
    if "#" not in raw:
        raise ValueError("Payload missing '#' separator")

    prefix, hex_data = raw.split("#", 1)

    if prefix != "10":
        raise ValueError(f"Expected prefix '10', got '{prefix}'")

    b = bytes.fromhex(hex_data)

    if len(b) < expected_length:
        raise ValueError(f"Expected at least {expected_length} bytes, got {len(b)}")

    # Allow payloads longer than expected (some devices send extra data)
    if len(b) > expected_length * 2:  # Reasonable upper limit
        raise ValueError(f"Payload too long: expected max {expected_length * 2} bytes, got {len(b)}")

    return b


def _validate_tag(b: bytes, offset: int, expected: int, device_name: str) -> None:
    """Raise ValueError naming the device when b[offset] is not `expected`."""
    actual = b[offset]
    if actual != expected:
        raise ValueError(f"{device_name}: Expected tag 0x{expected:02X} at offset {offset}, got 0x{actual:02X}")


def _extract_rssi(b: bytes) -> int:
    """Extract RSSI from payload bytes."""
    rssi_raw = b[1]
    return rssi_raw if rssi_raw < 128 else rssi_raw - 256


def _extract_battery_flag(b: bytes, *, dp_id_prefixed: bool = False) -> int | None:
    """Return the raw STA_BAT value of a frame, or None when it carries none.

    STA_BAT is a single byte located structurally, not by offset. The previous
    extraction read a two-byte word at a fixed offset and mapped it through a
    0x0FF6..0x0FFF ladder; those words are the extended-type header of whatever
    datapoint ends the frame (0x0FFF is STA_REPTIME, 0x0FFD is STA_EVTIME2, and
    so on down to 0x0FF6), so the ladder tracked the trailing datapoint's type
    rather than charge, and every captured frame we hold decoded as 100%.
    """
    value_bytes = _find_field_value(b, STA_BAT_FIELD, dp_id_prefixed=dp_id_prefixed)
    if not value_bytes:
        return None
    return value_bytes[0] & 0xFF


# No capture pairs a flag with a charge level, so no flag earns a percentage:
# a wrong number here is indistinguishable from a real one downstream.
_BATTERY_FLAG_NORMAL = {0, 1}

# STA_BAT 2 is the low-battery condition. Over 2026-09-06 to 2026-09-09 on a
# live HTV245FRF, all 17 of the cloud's own low-battery events (event/list
# code 143) landed on a poll reading 2 and none on a poll reading 1. Both
# come from the same device report, so this labels the flag rather than
# corroborating it twice. Every other value stays unmapped, 3 included: one
# HTV113FRF frame reports it and nothing says where it sits on the scale.
_BATTERY_FLAG_LOW = 2


def _battery_flag_to_percent(flag: int | None) -> int | None:
    """Map a raw STA_BAT flag to a percentage, or None when unproven."""
    if flag is None:
        return None
    return 100 if flag in _BATTERY_FLAG_NORMAL else None


def _battery_flag_is_low(flag: int | None) -> bool | None:
    """Map a raw STA_BAT flag to low/normal, or None when unproven."""
    if flag in _BATTERY_FLAG_NORMAL:
        return False
    return True if flag == _BATTERY_FLAG_LOW else None
