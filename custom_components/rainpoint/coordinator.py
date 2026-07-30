import json
import logging
import re
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
from .repairs import RainPointSilentDeviceIssues, SilentDeviceRecord, _sanitize_placeholder, silent_device_issue_id

_LOGGER = logging.getLogger(__name__)

STALE_VALVE_POLL_GUARD = timedelta(minutes=5)


class _AbsentStatus(dict):
    """Marker meaning "no status response arrived for this hub" (D-05/D-06).

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
    is no payload to begin with (a silent sub-device, D-15): decode_generic
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
    """Build the per-sensor metadata dict that goes into the coordinator's sensors output."""
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
        # unrelated to the D-05 absent-vs-omitted distinction, which concerns
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

            if hubs:
                status_by_mid = await RainPointCoordinator._fetch_status_by_mid(self, hubs)

            for hub in hubs:
                mid = hub["mid"]
                # STATUS_ABSENT is an unreachable safety net once _fetch_status_by_mid
                # covers every hub mid (D-05): a mid genuinely missing here would mean
                # its status was never obtained this poll, not that it arrived empty.
                status = status_by_mid.get(mid, STATUS_ABSENT)
                if isinstance(status, _AbsentStatus):
                    absent_hubs.append(hub)
                decoded_sensors.update(RainPointCoordinator._decode_hub_subdevices(self, hub, status))

            # A poll that returned no hubs at all, for an installation that had
            # some a moment ago, is a device-list outage rather than evidence
            # that every device left. Pruning and reconciling against it would
            # wipe each debounce counter and clear each still-valid issue, then
            # re-raise it once the list came back: the same clear-then-reraise
            # cycle the absent-hub signal above exists to prevent, entering
            # through the device-list door instead of the status door. Skipping
            # both is safe in the direction that matters, since a poll with no
            # hubs also decodes no sensors and so can never raise anything.
            if hubs or not self._silent_poll_counts:
                RainPointCoordinator._prune_silent_state(self, hubs)
                RainPointCoordinator._sync_silent_device_issues(self, decoded_sensors, absent_hubs)
            else:
                _LOGGER.warning(
                    "Device list came back empty while %d sub-device(s) were being tracked; "
                    "treating it as an outage and leaving not-reporting state untouched",
                    len(self._silent_poll_counts),
                )

            _LOGGER.info("Coordinator update complete: %d hubs, %d sensors", len(hubs), len(decoded_sensors))
            _LOGGER.debug(debug_with_version("Final data: hubs=%s, sensors=%s"), hubs, list(decoded_sensors.keys()))

            return {
                "hubs": hubs,
                "status": status_by_mid,
                "sensors": decoded_sensors,
            }
        except RainPointApiError as err:
            raise UpdateFailed(f"RainPoint API error: {err}") from err
        except Exception as err:
            _LOGGER.exception("Unexpected RainPoint error while refreshing")
            raise UpdateFailed(f"Unexpected RainPoint error: {err}") from err

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
            # (D-05). Filling it in here is what makes the absent-vs-omitted
            # split correct for the exact case that motivated this phase: a
            # hub whose status came back but never named one of its addrs.
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
                # silent entries for any of its children (D-06).
                status_by_mid[mid] = STATUS_ABSENT
        return status_by_mid

    def _notify_unknown_model(
        self, model: str | None, mid: int, addr: int, raw_value: str, model_code: int | str | None = None
    ) -> None:
        """Log the unsupported-sensor warning and fire a once-per-variant persistent notification.

        Reports modelCode alongside the model string because the two are not
        equivalent: the vendor catalog contains model strings that map to more
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
        gets a debounced "silent" entry (D-06/D-09), which a loop driven from
        the status response could never produce -- that asymmetry was the
        actual defect. A status entry whose sid resolves to an addr the hub
        does not list is still dropped, and an unresolvable sid is still
        ignored, exactly as before.

        An absent status (this hub's status could not be obtained this poll)
        contributes nothing at all for any of its children (D-06): that is an
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
                # A real reading resets the debounce counter (D-07).
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
        # status_entry={} per D-09/D-11: every downstream raw_status reader
        # already tolerates a missing "value"/"time" pair.
        return _build_sensor_entry(hub, sub, mid, addr, {}, decoded)

    def _prune_silent_state(self, hubs: list[dict]) -> None:
        """Drop any debounce counter for an addr no hub currently lists.

        Runs every poll so a device that leaves subDevices (unpaired, removed)
        cannot accumulate a counter forever (T-15-03).
        """
        live_keys = {_sensor_key(hub["hid"], hub["mid"], sd["addr"]) for hub in hubs for sd in hub.get("subDevices", [])}
        self._silent_poll_counts = {key: count for key, count in self._silent_poll_counts.items() if key in live_keys}

    def _sync_silent_device_issues(self, decoded_sensors: dict[str, dict], absent_hubs: list[dict]) -> None:
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
        """
        unreachable_ids = {
            silent_device_issue_id(hub["hid"], hub["mid"], sd["addr"]) for hub in absent_hubs for sd in hub.get("subDevices", [])
        }
        records = [
            SilentDeviceRecord(
                hid=entry["hid"],
                mid=entry["mid"],
                addr=entry["addr"],
                model=entry.get("model"),
                hub_name=entry.get("hub_name"),
                missed_polls=(entry.get("data") or {}).get("missed_polls", 0),
                silent=(entry.get("data") or {}).get("type") == SILENT_DATA_TYPE,
            )
            for entry in decoded_sensors.values()
        ]
        self._silent_issues.async_sync(records, unreachable_ids=unreachable_ids)
