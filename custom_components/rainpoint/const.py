from pathlib import Path

# Display Hub model constant
DOMAIN = "rainpoint"

# Integration version
VERSION = "1.9.0"  # x-release-please-version

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

# === Generic (catalog-driven) sensor entity factory ===
CONF_GENERIC_ENTITIES_ENABLED = "generic_entities_enabled"
# Shared by the generic factory and its entity-registry cleanup sweep, so both
# name the same unique_id vocabulary.
UNIQUE_ID_PREFIX = "rainpoint_"
GENERIC_UNIQUE_ID_MARKER = "_generic_"

# === Generic (catalog-driven) control entity factory ===
CONF_GENERIC_CONTROL_ENABLED = "generic_control_enabled"
# The control marker is nested inside GENERIC_UNIQUE_ID_MARKER (checkpoint
# decision: option-a) rather than given a top-level marker of its own. That
# keeps the existing __init__.py registry sweep guard (which matches on
# GENERIC_UNIQUE_ID_MARKER alone) already matching every control row, with no
# second substring guard needed. The trailing "ctl_" segment is what
# distinguishes a control row from a sensor row within the shared substring,
# per D-11's requirement that the two namespaces stay distinguishable -- every
# namespace test must assert on this FULL marker, never on GENERIC_UNIQUE_ID_MARKER
# alone, which a control unique_id also contains.
GENERIC_CONTROL_UNIQUE_ID_MARKER = f"{GENERIC_UNIQUE_ID_MARKER}ctl_"
# Trailing suffix for the duration companion entity (added in a later plan),
# mirroring the trusted zone-duration convention ("_zone{n}_duration"): the
# companion's unique_id is the control entity's own id plus this suffix, never
# a separate namespace of its own.
GENERIC_CONTROL_DURATION_SUFFIX = "_duration"
# Long enough for the device to actuate and report its new state back to the
# cloud; far shorter than DEFAULT_SCAN_INTERVAL, which remains the backstop
# poll if this delayed refresh still reads a stale state.
GENERIC_CONTROL_REFRESH_DELAY_SECONDS = 8
# Matches the provisional marker icon the generic sensor path already uses --
# one glance tells a reader an entity is unverified, whether it is read-only
# or a control.
GENERIC_CONTROL_MARKER_ICON = "mdi:flask-outline"
# Both the repair issue's translation key and the stem of its per-model,
# per-code issue id (see generic_control._create_command_failed_issue). The
# issue id itself is the dedup key -- two failures with the same model and
# the same extracted response code converge on the same id, so a retry loop
# or a multi-zone device raises one issue rather than one per attempt or per
# zone, mirroring how coordinator._notify_unknown_model dedupes on its
# notification id.
GENERIC_CONTROL_ISSUE_ID_PREFIX = "generic_control_command_failed"
# Committed, variant-keyed force-disable list. Each member is a
# (model, modelCode-as-string) tuple, keyed exactly the way the product
# catalog itself is keyed: get_catalog_entry's UNCODED_VARIANT sentinel ("*")
# is the modelCode for a variant the vendor supplied without one. Empty
# because no committed variant is currently known to be misrouted; it exists
# so one misrouted variant can be force-disabled without disabling every
# variant of that model line. Deliberately NOT keyed on bare model strings
# the way HAND_WRITTEN_MODELS is -- a model string alone is not a unique
# catalog key.
GENERIC_CONTROL_OVERRIDE_DISABLED: frozenset[tuple[str, str]] = frozenset()
# No subscribe topics: the observer's productKey policy forbids client
# subscriptions (any SUBSCRIBE force-closes the connection), and the broker
# auto-delivers the hub's thing/service/property/set downlink messages to the
# connected device unsolicited. See _parse_push_envelope for the payload shape.
MQTT_BROKER_HOST_TEMPLATE = "{product_key}.iot-as-mqtt.us-west-1.aliyuncs.com"
# TLS port. The credential's mqttHostUrl advertises the vendor's plaintext 1883,
# but the same broker also serves TLS on 8883. We always connect over TLS and
# ignore the advertised port, verifying the chain against the pinned root below.
MQTT_BROKER_PORT = 8883
MQTT_KEEPALIVE = 30
# Pinned Aliyun IoT private root CA ("Aliyun IoT Root CA", self-signed, valid
# until 2053). The broker's TLS leaf chains to this root, which is absent from
# every public trust store, so it must be supplied explicitly for the handshake
# to verify. Shipped in the package under certs/; its integrity is guarded by a
# test against Aliyun's published MD5.
MQTT_TLS_CA_CERT = str(Path(__file__).parent / "certs" / "ali_iot_ca.crt")

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
MODEL_HTV113FRF = "HTV113FRF"  # Single-outlet water timer (same 10# compact format as HTV145FRF)
MODEL_HTV145FRF = "HTV145FRF"  # Single-outlet WiFi water timer (10# compact valve payload)
MODEL_HTV213FRF = "HTV213FRF"  # Single-zone RF irrigation timer (similar to HTV0540FRF)
MODEL_HTV245FRF = "HTV245FRF"  # Irrigation valve (similar to HTV0540FRF)
MODEL_HTV345FRF = "HTV345FRF"  # Irrigation valve variant (similar to HTV245FRF)
MODEL_HTV405FRF = "HTV405FRF"  # 4-zone irrigation valve variant (similar to HTV245FRF)
MODEL_HTV0540FRF = "HTV0540FRF"  # Multi-zone valve hub (fully supported)

