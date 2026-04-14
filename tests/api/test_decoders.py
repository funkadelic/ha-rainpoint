"""Seed tests for RainPoint device decoders."""

from custom_components.rainpoint.api import decode_htv213frf_valve

# Sample ASCII payload from HTV245FRF valve hub.
# This is the known-good payload used by scripts/pre-commit-docker-test.sh.
# Format: [flags],[rssi],[flags];[zone1_data]|[zone2_data]
SAMPLE_HTV245_ASCII_PAYLOAD = "1,-84,1;0,149,0,0,0,0|0,6,0,0,0,0"

# Expected top-level keys the decoder must return for an ASCII payload.
# Phase 3 will assert exact field values; Phase 2 just proves the harness works.
EXPECTED_KEYS = {"type", "zones", "rssi_dbm", "raw_bytes"}


class TestDecodeHtv213frfValve:
    """Seed tests for decode_htv213frf_valve (shared by HTV213FRF and HTV245FRF)."""

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
