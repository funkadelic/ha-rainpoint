import logging
import re
from collections.abc import Callable, Iterable
from collections.abc import Set as AbstractSet
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlencode

import aiohttp
from homeassistant.components.persistent_notification import async_create
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import (
    DataUpdateCoordinator,
    UpdateFailed,
)

from .api import (
    RainPointApiError,
    RainPointClient,
    _summarize_record,
    decode_co2,
    decode_flowmeter,
    decode_generic,
    # New HCS decoder functions
    decode_hcs005frf,
    decode_hcs015arf,
    decode_hcs024frf_v1,
    decode_hcs0528arf,
    decode_hic801w,
    decode_htv145frf,
    decode_htv210b,
    decode_htv213frf_valve,
    decode_hws019wrf_v2,
    decode_moisture_full,
    decode_moisture_simple,
    decode_pool,
    decode_pool_plus,
    decode_rain,
    decode_temphum,
    decode_valve_hub,
    is_ascii_declined,
)
from .const import (
    CONF_HIDS,
    DEFAULT_SCAN_INTERVAL,
    ISSUE_URL,
    MODEL_CO2,
    MODEL_DISPLAY_HUB,
    MODEL_FLOWMETER,
    # New HCS sensor models
    MODEL_HCS005FRF,
    MODEL_HCS015ARF,
    MODEL_HCS024FRF_V1,
    MODEL_HCS0528ARF,
    MODEL_HIC801W,
    MODEL_HTV210B,  # HTV210B support
    MODEL_MOISTURE_FULL,
    MODEL_MOISTURE_SIMPLE,
    MODEL_POOL,
    MODEL_POOL_PLUS,
    MODEL_RAIN,
    MODEL_TEMPHUM,
    MODEL_VALVE_113,  # HTV113FRF support
    MODEL_VALVE_145,  # HTV145FRF support
    MODEL_VALVE_213,  # HTV213FRF support
    MODEL_VALVE_245,  # HTV245FRF support
    MODEL_VALVE_345,  # HTV345FRF support
    MODEL_VALVE_405,  # HTV405FRF support
    MODEL_VALVE_HUB,
    VALVE_MODELS,
    debug_with_version,
)
from .repairs import (
    HubConnectivityRecord,
    RainPointHubConnectivityIssues,
    RainPointSilentDeviceIssues,
    SilentDeviceRecord,
    _sanitize_placeholder,
    hub_connectivity_issue_id,
    silent_device_issue_id,
)

_LOGGER = logging.getLogger(__name__)

STALE_VALVE_POLL_GUARD = timedelta(minutes=5)


def _error_text(err: BaseException) -> str:
    """Return something readable for a transport error, whatever it carries.

    An aiohttp timeout raised with no message renders as an empty string, which
    is how a real log line ended up reading "falling back to individual calls: "
    with nothing after the colon. The class name is the least this can say and
    is always available.
    """
    return str(err) or type(err).__name__


class _AbsentStatus(dict):
    """Marker meaning "no status response arrived for this hub".

    Subclasses dict rather than using a bare sentinel object because
    _merge_push_sensor_entry and every other status reader do
    dict(status.get(mid, ...)) and similar dict operations; those must keep
    working unchanged. Callers distinguish it with isinstance(status, _AbsentStatus)
    rather than an equality check, since its contents are indistinguishable from
    a real "status arrived and reported nobody" dict.
    """


def _absent_status() -> _AbsentStatus:
    """Build a fresh absent-status marker.

    A factory rather than the module-level singleton this used to be. The
    singleton was stored into status_by_mid[mid] and so reached
    coordinator.data["status"] for every absent hub at once, all of them
    holding one object. Nothing mutated it, so nothing was broken, but a
    future consumer appending to status[mid]["subDeviceStatus"] would have
    polluted every absent hub simultaneously and the symptom would have read
    as cross-hub contamination rather than as a mutation bug.

    Note that a shallow copy would not have been enough to make sharing safe:
    dict(marker) copies the mapping but leaves "subDeviceStatus" pointing at
    the same list, so the inner list is the part that actually needed to stop
    being shared. Each call gets its own dict and its own list.

    Callers still detect it with isinstance(status, _AbsentStatus), never by
    identity, which is what lets this be a factory at all.
    """
    return _AbsentStatus({"subDeviceStatus": []})


# The coordinator data["type"] value for a sub-device the hub lists but no
# status response has ever mentioned. Deliberately distinct from "unknown":
# type == "unknown" is the admission ticket for the opt-in generic sensor and
# generic control paths, and a device the cloud reports nothing about must
# never become eligible for a generic valve entity.
SILENT_DATA_TYPE = "silent"

# Consecutive arrived-but-omitted polls required before a status-less addr is
# surfaced as silent (~6 minutes at the 120s DEFAULT_SCAN_INTERVAL). Absorbs a
# single-poll transient omission without hiding a genuinely silent device.
SILENT_DEBOUNCE_POLLS = 3

# Wall time a hub must have been reported disconnected before its Repairs
# card may be offered.
#
# Deliberately not derived from SILENT_DEBOUNCE_POLLS the way the alias below
# is, because this window is measured against the cloud's own change
# timestamp rather than against poll cadence. Two things follow from that and
# neither is available to a poll count: it survives a restart for free on any
# firmware that reports a change time, and it does not drift when the scan
# interval changes.
#
# Why 180 rather than the 360 a three-poll window produced at the default
# cadence. The cloud's own disconnect detection was measured at roughly six
# minutes (physical power cut 22:35 local, cloud edge 22:41:03, 2026-08-01),
# so the cloud edge is already the flap filter and anything added here is
# latency on an already-slow signal. Ten days of recorder history over this
# surface hold four connectivity edges and no flap at all, so the longer
# window was guarding against something never observed while hiding every
# outage that ended inside about six minutes end to end.
HUB_DISCONNECT_DEBOUNCE_SECONDS = 180

# Still an alias of SILENT_DEBOUNCE_POLLS, and now on its own terms rather
# than by inheritance: it absorbs a transient device-list shrink over the
# same roughly six-minute window at the default scan interval that the
# silence window covers, so retuning either should move both.
#
# Reads differently at its use site, though: it counts how many consecutive
# absences from the device list stay provisional, so the comparison there is
# "<=" (absences one through this value suppress, the next one releases),
# where ORPHANED_KEY_DEBOUNCE_POLLS's comparison is ">=" (a verdict fires
# once the count reaches the threshold).
#
# The hub-disconnect window used to be the third member of this family and
# has left it: it is wall time measured against the cloud's own change
# timestamp now, not a poll count, so there is nothing left here to alias it
# back to.
HUB_ABSENT_DEBOUNCE_POLLS = SILENT_DEBOUNCE_POLLS

# Consecutive polls a sensor key must stay absent from the hub's subDevices
# enumeration before its leftover entities can be offered for removal.
#
# Deliberately its own literal, and deliberately NOT aliased from or derived
# from SILENT_DEBOUNCE_POLLS or HUB_ABSENT_DEBOUNCE_POLLS the way those two
# are from each other. Those gate a Repairs card, which is cheap and
# reversible; this one gates offering the destruction of entities and their
# recorder history, so reusing a threshold tuned against the cheap cost model
# would import a cost model that does not apply here. A later retune of the
# shared debounce must not move this threshold with it, which a derived value
# or a multiple would do silently.
#
# The comparison at its use site is ">=", so a key ages out on its 30th
# consecutive absence (about an hour at the 120s DEFAULT_SCAN_INTERVAL). That
# is the verdict-fires reading rather than the stays-provisional reading
# HUB_ABSENT_DEBOUNCE_POLLS carries at its own use site above, and it is the
# single most likely thing a later reader will "correct" in the wrong
# direction.
ORPHANED_KEY_DEBOUNCE_POLLS = 30

# Hub-level cloud connectivity tri-state. Absent is never coerced to
# disconnected: HUB_CONNECTIVITY_UNKNOWN covers three distinct causes -- older
# firmware that omits the "connected" id, the Bluetooth wrapper record, and a
# status response that never arrived this poll -- all of which are equally
# "we do not know", not evidence that the hub dropped off the cloud.
HUB_CONNECTED = "connected"
HUB_DISCONNECTED = "disconnected"
HUB_CONNECTIVITY_UNKNOWN = "unknown"

# Decoder registry - maps device models to their decoder functions
DECODER_REGISTRY = {
    MODEL_MOISTURE_SIMPLE: decode_moisture_simple,
    MODEL_MOISTURE_FULL: decode_moisture_full,
    MODEL_RAIN: decode_rain,
    MODEL_TEMPHUM: decode_temphum,
    MODEL_FLOWMETER: decode_flowmeter,
    MODEL_CO2: decode_co2,
    MODEL_POOL: decode_pool,
    MODEL_POOL_PLUS: decode_pool_plus,
    MODEL_VALVE_HUB: decode_valve_hub,
    MODEL_VALVE_113: decode_htv145frf,  # HTV113FRF shares the HTV145FRF single-outlet 10# format
    MODEL_VALVE_145: decode_htv145frf,  # HTV145FRF single-outlet timer (10# compact format)
    MODEL_VALVE_213: decode_htv213frf_valve,  # HTV213FRF uses custom decoder
    MODEL_VALVE_245: decode_htv213frf_valve,  # HTV245FRF uses custom decoder
    MODEL_VALVE_345: decode_htv213frf_valve,  # HTV345FRF uses custom decoder
    MODEL_VALVE_405: decode_htv213frf_valve,  # HTV405FRF uses custom decoder
    MODEL_HTV210B: decode_htv210b,  # HTV210B structural record walk (hub-paired frames)
    MODEL_HIC801W: decode_hic801w,  # 8-station irrigation controller, read from the 279 accessory record
    # HCS sensor models (v1.3.0)
    MODEL_HCS005FRF: decode_hcs005frf,
    MODEL_HCS024FRF_V1: decode_hcs024frf_v1,
    MODEL_HCS015ARF: decode_hcs015arf,
    MODEL_HCS0528ARF: decode_hcs0528arf,
}


# Issue-form file that the pre-filled report link targets. GitHub issue forms
# accept query params keyed by each field's `id`, so the reporter lands on this
# form with the model and idle payload already populated.
NEW_DEVICE_ISSUE_TEMPLATE = "new_device.yml"

# Explicit marker for a silent device's report link: there is no payload to
# capture, and the absence of one is itself the finding, so the primary_payload
# field states that plainly instead of arriving blank.
NO_STATUS_PAYLOAD_MARKER = (
    "This device pairs to the hub but returns no status to the RainPoint cloud. There is no payload to capture."
)


def _format_generic_fields(generic: dict | None) -> str:
    """Render a best-effort generic decode as a text block for the issue form.

    Returns "" when the decode found no fields and the decoder did not
    decline an ASCII-framed body. A declined ASCII payload renders its
    reason as a leading line even when it found no fields, because a bug
    report that shows nothing is indistinguishable from a device that had
    nothing to say, and that ambiguity is what this rendering exists to
    retire. The gate is the ascii_framed marker, never the presence of an
    error key: a hex parse failure also carries error, and rendering that
    would change the shape of every hex bug report.
    """
    fields = (generic or {}).get("fields") or []
    lines = []
    if is_ascii_declined(generic):
        lines.append(f"Decoder: {generic.get('error', '')}")
    if not fields and not lines:
        return ""
    dp_prefixed = generic.get("dp_id_prefixed", False)
    for f in fields:
        suffix = f" (dp {f['dp_id']})" if dp_prefixed else ""
        catalog = f.get("catalog")
        zone_suffix = f" [zone {catalog['dp_port']}]" if catalog and catalog.get("dp_port") is not None else ""
        lines.append(f"{f['name']}: raw={f['raw']} value={f['value']}{suffix}{zone_suffix}")
    return "\n".join(lines)


def _format_gate_diagnostics(model: str | None, model_code: int | str | None) -> str:
    """Render what the catalog already explains about this model, for the issue form.

    Names the readings that have no verified definition yet and every reason
    the generic sensor factory produced nothing, so triage starts from what is
    already known rather than rediscovering it from the payload.

    Returns "" when there is nothing to say, so the caller can omit the
    pre-fill rather than seed the form with an empty section.
    """
    # Imported locally: generic_entities imports sensor, which imports this
    # module, so a top-level import here would close that cycle.
    from .generic_entities import describe_generic_gate

    described = describe_generic_gate(model, model_code)
    lines = []
    unmapped = described.get("unmapped_generic_identities") or []
    if unmapped:
        lines.append("Readings with no verified definition yet: " + ", ".join(unmapped))
    lines.extend(f"Blocked: {reason}" for reason in described.get("generic_gate_blocked_by") or [])
    return "\n".join(lines)


# GitHub answers a request line past roughly 8 KB with 414 URI Too Long, and
# a report link that silently fails is worse than one carrying less detail.
ISSUE_URL_MAX_LENGTH = 8000

_ISSUE_FIELD_TRUNCATION_NOTE = "\n... truncated to keep this link usable; the device's diagnostic entity carries the full text"

_ISSUE_PAYLOAD_TOO_LONG_NOTE = "too long for this link; please paste it from the device's Raw Payload sensor"


def _url_for_params(params: dict) -> str:
    """Render the issue URL for a parameter set."""
    return f"{ISSUE_URL}/new?{urlencode(params)}"


def _fit_param(params: dict, key: str, value: str) -> dict:
    """Return params with as much of value under key as the length budget allows.

    Percent-encoding expands the value by an amount that depends on its
    content, so the fit is measured against the rendered URL rather than
    estimated from the raw text. A value that cannot fit even partially is
    omitted rather than added empty, so the reporter sees a blank form field
    instead of a lone truncation marker.
    """
    if len(_url_for_params({**params, key: value})) <= ISSUE_URL_MAX_LENGTH:
        return {**params, key: value}
    low, high = 0, len(value)
    while low < high:
        mid = (low + high + 1) // 2
        if len(_url_for_params({**params, key: value[:mid] + _ISSUE_FIELD_TRUNCATION_NOTE})) <= ISSUE_URL_MAX_LENGTH:
            low = mid
        else:
            high = mid - 1
    if low == 0:
        return params
    return {**params, key: value[:low] + _ISSUE_FIELD_TRUNCATION_NOTE}


_FENCE_BREAKING_RE = re.compile(r"[`\r\n]+")


