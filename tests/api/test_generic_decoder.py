"""Tests for the model-agnostic diagnostic decoder (decode_generic)."""

import hashlib
import logging
from pathlib import Path

import custom_components.rainpoint.api.decoders as decoders_module
import custom_components.rainpoint.api.generic_decoder as generic_decoder_module
import custom_components.rainpoint.api.utils as api_utils_module
from custom_components.rainpoint.api import decode_generic, is_ascii_declined
from tests.payload_samples import (
    CATALOG_ANCHOR_MODEL,
    HWS019WRF_V2_PAYLOAD,
    MOISTURE_FULL_ASCII_PAYLOAD,
    MOISTURE_FULL_HEX_PAYLOAD,
    SAMPLE_HTV145_CLOSED_PAYLOAD,
    SAMPLE_HTV245_ASCII_PAYLOAD,
    SAMPLE_HTV245_TLV_PAYLOAD,
    SAMPLE_UNSUPPORTED_MULTI_SENSOR_PAYLOAD,
)

# custom_components/rainpoint/api/decoders.py is the trusted reference the
# generic ASCII-framing decoder reads and must never edit. Pinned by
# whole-file digest rather than a per-function comparison. Legitimate
# hand-written decoder additions (most recently decode_hic801w) move this
# hash forward on purpose; the guard exists to catch decode_generic and its
# helpers reaching back into this file, not to freeze it.
_DECODERS_PY_PRE_PHASE_SHA256 = "1f321b80c890c6ddff634b24926cfcdb15befda625c87fac085719f6bcdb09ad"


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
        assert [f["dp_id"] for f in fields] == [24, 25, 26, 37, 38]


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

    def test_non_string_raw_is_tolerated(self):
        """A malformed cloud record can hand this a non-string value despite the
        type hint; the ASCII-detection check ahead of the hex path must not
        raise out of the function on that shape either."""
        result = decode_generic(None)  # type: ignore[arg-type]
        assert result["decoder"] == "generic-tlv"
        assert "error" in result

    def test_non_string_raw_is_not_logged_verbatim(self, caplog):
        """A list-shaped raw reaches the same except block via a different
        exception (AttributeError on .split rather than a TypeError on 'in'),
        and its contents must not appear in the diagnostic log line -- only
        the type name may, since raw is untrusted cloud-supplied data."""
        caplog.set_level(logging.DEBUG)

        result = decode_generic(["marker-should-not-be-logged", ";"])  # type: ignore[arg-type]

        assert "error" in result
        rainpoint_records = [r for r in caplog.records if r.name.startswith("custom_components.rainpoint")]
        assert len(rainpoint_records) == 1
        message = rainpoint_records[0].getMessage()
        assert "marker-should-not-be-logged" not in message
        assert "list" in message


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

    def test_tlv_single_index_single_candidate_annotates_from_committed_catalog(self):
        """A field whose structural index has exactly one catalog candidate annotates from it.

        Exercises _annotate_fields_with_catalog directly against the real
        get_catalog_entry, with no monkeypatched catalog - the fix is proven
        against real vendor data, not a fixture built to match the code's
        own assumption. HTV245FRF is a hand-written model, so decode_generic
        itself never reaches this step for it in production; that trust
        boundary is proven separately in tests/api/test_trust.py and is out
        of scope here.
        """
        result = decode_generic(SAMPLE_HTV245_TLV_PAYLOAD)
        fields = result["fields"]
        generic_decoder_module._annotate_fields_with_catalog(fields, "HTV245FRF", result["dp_id_prefixed"], "303")
        by_name = {f["name"]: f for f in fields}

        assert by_name["STA_BAT"]["catalog"] == {
            "dp_port": 0,
            "data_type": "U8",
            "declared_width": 1,
            "signed": False,
            "port_number": 2,
            "width_mismatch": False,
        }

    def test_tlv_duplicate_index_group_pairs_ascending_dp_id_to_ascending_dp_port(self):
        """Two fields sharing one structural index resolve to two distinct dpPorts.

        The lower dp_id (zone 1) pairs to the lower dpPort and the higher
        dp_id (zone 2) to the higher dpPort - the pairing
        tests/api/test_tlv_catalog_alignment.py validates against the
        trusted hand-written valve decoder's exact zone assignment. Getting
        the direction backwards would silently swap zone 1 and zone 2, so
        this checks distinct dpPort values, not merely that annotation ran.
        """
        result = decode_generic(SAMPLE_HTV245_TLV_PAYLOAD)
        fields = result["fields"]
        generic_decoder_module._annotate_fields_with_catalog(fields, "HTV245FRF", result["dp_id_prefixed"], "303")

        wkstate_fields = sorted((f for f in fields if f["name"] == "STA_WKSTATE"), key=lambda f: f["dp_id"])
        assert [f["catalog"]["dp_port"] for f in wkstate_fields] == [1, 2]

        duration_fields = sorted((f for f in fields if f["name"] == "STA_DURATION"), key=lambda f: f["dp_id"])
        assert [f["catalog"]["dp_port"] for f in duration_fields] == [1, 2]

    def test_tlv_field_with_no_catalog_match_carries_no_catalog_key(self):
        """A TLV field whose index the committed catalog does not declare stays unannotated.

        HCS026FRF's sole variant 317 declares STA_BAT, STA_REPTIME, STA_RH and
        STA_RSSI, so the STA_ILLUMINANCE the frame genuinely carries has no
        catalog counterpart. A real gap in the committed catalog rather than a
        monkeypatched one, and a durable one: the model has exactly one
        variant, so there is no second row a later catalog refresh could
        resolve the field against.

        This replaced HTV405FRF's missing dpCode 54 (STA_REPTIME), which
        RainPoint closed in the 2026-08 catalog refresh.
        """
        no_model = decode_generic(MOISTURE_FULL_HEX_PAYLOAD)
        annotated_fields = [dict(f) for f in no_model["fields"]]
        generic_decoder_module._annotate_fields_with_catalog(annotated_fields, "HCS026FRF", no_model["dp_id_prefixed"], "317")

        # Matched by name, not dp_id: this frame is not dp-id-prefixed, so every
        # field reports dp_id 0 and a dp_id-keyed lookup would collapse to
        # whichever field came last. STA_ILLUMINANCE appears exactly once here.
        illuminance = next(f for f in annotated_fields if f["name"] == "STA_ILLUMINANCE")
        expected = next(f for f in no_model["fields"] if f["name"] == "STA_ILLUMINANCE")

        assert "catalog" not in illuminance
        assert illuminance["value"] == expected["value"]
        assert illuminance["raw"] == expected["raw"]

    def test_group_count_mismatch_leaves_the_whole_group_unannotated(self, monkeypatch):
        """A group whose field count disagrees with the catalog's candidate count is refused entirely.

        Refusing the whole group - not pairing a prefix of it - is what
        stops a corrupted or reordered catalog from silently minting a wrong
        zone number for a subset of the group's fields.
        """
        mismatched_catalog = [
            {"dpCode": 30, "identity": "STA_WKSTATE", "dpPort": 1, "dpDataType": "U8", "dpLen": 1},
            {"dpCode": 30, "identity": "STA_WKSTATE", "dpPort": 2, "dpDataType": "U8", "dpLen": 1},
            {"dpCode": 30, "identity": "STA_WKSTATE", "dpPort": 3, "dpDataType": "U8", "dpLen": 1},
        ]
        monkeypatch.setattr(generic_decoder_module, "get_catalog_entry", lambda model, model_code=None: mismatched_catalog)

        result = decode_generic(SAMPLE_HTV245_TLV_PAYLOAD, model="FAKE_TLV_MODEL")
        wkstate_fields = [f for f in result["fields"] if f["name"] == "STA_WKSTATE"]

        assert len(wkstate_fields) == 2
        assert all("catalog" not in f for f in wkstate_fields)

    def test_group_with_unusable_dp_port_leaves_the_whole_group_unannotated(self, monkeypatch):
        """A multi-member group refuses annotation entirely when any candidate's dpPort is not a plain int."""
        unusable_port_catalog = [
            {"dpCode": 30, "identity": "STA_WKSTATE", "dpPort": 1, "dpDataType": "U8", "dpLen": 1},
            {"dpCode": 30, "identity": "STA_WKSTATE", "dpPort": "2", "dpDataType": "U8", "dpLen": 1},
        ]
        monkeypatch.setattr(generic_decoder_module, "get_catalog_entry", lambda model, model_code=None: unusable_port_catalog)

        result = decode_generic(SAMPLE_HTV245_TLV_PAYLOAD, model="FAKE_TLV_MODEL")
        wkstate_fields = [f for f in result["fields"] if f["name"] == "STA_WKSTATE"]

        assert all("catalog" not in f for f in wkstate_fields)

    def test_single_member_group_annotates_even_with_unusable_dp_port(self, monkeypatch):
        """A lone field at its index annotates even when the catalog's dpPort for it is unusable.

        A single-candidate group keeps the flat path's simple behaviour and
        does not require a usable dpPort, so a variant whose dpPort the
        RainPoint left unusable still gets its data-type and width annotation.
        """
        odd_port_catalog = [{"dpCode": 31, "identity": "STA_BAT", "dpPort": None, "dpDataType": "U8", "dpLen": 1}]
        monkeypatch.setattr(generic_decoder_module, "get_catalog_entry", lambda model, model_code=None: odd_port_catalog)

        result = decode_generic(SAMPLE_HTV245_TLV_PAYLOAD, model="FAKE_TLV_MODEL")
        by_name = {f["name"]: f for f in result["fields"]}

        assert by_name["STA_BAT"]["catalog"]["dp_port"] is None
        assert by_name["STA_BAT"]["catalog"]["declared_width"] == 1

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
        was kept - or a RainPoint entry that omits it - must still be comparable.
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

        RainPoint's TD2 type really does appear at both 1 and 2 bytes in the
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

        dpLen 0 is RainPoint's own way of saying "no fixed width", so it must
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


