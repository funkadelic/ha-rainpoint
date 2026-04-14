"""Tests for RainPoint device decoders."""

from tests.payload_samples import SAMPLE_HTV245_ASCII_PAYLOAD, SAMPLE_HTV245_TLV_PAYLOAD

from custom_components.rainpoint.api import decode_htv213frf_valve

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
        payload_bytes = bytes([
            0x18, 0xDC, 0x01,        # hub online
            0x19, 0xD8, 0x01,        # zone 1 open
            0x25, 0xAD, 0xE8, 0x03,  # zone 1 duration = 1000s (LE)
        ])
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
