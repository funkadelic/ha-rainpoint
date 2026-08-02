import json
import logging
import re
from collections.abc import Iterable
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
    decode_co2,
    decode_flowmeter,
    decode_generic,
    # New HCS decoder functions
    decode_hcs005frf,
    decode_hcs015arf,
    decode_hcs024frf_v1,
    decode_hcs0528arf,
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


class _AbsentStatus(dict):
    """Marker meaning "no status response arrived for this hub".

    Subclasses dict rather than using a bare sentinel object because
    _merge_push_sensor_entry and every other status reader do
    dict(status.get(mid, ...)) and similar dict operations; those must keep
    working unchanged. Callers distinguish it with isinstance(status, _AbsentStatus)
    rather than an equality check, since its contents are indistinguishable from
    a real "status arrived and reported nobody" dict.
    """


# The single module-level instance every absent-status call site shares.
STATUS_ABSENT = _AbsentStatus({"subDeviceStatus": []})

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

# Derived rather than independently tuned, so the coordinator holds one
# debounce concept instead of two that could drift apart. The same roughly
# six-minute window at the default scan interval absorbs a single blip
# without being too late to be a useful discovery surface for a hub that has
# genuinely fallen off the RainPoint cloud.
HUB_DISCONNECT_DEBOUNCE_POLLS = SILENT_DEBOUNCE_POLLS

