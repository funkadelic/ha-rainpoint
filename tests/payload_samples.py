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

# --- Phase 5 Plan 01: Additional decoder payload constants ---

# HCS021FRF (moisture_full) hex payload from docstring.
# E1 A2 00 DC 01 85 AB 02 88 1F C6 60 06 00 FF 0F FA 28 F7 18
# b[1]=0xA2=162-256=-94 RSSI; b[6:7]=0x02AB=683 F*10 -> 68.3F -> 20.2C
# b[9]=0x1F=31%; b[11:12]=0x0660=1632 lux*10 -> 163.2 lux
MOISTURE_FULL_HEX_PAYLOAD = "10#E1A200DC0185AB02881FC6600600FF0FFA28F718"

# HCS021FRF (moisture_full) ASCII payload.
# Format: [flags],[rssi],[flags];[temp_raw_F10],[moisture],[lux_data]
# temp_raw=694 -> 69.4F -> (69.4-32)*5/9 = 20.78C; moisture=70%; lux from G=292478
MOISTURE_FULL_ASCII_PAYLOAD = "1,-73,1;694,70,G=292478"

# HCS012ARF (rain gauge) hex payload from docstring.
# E1 00 00 FD 04 00 00 FD 05 4E 07 FD 06 4E 07 DC 01 97 4E 07 00 00 FF 0F 04 10 F7 18
# b[5:6]=0x0000=0.0mm last hour; b[9:10]=0x074E=1870/10=187.0mm last 24h
# b[13:14]=0x074E=187.0mm last 7d; b[18:19]=0x074E=187.0mm total
RAIN_HEX_PAYLOAD = "10#E10000FD040000FD054E07FD064E07DC01974E070000FF0F0410F718"

# HCS026FRF (moisture_simple) hex payload from docstring.
# E1 C6 00 DC 01 88 1A FF 0F 5E 21 F7 18
# b[1]=0xC6=198-256=-58 RSSI; b[6]=0x1A=26% moisture
MOISTURE_SIMPLE_HEX_PAYLOAD = "10#E1C600DC01881AFF0F5E21F718"

# Minimal hex payload for basic decoder smoke tests (2+ bytes: RSSI extractable).
# E1=preamble, B0=rssi raw (176-256=-80), DC=tag, 01=value
BASIC_HEX_PAYLOAD = "10#E1B000DC01"

# Synthetic TLV payload for decode_valve_hub (HTV0540FRF).
# DP 0x18 type 0xDC value 0x01 (hub online)
# DP 0x19 type 0xD8 value 0x01 (zone 1 open)
# DP 0x25 type 0xAD value 0x2C01 (LE 300 seconds = zone 1 duration)
VALVE_HUB_TLV_PAYLOAD = "11#" + bytes([
    0x18, 0xDC, 0x01,        # hub online
    0x19, 0xD8, 0x01,        # zone 1 open
    0x25, 0xAD, 0x2C, 0x01,  # zone 1 duration = 300s (LE: 0x012C = 300)
]).hex()

# HWS019WRF-V2 CSV/semicolon payload from docstring.
HWS019WRF_V2_PAYLOAD = "1,0,1;707(707/694/1),42(42/39/1),P=9709(9709/9701/1),"
