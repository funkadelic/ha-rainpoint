# Display Hub model constant
DOMAIN = "rainpoint"

# Integration version
VERSION = "1.7.1"  # x-release-please-version

# Issue tracker URL
ISSUE_URL = "https://github.com/funkadelic/ha-rainpoint/issues"


# Helper function for debug messages with version
def debug_with_version(message: str) -> str:
    """Format debug message with integration version."""
    return f"[RainPoint v{VERSION}] {message}"


CONF_AREA_CODE = "area_code"
CONF_COUNTRY = "country"  # ISO 3166-1 alpha-2, source of truth for the UI dropdown
CONF_EMAIL = "email"
CONF_PASSWORD = "password"
CONF_HIDS = "hids"  # list of selected home IDs

DEFAULT_SCAN_INTERVAL = 120  # seconds

# Config entry data keys
CONF_TOKEN = "token"
CONF_REFRESH_TOKEN = "refresh_token"
CONF_TOKEN_EXPIRES_AT = "token_expires_at"

# Debug data submission
CONF_DEBUG_ENABLED = "debug_enabled"
CONF_DEBUG_AUTO_SUBMIT = "debug_auto_submit"
CONF_DEBUG_LAST_SUBMISSION = "debug_last_submission"

DEBUG_WORKER_URL = ""
DEBUG_SUBMISSION_INTERVAL = 86400  # 24 hours in seconds

# === Push channel (MQTT) ===
CONF_PUSH_ENABLED = "push_enabled"
MQTT_TOPIC_PROPERTY_SET = "/sys/{product_key}/{device_name}/thing/service/property/set"
MQTT_TOPIC_EVENT_POST = "/sys/{product_key}/{device_name}/thing/event/property/post"
MQTT_BROKER_HOST_TEMPLATE = "{product_key}.iot-as-mqtt.us-west-1.aliyuncs.com"
MQTT_BROKER_PORT = 1883
MQTT_KEEPALIVE = 30

# Push envelope layout (confirmed against live hardware).
# The state-carrying message arrives as a standard AliCloud IoT payload whose
# params.param value is a pipe-delimited string; one of its sections is an inner
# JSON object keyed by sub-device id. Only "D"-prefixed keys are sub-device
# status; each carries the same raw value string the poll-path decoders consume.
MQTT_PUSH_METHOD = "thing.service.property.set"
MQTT_PUSH_PARAMS_KEY = "param"
MQTT_PUSH_SECTION_DELIMITER = "|"
MQTT_PUSH_SUBDEVICE_PREFIX = "D"
MQTT_PUSH_VALUE_FIELD = "value"
MQTT_PUSH_TIME_FIELD = "time"

# Upper bound on an inbound push payload. Real envelopes are ~425 bytes; anything
# far larger is junk (or hostile) and is dropped before parsing. Generous so a
# firmware that grows the envelope is not rejected, small enough to bound work.
MQTT_PUSH_MAX_PAYLOAD_BYTES = 8192

# Push observability: hub-level diagnostic entities that surface the live push
# connection state and the age of the last received message. The unique_id is
# built by appending these suffixes to the hub's base unique_id, so they stay
# stable across restarts and are never regenerated.
PUSH_CONNECTED_UNIQUE_ID_SUFFIX = "push_connected"
PUSH_LAST_MESSAGE_UNIQUE_ID_SUFFIX = "push_last_message"

# Push watchdog: surfaces a disconnected push channel as a dismissible
# Settings > Repairs issue and clears it on recovery. Detection-only -- it never
# reconnects (the supervisor owns that) and never changes the poll cadence.
#
# Liveness = connected, OR a message arrived within the short message-grace
# window. The grace window exists only to bridge the brief connected=False gap
# while the supervisor tears down the old session and reconnects the new one at
# a renewal boundary; it is deliberately short so it does not stack with the
# dead-after clock (a message just before a disconnect must not delay flagging by
# a second full dead-after window). The channel is flagged only after staying
# non-functional continuously past the dead-after threshold, so worst-case
# latency to a raised issue is dead-after plus at most one grace window.
#
# Known limitation: a channel that stays TCP-connected but silently stops
# delivering data (a subscription detached cloud-side) is NOT flagged, because
# from message-absence alone it is indistinguishable from a healthy but idle
# channel (no device activity legitimately means no pushes). Detecting that would
# need an active probe/heartbeat; it is out of scope for this cut. Values are a
# conservative first cut chosen without field reconnect data (the renewal cycle
# is ~570s) and are expected to be tuned once real outage data exists.
PUSH_WATCHDOG_SCAN_INTERVAL_SECONDS = 60
PUSH_WATCHDOG_DEAD_AFTER_SECONDS = 900
PUSH_WATCHDOG_MESSAGE_GRACE_SECONDS = 180
PUSH_WATCHDOG_ISSUE_ID = "push_channel_down"