# Derived for the same reason HUB_DISCONNECT_DEBOUNCE_POLLS is: one debounce
# concept, one place to retune. Reads differently at its use site, though: it
# counts how many consecutive absences from the device list stay provisional,
# so the comparison there is "<=" (absences one through this value suppress,
# the next one releases), where HUB_DISCONNECT_DEBOUNCE_POLLS's comparison is
# "<" (a raise fires once the count reaches the threshold).
HUB_ABSENT_DEBOUNCE_POLLS = SILENT_DEBOUNCE_POLLS

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

    Returns "" when the decode found no fields, so the caller can omit the
    pre-fill rather than seed the form with an empty section.
    """
    fields = (generic or {}).get("fields") or []
    if not fields:
        return ""
    dp_prefixed = generic.get("dp_id_prefixed", False)
    lines = []
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
    a lagging disconnected poll, the reconcile sees connected and pops the
    counter. When a held pushed disconnected state is kept against a
    lagging connected poll, the reconcile sees disconnected and increments,
    and because the guard writes the held changed_at into the record it
    returns, the hold repeats on every following poll until the REST view's
    own connected time advances past the held moment. Three such polls
    therefore raise a card that no poll independently observed as
    disconnected. That is the intended consequence of treating a newer held
    value as fresher than a lagging poll, and it is not the push path
    incrementing a counter: apply_hub_push_update never touches
    _hub_disconnect_poll_counts on a disconnected edge, the poll reconcile
    counts the guarded record it was handed. A later reader should not read
    this as a defect in the push path and "fix" it by exempting held
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


class RainPointCoordinator(DataUpdateCoordinator):
    """Coordinator for RainPoint polling."""

    def __init__(self, hass: HomeAssistant, client: RainPointClient, entry):
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
        self._notified_unknown_models: set[tuple[str | None, int | None]] = set()
        self._last_valve_command_at: dict[tuple[str, int], datetime] = {}
        self._silent_poll_counts: dict[str, int] = {}
        self._silent_issues = RainPointSilentDeviceIssues(hass)
        self._hub_disconnect_poll_counts: dict[tuple[Any, int], int] = {}
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
        # _hub_disconnect_poll_counts does, because only the poll (never a
        # push) ever writes this state.
        self._last_poll_hub_keys: set[tuple[Any, int]] = set()
        self._hub_absent_poll_counts: dict[tuple[Any, int], int] = {}

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

        sub = {sd["addr"]: sd for sd in hub.get("subDevices", [])}.get(addr)
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
        method also explicitly pops `_hub_disconnect_poll_counts` and calls
        `_hub_connectivity_issues.async_clear`: the merge is
        copy-on-write over coordinator data and touches neither, so clearing
        has to be explicit here, exactly as apply_push_update already does
        for the silent-device pair. A pushed disconnected edge leaves both
        untouched: raising the card stays poll-counted only, so "3
        consecutive polls" keeps meaning literally that.
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
            # debounce already rejects. So a pushed disconnected edge leaves both the
            # counter and the issue untouched: the counter stays
            # poll-counted only, so "3 consecutive polls" keeps meaning
            # literally that and the coordinator holds one debounce concept
            # rather than two.
            self._hub_disconnect_poll_counts.pop((hub["hid"], mid), None)
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
        # STATUS_ABSENT's contents are the same shape, so reusing the shared
        # sentinel here is equivalent and avoids a second copy of the literal.
        mid_status = dict(status.get(mid, STATUS_ABSENT))
        sub_status = list(mid_status.get("subDeviceStatus", []))
        for index, existing in enumerate(sub_status):
            if existing.get("id") == sid:
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
            # concurrently-arriving push could be missed. If a future change
            # adds an await between this line and the return (or makes one of
            # the helpers called below async), that reasoning stops holding
            # and this comment needs revisiting alongside it. Degrades to {}
            # the same way hub_connectivity_record already does, so a first
            # poll after startup (self.data is falsy) or a snapshot that
            # never gained a hub_connectivity key both resolve to no prior
            # record.
            prior_connectivity = (self.data or {}).get("hub_connectivity") or {}

            for hub in hubs:
                mid = hub["mid"]
                # STATUS_ABSENT is an unreachable safety net once _fetch_status_by_mid
                # covers every hub mid: a mid genuinely missing here would mean
                # its status was never obtained this poll, not that it arrived empty.
                status = status_by_mid.get(mid, STATUS_ABSENT)
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
            _LOGGER.debug(debug_with_version("Final data: hubs=%s, sensors=%s"), hubs, list(decoded_sensors.keys()))

            return {
                "hubs": hubs,
                "status": status_by_mid,
                "sensors": decoded_sensors,
                "hub_connectivity": hub_connectivity,
            }
        except RainPointApiError as err:
            raise UpdateFailed(f"RainPoint API error: {err}") from err
        except Exception as err:
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
        if hubs or not (self._silent_poll_counts or self._hub_disconnect_poll_counts):
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
            # concurrent push is lost (a push that landed before the
            # prior_connectivity hoist above now survives the gap instead
            # of being dropped) but does not close it: a push landing
            # between that hoist and this method's return is still lost,
            # unchanged.
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
                len(self._hub_disconnect_poll_counts),
            )

    async def _collect_hubs(self) -> list[dict]:
        """Fetch hubs for every configured hid and inject hid + brand metadata."""
        homes = self._hids
        hubs: list[dict] = []
        _LOGGER.info("Updating data for HIDs: %s", homes)
        for hid in homes:
            devices = await self._client.get_devices_by_hid(hid)
            _LOGGER.info("Found %d devices for HID %s: %s", len(devices), hid, [d.get("model", "unknown") for d in devices])
            for hub in devices:
                hub_copy = dict(hub)
                hub_copy["hid"] = hid
                # All devices are RainPoint hardware
                hub_copy["brand"] = "RainPoint"
                # Full raw hub record, for diagnosing hub-level fields (RF channel,
                # firmware, etc.) that the integration does not yet surface. Gated so
                # the json.dumps cost is only paid when debug logging is on.
                if _LOGGER.isEnabledFor(logging.DEBUG):
                    _LOGGER.debug(
                        "Raw hub record model=%s mid=%s: %s",
                        hub.get("model"),
                        hub.get("mid"),
                        json.dumps(hub, default=str, sort_keys=True),
                    )
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
            _LOGGER.warning("multipleDeviceStatus transport error, falling back to individual calls: %s", e)

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
        # control flow.
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
                _LOGGER.error("Transport error getting status for mid=%s: %s", mid, individual_e)
                # This hub's status was not obtained this poll -- an outage, not
                # evidence that it reported nobody -- so it must contribute no
                # silent entries for any of its children.
                status_by_mid[mid] = STATUS_ABSENT
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
        _LOGGER.debug(debug_with_version("Sensor entity key=%s info=%s"), sensor_key, sensor_entry)
        return sensor_key, sensor_entry

    def _preserve_recent_valve_command_state(
        self,
        sensor_key: str,
        model: str | None,
        decoded: dict | None,
        status_entry: dict,
    ) -> dict | None:
        """Keep fresh command response zone state when a cloud poll is stale."""
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

        if isinstance(status, _AbsentStatus):
            _LOGGER.debug(debug_with_version("Status absent for mid=%s; contributing nothing"), mid)
            return {}

        status_by_addr: dict[int, dict] = {}
        for sid, s in {s["id"]: s for s in status.get("subDeviceStatus", [])}.items():
            addr = _resolve_addr_from_sid(sid)
            if addr is None:
                continue
            status_by_addr[addr] = s
        _LOGGER.debug(debug_with_version("Parsed status_by_addr for mid=%s: %s keys"), mid, len(status_by_addr))

        # Map addr -> subDevice, the primary loop (promoted from sub_status).
        addr_map = {sd["addr"]: sd for sd in hub.get("subDevices", [])}

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

        A restart mid-gap clears every card this method is protecting: the
        Repairs issue itself survives in Home Assistant's registry, but
        _last_poll_hub_keys and _hub_absent_poll_counts do not, so the first
        poll after a restart has no prior list, sees nothing missing, and
        clears normally. This is pre-existing on every poll-counted surface
        in this file -- _silent_poll_counts, _hub_disconnect_poll_counts and
        the empty-list guard all reset the same way on a fresh instance, and
        _sync_hub_connectivity_issues' own docstring already reasons about
        it -- so this method introduces no new instance of it. Fixing it
        means seeding this state from the issue registry at setup, which
        lands better alongside a time-based debounce, where a cloud
        timestamp survives a restart for free.

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
        current_keys = {(hub["hid"], hub["mid"]) for hub in hubs if is_hub_record(hub)}
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
            # by copying HUB_DISCONNECT_DEBOUNCE_POLLS's "<" comparison.
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
        """
        live_keys = {_sensor_key(hub["hid"], hub["mid"], sd["addr"]) for hub in hubs for sd in hub.get("subDevices", [])}
        protected_keys = _sensor_keys_for_hub_keys(self._silent_poll_counts, missing_hub_keys)
        self._silent_poll_counts = {
            key: count for key, count in self._silent_poll_counts.items() if key in live_keys or key in protected_keys
        }

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
            silent_device_issue_id(hub["hid"], hub["mid"], sd["addr"]) for hub in absent_hubs for sd in hub.get("subDevices", [])
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
        """Drop any hub-disconnect debounce counter for a hub no longer listed.

        A hub removed from the account (unpaired, home restructured) must not
        accumulate a counter forever, mirroring _prune_silent_state's
        reasoning for sub-devices.

        missing_hub_keys must be passed explicitly rather than derived here:
        live_keys is built from hubs, and a hub absent from hubs is never
        iterated, so without this parameter a missing hub's counter is wiped
        on the very first missing poll and the freeze _prune_silent_state
        applies to a silent child fails on the connectivity side.
        """
        live_keys = {(hub["hid"], hub["mid"]) for hub in hubs if is_hub_record(hub)}
        self._hub_disconnect_poll_counts = {
            key: count for key, count in self._hub_disconnect_poll_counts.items() if key in live_keys or key in missing_hub_keys
        }

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

        On the connected tri-state the debounce counter is popped back to
        zero and a non-disconnected record is emitted, which is the one and
        only path that clears a raised issue.

        On the disconnected tri-state the counter is incremented, and a
        record is emitted only once the counter reaches
        HUB_DISCONNECT_DEBOUNCE_POLLS -- this is the debounce itself, and it
        is why one or two consecutive disconnected polls raise nothing.
        Below the threshold the hub's issue id goes into unreachable_ids
        instead, exactly as the unknown tri-state does, so those polls
        neither raise nor clear. Emitting a disconnected=False record there
        would be a lie: the counter is per-instance, so a hub that is still
        down across a restart or a reload starts counting from one again, and
        a "not yet confirmed" record is indistinguishable from a "confirmed
        connected" one by the time it reaches the unconditional clear in
        repairs.py. That would delete a still-accurate card and take two more
        polls to re-raise it. _build_silent_subdevice makes the same choice
        for the same reason: say nothing until the debounce has decided.

        On the unknown tri-state (including a hub whose mid is altogether
        missing from hub_connectivity) no record is emitted and the counter is
        left untouched in both directions; the hub's issue id is instead added
        to a per-poll unreachable_ids set so the reconcile below suppresses
        clearing it. This is a one-directional asymmetry: unknown suppresses
        clearing and never suppresses raising, because a hub whose
        connectivity is unknown produces no disconnected record to raise from
        in the first place. unreachable_ids is a local built fresh every call,
        never manager state.

        missing_hub_keys names the enumeration door alongside the unknown
        tri-state above: a hub absent from hubs is never iterated by the
        loop below at all, so unlike the status-fetch door its id never
        reaches unreachable_ids without help -- a missing hub does not fall
        out through the unknown branch for free. Seeding unreachable_ids
        with each missing hub's id, and skipping
        self._hub_disconnect_poll_counts for those keys entirely, has to
        happen before the loop touches any hub the missing set does not
        name, so a held disconnected record on a hub that is also missing
        this poll cannot advance the debounce: doing so would raise a card
        from evidence the poll never contained, the connectivity-side
        statement of the same rule _prune_silent_state applies to a silent
        child. missing_hub_keys and the keys reachable from hubs are
        disjoint by construction -- a key is only missing when it is absent
        from hubs -- so this seeding can never collide with the loop's own
        verdicts. The empty-list guard in _reconcile_repairs_surfaces
        already covers both Repairs surfaces, so leaving this door covering
        only the not-reporting surface would make the two doors disagree
        about the same event.
        """
        records: list[HubConnectivityRecord] = []
        unreachable_ids: set[str] = {hub_connectivity_issue_id(hid, mid) for hid, mid in missing_hub_keys}

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
            state = (hub_connectivity.get(mid) or {}).get("state")

            if state == HUB_CONNECTED:
                self._hub_disconnect_poll_counts.pop(key, None)
                records.append(
                    HubConnectivityRecord(
                        hid=hid,
                        mid=mid,
                        hub_name=hub_name,
                        disconnected=False,
                        missed_polls=0,
                        model=hub_model,
                    )
                )
            elif state == HUB_DISCONNECTED:
                count = self._hub_disconnect_poll_counts.get(key, 0) + 1
                self._hub_disconnect_poll_counts[key] = count
                if count < HUB_DISCONNECT_DEBOUNCE_POLLS:
                    unreachable_ids.add(hub_connectivity_issue_id(hid, mid))
                    continue
                records.append(
                    HubConnectivityRecord(
                        hid=hid,
                        mid=mid,
                        hub_name=hub_name,
                        disconnected=True,
                        missed_polls=count,
                        model=hub_model,
                    )
                )
            else:
                unreachable_ids.add(hub_connectivity_issue_id(hid, mid))

        self._hub_connectivity_issues.async_sync(records, unreachable_ids=unreachable_ids)
