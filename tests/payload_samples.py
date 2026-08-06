"""Shared test payload constants for RainPoint device tests."""

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

# Real hex (11#) payload from a reporter's HTV345FRF (3-zone valve), all zones idle.
# Records are reordered: the two leading 0x9F usage records precede the 0x17/0xE1
# header. Unlike the HTV405FRF frame below it does carry usage records, at dp
# 0x29/0x2A/0x2B for zones 1/2/3.
SAMPLE_HTV345_TLV_PAYLOAD = (
    "11#"
    "2A9F00000000299F0000000017E1CA0019D8001AD8001BD8001D201E201F2018DC01"
    "21B70000000022B70000000023B70000000025AD000026AD000027AD00002B9F00000000"
    "FEFF0FEC4BCB19"
)

# Real hex (11#) payload from a reporter's HTV405FRF (4-zone valve), all zones idle.
# This frame carries no 0x9F usage records at all, so its zones decode with usage
# absent rather than zero.
#   DP 0x18 type 0xDC value 0x01 (hub online)
#   DP 0x19-0x1C type 0xD8 value 0x00 (zones 1-4 closed)
#   DP 0x25-0x28 type 0xAD value 0x0000 (zone 1-4 durations = 0s)
SAMPLE_HTV405_TLV_PAYLOAD = (
    "11#17E1CD0019D8001AD8001BD8001CD8001D201E201F20202018DC01"
    "21B70000000022B70000000023B70000000024B70000000025AD000026AD000027AD000028AD0000FEFF0F5B55D219"
)

# Real full hex (11#) status frames from the maintainer's HTV245FRF (2-zone valve).
# Both end with a [dp_id 0xFE][STA_REPTIME header 0xFF 0x0F][4-byte packed wall
# clock] record that the dp_id/type scan skips; the 0xFF 0x0F pair is that
# record's extended-type header, not a battery word. Battery is the 0x18 0xDC
# record near the front. The second capture (July 4) has zone 2 mid-run: nonzero
# last-event time (dp 0x22) and duration (dp 0x26), and its report time unpacks
# to July 4.
SAMPLE_HTV245_FULL_IDLE_PAYLOAD = (
    "11#17E1DB0018DC0119D8001AD8001D201E2021B70000000022B70000000025AD000026AD0000299FA50100002A9F30000000FEFF0F0270F219"
)
SAMPLE_HTV245_FULL_ZONE2_ACTIVE_PAYLOAD = (
    "11#17E1D90018DC0119D8001AD8211D201E2021B70000000022B77327C91925AD000026AD7C0B299F140100002A9F00000000FEFF0F331AC919"
)

# Real hex (10#) payloads from a reporter's HTV145FRF single-outlet WiFi water timer.
# This model ships a compact [type_byte][value...] marker stream, not the HTV213FRF
# dp_id/type/value layout. Markers: 0xE1 header (byte[1]=signed RSSI), 0xDC hub online,
# 0xD8 zone state (0x21 open / 0x00 closed), 0xAD 2-byte LE duration seconds, 0xFF terminator.
#   Closed sample: hub online, zone 1 closed, duration 0s, RSSI -68 dBm.
SAMPLE_HTV145_CLOSED_PAYLOAD = "10#E1BC00DC01D80020B700000000AD00009F95110000FF0F5D81D019"
#   Open sample: hub online, zone 1 open (0x21), duration 1200s (20 min), RSSI -62 dBm.
SAMPLE_HTV145_OPEN_PAYLOAD = "10#E1C200DC01D82120B7AE44E319ADB0049FA8020000FF0FAE3EE319"

# Real hex (10#) idle payload from a reporter's HTV113FRF single-outlet water timer
# (issue #64). Same marker layout as the HTV145FRF above, so it reuses that decoder.
#   Idle sample: zone 1 closed (0x00), duration 0s, RSSI -63 dBm, battery field 0xFF0F.
SAMPLE_HTV113_IDLE_PAYLOAD = "10#E1C100DC03D80020B700000000AD00009F00000000FF0F9B40D319"

