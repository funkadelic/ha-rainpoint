"""
Validation functions for RainPoint API.

This module contains functions for validating payloads, extracting data,
and performing common validation operations.
"""

import logging

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
    """Validate a tag byte at the specified offset."""
    actual = b[offset]
    if actual != expected:
        raise ValueError(f"{device_name}: Expected tag 0x{expected:02X} at offset {offset}, got 0x{actual:02X}")


def _extract_rssi(b: bytes) -> int:
    """Extract RSSI from payload bytes."""
    rssi_raw = b[1]
    return rssi_raw if rssi_raw < 128 else rssi_raw - 256


def _extract_status_code(b: bytes, offset: int, offset2: int) -> int:
    """Extract status code from payload bytes."""
    return b[offset] | (b[offset2] << 8)


_BATTERY_MAP = {
    0x0FFF: 100,
    0x0FFE: 90,
    0x0FFD: 80,
    0x0FFC: 70,
    0x0FFB: 60,
    0x0FFA: 50,
    0x0FF9: 40,
    0x0FF8: 30,
    0x0FF7: 20,
    0x0FF6: 10,
}


def _battery_status_to_percent(status_code: int) -> int:
    """Convert battery status code to percentage."""
    return _BATTERY_MAP.get(status_code, 0)
