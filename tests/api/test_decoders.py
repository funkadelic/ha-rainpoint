"""Tests for RainPoint device decoders."""

import logging
import traceback

import pytest

from custom_components.rainpoint.api import (
    decode_co2,
    decode_display,
    decode_flow_meter,
    decode_flowmeter,
    decode_generic,
    decode_hcs005frf,
    decode_hic801w,
    decode_htv145frf,
    decode_htv210b,
    decode_htv210b_dp_state,
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
    get_catalog_entry,
    has_bluetooth_control_identity,
    is_hand_written_model,
)
from custom_components.rainpoint.api.decoders import _hic801w_stations_from_mask
from custom_components.rainpoint.api.utils import _parse_entries, _parse_rainpoint_payload
from tests.payload_samples import (
    BASIC_HEX_PAYLOAD,
    HWS019WRF_V2_PAYLOAD,
    MOISTURE_FULL_ASCII_PAYLOAD,
    MOISTURE_FULL_HEX_PAYLOAD,
    MOISTURE_SIMPLE_HEX_PAYLOAD,
    MOISTURE_SIMPLE_SECOND_CAPTURE_PAYLOAD,
    RAIN_HEX_PAYLOAD,
    SAMPLE_HIC801W_ALL_FRAMES,
    SAMPLE_HIC801W_REPORTER_FRAMES,
    SAMPLE_HIC801W_SECOND_UNIT_FRAMES,
    SAMPLE_HIC801W_STATION3_PAYLOAD,
    SAMPLE_HTV113_IDLE_PAYLOAD,
    SAMPLE_HTV145_CLOSED_PAYLOAD,
    SAMPLE_HTV145_OPEN_PAYLOAD,
    SAMPLE_HTV210B_DP_CLOSE_STATE,
    SAMPLE_HTV210B_DP_OPEN_60S_STATE,
    SAMPLE_HTV210B_DP_OPEN_120S_STATE,
    SAMPLE_HTV210B_TLV_PAYLOAD,
    SAMPLE_HTV245_ASCII_PAYLOAD,
    SAMPLE_HTV245_FULL_IDLE_PAYLOAD,
    SAMPLE_HTV245_FULL_ZONE2_ACTIVE_PAYLOAD,
    SAMPLE_HTV245_TLV_PAYLOAD,
    SAMPLE_HTV345_TLV_PAYLOAD,
    SAMPLE_HTV405_TLV_PAYLOAD,
    VALVE_HUB_TLV_PAYLOAD,
)

# Expected top-level keys the decoder must return for an ASCII payload.
EXPECTED_KEYS = {"type", "zones", "rssi_dbm", "raw_bytes"}


class TestDecodeHtv213frfValve:
    """Tests for decode_htv213frf_valve (shared by HTV213FRF and HTV245FRF)."""

    # --- Seed tests ---

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
        """Non-negative RSSI is rejected as out of range and returns None."""
        payload_positive_rssi = "1,10,1;0,149,0,0,0,0|0,6,0,0,0,0"
        result = decode_htv213frf_valve(payload_positive_rssi)
        assert result["rssi_dbm"] is None

    def test_rssi_zero_returns_none(self):
        """Zero RSSI is non-negative and returns None."""
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

    # --- ASCII full-field assertions ---

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

    # --- TLV/hex path assertions ---

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

    # --- HTV405FRF (4-zone valve, reuses the HTV213/245 hex decoder) ---

    def test_htv405_payload_decodes_four_zones(self):
        """Real HTV405FRF (11#) payload decodes to four idle zones with the hub online."""
        result = decode_htv213frf_valve(SAMPLE_HTV405_TLV_PAYLOAD)

        assert result["type"] == "valve_hub"
        assert result["decoder"] == "htv213frf_hex"
        assert result["hub_online"] is True

        zones = result["zones"]
        assert set(zones) == {1, 2, 3, 4}
        for zone in zones.values():
            assert zone["open"] is False
            assert zone["duration_seconds"] == 0
            assert zone["state_raw"] == 0

    def test_htv345_payload_with_zone_dp_is_online(self):
        """HTV345FRF payloads with DP 0x19 but no hub DP are treated as online."""
        result = decode_htv213frf_valve(SAMPLE_HTV345_TLV_PAYLOAD)

        assert result["decoder"] == "htv213frf_hex"
        assert result["hub_online"] is True
        assert result["hub_state_raw"] is None
        assert result["zones"][1]["open"] is False
        assert result["zones"][2]["open"] is False
        assert result["zones"][3]["open"] is False

    # --- RSSI (0x17/0xE1 header record, not at a fixed offset) ---

    def test_full_frame_reports_real_rssi_from_header_record(self):
        """RSSI comes from the byte after the 0x17/0xE1 header, not the constant header byte.

        The two real captures differ only in signal (byte after 0xE1): 0xDB=-37 and
        0xD9=-39. Reading b[1] would return the constant 0xE1 header (a bogus -31).
        """
        assert decode_htv213frf_valve(SAMPLE_HTV245_FULL_IDLE_PAYLOAD)["rssi_dbm"] == -37
        assert decode_htv213frf_valve(SAMPLE_HTV245_FULL_ZONE2_ACTIVE_PAYLOAD)["rssi_dbm"] == -39

    def test_rssi_found_when_header_record_is_not_first(self):
        """The header record is located by signature, so a reordered stream still resolves RSSI."""
        # HTV345FRF frame whose leading records precede the 0x17/0xE1 header (0xCA -> -54).
        assert decode_htv213frf_valve(SAMPLE_HTV345_TLV_PAYLOAD)["rssi_dbm"] == -54

    def test_rssi_absent_when_no_header_record(self):
        """A frame with no 0x17/0xE1 header yields rssi_dbm None rather than a garbage value."""
        result = decode_htv213frf_valve(SAMPLE_HTV245_TLV_PAYLOAD)
        assert result["rssi_dbm"] is None

    def test_rssi_ignores_0x17e1_collision_inside_value_bytes(self):
        """A 0x17/0xE1 pair inside a value (not followed by 0x00) is skipped for the real header.

        The leading 0x9F record carries value bytes 17 E1 05 42: a false 0x17/0xE1
        pair whose fourth byte is 0x42, not the header's trailing 0x00. The decoder
        must skip it and resolve RSSI from the genuine 17E1CA00 header (0xCA -> -54).
        """
        raw = "11#2B9F17E1054217E1CA0018DC0119D800FEFF0FEC4BCB19"
        assert decode_htv213frf_valve(raw)["rssi_dbm"] == -54

    def test_rssi_survives_a_non_zero_phy_byte(self):
        """A header whose fourth byte is a real PHY, not padding, still yields RSSI.

        The captured HTV210B frame carries 17e1b401: 0xb4 is -76 dBm, which the
        RainPoint app showed for that device, and the 0x01 is the PHY. Matching the
        fourth byte against 0x00 voided the reading on any frame reporting a
        non-zero PHY, and the catalog declares this field two bytes wide on
        HTV213FRF and HTV405FRF, which share this decoder.
        """
        assert decode_htv213frf_valve(SAMPLE_HTV210B_TLV_PAYLOAD)["rssi_dbm"] == -76

    def test_rssi_rejects_a_positive_dbm_candidate(self):
        """A 0x17/0xE1 pair whose dBm byte is not negative is not the header.

        0x05 would be +5 dBm, which no radio reports, so the pair belongs to
        some other record's value bytes.
        """
        assert decode_htv213frf_valve("11#17E10500FEFF0FEC4BCB19")["rssi_dbm"] is None

    def test_rssi_rejects_an_unsupported_phy(self):
        """A negative dBm paired with a PHY no capture has shown is not accepted.

        0xb4 would be a valid -76, so this pins the PHY bound on its own rather
        than through the sign guard. Widening it for a future capture then has to
        be a deliberate change to both the bound and this test.
        """
        assert decode_htv213frf_valve("11#17E1B402FEFF0FEC4BCB19")["rssi_dbm"] is None

    # --- Battery (STA_BAT record on real hex frames) ---

    def test_full_frame_reports_battery_percent(self):
        """A real full HTV245FRF hex frame surfaces battery_percent from its STA_BAT record."""
        result = decode_htv213frf_valve(SAMPLE_HTV245_FULL_IDLE_PAYLOAD)
        assert result["battery_percent"] == 100

    def test_full_frame_zone2_active_still_reports_full_battery(self):
        """The July 4 capture (zone 2 mid-run) reads 100% battery and a running zone 2."""
        result = decode_htv213frf_valve(SAMPLE_HTV245_FULL_ZONE2_ACTIVE_PAYLOAD)
        assert result["battery_percent"] == 100
        assert result["zones"][2]["duration_seconds"] == 2940

    def test_trailing_report_time_header_does_not_drive_battery(self):
        """Rewriting the trailing STA_REPTIME header leaves battery untouched.

        The previous extraction read these two bytes as the battery word, so
        this edit used to move the reading from 100% to 50%.
        """
        raw = SAMPLE_HTV245_FULL_IDLE_PAYLOAD.replace("FEFF0F", "FEFA0F")
        result = decode_htv213frf_valve(raw)
        assert result["battery_percent"] == 100

    def test_uncorroborated_flag_yields_no_battery_percent(self):
        """A STA_BAT flag with no known charge level is reported as no reading.

        The raw flag is still surfaced, as it is on every other decoder: with
        no percentage to show it is the only remaining evidence of the frame's
        battery state.
        """
        raw = SAMPLE_HTV245_FULL_IDLE_PAYLOAD.replace("18DC01", "18DC03")
        result = decode_htv213frf_valve(raw)
        assert "battery_percent" not in result
        assert result["battery_flag"] == 3
        assert result["zones"]

    def test_full_frame_surfaces_the_raw_flag(self):
        """A healthy frame reports both the flag and the percentage."""
        result = decode_htv213frf_valve(SAMPLE_HTV245_FULL_IDLE_PAYLOAD)
        assert result["battery_flag"] == 1
        assert result["battery_percent"] == 100

    def test_ascii_payload_has_no_battery_percent(self):
        """The ASCII firmware format carries no STA_BAT record, so the key is absent."""
        result = decode_htv213frf_valve(SAMPLE_HTV245_ASCII_PAYLOAD)
        assert "battery_percent" not in result

    def test_short_frame_reads_battery_from_its_sta_bat_record(self):
        """A frame with no report-time tail still carries STA_BAT and reports it."""
        result = decode_htv213frf_valve(SAMPLE_HTV245_TLV_PAYLOAD)
        assert result["battery_percent"] == 100

    # --- Report time ---

    def test_full_frame_reports_device_wall_clock(self):
        """The trailing STA_REPTIME record unpacks to the device's wall clock."""
        result = decode_htv213frf_valve(SAMPLE_HTV245_FULL_IDLE_PAYLOAD)
        assert result["report_time"] == "2026-07-25T07:00:02"
        assert result["report_time_raw"] == 0x19F27002

    def test_zone2_capture_unpacks_to_its_known_capture_date(self):
        """The capture taken on July 4 unpacks to July 4.

        This is the evidence for the 2020 year base: a base of 2000 would put
        the same frame in 2006.
        """
        result = decode_htv213frf_valve(SAMPLE_HTV245_FULL_ZONE2_ACTIVE_PAYLOAD)
        assert result["report_time"] == "2026-07-04T17:40:51"

    def test_frame_without_report_time_omits_the_keys(self):
        """A frame with no STA_REPTIME record carries neither report-time key."""
        result = decode_htv213frf_valve(SAMPLE_HTV245_TLV_PAYLOAD)
        assert "report_time" not in result
        assert "report_time_raw" not in result


