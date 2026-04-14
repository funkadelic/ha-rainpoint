"""Tests for RainPoint API utility functions (COVR-03, COVR-04)."""

import pytest

from custom_components.rainpoint.api import (
    _f10_to_c,
    _le16,
    _parse_rainpoint_payload,
    _parse_tlv_payload,
)


class TestParseRainpointPayload:
    """Tests for _parse_rainpoint_payload (COVR-04)."""

    def test_10_prefix_flat_hex(self):
        """10# prefix returns decoded hex bytes."""
        assert _parse_rainpoint_payload("10#AABB") == b"\xaa\xbb"

    def test_11_prefix_tlv_hex(self):
        """11# prefix returns decoded hex bytes."""
        assert _parse_rainpoint_payload("11#AABB") == b"\xaa\xbb"

    def test_missing_hash_separator_raises(self):
        """Payload without '#' raises ValueError."""
        with pytest.raises(ValueError, match="missing '#' separator"):
            _parse_rainpoint_payload("garbage_no_hash")

    def test_unknown_prefix_raises(self):
        """Unrecognized prefix raises ValueError."""
        with pytest.raises(ValueError, match="Unknown payload prefix"):
            _parse_rainpoint_payload("99#AABB")

    def test_empty_hex_after_prefix(self):
        """Empty hex data after prefix returns empty bytes."""
        assert _parse_rainpoint_payload("10#") == b""

    def test_invalid_hex_raises(self):
        """Non-hex characters after prefix raise ValueError."""
        with pytest.raises(ValueError):
            _parse_rainpoint_payload("10#ZZZZ")


class TestParseTlvPayload:
    """Tests for _parse_tlv_payload (COVR-03)."""

    def test_tlv_known_type_widths(self):
        """All known type bytes decode with correct value widths."""
        # Build a payload with one record per known type:
        # dp_id=0x01 type=0xD8 val=0xFF (1 byte)
        # dp_id=0x02 type=0xDC val=0x01 (1 byte)
        # dp_id=0x03 type=0xAD val=E803 (2 bytes, LE=1000)
        # dp_id=0x04 type=0x20 val=000A (2 bytes, BE=10)
        # dp_id=0x05 type=0xE1 val=0014 (2 bytes, BE=20)
        # dp_id=0x06 type=0xB7 val=00000064 (4 bytes, BE=100)
        # dp_id=0x07 type=0x9F val=000000C8 (4 bytes, BE=200)
        # dp_id=0x08 type=0xC4 val=0x0A (1 byte)
        # dp_id=0x09 type=0xC5 val=0x0B (1 byte)
        # dp_id=0x0A type=0xC6 val=0x0C (1 byte)
        payload_bytes = bytes([
            0x01, 0xD8, 0xFF,
            0x02, 0xDC, 0x01,
            0x03, 0xAD, 0xE8, 0x03,
            0x04, 0x20, 0x00, 0x0A,
            0x05, 0xE1, 0x00, 0x14,
            0x06, 0xB7, 0x00, 0x00, 0x00, 0x64,
            0x07, 0x9F, 0x00, 0x00, 0x00, 0xC8,
            0x08, 0xC4, 0x0A,
            0x09, 0xC5, 0x0B,
            0x0A, 0xC6, 0x0C,
        ])
        raw = "11#" + payload_bytes.hex()
        result = _parse_tlv_payload(raw)

        assert result[0x01] == (0xD8, 0xFF, b"\xff")
        assert result[0x02] == (0xDC, 0x01, b"\x01")
        assert result[0x03] == (0xAD, 1000, b"\xe8\x03")  # LE
        assert result[0x04] == (0x20, 10, b"\x00\x0a")     # BE
        assert result[0x05] == (0xE1, 20, b"\x00\x14")     # BE
        assert result[0x06] == (0xB7, 100, bytes.fromhex("00000064"))  # BE
        assert result[0x07] == (0x9F, 200, bytes.fromhex("000000C8"))  # BE
        assert result[0x08] == (0xC4, 0x0A, b"\x0a")
        assert result[0x09] == (0xC5, 0x0B, b"\x0b")
        assert result[0x0A] == (0xC6, 0x0C, b"\x0c")

    def test_0xad_little_endian(self):
        """0xAD type decodes value as little-endian."""
        payload = bytes([0x25, 0xAD, 0xE8, 0x03])
        result = _parse_tlv_payload("11#" + payload.hex())
        _, value, _ = result[0x25]
        assert value == 1000  # LE: 0x03E8 = 1000; BE would give 59395

    def test_non_0xad_big_endian(self):
        """Non-0xAD 2-byte types decode as big-endian."""
        payload = bytes([0x04, 0x20, 0x00, 0x0A])
        result = _parse_tlv_payload("11#" + payload.hex())
        _, value, _ = result[0x04]
        assert value == 10  # BE: 0x000A = 10

    def test_unknown_type_skips_2_bytes(self):
        """Unknown type byte causes a 2-byte skip (dp_id + type), then parsing continues."""
        # Record 1: dp_id=0x01, type=0xFF (unknown) -> skip 2 bytes
        # Record 2: dp_id=0x02, type=0xD8, val=0x01 (known, 1 byte)
        payload = bytes([0x01, 0xFF, 0x02, 0xD8, 0x01])
        result = _parse_tlv_payload("11#" + payload.hex())
        assert 0x01 not in result, "Unknown type should not produce a result entry"
        assert 0x02 in result, "Record after unknown type should be parsed"
        assert result[0x02] == (0xD8, 0x01, b"\x01")

    def test_short_payload_returns_partial(self):
        """Payload too short for declared value width returns records parsed so far."""
        # Record 1: dp_id=0x01, type=0xD8, val=0xFF (valid, 1 byte)
        # Record 2: dp_id=0x02, type=0xB7 (4-byte width), but only 1 byte of value follows
        payload = bytes([0x01, 0xD8, 0xFF, 0x02, 0xB7, 0x01])
        result = _parse_tlv_payload("11#" + payload.hex())
        assert 0x01 in result
        assert result[0x01] == (0xD8, 0xFF, b"\xff")
        assert 0x02 not in result  # Truncated, not enough bytes

    def test_single_trailing_byte_returns_empty(self):
        """Payload with only one byte (dp_id but no type) returns empty dict."""
        result = _parse_tlv_payload("11#" + bytes([0x01]).hex())
        assert result == {}

    def test_empty_payload_returns_empty(self):
        """Empty hex data returns empty dict."""
        result = _parse_tlv_payload("11#")
        assert result == {}


class TestLe16:
    """Tests for _le16 helper."""

    def test_basic(self):
        assert _le16(b"\x05\x00", 0) == 5

    def test_with_offset(self):
        assert _le16(b"\x00\x00\xe8\x03", 2) == 1000

    def test_max_value(self):
        assert _le16(b"\xff\xff", 0) == 65535


class TestF10ToC:
    """Tests for _f10_to_c (Fahrenheit*10 to Celsius)."""

    def test_freezing(self):
        # 32F = 0C; 32*10 = 320
        assert abs(_f10_to_c(320) - 0.0) < 0.01

    def test_boiling(self):
        # 212F = 100C; 212*10 = 2120
        assert abs(_f10_to_c(2120) - 100.0) < 0.01

    def test_room_temp(self):
        # 72F ~ 22.22C; 72*10 = 720
        assert abs(_f10_to_c(720) - 22.22) < 0.1