class TestDecodeGenericAscii:
    """The comma-and-semicolon ASCII framing: header rssi read, body declined."""

    def test_yields_one_header_derived_rssi_field(self):
        """The HTV245 ASCII sample decodes to exactly one STA_RSSI field at -84."""
        fields = decode_generic(SAMPLE_HTV245_ASCII_PAYLOAD)["fields"]

        assert len(fields) == 1
        assert fields[0]["name"] == "STA_RSSI"
        assert fields[0]["value"] == -84

    def test_synthetic_entry_has_null_provenance_keys(self):
        """index, dp_id and raw are all None: there is no byte-stream position behind a header-derived value."""
        field = decode_generic(SAMPLE_HTV245_ASCII_PAYLOAD)["fields"][0]

        assert field["index"] is None
        assert field["dp_id"] is None
        assert field["raw"] is None

    def test_result_shape(self):
        """field_names, dp_id_prefixed and the ascii_framed marker all match the documented ASCII result shape."""
        result = decode_generic(SAMPLE_HTV245_ASCII_PAYLOAD)

        assert result["field_names"] == ["STA_RSSI"]
        assert result["dp_id_prefixed"] is False
        assert result["ascii_framed"] is True

    def test_error_names_the_real_condition(self):
        """The error names the ASCII framing and never claims 'empty' or 'hex'."""
        error = decode_generic(SAMPLE_HTV245_ASCII_PAYLOAD)["error"]

        assert isinstance(error, str)
        assert "ASCII" in error
        assert "empty" not in error.lower()
        assert "hex" not in error.lower()

    def test_is_ascii_declined_true_for_ascii_result(self):
        """is_ascii_declined reads the marker back off a real ASCII decode."""
        assert is_ascii_declined(decode_generic(SAMPLE_HTV245_ASCII_PAYLOAD)) is True

    def test_is_ascii_declined_false_for_hex_result(self):
        """A hex decode never carries the marker."""
        assert is_ascii_declined(decode_generic("11#1FD801")) is False

    def test_is_ascii_declined_fails_closed_on_none_and_empty_dict(self):
        """A caller that has not run a decode yet gets False, not a raise."""
        assert is_ascii_declined(None) is False
        assert is_ascii_declined({}) is False

    def test_no_catalog_annotation_even_with_model(self):
        """The ASCII branch returns before the catalog block; model is inert here."""
        result = decode_generic(SAMPLE_HTV245_ASCII_PAYLOAD, model="HTV245FRF", model_code="303")

        assert "catalog" not in result["fields"][0]

    def test_moisture_full_sample_yields_its_header_rssi(self):
        """The second committed family (HCS021FRF) reads the same way: header rssi, decline."""
        result = decode_generic(MOISTURE_FULL_ASCII_PAYLOAD)

        assert len(result["fields"]) == 1
        assert result["fields"][0]["name"] == "STA_RSSI"
        assert result["fields"][0]["value"] == -73
        assert is_ascii_declined(result) is True

    def test_hws019wrf_v2_non_negative_rssi_yields_no_field_and_no_log(self, caplog):
        """The third committed family carries a real rssi=0 sample.

        Both hand-written ASCII decoders emit an _LOGGER.warning on exactly
        this condition, which would spam every poll for an affected device on
        the generic path. Pinned at DEBUG so the assertion covers every level,
        not only WARNING.
        """
        caplog.set_level(logging.DEBUG)

        result = decode_generic(HWS019WRF_V2_PAYLOAD)

        assert result["fields"] == []
        assert result["field_names"] == []
        assert result["ascii_framed"] is True
        assert "ASCII" in result["error"]
        rainpoint_records = [r for r in caplog.records if r.name.startswith("custom_components.rainpoint")]
        assert rainpoint_records == []

    def test_truncated_header_declines_without_raising(self):
        """A header with fewer than three comma parts still declines cleanly."""
        result = decode_generic("1,-84;body")

        assert "error" in result
        assert result["fields"] == []
        assert result["ascii_framed"] is True

    def test_non_integer_rssi_token_declines_without_raising(self):
        """A non-integer rssi token still declines cleanly."""
        result = decode_generic("1,x,1;body")

        assert "error" in result
        assert result["fields"] == []
        assert result["ascii_framed"] is True

    def test_non_ascii_body_still_routes_to_the_ascii_branch(self):
        """Routing reads only the pre-semicolon header, never the body's content."""
        result = decode_generic("1,-84,1;" + "éèê")

        assert result["ascii_framed"] is True
        assert result["fields"][0]["value"] == -84

    def test_hex_payload_with_ascii_tail_is_unaffected(self):
        """Asserted positively: a hex-with-tail payload decodes exactly as before."""
        result = decode_generic("10#AABBCC,1,2")

        assert "ascii_framed" not in result
        assert is_ascii_declined(result) is False
        assert result == {
            "decoder": "generic-tlv",
            "dp_id_prefixed": False,
            "fields": [{"name": "STA_ENERGY", "index": 18, "dp_id": 0, "raw": "bbcc", "value": 52411}],
            "field_names": ["STA_ENERGY"],
        }

    def test_empty_hex_body_error_wording_is_untouched(self):
        """The hex branch's own error message is not touched by this phase."""
        assert decode_generic("11#")["error"] == "empty or odd-length hex body"


