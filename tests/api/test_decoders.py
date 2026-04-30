"""Tests for RainPoint device decoders."""

from custom_components.rainpoint.api import (
    decode_co2,
    decode_display,
    decode_flow_meter,
    decode_flowmeter,
    decode_hcs005frf,
    decode_hcs027arf,
    decode_htv213frf_valve,
    decode_hws019wrf_v2,
    decode_moisture_full,
    decode_moisture_simple,
    decode_pool,
    decode_pool_plus,
    decode_rain,
    decode_soil,
    decode_temp_hum,
    decode_temp_hum_full,
    decode_temphum,
    decode_unknown,
    decode_valve_hub,
)
from tests.payload_samples import (
    BASIC_HEX_PAYLOAD,
    HWS019WRF_V2_PAYLOAD,
    MOISTURE_FULL_ASCII_PAYLOAD,
    MOISTURE_FULL_HEX_PAYLOAD,
    MOISTURE_SIMPLE_HEX_PAYLOAD,
    RAIN_HEX_PAYLOAD,
    SAMPLE_HTV245_ASCII_PAYLOAD,
    SAMPLE_HTV245_TLV_PAYLOAD,
    VALVE_HUB_TLV_PAYLOAD,
)

# Expected top-level keys the decoder must return for an ASCII payload.
EXPECTED_KEYS = {"type", "zones", "rssi_dbm", "raw_bytes"}


