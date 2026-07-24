"""Tests for the model-agnostic diagnostic decoder (decode_generic)."""

import custom_components.rainpoint.api.generic_decoder as generic_decoder_module
from custom_components.rainpoint.api import decode_generic
from tests.payload_samples import (
    CATALOG_ANCHOR_MODEL,
    SAMPLE_HTV145_CLOSED_PAYLOAD,
    SAMPLE_HTV245_TLV_PAYLOAD,
    SAMPLE_UNSUPPORTED_MULTI_SENSOR_PAYLOAD,
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

    def test_anchor_model_annotates_matching_fields(self):
        """Fields whose structural index matches a catalog dp entry get a catalog block.

        Asserted against the real committed snapshot, not a fixture: this is the
        end-to-end proof that the shipped catalog's own vocabulary parses.
        """
        result = decode_generic(SAMPLE_UNSUPPORTED_MULTI_SENSOR_PAYLOAD, model=CATALOG_ANCHOR_MODEL)
        by_name = {f["name"]: f for f in result["fields"]}

        assert by_name["STA_TEM"]["catalog"] == {
            "dp_port": 0,
            "data_type": "S16",
            "declared_width": 2,
            "signed": True,
            "port_number": 1,
            "width_mismatch": False,
        }
        # The unsigned single-byte fields annotate too, and disagree with
        # STA_TEM on both width and signedness - so a passing STA_TEM assertion
        # cannot be a constant-valued accident.
        assert by_name["STA_RH"]["catalog"]["declared_width"] == 1
        assert by_name["STA_RH"]["catalog"]["signed"] is False
        assert by_name["STA_RH"]["catalog"]["width_mismatch"] is False
        assert by_name["STA_BAT"]["catalog"]["width_mismatch"] is False
        assert by_name["STA_RSSI"]["catalog"]["width_mismatch"] is False

    def test_unmatched_field_stays_unannotated(self):
        """STA_ALARM is not declared by the anchor model and carries no catalog key."""
        result = decode_generic(SAMPLE_UNSUPPORTED_MULTI_SENSOR_PAYLOAD, model=CATALOG_ANCHOR_MODEL)
        by_name = {f["name"]: f for f in result["fields"]}

        assert "catalog" not in by_name["STA_ALARM"]

    def test_value_and_raw_are_byte_for_byte_identical_to_no_model(self):
        """Annotation never touches value/raw - only adds the catalog key."""
        no_model = decode_generic(SAMPLE_UNSUPPORTED_MULTI_SENSOR_PAYLOAD)
        annotated = decode_generic(SAMPLE_UNSUPPORTED_MULTI_SENSOR_PAYLOAD, model=CATALOG_ANCHOR_MODEL)

        for expected, actual in zip(no_model["fields"], annotated["fields"], strict=True):
            assert actual["name"] == expected["name"]
            assert actual["index"] == expected["index"]
            assert actual["dp_id"] == expected["dp_id"]
            assert actual["raw"] == expected["raw"]
            assert actual["value"] == expected["value"]

    def test_tlv_framing_matches_by_per_entry_dp_id(self, monkeypatch):
        """11# framing matches catalog entries on the per-entry dp_id, not the structural index."""
        fake_catalog = [{"dpCode": 0x18, "identity": "STA_BAT", "dpPort": 2, "dpDataType": "U8", "dpLen": 1}]
        monkeypatch.setattr(generic_decoder_module, "get_catalog_entry", lambda model, model_code=None: fake_catalog)

        result = decode_generic(SAMPLE_HTV245_TLV_PAYLOAD, model="FAKE_TLV_MODEL")
        fields = result["fields"]

        # Only the entry whose dp_id (0x18) matches dpCode is annotated.
        assert fields[0]["dp_id"] == 0x18
        assert fields[0]["catalog"]["dp_port"] == 2
        for other in fields[1:]:
            assert "catalog" not in other

    def test_ambiguous_dp_code_leaves_the_field_unannotated(self, monkeypatch):
        """Two catalog entries sharing a dpCode annotate nothing, rather than picking one.

        dpCode is the vendor's per-instance identifier and should be unique
        within a model, but the catalog is regenerated from an external API,
        so a duplicate must not silently resolve to whichever entry sorted
        first: that is how one zone's port metadata ends up on another zone's
        field.
        """
        duplicate_catalog = [
            {"dpCode": 0x18, "identity": "STA_BAT", "dpPort": 1, "dpDataType": "U8", "dpLen": 1},
            {"dpCode": 0x18, "identity": "STA_BAT", "dpPort": 2, "dpDataType": "U8", "dpLen": 1},
        ]
        monkeypatch.setattr(generic_decoder_module, "get_catalog_entry", lambda model, model_code=None: duplicate_catalog)

        result = decode_generic(SAMPLE_HTV245_TLV_PAYLOAD, model="FAKE_TLV_MODEL")

        assert all("catalog" not in field for field in result["fields"])

    def test_ambiguous_dp_code_also_guards_the_flat_framing(self, monkeypatch):
        """The 10# path keys off the same dpCode field, so it needs the same guard."""
        first_index = decode_generic(SAMPLE_UNSUPPORTED_MULTI_SENSOR_PAYLOAD)["fields"][0]["index"]
        duplicate_catalog = [
            {"dpCode": first_index, "identity": "STA_X", "dpPort": 1, "dpDataType": "U8", "dpLen": 1},
            {"dpCode": first_index, "identity": "STA_X", "dpPort": 2, "dpDataType": "U8", "dpLen": 1},
        ]
        monkeypatch.setattr(generic_decoder_module, "get_catalog_entry", lambda model, model_code=None: duplicate_catalog)

        result = decode_generic(SAMPLE_UNSUPPORTED_MULTI_SENSOR_PAYLOAD, model=CATALOG_ANCHOR_MODEL)

        assert all("catalog" not in field for field in result["fields"])

    def test_empty_catalog_degrades_annotation(self, monkeypatch):
        """A model that resolves to no catalog entry (empty catalog) never annotates."""
        monkeypatch.setattr(generic_decoder_module, "get_catalog_entry", lambda model, model_code=None: None)

        result = decode_generic(SAMPLE_UNSUPPORTED_MULTI_SENSOR_PAYLOAD, model=CATALOG_ANCHOR_MODEL)
        no_model = decode_generic(SAMPLE_UNSUPPORTED_MULTI_SENSOR_PAYLOAD)

        assert result == no_model

    def test_width_mismatch_true_for_mismatched_field(self, monkeypatch):
        """A catalog-declared width that disagrees with the decoded byte count is flagged."""
        mismatched_catalog = [{"dpCode": 31, "identity": "STA_BAT", "dpPort": 1, "dpDataType": "U16", "dpLen": 2}]
        monkeypatch.setattr(generic_decoder_module, "get_catalog_entry", lambda model, model_code=None: mismatched_catalog)

        result = decode_generic(SAMPLE_UNSUPPORTED_MULTI_SENSOR_PAYLOAD, model=CATALOG_ANCHOR_MODEL)
        by_name = {f["name"]: f for f in result["fields"]}

        assert by_name["STA_BAT"]["catalog"]["width_mismatch"] is True
        # value/raw stay authoritative even when the catalog disagrees on width.
        assert by_name["STA_BAT"]["value"] == 0x64
        assert by_name["STA_BAT"]["raw"] == "64"

    def test_width_mismatch_false_for_matched_field(self, monkeypatch):
        """A catalog-declared width that agrees with the decoded byte count is not flagged."""
        matching_catalog = [{"dpCode": 31, "identity": "STA_BAT", "dpPort": 1, "dpDataType": "U8", "dpLen": 1}]
        monkeypatch.setattr(generic_decoder_module, "get_catalog_entry", lambda model, model_code=None: matching_catalog)

        result = decode_generic(SAMPLE_UNSUPPORTED_MULTI_SENSOR_PAYLOAD, model=CATALOG_ANCHOR_MODEL)
        by_name = {f["name"]: f for f in result["fields"]}

        assert by_name["STA_BAT"]["catalog"]["width_mismatch"] is False

    def test_data_type_supplies_the_width_when_dplen_is_absent(self, monkeypatch):
        """dpDataType is the fallback width source for an entry with no usable dpLen.

        dpLen is authoritative when present, but a catalog written before dpLen
        was kept - or a vendor entry that omits it - must still be comparable.
        """
        no_len_catalog = [{"dpCode": 31, "identity": "STA_BAT", "dpPort": 1, "dpDataType": "U16"}]
        monkeypatch.setattr(generic_decoder_module, "get_catalog_entry", lambda model, model_code=None: no_len_catalog)

        result = decode_generic(SAMPLE_UNSUPPORTED_MULTI_SENSOR_PAYLOAD, model=CATALOG_ANCHOR_MODEL)
        by_name = {f["name"]: f for f in result["fields"]}

        # STA_BAT decodes to 1 byte here, so a declared 2 is a real mismatch.
        assert by_name["STA_BAT"]["catalog"]["declared_width"] == 2
        assert by_name["STA_BAT"]["catalog"]["width_mismatch"] is True

    def test_dplen_wins_over_a_disagreeing_data_type(self, monkeypatch):
        """Where dpLen and the type name disagree, dpLen is authoritative.

        The vendor's TD2 type really does appear at both 1 and 2 bytes in the
        live catalog, so trusting the name over the length would flag phantom
        mismatches on every one of those entries.
        """
        conflicting = [{"dpCode": 31, "identity": "STA_BAT", "dpPort": 1, "dpDataType": "TD2", "dpLen": 1}]
        monkeypatch.setattr(generic_decoder_module, "get_catalog_entry", lambda model, model_code=None: conflicting)

        result = decode_generic(SAMPLE_UNSUPPORTED_MULTI_SENSOR_PAYLOAD, model=CATALOG_ANCHOR_MODEL)
        by_name = {f["name"]: f for f in result["fields"]}

        assert by_name["STA_BAT"]["catalog"]["declared_width"] == 1
        assert by_name["STA_BAT"]["catalog"]["width_mismatch"] is False
        # TD2 declares no signedness at all.
        assert by_name["STA_BAT"]["catalog"]["signed"] is None

    def test_variable_length_type_never_flags_mismatch(self, monkeypatch):
        """A variable-length type (STRING, dpLen 0) degrades to width_mismatch=False.

        dpLen 0 is the vendor's own way of saying "no fixed width", so it must
        read as "cannot compare" rather than "declared zero bytes" - which
        would flag a mismatch against every field that decoded any bytes.
        """
        odd_catalog = [{"dpCode": 31, "identity": "STA_BAT", "dpPort": 1, "dpDataType": "STRING", "dpLen": 0}]
        monkeypatch.setattr(generic_decoder_module, "get_catalog_entry", lambda model, model_code=None: odd_catalog)

        result = decode_generic(SAMPLE_UNSUPPORTED_MULTI_SENSOR_PAYLOAD, model=CATALOG_ANCHOR_MODEL)
        by_name = {f["name"]: f for f in result["fields"]}

        assert by_name["STA_BAT"]["catalog"]["width_mismatch"] is False

    def test_non_string_data_type_never_flags_mismatch(self, monkeypatch):
        """A dp entry declaring neither dpLen nor a parseable dpDataType degrades cleanly."""
        odd_catalog = [{"dpCode": 31, "identity": "STA_BAT", "dpPort": 1, "dpDataType": None, "dpLen": None}]
        monkeypatch.setattr(generic_decoder_module, "get_catalog_entry", lambda model, model_code=None: odd_catalog)

        result = decode_generic(SAMPLE_UNSUPPORTED_MULTI_SENSOR_PAYLOAD, model=CATALOG_ANCHOR_MODEL)
        by_name = {f["name"]: f for f in result["fields"]}

        assert by_name["STA_BAT"]["catalog"]["width_mismatch"] is False

    def test_non_byte_aligned_data_type_never_flags_mismatch(self, monkeypatch):
        """A dpDataType bit count that is not a whole number of bytes is unparseable.

        Only reachable via the dpDataType fallback, since this entry declares
        no usable dpLen.
        """
        odd_catalog = [{"dpCode": 31, "identity": "STA_BAT", "dpPort": 1, "dpDataType": "S3", "dpLen": None}]
        monkeypatch.setattr(generic_decoder_module, "get_catalog_entry", lambda model, model_code=None: odd_catalog)

        result = decode_generic(SAMPLE_UNSUPPORTED_MULTI_SENSOR_PAYLOAD, model=CATALOG_ANCHOR_MODEL)
        by_name = {f["name"]: f for f in result["fields"]}

        assert by_name["STA_BAT"]["catalog"]["width_mismatch"] is False

    def test_non_width_digit_in_data_type_never_flags_mismatch(self, monkeypatch):
        """A dpDataType with a non-width digit is never misparsed as a byte width.

        A hypothetical "ENUM16" embeds a digit that would mean "16 possible
        states", not "16 bits". A loose digit-search would misparse it as a
        2-byte width and (since STA_BAT decodes to 1 byte here) incorrectly
        flag a mismatch. The anchored signedness-letter parse must reject it,
        so no mismatch is flagged.
        """
        odd_catalog = [{"dpCode": 31, "identity": "STA_BAT", "dpPort": 1, "dpDataType": "ENUM16", "dpLen": None}]
        monkeypatch.setattr(generic_decoder_module, "get_catalog_entry", lambda model, model_code=None: odd_catalog)

        result = decode_generic(SAMPLE_UNSUPPORTED_MULTI_SENSOR_PAYLOAD, model=CATALOG_ANCHOR_MODEL)
        by_name = {f["name"]: f for f in result["fields"]}

        assert by_name["STA_BAT"]["catalog"]["width_mismatch"] is False

    def test_annotation_failure_does_not_break_decode(self, monkeypatch):
        """A broken catalog lookup degrades to the unannotated shape, never raises."""

        def _boom(model, model_code=None):
            raise RuntimeError("catalog explosion")

        monkeypatch.setattr(generic_decoder_module, "get_catalog_entry", _boom)

        result = decode_generic(SAMPLE_UNSUPPORTED_MULTI_SENSOR_PAYLOAD, model=CATALOG_ANCHOR_MODEL)

        assert "error" not in result
        for field in result["fields"]:
            assert "catalog" not in field
