from pathlib import Path

# Display Hub model constant
DOMAIN = "rainpoint"

# Integration version
VERSION = "1.22.0"  # x-release-please-version

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

# === Hub identity ===
# Hub identity is spelled {hid}_{mid} everywhere: the hub device identifier is
# HUB_IDENTIFIER_PREFIX + "{hid}_{mid}", and every hub-level entity unique id is
# HUB_UNIQUE_ID_PREFIX + "{hid}_{mid}_{suffix}". device.py and hub_entities.py
# (the writers) and __init__.py's hub identity migration (the reader that
# recognizes and rewrites hub rows) both build from these constants rather than
# independently spelling "hub_" / "rainpoint_hub_", so the two sides cannot
# silently drift apart. The values themselves must never change: they are
# persisted in Home Assistant's device and entity registries, and changing
# either string is a breaking migration.
HUB_IDENTIFIER_PREFIX = "hub_"
HUB_UNIQUE_ID_PREFIX = f"{DOMAIN}_hub_"

# === Generic (catalog-driven) control entity factory ===
CONF_GENERIC_CONTROL_ENABLED = "generic_control_enabled"
# The sub-device keys that were already control-eligible when the user last
# saved the control toggle on. This is the consent baseline the new-controls
# notice measures against, and it lives in options because the entity registry
# cannot serve as one: __init__._generic_control_row_removal_reason deletes
# every control-namespace row for the entry whenever the toggle is off, so an
# off-and-on again would re-announce the whole fleet. Absent means "written by
# a version before this key existed", which falls back to the registry rather
# than announcing everything on upgrade.
CONF_GENERIC_CONTROL_ACKED_KEYS = "generic_control_acked_keys"
# The control marker is nested inside GENERIC_UNIQUE_ID_MARKER rather than
# given a top-level marker of its own. That keeps the existing __init__.py
# registry sweep guard (which matches on GENERIC_UNIQUE_ID_MARKER alone)
# already matching every control row, with no second substring guard needed.
# The trailing "ctl_" segment is what distinguishes a control row from a
# sensor row within the shared substring, which the two namespaces must stay
# distinguishable by -- every namespace test must assert on this FULL marker,
# never on GENERIC_UNIQUE_ID_MARKER alone, which a control unique_id also
# contains.
GENERIC_CONTROL_UNIQUE_ID_MARKER = f"{GENERIC_UNIQUE_ID_MARKER}ctl_"
# Trailing suffix for the duration companion entity, mirroring the trusted
# zone-duration convention ("_zone{n}_duration"): the
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
# Both the translation key and the stem of the per-device issue id for the
# notice raised when a sub-device gains generic control entities it has never
# had before (see repairs.new_generic_controls_issue_id). Entry-scoped like
# ORPHANED_ENTITIES_ISSUE_ID_PREFIX, because a sensor key is not unique across
# two config entries resolving the same home.
NEW_GENERIC_CONTROLS_ISSUE_ID_PREFIX = "new_generic_controls"
# Both the repair issue's translation key and the stem of its per-device
# issue id (see repairs.silent_device_issue_id). The issue id itself is the
# dedup key: it is built as f"{SILENT_DEVICE_ISSUE_ID_PREFIX}_{hid}_{mid}_{addr}",
# so a hub with several silent children raises one issue per child rather
# than colliding on a single id.
SILENT_DEVICE_ISSUE_ID_PREFIX = "device_not_reporting"
# Both the repair issue's translation key and the stem of its per-hub issue
# id (see repairs.hub_connectivity_issue_id). The issue id itself is the
# dedup key: it is built as f"{HUB_CONNECTIVITY_ISSUE_ID_PREFIX}_{hid}_{mid}",
# so a home holding several hubs raises one issue per hub rather than
# colliding on a single id.
HUB_CONNECTIVITY_ISSUE_ID_PREFIX = "hub_disconnected"
# Both the repair issue's translation key and the stem of its per-entry,
# per-key issue id (see repairs.orphaned_entities_issue_id). The issue id
# itself is the dedup key: it is built as
# f"{ORPHANED_ENTITIES_ISSUE_ID_PREFIX}_{entry_id}_{sensor_key}", and a sensor
# key is already {hid}_{mid}_{addr}. An account holding several vanished keys
# therefore raises one issue per key rather than colliding on a single id.
# The entry id is in there and is not in either sibling's id, because this is
# the only manager that withdraws its own cards: a sensor key is not unique
# across config entries resolving the same home, and an unscoped id would let
# one entry's unload delete a card another entry raised. The translation key
# is this bare prefix, so the two are independent and the id shape can change
# without touching translations/en.json.
ORPHANED_ENTITIES_ISSUE_ID_PREFIX = "orphaned_device_entities"
# How many consecutive qualifying observations a registry row has to make
# before its (domain, unique_id) pair may be offered on a card. Counted in
# coordinator updates rather than polls, and the distinction is real: this
# sweep runs from a coordinator listener, which also fires on every pushed
# frame, so an update is not the same event as a poll here. Held at the same
# number as the departed-key window so the two shapes of the same card do not
# ask a user to reason about two different waiting periods.
LEFTOVER_ROW_DEBOUNCE_UPDATES = 30
# The translation key the still-present shape of the leftover card renders
# from. Independent of the issue id, which keeps the
# ORPHANED_ENTITIES_ISSUE_ID_PREFIX shape for both: the two shapes are
# mutually exclusive for one sensor key, because the leftover derivation
# requires the key to be in the current poll while an aged-out key is by
# definition absent from it, so one id per key still holds and an active card
# never has to change its body underneath the user.
LEFTOVER_ENTITIES_TRANSLATION_KEY = "leftover_device_entities"
# Committed, variant-keyed force-disable list. Each member is a
# (model, modelCode-as-string) tuple, keyed exactly the way the product
# catalog itself is keyed: get_catalog_entry's UNCODED_VARIANT sentinel ("*")
# is the modelCode for a variant RainPoint supplied without one. Empty
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
# TLS port. The credential's mqttHostUrl advertises RainPoint's plaintext 1883,
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