# --- Additional decoder payload constants ---

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
# b[1]=0xC6=198-256=-58 RSSI; b[4]=0x01 STA_BAT; b[6]=0x1A=26% moisture
MOISTURE_SIMPLE_HEX_PAYLOAD = "10#E1C600DC01881AFF0F5E21F718"

# Second HCS026FRF capture, from a device added on 2026-07-29. Its STA_REPTIME
# record unpacks to the moment it was pulled, which is what pins the packed
# format's year base at 2020.
# E1 C4 00 DC 01 88 25 FF 0F E1 C4 FA 19
# b[1]=0xC4=196-256=-60 RSSI; b[4]=0x01 STA_BAT; b[6]=0x25=37% moisture
MOISTURE_SIMPLE_SECOND_CAPTURE_PAYLOAD = "10#E1C400DC018825FF0FE1C4FA19"

# Minimal hex payload for basic decoder smoke tests (2+ bytes: RSSI extractable).
# E1=preamble, B0=rssi raw (176-256=-80), DC=tag, 01=value
BASIC_HEX_PAYLOAD = "10#E1B000DC01"

# Synthetic TLV payload for decode_valve_hub (HTV0540FRF).
# DP 0x18 type 0xDC value 0x01 (hub online)
# DP 0x19 type 0xD8 value 0x01 (zone 1 open)
# DP 0x25 type 0xAD value 0x2C01 (LE 300 seconds = zone 1 duration)
VALVE_HUB_TLV_PAYLOAD = (
    "11#"
    + bytes(
        [
            0x18,
            0xDC,
            0x01,  # hub online
            0x19,
            0xD8,
            0x01,  # zone 1 open
            0x25,
            0xAD,
            0x2C,
            0x01,  # zone 1 duration = 300s (LE: 0x012C = 300)
        ]
    ).hex()
)

# HWS019WRF-V2 CSV/semicolon payload from docstring.
HWS019WRF_V2_PAYLOAD = "1,0,1;707(707/694/1),42(42/39/1),P=9709(9709/9701/1),"

# --- Additional synthetic payloads for coverage push ---

# Synthetic TLV payload for decode_valve_hub (MODEL_VALVE_HUB / HTV0540FRF).
# Used to cover the MODEL_VALVE_HUB branch of
# RainPointValveEntity._apply_response_state (only MODEL_VALVE_245 was previously covered).
# DP 0x18 type 0xDC value 0x01 (hub online)
# DP 0x19 type 0xD8 value 0x01 (zone 1 open)
# DP 0x25 type 0xAD value 0xAC01 -> LE 0x01AC = 428 seconds (zone 1 duration)
VALVE_HUB_APPLY_TLV_PAYLOAD = (
    "11#"
    + bytes(
        [
            0x18,
            0xDC,
            0x01,
            0x19,
            0xD8,
            0x01,
            0x25,
            0xAD,
            0xAC,
            0x01,
        ]
    ).hex()
)

# Minimal synthetic MOISTURE_FULL data dict for parametrized sensor tests.
# Not a raw payload: this is the already-decoded dict, stored as the "data"
# side of a coordinator entry.
SYNTHETIC_MOISTURE_FULL_DATA = {
    "type": "moisture_full",
    "moisture_percent": 42,
    "temperature_c": 20.5,
    "illuminance_lux": 1000.0,
    "rssi_dbm": -75,
    "battery_percent": 80,
}

# Minimal synthetic TEMPHUM data dict. Keys match the field names read by the
# six RainPointTempHum* classes in sensor.py.
SYNTHETIC_TEMPHUM_DATA = {
    "type": "temphum",
    "tempcurrent": 21.5,
    "temphigh": 25.0,
    "templow": 18.0,
    "humiditycurrent": 55,
    "humidityhigh": 70,
    "humiditylow": 40,
    "rssi_dbm": -72,
    "battery_percent": 88,
}