class TestDecodeHtv213frfValve:
    """Tests for decode_htv213frf_valve (shared by HTV213FRF and HTV245FRF)."""

    # --- Seed tests (Phase 2) ---

    def test_ascii_payload_returns_dict(self):
        """Smoke test: ASCII payload decodes to a dict with expected top-level keys."""
        result = decode_htv213frf_valve(SAMPLE_HTV245_ASCII_PAYLOAD)
        assert isinstance(result, dict), f"Expected dict, got {type(result)}"
        missing = EXPECTED_KEYS - result.keys()
        assert not missing, f"Missing expected keys: {missing}"

    def test_ascii_payload_type_is_valve_hub(self):
        """The decoded type field identifies this as a valve hub device."""
        result = decode_htv213frf_valve(SAMPLE_HTV245_ASCII_PAYLOAD)
        assert result["type"] == "valve_hub", f"Expected type='valve_hub', got {result['type']!r}"

    def test_empty_payload_returns_error_dict(self):
        """Empty string payload returns a dict with an error key instead of raising."""
        result = decode_htv213frf_valve("")
        assert isinstance(result, dict)
        assert "error" in result

    def test_malformed_payload_returns_error_dict(self):
        """Completely invalid payload returns a dict with an error key."""
        result = decode_htv213frf_valve("not_a_valid_payload")
        assert isinstance(result, dict)
        assert "error" in result

    def test_rssi_negative_value_preserved(self):
        """Negative RSSI value (-84) is preserved in the decoded output."""
        result = decode_htv213frf_valve(SAMPLE_HTV245_ASCII_PAYLOAD)
        assert result["rssi_dbm"] == -84

    def test_rssi_non_negative_returns_none(self):
        """Non-negative RSSI triggers WR-03 clamping and returns None."""
        payload_positive_rssi = "1,10,1;0,149,0,0,0,0|0,6,0,0,0,0"
        result = decode_htv213frf_valve(payload_positive_rssi)
        assert result["rssi_dbm"] is None

    def test_rssi_zero_returns_none(self):
        """Zero RSSI is non-negative and returns None per WR-03."""
        payload_zero_rssi = "1,0,1;0,149,0,0,0,0|0,6,0,0,0,0"
        result = decode_htv213frf_valve(payload_zero_rssi)
        assert result["rssi_dbm"] is None

    def test_multiple_zones_parsed(self):
        """Payload with two pipe-separated zones produces two zone entries."""
        result = decode_htv213frf_valve(SAMPLE_HTV245_ASCII_PAYLOAD)
        zones = result["zones"]
        assert len(zones) == 2, f"Expected 2 zones, got {len(zones)}"
        for zone_key, zone_val in zones.items():
            assert isinstance(zone_val, dict), f"Zone {zone_key} should be a dict"

    # --- ASCII full-field assertions (Phase 3, COVR-01) ---

    def test_ascii_payload_asserts_all_fields(self):
        """ASCII payload decodes every field the integration exposes."""
        result = decode_htv213frf_valve(SAMPLE_HTV245_ASCII_PAYLOAD)

        assert result["type"] == "valve_hub"
        assert result["rssi_dbm"] == -84
        assert result["hub_online"] is True
        assert result["decoder"] == "htv213frf_ascii"
        assert result["tlv_raw"] == {}
        assert result["raw_bytes"] == SAMPLE_HTV245_ASCII_PAYLOAD.encode("ascii")

        # Two zones expected
        assert len(result["zones"]) == 2

        # Zone 1: state=149 (!=0 so open), duration=0
        zone1 = result["zones"][1]
        assert zone1["raw_zone_id"] == 0
        assert zone1["open"] is True
        assert zone1["duration_seconds"] == 0

        # Zone 2: state=6 (!=0 so open), duration=0
        zone2 = result["zones"][2]
        assert zone2["raw_zone_id"] == 0
        assert zone2["open"] is True
        assert zone2["duration_seconds"] == 0

    # --- TLV/hex path assertions (Phase 3, COVR-01) ---

    def test_tlv_payload_returns_valve_hub_type(self):
        """TLV (11#) payload decodes to dict with type='valve_hub'."""
        result = decode_htv213frf_valve(SAMPLE_HTV245_TLV_PAYLOAD)
        assert result["type"] == "valve_hub"

    def test_tlv_payload_decoder_field(self):
        """TLV payload decoder field is 'htv213frf_hex'."""
        result = decode_htv213frf_valve(SAMPLE_HTV245_TLV_PAYLOAD)
        assert result["decoder"] == "htv213frf_hex"

    def test_tlv_payload_zone_states(self):
        """TLV payload zone open/closed states and durations match expected values.

        Synthetic payload has:
        - Zone 1: open (0xD8 value 0x01), duration 60s (0xAD LE 0x3C00)
        - Zone 2: closed (0xD8 value 0x00), duration 0s
        """
        result = decode_htv213frf_valve(SAMPLE_HTV245_TLV_PAYLOAD)
        zones = result["zones"]

        assert len(zones) == 2

        zone1 = zones[1]
        assert zone1["open"] is True
        assert zone1["duration_seconds"] == 60
        assert zone1["state_raw"] == 1

        zone2 = zones[2]
        assert zone2["open"] is False
        assert zone2["duration_seconds"] == 0
        assert zone2["state_raw"] == 0

    def test_tlv_payload_hub_online(self):
        """TLV payload hub_online reflects the 0x18 DP with type 0xDC."""
        result = decode_htv213frf_valve(SAMPLE_HTV245_TLV_PAYLOAD)
        assert result["hub_online"] is True


