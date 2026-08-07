"""Tests for the RainPoint API utility functions."""

import logging

import pytest

from custom_components.rainpoint.api import (
    _base_decoder_dict,
    _extract_report_time,
    _f10_to_c,
    _le16,
    _parse_hub_broadcast_flag,
    _parse_rainpoint_payload,
    _parse_sub_power_mode,
    _parse_tlv_payload,
    _splice_hub_broadcast_param,
    _splice_sub_power_mode,
)
from custom_components.rainpoint.api.utils import _is_ascii_payload, _parse_ascii_rssi, _split_prefix
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


class TestSplitPrefixCommaTruncation:
    """Characterization tests for _split_prefix's existing tail-truncation behaviour.

    This behaviour must survive unchanged; these pin it behaviourally as
    well as by the source-identity check in tests/api/test_generic_decoder.py.
    """

    def test_hex_body_with_no_comma_tail_is_returned_whole(self):
        """A hex body with no ASCII tail passes through unchanged (uppercased)."""
        assert _split_prefix("10#aabbcc") == ("AABBCC", False)

    def test_hex_body_with_ascii_tail_truncates_at_first_comma(self):
        """The comma-separated ASCII tail is dropped, keeping only the hex block."""
        assert _split_prefix("10#AABBCC,1,2") == ("AABBCC", False)

    def test_11_prefix_marks_dp_id_prefixed(self):
        """An 11# prefix sets dp_id_prefixed True; 10# leaves it False."""
        assert _split_prefix("11#AABB")[1] is True
        assert _split_prefix("10#AABB")[1] is False

    def test_no_hash_prefix_returns_raw_body_uppercased(self):
        """A payload with no '#' is returned as-is (uppercased, comma-truncated)."""
        assert _split_prefix("aabb") == ("AABB", False)


class TestParseHubBroadcastFlag:
    """Happy-path reads of the hub record's param index-1 broadcast flag."""

    def test_flag_on(self):
        """Index 1 == '1' reads as True."""
        assert _parse_hub_broadcast_flag("0|1||") is True

    def test_flag_off(self):
        """Index 1 == '0' reads as False."""
        assert _parse_hub_broadcast_flag("0|0||") is False


class TestSpliceHubBroadcastParam:
    """Happy-path writes of the hub record's param index-1 broadcast flag."""

    def test_splice_to_on(self):
        """Splicing True into an off param flips only index 1."""
        assert _splice_hub_broadcast_param("0|0||", True) == "0|1||"

    def test_splice_to_off(self):
        """Splicing False into an on param flips only index 1."""
        assert _splice_hub_broadcast_param("0|1||", False) == "0|0||"


# Every input in this matrix is a param this module must not trust, whether it
# arrives as a non-str type from a cloud JSON document or as a str whose
# index-1 token is missing, empty, padded, or not one of the two literals.
# bool is included deliberately: it is an int subclass and a naive isinstance
# check gets it wrong.
_MALFORMED_HUB_PARAMS = [
    pytest.param(None, id="none"),
    pytest.param("", id="empty-string"),
    pytest.param("0", id="single-field-zero"),
    pytest.param("1", id="single-field-one"),
    pytest.param(0, id="int-zero"),
    pytest.param(1, id="int-one"),
    pytest.param(True, id="bool-true"),
    pytest.param([], id="empty-list"),
    pytest.param({}, id="empty-dict"),
    pytest.param("0|", id="empty-index-1"),
    pytest.param("0|2||", id="out-of-range-token"),
    pytest.param("0|x||", id="non-numeric-token"),
    pytest.param("0|1 ||", id="padded-token"),
    pytest.param("0||1|", id="empty-field-at-index-1"),
]


