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