def _fence_safe(raw_value: Any) -> str:
    """Make a cloud payload safe to drop inside a fenced code block.

    _sanitize_placeholder is the wrong tool here and would destroy the thing
    being reported: it strips "#", which is the prefix separator in
    "10#E1BC00...", and "|", which separates fields in the ASCII framing some
    firmwares use. The payload is the one item in this notification that
    cannot be regenerated later, so it has to survive intact.

    The only real exposure inside a fence is a value that closes it, so this
    removes backticks and line breaks and nothing else. Everything a payload
    legitimately contains (hex, "#", commas, semicolons, pipes) passes
    through unchanged.
    """
    return _FENCE_BREAKING_RE.sub("", str(raw_value)) if raw_value is not None else ""


def _build_new_device_issue_url(
    model: str,
    raw_value: str | None,
    model_code: int | str | None = None,
    *,
    payload_note: str | None = None,
) -> str:
    """Return a GitHub New-device-support URL pre-filled with what the integration already knows.

    The reporter only has to add what the RainPoint app shows and submit, instead
    of hand-copying the model and hex payload into a blank issue. When the payload
    yields any named fields, an unverified auto-decode is pre-filled too, to give
    triage a head start.

    modelCode is carried whenever it is known because a model string can map to
    more than one modelCode and the variants can differ in port count, so a
    report naming only the model string can be ambiguous about which hardware it
    describes.

    The two growable fields are fitted to a total length budget, lowest value
    first. The raw payload is preferred over both: it is the one thing here
    that cannot be regenerated later. The auto-decode can be recomputed from
    that payload, and the catalog summary from the model and modelCode, so
    both are recoverable if they are cut.

    A payload large enough to blow the budget on its own is the one case where
    that preference is dropped, since a link too long to open carries nothing
    at all. The payload is left out and named in the form instead, so the
    reporter is told to paste it from the device's raw payload sensor rather
    than being handed a link GitHub refuses.

    payload_note replaces the raw payload with an explicit statement when there
    is no payload to begin with (a silent sub-device): decode_generic
    cannot read prose, so the auto-decode step is skipped entirely rather than
    run against text it was never meant to parse.
    """
    params = {
        "template": NEW_DEVICE_ISSUE_TEMPLATE,
        "title": f"Add support for {model}",
        "model": model,
        "primary_payload": payload_note if payload_note is not None else (raw_value or ""),
    }
    if model_code is not None:
        params["model_code"] = str(model_code)
    if len(_url_for_params(params)) > ISSUE_URL_MAX_LENGTH:
        return _url_for_params({**params, "primary_payload": _ISSUE_PAYLOAD_TOO_LONG_NOTE})
    auto_decoded = (
        _format_generic_fields(decode_generic(raw_value, model=model, model_code=model_code))
        if raw_value and payload_note is None
        else ""
    )
    if auto_decoded:
        params = _fit_param(params, "auto_decoded", auto_decoded)
    gate_diagnostics = _format_gate_diagnostics(model, model_code)
    if gate_diagnostics:
        params = _fit_param(params, "gate_diagnostics", gate_diagnostics)
    return _url_for_params(params)


def _resolve_addr_from_sid(sid: str) -> int | None:
    """Return integer addr from a 'D'-prefixed sid (e.g. 'D1' -> 1).

    Returns None if sid does not start with 'D' or the suffix is not a base-10 integer.
    """
    if not sid.startswith("D"):
        return None
    try:
        return int(sid[1:])
    except ValueError:
        return None


def _sensor_key(hid, mid: int, addr: int) -> str:
    """Return the canonical sensor key, the single definition every caller shares."""
    return f"{hid}_{mid}_{addr}"


def _sensor_keys_for_hub_keys(sensor_keys: Iterable[str], hub_keys: AbstractSet[tuple[Any, int]]) -> set[str]:
    """Return the subset of sensor_keys whose (hid, mid) half is in hub_keys.

    A hub absent from the device list carries no subDevices to walk, so its
    children can only be reached through the counters already being tracked
    for them, and a child with no counter has nothing to protect because it
    was never silent.

    Each key's hub half is derived with rsplit on the last underscore (the
    addr), then compared against the same f"{hid}_{mid}" text _sensor_key
    builds, so an underscore inside a hid cannot produce a wrong match and a
    neighbouring hid/mid pair cannot match by string prefix.
    """
    hub_prefixes = {f"{hid}_{mid}" for hid, mid in hub_keys}
    protected: set[str] = set()
    for key in sensor_keys:
        hub_half, _sep, _addr = key.rpartition("_")
        if hub_half in hub_prefixes:
            protected.add(key)
    return protected


def is_hub_record(hub: dict) -> bool:
    """Return True when a top-level device record is a real hub.

    getDeviceByHid returns one top-level record per parent device in a home, and
    not all of them are hubs. Pairing a Bluetooth valve makes the cloud add a
    second record that exists only to carry that valve in its subDevices: every
    identity field on it (did, mac, productKey, model, name) is an empty string
    rather than a missing key, so `.get(key, default)` hands back "" and the
    default never fires.

    Such a record must not produce hub-level entities. It is still kept in the
    "hubs" list so its subDevices stay discoverable; this only gates the things
    that describe a hub as a device.
    """
    return bool(hub.get("did") or hub.get("mac") or hub.get("productKey") or hub.get("model"))


def _is_usable_sub_device(sub: object) -> bool:
    """Return True when a subDevices entry carries enough shape to index.

    The list is cloud-supplied and nothing guarantees its shape. An entry
    with no addr carries no identity to key on, so it is dropped rather than
    trusted.

    The addr value itself is checked only for the two properties every
    consumer needs, and no further: it must be hashable, because it is used
    directly as a dict key and an unhashable one would raise TypeError out
    of the same walk this function exists to keep alive, and it must not be
    None, because no sid resolves to None and such a record would otherwise
    be enumerated under a key that can never report, earning a
    not-reporting card for a device that does not exist.

    Any other hashable value is tolerated rather than supported, and the
    difference matters. It builds a usable sensor key, because _sensor_key
    composes an f-string and does not care about the type, but the status
    join in _decode_hub_subdevices looks a reading up in a dict keyed by
    _resolve_addr_from_sid's return value, which is always an int. A string
    addr would therefore miss its own reading and read as silent while that
    reading sat in the same response. That mismatch predates this guard and
    is deliberately left alone: narrowing to int here would reject data on a
    case nobody has observed.
    """
    if not isinstance(sub, dict):
        return False
    addr = sub.get("addr")
    if addr is None:
        return False
    try:
        hash(addr)
    except TypeError:
        return False
    return True


def _is_usable_status_entry(entry: object) -> bool:
    """Return True when a subDeviceStatus entry carries a string id.

    The list is cloud-supplied and nothing guarantees its shape. A non-dict
    entry is dropped for that reason; a non-string id is dropped too, because
    _resolve_addr_from_sid calls .startswith on it and would otherwise raise
    out of the same walk.
    """
    return isinstance(entry, dict) and isinstance(entry.get("id"), str)


def _sub_devices_by_addr(hub: dict) -> dict[Any, dict]:
    """Return this hub's subDevices indexed by addr, dropping unusable entries.

    The single definition every walk over a hub's subDevices shares, poll path
    and push path alike, so those walks cannot disagree about which records
    exist: a record the decode walk skips is skipped identically by the
    orphan-key walk, the silent-state prune, the absent-hub issue walk and the
    push path's own lookup. The key type is deliberately not narrowed to int:
    _is_usable_sub_device enforces only that addr is present, since any
    hashable already works end to end today through _sensor_key's f-string,
    and narrowing here would reject data that currently works. Built from
    `hub.get("subDevices") or []` rather
    than a `.get` default so a subDevices key present with a None value is
    tolerated too, matching _find_hub_status_entries' handling of
    subDeviceStatus.
    """
    return {sub["addr"]: sub for sub in hub.get("subDevices") or [] if _is_usable_sub_device(sub)}


def _find_hub_status_entries(status: dict) -> tuple[dict | None, dict | None]:
    """Return the hub-scoped (connected, state) entries from one status response.

    Both ids are read in a single pass over subDeviceStatus -- the same list
    RainPointHubRSSISensor already scans for `state`. They are hub-scoped
    rather than per-addr, so no addr-keyed dict is built for them the way
    _resolve_addr_from_sid builds one for D-prefixed sub-device ids.

    A non-dict entry is skipped rather than trusted: the list is cloud-supplied
    and nothing guarantees its shape.
    """
    connected_entry: dict | None = None
    state_entry: dict | None = None
    for entry in status.get("subDeviceStatus") or []:
        if not isinstance(entry, dict):
            continue
        entry_id = entry.get("id")
        if entry_id == "connected":
            connected_entry = entry
        elif entry_id == "state":
            state_entry = entry
    return connected_entry, state_entry


def _connected_tri_state(connected_entry: dict | None) -> str:
    """Map the `connected` entry to HUB_CONNECTED / HUB_DISCONNECTED / unknown.

    Only the literal string values "1" and "0" map to a definite state. Every
    other value, including a non-string and a missing entry, maps to unknown,
    because a cloud that returns garbage should degrade to "we do not know"
    rather than a false disconnected.
    """
    value = connected_entry.get("value") if connected_entry else None
    if value == "1":
        return HUB_CONNECTED
    if value == "0":
        return HUB_DISCONNECTED
    return HUB_CONNECTIVITY_UNKNOWN


def _connected_changed_at(connected_entry: dict | None) -> str | None:
    """Return when the cloud last flipped `connected`, as an ISO-8601 UTC string.

    None when the entry is absent or carries no usable time, so the attribute
    reads as unknown rather than inventing a moment.
    """
    if connected_entry is None:
        return None
    changed_dt = _status_entry_time(connected_entry)
    return changed_dt.isoformat() if changed_dt is not None else None


def _read_hub_connectivity(status: dict) -> dict:
    """Return the hub-level cloud connectivity record for one hub's status.

    Returns exactly three keys: "state" (one of HUB_CONNECTED,
    HUB_DISCONNECTED, HUB_CONNECTIVITY_UNKNOWN), "changed_at" (an ISO-8601 UTC
    string naming when the cloud last flipped `connected`, or None), and
    "state_raw" (the raw `state` id's value, undecoded, or None).

    An absent status (this hub's status could not be obtained this poll)
    yields the unknown record immediately with both other fields None --
    absent must never be coerced to disconnected. The per-field rules live in
    the three helpers above; this function is the assembly point and the one
    place the record's shape is written down.
    """
    if isinstance(status, _AbsentStatus):
        return {"state": HUB_CONNECTIVITY_UNKNOWN, "changed_at": None, "state_raw": None}

    connected_entry, state_entry = _find_hub_status_entries(status)

    # Carried undecoded on purpose: the first field read '0' in every observed
    # condition on both sides of a real power cycle, so assigning it a meaning
    # would be a guess shipped as fact. A non-string degrades to None.
    state_raw = state_entry.get("value") if state_entry else None
    if not isinstance(state_raw, str):
        state_raw = None

    return {
        "state": _connected_tri_state(connected_entry),
        "changed_at": _connected_changed_at(connected_entry),
        "state_raw": state_raw,
    }


def _guard_hub_connectivity_order(polled: dict, prior: dict | None) -> dict:
    """Hold a strictly older poll's connectivity half against a newer held one.

    Applied at the single _async_update_data call site both fetch paths
    funnel through, immediately after _read_hub_connectivity. Kept as a
    separate merge step rather than a parameter threaded into
    _read_hub_connectivity, so that function stays a pure function of one
    status dict and its 13 existing tests need no edit.

    First, why the guard exists. If the REST view lags the push, the next
    poll can carry the old connectivity flag and revert a newer pushed edge,
    producing a visible flip-back that reverts again two minutes later. That
    is the same stale-overwrites-fresh defect class the push-side ordering
    guard closes, entering through the poll side instead of the push side.

    Second, why only strictly older is held. An equal moment is the same
    edge already held, so returning polled changes nothing observable
    either way. An unordered pair, meaning either side carries no usable
    time, is not evidence of staleness, so the poll wins in both the equal
    and the unordered case. That is what keeps the poll authoritative: the
    guard only ever holds a moment it can prove is older.

    Third, state_raw always takes the latest polled value regardless
    of whether the guard fired. The guard exists to stop a stale
    connectivity flag winning, and state_raw is an unrelated diagnostic the
    push never carries, so discarding it would blank the raw state
    attribute for a full cycle every time the guard fires. This pairs with
    apply_hub_push_update from the other direction: neither path destroys
    the field the other owns.

    Fourth, the absent case. An absent status yields the unknown record
    from _read_hub_connectivity and this guard takes it whole, because
    absent carries no moment of its own and so is never strictly older than
    anything held. The rule that an absent status is unknown rather than
    disconnected is not something this guard may quietly change.

    Fifth, and this is the point a reader is most likely to be surprised
    by, so it is stated plainly rather than left to be discovered: the
    guarded record returned here is what _sync_hub_connectivity_issues
    reconciles against, so the guard composes with the poll-side debounce
    in both directions. When a held pushed connected state is kept against
    a lagging disconnected poll, the reconcile sees connected and drops the
    window start. When a held pushed disconnected state is kept against a
    lagging connected poll, the reconcile sees disconnected and opens a
    window, and because the guard writes the held changed_at into the record
    it returns, that held moment is also the window start and the hold
    repeats on every following poll until the REST view's own connected time
    advances past it. A single such poll therefore raises a card that no
    poll independently observed as disconnected, as soon as the held moment
    is itself older than HUB_DISCONNECT_DEBOUNCE_SECONDS -- there is no
    count left to run down first. That is the intended consequence of
    treating a newer held value as fresher than a lagging poll, and it is
    not the push path opening a window: apply_hub_push_update never touches
    _hub_disconnect_since on a disconnected edge, the poll reconcile
    measures the guarded record it was handed. A later reader should not
    read this as a defect in the push path and "fix" it by exempting held
    records from the reconcile.
    """
    prior_moment = _changed_at_datetime(prior)
    polled_moment = _changed_at_datetime(polled)
    if prior_moment is not None and polled_moment is not None and polled_moment < prior_moment:
        return {
            "state": prior["state"],
            "changed_at": prior["changed_at"],
            "state_raw": polled.get("state_raw"),
        }
    return polled


def hub_connectivity_record(coordinator, mid) -> dict:
    """Return one hub's connectivity record from a coordinator snapshot, or {}.

    The lookup lives here alone so the three surfaces that read it -- the hub
    connectivity binary sensor, the sub-device attributes, and valve
    availability -- cannot drift apart in how they tolerate a partial
    snapshot. Every step degrades to {} rather than raising: no data at all,
    no "hub_connectivity" key (what every pre-existing test fake in this
    suite supplies), an explicit None stored under that key, and no record
    for this mid. {} is what hub_connected_flag maps to "we do not know",
    so an absent record never reads as disconnected.
    """
    connectivity = (coordinator.data or {}).get("hub_connectivity") or {}
    return connectivity.get(mid) or {}