# Hub-level connectivity frame shape, confirmed against the 2026-07-31
# UAT capture: "#P260731181730000016822282236547|0|1785521850011|112882164350#".
# Section 1 decomposes as the "#P" prefix, a 12-digit YYMMDDHHMMSS stamp,
# "0000", an 8-digit account id, and a 6-digit mid -- the mid is a fixed-width
# tail, not an open-ended suffix, so _frame_mid reads it by slicing a known
# slot rather than scanning for a substring.
MQTT_PUSH_HUB_FRAME_PREFIX = "#P"
MQTT_PUSH_HUB_FRAME_SECTIONS = 4
MQTT_PUSH_HUB_FRAME_TERMINATOR = "#"
MQTT_PUSH_HUB_FRAME_MID_WIDTH = 6
# 2 prefix + 12 stamp + 4 fixed + 8 account + 6 mid. A section 1 of any other
# length is a layout no capture has produced, so the mid slot cannot be read
# from it by position and the frame is declined rather than guessed at.
# Summed from its terms rather than written as 32, so widening the mid slot
# cannot leave the total silently wrong and the slice reading the wrong
# characters out of a section this width check then accepts.
MQTT_PUSH_FRAME_SECTION_ONE_WIDTH = len(MQTT_PUSH_HUB_FRAME_PREFIX) + 12 + 4 + 8 + MQTT_PUSH_HUB_FRAME_MID_WIDTH