class TestDecodeHtv145frf:
    """Tests for decode_htv145frf (single-outlet WiFi water timer, 10# compact format)."""

    def test_closed_payload_zone_idle(self):
        """Real closed-state payload: hub online, zone 1 closed, duration 0s."""
        result = decode_htv145frf(SAMPLE_HTV145_CLOSED_PAYLOAD)

        assert result["type"] == "valve_hub"
        assert result["decoder"] == "htv145frf_hex"
        assert result["hub_online"] is True
        assert result["hub_state_raw"] == 0x01

        zones = result["zones"]
        assert set(zones) == {1}
        assert zones[1]["open"] is False
        assert zones[1]["duration_seconds"] == 0
        assert zones[1]["state_raw"] == 0x00

    def test_open_payload_zone_running(self):
        """Real open-state payload: zone 1 open (0x21), duration 1200s (20 min)."""
        result = decode_htv145frf(SAMPLE_HTV145_OPEN_PAYLOAD)

        assert result["decoder"] == "htv145frf_hex"
        assert result["hub_online"] is True

        zone = result["zones"][1]
        assert zone["open"] is True
        assert zone["state_raw"] == 0x21
        assert zone["duration_seconds"] == 1200

    def test_rssi_is_signed_dbm(self):
        """byte[1] of the payload is the signed-dBm RSSI."""
        assert decode_htv145frf(SAMPLE_HTV145_CLOSED_PAYLOAD)["rssi_dbm"] == -68
        assert decode_htv145frf(SAMPLE_HTV145_OPEN_PAYLOAD)["rssi_dbm"] == -62

    def test_ff_terminator_stops_before_trailing_timestamp(self):
        """Parsing stops at 0xFF, so the trailing device timestamp is not misread."""
        # A bogus type byte after the 0xFF terminator must not create a zone.
        raw = "10#E1BC00DC01D80020B700000000AD00009F95110000FF0FD8FFFFFF"
        result = decode_htv145frf(raw)
        assert set(result["zones"]) == {1}
        assert result["zones"][1]["state_raw"] == 0x00

    def test_unknown_type_byte_realigns(self):
        """An unrecognized type byte advances 1 byte so parsing re-aligns."""
        # AA is unknown (+1), then DC01 hub online, D800 zone closed, FF terminator.
        result = decode_htv145frf("10#AADC01D800FF")
        assert result["hub_online"] is True
        assert result["zones"][1]["open"] is False

    def test_truncated_record_stops(self):
        """A record whose value runs past the payload end stops the scan."""
        # DC01 hub online, then 9F (needs 4 value bytes) with only 2 remaining.
        result = decode_htv145frf("10#DC019F9511")
        assert result["hub_online"] is True
        assert result["zones"] == {}

    def test_no_zone_marker_yields_empty_zones(self):
        """A payload with no 0xD8 marker reports no zones but still reads hub state."""
        # DC01 hub online only, stream ends without a 0xFF terminator.
        result = decode_htv145frf("10#DC01")
        assert result["hub_online"] is True
        assert result["zones"] == {}

    def test_malformed_payload_returns_error_dict(self):
        """A non-hex payload is caught and returned as a safe error dict."""
        result = decode_htv145frf("10#not_hex")
        assert result["type"] == "valve_hub"
        assert result["decoder"] == "htv145frf_error"
        assert result["zones"] == {}
        assert "error" in result

    def test_empty_payload_returns_error_dict(self):
        """A payload missing the '#' separator is handled gracefully."""
        result = decode_htv145frf("")
        assert result["decoder"] == "htv145frf_error"
        assert result["zones"] == {}

    def test_non_10_prefix_payload_is_rejected(self):
        """A 11# TLV payload is rejected before scanning, so bytes that coincide
        with HTV145 markers cannot fabricate hub-online or valve state."""
        # DC01 / D821 would read as hub-online + zone-1-open if scanned as markers.
        result = decode_htv145frf("11#DC01D82100")
        assert result["decoder"] == "htv145frf_error"
        assert result["zones"] == {}
        assert result.get("hub_online") is not True


class TestDecodeHtv113frf:
    """HTV113FRF (issue #64) shares the HTV145FRF single-outlet 10# marker format,
    so it is decoded by the same decode_htv145frf function."""

    def test_idle_payload_zone_closed(self):
        """Real idle payload: zone 1 closed (0x00), duration 0s, RSSI -63 dBm."""
        result = decode_htv145frf(SAMPLE_HTV113_IDLE_PAYLOAD)

        assert result["type"] == "valve_hub"
        assert result["decoder"] == "htv145frf_hex"
        assert result["rssi_dbm"] == -63

        zones = result["zones"]
        assert set(zones) == {1}
        assert zones[1]["open"] is False
        assert zones[1]["state_raw"] == 0x00
        assert zones[1]["duration_seconds"] == 0

    def test_hub_online_from_0x03_status(self):
        """HTV113 reports 0xDC status 0x03. Bit 0 is the online flag, so the valve
        entity must stay available (valve.py gates availability on hub_online)."""
        result = decode_htv145frf(SAMPLE_HTV113_IDLE_PAYLOAD)

        assert result["hub_state_raw"] == 0x03
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
    """Tests for decode_moisture_full (HCS021FRF): hex and ASCII paths."""

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

    def test_hex_battery_and_report_time(self):
        """Battery reads the STA_BAT record and the frame's own clock is decoded."""
        result = decode_moisture_full(MOISTURE_FULL_HEX_PAYLOAD)
        assert result["battery_flag"] == 1
        assert result["battery_percent"] == 100
        assert result["report_time"] == "2026-03-27T18:35:58"

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
    """Tests for decode_hws019wrf_v2: CSV/semicolon payload."""

    def test_readings_parsed(self):
        """Readings parsed."""
        result = decode_hws019wrf_v2(HWS019WRF_V2_PAYLOAD)
        assert result["type"] == "hws019wrf_v2"
        assert result["readings"]["temp"] == "707"
        assert result["readings"]["humidity"] == "42"
        assert result["readings"]["P"] == "9709"

    def test_missing_separator_routes_to_error_path(self):
        """A payload with no ';' separator must surface via the error path, not return empty readings."""
        result = decode_hws019wrf_v2("1,0,1")
        assert result["type"] == "hws019wrf_v2"
        assert "readings" not in result
        assert "flags" not in result
        assert "';'" in result["error"]
        assert "1,0,1" in result["error"]

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

    def test_battery_and_report_time(self):
        """Battery reads the STA_BAT record and the frame's own clock is decoded.

        The gauge nests its rain readings in extended-escape records, so this
        also covers the walk reaching STA_BAT past several of them.
        """
        result = decode_rain(RAIN_HEX_PAYLOAD)
        assert result["battery_flag"] == 1
        assert result["battery_percent"] == 100
        assert result["report_time"] == "2026-03-27T17:00:04"


class TestDecodeMoistureSimple:
    """Tests for decode_moisture_simple (HCS026FRF)."""

    def test_moisture_and_rssi(self):
        """Moisture and rssi."""
        result = decode_moisture_simple(MOISTURE_SIMPLE_HEX_PAYLOAD)
        assert result["type"] == "moisture_simple"
        assert result["rssi_dbm"] == -58
        assert result["moisture_percent"] == 26

    def test_battery_comes_from_the_sta_bat_record(self):
        """Battery reads the flag byte, not the trailing report-time header."""
        result = decode_moisture_simple(MOISTURE_SIMPLE_HEX_PAYLOAD)
        assert result["battery_flag"] == 1
        assert result["battery_percent"] == 100

    def test_report_time_from_second_capture(self):
        """The 2026-07-29 capture unpacks to the moment it was pulled."""
        result = decode_moisture_simple(MOISTURE_SIMPLE_SECOND_CAPTURE_PAYLOAD)
        assert result["moisture_percent"] == 37
        assert result["rssi_dbm"] == -60
        assert result["report_time"] == "2026-07-29T12:19:33"

    def test_rewritten_report_time_header_leaves_battery_alone(self):
        """Battery no longer moves when the trailing type header changes.

        Under the previous fixed-offset read this edit moved the reported
        battery from 100% to 50%.
        """
        raw = MOISTURE_SIMPLE_HEX_PAYLOAD.replace("FF0F", "FF0A")
        result = decode_moisture_simple(raw)
        assert result["battery_percent"] == 100


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
    """Tests for decode_unknown: the catch-all fallback."""

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
    """Verify HCS stub decoders delegate to their real implementations."""

    def test_hcs005frf_matches_moisture_simple(self):
        """decode_hcs005frf should produce the same output as decode_moisture_simple."""
        delegated = decode_hcs005frf(MOISTURE_SIMPLE_HEX_PAYLOAD)
        real = decode_moisture_simple(MOISTURE_SIMPLE_HEX_PAYLOAD)
        assert delegated == real