class TestLittleEndianTripwire:
    """Regression test: 0xAD duration values MUST be decoded as little-endian.

    The HTV213FRF/HTV245FRF valve hub firmware encodes zone duration seconds
    with type byte 0xAD in little-endian byte order. All other TLV types use
    big-endian. This was a real bug -- do not revert.
    """

    def test_0xad_duration_decoded_as_little_endian(self):
        """0xAD duration bytes E8 03 = 1000 seconds (LE), NOT 59395 (BE).

        If someone removes the little-endian branch for 0xAD, this value
        will decode as int.from_bytes(b'\\xe8\\x03', 'big') = 59395 instead
        of the correct int.from_bytes(b'\\xe8\\x03', 'little') = 1000.
        """
        from custom_components.rainpoint.api.utils import _parse_tlv_payload

        # Construct a minimal TLV payload with one 0xAD-typed record:
        # DP ID 0x25 (zone 1 duration), type 0xAD, value bytes E8 03
        tlv_hex = "11#" + bytes([0x25, 0xAD, 0xE8, 0x03]).hex()
        result = _parse_tlv_payload(tlv_hex)

        assert 0x25 in result, f"DP 0x25 not found in TLV result: {result}"
        type_byte, value_int, raw_bytes = result[0x25]
        assert type_byte == 0xAD
        assert raw_bytes == b"\xe8\x03"
        # Little-endian: 0xE803 -> 0x03E8 = 1000
        # Big-endian would give: 0xE803 = 59395  <-- WRONG
        assert value_int == 1000, (
            f"0xAD duration decoded as {value_int}; expected 1000 (LE). "
            f"If you see 59395, the little-endian branch for 0xAD was removed."
        )

    def test_0xad_via_full_decoder_also_little_endian(self):
        """The full decoder's _decode_htv213frf_hex also respects 0xAD LE.

        Construct a minimal but valid HTV213FRF hex payload with:
        - DP 0x18 type 0xDC value 0x01 (hub online)
        - DP 0x19 type 0xD8 value 0x01 (zone 1 open)
        - DP 0x25 type 0xAD value E8 03 (zone 1 duration = 1000s LE)
        """
        payload_bytes = bytes(
            [
                0x18,
                0xDC,
                0x01,  # hub online
                0x19,
                0xD8,
                0x01,  # zone 1 open
                0x25,
                0xAD,
                0xE8,
                0x03,  # zone 1 duration = 1000s (LE)
            ]
        )
        raw = "11#" + payload_bytes.hex()
        result = decode_htv213frf_valve(raw)

        assert result["type"] == "valve_hub"
        assert result["hub_online"] is True
        assert 1 in result["zones"]
        zone1 = result["zones"][1]
        assert zone1["open"] is True
        # Little-endian: 0xE803 -> 1000; big-endian would give 59395
        assert zone1["duration_seconds"] == 1000, (
            f"Zone 1 duration is {zone1['duration_seconds']}; expected 1000 (LE). "
            f"59395 means the 0xAD little-endian branch was removed."
        )


class TestHtv213DpMapEdgeCases:
    """Defensive parsing edge cases for the HTV213FRF/HTV245FRF dp_map scan
    and zone extraction.
    """

    def test_duration_dp_with_wrong_type_defaults_to_zero(self):
        """A non-0xAD type at DP 0x24+N must not be misread as duration seconds.

        The documented duration DP type is 0xAD (2-byte little-endian seconds).
        If the firmware ever places a different type at the duration DP, the
        decoder should default duration_seconds to 0 rather than reinterpret a
        differently-typed value as a count of seconds.
        """
        # Hub online, zone 1 open, zone 1 "duration" sent with type 0xD8 (val_len=1, value=0x05)
        payload_bytes = bytes(
            [
                0x18,
                0xDC,
                0x01,
                0x19,
                0xD8,
                0x01,
                0x25,
                0xD8,
                0x05,  # wrong type for duration DP
            ]
        )
        raw = "11#" + payload_bytes.hex()
        result = decode_htv213frf_valve(raw)

        assert result["hub_online"] is True
        zone1 = result["zones"][1]
        assert zone1["open"] is True
        assert zone1["duration_seconds"] == 0, (
            f"Zone 1 duration is {zone1['duration_seconds']}; expected 0. "
            f"A non-0xAD value at the duration DP must not populate duration_seconds."
        )

    def test_truncated_known_type_record_is_skipped(self):
        """A known type byte with insufficient remaining buffer is skipped, and
        the i += 1 advance lets the scanner pick up valid records that follow.

        Two cases:
        1. A bare truncated 0xAD record (needs 2 value bytes, has 1) yields
           an empty dp_map.
        2. A truncated 0xB7 record (needs 4 value bytes, has 3) followed by a
           valid 0xDC record exercises the re-alignment path: the truncated
           branch fires at offset 0, the unknown branch absorbs one byte of
           drift at offset 1, and the success branch captures DP 0x11 at
           offset 2. A 2-byte 0xAD truncation cannot be used here because
           appending any 3+ trailing bytes would satisfy 0xAD's value length
           and short-circuit the truncated branch.
        """
        from custom_components.rainpoint.api.decoders import _scan_htv213_dp_map

        bare_truncated = bytes([0x10, 0xAD, 0x01])
        assert _scan_htv213_dp_map(bare_truncated) == {}, "Bare truncation should yield empty dp_map"

        with_trailing = bytes([0x10, 0xB7, 0x11, 0xDC, 0x05])
        dp_map = _scan_htv213_dp_map(with_trailing)
        assert 0x10 not in dp_map, f"Truncated DP 0x10 should not be captured; got {dp_map}"
        assert dp_map.get(0x11) == (0xDC, 0x05), f"Expected DP 0x11 = (0xDC, 0x05) after re-alignment; got {dp_map}"

    def test_unknown_type_byte_is_skipped(self):
        """An unrecognized type byte advances 1 byte and produces no dp entry."""
        from custom_components.rainpoint.api.decoders import _scan_htv213_dp_map

        # DP 0x10, type 0x00 (not in _HTV213_TYPE_LENGTHS), one trailing byte
        unknown = bytes([0x10, 0x00, 0x01])
        dp_map = _scan_htv213_dp_map(unknown)

        assert dp_map == {}, f"Unknown-type record should be skipped; got {dp_map}"