def hub_connected_flag(record: dict | None) -> bool | None:
    """Map a hub_connectivity record to True / False / None.

    This is the single mapping site shared by the hub connectivity binary
    sensor and the sub-device attribute helper (sub_device_attributes), so
    the two surfaces cannot disagree about what "disconnected" means. A
    falsy record (None, or a record with no recognized state) maps to None,
    the same "we do not know" answer an unknown tri-state produces.
    """
    if not record:
        return None
    state = record.get("state")
    if state == HUB_CONNECTED:
        return True
    if state == HUB_DISCONNECTED:
        return False
    return None


def first_hub_record(hubs: list[dict]) -> dict | None:
    """Return the first real hub in API order, or None when there is none.

    Callers that need "the" hub used to take hubs[0] unconditionally. That holds
    only while the cloud happens to order a real hub first; a Bluetooth wrapper
    record in slot 0 has an empty deviceName and productKey, which silently
    yields a hub that cannot be identified.
    """
    return next((hub for hub in hubs if is_hub_record(hub)), None)


def _decode_subdevice_payload(model: str | None, raw_value: str, model_code: int | str | None = None) -> dict:
    """Dispatch raw_value through DECODER_REGISTRY or the MODEL_DISPLAY_HUB special case.

    Returns the decoded dict, or an {"type": "unknown", ...} shape if no decoder is
    registered. Raises whatever the underlying decoder raises - callers handle the
    try/except and any side-effects (logging, persistent notifications).
    """
    if model == MODEL_DISPLAY_HUB:
        return decode_hws019wrf_v2(raw_value)
    decoder_func = DECODER_REGISTRY.get(model)
    if decoder_func:
        return decoder_func(raw_value)
    # No per-model decoder: fall back to a model-agnostic structural decode so
    # the diagnostic sensor and bug report show named fields instead of raw hex.
    # This is best-effort and unverified - it never feeds entities or control.
    return {
        "type": "unknown",
        "model": model,
        "raw_value": raw_value,
        "generic": decode_generic(raw_value, model=model, model_code=model_code),
    }


def _attach_device_timestamp(decoded: dict | None, status_entry: dict) -> None:
    """Mutate decoded in place to add device_timestamp / timestamp_source.

    No-op when decoded is falsy or status_entry["time"] is missing (None). A
    "time" of 0 is treated as a valid epoch-ms (1970-01-01). Silently swallows
    ValueError, TypeError, OSError, and OverflowError raised while parsing.
    """
    device_time = status_entry.get("time")
    if device_time is None:
        return
    try:
        dt = datetime.fromtimestamp(device_time / 1000, tz=UTC)
        if decoded:
            decoded["device_timestamp"] = dt.isoformat()
            decoded["timestamp_source"] = "device"
    except (ValueError, TypeError, OSError, OverflowError):
        pass


def _status_entry_time(status_entry: dict) -> datetime | None:
    """Return status_entry["time"] as a UTC datetime, or None when unavailable."""
    device_time = status_entry.get("time")
    if device_time is None:
        return None
    try:
        return datetime.fromtimestamp(device_time / 1000, tz=UTC)
    except (ValueError, TypeError, OSError, OverflowError):
        return None


def _changed_at_datetime(record: dict | None) -> datetime | None:
    """Return a hub_connectivity record's changed_at as a UTC datetime, or None.

    None for a falsy record, a changed_at that is not a string, or a string
    that fails to parse as ISO-8601. Consumed by the push-side ordering guard
    and the poll-side one.

    Every writer of changed_at emits an offset today, so a parsed value is
    aware in practice. A naive one is assumed to be UTC rather than returned
    as-is: both ordering sites compare the result against an aware datetime,
    which would raise TypeError on the event loop out of a helper whose
    documented contract is to degrade rather than raise.
    """
    if not record:
        return None
    changed_at = record.get("changed_at")
    if not isinstance(changed_at, str):
        return None
    try:
        parsed = datetime.fromisoformat(changed_at)
    except (ValueError, TypeError):
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _last_seen_from_entry(previous: dict | None) -> str | None:
    """Return an ISO-8601 last-seen timestamp carried by a previous sensor entry, or None.

    Resolution order: a previous silent entry carries its own last_seen forward
    unchanged (it never had a real reading to time itself by); otherwise a real
    entry's device_timestamp is used when present; otherwise the raw_status
    arrival time is used as a last resort. Returns None only when the device
    has never reported anything at all.
    """
    if not previous:
        return None
    data = previous.get("data") or {}
    if data.get("type") == SILENT_DATA_TYPE:
        return data.get("last_seen")
    device_timestamp = data.get("device_timestamp")
    if device_timestamp:
        return device_timestamp
    entry_time = _status_entry_time(previous.get("raw_status") or {})
    return entry_time.isoformat() if entry_time else None


def _valve_zone_poll_is_stale(poll_time: datetime | None, last_command_time: datetime, now: datetime) -> bool:
    """Return True when a poll should be treated as older than the last command.

    Prefers the device-reported poll timestamp; when it is unavailable, falls
    back to a wall-clock guard window so a fresh command response isn't clobbered.
    """
    if poll_time is not None:
        return poll_time < last_command_time
    return now - last_command_time < STALE_VALVE_POLL_GUARD


def _build_sensor_entry(
    hub: dict,
    sub: dict,
    mid: int,
    addr: int,
    status_entry: dict,
    decoded: dict | None,
) -> dict:
    """Build the per-sensor metadata dict that goes into the coordinator's sensors output.

    hub_paired is a derived verdict, not a payload passthrough: it is
    is_hub_record(hub)'s answer to "does a real hub carry this sub-device",
    cached once here at construction time so no consumer re-derives it from
    the raw hub fields on its own.
    """
    return {
        "hid": hub["hid"],
        "mid": mid,
        "addr": addr,
        "home_name": hub.get("homeName"),
        "hub_name": hub.get("name", "Hub"),
        "sub_name": sub.get("name"),
        "model": sub.get("model"),
        "model_code": sub.get("modelCode"),
        "firmware_version": sub.get("softVer"),
        "device_name": hub.get("deviceName"),
        "product_key": hub.get("productKey"),
        "hub_paired": is_hub_record(hub),
        "raw_status": status_entry,
        "data": decoded,
    }


def _utcnow() -> datetime:
    """Return the current moment as an aware UTC datetime.

    The coordinator's default clock, replaceable through its constructor so
    a test can drive the durational hub-disconnect window across a real
    construct-then-refresh timeline without sleeping. Injection rather than a
    module global patched in place, matching the two other places this
    project already injects a clock: a timeline test that forgot the patch
    would measure real wall time, never cross the threshold, and pass while
    proving nothing.
    """
    return datetime.now(UTC)