class TestHtv213frfAsciiErrorBranches:
    """Cover ASCII-format error/guard branches inside _decode_htv213frf_ascii.

    These all enter via the public wrapper decode_htv213frf_valve, which
    catches the inner re-raise and returns an error dict.
    """

    def test_ascii_missing_semicolon_returns_error_dict(self):
        """Comma+pipe but no semicolon routes to ASCII then raises 'missing semicolon'."""
        result = decode_htv213frf_valve("1,2,3|4,5,6")
        assert result["decoder"] == "htv213frf_error"
        assert "missing semicolon" in result["error"]

    def test_ascii_short_header_returns_error_dict(self):
        """Header with fewer than 3 comma-separated values triggers the header guard."""
        result = decode_htv213frf_valve("1,2;0,0,0,0,0,0")
        assert result["decoder"] == "htv213frf_error"
        assert "header" in result["error"].lower()

    def test_ascii_empty_zone_section_is_skipped(self):
        """An empty zone section (consecutive '|') is silently skipped, not fatal."""
        result = decode_htv213frf_valve("1,-84,1;0,149,0,0,0,0||0,6,0,0,0,0")
        assert result["decoder"] == "htv213frf_ascii"
        # Two non-empty zones, the empty one between them was skipped.
        assert len(result["zones"]) == 2

    def test_ascii_short_zone_is_warned_and_skipped(self):
        """A zone with fewer than 6 fields is logged and skipped, not fatal."""
        result = decode_htv213frf_valve("1,-84,1;0,149,0|0,6,0,0,0,0")
        assert result["decoder"] == "htv213frf_ascii"
        # Only the well-formed zone survives.
        assert len(result["zones"]) == 1


class TestHtv213frfHexErrorBranches:
    """Cover hex-format error/guard branches inside _decode_htv213frf_hex
    and the _extract_htv213_hub_state / zone helpers.
    """

    def test_hex_invalid_hex_returns_error_dict(self):
        """Non-hex characters after '11#' surface through the wrapper as an error dict."""
        result = decode_htv213frf_valve("11#zz")
        assert result["decoder"] == "htv213frf_error"
        assert "non-hex" in result["error"].lower() or "hexadecimal" in result["error"].lower()

    def test_hex_missing_hub_state_and_zone_1_dp_is_offline(self):
        """Hex payload without DP 0x18 or 0x19 yields hub_online=False."""
        # Empty payload: parses to empty bytes, no DPs -> 0x18 absent.
        result = decode_htv213frf_valve("11#")
        assert result["decoder"] == "htv213frf_hex"
        assert result["hub_online"] is False
        assert result["hub_state_raw"] is None

    def test_hex_missing_hub_state_dp_with_zone_1_dp_is_online(self):
        """DP 0x19 presence is enough to mark the hub online when DP 0x18 is absent."""
        payload = bytes([0x19, 0xD8, 0x01]).hex()
        result = decode_htv213frf_valve("11#" + payload)
        assert result["decoder"] == "htv213frf_hex"
        assert result["hub_online"] is True
        assert result["hub_state_raw"] is None
        assert result["zones"][1]["open"] is True

    def test_hex_hub_state_dp_with_wrong_type_is_ignored(self):
        """DP 0x18 with a type other than 0xDC and no 0x19 yields hub_online=False."""
        # DP 0x18, type 0xD8 (zone-state type, not hub-state type), value 0x01.
        payload = bytes([0x18, 0xD8, 0x01]).hex()
        result = decode_htv213frf_valve("11#" + payload)
        assert result["decoder"] == "htv213frf_hex"
        assert result["hub_online"] is False
        # The raw value is still passed back for diagnostic visibility.
        assert result["hub_state_raw"] == 0x01

    def test_hex_wrong_hub_state_type_with_zone_1_dp_is_online(self):
        """DP 0x19 presence marks the hub online even when DP 0x18 has the wrong type."""
        payload = bytes([0x18, 0xD8, 0x01, 0x19, 0xD8, 0x01]).hex()
        result = decode_htv213frf_valve("11#" + payload)

        assert result["decoder"] == "htv213frf_hex"
        assert result["hub_online"] is True
        assert result["hub_state_raw"] == 0x01
        assert result["zones"][1]["open"] is True

    def test_hex_zone_dp_with_wrong_type_is_skipped(self):
        """DP 0x19 (zone-1 state) with type other than 0xD8 is skipped, not misread."""
        # Hub online + zone-1 DP with type 0xDC (hub-state type) instead of 0xD8.
        payload = bytes([0x18, 0xDC, 0x01, 0x19, 0xDC, 0x01]).hex()
        result = decode_htv213frf_valve("11#" + payload)
        assert result["hub_online"] is True
        assert result["zones"] == {}


class TestDecodeMoistureFullErrorBranches:
    """Cover decode_moisture_full wrapper and _decode_moisture_full_ascii guards."""

    def test_unknown_format_returns_error_dict(self):
        """A payload matching neither hex nor ASCII layout yields an error dict."""
        result = decode_moisture_full("not_matching_any_format")
        assert result["decoder"] == "hcs021frf_error"
        assert "Unexpected payload format" in result["error"]

    def test_invalid_hex_returns_error_dict(self):
        """Bad hex characters after '10#' route through the wrapper exception path."""
        result = decode_moisture_full("10#zz")
        assert result["decoder"] == "hcs021frf_error"
        assert result["type"] == "moisture_full"

    def test_ascii_missing_semicolon_returns_error_dict(self):
        """ASCII-shaped payload missing ';' raises inside the inner ASCII decoder."""
        # Comma + '=' routes to ASCII; missing ';' trips the inner guard.
        result = decode_moisture_full("1,2=3")
        assert result["decoder"] == "hcs021frf_error"
        assert "missing semicolon" in result["error"]

    def test_ascii_short_header_returns_error_dict(self):
        """ASCII header with fewer than 3 fields trips the header guard."""
        result = decode_moisture_full("1,2;694,70,G=292478")
        assert result["decoder"] == "hcs021frf_error"
        assert "header" in result["error"].lower()

    def test_ascii_non_negative_rssi_clamped_to_none(self):
        """Non-negative ASCII RSSI is clamped to None (hardware never reports >=0 dBm)."""
        result = decode_moisture_full("1,5,1;694,70,G=292478")
        assert result["rssi_dbm"] is None
        assert result["decoder"] == "hcs021frf_ascii"

    def test_ascii_short_sensor_section_returns_error_dict(self):
        """ASCII sensor section with fewer than 3 fields trips the sensor-data guard."""
        result = decode_moisture_full("1,-73,1;694,70")
        assert result["decoder"] == "hcs021frf_error"
        assert "sensor" in result["error"].lower()

    def test_ascii_lux_with_multi_equals_falls_back_to_zero(self):
        """A lux token like 'G=A=B' splits into 3 parts and falls back to 0."""
        result = decode_moisture_full("1,-73,1;694,70,G=A=B")
        assert result["illuminance_lux"] == 0

    def test_ascii_lux_numeric_no_equals_parsed(self):
        """A bare numeric lux token (no '=') is parsed as int / 10."""
        result = decode_moisture_full("1,-73,1;694,70,1234")
        assert result["illuminance_lux"] == 123.4

    def test_ascii_lux_non_numeric_no_equals_falls_back_to_zero(self):
        """A non-numeric lux token without '=' falls back to 0 via ValueError."""
        result = decode_moisture_full("1,-73,1;694,70,abc")
        assert result["illuminance_lux"] == 0

    def test_hex_payload_too_long_returns_error_dict(self):
        """A 21-byte hex payload (>20) trips the explicit length cap."""
        # 21 valid bytes; first 20 mirror the documented layout, 21st is filler.
        too_long = bytes(
            [
                0xE1,
                0xA2,
                0x00,
                0xDC,
                0x01,
                0x85,
                0xAB,
                0x02,
                0x88,
                0x1F,
                0xC6,
                0x60,
                0x06,
                0x00,
                0xFF,
                0x0F,
                0xFA,
                0x28,
                0xF7,
                0x18,
                0xAA,
            ]
        )
        result = decode_moisture_full("10#" + too_long.hex())
        assert result["decoder"] == "hcs021frf_error"
        assert "too long" in result["error"]


class TestDecodeRainTagGuards:
    """Cover the three FD-tag validation guards in decode_rain."""

    def _padded(self, base: bytes) -> str:
        """Pad to 24 bytes so _validate_payload accepts the length."""
        return "10#" + (base + bytes(24 - len(base))).hex()

    def test_missing_fd04_raises(self):
        """A payload without FD 04 at offset [3:5] raises a tagged error."""
        # b[3]=0xAA instead of 0xFD.
        bad = bytes([0xE1, 0, 0, 0xAA, 0x04, 0, 0, 0xFD, 0x05])
        try:
            decode_rain(self._padded(bad))
        except ValueError as e:
            assert "FD 04" in str(e)
        else:
            raise AssertionError("decode_rain should have raised on missing FD 04")

    def test_missing_fd05_raises(self):
        """A payload without FD 05 at offset [7:9] raises a tagged error."""
        bad = bytes([0xE1, 0, 0, 0xFD, 0x04, 0, 0, 0xAA, 0x05])
        try:
            decode_rain(self._padded(bad))
        except ValueError as e:
            assert "FD 05" in str(e)
        else:
            raise AssertionError("decode_rain should have raised on missing FD 05")

    def test_missing_fd06_raises(self):
        """A payload without FD 06 at offset [11:13] raises a tagged error."""
        bad = bytes([0xE1, 0, 0, 0xFD, 0x04, 0, 0, 0xFD, 0x05, 0, 0, 0xAA, 0x06])
        try:
            decode_rain(self._padded(bad))
        except ValueError as e:
            assert "FD 06" in str(e)
        else:
            raise AssertionError("decode_rain should have raised on missing FD 06")


