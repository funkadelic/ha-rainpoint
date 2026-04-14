"""Shared test payload constants for RainPoint device tests (Phase 3)."""

# Real ASCII payload from maintainer's HTV245FRF device.
# Format: [flags],[rssi],[flags];[zone1_data]|[zone2_data]
SAMPLE_HTV245_ASCII_PAYLOAD = "1,-84,1;0,149,0,0,0,0|0,6,0,0,0,0"

# Synthetic TLV payload for HTV245FRF device (11# prefix).
# Constructed from TLV spec to exercise all code paths:
#   DP 0x18 type 0xDC value 0x01 (hub online)
#   DP 0x19 type 0xD8 value 0x01 (zone 1 open)
#   DP 0x1A type 0xD8 value 0x00 (zone 2 closed)
#   DP 0x25 type 0xAD value 0x3C00 (zone 1 duration = 60s, little-endian)
#   DP 0x26 type 0xAD value 0x0000 (zone 2 duration = 0s)
SAMPLE_HTV245_TLV_PAYLOAD = "11#18dc0119d8011ad80025ad3c0026ad0000"