class TestAsciiFramingNonRegression:
    """Source-level pins: the trusted decoders and the no-ordering-table rule."""

    def test_decoders_py_is_byte_identical_to_the_phase_base(self):
        """api/decoders.py is read for reference and never edited.

        Pinned by whole-file digest rather than relying on `git diff` alone,
        so a regression is caught by the test suite itself.
        """
        source = Path(generic_decoder_module.__file__).parent / "decoders.py"
        digest = hashlib.sha256(source.read_bytes()).hexdigest()

        assert digest == _DECODERS_PY_PRE_PHASE_SHA256

    def test_no_per_family_body_position_table_exists(self):
        """No ordering machinery ships, not even a declared-empty table.

        Checked as an absence over the two edited modules' own namespaces,
        not a repo-wide grep, so the explanatory comment elsewhere describing
        why no table exists cannot trip its own gate.
        """
        forbidden_fragments = ("BODY_ORDER", "FIELD_ORDER", "POSITION_TABLE", "ZONE_ORDER", "ORDERING_TABLE")

        for module in (generic_decoder_module, api_utils_module):
            offenders = [name for name in vars(module) if any(fragment in name.upper() for fragment in forbidden_fragments)]
            assert offenders == [], f"{module.__name__} defines: {offenders}"


def _hic801w_field_constant_comment() -> str:
    """Return the comment block directly above the HIC801W field constants.

    Walks back from the ``_HIC801W_FIELD_DURATION`` assignment over the
    contiguous run of comment lines immediately preceding it. Scoping the
    retired-clause check below to this block is what keeps it from failing
    on an unrelated future comment elsewhere in the file that happens to use
    the same ordinary phrase.
    """
    lines = Path(decoders_module.__file__).read_text().splitlines()
    anchor = next(i for i, line in enumerate(lines) if line.startswith("_HIC801W_FIELD_DURATION"))
    start = anchor
    while start > 0 and lines[start - 1].lstrip().startswith("#"):
        start -= 1
    assert start < anchor, "no comment block found above _HIC801W_FIELD_DURATION"
    return "\n".join(lines[start:anchor])