class TestValveHubErrorPath:
    """Cover decode_valve_hub error fallback and _extract_valve_hub_state default."""

    def test_invalid_payload_returns_error_dict(self):
        """Garbage input produces the documented error-shaped dict, not an exception."""
        result = decode_valve_hub("garbage_no_separator")
        assert result["decoder"] == "valve_hub_error"
        assert result["zones"] == {}
        assert result["raw_bytes"] == []
        assert "missing" in result["error"].lower() or "unknown" in result["error"].lower()

    def test_extract_valve_hub_state_no_dp_returns_false(self):
        """An empty TLV map yields hub_online=False without raising."""
        from custom_components.rainpoint.api.decoders import _extract_valve_hub_state

        assert _extract_valve_hub_state({}) is False


class TestHws019PartialBranches:
    """Cover the remaining helper branches in decode_hws019wrf_v2 helpers."""

    def test_keyed_item_without_parens_takes_full_value(self):
        """A 'K=plain_value' item with no '(' stores the value as-is."""
        from custom_components.rainpoint.api.decoders import _apply_hws019_keyed_item

        readings: dict = {}
        stats: dict = {}
        _apply_hws019_keyed_item("K=plain_value", readings, stats)
        assert readings == {"K": "plain_value"}
        assert stats == {}

    def test_third_positional_item_after_humidity_is_ignored(self):
        """A third positional item is silently dropped once temp + humidity are filled."""
        from custom_components.rainpoint.api.decoders import _parse_hws019_readings

        readings, stats = _parse_hws019_readings("707(707/694/1),42(42/39/1),99(99/0/1)")
        assert readings == {"temp": "707", "humidity": "42"}
        assert set(stats) == {"temp", "humidity"}

    def test_readings_token_without_equals_or_parens_is_ignored(self):
        """A readings token with neither '=' nor '(' is silently dropped."""
        from custom_components.rainpoint.api.decoders import _parse_hws019_readings

        readings, stats = _parse_hws019_readings("plain_text_no_special_chars")
        assert readings == {}
        assert stats == {}

    def test_daily_max_min_are_captured_for_each_reading(self):
        """The bracketed trailer is preserved as day max/min alongside the current value."""
        from custom_components.rainpoint.api.decoders import _parse_hws019_readings

        readings, stats = _parse_hws019_readings("707(707/694/1),42(42/39/1),P=9709(9709/9701/1)")
        assert readings == {"temp": "707", "humidity": "42", "P": "9709"}
        assert stats == {
            "temp": {"max": "707", "min": "694", "unknown": "1"},
            "humidity": {"max": "42", "min": "39", "unknown": "1"},
            "P": {"max": "9709", "min": "9701", "unknown": "1"},
        }

    def test_malformed_trailer_yields_no_stats(self):
        """A '(...)' trailer that is not a numeric triple is dropped, keeping the current value."""
        from custom_components.rainpoint.api.decoders import _parse_hws019_readings

        readings, stats = _parse_hws019_readings("707(abc),P=9709(x/y)")
        assert readings == {"temp": "707", "P": "9709"}
        assert stats == {}

    def test_embedded_bracket_group_yields_no_stats(self):
        """A reading with more than one bracketed group contributes no stats.

        Searching for the triple anywhere in the token would pick up the second
        group and record stats for a token that is structurally malformed.
        """
        from custom_components.rainpoint.api.decoders import _parse_hws019_readings

        readings, stats = _parse_hws019_readings("707(abc)(798/750/1)")
        assert readings == {"temp": "707"}
        assert stats == {}

    def test_trailing_junk_after_trailer_yields_no_stats(self):
        """Text after the trailer marks the token malformed, so no stats are recorded."""
        from custom_components.rainpoint.api.decoders import _parse_hws019_readings

        readings, stats = _parse_hws019_readings("707(798/750/1)junk")
        assert readings == {"temp": "707"}
        assert stats == {}

    def test_negative_daily_min_is_captured(self):
        """
        A sub-zero daily minimum still yields stats.

        Temperature is reported in tenths of a degree Fahrenheit, so a day-min
        below 0F arrives with a leading '-'. A digits-only pattern matches
        nothing and drops the entire trailer, losing the max as well.
        """
        from custom_components.rainpoint.api.decoders import _parse_hws019_readings

        readings, stats = _parse_hws019_readings("20(50/-50/1)")
        assert readings == {"temp": "20"}
        assert stats["temp"] == {"max": "50", "min": "-50", "unknown": "1"}

    def test_negative_current_value_and_min_are_captured(self):
        """A reading that is itself below zero keeps both its value and its stats."""
        from custom_components.rainpoint.api.decoders import _parse_hws019_readings

        readings, stats = _parse_hws019_readings("-50(20/-90/1)")
        assert readings == {"temp": "-50"}
        assert stats["temp"] == {"max": "20", "min": "-90", "unknown": "1"}

    def test_daily_max_min_straddle_the_current_value(self):
        """
        A capture where the trailer brackets the current reading fixes the slot order.

        Temperature reads 758 with a trailer of (798/750): 750 < 758 < 798. Only
        max-then-min explains that ordering, which is what distinguishes this
        format from a 'current/min/count' reading.

        Payload provenance: brettmeyerowitz/homeassistant-homgar,
        tests/fixtures/payloads/HWS019WRF-V2.json. It is not a synthetic value.
        """
        from custom_components.rainpoint.api.decoders import decode_hws019wrf_v2

        result = decode_hws019wrf_v2("1,0,1;758(798/750/1),54(54/46/1),P=8569(8569/8540/1),")
        assert result["readings"] == {"temp": "758", "humidity": "54", "P": "8569"}
        assert result["reading_stats"]["temp"] == {"max": "798", "min": "750", "unknown": "1"}
        assert int(result["reading_stats"]["temp"]["min"]) < int(result["readings"]["temp"])
        assert int(result["readings"]["temp"]) < int(result["reading_stats"]["temp"]["max"])

    def test_rain_window_trailer_is_not_treated_as_max_min(self):
        """
        'R=' reuses the trailer syntax for cumulative windows, not a max/min pair.

        In 'R=4870(10/20/430)' the values ascend and do not bracket the current
        reading, so recording them as max/min would invert their meaning.
        """
        from custom_components.rainpoint.api.decoders import _parse_hws019_readings

        readings, stats = _parse_hws019_readings("R=4870(10/20/430)")
        assert readings == {"R": "4870"}
        assert stats == {}


class TestBasicDecoderShortBufferBranches:
    """Cover the 'len(b) > 1' False branch on the eight basic decoders.

    A '10#' payload with 0 or 1 hex bytes parses successfully but falls through
    the RSSI extraction guard, leaving rssi=None on the returned dict.
    """

    def test_decode_flow_meter_short_buffer_leaves_rssi_none(self):
        """0-byte buffer skips the rssi branch."""
        result = decode_flow_meter("10#")
        assert result["type"] == "flowmeter"
        assert result["rssi"] is None

    def test_decode_pool_plus_short_buffer_leaves_rssi_none(self):
        """0-byte buffer skips the rssi branch."""
        result = decode_pool_plus("10#")
        assert result["type"] == "co2"
        assert result["rssi"] is None

    def test_decode_soil_short_buffer_leaves_rssi_none(self):
        """1-byte buffer (len==1) still fails the >1 guard."""
        result = decode_soil("10#aa")
        assert result["type"] == "soil"
        assert result["rssi"] is None

    def test_decode_temp_hum_short_buffer_leaves_rssi_none(self):
        """0-byte buffer skips the rssi branch."""
        result = decode_temp_hum("10#")
        assert result["type"] == "temphum"
        assert result["rssi"] is None

    def test_decode_temp_hum_full_short_buffer_leaves_rssi_none(self):
        """0-byte buffer skips the rssi branch."""
        result = decode_temp_hum_full("10#")
        assert result["type"] == "temphum_full"
        assert result["rssi"] is None

    def test_decode_co2_short_buffer_leaves_rssi_none(self):
        """0-byte buffer skips the rssi branch."""
        result = decode_co2("10#")
        assert result["type"] == "co2"
        assert result["rssi"] is None

    def test_decode_display_short_buffer_leaves_rssi_none(self):
        """0-byte buffer skips the rssi branch."""
        result = decode_display("10#")
        assert result["type"] == "display"
        assert result["rssi"] is None

    def test_decode_temphum_short_buffer_leaves_rssi_none(self):
        """0-byte buffer skips the rssi branch (HCS014ARF)."""
        result = decode_temphum("10#")
        assert result["type"] == "temphum"
        assert result["rssi"] is None

    def test_decode_pool_short_buffer_leaves_rssi_none(self):
        """0-byte buffer skips the rssi branch (HCS0528ARF)."""
        result = decode_pool("10#")
        assert result["type"] == "pool"
        assert result["rssi"] is None

    def test_decode_unknown_short_buffer_leaves_rssi_none(self):
        """0-byte buffer skips the rssi branch (catch-all fallback)."""
        result = decode_unknown("10#")
        assert result["type"] == "unknown"
        assert result["rssi"] is None