# Known models (original devices)
MODEL_HCS026FRF = "HCS026FRF"  # Moisture only
MODEL_HCS021FRF = "HCS021FRF"  # Moisture + temp + lux
MODEL_HCS012ARF = "HCS012ARF"  # Rain gauge
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
MODEL_POOL = MODEL_HCS0528ARF
MODEL_POOL_PLUS = MODEL_HCS015ARF_PLUS
MODEL_DISPLAY_HUB = MODEL_HWS019WRF_V2

# === HCS Sensor Series (v1.3.0) ===

# Moisture-only sensors
MODEL_HCS005FRF = "HCS005FRF"  # Moisture-only sensor
MODEL_HCS003FRF = "HCS003FRF"  # Moisture-only sensor

# Multi-sensors (temp + moisture + lux)
MODEL_HCS024FRF_V1 = "HCS024FRF-V1"  # Multi-sensor (temp+moisture+lux)
MODEL_HCS044FRF = "HCS044FRF"  # Multi-sensor device
MODEL_HCS666FRF = "HCS666FRF"  # Sensor variant (similar to HCS021FRF)
MODEL_HCS666RFR_P = "HCS666RFR-P"  # Sensor variant with plus features
MODEL_HCS999FRF = "HCS999FRF"  # Advanced sensor variant
MODEL_HCS999FRF_P = "HCS999FRF-P"  # Advanced sensor variant with plus features
MODEL_HCS666FRF_X = "HCS666FRF-X"  # Extended sensor variant

# Temperature/Humidity sensors
MODEL_HCS027ARF = "HCS027ARF"  # Temperature/humidity sensor
MODEL_HCS016ARF = "HCS016ARF"  # Temperature/humidity sensor
MODEL_HCS701B = "HCS701B"  # Wall-mounted sensor
MODEL_HCS596WB = "HCS596WB"  # Weather station base
MODEL_HCS596WB_V4 = "HCS596WB-V4"  # Weather station base v4
MODEL_HCS706ARF = "HCS706ARF"  # Environmental sensor
MODEL_HCS802ARF = "HCS802ARF"  # Environmental sensor
MODEL_HCS048B = "HCS048B"  # Compact sensor device
MODEL_HCS888ARF_V1 = "HCS888ARF-V1"  # Multi-function sensor v1
MODEL_HCS0600ARF = "HCS0600ARF"  # Advanced environmental sensor

# Pool temperature sensors
MODEL_HCS015ARF = "HCS015ARF"  # Pool temperature sensor
# Note: MODEL_HCS0528ARF defined above as primary pool sensor

# === Valve Controllers (v1.2.0) ===
MODEL_HTV213FRF = "HTV213FRF"  # Single-zone RF irrigation timer (similar to HTV0540FRF)
MODEL_HTV245FRF = "HTV245FRF"  # Irrigation valve (similar to HTV0540FRF)
MODEL_HTV345FRF = "HTV345FRF"  # Irrigation valve variant (similar to HTV245FRF)
MODEL_HTV405FRF = "HTV405FRF"  # 4-zone irrigation valve variant (similar to HTV245FRF)
MODEL_HTV0540FRF = "HTV0540FRF"  # Multi-zone valve hub (fully supported)

# Legacy valve aliases
MODEL_VALVE_213 = MODEL_HTV213FRF
MODEL_VALVE_245 = MODEL_HTV245FRF
MODEL_VALVE_345 = MODEL_HTV345FRF
MODEL_VALVE_405 = MODEL_HTV405FRF
MODEL_VALVE_HUB = MODEL_HTV0540FRF

VALVE_MODELS = {
    MODEL_VALVE_HUB,
    MODEL_VALVE_213,
    MODEL_VALVE_245,
    MODEL_VALVE_345,
    MODEL_VALVE_405,
}
