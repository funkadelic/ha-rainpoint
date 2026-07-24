"""Tests for the model-agnostic diagnostic decoder (decode_generic)."""

import custom_components.rainpoint.api.generic_decoder as generic_decoder_module
from custom_components.rainpoint.api import decode_generic
from tests.payload_samples import (
    SAMPLE_HTV145_CLOSED_PAYLOAD,
    SAMPLE_HTV245_TLV_PAYLOAD,
)

# A synthetic 10# (flat) payload built for these tests: a compact-form
# STA_ALARM entry the seeded catalog does not know about, followed by four
# wide-form entries (STA_TEM, STA_RH, STA_BAT, STA_RSSI) whose structural
# indices match the "HCS777ARF" bootstrap seed committed in
# api/data/product_catalog.json.
SAMPLE_UNSUPPORTED_MULTI_SENSOR_PAYLOAD = "10#208500968832DC64E0C5"
SEEDED_CATALOG_MODEL = "HCS777ARF"


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


class TestDecodeGenericCatalogAnnotation:
    """Catalog enrichment: the model= parameter annotates matching fields."""

    def test_no_model_argument_stays_unannotated(self):
        """Back-compat: calling with no model produces today's shape, no catalog keys."""
        result = decode_generic(SAMPLE_UNSUPPORTED_MULTI_SENSOR_PAYLOAD)

        for field in result["fields"]:
            assert "catalog" not in field

    def test_unknown_model_degrades_to_unannotated(self):
        """A model absent from the catalog produces the exact no-model shape."""
        no_model = decode_generic(SAMPLE_UNSUPPORTED_MULTI_SENSOR_PAYLOAD)
        unknown_model = decode_generic(SAMPLE_UNSUPPORTED_MULTI_SENSOR_PAYLOAD, model="TOTALLY_UNKNOWN")

        assert unknown_model == no_model

    def test_seeded_model_annotates_matching_fields(self):
        """Fields whose structural index matches a catalog dp entry get a catalog block."""
        result = decode_generic(SAMPLE_UNSUPPORTED_MULTI_SENSOR_PAYLOAD, model=SEEDED_CATALOG_MODEL)
        by_name = {f["name"]: f for f in result["fields"]}

        assert by_name["STA_TEM"]["catalog"] == {
            "dp_port": 1,
            "data_type": "int16",
            "port_number": 1,
            "width_mismatch": False,
        }
        assert by_name["STA_RH"]["catalog"]["width_mismatch"] is False
        assert by_name["STA_BAT"]["catalog"]["width_mismatch"] is False
        assert by_name["STA_RSSI"]["catalog"]["width_mismatch"] is False

    def test_unmatched_field_stays_unannotated(self):
        """STA_ALARM has no entry in the seeded catalog and carries no catalog key."""
        result = decode_generic(SAMPLE_UNSUPPORTED_MULTI_SENSOR_PAYLOAD, model=SEEDED_CATALOG_MODEL)
        by_name = {f["name"]: f for f in result["fields"]}

        assert "catalog" not in by_name["STA_ALARM"]

    def test_value_and_raw_are_byte_for_byte_identical_to_no_model(self):
        """Annotation never touches value/raw - only adds the catalog key."""
        no_model = decode_generic(SAMPLE_UNSUPPORTED_MULTI_SENSOR_PAYLOAD)
        annotated = decode_generic(SAMPLE_UNSUPPORTED_MULTI_SENSOR_PAYLOAD, model=SEEDED_CATALOG_MODEL)

        for expected, actual in zip(no_model["fields"], annotated["fields"], strict=True):
            assert actual["name"] == expected["name"]
            assert actual["index"] == expected["index"]
            assert actual["dp_id"] == expected["dp_id"]
            assert actual["raw"] == expected["raw"]
            assert actual["value"] == expected["value"]

    def test_tlv_framing_matches_by_per_entry_dp_id(self, monkeypatch):
        """11# framing matches catalog entries on the per-entry dp_id, not the structural index."""
        fake_catalog = [{"dpCode": 0x18, "identity": "STA_BAT", "dpPort": 2, "dpDataType": "uint8", "portNumber": 2}]
        monkeypatch.setattr(generic_decoder_module, "get_catalog_entry", lambda model: fake_catalog)

        result = decode_generic(SAMPLE_HTV245_TLV_PAYLOAD, model="FAKE_TLV_MODEL")
        fields = result["fields"]

        # Only the entry whose dp_id (0x18) matches dpCode is annotated.
        assert fields[0]["dp_id"] == 0x18
        assert fields[0]["catalog"]["dp_port"] == 2
        for other in fields[1:]:
            assert "catalog" not in other
