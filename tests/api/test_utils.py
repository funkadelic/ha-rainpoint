"""Tests for the RainPoint API utility functions."""

import pytest

from custom_components.rainpoint.api import (
    _base_decoder_dict,
    _extract_report_time,
    _f10_to_c,
    _le16,
    _parse_rainpoint_payload,
    _parse_tlv_payload,
)
from custom_components.rainpoint.api.utils import _is_ascii_payload, _parse_ascii_rssi
from tests.payload_samples import (
    HWS019WRF_V2_PAYLOAD,
    MOISTURE_FULL_ASCII_PAYLOAD,
    SAMPLE_HTV245_ASCII_PAYLOAD,
)


class TestParseRainpointPayload:
    """Tests for _parse_rainpoint_payload."""

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
    """Tests for _parse_tlv_payload."""

    def test_tlv_known_type_widths(self):
        """All known type bytes decode with correct value widths, little-endian throughout."""
        # Build a payload with one record per known type:
        # dp_id=0x01 type=0xD8 val=0xFF (1 byte)
        # dp_id=0x02 type=0xDC val=0x01 (1 byte)
        # dp_id=0x03 type=0xAD val=E803 (2 bytes, LE=1000)
        # dp_id=0x04 type=0x20 val=000A (2 bytes, LE=2560)
        # dp_id=0x05 type=0xE1 val=0014 (2 bytes, LE=5120)
        # dp_id=0x06 type=0xB7 val=00000064 (4 bytes, LE=1677721600)
        # dp_id=0x07 type=0x9F val=000000C8 (4 bytes, LE=3355443200)
        # dp_id=0x08 type=0xC4 val=0x0A (1 byte)
        # dp_id=0x09 type=0xC5 val=0x0B (1 byte)
        # dp_id=0x0A type=0xC6 val=0x0C (1 byte)
        payload_bytes = bytes(
            [
                0x01,
                0xD8,
                0xFF,
                0x02,
                0xDC,
                0x01,
                0x03,
                0xAD,
                0xE8,
                0x03,
                0x04,
                0x20,
                0x00,
                0x0A,
                0x05,
                0xE1,
                0x00,
                0x14,
                0x06,
                0xB7,
                0x00,
                0x00,
                0x00,
                0x64,
                0x07,
                0x9F,
                0x00,
                0x00,
                0x00,
                0xC8,
                0x08,
                0xC4,
                0x0A,
                0x09,
                0xC5,
                0x0B,
                0x0A,
                0xC6,
                0x0C,
            ]
        )
        raw = "11#" + payload_bytes.hex()
        result = _parse_tlv_payload(raw)

        assert result[0x01] == (0xD8, 0xFF, b"\xff")
        assert result[0x02] == (0xDC, 0x01, b"\x01")
        # Every multi-byte value is little-endian; these byte patterns were
        # chosen to read as round numbers big-endian, so the values below are
        # what a big-endian regression would visibly break.
        assert result[0x03] == (0xAD, 1000, b"\xe8\x03")
        assert result[0x04] == (0x20, 2560, b"\x00\x0a")
        assert result[0x05] == (0xE1, 5120, b"\x00\x14")
        assert result[0x06] == (0xB7, 1677721600, bytes.fromhex("00000064"))
        assert result[0x07] == (0x9F, 3355443200, bytes.fromhex("000000C8"))
        assert result[0x08] == (0xC4, 0x0A, b"\x0a")
        assert result[0x09] == (0xC5, 0x0B, b"\x0b")
        assert result[0x0A] == (0xC6, 0x0C, b"\x0c")

    def test_0xad_little_endian(self):
        """0xAD type decodes value as little-endian."""
        payload = bytes([0x25, 0xAD, 0xE8, 0x03])
        result = _parse_tlv_payload("11#" + payload.hex())
        _, value, _ = result[0x25]
        assert value == 1000  # LE: 0x03E8 = 1000; BE would give 59395

    def test_non_0xad_types_are_little_endian_too(self):
        """Endianness is a property of the framing, not of the 0xAD type byte.

        Captured frames decode correctly as little-endian for the 4-byte usage
        and timestamp records as well, so no type byte is exempt.
        """
        payload = bytes([0x04, 0x20, 0x00, 0x0A])
        result = _parse_tlv_payload("11#" + payload.hex())
        _, value, _ = result[0x04]
        assert value == 2560  # LE: 0x0A00 = 2560; BE would give 10

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
        """Basic."""
        assert _le16(b"\x05\x00", 0) == 5

    def test_with_offset(self):
        """With offset."""
        assert _le16(b"\x00\x00\xe8\x03", 2) == 1000

    def test_max_value(self):
        """Max value."""
        assert _le16(b"\xff\xff", 0) == 65535