# Minimal synthetic FLOWMETER data dict. Keys match the seven RainPointFlow*
# classes in sensor.py. Flow battery is `flowbatt`, not `battery_percent`.
SYNTHETIC_FLOWMETER_DATA = {
    "type": "flowmeter",
    "flowcurrentused": 12.3,
    "flowcurrenduration": 60,
    "flowlastused": 45.6,
    "flowlastusedduration": 300,
    "flowtotaltoday": 78.9,
    "flowtotal": 1234.5,
    "flowbatt": 77,
    "rssi_dbm": -68,
}

# Minimal synthetic CO2 data dict for the six RainPointCO2* sensor classes.
# Keys are `co2`, `co2low`, `co2high`, `co2temp`, `co2humidity`, `co2batt`
# (all prefixed "co2", not "co2_current" etc.).
SYNTHETIC_CO2_DATA = {
    "type": "co2",
    "co2": 450,
    "co2low": 300,
    "co2high": 600,
    "co2temp": 22.0,
    "co2humidity": 48,
    "co2batt": 82,
    "rssi_dbm": -70,
}

# Minimal synthetic POOL data dict for the four RainPointPool* sensor classes.
# Keys are the unprefixed `tempcurrent` / `temphigh` / `templow` / `tempbatt`
# (shared with TempHum classes; disambiguation is per-model).
SYNTHETIC_POOL_DATA = {
    "type": "pool",
    "tempcurrent": 26.5,
    "temphigh": 28.0,
    "templow": 24.0,
    "tempbatt": 90,
    "rssi_dbm": -65,
}

# Minimal synthetic POOL_PLUS data dict for the nine RainPointPoolPlus* classes.
# Pool temps use `pool_*`, ambient temps use `ambient_*`, humidity uses
# `humidity_current` / `humidity_high` / `humidity_low` (underscored, unlike
# TempHum's `humiditycurrent`).
SYNTHETIC_POOL_PLUS_DATA = {
    "type": "pool_plus",
    "pool_tempcurrent": 27.0,
    "pool_temphigh": 29.0,
    "pool_templow": 23.0,
    "ambient_tempcurrent": 20.5,
    "ambient_temphigh": 25.0,
    "ambient_templow": 15.0,
    "humidity_current": 52,
    "humidity_high": 70,
    "humidity_low": 40,
    "battery_percent": 76,
    "rssi_dbm": -69,
}

# A synthetic 10# (flat) payload built for the generic-decoder and catalog
# tests: a compact-form STA_ALARM entry the anchor model does not declare,
# followed by four wide-form entries (STA_TEM, STA_RH, STA_BAT, STA_RSSI)
# whose structural indices match the anchor model's dpCodes.
SAMPLE_UNSUPPORTED_MULTI_SENSOR_PAYLOAD = "10#208500968832DC64E0C5"

# The real catalog model the enrichment tests anchor on. Chosen because it is
# a genuine entry in the committed snapshot that is NOT in HAND_WRITTEN_MODELS
# (so the generic path actually runs for it), has exactly one modelCode
# variant (so lookups need no model_code), declares dpCodes 9/10/31/32 to match
# the payload above, and does NOT declare dpCode 2 - which keeps the
# "decoded field the catalog knows nothing about" case alive for STA_ALARM.
# Its STA_TEM is S16, so it also exercises the signed/multi-byte annotation.
CATALOG_ANCHOR_MODEL = "HCS702B"


# Real hex (11#) payload from the maintainer's HTV210B, captured after moving it
# off Bluetooth onto the hub, both zones idle and no usage history yet. Kept for
# the RSSI record it carries: 17e1b401, where 0xb4 is -76 dBm (the value the
# RainPoint app showed at the time) and the trailing 0x01 is the PHY. Every other
# frame we hold carries 0x00 there, so this is the only capture proving that
# byte varies. Decoded by decode_htv210b; also exercises the shared byte
# scanning, since the HTV213 family's RSSI extractor must read this frame too.
SAMPLE_HTV210B_TLV_PAYLOAD = (
    "11#37FF0D0000000018DC0117E1B40119D8001AD8001D201E2021B70000000022B7000000002"
    "5AF0000000026AF00000000299F000000002A9F0000000038FF0D00000000FEFF0F1527FB19"
)