class TestBasicDecoderLogAndSwallowBranches:
    """Cover the bare 'except Exception: log' blocks on the eight basic decoders.

    Feeding a payload that survives the function entry but trips
    _parse_rainpoint_payload (no '#' separator) reaches the log-and-swallow
    branch and returns the default dict with rssi=None.
    """

    def test_decode_flow_meter_swallows_parse_error(self):
        """No '#' separator raises in _parse_rainpoint_payload, caught and swallowed."""
        result = decode_flow_meter("garbage_no_separator")
        assert result["type"] == "flowmeter"
        assert result["rssi"] is None

    def test_decode_pool_plus_swallows_parse_error(self):
        """No '#' separator raises in _parse_rainpoint_payload, caught and swallowed."""
        result = decode_pool_plus("garbage_no_separator")
        assert result["type"] == "co2"
        assert result["rssi"] is None

    def test_decode_soil_swallows_parse_error(self):
        """No '#' separator raises in _parse_rainpoint_payload, caught and swallowed."""
        result = decode_soil("garbage_no_separator")
        assert result["type"] == "soil"
        assert result["rssi"] is None

    def test_decode_temp_hum_swallows_parse_error(self):
        """No '#' separator raises in _parse_rainpoint_payload, caught and swallowed."""
        result = decode_temp_hum("garbage_no_separator")
        assert result["type"] == "temphum"
        assert result["rssi"] is None

    def test_decode_temp_hum_full_swallows_parse_error(self):
        """No '#' separator raises in _parse_rainpoint_payload, caught and swallowed."""
        result = decode_temp_hum_full("garbage_no_separator")
        assert result["type"] == "temphum_full"
        assert result["rssi"] is None

    def test_decode_co2_swallows_parse_error(self):
        """No '#' separator raises in _parse_rainpoint_payload, caught and swallowed."""
        result = decode_co2("garbage_no_separator")
        assert result["type"] == "co2"
        assert result["rssi"] is None

    def test_decode_display_swallows_parse_error(self):
        """No '#' separator raises in _parse_rainpoint_payload, caught and swallowed."""
        result = decode_display("garbage_no_separator")
        assert result["type"] == "display"
        assert result["rssi"] is None

    def test_decode_temphum_swallows_parse_error(self):
        """No '#' separator raises in _parse_rainpoint_payload, caught and swallowed (HCS014ARF)."""
        result = decode_temphum("garbage_no_separator")
        assert result["type"] == "temphum"
        assert result["rssi"] is None

    def test_decode_pool_swallows_parse_error(self):
        """No '#' separator raises in _parse_rainpoint_payload, caught and swallowed (HCS0528ARF)."""
        result = decode_pool("garbage_no_separator")
        assert result["type"] == "pool"
        assert result["rssi"] is None


class TestHtv213ZoneUsageAndEventTime:
    """Per-zone water usage and event time, decoded from the maintainer's real frames."""

    def test_idle_frame_reports_per_zone_usage_counts(self):
        """Both zones carry their last run's raw flow count in the 0x29/0x2A records.

        Little-endian is load-bearing here: a5010000 reads as 421 little-endian
        and as 2,768,306,176 big-endian.
        """
        zones = decode_htv213frf_valve(SAMPLE_HTV245_FULL_IDLE_PAYLOAD)["zones"]
        assert zones[1]["last_usage_counts"] == 421
        assert zones[2]["last_usage_counts"] == 48

    def test_usage_counts_convert_to_gallons(self):
        """421 counts is the calibration point: it showed as 0.8 gal in the RainPoint app."""
        zones = decode_htv213frf_valve(SAMPLE_HTV245_FULL_IDLE_PAYLOAD)["zones"]
        assert zones[1]["last_usage_gallons"] == 0.842
        assert round(zones[1]["last_usage_gallons"], 1) == 0.8
        assert zones[2]["last_usage_gallons"] == 0.096

    def test_running_zone_reports_zero_usage(self):
        """A zone reports no usage while it is mid-run; the idle zone still reports its last."""
        zones = decode_htv213frf_valve(SAMPLE_HTV245_FULL_ZONE2_ACTIVE_PAYLOAD)["zones"]
        assert zones[2]["open"] is True
        assert zones[2]["last_usage_counts"] == 0
        assert zones[2]["last_usage_gallons"] == 0.0
        assert zones[1]["last_usage_counts"] == 276

    def test_usage_absent_rather_than_zero_when_frame_omits_the_record(self):
        """No 0x9F record yields None, so "not reported" stays distinct from "none used"."""
        zones = decode_htv213frf_valve(SAMPLE_HTV245_TLV_PAYLOAD)["zones"]
        assert zones[1]["last_usage_counts"] is None
        assert zones[1]["last_usage_gallons"] is None

    def test_usage_record_of_the_wrong_type_is_refused(self):
        """A 0x29 record that is not type 0x9F leaves usage empty rather than misread."""
        # Zone 1 state, then a 0x29 record carrying the duration type byte.
        raw = "11#18dc0119d80029ad3c00"
        zones = decode_htv213frf_valve(raw)["zones"]
        assert zones[1]["last_usage_counts"] is None

    def test_running_zone_event_time_is_report_time_plus_duration(self):
        """The mid-run frame's zone 2 event time lands exactly one duration after the report time.

        Report stamp 0x19C91A33 decodes to 17:40:51 and the zone has 2940s
        (49 minutes) left, which is the 18:29:51 asserted here. That agreement
        is what fixes the bit layout and the 2020 year base.
        """
        zones = decode_htv213frf_valve(SAMPLE_HTV245_FULL_ZONE2_ACTIVE_PAYLOAD)["zones"]
        assert zones[2]["duration_seconds"] == 2940
        assert zones[2]["event_time"] == "2026-07-04T18:29:51"

    def test_idle_zones_report_no_event_time(self):
        """A zero stamp means "no event" and yields None rather than a 2020 epoch date."""
        zones = decode_htv213frf_valve(SAMPLE_HTV245_FULL_IDLE_PAYLOAD)["zones"]
        assert zones[1]["event_time"] is None
        assert zones[2]["event_time"] is None

    def test_event_time_record_of_the_wrong_type_is_refused(self):
        """A 0x21 record that is not type 0xB7 leaves the event time empty."""
        raw = "11#18dc0119d80021ad3c00"
        zones = decode_htv213frf_valve(raw)["zones"]
        assert zones[1]["event_time"] is None

    def test_impossible_packed_date_yields_none(self):
        """A word whose fields are not a real date is dropped, not clamped into one."""
        from custom_components.rainpoint.api.decoders import _decode_packed_timestamp

        # Month field 0 and day field 0: structurally parseable, not a date.
        assert _decode_packed_timestamp(0x18000000) is None

    def test_packed_timestamp_covers_the_full_field_range(self):
        """Each bit field lands in its own component rather than bleeding into a neighbour."""
        from custom_components.rainpoint.api.decoders import _decode_packed_timestamp

        # year 5 (2025), month 12, day 31, hour 23, minute 59, second 59.
        packed = (5 << 26) | (12 << 22) | (31 << 17) | (23 << 12) | (59 << 6) | 59
        assert _decode_packed_timestamp(packed) == "2025-12-31T23:59:59"


class TestHtvWiderFamilyUsageRecords:
    """The 3- and 4-zone family members decode through the same dp_id blocks."""

    def test_htv345_reports_usage_for_every_zone(self):
        """Three zones, three 0x9F records at 0x29/0x2A/0x2B, paired to zones 1/2/3."""
        zones = decode_htv213frf_valve(SAMPLE_HTV345_TLV_PAYLOAD)["zones"]
        assert sorted(zones) == [1, 2, 3]
        assert [zones[n]["last_usage_counts"] for n in (1, 2, 3)] == [0, 0, 0]

    def test_htv405_frame_carries_no_usage_records(self):
        """Four zones and no 0x9F records at all: usage is absent, not zero.

        This is the captured behaviour of that model, and the distinction is
        the reason the decoder returns None instead of defaulting to 0: a hub
        that never reports usage must not look like one that reported none
        used.
        """
        zones = decode_htv213frf_valve(SAMPLE_HTV405_TLV_PAYLOAD)["zones"]
        assert sorted(zones) == [1, 2, 3, 4]
        assert all(zones[n]["last_usage_counts"] is None for n in zones)
        assert all(zones[n]["last_usage_gallons"] is None for n in zones)

    def test_four_zones_fill_the_dp_id_blocks_without_colliding(self):
        """Zone 4's duration (0x28) and zone 1's usage (0x29) are adjacent but distinct."""
        zones = decode_htv213frf_valve(SAMPLE_HTV405_TLV_PAYLOAD)["zones"]
        assert zones[4]["duration_seconds"] == 0
        assert zones[4]["event_time"] is None