# Hard cap on the per-client one-shot-per-shape unrecognised-downlink
# bookkeeping. Keeps the set bounded against a hostile or chatty
# downlink; a shape count past this logs at DEBUG instead of INFO.
MQTT_UNRECOGNISED_SHAPE_LOG_LIMIT = 32

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
# What the dead-after value bounds, stated plainly because assuming otherwise
# sends a reader here for the wrong problem: it bounds loss of the session to
# the cloud broker, not loss of a hub. The MQTT session is to the broker
# rather than to the hub, so a hub can be off the cloud entirely while the
# channel stays healthy, and this constant will never fire for it. A hub going
# offline surfaces through its own connectivity record and its own Repairs
# card, on a window measured in the coordinator; nothing in this block moves
# that latency, so do not reach for these values when a hub outage surfaces
# late.
#
# Known limitation: a channel that stays TCP-connected but silently stops
# delivering data (a subscription detached cloud-side) is NOT flagged, because
# from message-absence alone it is indistinguishable from a healthy but idle
# channel (no device activity legitimately means no pushes). Detecting that would
# need an active probe/heartbeat; it is out of scope for this cut.
#
# Both values now have a measured justification and were deliberately left
# alone when the hub-disconnect window was retuned, so this is a settled
# question rather than an open one. 900 is roughly 1.76 times the 510 second
# credential renewal interval, so the channel is flagged after about two
# consecutive renewal failures rather than after a single one that the next
# renewal would have repaired. The 180 second message grace is sized to bridge
# the roughly 300 millisecond bounce while the supervisor swaps sessions at a
# renewal boundary, with room to spare and none of it stacking onto the
# dead-after clock.
PUSH_WATCHDOG_SCAN_INTERVAL_SECONDS = 60
PUSH_WATCHDOG_DEAD_AFTER_SECONDS = 900
PUSH_WATCHDOG_MESSAGE_GRACE_SECONDS = 180
PUSH_WATCHDOG_ISSUE_ID = "push_channel_down"
PUSH_HUB_IDENTITY_ISSUE_ID = "push_hub_identity_unresolved"

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
MODEL_HTV210B = "HTV210B"  # Bluetooth valve; reports over RF as a normal hub sub-device once hub-paired
MODEL_HIC801W = "HIC801W"  # 8-station irrigation controller; catalog variant 279 is the accessory
# record carrying the stations, while 278 is the pairable main record with no ports.

# How many stations an HIC801W fans out to. Fixed from the model rather than
# derived from a frame, unlike the HTV valve factories which read their zones
# out of the decoded payload: variant 279 sends one aggregate record carrying a
# single running-station number and enumerates no stations of its own, so there
# is nothing in a frame to derive this from. Variant 279 declares portNumber 8.
#
# Named once here because three independent surfaces fan out over it -- the
# per-station watching binary sensors, the station valves, and their companion
# duration numbers -- and three literal 8s would be three places to miss when a
# sibling controller with a different station count arrives.
HIC801W_STATION_COUNT = 8

# Legacy valve aliases
MODEL_VALVE_113 = MODEL_HTV113FRF
MODEL_VALVE_145 = MODEL_HTV145FRF
MODEL_VALVE_213 = MODEL_HTV213FRF
MODEL_VALVE_245 = MODEL_HTV245FRF
MODEL_VALVE_345 = MODEL_HTV345FRF
MODEL_VALVE_405 = MODEL_HTV405FRF
MODEL_VALVE_445 = MODEL_HTV445FRF
MODEL_VALVE_HUB = MODEL_HTV0540FRF

# Membership here enrols a model in three things at once: valve.py and
# number.py build its entities, and coordinator.py's
# _preserve_recent_valve_command_state protects its command-versus-poll
# staleness guard. Which write endpoint a zone commands through is a separate
# question, decided by the catalog datapoint identity in api/trust.py, not by
# this set.
VALVE_MODELS = {
    MODEL_VALVE_HUB,
    MODEL_VALVE_113,
    MODEL_VALVE_145,
    MODEL_VALVE_213,
    MODEL_VALVE_245,
    MODEL_VALVE_345,
    MODEL_VALVE_405,
    MODEL_VALVE_445,
    MODEL_HTV210B,
    # MODEL_HIC801W is still deliberately absent, and station control shipping
    # is what settled that rather than what changed it. This set means
    # "zone-shaped valve model": every path it gates reads decoded["zones"],
    # and the HIC801W carries no zones dict at all -- one aggregate record with
    # a single running-station number instead. Enrolling it here would hand
    # valve.py and number.py a model whose builders find no zones and emit
    # nothing, and would put it through a staleness guard keyed on a mapping it
    # does not have. Its control entities are built by its own station-shaped
    # factories, dispatched on MODEL_HIC801W the way binary_sensor.py already
    # dispatches its per-station entities, and its staleness guard is a branch
    # of its own that preserves the whole aggregate record.
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
        MODEL_HTV210B,
        MODEL_HIC801W,
        MODEL_HCS005FRF,
        MODEL_HCS024FRF_V1,
        MODEL_HCS015ARF,
        MODEL_HCS0528ARF,
        MODEL_DISPLAY_HUB,
    }
)