class TestDecodeMoistureFull:
    """Tests for decode_moisture_full (HCS021FRF) — hex and ASCII paths."""

    def test_hex_payload_fields(self):
        """Hex payload fields."""
        result = decode_moisture_full(MOISTURE_FULL_HEX_PAYLOAD)
        assert result["type"] == "moisture_full"
        assert result["rssi_dbm"] == -94
        assert result["moisture_percent"] == 31
        assert result["illuminance_lux"] == 163.2
        # 683/10=68.3F -> (68.3-32)*5/9 ≈ 20.17C
        assert abs(result["temperature_c"] - 20.17) < 0.05
        assert result["decoder"] == "hcs021frf_hex"

    def test_ascii_payload_fields(self):
        """Ascii payload fields."""
        result = decode_moisture_full(MOISTURE_FULL_ASCII_PAYLOAD)
        assert result["type"] == "moisture_full"
        assert result["rssi_dbm"] == -73
        assert result["moisture_percent"] == 70
        # 694/10=69.4F -> (69.4-32)*5/9 ≈ 20.78C
        assert abs(result["temperature_c"] - 20.78) < 0.05
        assert result["decoder"] == "hcs021frf_ascii"


class TestDecodeHws019wrfV2:
    """Tests for decode_hws019wrf_v2 — CSV/semicolon payload."""

    def test_readings_parsed(self):
        """Readings parsed."""
        result = decode_hws019wrf_v2(HWS019WRF_V2_PAYLOAD)
        assert result["type"] == "hws019wrf_v2"
        assert result["readings"]["temp"] == "707"
        assert result["readings"]["humidity"] == "42"
        assert result["readings"]["P"] == "9709"

    def test_malformed_flag_token_routes_to_error_path(self):
        """A non-digit flag token surfaces via the decoder's error path, not a partial list."""
        result = decode_hws019wrf_v2("1,abc,0;707(707/694/1)")
        assert result["type"] == "hws019wrf_v2"
        assert "flags" not in result
        assert "abc" in result["error"]
        assert "1,abc,0" in result["error"]

    def test_empty_flag_tokens_are_skipped(self):
        """Empty flag tokens (e.g. from a stray comma) are tolerated, not treated as malformed."""
        result = decode_hws019wrf_v2("1,,0;707(707/694/1)")
        assert result["type"] == "hws019wrf_v2"
        assert result["flags"] == [1, 0]


class TestDecodeValveHub:
    """Tests for decode_valve_hub (HTV0540FRF TLV payload)."""

    def test_hub_online_and_zone_state(self):
        """Hub online and zone state."""
        result = decode_valve_hub(VALVE_HUB_TLV_PAYLOAD)
        assert result["type"] == "valve_hub"
        assert result["hub_online"] is True
        assert 1 in result["zones"]
        zone1 = result["zones"][1]
        assert zone1["open"] is True
        # 0x012C little-endian = 300
        assert zone1["duration_seconds"] == 300


class TestDecodeRain:
    """Tests for decode_rain (HCS012ARF rain gauge)."""

    def test_rain_values(self):
        """Rain values."""
        result = decode_rain(RAIN_HEX_PAYLOAD)
        assert result["type"] == "rain"
        assert result["rain_last_hour_mm"] == 0.0
        assert result["rain_last_24h_mm"] == 187.0
        assert result["rain_last_7d_mm"] == 187.0
        assert result["rain_total_mm"] == 187.0