class TestParseHubBroadcastFlagMalformedMatrix:
    """Every unreadable param shape leaves the flag unknown, never guessed.

    Pins the rejected exact-4-field-gate alternative from the other side:
    a two-field and a five-field param both parse below, so tightening the
    gate to require exactly four fields would fail those two tests, not this
    class's.
    """

    @pytest.mark.parametrize("param", _MALFORMED_HUB_PARAMS)
    def test_returns_none_and_does_not_raise(self, param):
        """Every malformed shape in the matrix returns None."""
        assert _parse_hub_broadcast_flag(param) is None

    def test_two_field_param_parses(self):
        """A two-field param still has a recoverable index 1."""
        assert _parse_hub_broadcast_flag("0|1") is True

    def test_five_field_param_parses(self):
        """A five-field param still has a recoverable index 1.

        The one observed hub produces four fields; this is not that hub, and
        the gate must not have been narrowed to require its exact shape.
        """
        assert _parse_hub_broadcast_flag("0|1|2|3|4") is True

    def test_no_log_record_emitted_for_any_matrix_input(self, caplog):
        """No log record at any level for any input in the malformed matrix."""
        with caplog.at_level(logging.DEBUG):
            for param in [p.values[0] for p in _MALFORMED_HUB_PARAMS]:
                _parse_hub_broadcast_flag(param)
        assert caplog.records == []


class TestSpliceHubBroadcastParamMalformedMatrix:
    """A splice refuses on exactly the inputs the parser refuses on.

    This is the field-blanking-bug guard from the other side: a splice that
    fell open to a reconstructed default for any of these inputs is exactly
    the bug this gate exists to prevent. One gate governs both functions, so
    this class parametrizes over the same matrix the parse test above uses.
    """

    @pytest.mark.parametrize("param", _MALFORMED_HUB_PARAMS)
    def test_returns_none_and_does_not_raise(self, param):
        """Every malformed shape in the matrix refuses to build a write."""
        assert _splice_hub_broadcast_param(param, True) is None
        assert _splice_hub_broadcast_param(param, False) is None

    def test_no_log_record_emitted_for_any_matrix_input(self, caplog):
        """No log record at any level for any input in the malformed matrix."""
        with caplog.at_level(logging.DEBUG):
            for param in [p.values[0] for p in _MALFORMED_HUB_PARAMS]:
                _splice_hub_broadcast_param(param, True)
        assert caplog.records == []


class TestSpliceHubBroadcastParamFieldPreservation:
    """Every field beyond index 1 round-trips byte-identical, index for index.

    Proven against param shapes no observed hub has produced -- five fields
    with non-empty trailing fields, and fields carrying non-ASCII code
    points -- which is the phase's own success criterion.
    """

    def test_five_field_param_preserves_every_other_index(self):
        """A five-field param with non-empty trailing fields round-trips."""
        original = "9|0|abc|def|xyz"
        result = _splice_hub_broadcast_param(original, True)
        assert result == "9|1|abc|def|xyz"

        original_fields = original.split("|")
        result_fields = result.split("|")
        assert len(result_fields) == len(original_fields)
        for index, (before, after) in enumerate(zip(original_fields, result_fields, strict=True)):
            if index == 1:
                continue
            assert after == before

    def test_non_ascii_fields_are_not_reencoded_or_normalised(self):
        """Fields carrying non-ASCII code points compare equal before and after."""
        original = "0|0|éè|中文"
        result = _splice_hub_broadcast_param(original, True)

        original_fields = original.split("|")
        result_fields = result.split("|")
        assert len(result_fields) == len(original_fields)
        assert result_fields[2] == original_fields[2]
        assert result_fields[3] == original_fields[3]

    def test_adjacent_delimiters_survive_as_empty_fields(self):
        """Splicing '0|0||' yields '0|1||': the two trailing empty fields
        are neither collapsed nor trimmed."""
        assert _splice_hub_broadcast_param("0|0||", True) == "0|1||"

    def test_resplicing_an_already_set_value_is_a_string_no_op(self):
        """Re-splicing a param whose flag already matches the request changes nothing."""
        assert _splice_hub_broadcast_param("0|1||", True) == "0|1||"

    @pytest.mark.parametrize(
        "param",
        ["0|0||", "9|0|abc|def|xyz", "0|1|2|3|4", "0|1"],
    )
    def test_output_field_count_always_equals_input_field_count(self, param):
        """len(splice(x).split('|')) == len(x.split('|')) for every parseable x."""
        result = _splice_hub_broadcast_param(param, True)
        assert len(result.split("|")) == len(param.split("|"))