# Legacy valve aliases
MODEL_VALVE_113 = MODEL_HTV113FRF
MODEL_VALVE_145 = MODEL_HTV145FRF
MODEL_VALVE_213 = MODEL_HTV213FRF
MODEL_VALVE_245 = MODEL_HTV245FRF
MODEL_VALVE_345 = MODEL_HTV345FRF
MODEL_VALVE_405 = MODEL_HTV405FRF
MODEL_VALVE_HUB = MODEL_HTV0540FRF

VALVE_MODELS = {
    MODEL_VALVE_HUB,
    MODEL_VALVE_113,
    MODEL_VALVE_145,
    MODEL_VALVE_213,
    MODEL_VALVE_245,
    MODEL_VALVE_345,
    MODEL_VALVE_405,
}

# Every model with a hand-written, fixture-validated decoder (mirrors the
# coordinator's DECODER_REGISTRY keys) plus MODEL_DISPLAY_HUB, which is
# dispatched as a special case rather than through the registry. This is the
# authoritative membership set behind is_hand_written_model() in api/trust.py:
# any model here is structurally excluded from the model-agnostic generic
# decode path. Defined here (not read from DECODER_REGISTRY directly) so it
# can be imported without pulling in coordinator.py and risking a circular
# import; a drift test keeps it in sync with the registry.
HAND_WRITTEN_MODELS: frozenset[str] = frozenset(
    {
        MODEL_MOISTURE_SIMPLE,
        MODEL_MOISTURE_FULL,
        MODEL_RAIN,
        MODEL_TEMPHUM,
        MODEL_FLOWMETER,
        MODEL_CO2,
        MODEL_POOL,
        MODEL_POOL_PLUS,
        MODEL_VALVE_HUB,
        MODEL_VALVE_113,
        MODEL_VALVE_145,
        MODEL_VALVE_213,
        MODEL_VALVE_245,
        MODEL_VALVE_345,
        MODEL_VALVE_405,
        MODEL_HCS005FRF,
        MODEL_HCS003FRF,
        MODEL_HCS024FRF_V1,
        MODEL_HCS015ARF,
        MODEL_HCS0528ARF,
        MODEL_HCS027ARF,
        MODEL_HCS016ARF,
        MODEL_HCS044FRF,
        MODEL_HCS666FRF,
        MODEL_HCS666RFR_P,
        MODEL_HCS999FRF,
        MODEL_HCS999FRF_P,
        MODEL_HCS666FRF_X,
        MODEL_HCS701B,
        MODEL_HCS596WB,
        MODEL_HCS596WB_V4,
        MODEL_HCS706ARF,
        MODEL_HCS802ARF,
        MODEL_HCS048B,
        MODEL_HCS888ARF_V1,
        MODEL_HCS0600ARF,
        MODEL_DISPLAY_HUB,
    }
)