class TestDecodeMoistureSimple:
    """Tests for decode_moisture_simple (HCS026FRF)."""

    def test_moisture_and_rssi(self):
        """Moisture and rssi."""
        result = decode_moisture_simple(MOISTURE_SIMPLE_HEX_PAYLOAD)
        assert result["type"] == "moisture_simple"
        assert result["rssi_dbm"] == -58
        assert result["moisture_percent"] == 26


class TestBasicDecoders:
    """Tests for basic decoders that extract only type and RSSI."""

    def test_decode_flow_meter(self):
        """Decode flow meter."""
        result = decode_flow_meter(BASIC_HEX_PAYLOAD)
        assert result["type"] == "flowmeter"
        assert result["rssi"] is not None

    def test_decode_flowmeter_alias(self):
        """Decode flowmeter alias."""
        result = decode_flowmeter(BASIC_HEX_PAYLOAD)
        assert result["type"] == "flowmeter"
        assert result["rssi"] is not None

    def test_decode_pool_plus(self):
        """Decode pool plus."""
        # decode_pool_plus returns type="co2" (HCS0530THO pool/spa monitor)
        result = decode_pool_plus(BASIC_HEX_PAYLOAD)
        assert result["type"] == "co2"
        assert result["rssi"] is not None

    def test_decode_soil(self):
        """Decode soil."""
        result = decode_soil(BASIC_HEX_PAYLOAD)
        assert result["type"] == "soil"
        assert result["rssi"] is not None

    def test_decode_temp_hum(self):
        """Decode temp hum."""
        result = decode_temp_hum(BASIC_HEX_PAYLOAD)
        assert result["type"] == "temphum"
        assert result["rssi"] is not None

    def test_decode_temp_hum_full(self):
        """Decode temp hum full."""
        result = decode_temp_hum_full(BASIC_HEX_PAYLOAD)
        assert result["type"] == "temphum_full"
        assert result["rssi"] is not None

    def test_decode_co2(self):
        """Decode co2."""
        result = decode_co2(BASIC_HEX_PAYLOAD)
        assert result["type"] == "co2"
        assert result["rssi"] is not None

    def test_decode_display(self):
        """Decode display."""
        result = decode_display(BASIC_HEX_PAYLOAD)
        assert result["type"] == "display"
        assert result["rssi"] is not None

    def test_decode_temphum(self):
        """Decode temphum."""
        result = decode_temphum(BASIC_HEX_PAYLOAD)
        assert result["type"] == "temphum"
        assert result["rssi"] is not None

    def test_decode_pool(self):
        """Decode pool."""
        result = decode_pool(BASIC_HEX_PAYLOAD)
        assert result["type"] == "pool"
        assert result["rssi"] is not None


class TestDecodeUnknown:
    """Tests for decode_unknown — the catch-all fallback."""

    def test_valid_payload(self):
        """Valid payload."""
        result = decode_unknown(BASIC_HEX_PAYLOAD)
        assert result["type"] == "unknown"
        assert result["rssi"] == -80

    def test_non_parseable_payload(self):
        """Non parseable payload."""
        # A payload missing the '#' separator triggers the except branch.
        # decode_unknown handles it gracefully rather than raising.
        result = decode_unknown("garbage-no-separator")
        assert result["type"] == "unknown"
        # rssi is None when the payload could not be parsed
        assert result["rssi"] is None


class TestHcsDelegation:
    """Verify HCS stub decoders delegate to their real implementations (D-09)."""

    def test_hcs005frf_matches_moisture_simple(self):
        """decode_hcs005frf should produce the same output as decode_moisture_simple."""
        delegated = decode_hcs005frf(MOISTURE_SIMPLE_HEX_PAYLOAD)
        real = decode_moisture_simple(MOISTURE_SIMPLE_HEX_PAYLOAD)
        assert delegated == real

    def test_hcs027arf_matches_unknown(self):
        """decode_hcs027arf should produce the same output as decode_unknown."""
        delegated = decode_hcs027arf(BASIC_HEX_PAYLOAD)
        real = decode_unknown(BASIC_HEX_PAYLOAD)
        assert delegated == real