class TestDecodeHtv210b:
    """The HTV210B structural decoder against the real idle capture and evidenced run states.

    The running-state payloads are synthetic but every field value in them is
    taken from a timed two-minute run on real hardware: work state 0x00 ->
    0x21 -> 0x20, a 120-second commanded duration that persists after the
    run, and an event time exactly two minutes after the run started.
    """

    # Zone 1 running: state 0x21, 4-byte duration 120s (header 0xAF, the width
    # the real idle capture uses), event time 2026-07-29T19:08:17 packed.
    RUNNING_PAYLOAD = "11#18DC0117E1B40119D8211AD80021B71132FB1922B70000000025AF7800000026AF00000000FEFF0F1527FB19"

    def test_idle_capture_decodes_both_zones_closed(self):
        """The real idle capture yields two closed zones with no run data."""
        result = decode_htv210b(SAMPLE_HTV210B_TLV_PAYLOAD)
        assert result["decoder"] == "htv210b_hex"
        assert result["type"] == "valve_hub"
        assert sorted(result["zones"]) == [1, 2]
        for zone in result["zones"].values():
            assert zone["open"] is False
            assert zone["state_raw"] == 0x00
            assert zone["duration_seconds"] == 0
            assert zone["event_time"] is None

    def test_idle_capture_reports_rssi_battery_and_report_time(self):
        """The frame's diagnostics match what the RainPoint app showed at capture time.

        RSSI -76 dBm at 1M PHY, battery flag 1, and a report time that decodes
        to the calendar day the capture was taken.
        """
        result = decode_htv210b(SAMPLE_HTV210B_TLV_PAYLOAD)
        assert result["rssi_dbm"] == -76
        assert result["battery_flag"] == 1
        assert result["battery_percent"] == 100
        assert result["report_time"] == "2026-07-29T18:28:21"
        assert result["hub_online"] is True

    def test_zones_carry_no_usage_fields(self):
        """No flow meter, so the zone dict must not manufacture usage keys.

        The frame does carry per-zone usage records, but they read zero in
        every capture including mid-run, which is a meter that never moves,
        not a meter reading zero.
        """
        zones = decode_htv210b(SAMPLE_HTV210B_TLV_PAYLOAD)["zones"]
        for zone in zones.values():
            assert "last_usage_counts" not in zone
            assert "last_usage_gallons" not in zone

    def test_running_zone_reports_open_duration_and_end_time(self):
        """Work-state bit 0 open, commanded seconds, and the packed end-of-run stamp."""
        zones = decode_htv210b(self.RUNNING_PAYLOAD)["zones"]
        assert zones[1]["open"] is True
        assert zones[1]["state_raw"] == 0x21
        assert zones[1]["duration_seconds"] == 120
        assert zones[1]["event_time"] == "2026-07-29T19:08:17"
        assert zones[2]["open"] is False

    def test_after_run_latched_bit_still_reads_closed(self):
        """0x20 after the zone's first use: bit 5 is latched, bit 0 is the state."""
        zones = decode_htv210b("11#19D820")["zones"]
        assert zones[1]["open"] is False
        assert zones[1]["state_raw"] == 0x20

    def test_two_byte_duration_width_decodes_the_same(self):
        """A 2-byte duration record (the HTV213 family's width) reads identically.

        The structural walk resolves both widths to the same field, so a
        firmware that writes the narrower record needs no special case.
        """
        zones = decode_htv210b("11#19D82125AD7800")["zones"]
        assert zones[1]["duration_seconds"] == 120

    def test_missing_duration_and_event_records_leave_defaults(self):
        """A state-only frame reads duration 0 and no event time."""
        zones = decode_htv210b("11#19D800")["zones"]
        assert zones[1]["duration_seconds"] == 0
        assert zones[1]["event_time"] is None

    def test_wrong_width_state_record_is_skipped(self):
        """A 2-byte record on the state field is not a zone state and grows no zone."""
        result = decode_htv210b("11#19D90021")
        assert result["zones"] == {}
        assert result["hub_online"] is False

    def test_wrong_width_event_record_is_ignored(self):
        """A 2-byte record on the event-time field leaves the zone's event time empty."""
        zones = decode_htv210b("11#19D82121B51132")["zones"]
        assert zones[1]["event_time"] is None

    def test_battery_percent_absent_when_frame_has_no_battery_record(self):
        """A frame with no battery record leaves the flag None and the percent absent."""
        result = decode_htv210b("11#19D800")
        assert result["battery_flag"] is None
        assert "battery_percent" not in result

    def test_rssi_read_is_structural_not_a_byte_scan(self):
        """A 17E1B401 sequence inside another record's value bytes is not an RSSI.

        The byte pattern of a genuine RSSI record sits entirely within a 4-byte
        usage value here; a linear scan would read it as -76. The structural
        walk sees one usage record and no RSSI record at all.
        """
        result = decode_htv210b("11#299F17E1B40119D800")
        assert result["rssi_dbm"] is None
        assert result["zones"][1]["open"] is False

    def test_rssi_rejects_a_positive_dbm_reading(self):
        """A non-negative dBm value is no reading; no radio reports +5 dBm."""
        assert decode_htv210b("11#17E10500")["rssi_dbm"] is None

    def test_rssi_record_truncated_to_no_value_bytes_reads_none(self):
        """A frame ending mid-record leaves the RSSI empty rather than misread."""
        assert decode_htv210b("11#17E1")["rssi_dbm"] is None

    def test_duration_record_of_unobserved_width_is_rejected(self):
        """A 3-byte duration is a truncated record, not a third firmware width.

        The record walk silently shortens a record whose declared width runs
        past the buffer end; only the two widths captures have shown decode as
        seconds, so the cut-short value stays out of duration_seconds.
        """
        zones = decode_htv210b("11#19D82125AF780000")["zones"]
        assert zones[1]["duration_seconds"] == 0

    def test_non_hex_frame_returns_error_dict(self):
        """A payload this decoder cannot read degrades to the family's error shape."""
        result = decode_htv210b("10#208500968832DC64E0C5")
        assert result["decoder"] == "htv210b_error"
        assert result["zones"] == {}
        assert "error" in result
        # None, not 0: the RSSI sensor renders this verbatim, and 0 dBm would
        # read as a perfect signal instead of no reading.
        assert result["rssi_dbm"] is None
        assert result["hub_online"] is False
        assert result["battery_flag"] is None

    def test_alarm_records_do_not_misframe_the_stream(self):
        """The compact 1-byte alarm records parse as themselves, not as a 2-byte type.

        The HTV213 type-byte table reads 0x20 as a 2-byte record, which on
        this frame would swallow the second alarm's dp_id. The structural walk
        keeps every following record intact, which this asserts through the
        zone 2 event time surviving unshifted.
        """
        result = decode_htv210b("11#19D8001AD8001D201E2022B71132FB19")
        assert result["zones"][2]["event_time"] == "2026-07-29T19:08:17"


class TestDecodeHtv210bDpState:
    """decode_htv210b_dp_state against the captured controlWorkModeDP response
    blobs, field by field rather than as a whole-dict comparison against a
    constant built by the same code path.
    """

    def test_60_second_open_decodes_every_field(self):
        """The captured 60-second open blob: open, the commanded seconds, the
        raw work-state byte, and the packed end-of-run stamp."""
        result = decode_htv210b_dp_state(SAMPLE_HTV210B_DP_OPEN_60S_STATE)
        assert result["open"] is True
        assert result["duration_seconds"] == 60
        assert result["state_raw"] == 0x21
        assert result["event_time"] == "2026-08-05T18:15:17"

    def test_120_second_open_decodes_the_longer_duration(self):
        """The second commanded duration confirmed on hardware in the same session."""
        result = decode_htv210b_dp_state(SAMPLE_HTV210B_DP_OPEN_120S_STATE)
        assert result["duration_seconds"] == 120

    def test_close_decodes_to_closed_with_zeroed_fields(self):
        """A close zeroes both the duration and the time word."""
        result = decode_htv210b_dp_state(SAMPLE_HTV210B_DP_CLOSE_STATE)
        assert result["open"] is False
        assert result["state_raw"] == 0x00
        assert result["duration_seconds"] == 0
        assert result["event_time"] is None

    def test_used_latch_bit_stored_undecomposed(self):
        """state_raw is the raw work-state byte: bit 0 (open) and bit 5 (the
        used latch) are both set in the 60-second open sample, and the
        decoder returns the byte as-is rather than pre-splitting it."""
        state_raw = decode_htv210b_dp_state(SAMPLE_HTV210B_DP_OPEN_60S_STATE)["state_raw"]
        assert state_raw & 0x01  # bit 0: open
        assert state_raw & 0x20  # bit 5: used latch
        assert state_raw == 0x21

    def test_missing_comma_returns_none(self):
        """A blob with no ',' separator at all is not the DP comma form."""
        assert decode_htv210b_dp_state("D821AF3C000000B7D1230B1A") is None

    def test_odd_length_hex_body_returns_none(self):
        """A truncated hex body cannot be read as bytes."""
        assert decode_htv210b_dp_state("1,D821AF3C00000") is None

    def test_empty_hex_body_returns_none(self):
        """A comma with nothing after it carries no record to walk."""
        assert decode_htv210b_dp_state("1,") is None

    def test_body_with_no_work_state_record_returns_none(self):
        """A body that never mentions the work-state field cannot describe a zone."""
        assert decode_htv210b_dp_state("1,AF3C000000") is None

    def test_missing_duration_and_event_records_default_to_zero_and_none(self):
        """A body carrying only the work-state record still decodes: duration
        defaults to 0 and event_time stays None rather than raising."""
        result = decode_htv210b_dp_state("1,D821")
        assert result["open"] is True
        assert result["duration_seconds"] == 0
        assert result["event_time"] is None

    def test_the_framed_poll_payload_is_not_the_dp_response(self):
        """This decoder is not decode_htv210b: feeding it the poll's 11# frame
        must not accidentally succeed, since it carries no leading-digit comma
        form at all."""
        assert decode_htv210b_dp_state(SAMPLE_HTV210B_TLV_PAYLOAD) is None