# Captured controlWorkModeDP response "state" blobs, 2026-08-05 session. Each
# is the comma form decode_htv210b_dp_state reads: a leading mode digit, then the
# same self-describing record stream the poll-path TLV frame carries, but with
# no dp_id prefix and describing exactly one zone.
SAMPLE_HTV210B_DP_OPEN_60S_STATE = "1,D821AF3C000000B7D1230B1A"
SAMPLE_HTV210B_DP_OPEN_120S_STATE = "1,D821AF78000000B7D1230B1A"
SAMPLE_HTV210B_DP_CLOSE_STATE = "0,D800AF00000000B700000000"

# Verbatim pipe-delimited hub-level connectivity frames from the 2026-07-31 UAT
# on v1.12.0b1, both delivered on thing/service/property/set. Section 1
# decomposes as the "#P" prefix, a 12-digit YYMMDDHHMMSS stamp in UTC, "0000",
# the 8-digit account id 16822282, and the 6-digit mid 236547; section 2 is the
# connected flag; section 3 is the change timestamp; section 4 is a propVer
# matching the next poll's.
#
# Section 3 is close to, but NOT the same value as, the poll's `connected`
# entry `time`. A second live capture (2026-08-01) measured the poll trailing
# the frame by a few ms on both edges: disconnect 1785562863072 vs
# 1785562863078, reconnect 1785523062039 vs 1785523062046. They are two cloud
# timestamps for one edge, not one shared field. This does not weaken the
# ordering guard -- real connect/disconnect edges are seconds to minutes
# apart, so a few ms of skew cannot invert them -- but do not write a test
# that asserts the two are equal.
SAMPLE_HUB_DISCONNECT_FRAME = "#P260731181730000016822282236547|0|1785521850011|112882164350#"
# Reconstructed from the disconnect frame's shape: the capture recorded the
# reconnect frame elided as "...|1|1785523062039|112882164351#". The
# reconstruction is arithmetically consistent: 1785523062039 ms is
# 2026-07-31T18:37:42.039+00:00, matching the measured 11:37:42 local
# reconnect edge, and the delta from the disconnect frame's 1785521850011 ms
# is exactly 20m12s -- the measured 11:17:30 -> 11:37:42 gap. The 2026-08-01
# UAT captured a real reconnect frame
# ("#P260801054717000016822282236547|1|1785563237200|113059798638#") that
# parses to the same shape, confirming the reconstruction.
SAMPLE_HUB_RECONNECT_FRAME = "#P260731183742000016822282236547|1|1785523062039|112882164351#"
# The mid and expected ISO changed_at strings both frames above decode to, so
# tests assert against one shared definition rather than repeating literals.
SAMPLE_HUB_FRAME_MID = 236547
SAMPLE_HUB_DISCONNECT_CHANGED_AT_ISO = "2026-07-31T18:17:30.011000+00:00"
SAMPLE_HUB_RECONNECT_CHANGED_AT_ISO = "2026-07-31T18:37:42.039000+00:00"

# A third "#P" frame family, captured verbatim on the same downlink topic
# during the 2026-08-01 UAT, moments after a hub reconnect. It must be
# rejected, and it is the only observed (rather than hand-mutated) payload
# that has to be. Three properties make it the adversarial case the strict
# recognition rule exists for, and none of the synthetic mutations cover them:
# it fails two clauses at once (three sections, and an empty section 2 rather
# than a literal 0/1); its section-1 tail is 182509, the *hid*, not a mid; and
# its account slot is 16822204, proving that field is not fixed across frames.
# No single relaxed clause misreads it: the section-count check and the
# 0/1 check each reject it on their own, so both would have to go, and the
# mid cross-check is a third independent gate behind them (the tail is
# 182509, so a client whose mid is 236547 drops it regardless). It would take
# all three giving way to read this as "hub 182509 disconnected" against a
# real record, which is the point -- the layers are why that misread is hard.
SAMPLE_NON_HUB_PIPE_FRAME = "#P260801054717000016822204182509||113060569563#"
