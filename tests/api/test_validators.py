"""Tests for RainPoint API validation functions (COVR-05)."""

import pytest

from custom_components.rainpoint.api import (
    _battery_status_to_percent,
    _extract_rssi,
    _extract_status_code,
    _validate_payload,
    _validate_tag,
)


class TestValidatePayload:
    """Tests for _validate_payload (COVR-05)."""

    def test_length_match(self):
        """Exact length match returns parsed bytes."""
        result = _validate_payload("10#AABB", 2)
        assert result == b"\xaa\xbb"

    def test_longer_within_limit(self):
        """Payload longer than expected but within 2x limit succeeds."""
        # expected=2, max=4 bytes. 3 bytes is within limit.
        result = _validate_payload("10#AABBCC", 2)
        assert result == b"\xaa\xbb\xcc"

    def test_too_short_raises(self):
        """Payload shorter than expected raises ValueError."""
        with pytest.raises(ValueError, match="Expected at least 2 bytes, got 1"):
            _validate_payload("10#AA", 2)

    def test_too_long_raises(self):
        """Payload exceeding 2x expected length raises ValueError."""
        # expected=2, max=4 bytes. 5 bytes exceeds limit.
        hex_5_bytes = "AA" * 5
        with pytest.raises(ValueError, match="Payload too long"):
            _validate_payload("10#" + hex_5_bytes, 2)

    def test_11_prefix_raises(self):
        """11# prefix is rejected (only 10# is valid for _validate_payload)."""
        with pytest.raises(ValueError, match="Expected prefix '10'"):
            _validate_payload("11#AABB", 2)

    def test_missing_hash_raises(self):
        """Payload without '#' separator raises ValueError."""
        with pytest.raises(ValueError, match="missing '#' separator"):
            _validate_payload("no_hash_here", 2)


class TestExtractRssi:
    """Tests for _extract_rssi (COVR-05)."""

    def test_negative_rssi(self):
        """High byte (>=128) produces negative signed value."""
        # b[1] = 0xAC = 172 -> 172 - 256 = -84
        b = bytes([0x00, 0xAC])
        assert _extract_rssi(b) == -84

    def test_positive_rssi(self):
        """Low byte (<128) returned as-is."""
        b = bytes([0x00, 0x50])
        assert _extract_rssi(b) == 80

    def test_zero_rssi(self):
        """Zero RSSI."""
        b = bytes([0x00, 0x00])
        assert _extract_rssi(b) == 0

    def test_boundary_127(self):
        """127 is the last positive value."""
        b = bytes([0x00, 0x7F])
        assert _extract_rssi(b) == 127

    def test_boundary_128(self):
        """128 wraps to negative: 128 - 256 = -128."""
        b = bytes([0x00, 0x80])
        assert _extract_rssi(b) == -128


class TestBatteryStatusToPercent:
    """Tests for _battery_status_to_percent (COVR-05)."""

    def test_full_battery(self):
        assert _battery_status_to_percent(0x0FFF) == 100

    def test_ninety_percent(self):
        assert _battery_status_to_percent(0x0FFE) == 90

    def test_ten_percent(self):
        assert _battery_status_to_percent(0x0FF6) == 10

    def test_unknown_code_returns_zero(self):
        """Unmapped status code returns 0."""
        assert _battery_status_to_percent(0x0000) == 0

    def test_all_mapped_values(self):
        """Verify all 10 mapped values from 100 down to 10."""
        expected = {
            0x0FFF: 100, 0x0FFE: 90, 0x0FFD: 80, 0x0FFC: 70,
            0x0FFB: 60, 0x0FFA: 50, 0x0FF9: 40, 0x0FF8: 30,
            0x0FF7: 20, 0x0FF6: 10,
        }
        for code, pct in expected.items():
            assert _battery_status_to_percent(code) == pct, f"Code 0x{code:04X} should be {pct}%"


class TestValidateTag:
    """Tests for _validate_tag (COVR-05)."""

    def test_matching_tag_passes(self):
        """No exception when tag matches expected value."""
        b = bytes([0x00, 0xAA, 0x00])
        _validate_tag(b, 1, 0xAA, "TestDevice")  # should not raise

    def test_mismatched_tag_raises(self):
        """Mismatched tag raises ValueError with device name."""
        b = bytes([0x00, 0xBB, 0x00])
        with pytest.raises(ValueError, match="TestDevice.*Expected tag 0xAA.*got 0xBB"):
            _validate_tag(b, 1, 0xAA, "TestDevice")


class TestExtractStatusCode:
    """Tests for _extract_status_code (COVR-05)."""

    def test_simple_value(self):
        """Low byte only."""
        b = bytes([0x00, 0x00, 0x0A, 0x00])
        assert _extract_status_code(b, 2, 3) == 0x0A

    def test_high_byte_shifted(self):
        """High byte is shifted left 8 bits and ORed with low byte."""
        b = bytes([0x00, 0x00, 0xFF, 0x0F])
        assert _extract_status_code(b, 2, 3) == 0x0FFF

    def test_both_bytes_contribute(self):
        """Both bytes assemble into a 16-bit value: 0x12 << 8 | 0x34 = 0x1234."""
        b = bytes([0x00, 0x00, 0x34, 0x12])
        assert _extract_status_code(b, 2, 3) == 0x1234