class RainPointCoordinator(DataUpdateCoordinator):
    """Coordinator for RainPoint polling."""

    def __init__(
        self,
        hass: HomeAssistant,
        client: RainPointClient,
        entry,
        *,
        time_source: Callable[[], datetime] = _utcnow,
    ):
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name="RainPoint coordinator",
            update_interval=timedelta(seconds=DEFAULT_SCAN_INTERVAL),
        )
        self._client = client
        self._entry = entry
        self._hids = entry.data.get(CONF_HIDS, [])
        self._time_source = time_source
        self._notified_unknown_models: set[tuple[str | None, int | None]] = set()
        self._last_valve_command_at: dict[tuple[str, int], datetime] = {}
        self._silent_poll_counts: dict[str, int] = {}
        self._silent_issues = RainPointSilentDeviceIssues(hass)
        # When each still-disconnected hub's current disconnect window
        # started: the cloud's own change moment where the hub reports one,
        # otherwise the first poll that observed the disconnected tri-state.
        # One aware UTC datetime per (hid, mid), written only by the poll's
        # disconnected branch, popped by its connected branch and by a pushed
        # connected edge, and pruned against the live and provisionally
        # missing hub keys.
        self._hub_disconnect_since: dict[tuple[Any, int], datetime] = {}
        self._hub_connectivity_issues = RainPointHubConnectivityIssues(hass)
        # Cross-poll hub-enumeration memory: the (hid, mid) of every real hub
        # the last trusted poll listed, plus any hub still within its
        # provisional absence window, and how many consecutive polls each
        # missing hub has been absent for. This is a different concept from
        # _silent_poll_counts (per-child silence): it decides whether a poll's
        # device list can be trusted at all, not whether a given addr is still
        # reporting. Deliberately not the structure an orphaned-entity sweep
        # would need, which resolves entity-registry rows against
        # coordinator.data["sensors"]. Living outside self.data accepts less
        # instance-attribute-versus-self.data skew than
        # _hub_disconnect_since does, because only the poll (never a push)
        # ever writes this state.
        self._last_poll_hub_keys: set[tuple[Any, int]] = set()
        self._hub_absent_poll_counts: dict[tuple[Any, int], int] = {}
        # Cross-poll sub-device-enumeration memory: every sensor key the last
        # trusted poll's subDevices enumeration listed, plus every key still
        # being counted, and how many consecutive polls each vanished key has
        # been absent for. This is the sensor-key analogue of the
        # _last_poll_hub_keys plus _hub_absent_poll_counts pair above -- one
        # structure in two attributes -- and mirrors that shape on purpose
        # rather than inventing a third one.
        #
        # Deliberately not merged with _silent_poll_counts. That one counts
        # per-child silence observed in status responses; this one counts
        # per-key absence from the device list enumeration, so a device that
        # is merely quiet for a poll or two never starts a removal count.
        #
        # Living outside self.data carries the same instance-attribute-versus-
        # self.data skew the hub enumeration memory above does, and it is just
        # as weak for the same reason: only the poll ever writes this state,
        # and no push path reads or writes it.
        self._last_poll_sensor_keys: set[str] = set()
        self._orphaned_key_poll_counts: dict[str, int] = {}
        self._aged_out_sensor_keys: frozenset[str] = frozenset()
        # The enumeration the orphan counter counts against, published for the
        # one consumer that has to agree with it: the removal confirm's
        # staleness guard. None until a poll has carried a device list, which is
        # a different answer from "the enumeration is empty" and is why this is
        # not seeded to frozenset(). See enumerated_sensor_keys.
        self._last_enumerated_sensor_keys: frozenset[str] | None = None
        # The hub keys whose empty enumeration has already been warned about,
        # so that warning fires on the edge rather than on every poll. Sized by
        # the number of hubs in the account, and rebuilt whole on every poll
        # that reaches the counting step.
        self._warned_empty_enumeration: set[str] = set()
        # The hub keys already warned about an unusable cloud record this
        # session, so that warning fires once per degradation edge rather
        # than once per poll. Sized by the number of hubs in the account,
        # and a hub is dropped from it on its first clean poll so a later
        # degradation gets its own line.
        self._warned_malformed_records: set[str] = set()

    def record_valve_command(self, sensor_key: str, zone_num: int) -> datetime:
        """Remember the latest successful valve command time for stale-poll protection."""
        command_dt = datetime.now(UTC)
        self._last_valve_command_at[(sensor_key, zone_num)] = command_dt
        return command_dt

    def apply_push_update(self, mid: int, sid: str, raw_value: str, device_ts: int | None) -> None:
        """Merge a single pushed sub-device reading into coordinator data.

        The one sanctioned entry point for the push channel. Resolves the hub by
        mid and the sub-device by sid, runs the reading through the SAME decode
        and valve-staleness path the 120s poll uses, then merges the result
        copy-on-write and notifies listeners. Any miss (unknown mid, unresolvable
        addr, or a sub-device the hub does not report) is logged at DEBUG and
        dropped without mutating coordinator data, mirroring the poll path's
        continue-on-miss. Never touches the poll timer, so polling keeps running
        as the fallback.

        Every drop log carries the raw value. A device paired between two polls
        pushes against a sub-device map that does not list it yet, so its first
        frames are dropped; those are exactly the frames worth having when the
        model has no decoder, and without the value in the log they were gone.
        """
        data = self.data
        if not data:
            _LOGGER.debug("Dropping push before first poll: mid=%s sid=%s value=%s", mid, sid, raw_value)
            return

        hub = next((h for h in data.get("hubs", []) if h.get("mid") == mid), None)
        if hub is None:
            _LOGGER.debug("Dropping push for unknown mid=%s (sid=%s) value=%s", mid, sid, raw_value)
            return

        addr = _resolve_addr_from_sid(sid)
        if addr is None:
            _LOGGER.debug("Dropping push with unresolvable sid=%s for mid=%s value=%s", sid, mid, raw_value)
            return

        # The same shared shape helper the poll path uses, and for a reason
        # this method did not previously have: before the poll learned to
        # skip an unusable record, one could never reach self.data at all
        # because the poll raised first, so every consumer of self.data
        # inherited that guarantee for free. The poll is tolerant now, so an
        # unusable record does reach here, and indexing it raw would move the
        # crash from the poll onto paho's callback thread.
        sub = _sub_devices_by_addr(hub).get(addr)
        if sub is None:
            _LOGGER.debug("Dropping push for unknown addr=%s (mid=%s sid=%s) value=%s", addr, mid, sid, raw_value)
            return

        status_entry = {"id": sid, "value": raw_value, "time": device_ts}
        sensor_key, sensor_entry = RainPointCoordinator._decode_one_subdevice(self, hub, mid, addr, sub, status_entry)
        RainPointCoordinator._merge_push_sensor_entry(self, mid, sid, sensor_key, sensor_entry, status_entry)
        # _merge_push_sensor_entry's contract is a copy-on-write merge of
        # coordinator data; the debounce counter and the not-reporting repair
        # issue are separate state it does not touch, so clearing both on push
        # arrival has to be explicit here rather than folded into the merge.
        self._silent_poll_counts.pop(sensor_key, None)
        self._silent_issues.async_clear(hub["hid"], mid, addr)
        # Notify listeners WITHOUT async_set_updated_data so the poll interval
        # timer is never reset; the 120s poll keeps running as the fallback.
        self.async_update_listeners()

    def apply_hub_push_update(self, mid: int, connected: bool, changed_ts: int) -> None:
        """Merge a pushed hub-level connectivity edge into coordinator data.

        The second sanctioned push entry point, alongside
        apply_push_update: a hub frame carries no addr, no decoder, and
        touches neither the sensors nor the status branch, so it shares none
        of that method's merge logic even though it mirrors its drop ladder
        and copy-on-write/notify shape.

        Mirrors apply_push_update's before-first-poll and unknown-mid drops,
        and adds one drop with no equivalent there: a resolved hub that fails
        is_hub_record, because the Bluetooth wrapper record contributes no
        connectivity record on the poll path, and writing one here would
        create a record the next poll deletes.

        The ordering guard below decides whether the pushed
        edge is applied at all. On a connected edge that is applied, this
        method also explicitly pops `_hub_disconnect_since` and calls
        `_hub_connectivity_issues.async_clear`: the merge is
        copy-on-write over coordinator data and touches neither, so clearing
        has to be explicit here, exactly as apply_push_update already does
        for the silent-device pair. A pushed disconnected edge leaves both
        untouched: the window start is written by the poll's own reconcile
        alone, so the card is still only ever raised from a disconnected
        tri-state a poll observed.
        """
        data = self.data
        if not data:
            _LOGGER.debug(
                "Dropping hub push before first poll: mid=%s connected=%s changed_ts=%s",
                mid,
                connected,
                changed_ts,
            )
            return

        hub = next((h for h in data.get("hubs", []) if h.get("mid") == mid), None)
        if hub is None:
            _LOGGER.debug("Dropping hub push for unknown mid=%s connected=%s changed_ts=%s", mid, connected, changed_ts)
            return

        if not is_hub_record(hub):
            _LOGGER.debug("Dropping hub push for non-hub record mid=%s connected=%s changed_ts=%s", mid, connected, changed_ts)
            return

        changed_dt = _status_entry_time({"time": changed_ts})
        if changed_dt is None:
            # Deliberate divergence from _read_hub_connectivity: a poll is a
            # complete observation, so an unconvertible timestamp there
            # honestly degrades to unknown. A push is an increment on top of
            # a poll, so the honest answer here is to decline the increment
            # rather than knock a good held value down to unknown.
            _LOGGER.debug(
                "Dropping hub push with unconvertible changed_ts=%s for mid=%s connected=%s",
                changed_ts,
                mid,
                connected,
            )
            return

        held = ((data.get("hub_connectivity") or {}).get(mid)) or {}

        # Ordering guard. The change timestamp is the
        # ordering key, compared against the held record's own changed_at:
        # the poll path already stores that field from the same cloud field,
        # so both channels order against one identical value rather than two
        # parallel clocks.
        held_moment = _changed_at_datetime(held)
        if held_moment is not None:
            if changed_dt < held_moment:
                _LOGGER.debug(
                    "Dropping older hub push: mid=%s pushed=%s held=%s",
                    mid,
                    changed_dt.isoformat(),
                    held_moment.isoformat(),
                )
                return
            if changed_dt == held_moment:
                # The common case: the poll routinely picks up the very edge
                # the push already delivered. Applying it would rewrite an
                # identical record and fire listeners for nothing.
                _LOGGER.debug("Dropping already-recorded hub push: mid=%s moment=%s", mid, changed_dt.isoformat())
                return
        # A held moment of None, an absent held record, and an unparseable
        # held changed_at all fall through to apply: there is no
        # recorded moment for the pushed edge to be older than, and refusing
        # would make push a permanent no-op on any firmware whose connected
        # entry carries no usable time, silently disabling the feature with
        # no way for the user to tell.

        record = {
            "state": HUB_CONNECTED if connected else HUB_DISCONNECTED,
            "changed_at": changed_dt.isoformat(),
            # Carried forward from the held record, untouched: the
            # frame does not carry the raw `state` id, so a pushed edge must
            # neither invent this field nor blank a poll-established
            # diagnostic for up to 120s.
            "state_raw": held.get("state_raw"),
        }
        RainPointCoordinator._merge_push_hub_connectivity(self, mid, record)

        if connected:
            # Connected-edge clear. The merge above is
            # copy-on-write over coordinator data and touches no debounce or
            # issue state, so clearing has to be explicit here rather than
            # folded into the merge, exactly as apply_push_update already
            # does for _silent_poll_counts / _silent_issues.async_clear.
            # _sync_hub_connectivity_issues is deliberately not driven from
            # here: it is built around a full per-poll sweep over hubs that
            # produces a record for every hub, so a single-hub push edge
            # would mean either faking a poll or splitting the function, and
            # it would re-run the unknown/unreachable logic for hubs the
            # push said nothing about.
            #
            # The asymmetry is intentional. Clearing early for a hub the
            # cloud already says is back is safe and self-correcting -- the
            # next poll re-raises if it was wrong -- while raising early is
            # not, which is the flap-raises-a-card case the poll-side
            # debounce already rejects. So a pushed disconnected edge leaves
            # both the window start and the issue untouched: the window is
            # opened by the poll's own reconcile alone, so a card is still
            # only ever raised from a disconnected tri-state a poll observed,
            # and the coordinator holds one debounce concept rather than two.
            self._hub_disconnect_since.pop((hub["hid"], mid), None)
            self._hub_connectivity_issues.async_clear(hub["hid"], mid)

        # Notify listeners WITHOUT async_set_updated_data so the poll interval
        # timer is never reset; the 120s poll keeps running as the fallback.
        self.async_update_listeners()

    def _merge_push_hub_connectivity(self, mid: int, record: dict) -> None:
        """Copy-on-write merge of one pushed hub connectivity record into self.data.

        Replaces only hub_connectivity[mid], shallow-copying the top-level
        dict and the hub_connectivity sub-dict along the way so every sibling
        mid keeps its object identity and hubs/sensors/status are carried by
        reference. Assigns the rebuilt top-level dict back to self.data.
        """
        data = dict(self.data)
        hub_connectivity = dict(data.get("hub_connectivity", {}))
        hub_connectivity[mid] = record
        data["hub_connectivity"] = hub_connectivity
        self.data = data

    def _merge_push_sensor_entry(
        self,
        mid: int,
        sid: str,
        sensor_key: str,
        sensor_entry: dict,
        status_entry: dict,
    ) -> None:
        """Copy-on-write merge of one pushed reading into self.data.

        Replaces only the touched sensors[sensor_key] and status[mid] branches,
        shallow-copying the containers along the way so every other sensors key
        and status mid keeps its object identity (and hubs is carried by
        reference). Assigns the rebuilt top-level dict back to self.data.
        """
        data = dict(self.data)

        sensors = dict(data.get("sensors", {}))
        sensors[sensor_key] = sensor_entry
        data["sensors"] = sensors

        status = dict(data.get("status", {}))
        # Merely "no prior status recorded for this mid to merge into yet" --
        # unrelated to the absent-vs-omitted distinction, which concerns
        # the fetch layer's status_by_mid, not this push-side merge target.
        # A bare literal rather than the absent marker, deliberately: this site
        # makes no absence claim, and the surrounding dict() would strip the
        # marker's type anyway, so building one here would allocate on every
        # push merge to say something this site does not mean.
        mid_status = dict(status.get(mid, {"subDeviceStatus": []}))
        sub_status = list(mid_status.get("subDeviceStatus", []))
        for index, existing in enumerate(sub_status):
            # Skipped rather than indexed, for the same reason apply_push_update
            # now uses the shared helper: this list is the cloud's own
            # subDeviceStatus as the poll stored it, and the poll no longer
            # raises on an unusable entry, so one can be sitting here. A
            # non-dict entry can never be the entry being replaced anyway.
            if not _is_usable_status_entry(existing):
                continue
            if existing["id"] == sid:
                sub_status[index] = status_entry
                break
        else:
            sub_status.append(status_entry)
        mid_status["subDeviceStatus"] = sub_status
        status[mid] = mid_status
        data["status"] = status

        self.data = data

    async def _async_update_data(self):
        """Fetch and decode data from RainPoint."""
        try:
            # Dispatch helpers via the class (not self.<method>) so the existing
            # SimpleNamespace-based test pattern in tests/test_coordinator.py
            # continues to work without modification.
            hubs = await RainPointCoordinator._collect_hubs(self)
            status_by_mid: dict[int, dict] = {}
            decoded_sensors: dict[str, dict] = {}
            absent_hubs: list[dict] = []
            hub_connectivity: dict[int, dict] = {}

            if hubs:
                status_by_mid = await RainPointCoordinator._fetch_status_by_mid(self, hubs)

            # The prior snapshot both _fetch_status_by_mid paths' guard
            # reads from, hoisted once rather than per hub. Read as late as
            # possible -- here, after every await above (_collect_hubs and
            # _fetch_status_by_mid) has already yielded control back to the
            # event loop -- because apply_hub_push_update is synchronous and
            # can run to completion during either of those awaits. Hoisting
            # this read any earlier would let a push landing in that window
            # go unseen by the guard below, then get silently discarded when
            # this poll's return value replaces self.data wholesale.
            #
            # Everything from here to the return below is synchronous: no
            # further await exists in this method, so this read is not just
            # "as late as possible" but the last point at which a
            # concurrently-arriving push could be missed, and the window
            # between this line and the return is zero-width rather than
            # merely small.
            #
            # Three of the four legs that hold that up are pinned rather than
            # left to this comment, because each can be taken away while every
            # behavioural test stays green. TestPollTailHasNoSuspensionPoint
            # covers two: no await below this line, and every helper the tail
            # reaches, at any depth, staying a synchronous method rather than
            # a coroutine or a job handed to the loop to run later.
            # TestPushDispatchNeverRunsOnPahoThread covers the third, that no
            # push reaches the coordinator except after a hop onto the loop.
            # The fourth is Home Assistant's, not ours: DataUpdateCoordinator
            # resumes from `self.data = await self._async_update_data()`
            # without yielding, so the assignment lands in the same task step
            # as the return. Nothing here can pin that, and a release that
            # changed it would reopen this window silently.
            #
            # Degrades to {} the same way hub_connectivity_record does, so a first
            # poll after startup (self.data is falsy) or a snapshot that
            # never gained a hub_connectivity key both resolve to no prior
            # record.
            prior_connectivity = (self.data or {}).get("hub_connectivity") or {}

            for hub in hubs:
                mid = hub["mid"]
                # An unreachable safety net once _fetch_status_by_mid covers
                # every hub mid: a mid genuinely missing here would mean its
                # status was never obtained this poll, not that it arrived empty.
                status = status_by_mid.get(mid, _absent_status())
                if isinstance(status, _AbsentStatus):
                    absent_hubs.append(hub)
                # The Bluetooth wrapper record has no cloud connection to report
                # on, so it must contribute no connectivity record at all. Both
                # the multipleDeviceStatus path and the _fallback_per_hub_status
                # path funnel into status_by_mid above, so applying the
                # guard at this one site is what makes the two fetch paths
                # observe an identical ordering rule; a second guard site would
                # be the way they could drift apart.
                if is_hub_record(hub):
                    hub_connectivity[mid] = _guard_hub_connectivity_order(
                        _read_hub_connectivity(status), prior_connectivity.get(mid)
                    )
                decoded_sensors.update(RainPointCoordinator._decode_hub_subdevices(self, hub, status))

            RainPointCoordinator._reconcile_repairs_surfaces(
                self, hubs, decoded_sensors, absent_hubs, hub_connectivity, prior_connectivity
            )

            _LOGGER.info("Coordinator update complete: %d hubs, %d sensors", len(hubs), len(decoded_sensors))
            # hubs is summarised rather than dumped: the raw records carry
            # deviceName, productKey and every cloud field beside them. The
            # sensor keys are this integration's own composed
            # {hid}_{mid}_{addr}, not cloud free text, so they stay. Gated like
            # the hub-record line below, because both arguments are built
            # eagerly whether or not the record is ever emitted.
            if _LOGGER.isEnabledFor(logging.DEBUG):
                _LOGGER.debug(
                    debug_with_version("Final data: hubs=%s, sensors=%s"),
                    _summarize_record(hubs),
                    list(decoded_sensors.keys()),
                )

            return {
                "hubs": hubs,
                "status": status_by_mid,
                "sensors": decoded_sensors,
                "hub_connectivity": hub_connectivity,
            }
        except RainPointApiError as err:
            raise UpdateFailed(f"RainPoint API error: {err}") from err
        except (aiohttp.ClientError, TimeoutError) as err:
            # The cloud being unreachable is an expected condition, not a defect,
            # so it gets the same one-line treatment RainPointApiError already
            # gets. It reaches this handler from the calls the two fetch helpers
            # do not wrap, `getDeviceByHid` above all, and it used to fall
            # through to the handler below and log a full traceback on every
            # failed poll. A reporter on a flaky link sent 242 of those in three
            # days, 98% of the lines in their log, which buried the one line
            # they had been asked for.
            #
            # DataUpdateCoordinator already logs its own "Error fetching ..."
            # line for an UpdateFailed and suppresses the repeats, so there is
            # deliberately no _LOGGER call of our own here.
            raise UpdateFailed(f"RainPoint transport error: {_error_text(err)}") from err
        except Exception as err:
            # Left broad and left loud: what reaches here now is a shape nobody
            # anticipated, which is exactly when a traceback earns its place.
            _LOGGER.exception("Unexpected RainPoint error while refreshing")
            raise UpdateFailed(f"Unexpected RainPoint error: {err}") from err

    def _reconcile_repairs_surfaces(
        self,
        hubs: list[dict],
        decoded_sensors: dict[str, dict],
        absent_hubs: list[dict],
        hub_connectivity: dict[int, dict],
        prior_connectivity: dict,
    ) -> None:
        """Reconcile both Repairs surfaces against one poll's outcome.

        Extracted from _async_update_data so that method stays within the
        project's cognitive-complexity budget; it is the whole outage-guard
        half of a poll and has no caller but that one. Mutates
        hub_connectivity in place for a provisionally missing hub, and
        otherwise writes only coordinator debounce state, so nothing here
        needs to reach the returned snapshot by any other route.

        A poll that returned no hubs at all, for an installation that had
        some a moment ago, is a device-list outage rather than evidence
        that every device left. Pruning and reconciling against it would
        wipe each debounce counter and clear each still-valid issue, then
        re-raise it once the list came back: the same clear-then-reraise
        cycle the absent-hub signal exists to prevent, entering through the
        device-list door instead of the status door. Skipping both is safe
        in the direction that matters, since a poll with no hubs also
        decodes no sensors and so can never raise anything. The guard
        covers both the not-reporting and the hub-connectivity state, since
        an empty device list is the same outage for both.
        """
        if hubs or not (self._silent_poll_counts or self._hub_disconnect_since):
            # Must run first, inside this guard, and only for a non-empty
            # device list. An empty list is a total outage and freezes the
            # enumeration memory entirely: no counter advances and
            # _last_poll_hub_keys is unchanged, so a partial list arriving
            # after a total outage still computes the correct missing set
            # against the pre-outage memory.
            #
            # The emptiness test is deliberately separate from the branch
            # condition above, because that condition does not partition
            # total outages cleanly: with nothing being debounced,
            # "not (silent or disconnect)" is true and an empty list falls
            # into this branch rather than the else below. Deriving the
            # freeze from the branch alone would therefore make a total
            # outage behave one way in a quiet installation and another
            # way once a single device happened to be mid-debounce, which
            # is the same event handled two ways. Keyed on the list itself,
            # both doors agree: an empty device list never advances
            # enumeration state, whatever else is being tracked.
            missing_hub_keys: frozenset[tuple[Any, int]] = frozenset()
            if hubs:
                missing_hub_keys = RainPointCoordinator._track_missing_hubs(self, hubs)
                # Ordering requirement, not a convenience: the call above must
                # run first, because its return value is what decides which
                # keys the tracker below is allowed to count, and its release
                # rule is what lifts the freeze.
                #
                # Inside this `if hubs:` block rather than the outer branch,
                # for the same reason the enumeration memory is: an empty
                # device list is a total outage and must freeze this counter
                # entirely. Advancing it there would let one failed
                # getDeviceByHid push every key in the account a step closer
                # to being offered for deletion at once.
                aged_out = RainPointCoordinator._track_orphaned_keys(self, hubs, missing_hub_keys=missing_hub_keys)
                self._aged_out_sensor_keys = aged_out

            # Deliberately an independent inline hold, not a reuse of
            # _merge_push_hub_connectivity or
            # _guard_hub_connectivity_order. _merge_push_hub_connectivity
            # assigns self.data directly, while this method returns a
            # dict that DataUpdateCoordinator then assigns; using it here
            # would write a snapshot this poll's own return value
            # immediately overwrites. _guard_hub_connectivity_order
            # orders a polled record against a held one, and a missing
            # hub produces no polled record to order -- bolting a hold
            # onto it would require synthesizing a stand-in record, the
            # same shape _prune_hub_connectivity_state's parameter exists
            # to avoid: any invented identity leaking into
            # coordinator.data["hubs"] reaches every entity platform. So
            # this reads the already-hoisted prior_connectivity local
            # directly and writes hub_connectivity[mid] for provisionally
            # missing hubs only, carrying the record unchanged. Writes no
            # entry when there is no prior record, so a hub that vanished
            # before it ever reported stays absent rather than gaining
            # an invented one. This narrows the window in which a
            # concurrent push is lost: a push that landed before the
            # prior_connectivity hoist above survives the gap instead of
            # being dropped. What is left after that is not a narrower
            # window but no window: the hoist sits after every await in
            # _async_update_data and nothing below it suspends or defers,
            # so no push callback can run between the two. The hoist's own
            # comment carries the full argument and names what pins it.
            for _missing_hid, missing_mid in missing_hub_keys:
                held_record = prior_connectivity.get(missing_mid)
                if held_record is not None:
                    hub_connectivity[missing_mid] = held_record

            RainPointCoordinator._prune_silent_state(self, hubs, missing_hub_keys=missing_hub_keys)
            RainPointCoordinator._sync_silent_device_issues(self, decoded_sensors, absent_hubs, missing_hub_keys=missing_hub_keys)
            RainPointCoordinator._prune_hub_connectivity_state(self, hubs, missing_hub_keys=missing_hub_keys)
            RainPointCoordinator._sync_hub_connectivity_issues(self, hubs, hub_connectivity, missing_hub_keys=missing_hub_keys)
        else:
            _LOGGER.warning(
                "Device list came back empty while %d sub-device(s) and %d hub(s) were being "
                "tracked; treating it as an outage and leaving connectivity state untouched",
                len(self._silent_poll_counts),
                len(self._hub_disconnect_since),
            )

    async def _collect_hubs(self) -> list[dict]:
        """Fetch hubs for every configured hid and inject hid + brand metadata."""
        homes = self._hids
        hubs: list[dict] = []
        _LOGGER.info("Updating data for HIDs: %s", homes)
        for hid in homes:
            devices = await self._client.get_devices_by_hid(hid)
            _LOGGER.info("Found %d devices for HID %s", len(devices), hid)
            for hub in devices:
                hub_copy = dict(hub)
                hub_copy["hid"] = hid
                # All devices are RainPoint hardware
                hub_copy["brand"] = "RainPoint"
                # The hub record's shape, for diagnosing which hub-level fields
                # (RF channel, firmware, etc.) the cloud is sending that the
                # integration does not yet surface. This used to dump the whole
                # record, which put the hub MAC, deviceName, productKey, iotId
                # and every param blob into the log. The key set answers the
                # same question -- a field the vendor adds shows up as a name --
                # without carrying a single value, and the model string goes
                # with it because it is cloud free text on a cloud-record path.
                # Still gated: the summary is far cheaper than the json.dumps it
                # replaces, but it is built eagerly as a call argument, so the
                # gate is what keeps it off the hot path when debug is off.
                if _LOGGER.isEnabledFor(logging.DEBUG):
                    _LOGGER.debug("Raw hub record mid=%s: %s", hub.get("mid"), _summarize_record(hub))
                hubs.append(hub_copy)
        return hubs

    async def _fetch_status_by_mid(self, hubs: list[dict]) -> dict[int, dict]:
        """Try multipleDeviceStatus first, fall back to per-hub get_device_status on empty
        response or transport-level errors (aiohttp.ClientError / TimeoutError).
        RainPointApiError surfaces to UpdateFailed; programming bugs propagate to the outer
        Exception handler instead of being silently masked by the fallback."""
        # Prepare device list for multipleDeviceStatus API
        device_list = [
            {"mid": hub["mid"], "deviceName": hub.get("deviceName", ""), "productKey": hub.get("productKey", "")} for hub in hubs
        ]

        # Try multipleDeviceStatus first (more efficient)
        multiple_status: list | None = None
        # Set when the call never returned at all, which is what separates the two
        # sentences the fallback below can say about itself.
        transport_failed = False
        try:
            multiple_status = await self._client.get_multiple_device_status(device_list)
            _LOGGER.debug(
                debug_with_version("multipleDeviceStatus successful, got data for %d devices"),
                len(multiple_status) if multiple_status else 0,
            )
        except RainPointApiError:
            # Surface API errors to the outer except RainPointApiError -> UpdateFailed wrapper
            # so HA marks entities unavailable instead of silently falling back.
            raise
        except (aiohttp.ClientError, TimeoutError) as e:
            # Only treat transport-level errors as transient. Programming bugs
            # (KeyError, AttributeError, etc.) propagate to the outer handler so
            # they surface as UpdateFailed and are not silently masked by the fallback.
            transport_failed = True
            _LOGGER.warning("multipleDeviceStatus transport error, falling back to individual calls: %s", _error_text(e))

        # Convert response to status_by_mid format when populated.
        # Note: get_multiple_device_status already converts "status" to "subDeviceStatus".
        if multiple_status:
            status_by_mid: dict[int, dict] = {}
            for device_data in multiple_status:
                mid = device_data["mid"]
                status_array = device_data.get("subDeviceStatus", [])
                status_by_mid[mid] = {"subDeviceStatus": status_array}
                _LOGGER.debug(debug_with_version("Fetched status for mid=%s using multipleDeviceStatus"), mid)
            # The call succeeded, so a hub mid the response did not mention is
            # evidence about that hub (arrived, reported nobody), not an outage
            # Filling it in here is what makes the absent-vs-omitted split
            # correct for the exact case it exists for: a hub whose status
            # came back but never named one of its addrs.
            for hub in hubs:
                status_by_mid.setdefault(hub["mid"], {"subDeviceStatus": []})
            return status_by_mid

        # Plain conditional fallback path: empty / None / transient-error multi-status all
        # converge here, replacing the prior raised-exception sentinel that doubled as
        # control flow. The convergence is deliberate and stays; only the line is
        # conditional, because a call that raised did not return empty data, it did
        # not return at all, and saying both about one failure was two warnings and
        # one of them untrue.
        if not transport_failed:
            _LOGGER.warning("multipleDeviceStatus returned empty data, falling back to individual calls")
        # Class-level dispatch matches the orchestrator pattern in _async_update_data so the
        # SimpleNamespace-based test fixture in tests/test_coordinator.py keeps working
        # without modification.
        return await RainPointCoordinator._fallback_per_hub_status(self, hubs)

    async def _fallback_per_hub_status(self, hubs: list[dict]) -> dict[int, dict]:
        """Per-hub fallback fetch. RainPointApiError surfaces to UpdateFailed; transport
        errors (aiohttp.ClientError / TimeoutError) record an empty subDeviceStatus list
        and continue with the next hub so a single transient hub failure does not wipe a
        multi-hub poll. Programming bugs propagate to the outer Exception handler."""
        status_by_mid: dict[int, dict] = {}
        for hub in hubs:
            mid = hub["mid"]
            try:
                status = await self._client.get_device_status(mid)
                status_by_mid[mid] = status
                _LOGGER.debug(debug_with_version("Fetched status for mid=%s using individual call"), mid)
            except RainPointApiError:
                # Surface API errors to the outer except RainPointApiError -> UpdateFailed wrapper.
                raise
            except (aiohttp.ClientError, TimeoutError) as individual_e:
                # WARNING rather than ERROR: this is a condition the loop is
                # written to absorb, it recovers on the next poll, and the hub
                # is already marked absent below. ERROR reads as a fault needing
                # attention, which one unreachable poll on a flaky link is not.
                _LOGGER.warning("Transport error getting status for mid=%s: %s", mid, _error_text(individual_e))
                # This hub's status was not obtained this poll -- an outage, not
                # evidence that it reported nobody -- so it must contribute no
                # silent entries for any of its children.
                status_by_mid[mid] = _absent_status()
        return status_by_mid

    def _notify_unknown_model(
        self, model: str | None, mid: int, addr: int, raw_value: str, model_code: int | str | None = None
    ) -> None:
        """Log the unsupported-sensor warning and fire a once-per-variant persistent notification.

        Reports modelCode alongside the model string because the two are not
        equivalent: the RainPoint catalog contains model strings that map to more
        than one modelCode, and the variants can differ in port count. A report
        carrying only the model string can therefore be ambiguous.

        Deduplication is keyed on (model, model_code) for the same reason, so
        two variants sharing a model string are each reported once rather than
        the second being suppressed as a duplicate of the first.
        """
        _LOGGER.warning(
            "=" * 60 + "\n"
            "UNSUPPORTED SENSOR MODEL DETECTED\n"
            "Please report this to: %s\n"
            "Include the following information:\n"
            "  Model: %s\n"
            "  Model Code: %s\n"
            "  Device ID (mid): %s\n"
            "  Address: %s\n"
            "  Raw Payload: %s\n" + "=" * 60,
            ISSUE_URL,
            model,
            model_code,
            mid,
            addr,
            raw_value,
        )
        # Send persistent notification (once per model/modelCode variant)
        variant = (model, model_code)
        if model and variant not in self._notified_unknown_models:
            self._notified_unknown_models.add(variant)
            # Sanitized for the same reason model is: modelCode is cloud-typed
            # (int | str | None), it lands on a Markdown-rendered line, and a
            # backtick in it would close the span it sits inside.
            code_line = f" (modelCode `{_sanitize_placeholder(model_code)}`)" if model_code is not None else ""
            # Only suffix the notification id when a code is present. Devices
            # without a modelCode must keep the pre-existing
            # "rainpoint_unsupported_{model}" id, otherwise reloading the
            # integration leaves the old notification in place and adds a
            # second one under "..._None" instead of replacing it.
            code_suffix = f"_{model_code}" if model_code is not None else ""
            # Home Assistant renders a persistent notification as Markdown and
            # both of these come from the cloud unvalidated, so the same
            # reasoning that guards the Repairs cards applies here. Only the
            # displayed copy is treated: the notification id stays keyed on the
            # raw model so a reload still replaces its own notification rather
            # than adding a second one, and the report link keeps the true
            # model and payload because urlencode already makes them safe and a
            # maintainer needs the real strings.
            safe_model = _sanitize_placeholder(model)
            safe_payload = _fence_safe(raw_value)
            report_url = _build_new_device_issue_url(model, raw_value, model_code)
            async_create(
                self.hass,
                f"RainPoint detected an unsupported sensor model: **{safe_model}**{code_line}\n\n"
                f"**[Report this device]({report_url})** to help add support. The link opens a "
                f"New device support form with the model and payload already filled in; just add "
                f"what the RainPoint app shows and submit.\n\n"
                f"Prefer to file it by hand? Open {ISSUE_URL} and include this raw payload:\n"
                f"```\n{safe_payload}\n```\n\n"
                f"You can also find this data in the sensor's attributes in Home Assistant.",
                title="RainPoint: Unsupported Sensor Detected",
                notification_id=f"rainpoint_unsupported_{model}{code_suffix}",
            )

    def _decode_one_subdevice(
        self,
        hub: dict,
        mid: int,
        addr: int,
        sub: dict,
        status_entry: dict,
    ) -> tuple[str, dict]:
        """Decode a single sub-device and return (sensor_key, sensor_entry_dict)."""
        sid = status_entry.get("id", "")
        raw_value = status_entry.get("value")
        model = sub.get("model")

        if not raw_value:
            # No reading / offline
            decoded: dict | None = None
            _LOGGER.debug("No raw_value for mid=%s addr=%s (sid=%s)", mid, addr, sid)
        else:
            try:
                _LOGGER.debug(
                    debug_with_version("Decoding payload for model=%s mid=%s addr=%s: %s"),
                    model,
                    mid,
                    addr,
                    raw_value,
                )
                model_code = sub.get("modelCode")
                decoded = _decode_subdevice_payload(model, raw_value, model_code)
                if decoded.get("type") == "unknown":
                    RainPointCoordinator._notify_unknown_model(self, model, mid, addr, raw_value, model_code)
                _LOGGER.debug(debug_with_version("Decoded data for mid=%s addr=%s: %s"), mid, addr, decoded)
            except Exception as ex:
                _LOGGER.warning(
                    "Failed to decode payload for %s addr=%s: %s",
                    model,
                    addr,
                    ex,
                )
                decoded = None

        _attach_device_timestamp(decoded, status_entry)

        sensor_key = _sensor_key(hub["hid"], mid, addr)
        decoded = self._preserve_recent_valve_command_state(
            sensor_key,
            model,
            decoded,
            status_entry,
        )
        sensor_entry = _build_sensor_entry(hub, sub, mid, addr, status_entry, decoded)
        # The entry itself is never dumped: it carries device_name, product_key,
        # hub_name, sub_name, model and the raw status payload. The key is this
        # integration's own {hid}_{mid}_{addr}, so it stays as the thing that
        # makes the line greppable against an entity. Gated because this runs
        # once per sub-device per poll and the summary is built eagerly.
        if _LOGGER.isEnabledFor(logging.DEBUG):
            _LOGGER.debug(debug_with_version("Sensor entity key=%s info=%s"), sensor_key, _summarize_record(sensor_entry))
        return sensor_key, sensor_entry

    def _preserve_recent_valve_command_state(
        self,
        sensor_key: str,
        model: str | None,
        decoded: dict | None,
        status_entry: dict,
    ) -> dict | None:
        """Keep fresh command response zone state when a cloud poll is stale."""
        # Dispatched before the zone-shaped body below, not folded into it.
        # The HIC801W carries no zones mapping, so every line that follows
        # would fall straight through for it, and what has to be preserved is
        # a different thing: one aggregate record rather than a mapping of
        # independently-commanded zones.
        if model == MODEL_HIC801W:
            # Class-level dispatch, not self._..., matching every other
            # extracted helper in this module: parts of the test suite drive
            # these bodies with `self` faked as a SimpleNamespace, which
            # carries no methods of its own.
            return RainPointCoordinator._preserve_recent_hic_command_state(self, sensor_key, decoded, status_entry)
        if model not in VALVE_MODELS or not decoded or not isinstance(decoded.get("zones"), dict):
            return decoded

        current_data = (self.data or {}).get("sensors", {}).get(sensor_key, {}).get("data") or {}
        current_zones = current_data.get("zones") or {}
        if not current_zones:
            return decoded

        poll_time = _status_entry_time(status_entry)
        poll_time_iso = poll_time.isoformat() if poll_time else None
        now = datetime.now(UTC)
        zones = dict(decoded["zones"])
        changed = False

        for zone_num in zones:
            last_command_time = self._last_valve_command_at.get((sensor_key, zone_num))
            if last_command_time is None or zone_num not in current_zones:
                continue

            if not _valve_zone_poll_is_stale(poll_time, last_command_time, now):
                continue

            _LOGGER.debug(
                "Ignoring stale RainPoint valve poll for key=%s zone=%s: poll_time=%s, last_command_time=%s",
                sensor_key,
                zone_num,
                poll_time_iso,
                last_command_time.isoformat(),
            )
            zones[zone_num] = current_zones[zone_num]
            changed = True

        if not changed:
            return decoded

        preserved = dict(decoded)
        preserved["zones"] = zones
        return preserved

    def _preserve_recent_hic_command_state(
        self,
        sensor_key: str,
        decoded: dict | None,
        status_entry: dict,
    ) -> dict | None:
        """Keep an HIC801W's command-fresh record when a cloud poll is stale.

        The station-shaped counterpart to the zone walk above, and it
        preserves the record whole rather than per station. The controller
        reports one aggregate frame naming the single station it is running,
        so there is no per-station state to merge selectively: replacing part
        of it would leave a record describing two different moments.

        Any station's recent command holds the whole record for that reason,
        and the most recent one wins. A stop on station 1 and a start on
        station 2 both rewrite the same aggregate, so the question this has to
        answer is only whether the poll predates the last thing this
        integration told the controller to do.

        Falls through to the polled record whenever the current one carries no
        station reading. That covers the first poll, a silent entry, and a
        frame that failed its shape check, and in each case the polled reading
        is the better of the two rather than something to protect.
        """
        if not decoded:
            return decoded

        current_data = (self.data or {}).get("sensors", {}).get(sensor_key, {}).get("data") or {}
        if current_data.get("current_station") is None:
            return decoded

        last_command_time = max(
            (command_at for (key, _station), command_at in self._last_valve_command_at.items() if key == sensor_key),
            default=None,
        )
        if last_command_time is None:
            return decoded

        poll_time = _status_entry_time(status_entry)
        if not _valve_zone_poll_is_stale(poll_time, last_command_time, datetime.now(UTC)):
            return decoded

        _LOGGER.debug(
            "Ignoring stale RainPoint irrigation controller poll for key=%s: poll_time=%s, last_command_time=%s",
            sensor_key,
            poll_time.isoformat() if poll_time else None,
            last_command_time.isoformat(),
        )
        return current_data

    def _warn_on_malformed_records(self, hub: dict, status: dict) -> None:
        """Warn once per hub per degradation edge when a cloud record is unusable.

        Counts the subDevices entries _is_usable_sub_device rejects and the
        subDeviceStatus entries _is_usable_status_entry rejects. A hub with
        neither kind is dropped from _warned_malformed_records (if present)
        and nothing is logged: that is the clean poll that re-arms the next
        degradation. A hub already in the set stays quiet, so the WARNING
        fires exactly once per degradation edge rather than once per poll.

        The consequence outlives the log line, and this method changes
        nothing about it: a skipped record carries no addr, so it is
        invisible to _prune_silent_state and _track_orphaned_keys, and any
        previously listed key for it counts toward removal whether or not
        this warning ever fires.

        Carries only the hub key and integer counts, following the
        discipline above _track_missing_hubs' warning: never a record's
        contents, a name, a model or any other cloud-supplied string.
        """
        malformed_devices = sum(1 for sd in hub.get("subDevices") or [] if not _is_usable_sub_device(sd))
        malformed_status = sum(1 for entry in status.get("subDeviceStatus") or [] if not _is_usable_status_entry(entry))
        hub_key = f"{hub['hid']}_{hub['mid']}"
        if malformed_devices == 0 and malformed_status == 0:
            self._warned_malformed_records.discard(hub_key)
            return
        if hub_key in self._warned_malformed_records:
            return
        self._warned_malformed_records.add(hub_key)
        _LOGGER.warning(
            "Hub %s reported %d unusable sub-device record(s) and %d unusable status record(s); "
            "skipping them, which leaves any previously listed key of theirs counting toward removal",
            hub_key,
            malformed_devices,
            malformed_status,
        )

    def _decode_hub_subdevices(self, hub: dict, status: dict) -> dict[str, dict]:
        """Walk this hub's own sub-device list and return a {sensor_key: sensor_entry} dict.

        Driven by addr_map (the addrs the hub itself lists), not by the status
        response: an addr the hub lists but the status response omits still
        gets a debounced "silent" entry, which a loop driven from
        the status response could never produce -- that asymmetry was the
        actual defect. A status entry whose sid resolves to an addr the hub
        does not list is still dropped, and an unresolvable sid is still
        ignored, exactly as before.

        An absent status (this hub's status could not be obtained this poll)
        contributes nothing at all for any of its children: that is an
        outage, not evidence about any particular addr.
        """
        mid = hub["mid"]
        _LOGGER.debug(debug_with_version("Processing hub mid=%s with status"), mid)

        # Before the absent-status return below, not after: an absent status
        # contributes no sensors, but the hub's subDevices list is still
        # walked by _track_orphaned_keys in the same poll, so a record
        # skipped there still carries the removal consequence and must still
        # be reported. The absent sentinel carries an empty subDeviceStatus,
        # so the status-side count is naturally zero on that path.
        RainPointCoordinator._warn_on_malformed_records(self, hub, status)

        if isinstance(status, _AbsentStatus):
            _LOGGER.debug(debug_with_version("Status absent for mid=%s; contributing nothing"), mid)
            return {}

        status_by_addr: dict[int, dict] = {}
        for entry in status.get("subDeviceStatus") or []:
            if not _is_usable_status_entry(entry):
                continue
            addr = _resolve_addr_from_sid(entry["id"])
            if addr is None:
                continue
            # Dropping the intermediate id-keyed dict changes nothing: both
            # the old two-step lookup and this single pass resolve on addr in
            # list order, so whichever entry appears last for a given addr
            # wins either way. _resolve_addr_from_sid is emphatically not
            # injective, and nothing here needs it to be: it is int(sid[1:]),
            # so "D1" and "D01" both resolve to addr 1. The old intermediate
            # dict deduplicated on exact sid string equality, which never
            # prevented that collision either.
            status_by_addr[addr] = entry
        _LOGGER.debug(debug_with_version("Parsed status_by_addr for mid=%s: %s keys"), mid, len(status_by_addr))

        # Map addr -> subDevice, the primary loop (promoted from sub_status).
        addr_map = _sub_devices_by_addr(hub)

        decoded_sensors: dict[str, dict] = {}
        for addr, sub in addr_map.items():
            sensor_key = _sensor_key(hub["hid"], mid, addr)
            s = status_by_addr.get(addr)
            if s is not None:
                sensor_key, sensor_entry = RainPointCoordinator._decode_one_subdevice(self, hub, mid, addr, sub, s)
                # A real reading resets the debounce counter.
                self._silent_poll_counts.pop(sensor_key, None)
            else:
                sensor_entry = RainPointCoordinator._build_silent_subdevice(self, hub, mid, addr, sub, sensor_key)
            if sensor_entry is not None:
                decoded_sensors[sensor_key] = sensor_entry

        return decoded_sensors

    def _build_silent_subdevice(
        self,
        hub: dict,
        mid: int,
        addr: int,
        sub: dict,
        sensor_key: str,
    ) -> dict | None:
        """Return a "silent" sensor entry once an addr has been omitted for
        SILENT_DEBOUNCE_POLLS consecutive arrived-but-silent polls, else None.

        Bypasses _decode_one_subdevice entirely: there is no payload to decode,
        so there is nothing to run _notify_unknown_model against either.
        """
        count = self._silent_poll_counts.get(sensor_key, 0) + 1
        self._silent_poll_counts[sensor_key] = count
        if count < SILENT_DEBOUNCE_POLLS:
            return None

        previous = (self.data or {}).get("sensors", {}).get(sensor_key)
        last_seen = _last_seen_from_entry(previous)
        decoded = {
            "type": SILENT_DATA_TYPE,
            "model": sub.get("model"),
            "silent_state": "stopped_reporting" if last_seen else "never_reported",
            "last_seen": last_seen,
            "missed_polls": count,
        }
        # status_entry={} is deliberate: every downstream raw_status reader
        # already tolerates a missing "value"/"time" pair.
        return _build_sensor_entry(hub, sub, mid, addr, {}, decoded)

    def _track_missing_hubs(self, hubs: list[dict]) -> frozenset[tuple[Any, int]]:
        """Compare this poll's real hubs against the last trusted poll's, and
        return the (hid, mid) of every hub still within its provisional
        absence window.

        A hub reappearing at all resets its absence counter regardless of
        what its subDevices lists: the hid/mid key alone is what "back"
        means here, not any judgement about its children. A hub missing for
        HUB_ABSENT_DEBOUNCE_POLLS or fewer consecutive polls is provisional
        and keeps its counter and its remembered key; once a missing hub's
        consecutive-absence count exceeds that threshold it is released,
        dropping both the counter and the key, so the shrunken list becomes
        authoritative for it again and the memory this method owns cannot
        grow without bound.

        Every top-level record is remembered, not only the ones satisfying
        is_hub_record. A Bluetooth wrapper record is a parent that carries
        children, and its disappearance is the same outage for them that a real
        hub's is: nothing about it says whether any particular child has left.
        Excluding it meant _prune_silent_state dropped its children's debounce
        counters and _sync_silent_device_issues cleared their not-reporting
        cards the moment it went, which are then raised again once it returns
        and the window has served a second time. That is the
        clear-then-reraise cycle this method exists to prevent, reached
        through the one door it used to leave open.

        The key is available and it is stable. A wrapper record carries no
        identity fields, but it does carry a mid, and that mid is what every
        unique id behind it is already built from and persisted under. On the
        account this surface was written for it has read the same value in
        every capture taken since the record first appeared. The sub-device
        behind it has moved between parents, which is a different fact and is
        not evidence that the parent's own key churns.

        The cost of being wrong is bounded by this method's own release rule
        rather than open-ended. A remembered key is released once its absences
        exceed HUB_ABSENT_DEBOUNCE_POLLS, so a wrapper record that really has
        gone for good delays its children's removal counting by that many
        polls and no more, and holds a stale not-reporting card for the same
        span. Freezing for as long as the record is absent was rejected here
        for the same reason it was rejected for a real hub.

        A restart mid-gap clears every card this method is protecting: the
        Repairs issue itself survives in Home Assistant's registry, but
        _last_poll_hub_keys and _hub_absent_poll_counts do not, so the first
        poll after a restart has no prior list, sees nothing missing, and
        clears normally. This is pre-existing on every poll-counted surface
        in this file -- _silent_poll_counts and the empty-list guard both
        reset the same way on a fresh instance -- so this method introduces
        no new instance of it. Fixing it means seeding this state from the
        issue registry at setup. The hub-disconnect window is the one surface
        that no longer needs that, because it is measured against the cloud's
        own change timestamp and so survives a restart wherever the firmware
        reports one; _sync_hub_connectivity_issues' own docstring reasons
        about the path that still does not.

        This map answers a different question from an orphaned-entity sweep:
        it remembers hub enumeration to decide whether a poll is
        trustworthy, while such a sweep needs to find entity-registry rows
        whose sensor key has vanished from coordinator.data["sensors"]. The
        two are deliberately not merged.

        Nothing here reads from MQTT, and apply_push_update /
        apply_hub_push_update read nothing from here: MQTT carries no
        enumeration information, so inferring hub presence from a pushed
        payload would let one chatty sub-device mask a hub that has really
        left the account, which is exactly the case this method's release
        rule exists to catch. Observed consequence, recorded rather than
        fixed here: during a gap, a push addressed to a child of the missing
        hub is dropped by apply_push_update's own unknown-mid guard, since
        that method resolves the hub from coordinator.data["hubs"] and the
        missing hub is not in it. That is pre-existing behaviour this method
        deliberately does not change.
        """
        current_keys = {(hub["hid"], hub["mid"]) for hub in hubs}
        missing_keys = self._last_poll_hub_keys - current_keys

        # A hub reappearing at all resets its counter, regardless of what its
        # subDevices lists.
        for key in current_keys:
            self._hub_absent_poll_counts.pop(key, None)

        provisional_keys: set[tuple[Any, int]] = set()
        for key in missing_keys:
            count = self._hub_absent_poll_counts.get(key, 0) + 1
            # "<=" here, not "<": HUB_ABSENT_DEBOUNCE_POLLS counts how many
            # consecutive absences stay provisional, so absences one through
            # the threshold suppress and the next one releases. This is the
            # single most likely thing a later reader will "fix" incorrectly
            # by copying the verdict-fires comparison
            # ORPHANED_KEY_DEBOUNCE_POLLS uses at its own use site.
            if count <= HUB_ABSENT_DEBOUNCE_POLLS:
                self._hub_absent_poll_counts[key] = count
                provisional_keys.add(key)
                # Bounded to at most HUB_ABSENT_DEBOUNCE_POLLS warnings per
                # hub per gap. Carries only the hub key and integer counts --
                # never hub.get("name")/deviceName/model or any other
                # cloud-supplied string, since a missing hub has no dict here
                # to read them from anyway, and keeping cloud free text out
                # of the log line is what makes this surface immune to the
                # log-injection threat the Markdown-rendered Repairs cards
                # need _sanitize_placeholder for.
                protected_count = len(_sensor_keys_for_hub_keys(self._silent_poll_counts, {key}))
                _LOGGER.warning(
                    "Hub %s missing from device list (%d/%d consecutive poll(s)); "
                    "treating as an outage and protecting %d tracked sensor key(s)",
                    key,
                    count,
                    HUB_ABSENT_DEBOUNCE_POLLS,
                    protected_count,
                )
            else:
                self._hub_absent_poll_counts.pop(key, None)
                # One INFO line at the moment the shrunken list becomes
                # authoritative for this hub, since that is the moment its
                # cards can disappear and that moment needs its own
                # breadcrumb -- earlier defects on this path were invisible
                # in production logs.
                _LOGGER.info(
                    "Hub %s absent for %d consecutive polls; releasing it and treating the shrunken device list as authoritative",
                    key,
                    count,
                )

        self._last_poll_hub_keys = current_keys | provisional_keys
        return frozenset(provisional_keys)

    def _prune_silent_state(self, hubs: list[dict], *, missing_hub_keys: frozenset[tuple[Any, int]] = frozenset()) -> None:
        """Drop any debounce counter for an addr no hub currently lists.

        Runs every poll so a device that leaves subDevices (unpaired, removed)
        cannot accumulate a counter forever.

        missing_hub_keys carries the hub keys currently within their
        provisional absence window (the enumeration door, alongside the
        status-fetch door in _sync_silent_device_issues): a child of one of
        those hubs keeps its counter too, since the hub not appearing in
        this poll's device list says nothing about whether that particular
        child is still silent -- a missing hub is an outage, not evidence
        about any addr, so it can neither confirm nor deny that a child is
        silent. The increment half of that freeze holds for
        free: the only site that advances _silent_poll_counts is
        _build_silent_subdevice, reached from _decode_hub_subdevices, which a
        missing hub never enters. This function is the reset half, which is
        what the protected set below closes.

        A subDevices entry the shared shape helper skips is absent from
        live_keys, so a previously known key for it is dropped here exactly
        as if the addr had genuinely left the hub's enumeration. That is
        deliberate and unavoidable at record granularity: a record carrying
        no addr carries no identity, so there is no sensor key available to
        freeze. The exposure is bounded by the standing rule that no entity
        is ever removed without a human submitting the Repairs form, and it
        is made non-silent by _warn_on_malformed_records' breadcrumb.
        """
        live_keys = {_sensor_key(hub["hid"], hub["mid"], addr) for hub in hubs for addr in _sub_devices_by_addr(hub)}
        protected_keys = _sensor_keys_for_hub_keys(self._silent_poll_counts, missing_hub_keys)
        self._silent_poll_counts = {
            key: count for key, count in self._silent_poll_counts.items() if key in live_keys or key in protected_keys
        }

    def _track_orphaned_keys(
        self, hubs: list[dict], *, missing_hub_keys: frozenset[tuple[Any, int]] = frozenset()
    ) -> frozenset[str]:
        """Count how long each vanished sensor key has been gone, and return the aged-out set.

        "Gone" means absent from the hub's subDevices enumeration, never
        absent from coordinator.data["sensors"]. The two differ and the
        difference matters: a paired device the cloud returns no status for is
        absent from that dict for SILENT_DEBOUNCE_POLLS polls before a silent
        entry is built for it, so counting against it would start and abandon
        a removal count on every device that briefly goes quiet, and would
        entangle this window with the silent debounce for no benefit. This is
        the cloud saying the hub no longer carries that addr, which is what a
        re-key or an unpair actually is.

        The returned set is opaque to this class beyond being sensor keys: the
        coordinator counts, and the listener in the package __init__ decides
        what, if anything, to offer for removal.

        A key whose hub is inside its provisional absence window is skipped
        entirely: not incremented, not reset, and not newly aged out. Its
        stored count is left exactly as it is, so it resumes where it stopped.
        The reason transfers verbatim from _prune_silent_state: a missing hub
        is an outage, not evidence about any addr, so it can neither confirm
        nor deny that a child has left, and no key may ever be offered for
        removal on evidence a poll did not contain. Keeping a frozen key in
        _last_poll_sensor_keys is what makes the resume work; dropping it
        there would stop it being counted the moment its hub came back.

        A key that already reached the threshold keeps its aged-out verdict
        through the freeze. Withdrawing it would clear the card on the outage
        and re-raise it once the hub returned, which is the clear-then-reraise
        cycle every outage guard in this file exists to prevent, arriving from
        the other side.

        Nothing carries a released hub forward, and nothing needs to:
        _track_missing_hubs drops a hub from _hub_absent_poll_counts and
        _last_poll_hub_keys once its absences exceed
        HUB_ABSENT_DEBOUNCE_POLLS, so it stops appearing in missing_hub_keys
        and the freeze lifts by itself on that same poll. The total protection
        for a child of a departing hub is HUB_ABSENT_DEBOUNCE_POLLS and
        ORPHANED_KEY_DEBOUNCE_POLLS in series, not one window alone. Staying
        frozen for as long as the hub is absent was rejected: an account that
        genuinely loses a hub would keep every child's leftover entities
        forever.

        The Bluetooth wrapper record reaches missing_hub_keys like any other
        top-level record, so its children are frozen rather than counted while
        it is absent. It used to be excluded, on the reading that a wrapper
        vanishing as a hub's mid changes was the case this surface exists for.
        That reading does not survive the capture it rests on: the mid that
        moved belonged to the sub-device changing parents, not to the wrapper,
        whose own mid has held the same value throughout. The case the freeze
        protects against is the ordinary one, a parent missing from one poll
        saying nothing about any child, and the freeze is released by
        _track_missing_hubs after HUB_ABSENT_DEBOUNCE_POLLS either way.

        What this does not touch, and the distinction is worth keeping
        straight, is a wrapper record that stays listed and loses a child.
        That child's key leaves the enumeration with its parent present, so
        nothing is missing, nothing is frozen, and the count runs as it
        should. Re-pairing a Bluetooth device onto a hub is exactly that
        shape.

        Every log line here carries only the sensor key and integer counts,
        never a cloud-supplied name or model, following _track_missing_hubs'
        discipline.

        A subDevices entry the shared shape helper skips is absent from
        live_keys, so a previously known key for it counts as missing here
        and advances toward ORPHANED_KEY_DEBOUNCE_POLLS on every poll it
        stays unusable, exactly as if the addr had genuinely left the hub's
        enumeration. That is deliberate and unavoidable at record
        granularity: a record carrying no addr carries no identity to freeze
        on, and freezing at hub granularity instead was considered and
        declined as beyond this method's scope. The exposure is bounded on
        both sides: this method's own consecutive-poll window, and the
        standing rule that no entity is ever removed without a human
        submitting the Repairs form. _warn_on_malformed_records' breadcrumb
        is what keeps that window from being silent.
        """
        # The same helper call _prune_silent_state makes, over the same
        # enumeration. The Bluetooth wrapper record is deliberately not
        # filtered out by is_hub_record: it carries real children, and
        # _prune_silent_state does not filter it either.
        live_keys = {_sensor_key(hub["hid"], hub["mid"], addr) for hub in hubs for addr in _sub_devices_by_addr(hub)}
        # Published from here rather than recomputed by the consumer, and this
        # assignment is the whole of what makes the removal confirm's staleness
        # guard agree with this counter about what "departed" means. Written
        # before the freeze below, so it is the enumeration this poll actually
        # carried rather than the subset this method chose to count.
        self._last_enumerated_sensor_keys = frozenset(live_keys)
        missing = self._last_poll_sensor_keys - live_keys
        # A missing hub is an outage, not evidence about any addr, so its
        # children's counters neither advance nor reset while it is inside its
        # provisional absence window.
        frozen = _sensor_keys_for_hub_keys(missing, missing_hub_keys)
        if frozen:
            # One line per frozen poll rather than one per key per poll, so a
            # frozen window is visible in production logs without drowning
            # them. Carries integer counts only.
            _LOGGER.debug(
                "Freezing %d orphan candidate key(s) under %d provisionally missing hub(s); "
                "neither counting nor resetting them this poll",
                len(frozen),
                len(missing_hub_keys),
            )

        # A hub that is present but enumerates nothing is trusted from the very
        # first poll: it is in neither the missing nor the provisional hub set,
        # so the freeze above does not reach it, and every child it stopped
        # listing starts counting on that same poll. That shape is a genuine
        # unpair-everything and an equally genuine partial-degradation response
        # from getDeviceByHid, and the integration cannot tell the two apart.
        # Logged rather than guarded, because the guard is a decision about
        # whether an empty enumeration is ever authoritative and what it costs
        # an account that really did unpair its last sub-device. Until that is
        # settled, the case has to at least be visible before the cards appear,
        # so the count is a warning naming the hub key and integer counts only.
        #
        # Gated on the transition rather than on the state, the same way the
        # age-out breadcrumb below is. The state is permanent once it holds:
        # missing only ever grows, and a hub that is present is never frozen,
        # so a hub that stops enumerating its children keeps every one of those
        # keys counted on every later poll. Warning on the state would put one
        # line per hub in the log every 120 seconds for the life of the
        # session, including long after the user confirmed the removal, since
        # the fix flow never reaches coordinator state.
        newly_empty: set[str] = set()
        for hub in hubs:
            if hub.get("subDevices"):
                continue
            hub_key = f"{hub['hid']}_{hub['mid']}"
            counted = {key for key in missing - frozen if key.rpartition("_")[0] == hub_key}
            if not counted:
                continue
            newly_empty.add(hub_key)
            if hub_key in self._warned_empty_enumeration:
                continue
            _LOGGER.warning(
                "Hub %s is listed but enumerates no sub-devices; counting %d of its previously listed "
                "key(s) toward removal, which an outage on this endpoint is indistinguishable from",
                hub_key,
                len(counted),
            )
        # Rebuilt whole rather than added to, so a hub that lists children again
        # re-arms: a second degradation is visible on its own edge instead of
        # being swallowed by the first one's mark. A hub that vanishes from the
        # list entirely drops out here too, which is the same re-arm reached
        # from the other side.
        self._warned_empty_enumeration = newly_empty

        # A key that reappears at all restarts from zero.
        for key in live_keys:
            self._orphaned_key_poll_counts.pop(key, None)

        # Read before the caller reassigns it, so the breadcrumb below fires
        # once per age-out transition rather than on every later poll.
        previously_aged_out = self._aged_out_sensor_keys
        aged_out: set[str] = set()
        for key in missing - frozen:
            count = self._orphaned_key_poll_counts.get(key, 0) + 1
            self._orphaned_key_poll_counts[key] = count
            # ">=" here, not "<=": this threshold counts polls until the card
            # can be offered, so the verdict fires once the count reaches it.
            # That is not the stays-provisional reading
            # HUB_ABSENT_DEBOUNCE_POLLS carries at its own use site above.
            if count >= ORPHANED_KEY_DEBOUNCE_POLLS:
                aged_out.add(key)
                if key not in previously_aged_out:
                    # The moment a card can appear, so it gets its own
                    # breadcrumb. Carries the sensor key and integer counts
                    # only -- never sub.get("name") or a model string.
                    _LOGGER.info(
                        "Sensor key %s no longer listed by its hub for %d consecutive polls; "
                        "offering its leftover entities for removal",
                        key,
                        count,
                    )

        # A frozen key that had already reached the threshold keeps its
        # verdict, read from its own stored count rather than from the caller's
        # copy of the last returned set.
        aged_out |= {key for key in frozen if self._orphaned_key_poll_counts.get(key, 0) >= ORPHANED_KEY_DEBOUNCE_POLLS}

        # A key is remembered once it has been seen and stays remembered while
        # it is being counted, so this holds one entry per sensor key that has
        # vanished in this session -- the same session-scoped bound the late
        # adders' add-once sets carry. It is deliberately not dropped on
        # age-out: dropping it would stop the key being counted and the card
        # would flap.
        #
        # Since missing is _last_poll_sensor_keys - live_keys, this assignment
        # is old | live_keys and the set only ever grows. Nothing prunes it, and
        # nothing prunes _orphaned_key_poll_counts either except a key
        # reappearing, so a confirmed removal leaves the key here and keeps
        # incrementing its count and reporting it aged out for the rest of the
        # session. That is inert rather than correct: the fix flow's forget
        # reaches the adders' ledgers and hass.data only, never coordinator
        # state, and it is the ledger that _build_orphaned_entity_records gates
        # on, so an emptied key yields no record and no card whatever this holds.
        # Both structures are session bounded and both go on a reload.
        self._last_poll_sensor_keys = live_keys | missing
        return frozenset(aged_out)

    def aged_out_sensor_keys(self) -> frozenset[str]:
        """Return the sensor keys absent long enough to be offered for removal.

        The whole of this class's side of the boundary. The coordinator counts
        and publishes an opaque key set; it knows nothing about entity
        registry rows, late adders or Repairs cards, and the listener that
        owns those knows nothing about how the counting works.

        Reflects the last poll that carried a device list. A poll with no hubs
        at all is a total outage and leaves this value untouched.
        """
        return self._aged_out_sensor_keys

    def enumerated_sensor_keys(self) -> frozenset[str] | None:
        """Return the keys the last device list actually enumerated, or None.

        The other half of the same boundary, and it exists so one consumer can
        ask the question this class's counter is built on rather than a
        near-miss of it. The removal confirm has to know whether RainPoint still
        lists an addr before it deletes that addr's rows, which is exactly what
        _track_orphaned_keys counts, and asking coordinator.data["sensors"]
        instead is the conflation that method's own docstring rules out: a
        device that is merely quiet is absent from ``sensors`` for up to
        SILENT_DEBOUNCE_POLLS polls while still being enumerated, so a guard
        reading ``sensors`` would read a briefly quiet device as departed and
        clear the way to deleting a live device's rows and their history.

        None means no poll has carried a device list yet, which is a different
        answer from an empty enumeration and the only one a caller may not treat
        as "this key is gone". Reflects the last poll that carried one: a total
        outage leaves this value untouched, so a stale answer still names the
        keys that were last seen, which errs toward keeping rows.
        """
        return self._last_enumerated_sensor_keys

    def _sync_silent_device_issues(
        self,
        decoded_sensors: dict[str, dict],
        absent_hubs: list[dict],
        *,
        missing_hub_keys: frozenset[tuple[Any, int]] = frozenset(),
    ) -> None:
        """Reconcile the per-device not-reporting repair issue against this poll's sensors.

        Translates this poll's sensor entries into plain SilentDeviceRecord
        instances -- repairs.py holds no knowledge of this dict's shape.
        Deliberately calls no unknown-model notification: that one only
        fires for a payload that decoded to "unknown", and a silent entry has
        no payload to decode at all. The repair issue is the notification for
        this case.

        absent_hubs carries the hubs whose status could not be obtained this
        poll. Such a hub contributes no decoded sensors at all, so without it
        its still-silent children look identical to children that left the
        sub-device list, and their issues would be cleared here and re-raised
        on the next successful poll. The asymmetry only ever suppresses
        clearing, never raising: a hub that cannot be reached also produces no
        silent entries, so there is nothing to raise from in the first place.

        missing_hub_keys carries the enumeration door alongside the
        status-fetch door above: the still-silent children of a hub
        currently within its provisional absence window. Suppression here is
        scoped per hub, never global -- in a poll where hub A is
        missing and hub B reported normally, only A's children enter the
        set, because missing_hub_keys names only A, and B's cards raise and
        clear on schedule. The asymmetry stated above holds trivially on
        this door too: a missing hub decodes no sensors, so there is nothing
        to raise from here either.
        """
        unreachable_ids = {
            silent_device_issue_id(hub["hid"], hub["mid"], addr) for hub in absent_hubs for addr in _sub_devices_by_addr(hub)
        }
        protected_keys = _sensor_keys_for_hub_keys(self._silent_poll_counts, missing_hub_keys)
        for protected_key in protected_keys:
            # Exact rather than a reconstruction: the issue id is the sensor
            # key with the prefix prepended, so recovering the three typed
            # parts and calling silent_device_issue_id keeps this in lockstep
            # with the id repairs.py itself builds. Splitting from the right
            # keeps a hid containing an underscore intact.
            hid_part, mid_part, addr_part = protected_key.rsplit("_", 2)
            unreachable_ids.add(silent_device_issue_id(hid_part, int(mid_part), int(addr_part)))
        records = [
            SilentDeviceRecord(
                hid=entry["hid"],
                mid=entry["mid"],
                addr=entry["addr"],
                model=entry.get("model"),
                hub_name=entry.get("hub_name"),
                missed_polls=(entry.get("data") or {}).get("missed_polls", 0),
                silent=(entry.get("data") or {}).get("type") == SILENT_DATA_TYPE,
                # The canonical is_hub_record verdict, computed once when this
                # entry was built (_build_sensor_entry) and read here rather
                # than re-derived from the raw hub fields it supersedes, so
                # this card and the via_device link (device.py) can never
                # drift onto two different answers to "is there a hub at
                # all", which is a different question from "what is it
                # called". Absence defaults to hub-linked, matching
                # build_sub_device_info's own default.
                #
                # Behaviour change on a shape never observed: is_hub_record
                # tests did, mac, productKey and model, while the retired
                # inline predicate tested only productKey and deviceName. A
                # top-level record carrying any of did, mac or model but
                # neither productKey nor deviceName therefore now counts as a
                # real hub. The not-reporting card is raised either way; what
                # changes is its "Hub:" line, which renders the record's own
                # (possibly empty) name instead of the literal "none". The two
                # predicates agree on the wrapper record, the only shape ever
                # captured, so this is invisible on all real data seen to date.
                hub_paired=entry.get("hub_paired", True),
            )
            for entry in decoded_sensors.values()
        ]
        self._silent_issues.async_sync(records, unreachable_ids=unreachable_ids)

    def _prune_hub_connectivity_state(
        self, hubs: list[dict], *, missing_hub_keys: frozenset[tuple[Any, int]] = frozenset()
    ) -> None:
        """Drop any hub-disconnect window start for a hub no longer listed.

        A hub removed from the account (unpaired, home restructured) must not
        hold a window start forever, mirroring _prune_silent_state's
        reasoning for sub-devices.

        missing_hub_keys must be passed explicitly rather than derived here:
        live_keys is built from hubs, and a hub absent from hubs is never
        iterated, so without this parameter a missing hub's window start is
        wiped on the very first missing poll and the freeze
        _prune_silent_state applies to a silent child fails on the
        connectivity side.
        """
        live_keys = {(hub["hid"], hub["mid"]) for hub in hubs if is_hub_record(hub)}
        self._hub_disconnect_since = {
            key: since for key, since in self._hub_disconnect_since.items() if key in live_keys or key in missing_hub_keys
        }

    def _hub_disconnect_window_start(self, key: tuple[Any, int], connectivity: dict, now: datetime) -> datetime:
        """Return, and store, the moment this hub's current outage started.

        Split out of _sync_hub_connectivity_issues' disconnected arm so that
        arm reads as one flat step alongside the other two tri-states. The
        two paths below are the same two the caller's docstring describes,
        and the choice between them is the only thing that happens here: the
        elapsed measurement and the threshold comparison stay with the caller,
        because they are what decides whether a record is emitted.
        """
        cloud_moment = _changed_at_datetime(connectivity)
        if cloud_moment is None:
            # No cloud change time: measure from the first poll that
            # observed this hub disconnected, and keep that stamp on
            # every later poll. setdefault rather than an assignment
            # is the whole debounce on this path -- re-stamping would
            # restart the window every poll and never raise.
            return self._hub_disconnect_since.setdefault(key, now)
        # Floored against whatever start this key already carries,
        # so the window can only ever move earlier. A later cloud
        # edge is still honoured on a key with no start yet, which
        # is what lets a restart mid-outage measure from the real
        # edge, but it can never push a running window forward.
        #
        # The floor is what keeps this branch honest on firmware
        # nobody here has captured. Every observed hub reports the
        # moment the connection changed, so re-reading it each poll
        # returns the same instant. A firmware whose entry carried
        # the moment of the report instead would hand back a newer
        # instant every poll, restarting the window forever and
        # raising no card at all, which is worse than the poll
        # count this replaced: that one always raised eventually.
        prior = self._hub_disconnect_since.get(key)
        since = cloud_moment if prior is None else min(prior, cloud_moment)
        self._hub_disconnect_since[key] = since
        return since

    def _sync_hub_connectivity_issues(
        self,
        hubs: list[dict],
        hub_connectivity: dict[int, dict],
        *,
        missing_hub_keys: frozenset[tuple[Any, int]] = frozenset(),
    ) -> None:
        """Reconcile the per-hub connectivity repair issue against this poll's tri-states.

        Translates each real hub's tri-state into a plain HubConnectivityRecord
        -- repairs.py holds no knowledge of this dict's shape. The Bluetooth
        wrapper record is skipped via is_hub_record, the same gate every other
        hub-level surface uses.

        On the connected tri-state the hub's disconnect window start is
        dropped and a non-disconnected record is emitted, which is the one
        and only path that clears a raised issue.

        On the disconnected tri-state a window start is established and the
        elapsed wall time measured against it; a record is emitted only once
        that elapsed time reaches HUB_DISCONNECT_DEBOUNCE_SECONDS. This is
        the debounce itself, and it is why a hub that has been reported down
        for under three minutes raises nothing. The window start is the
        cloud's own change moment where the record carries one, and
        otherwise the first poll that observed the disconnected tri-state
        (_hub_disconnect_window_start picks between the two; the note below
        says why the difference does not matter here). Below the threshold the hub's issue id
        goes into unreachable_ids instead, exactly as the unknown tri-state
        does, so those polls neither raise nor clear. Emitting a
        disconnected=False record there would be a lie, because a "not yet
        confirmed" record is indistinguishable from a "confirmed connected"
        one by the time it reaches the unconditional clear in repairs.py, so
        it would delete a still-accurate card and take a further window to
        re-raise it. _build_silent_subdevice makes the same choice for the
        same reason: say nothing until the debounce has decided.

        That reason does not depend on which of the two window-start paths
        a given hub takes, and it is worth saying which is which, because the
        restart behaviour differs and the earlier poll-counted version had
        only one of them. On the absent-timestamp path the window start is
        this instance's own first observation, so a hub still down across a
        restart or a reload starts its window again and needs a fresh three
        minutes. On the cloud-timestamp path the window start comes from the
        cloud rather than from this process, so a coordinator built while a
        hub has already been down for longer than the threshold can raise on
        its very first refresh with no gap at all. Both are correct; neither
        makes a below-threshold disconnected=False record any less of a lie.

        On the unknown tri-state (including a hub whose mid is altogether
        missing from hub_connectivity) no record is emitted, any running
        window start is dropped, and the hub's issue id is added to a per-poll
        unreachable_ids set so the reconcile below suppresses clearing it.
        This is a one-directional asymmetry: unknown suppresses clearing and
        never suppresses raising, because a hub whose connectivity is unknown
        produces no disconnected record to raise from in the first place.
        unreachable_ids is a local built fresh every call, never manager
        state.

        Dropping the window start is a deliberate reversal of what the poll
        count did here, and the reason is the mechanism rather than a change
        of mind. A count could only advance on an observed disconnect, so
        carrying it across an unknown poll was free; wall time advances on its
        own, so carrying a start across one lets the window keep growing
        through a reconnect that poll was too blind to see. The full sequence
        it prevents is in the branch's own comment below.

        missing_hub_keys names the enumeration door alongside the unknown
        tri-state above: a hub absent from hubs is never iterated by the
        loop below at all, so unlike the status-fetch door its id never
        reaches unreachable_ids without help -- a missing hub does not fall
        out through the unknown branch for free. Seeding unreachable_ids
        with each missing hub's id, and skipping
        self._hub_disconnect_since for those keys entirely, has to
        happen before the loop touches any hub the missing set does not
        name, so a held disconnected record on a hub that is also missing
        this poll cannot raise: doing so would raise a card from evidence
        the poll never contained, the connectivity-side statement of the
        same rule _prune_silent_state applies to a silent child. This door
        carries more weight than it did under a poll count, because elapsed
        wall time keeps running through a device-list gap whether or not any
        poll observes the hub; the loop never iterating a missing hub is the
        whole of what stops a card raising out of that gap.
        missing_hub_keys and the keys reachable from hubs are
        disjoint by construction -- a key is only missing when it is absent
        from hubs -- so this seeding can never collide with the loop's own
        verdicts. The empty-list guard in _reconcile_repairs_surfaces
        already covers both Repairs surfaces, so leaving this door covering
        only the not-reporting surface would make the two doors disagree
        about the same event.
        """
        records: list[HubConnectivityRecord] = []
        unreachable_ids: set[str] = {hub_connectivity_issue_id(hid, mid) for hid, mid in missing_hub_keys}
        # Read once, before the loop, so every hub in one poll is measured
        # against one instant rather than against a clock that moves between
        # them.
        now = self._time_source()

        for hub in hubs:
            if not is_hub_record(hub):
                continue
            hid = hub["hid"]
            mid = hub["mid"]
            key = (hid, mid)
            # An empty string is the cloud's way of omitting the field, not a
            # real hub name -- treat it as absent so the sanitizer's "unknown"
            # fallback fires instead of rendering a blank.
            hub_name = hub.get("name") or None
            # Same field the hub's own DeviceInfo carries, so the card names the
            # model the user sees on the device page rather than a second string.
            hub_model = hub.get("model") or None
            # Hoisted so the tri-state and the change moment below come from
            # one lookup rather than two that could disagree.
            connectivity = hub_connectivity.get(mid) or {}
            state = connectivity.get("state")

            if state == HUB_CONNECTED:
                self._hub_disconnect_since.pop(key, None)
                records.append(
                    HubConnectivityRecord(
                        hid=hid,
                        mid=mid,
                        hub_name=hub_name,
                        disconnected=False,
                        offline_seconds=0,
                        model=hub_model,
                    )
                )
            elif state == HUB_DISCONNECTED:
                # Class-level dispatch: test_coordinator.py drives this method
                # with a SimpleNamespace standing in for self, which carries no
                # bound methods of its own.
                since = RainPointCoordinator._hub_disconnect_window_start(self, key, connectivity, now)
                # A changed_at in the future (cloud clock skew) yields a
                # negative elapsed, which fails the comparison below and
                # suppresses the card. That is the safe direction, so it
                # needs no branch of its own.
                offline_seconds = int((now - since).total_seconds())
                if offline_seconds < HUB_DISCONNECT_DEBOUNCE_SECONDS:
                    unreachable_ids.add(hub_connectivity_issue_id(hid, mid))
                    continue
                records.append(
                    HubConnectivityRecord(
                        hid=hid,
                        mid=mid,
                        hub_name=hub_name,
                        disconnected=True,
                        offline_seconds=offline_seconds,
                        model=hub_model,
                    )
                )
            else:
                # An unknown tri-state drops any running window start. Under
                # the poll count this branch deliberately left the counter
                # alone, because a count only ever advanced on an observed
                # disconnect and could not run on its own. A wall-clock window
                # can: an unknown poll hides whatever happened during it, so a
                # start kept across one keeps accruing time through a reconnect
                # nobody saw, and the next disconnect then raises a card
                # claiming an outage that already ended. Restarting costs a
                # delayed raise and never invents one, which is the direction
                # this whole surface is built to fail in.
                #
                # It costs nothing on the path every observed hub takes: the
                # cloud's own change moment is re-read on the next disconnected
                # poll, so a genuinely continuous outage raises again straight
                # away. Only the no-change-time fallback pays, and there an
                # alternating unknown and disconnected sequence never matures.
                # That is the safe half of the same trade.
                self._hub_disconnect_since.pop(key, None)
                unreachable_ids.add(hub_connectivity_issue_id(hid, mid))

        self._hub_connectivity_issues.async_sync(records, unreachable_ids=unreachable_ids)