# Expected decode for every one of the 22 committed HIC801W frames, keyed by
# the same capture label as tests.payload_samples.SAMPLE_HIC801W_ALL_FRAMES.
# Derived by hand-walking each committed frame through _parse_entries and
# cross-checking the result against both capture corpora; do not re-derive
# from the raw hex without redoing that cross-check.
_HIC801W_EXPECTED_DECODE = {
    "2026-07-17 mid-run st1": {
        "current_station": 1,
        "program_stations": [1, 2],
        "program_stations_completed": [],
        "run_duration_seconds": 1800,
        "run_ends_at": "2026-07-13T19:55:52",
    },
    "2026-08-08 all off": {
        "current_station": 0,
        "program_stations": [],
        "program_stations_completed": [],
        "run_duration_seconds": 0,
        "run_ends_at": None,
    },
    "2026-08-08 st1 master on": {
        "current_station": 1,
        "program_stations": [1, 2],
        "program_stations_completed": [],
        "run_duration_seconds": 300,
        "run_ends_at": "2026-08-08T19:38:55",
    },
    "2026-08-08 st2 master on": {
        "current_station": 2,
        "program_stations": [1, 2],
        "program_stations_completed": [1],
        "run_duration_seconds": 300,
        "run_ends_at": "2026-08-08T19:43:56",
    },
    "2026-08-10 st1": {
        "current_station": 1,
        "program_stations": [1, 2, 3, 4, 5, 6, 7, 8],
        "program_stations_completed": [],
        "run_duration_seconds": 60,
        "run_ends_at": "2026-08-10T20:26:02",
    },
    "2026-08-10 st2": {
        "current_station": 2,
        "program_stations": [1, 2, 3, 4, 5, 6, 7, 8],
        "program_stations_completed": [1],
        "run_duration_seconds": 60,
        "run_ends_at": "2026-08-10T20:27:03",
    },
    "2026-08-10 st3": {
        "current_station": 3,
        "program_stations": [1, 2, 3, 4, 5, 6, 7, 8],
        "program_stations_completed": [1, 2],
        "run_duration_seconds": 60,
        "run_ends_at": "2026-08-10T20:28:04",
    },
    "2026-08-10 st4": {
        "current_station": 4,
        "program_stations": [1, 2, 3, 4, 5, 6, 7, 8],
        "program_stations_completed": [1, 2, 3],
        "run_duration_seconds": 60,
        "run_ends_at": "2026-08-10T20:29:05",
    },
    "2026-08-10 st5": {
        "current_station": 5,
        "program_stations": [1, 2, 3, 4, 5, 6, 7, 8],
        "program_stations_completed": [1, 2, 3, 4],
        "run_duration_seconds": 60,
        "run_ends_at": "2026-08-10T20:30:06",
    },
    "2026-08-10 st6": {
        "current_station": 6,
        "program_stations": [1, 2, 3, 4, 5, 6, 7, 8],
        "program_stations_completed": [1, 2, 3, 4, 5],
        "run_duration_seconds": 60,
        "run_ends_at": "2026-08-10T20:31:07",
    },
    "2026-08-10 st7": {
        "current_station": 7,
        "program_stations": [1, 2, 3, 4, 5, 6, 7, 8],
        "program_stations_completed": [1, 2, 3, 4, 5, 6],
        "run_duration_seconds": 60,
        "run_ends_at": "2026-08-10T20:32:08",
    },
    "2026-08-10 st8": {
        "current_station": 8,
        "program_stations": [1, 2, 3, 4, 5, 6, 7, 8],
        "program_stations_completed": [1, 2, 3, 4, 5, 6, 7],
        "run_duration_seconds": 60,
        "run_ends_at": "2026-08-10T20:33:09",
    },
    "2026-08-10 idle": {
        "current_station": 0,
        "program_stations": [],
        "program_stations_completed": [],
        "run_duration_seconds": 0,
        "run_ends_at": None,
    },
    "unit2 idle": {
        "current_station": 0,
        "program_stations": [],
        "program_stations_completed": [],
        "run_duration_seconds": 0,
        "run_ends_at": None,
    },
    "unit2 zone 1": {
        "current_station": 1,
        "program_stations": [1, 2, 3, 4],
        "program_stations_completed": [],
        "run_duration_seconds": 60,
        "run_ends_at": "2026-04-12T18:15:52",
    },
    "unit2 zone 2": {
        "current_station": 2,
        "program_stations": [2],
        "program_stations_completed": [],
        "run_duration_seconds": 360,
        "run_ends_at": "2026-04-12T19:31:32",
    },
    "unit2 zone 3": {
        "current_station": 3,
        "program_stations": [3],
        "program_stations_completed": [],
        "run_duration_seconds": 36000,
        "run_ends_at": "2026-04-13T05:28:47",
    },
    "unit2 zone 4": {
        "current_station": 4,
        "program_stations": [4],
        "program_stations_completed": [],
        "run_duration_seconds": 600,
        "run_ends_at": "2026-04-12T19:42:17",
    },
    "unit2 zone 5": {
        "current_station": 5,
        "program_stations": [1, 2, 3, 4, 5, 6, 7, 8],
        "program_stations_completed": [1, 2, 3, 4],
        "run_duration_seconds": 180,
        "run_ends_at": "2026-04-12T19:48:53",
    },
    "unit2 zone 6": {
        "current_station": 6,
        "program_stations": [1, 2, 3, 4, 5, 6, 7, 8],
        "program_stations_completed": [1, 2, 3, 4, 5],
        "run_duration_seconds": 180,
        "run_ends_at": "2026-04-12T19:50:03",
    },
    "unit2 zone 7": {
        "current_station": 7,
        "program_stations": [1, 2, 3, 4, 5, 6, 7, 8],
        "program_stations_completed": [1, 2, 3, 4, 5, 6],
        "run_duration_seconds": 180,
        "run_ends_at": "2026-04-12T19:51:21",
    },
    "unit2 zone 8": {
        "current_station": 8,
        "program_stations": [1, 2, 3, 4, 5, 6, 7, 8],
        "program_stations_completed": [1, 2, 3, 4, 5, 6, 7],
        "run_duration_seconds": 15685,
        "run_ends_at": "2026-04-12T19:53:05",
    },
}

assert set(_HIC801W_EXPECTED_DECODE) == set(SAMPLE_HIC801W_ALL_FRAMES)