class TestF10ToC:
    """Tests for _f10_to_c (Fahrenheit*10 to Celsius)."""

    def test_freezing(self):
        """Freezing."""
        # 32F = 0C; 32*10 = 320
        assert abs(_f10_to_c(320) - 0.0) < 0.01

    def test_boiling(self):
        """Boiling."""
        # 212F = 100C; 212*10 = 2120
        assert abs(_f10_to_c(2120) - 100.0) < 0.01

    def test_room_temp(self):
        """Room temp."""
        # 72F ~ 22.22C; 72*10 = 720
        assert abs(_f10_to_c(720) - 22.22) < 0.1


class TestBaseDecoderDict:
    """Tests for _base_decoder_dict."""

    def test_returns_expected_keys(self):
        """Returns expected keys."""
        result = _base_decoder_dict("valve_hub", -84, b"\xaa\xbb")
        assert result == {
            "type": "valve_hub",
            "rssi_dbm": -84,
            "raw_bytes": b"\xaa\xbb",
        }

    def test_returns_independent_dicts(self):
        """Each call returns a fresh dict so callers can mutate safely."""
        a = _base_decoder_dict("soil", -70, b"\x01")
        b = _base_decoder_dict("soil", -70, b"\x01")
        a["extra"] = True
        assert "extra" not in b


class TestExtractReportTime:
    """Tests for _extract_report_time and the packed wall-clock format."""

    def test_reads_trailing_record(self):
        """The 4-byte STA_REPTIME value unpacks to a naive ISO wall clock."""
        b = bytes.fromhex("E1C400DC018825FF0FE1C4FA19")
        assert _extract_report_time(b) == ("2026-07-29T12:19:33", 0x19FAC4E1)

    def test_dp_id_prefixed_frame(self):
        """An 11# frame prefixes the record with a dp_id."""
        b = bytes.fromhex("17E1DB0018DC01FEFF0F0270F219")
        iso, raw = _extract_report_time(b, dp_id_prefixed=True)
        assert iso == "2026-07-25T07:00:02"
        assert raw == 0x19F27002

    def test_frame_without_the_record_returns_none(self):
        """No STA_REPTIME record means no reading."""
        b = bytes.fromhex("E1C400DC018825")
        assert _extract_report_time(b) is None

    def test_impossible_date_returns_none(self):
        """A word whose month field is zero is rejected, not clamped into range.

        A misaligned or truncated frame must not surface a fabricated date.
        """
        b = bytes.fromhex("E1C400FF0F00000000")
        assert _extract_report_time(b) is None


class TestIsAsciiPayload:
    """Tests for _is_ascii_payload, the ASCII/hex discrimination predicate."""

    def test_htv245_sample_is_ascii(self):
        """The committed HTV245 ASCII sample is recognised."""
        assert _is_ascii_payload(SAMPLE_HTV245_ASCII_PAYLOAD) is True

    def test_moisture_full_sample_is_ascii(self):
        """The committed HCS021FRF ASCII sample is recognised."""
        assert _is_ascii_payload(MOISTURE_FULL_ASCII_PAYLOAD) is True

    def test_hws019wrf_v2_sample_is_ascii(self):
        """The committed HWS019WRF-V2 ASCII sample is recognised."""
        assert _is_ascii_payload(HWS019WRF_V2_PAYLOAD) is True

    def test_11_hash_prefix_is_not_ascii(self):
        """A '#' prefix routes to the hex path before the ASCII test is reached."""
        assert _is_ascii_payload("11#1FD801") is False

    def test_10_hash_prefix_with_ascii_tail_is_not_ascii(self):
        """A hex payload carrying a comma tail still routes to hex, not ASCII."""
        assert _is_ascii_payload("10#AABBCC,1,2") is False

    def test_no_semicolon_no_hash_is_not_ascii(self):
        """A bare hex-looking string with neither marker is not ASCII-shaped."""
        assert _is_ascii_payload("AABBCC") is False


class TestParseAsciiRssi:
    """Tests for _parse_ascii_rssi, the header rssi reader."""

    def test_negative_rssi_from_htv245_sample(self):
        """The committed HTV245 sample's header rssi parses to -84."""
        assert _parse_ascii_rssi(SAMPLE_HTV245_ASCII_PAYLOAD) == -84

    def test_negative_rssi_from_moisture_full_sample(self):
        """The committed HCS021FRF sample's header rssi parses to -73."""
        assert _parse_ascii_rssi(MOISTURE_FULL_ASCII_PAYLOAD) == -73

    def test_non_negative_rssi_returns_none(self):
        """A non-negative header rssi (HWS019WRF_V2's real 0) yields None, not 0."""
        assert _parse_ascii_rssi(HWS019WRF_V2_PAYLOAD) is None

    def test_truncated_header_returns_none(self):
        """A header with fewer than three comma parts yields None, no raise."""
        assert _parse_ascii_rssi("1,-84;body") is None

    def test_non_integer_rssi_token_returns_none(self):
        """A non-integer rssi token yields None, no raise."""
        assert _parse_ascii_rssi("1,x,1;body") is None