class TestHic801wFieldConstantsExcludeWkstate:
    """decode_hic801w's structural field constants never name field index 30.

    Placed here rather than in test_decoders.py because this module already
    reads decoders.py's own namespace structurally (see
    TestAsciiFramingNonRegression.test_no_per_family_body_position_table_exists
    above), and this guard follows the same "absence over the module's own
    namespace" shape rather than a repo-wide grep. STA_WKSTATE is field index
    30 in the HIC801W structural record; decode_hic801w must read no byte at
    that index, which is the fact the field-constant comment above
    _HIC801W_FIELD_DURATION states in prose. This test proves the fact
    structurally rather than trusting the comment's wording, so a future edit
    that quietly adds a work-state read (by adding a
    ``_HIC801W_FIELD_WKSTATE = 30`` constant, or by repointing an existing
    constant to 30) fails the suite regardless of what the comment still says.
    No positive wording of the comment is pinned here, so rewording it must
    not start failing these tests. The one text assertion below is an
    absence check on a single retired clause, scoped to this comment block.
    """

    def test_no_field_constant_equals_the_wkstate_index(self):
        """No _HIC801W_FIELD_* constant in the module equals index 30 (STA_WKSTATE)."""
        offenders = {
            name: value for name, value in vars(decoders_module).items() if name.startswith("_HIC801W_FIELD_") and value == 30
        }
        assert offenders == {}, f"a HIC801W field constant reads the STA_WKSTATE index: {offenders}"

    def test_wkstate_field_constant_name_is_absent(self):
        """The module defines no _HIC801W_FIELD_WKSTATE constant at all."""
        assert not hasattr(decoders_module, "_HIC801W_FIELD_WKSTATE")

    def test_retired_disclaiming_clause_is_gone(self):
        """The retired 'other model families' clause does not reappear in this comment.

        Guards the meaning of the comment without pinning its current
        wording: only the specific retired clause is checked for absence, not
        any positive text the comment currently carries. Scoped to the
        HIC801W field-constant block rather than the whole file, so an
        unrelated comment elsewhere that reuses the same ordinary phrase
        cannot fail this test for a reason that has nothing to do with it.
        """
        assert "other model families" not in _hic801w_field_constant_comment()
