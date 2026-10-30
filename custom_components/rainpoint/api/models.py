"""Model identifiers and the set of models with a hand-written decoder."""

# Known models (original devices)
MODEL_HCS026FRF = "HCS026FRF"  # Moisture only
MODEL_HCS021FRF = "HCS021FRF"  # Moisture + temp + lux
MODEL_HCS012ARF = "HCS012ARF"  # Rain gauge
MODEL_HCS044FRF = "HCS044FRF"  # Rain detector (wet/dry only, no rainfall totals)
MODEL_HCS014ARF = "HCS014ARF"  # Temperature/Humidity
MODEL_HCS008FRF = "HCS008FRF"  # Flowmeter
MODEL_HCS0530THO = "HCS0530THO"  # CO2/Temp/Humidity
MODEL_HCS0528ARF = "HCS0528ARF"  # Pool/Temperature
MODEL_HCS015ARF_PLUS = "HCS015ARF+"  # Pool + Ambient temp/humidity
MODEL_HWS019WRF_V2 = "HWS019WRF-V2"  # Smart+ Irrigation Display Hub

# Legacy aliases for backward compatibility
MODEL_MOISTURE_SIMPLE = MODEL_HCS026FRF
MODEL_MOISTURE_FULL = MODEL_HCS021FRF
MODEL_RAIN = MODEL_HCS012ARF
MODEL_TEMPHUM = MODEL_HCS014ARF
MODEL_FLOWMETER = MODEL_HCS008FRF
MODEL_CO2 = MODEL_HCS0530THO
# Not a second model: sensor.py keys its entity factories off this name, but a
# registry keyed on both spellings holds one entry and silently drops the other.
MODEL_POOL = MODEL_HCS0528ARF
MODEL_POOL_PLUS = MODEL_HCS015ARF_PLUS
MODEL_DISPLAY_HUB = MODEL_HWS019WRF_V2

# === HCS Sensor Series (v1.3.0) ===

# Moisture-only sensors
MODEL_HCS005FRF = "HCS005FRF"  # Moisture-only sensor

# Multi-sensors (temp + moisture + lux)
MODEL_HCS024FRF_V1 = "HCS024FRF-V1"  # Multi-sensor (temp+moisture+lux)

# Temperature/Humidity sensors

# Pool temperature sensors
MODEL_HCS015ARF = "HCS015ARF"  # Pool temperature sensor
# Note: MODEL_HCS0528ARF defined above as primary pool sensor

# === Valve Controllers (v1.2.0) ===
MODEL_HTV113FRF = "HTV113FRF"  # Single-outlet water timer (same 10# compact format as HTV145FRF)
MODEL_HTV145FRF = "HTV145FRF"  # Single-outlet WiFi water timer (10# compact valve payload)
MODEL_HTV213FRF = "HTV213FRF"  # Single-zone RF irrigation timer (similar to HTV0540FRF)
MODEL_HTV245FRF = "HTV245FRF"  # Irrigation valve (similar to HTV0540FRF)
MODEL_HTV345FRF = "HTV345FRF"  # Irrigation valve variant (similar to HTV245FRF)
MODEL_HTV405FRF = "HTV405FRF"  # 4-zone irrigation valve variant (similar to HTV245FRF)
MODEL_HTV445FRF = "HTV445FRF"  # 4-zone irrigation valve variant (similar to HTV405FRF)
MODEL_HTV0540FRF = "HTV0540FRF"  # Multi-zone valve hub (fully supported)
MODEL_HTV157B = "HTV157B"  # Single-outlet water timer; same 10# frame as the HTV113FRF/HTV145FRF, 4-byte duration
MODEL_HTP160FRF = "HTP160FRF"  # Single-outlet RF water timer; same 10# frame again, with no STA_LASTUSAGE record
MODEL_HTV210B = "HTV210B"  # Bluetooth valve; reports over RF as a normal hub sub-device once hub-paired
MODEL_HIC801W = "HIC801W"  # 8-station irrigation controller; catalog variant 279 is the accessory
# record carrying the stations, while 278 is the pairable main record with no ports.

# Legacy valve aliases
MODEL_VALVE_113 = MODEL_HTV113FRF
MODEL_VALVE_145 = MODEL_HTV145FRF
MODEL_VALVE_213 = MODEL_HTV213FRF
MODEL_VALVE_245 = MODEL_HTV245FRF
MODEL_VALVE_345 = MODEL_HTV345FRF
MODEL_VALVE_405 = MODEL_HTV405FRF
MODEL_VALVE_445 = MODEL_HTV445FRF
MODEL_VALVE_HUB = MODEL_HTV0540FRF

# Every model with a hand-written, fixture-validated decoder (mirrors the
# coordinator's DECODER_REGISTRY keys) plus MODEL_DISPLAY_HUB, which is
# dispatched as a special case rather than through the registry. This is the
# authoritative membership set behind is_hand_written_model() in api/trust.py:
# any model here is structurally excluded from the model-agnostic generic
# decode path. Defined here (not read from DECODER_REGISTRY directly) so the
# api package stands alone; the integration's drift test keeps it in sync with
# the registry.
HAND_WRITTEN_MODELS: frozenset[str] = frozenset(
    {
        MODEL_MOISTURE_SIMPLE,
        MODEL_MOISTURE_FULL,
        MODEL_RAIN,
        MODEL_HCS044FRF,
        MODEL_TEMPHUM,
        MODEL_FLOWMETER,
        MODEL_CO2,
        MODEL_POOL_PLUS,
        MODEL_VALVE_HUB,
        MODEL_VALVE_113,
        MODEL_VALVE_145,
        MODEL_VALVE_213,
        MODEL_VALVE_245,
        MODEL_VALVE_345,
        MODEL_VALVE_405,
        MODEL_VALVE_445,
        MODEL_HTV157B,
        MODEL_HTP160FRF,
        MODEL_HTV210B,
        MODEL_HIC801W,
        MODEL_HCS005FRF,
        MODEL_HCS024FRF_V1,
        MODEL_HCS015ARF,
        MODEL_HCS0528ARF,
        MODEL_DISPLAY_HUB,
    }
)
