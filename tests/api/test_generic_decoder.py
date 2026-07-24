"""Tests for the model-agnostic diagnostic decoder (decode_generic)."""

from custom_components.rainpoint.api import decode_generic
from tests.payload_samples import (
    SAMPLE_HTV145_CLOSED_PAYLOAD,
    SAMPLE_HTV245_TLV_PAYLOAD,
)


class TestDecodeGenericTLV:
    """11# self-describing TLV framing (dp_id-prefixed entries)."""

    def test_htv245_tlv_recovers_named_fields(self):
        """A synthetic HTV245 valve payload decodes to its known field set."""
        result = decode_generic(SAMPLE_HTV245_TLV_PAYLOAD)

        assert result["decoder"] == "generic-tlv"
        assert result["dp_id_prefixed"] is True
        assert "error" not in result
        assert result["field_names"] == [
            "STA_BAT",
            "STA_WKSTATE",
            "STA_WKSTATE",
            "STA_DURATION",
            "STA_DURATION",
        ]

    def test_htv245_duration_is_little_endian(self):
        """STA_DURATION (field index 19) is decoded little-endian: 3c00 -> 60."""
        fields = decode_generic(SAMPLE_HTV245_TLV_PAYLOAD)["fields"]
        durations = [f for f in fields if f["name"] == "STA_DURATION"]

        assert [f["value"] for f in durations] == [60, 0]
        assert durations[0]["raw"] == "3c00"

    def test_htv245_carries_dp_ids(self):
        """dp_id-prefixed framing exposes the per-entry dp_id for port mapping."""
        fields = decode_generic(SAMPLE_HTV245_TLV_PAYLOAD)["fields"]
        assert [f["dp_id"] for f in fields] == [0x18, 0x19, 0x1A, 0x25, 0x26]


class TestDecodeGenericFlat:
    """10# framing (no per-entry dp_id prefix)."""

    def test_htv145_flat_payload_parses(self):
        """A real 10# single-outlet payload decodes without error."""
        result = decode_generic(SAMPLE_HTV145_CLOSED_PAYLOAD)

        assert result["decoder"] == "generic-tlv"
        assert result["dp_id_prefixed"] is False
        assert "error" not in result
        # The leading entry is the RSSI field common to these devices.
        assert result["field_names"][0] == "STA_RSSI"
        assert all(f["dp_id"] == 0 for f in result["fields"])


class TestDecodeGenericRobustness:
    """The diagnostic decoder must never raise - polling depends on it."""

    def test_missing_prefix_hash_is_tolerated(self):
        result = decode_generic("not-a-payload")
        assert result["decoder"] == "generic-tlv"
        # No '#', body has odd length / non-hex -> reported as an error, no raise.
        assert "error" in result

    def test_odd_length_hex_reports_error(self):
        assert "error" in decode_generic("11#ABC")

    def test_empty_string(self):
        assert "error" in decode_generic("")

    def test_non_hex_body(self):
        assert "error" in decode_generic("11#ZZZZ")


class TestDecodeGenericFraming:
    """Cover the remaining header-form and value-width branches."""

    def test_compact_form_header(self):
        """Bit-7-clear header carries a 3-bit field index and its own value."""
        # 0x20 -> bit 7 clear, index (0x20>>4)&7 = 2 = STA_ALARM, value 0x20.
        fields = decode_generic("10#20")["fields"]
        assert fields == [{"name": "STA_ALARM", "index": 2, "dp_id": 0, "raw": "20", "value": 0x20}]

    def test_extended_index_escape(self):
        """index5 == 31 pulls the real field index from the following byte (+39)."""
        # 0xFC -> wide form, index5 = 31, so field = next_byte(0x05) + 39 = 44.
        fields = decode_generic("10#FC0500")["fields"]
        assert fields[0]["index"] == 44
        assert fields[0]["name"] == "STA_DAY_RAIN"

    def test_trailing_ascii_tail_is_stripped(self):
        """A comma-separated ASCII tail after the hex block is ignored."""
        result = decode_generic("10#DC01,junk,tail")
        assert "error" not in result
        assert result["field_names"] == ["STA_BAT"]

    def test_truncated_value_gives_none(self):
        """A wide-form entry with no value bytes decodes to value None."""
        fields = decode_generic("10#DC")["fields"]
        assert fields[0]["name"] == "STA_BAT"
        assert fields[0]["value"] is None
        assert fields[0]["raw"] == ""

    def test_truncated_dp_id_only(self):
        """A dangling dp_id byte with no header terminates cleanly."""
        result = decode_generic("11#18")
        assert "error" not in result
        assert result["fields"] == []

    def test_truncated_extended_header(self):
        """A wide-form escape header (index5>30) with no trailing byte stops."""
        # 0xFC -> wide form, index5 = 31 -> needs a following index byte that is absent.
        result = decode_generic("10#FC")
        assert "error" not in result
        assert result["fields"] == []