# The captured HTV210B blob verbatim, used for the round-trip case rather
# than a blob this test file constructs from parts -- a self-constructed
# blob only proves the test agrees with itself.
_HTV210B_CAPTURED_PARAM = (
    "5=01,11=58020a001e000000000000000000,12=58020a001e000000000000000000,50=646464646464646464646464,51=646464646464646464646464"
)


class TestParseSubPowerMode:
    """Happy-path reads of the sub-device record's param key-5 power mode."""

    @pytest.mark.parametrize(
        "param, expected",
        [
            pytest.param("5=00", "0", id="unpadded-width-zero-padding"),
            pytest.param("5=0", "0", id="power-saving-unpadded"),
            pytest.param("5=01", "1", id="standard-padded"),
            pytest.param("5=1", "1", id="standard-unpadded"),
            pytest.param("5=02", "2", id="enhance-padded"),
            pytest.param("5=2", "2", id="enhance-unpadded"),
        ],
    )
    def test_recognised_wire_values(self, param, expected):
        """Both the unpadded literal set and the captured zero-padded set parse."""
        assert _parse_sub_power_mode(param) == expected

    def test_only_key_five_present_parses(self):
        """A blob carrying exactly key 5 and nothing else still parses."""
        assert _parse_sub_power_mode("5=01") == "1"

    def test_unidentified_key_alongside_a_valid_key_five_still_parses(self):
        """A key nobody has identified does not block an otherwise-valid blob."""
        assert _parse_sub_power_mode("5=01,99=whatever-this-is") == "1"

    def test_captured_htv210b_blob_parses_as_standard(self):
        """The captured HTV210B blob's key 5 (01) reads as Standard."""
        assert _parse_sub_power_mode(_HTV210B_CAPTURED_PARAM) == "1"


# Every input in this matrix is a param neither the parse gate nor the splice
# gate may trust: a non-str type from a cloud JSON document, an empty string,
# a malformed token, a duplicate key (key 5 or otherwise), or a blob missing
# key 5 entirely -- including one whose keys merely contain "5" as a
# substring ("15", "50"). Shared by both the parse-refusal and the
# splice-refusal test classes below so the two gates cannot silently drift
# apart on what counts as readable, since the splice calls the parse as its
# own precondition.
_MALFORMED_SUB_PARAMS = [
    pytest.param(None, id="none"),
    pytest.param(0, id="int-zero"),
    pytest.param(1, id="int-one"),
    pytest.param(True, id="bool-true"),
    pytest.param([], id="empty-list"),
    pytest.param({}, id="empty-dict"),
    pytest.param("", id="empty-string"),
    pytest.param("5=3", id="unrecognised-value-one-past-enhance"),
    pytest.param("5=03", id="unrecognised-value-padded-one-past-enhance"),
    pytest.param("5=002", id="unrecognised-value-three-digit-padding"),
    pytest.param("5=-1", id="unrecognised-value-negative"),
    pytest.param("5=", id="empty-value"),
    pytest.param("5=x", id="non-numeric-value"),
    pytest.param("5", id="token-with-no-assignment-character"),
    pytest.param("5=01=02", id="token-with-two-assignment-characters"),
    pytest.param("5=01,5=02", id="duplicate-key-five"),
    pytest.param("11=a,11=b,5=01", id="duplicate-key-not-five"),
    pytest.param(
        "11=58020a001e000000000000000000,12=58020a001e000000000000000000,50=646464646464646464646464,51=646464646464646464646464",
        id="every-known-key-except-five",
    ),
    pytest.param("15=a,50=b", id="keys-containing-five-as-a-substring-but-not-five-itself"),
]


