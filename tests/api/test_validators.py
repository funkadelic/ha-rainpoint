"""Tests for the RainPoint API validation functions."""

import pytest

from custom_components.rainpoint.api import (
    _battery_flag_to_percent,
    _extract_battery_flag,
    _extract_rssi,
    _validate_payload,
    _validate_tag,
)


class TestValidatePayload:
    """Tests for _validate_payload."""

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
    """Tests for _extract_rssi."""

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


class TestExtractBatteryFlag:
    """Tests for _extract_battery_flag."""

    def test_reads_sta_bat_record(self):
        """The byte following the 0xDC header is the flag."""
        b = bytes.fromhex("E1C600DC01881A")
        assert _extract_battery_flag(b) == 1

    def test_reads_non_normal_flag(self):
        """A flag other than 1 is returned as-is, not clamped."""
        b = bytes.fromhex("E1C600DC03881A")
        assert _extract_battery_flag(b) == 3

    def test_dp_id_prefixed_frame(self):
        """An 11# frame carries a dp_id ahead of every record header."""
        b = bytes.fromhex("17E1AE0018DC01")
        assert _extract_battery_flag(b, dp_id_prefixed=True) == 1

    def test_frame_without_sta_bat_returns_none(self):
        """No STA_BAT record means no reading, not a default."""
        b = bytes.fromhex("E1C600881A")
        assert _extract_battery_flag(b) is None

    def test_value_byte_equal_to_header_is_not_matched(self):
        """0xDC appearing inside another record's value is not read as STA_BAT.

        The illuminance value below contains a 0xDC byte. A scan for the header
        byte would return the byte after it; the structural walk skips it.
        """
        b = bytes.fromhex("E1C60085DC0000")
        assert _extract_battery_flag(b) is None


class TestBatteryFlagToPercent:
    """Tests for _battery_flag_to_percent."""

    @pytest.mark.parametrize("flag", [0, 1])
    def test_normal_flags_map_to_full(self, flag):
        """The corroborated readings both mean a healthy cell."""
        assert _battery_flag_to_percent(flag) == 100

    @pytest.mark.parametrize("flag", [2, 3, 4, 255])
    def test_unproven_flags_map_to_none(self, flag):
        """No capture pairs these with a charge level, so none is invented."""
        assert _battery_flag_to_percent(flag) is None

    def test_missing_flag_maps_to_none(self):
        """A frame with no STA_BAT record yields no percentage."""
        assert _battery_flag_to_percent(None) is None


class TestValidateTag:
    """Tests for _validate_tag."""

    def test_matching_tag_passes(self):
        """No exception when tag matches expected value."""
        b = bytes([0x00, 0xAA, 0x00])
        _validate_tag(b, 1, 0xAA, "TestDevice")  # should not raise

    def test_mismatched_tag_raises(self):
        """Mismatched tag raises ValueError with device name."""
        b = bytes([0x00, 0xBB, 0x00])
        with pytest.raises(ValueError, match=r"TestDevice.*Expected tag 0xAA.*got 0xBB"):
            _validate_tag(b, 1, 0xAA, "TestDevice")