class TestDecodeHic801w:
    """decode_hic801w against all 22 committed frames from both units, plus
    the rejection, ordering and unverified-field edges the captures settle
    for this decoder."""

    @pytest.mark.parametrize("label", sorted(SAMPLE_HIC801W_ALL_FRAMES))
    def test_corpus_frame_decodes_to_the_settled_reading(self, label):
        """Every committed frame pins current_station, both station lists,
        the run duration, and the run end time against the ground-truth
        captures. This is what makes the committed corpus load bearing."""
        result = decode_hic801w(SAMPLE_HIC801W_ALL_FRAMES[label])
        expected = _HIC801W_EXPECTED_DECODE[label]
        assert result["current_station"] == expected["current_station"]
        assert result["program_stations"] == expected["program_stations"]
        assert result["program_stations_completed"] == expected["program_stations_completed"]
        assert result["run_duration_seconds"] == expected["run_duration_seconds"]
        assert result["run_ends_at"] == expected["run_ends_at"]

    @pytest.mark.parametrize("label", sorted(SAMPLE_HIC801W_ALL_FRAMES))
    def test_corpus_frame_decodes_cleanly(self, label):
        """Every frame in the corpus decodes on the happy path: no error key
        and the hex decoder tag."""
        result = decode_hic801w(SAMPLE_HIC801W_ALL_FRAMES[label])
        assert result["decoder"] == "hic801w_hex"
        assert "error" not in result

    def test_corpus_is_exactly_22_frames(self):
        """13 reporter frames plus 9 second-unit
        frames, keyed by capture label so the two byte-identical reporter
        idle captures below do not collapse into one entry."""
        assert len(SAMPLE_HIC801W_ALL_FRAMES) == 22
        assert len(SAMPLE_HIC801W_REPORTER_FRAMES) == 13
        assert len(SAMPLE_HIC801W_SECOND_UNIT_FRAMES) == 9

    def test_the_two_reporter_idle_captures_are_byte_identical_yet_distinct_entries(self):
        """The "2026-08-08 all off" and "2026-08-10 idle" reporter frames are
        the same byte string, taken two days apart, and both must survive in
        the corpus as separate keys."""
        assert SAMPLE_HIC801W_REPORTER_FRAMES["2026-08-08 all off"] == SAMPLE_HIC801W_REPORTER_FRAMES["2026-08-10 idle"]
        assert "2026-08-08 all off" in SAMPLE_HIC801W_ALL_FRAMES
        assert "2026-08-10 idle" in SAMPLE_HIC801W_ALL_FRAMES

    def test_both_units_idle_frames_read_none_not_the_2020_sentinel(self):
        """Idle must never publish the packed sentinel's naive rendering
        2020-01-01T02:00:00: it is a real-looking date, so it is
        suppressed deliberately."""
        for label in ("2026-08-10 idle", "unit2 idle"):
            result = decode_hic801w(SAMPLE_HIC801W_ALL_FRAMES[label])
            assert result["current_station"] == 0
            assert result["program_stations"] == []
            assert result["program_stations_completed"] == []
            assert result["run_duration_seconds"] == 0
            assert result["run_ends_at"] is None
            assert result["run_ends_at"] != "2020-01-01T02:00:00"

    @pytest.mark.parametrize("raw", ["", "10#", None, "10#ABC"])
    def test_empty_and_malformed_input_returns_the_error_envelope_never_raises(self, raw):
        """The empty edge: none of these four inputs may raise, and each must
        return the error envelope with every decoded field None."""
        result = decode_hic801w(raw)
        assert result["decoder"] == "hic801w_error"
        assert result["type"] == "irrigation_controller"
        assert result["current_station"] is None
        assert result["program_stations"] is None
        assert result["program_stations_completed"] is None
        assert result["run_duration_seconds"] is None
        assert result["run_ends_at"] is None
        assert result["raw_bytes"] == []
        assert "error" in result

    def test_truncated_frame_missing_evtime_is_rejected(self):
        """A frame carrying STA_DURATION but truncated before STA_EVTIME
        rejects on the missing field, distinctly from the missing-duration
        case above."""
        # SAMPLE_HIC801W_STATION3_PAYLOAD's body up through STA_DURATION's
        # record alone (fields 1, 10, 19), with everything after cut off.
        truncated = "10#108800AF3C000000"
        result = decode_hic801w(truncated)
        assert result["decoder"] == "hic801w_error"
        assert "STA_EVTIME" in result["error"]

    def test_truncated_frame_missing_water_zones_is_rejected(self):
        """A frame carrying both STA_DURATION and STA_EVTIME but truncated
        before STA_WATER_ZONES rejects on the missing field."""
        # SAMPLE_HIC801W_STATION3_PAYLOAD's body up through STA_EVTIME's
        # record (fields 1, 10, 19, 21), with everything after cut off.
        truncated = "10#108800AF3C000000B70447151A"
        result = decode_hic801w(truncated)
        assert result["decoder"] == "hic801w_error"
        assert "STA_WATER_ZONES" in result["error"]

    def test_stations_from_mask_is_ascending_and_1_based(self):
        """The ordering edge: a full mask yields every station in ascending
        order, and an empty mask yields an empty list."""
        assert _hic801w_stations_from_mask(0xFF) == [1, 2, 3, 4, 5, 6, 7, 8]
        assert _hic801w_stations_from_mask(0) == []
        # Bit 0 is station 1, not station 0: a single-bit mask at bit 0
        # names station 1 alone.
        assert _hic801w_stations_from_mask(0x01) == [1]
        assert _hic801w_stations_from_mask(0x80) == [8]

    def test_water_zones_b3_non_zero_is_rejected_with_exactly_one_sanitized_warning(self, caplog):
        """A real capture's STA_WATER_ZONES b3 mutated to a non-zero
        byte is rejected outright, and the rejection is exactly one WARNING
        naming the field and the byte, carrying no cloud-supplied name."""
        # SAMPLE_HIC801W_STATION3_PAYLOAD's STA_WATER_ZONES value is 03FF0300;
        # flip the trailing (b3) byte from 00 to 01.
        mutated = SAMPLE_HIC801W_STATION3_PAYLOAD.replace("F703FF0300F9", "F703FF0301F9")
        assert mutated != SAMPLE_HIC801W_STATION3_PAYLOAD

        with caplog.at_level(logging.WARNING, logger="custom_components.rainpoint.api.decoders"):
            result = decode_hic801w(mutated)

        assert result["decoder"] == "hic801w_error"
        assert result["current_station"] is None

        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) == 1
        message = warnings[0].getMessage()
        assert "STA_WATER_ZONES" in message
        assert "b3" in message
        # No cloud-supplied free text: decode_hic801w takes only the raw
        # payload, so it structurally cannot echo a device, hub or home
        # name into the line. "HIC801W" itself is a hardcoded literal in
        # the log format string, the same way "HTV210B" is in
        # decode_htv210b's own error log, not a cloud-supplied value.
        for forbidden in ("Test Home", "Test Hub", "Test Sensor"):
            assert forbidden not in message

    def test_water_zones_b3_rejection_logs_nothing_above_warning(self, caplog):
        """The b3 rejection is a diagnosed, expected condition, so the one
        sanitized WARNING is the whole log record for it.

        Returning the envelope rather than raising into the shared handler is
        what keeps this true. Raising logged the same event a second time at
        ERROR with a full traceback, and a device that persistently sends a
        non-zero b3 repeats that on every poll, which reads as a recurring
        crash rather than the handled rejection it is.
        """
        mutated = SAMPLE_HIC801W_STATION3_PAYLOAD.replace("F703FF0300F9", "F703FF0301F9")
        assert mutated != SAMPLE_HIC801W_STATION3_PAYLOAD

        with caplog.at_level(logging.DEBUG, logger="custom_components.rainpoint.api.decoders"):
            result = decode_hic801w(mutated)

        assert result["decoder"] == "hic801w_error"
        assert [r.levelno for r in caplog.records] == [logging.WARNING]
        assert not any(r.exc_info for r in caplog.records)

    def test_no_error_log_carries_the_payload(self, caplog):
        """No HIC801W log record echoes the payload, by either route.

        The format string is the obvious route; the ValueError message is the
        back door, because `_LOGGER.exception` prints the traceback and the
        message travels inside it. Both are checked here against a payload
        distinctive enough that a substring match cannot pass by accident.
        The payload stays reachable through the disabled-by-default
        _raw_payload diagnostic and the diagnostics download, so nothing
        diagnostic depends on it being on this line.
        """
        payload = "99#DEADBEEFCAFEBABE"

        with caplog.at_level(logging.DEBUG, logger="custom_components.rainpoint.api.decoders"):
            result = decode_hic801w(payload)

        assert result["decoder"] == "hic801w_error"
        # getMessage() is the format string applied to its args; the traceback
        # text is separate, and the ValueError message lives there.
        rendered = "\n".join(r.getMessage() for r in caplog.records)
        tracebacks = "\n".join(self._format_exc_text(r) for r in caplog.records)
        for haystack in (rendered, tracebacks):
            assert "DEADBEEF" not in haystack
            assert payload not in haystack
        assert "19-character blob" in rendered

    @staticmethod
    def _format_exc_text(record) -> str:
        """Return a record's traceback text, or empty when it carries none."""
        if not record.exc_info:
            return ""
        return "".join(traceback.format_exception(*record.exc_info))

    def test_sta_ts_det_width_mismatch_never_rejects_a_real_frame(self):
        """STA_TS_DET arrives 2 bytes wide against a declared 4 in
        every one of the 22 captures. The shape check must never reject on
        that mismatch, because doing so would reject every real frame."""
        for label, raw in SAMPLE_HIC801W_ALL_FRAMES.items():
            b = _parse_rainpoint_payload(raw)
            fields = {e["field"]: e["value_bytes"] for e in _parse_entries(list(b), False)}
            ts_det = fields.get(38)
            assert ts_det is not None, label
            assert len(ts_det) == 2, label
            assert decode_hic801w(raw)["decoder"] == "hic801w_hex", label

    def test_neither_envelope_carries_a_key_for_an_unverified_reading(self):
        """No key for STA_RAIN, STA_RH, STA_TS_DET or b3, in
        either envelope, not even as an attribute, so a future field
        addition to either branch trips this test."""
        forbidden_substrings = ("rain", "humidity", "ts_det", "b3")
        happy = decode_hic801w(SAMPLE_HIC801W_STATION3_PAYLOAD)
        error = decode_hic801w("")
        for envelope in (happy, error):
            for key in envelope:
                lowered = key.lower()
                for forbidden in forbidden_substrings:
                    assert forbidden not in lowered, f"{key!r} in {envelope['decoder']} envelope"

    def test_hic801w_never_reaches_the_generic_decoder(self):
        """The trust boundary: decode_hic801w's own source never names the
        model-agnostic decoder, and the model is registered as hand-written."""
        import inspect

        source = inspect.getsource(decode_hic801w)
        assert "decode_generic" not in source
        assert is_hand_written_model("HIC801W") is True


class TestHic801wGenericLockout:
    """The generic path is observably closed for HIC801W end to end, not
    merely a set-membership fact. This is what the milestone's
    recorded trade actually buys: evaluate_generic_gate("HIC801W", "279") is
    all-or-nothing per variant and STA_RAIN and STA_TS_DET can never be
    defined from constants on either unit's corpus, so the gate could never
    pass -- a hand-written decoder was the only route this model ever had.
    """

    def test_decode_generic_attaches_no_catalog_annotation_for_hic801w(self):
        """Even though the committed catalog genuinely holds variants 278 and
        279 for this model, is_hand_written_model's lockout means
        decode_generic never annotates a field with catalog metadata for it."""
        result = decode_generic(SAMPLE_HIC801W_STATION3_PAYLOAD, model="HIC801W", model_code=279)
        for field in result["fields"]:
            assert "catalog" not in field


class TestHic801wCatalogKeying:
    """The variant this decoder relies on resolves through modelCode alone
    keyed by modelCode, over the real committed catalog snapshot rather than a synthetic
    one."""

    def test_variant_279_is_the_8_port_accessory_with_ctl_water(self):
        """279 declares 9 datapoints, an 8-port accessory, and CTL_WATER --
        never CTL_BT_WATER -- so no DP-endpoint routing is involved."""
        entry = get_catalog_entry("HIC801W", 279)
        assert len(entry) == 9
        identities = [e["identity"] for e in entry]
        assert "CTL_WATER" in identities
        assert "CTL_BT_WATER" not in identities
        assert has_bluetooth_control_identity("HIC801W", 279) is False

    def test_variant_278_is_the_portless_main_record(self):
        """278 is the pairable main record the ground-truth document
        describes: no datapoints, so nothing on this decode path reads it."""
        assert get_catalog_entry("HIC801W", 278) == []

    def test_resolution_requires_modelcode_the_committed_catalog_carries_no_disambiguator(self):
        """The committed catalog snapshot carries no hasDistribution or
        similar per-variant flag to fall back on (product_catalog.py trims
        each variant record to {"portNumber", "dp"}): HIC801W has two
        modelCode variants and no uncoded ("*") bucket, so a lookup with no
        modelCode resolves to None rather than guessing between them. This is
        the property this rests on -- the resolution the decoder relies on is
        the modelCode one, and nothing else could stand in for it even if a
        caller tried."""
        assert get_catalog_entry("HIC801W", None) is None
        assert get_catalog_entry("HIC801W", 279) is not None
        assert get_catalog_entry("HIC801W", 278) is not None


class TestHic801wWidthToleranceProperty:
    """The STA_TS_DET width tolerance, stated as a property over the whole corpus rather than a single
    happy-path frame: the catalog's declared width and the wire's actual
    width for STA_TS_DET disagree on every capture, and the shape check must
    never turn that disagreement into a rejection."""

    def test_declared_width_disagrees_with_every_frame_and_none_are_rejected(self):
        declared_len = next(e["dpLen"] for e in get_catalog_entry("HIC801W", 279) if e["identity"] == "STA_TS_DET")
        assert declared_len == 4

        for label, raw in SAMPLE_HIC801W_ALL_FRAMES.items():
            b = _parse_rainpoint_payload(raw)
            fields = {e["field"]: e["value_bytes"] for e in _parse_entries(list(b), False)}
            actual_len = len(fields[38])
            assert actual_len != declared_len, label
            assert actual_len == 2, label
            assert decode_hic801w(raw)["decoder"] == "hic801w_hex", label