class TestParseSubPowerModeMalformedMatrix:
    """Every unreadable param shape leaves the mode unknown, never guessed."""

    @pytest.mark.parametrize("param", _MALFORMED_SUB_PARAMS)
    def test_returns_none_and_does_not_raise(self, param):
        """Every malformed shape in the matrix returns None."""
        assert _parse_sub_power_mode(param) is None

    def test_no_log_record_emitted_for_any_matrix_input(self, caplog):
        """No log record at any level for any input in the malformed matrix."""
        with caplog.at_level(logging.DEBUG):
            for param in [p.values[0] for p in _MALFORMED_SUB_PARAMS]:
                _parse_sub_power_mode(param)
        assert caplog.records == []


class TestSpliceSubPowerMode:
    """Happy-path and field-preservation writes of the sub-device param key 5."""

    def test_captured_blob_splices_to_enhance_preserving_every_other_key(self):
        """Splicing the captured blob to Enhance changes only the key-5 token.

        Keys 11, 12, 50 and 51 compare equal to the captured literals
        character for character.
        """
        result = _splice_sub_power_mode(_HTV210B_CAPTURED_PARAM, "2")
        assert result == (
            "5=02,11=58020a001e000000000000000000,12=58020a001e000000000000000000,"
            "50=646464646464646464646464,51=646464646464646464646464"
        )

    def test_unpadded_input_splices_to_an_unpadded_output(self):
        """Splicing an unpadded '5=1' blob to Enhance yields '5=2', not '5=02'.

        The replacement keeps the character width of the value it replaced.
        """
        assert _splice_sub_power_mode("5=1,11=a", "2") == "5=2,11=a"

    def test_token_order_survives_a_deliberately_unordered_blob(self):
        """Token order in the output equals token order in the input."""
        original = "51=z,5=01,11=a,50=y,12=b"
        result = _splice_sub_power_mode(original, "2")
        assert result == "51=z,5=02,11=a,50=y,12=b"

    def test_resplicing_an_already_set_value_is_a_string_no_op(self):
        """Re-splicing a param whose mode already matches the request changes nothing."""
        assert _splice_sub_power_mode("5=02", "2") == "5=02"

    @pytest.mark.parametrize("mode", ["3", "x", "", "01", None])
    def test_non_canonical_mode_returns_none(self, mode):
        """A mode that is not one of the three canonical digits refuses the write."""
        assert _splice_sub_power_mode("5=01,11=a", mode) is None


class TestSpliceSubPowerModeMalformedMatrix:
    """A splice refuses on exactly the inputs the parser refuses on.

    Driven from the same _MALFORMED_SUB_PARAMS table
    TestParseSubPowerModeMalformedMatrix uses, rather than a second
    hand-written list, so the read and write refusal contracts cannot drift
    apart (the exact asymmetry the hub broadcast splice closed, generalized
    here).
    """

    @pytest.mark.parametrize("param", _MALFORMED_SUB_PARAMS)
    def test_returns_none_and_does_not_raise(self, param):
        """Every malformed shape in the matrix refuses to build a write."""
        assert _splice_sub_power_mode(param, "0") is None
        assert _splice_sub_power_mode(param, "1") is None
        assert _splice_sub_power_mode(param, "2") is None

    def test_no_log_record_emitted_for_any_matrix_input(self, caplog):
        """No log record at any level for any input in the malformed matrix."""
        with caplog.at_level(logging.DEBUG):
            for param in [p.values[0] for p in _MALFORMED_SUB_PARAMS]:
                _splice_sub_power_mode(param, "1")
        assert caplog.records == []
