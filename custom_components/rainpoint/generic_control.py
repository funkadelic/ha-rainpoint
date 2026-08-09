"""Opt-in, model-agnostic control entity factory for catalog-recognized models.

This is the write path: unlike generic_entities.py's read-only sensors, an
entity built here calls control_work_mode and actuates real hardware. It
reads its semantics from the single curated identity table in
generic_entities.py (``_IDENTITY_SPECS``) rather than keeping a second,
control-only table of its own -- two tables for one identity would eventually
disagree. It never writes a state it has not read back: no command response
is ever decoded into coordinator data, and the entity's reported state only
ever changes when the coordinator's own poll (or the delayed confirming
refresh this module schedules) supplies a new run-state reading.

Control eligibility is gated far more narrowly than the sensor path: a
variant must declare an allowlisted control identity, have no hand-written
decoder, not be in the committed override list, and have a command port that
resolves unambiguously from the catalog. Anything that does not meet every
condition is disabled by construction, never by an explicit deny entry.

With both options on, the run-state reading is exposed twice for the same
zone: once as the read-only generic sensor and once as this module's control
entity, whose state is a readback of that same record. That is intended, and
it does depart from the trusted path, where a model gets zone state either on
a valve entity or as a sensor but never both. That either/or comes from one
per-model factory deciding both platforms at once, which is not available
here: the two generic options are gated independently, so suppressing the
sensor whenever control is on would make the sensor platform read the control
option, and would leave a row behind when that option flips, since each
namespace sweep is keyed to its own option. The overlap also earns its keep
while these mappings are unverified, because the raw reading sits beside the
actuator that claims to drive it, which is where a disagreement shows up.
"""

from __future__ import annotations

import logging
import math
import re
from dataclasses import dataclass
from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.components.valve import ValveEntity, ValveEntityFeature
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .api import (
    RainPointApiError,
    get_catalog_entry,
    get_catalog_port_number,
    is_ascii_declined,
    is_hand_written_model,
)
from .api.product_catalog import UNCODED_VARIANT
from .const import (
    DOMAIN,
    GENERIC_CONTROL_DURATION_SUFFIX,
    GENERIC_CONTROL_ISSUE_ID_PREFIX,
    GENERIC_CONTROL_MARKER_ICON,
    GENERIC_CONTROL_OVERRIDE_DISABLED,
    GENERIC_CONTROL_REFRESH_DELAY_SECONDS,
    GENERIC_CONTROL_UNIQUE_ID_MARKER,
    UNIQUE_ID_PREFIX,
)
from .coordinator import RainPointCoordinator
from .device import build_sub_device_info
from .generic_entities import (
    _IDENTITY_SPECS,
    _matching_field,
    _unresolved_variant_reason,
    _usable_port,
    has_declared_width,
)
from .repairs import _sanitize_placeholder

_LOGGER = logging.getLogger(__name__)

# Fallback used when the registry-resolved per-zone duration companion (see
# RainPointGenericValve._get_configured_duration_seconds) is not yet
# available -- mirrors valve.py's own DEFAULT_DURATION_SECONDS.
DEFAULT_CONTROL_DURATION_SECONDS = 600  # 10 minutes

# The narrow set of catalog control identities this factory ever considers.
# Never widened by inference -- this is a closed literal set in source, so
# "generic control is never a wildcard" is a structural property provable by
# reading it, not a runtime edge case.
#
# CTL_BT_WATER is deliberately absent, and the exclusion is proven rather
# than suspected. Every entity built here commands through
# client.control_work_mode, and a hardware trial on 2026-07-29 (via
# scripts/trial_control_work_mode.py) showed a hub-paired HTV210B rejects
# that call with response code 3 for both open and close, in the same
# session where the identical call shape was accepted by an HTV245FRF on
# the same hub. The RainPoint app can command the valve through the cloud with
# Bluetooth off, so a cloud path exists, but it is not this endpoint. The
# models declaring this identity (HTV102B, HTV107B, HTV124LT, HTV210B,
# HTV224B) carry no CTL_WATER datapoint at all, so admitting the identity
# would build a valve entity whose every command fails.
#
# A 2026-08-05 traffic capture identified the endpoint and payload the app
# actually uses: a distinct path, controlWorkModeDP, plus three fields the
# plain call never sends (productKey, deviceName, dpCode), no duration
# field, and the run duration carried as a little-endian hex string
# instead. The condition this comment used to name is met, and the
# decision still goes the same way.
#
# HTV210B is the one device with a verified call against that endpoint, and
# it already carries a hand-written decoder, so is_hand_written_model keeps
# it out of this generic path structurally, regardless of this allowlist.
# Readmitting the identity buys the verified device nothing. What it would
# cost: the other four models declaring this identity, HTV102B, HTV107B,
# HTV124LT and HTV224B, are unowned and undecoded. Readmitting the identity
# would hand each of them a control entity commanding over an endpoint
# verified on exactly one unit, and would need a second write path in this
# module plus a disclosure-copy update for the failure mode where a
# command reaches the cloud and nothing moves, which the current copy does
# not cover.
#
# The exclusion is load-bearing, not inert: every variant in the committed
# catalog declaring this identity also declares STA_WKSTATE, the run-state
# datapoint this module reads to decide eligibility, so the control gate
# would pass for all five of them if the identity were readmitted. Removing
# this exclusion would immediately build five entities, not zero.
#
# Reverses if someone acquires one of the other four and verifies the
# endpoint on that hardware.
CONTROL_IDENTITY_ALLOWLIST = frozenset({"CTL_WATER", "CTL_SOCK"})
VALVE_CONTROL_IDENTITIES = frozenset({"CTL_WATER"})
SWITCH_CONTROL_IDENTITIES = frozenset({"CTL_SOCK"})

# The single curated row (generic_entities._IDENTITY_SPECS) both the sensor
# and control paths read the open/closed reading from.
RUN_STATE_IDENTITY = "STA_WKSTATE"

# Matches "code" followed by whitespace and an optionally negative run of
# digits, e.g. "controlWorkMode failed: code 5". Anchored to the word "code"
# rather than to a fixed position, since the sentence prefix is not part of
# any contract this module owns.
_RESPONSE_CODE_PATTERN = re.compile(r"code\s+(-?\d+)")

# Length cap for the error text on the command-failed card. Deliberately
# larger than _sanitize_placeholder's default: the error is the only
# diagnostic that card carries, and 64 characters cuts a real one mid-message,
# while an uncapped cloud response body would swamp the card.
_ERROR_PLACEHOLDER_LIMIT = 200


def _response_code_from_error(exc: Exception) -> str:
    """Extract the numeric controlWorkMode response code from the client's error text.

    Extracting from the message rather than from an attribute on the
    exception is deliberate: api/client.py is reused unchanged, so the
    message is the only place the code is exposed. Do not
    "fix" this by adding a code attribute to RainPointApiError -- that would
    be an unrequested change to the reused client.

    Returns the literal string "unknown" when no code can be found (e.g. the
    HTTP-status branch of control_work_mode, which never mentions "code" at
    all), rather than raising or omitting the segment.
    """
    match = _RESPONSE_CODE_PATTERN.search(str(exc))
    return match.group(1) if match else "unknown"


def _create_command_failed_issue(hass: Any, model: str | None, exc: Exception) -> None:
    """Raise the one-shot repair issue for a failed generic control command.

    The issue id (built from the prefix, the model, and the extracted
    response code) is itself the dedup key: two failures with the same model
    and the same code converge on the same id, so a retry loop -- or a
    multi-zone device where several entities hit the same failure -- raises
    one issue for the whole device and code rather than one per attempt or
    per zone, mirroring how coordinator._notify_unknown_model dedupes on its
    notification id.

    Guarded by its own narrow try/except so a failing diagnostic surface can
    never suppress the real error the caller re-raises immediately after
    calling this.
    """
    model_label = model or "unknown"
    code = _response_code_from_error(exc)
    issue_id = f"{GENERIC_CONTROL_ISSUE_ID_PREFIX}_{model_label}_{code}"
    try:
        ir.async_create_issue(
            hass,
            DOMAIN,
            issue_id,
            is_fixable=False,
            severity=ir.IssueSeverity.ERROR,
            translation_key=GENERIC_CONTROL_ISSUE_ID_PREFIX,
            # Both values reach a Repairs card that Home Assistant renders as
            # Markdown, and both are unvalidated: model comes from the cloud
            # catalog and the error text can quote a cloud response body. Same
            # treatment as the not-reporting card. The error gets a longer cap
            # than the placeholder default because it is the only diagnostic
            # the card carries, and 64 characters truncates a real one.
            translation_placeholders={
                "model": _sanitize_placeholder(model_label),
                "error": _sanitize_placeholder(exc, limit=_ERROR_PLACEHOLDER_LIMIT),
            },
        )
    except Exception as issue_exc:
        _LOGGER.debug(
            "Failed to create the repair issue for a failed generic control command (model=%s): %s",
            model_label,
            issue_exc,
        )


@dataclass(frozen=True)
class ControlDatapoint:
    """One resolved, allowlisted control datapoint ready to become an entity.

    ``dp_port`` (the catalog's declared port) and ``command_port`` (the
    resolved, 1-based value sent to the client) are kept as two separate
    fields on purpose: they differ for a variant declaring port zero, and
    collapsing them into one value would either break the run-state field
    match (which keys on the catalog's dp_port) or send the command to the
    wrong zone (which must use the resolved command_port).
    """

    identity: str
    dp_port: int
    command_port: int
    dp_code: Any
    dp_data_type: Any


@dataclass(frozen=True)
class ControlGateResult:
    """The single verdict evaluate_control_gate produces for one model variant.

    ``datapoints`` is the ordered list of admitted control datapoints -
    always empty when the gate fails. ``blocked_by`` accumulates every
    independent reason the variant (or one of its datapoints) was rejected,
    not just the first one encountered.
    """

    datapoints: tuple[ControlDatapoint, ...]
    blocked_by: tuple[str, ...]
    port_number: int | None

    @property
    def passed(self) -> bool:
        """True only when at least one control datapoint was admitted."""
        return bool(self.datapoints)


def resolve_control_port(dp_port: Any, port_number: Any) -> int | None:
    """Return the 1-based command port for a declared dpPort, or None to refuse it.

    A dpPort of 1 or greater resolves to itself, unchanged. A dpPort of
    exactly 0 resolves to command port 1 only when the variant declares
    exactly one port -- the single case where "port zero" unambiguously means
    the one zone that exists. Every other shape (a bool, a non-int, a
    negative int, or a zero on a multi-zone variant) refuses the datapoint:
    zero is not a defined value for control_work_mode's documented 1-based
    port argument, and guessing on a multi-zone device would actuate the
    wrong zone.
    """
    if not _usable_port(dp_port):
        return None
    if dp_port >= 1:
        return dp_port
    if dp_port == 0 and port_number == 1:
        return 1
    return None


def _override_key(model: str | None, model_code: int | str | None) -> tuple[str | None, str]:
    """Build the (model, modelCode-as-string) lookup key, falling back to the uncoded bucket."""
    code = model_code if model_code is not None else UNCODED_VARIANT
    return (model, str(code))


def _override_reason(model: str | None, model_code: int | str | None) -> str | None:
    """Rule: a committed, maintainer-authored force-disable for this exact variant."""
    if _override_key(model, model_code) in GENERIC_CONTROL_OVERRIDE_DISABLED:
        return f"generic control for this variant of {model} has been force-disabled by the maintainer"
    return None


def _resolve_datapoint(entry: dict, run_state_entries: list[dict], port_number: Any) -> ControlDatapoint | str:
    """Resolve one control dp entry to a ControlDatapoint, or a reason it is refused.

    Two independent conditions, checked in order, each with its own reason:
    the datapoint must pair to exactly one run-state datapoint declaring the
    same port (checked first -- across all 34 committed allowlist variants
    the set of control ports is exactly the set of run-state ports, so this
    is safe and load-bearing, not a heuristic), and its command port must
    resolve. Refusing on an ambiguous or absent pairing before ever looking
    at the port is what stops a plausible-looking but unconfirmable port
    from ever reaching the client -- confirming a command by re-polling is
    meaningless for a datapoint whose state can never be read back.
    """
    identity = entry.get("identity")
    dp_port = entry.get("dpPort")
    matches = [rs for rs in run_state_entries if rs.get("dpPort") == dp_port]
    if len(matches) != 1:
        return (
            f"{identity} on port {dp_port!r} has {len(matches)} matching run-state readings "
            "instead of exactly one, so its state can never be confirmed"
        )
    command_port = resolve_control_port(dp_port, port_number)
    if command_port is None:
        return f"{identity} on port {dp_port!r} has no resolvable command port, so that zone is refused"
    # identity and dp_port are typed str and int on the dataclass, and both are
    # invariants this function has already established rather than assumptions:
    # the caller admits an entry only when its identity is in the allowlist (a
    # set of literal strings), and the run-state pairing plus resolve_control_port
    # above both reject a dpPort that is not a usable int. Left unguarded for the
    # same reason the sensor path leaves its own port unguarded.
    return ControlDatapoint(
        identity=identity,
        dp_port=dp_port,
        command_port=command_port,
        dp_code=entry.get("dpCode"),
        dp_data_type=entry.get("dpDataType"),
    )


def _evaluate_control_gate(model: str | None, model_code: int | str | None) -> ControlGateResult:
    """Body of evaluate_control_gate, without the never-raise wrapper.

    Kept separate so the outer wrapper's except clause is the single place
    that decides what a raising catalog lookup degrades to. Every branch
    below fails closed by construction: a model absent from the catalog, a
    variant declaring no allowlisted identity, and a datapoint whose port
    cannot be resolved all land on no entities with a distinct stated reason,
    never a default or fallback port.
    """
    # Load-bearing, not defence in depth: a hand-written valve model must
    # never reach this factory, whatever the catalog says about it.
    if is_hand_written_model(model):
        return ControlGateResult(
            datapoints=(),
            blocked_by=("this model already has a hand-written decoder, so it never uses generic control",),
            port_number=None,
        )

    # Terminal: checked before the catalog is even consulted, so one
    # misrouted variant can be force-disabled without depending on what the
    # catalog says about it.
    override_reason = _override_reason(model, model_code)
    if override_reason is not None:
        return ControlGateResult(
            datapoints=(),
            blocked_by=(override_reason,),
            port_number=get_catalog_port_number(model, model_code),
        )

    raw_entry = get_catalog_entry(model, model_code)
    if raw_entry is None:
        return ControlGateResult(
            datapoints=(),
            blocked_by=(_unresolved_variant_reason(model, model_code),),
            port_number=None,
        )
    if not raw_entry:
        return ControlGateResult(
            datapoints=(),
            blocked_by=(f"{model} is in the product catalog, but the catalog lists no readings for it",),
            port_number=get_catalog_port_number(model, model_code),
        )

    port_number = get_catalog_port_number(model, model_code)
    control_entries = [
        entry for entry in raw_entry if isinstance(entry, dict) and entry.get("identity") in CONTROL_IDENTITY_ALLOWLIST
    ]
    if not control_entries:
        return ControlGateResult(
            datapoints=(),
            blocked_by=(f"{model} declares no allowlisted control identity, so generic control does not apply",),
            port_number=port_number,
        )

    # Deliberately NOT the sensor gate's dpCode-uniqueness rule: that rule
    # exists because the sensor path resolves an entity to a port through an
    # ambiguous dpCode alone. This path resolves ports by pairing against the
    # run-state datapoints (below), and a real multi-zone valve hub commonly
    # reuses one dpCode for the same identity across its zones (see
    # HTV214FRF). Carrying the sensor rule over would refuse every multi-zone
    # valve hub, which is the hardware this path exists to serve.
    run_state_entries = [entry for entry in raw_entry if isinstance(entry, dict) and entry.get("identity") == RUN_STATE_IDENTITY]

    datapoints: list[ControlDatapoint] = []
    reasons: list[str] = []
    for entry in control_entries:
        resolved = _resolve_datapoint(entry, run_state_entries, port_number)
        if isinstance(resolved, ControlDatapoint):
            datapoints.append(resolved)
        else:
            reasons.append(resolved)

    ordered = tuple(sorted(datapoints, key=lambda dp: (dp.dp_port, dp.identity)))
    return ControlGateResult(datapoints=ordered, blocked_by=tuple(reasons), port_number=port_number)


def evaluate_control_gate(model: str | None, model_code: int | str | None = None) -> ControlGateResult:
    """Decide whether a model variant yields generic control entities, and why not.

    The single producer of the verdict, consumed by both
    build_generic_control_entities (the create-or-not decision) and
    describe_control_gate (the human-readable reason), so the two can never
    disagree. Never raises: any exception degrades to a fail-closed result.
    """
    try:
        return _evaluate_control_gate(model, model_code)
    except Exception as exc:
        _LOGGER.debug("evaluate_control_gate failed for model=%s model_code=%s: %s", model, model_code, exc)
        return ControlGateResult(
            datapoints=(),
            blocked_by=("the product catalog could not be read",),
            port_number=None,
        )


def describe_control_gate(model: str | None, model_code: int | str | None = None) -> dict:
    """Project evaluate_control_gate's result to the one key a control entity attribute needs.

    Holds no logic of its own and never raises. Named identically to the
    entity attribute a later plan surfaces it under, so that plan adopts it
    with a plain dict update and no remap step.
    """
    result = evaluate_control_gate(model, model_code)
    return {"generic_control_blocked_by": list(result.blocked_by)}


def count_generic_control_eligible_devices(coordinator_data: dict | None) -> tuple[int, int]:
    """Return (eligible, unsupported_total) across the devices in coordinator data.

    Mirrors generic_entities.count_generic_eligible_devices, against the
    control gate instead of the sensor gate, so the control toggle's options
    copy can state its own real effect. Never raises: malformed or absent
    coordinator data degrades to (0, 0).
    """
    eligible = 0
    unsupported = 0
    try:
        sensors = (coordinator_data or {}).get("sensors") or {}
        for info in sensors.values():
            data = (info or {}).get("data") or {}
            if data.get("type") != "unknown":
                continue
            unsupported += 1
            if evaluate_control_gate(info.get("model"), info.get("model_code")).passed:
                eligible += 1
    except Exception as exc:
        _LOGGER.debug("count_generic_control_eligible_devices failed: %s", exc)
        return (0, 0)
    return (eligible, unsupported)


def _build_generic_entities(
    coordinator,
    sensor_key: str,
    sensor_info: dict,
    base_slug: str,
    identity_set: frozenset[str],
    entity_cls: type,
) -> list:
    """Shared body: one gate evaluation, projected through one identity set and one entity class.

    Both platform-specific wrappers (build_generic_valve_entities,
    build_generic_switch_entities) call this with their own identity_set and
    entity_cls, so the gate is evaluated identically for both domains and can
    never disagree about which datapoints are admitted -- only which class
    they become. Never raises: a malformed catalog entry must not abort
    platform setup for the whole integration.
    """
    try:
        # A per-poll fact, not a static model property, so it stays here
        # rather than moving into evaluate_control_gate.
        data = sensor_info.get("data") or {}
        if data.get("type") != "unknown":
            return []

        model = sensor_info.get("model")
        model_code = sensor_info.get("model_code")
        result = evaluate_control_gate(model, model_code)
        if not result.passed:
            return []

        return [
            entity_cls(coordinator, sensor_key, sensor_info, base_slug, datapoint, result.port_number)
            for datapoint in result.datapoints
            if datapoint.identity in identity_set
        ]
    except Exception as exc:
        _LOGGER.debug("_build_generic_entities failed for sensor_key=%s: %s", sensor_key, exc)
        return []


def build_generic_valve_entities(coordinator, sensor_key: str, sensor_info: dict, base_slug: str) -> list:
    """Return the generic valve entities (CTL_WATER) for one sub-device, or []."""
    return _build_generic_entities(
        coordinator, sensor_key, sensor_info, base_slug, VALVE_CONTROL_IDENTITIES, RainPointGenericValve
    )


def build_generic_switch_entities(coordinator, sensor_key: str, sensor_info: dict, base_slug: str) -> list:
    """Return the generic switch entities (CTL_SOCK) for one sub-device, or [].

    Ships the allowlist's third identity literally rather than silently
    narrowing generic control to valves: a CTL_SOCK datapoint names a mains
    socket, not a valve, so it belongs on the switch platform.
    """
    return _build_generic_entities(
        coordinator, sensor_key, sensor_info, base_slug, SWITCH_CONTROL_IDENTITIES, RainPointGenericSwitch
    )


class RainPointGenericControlBase(CoordinatorEntity[RainPointCoordinator]):
    """Shared unique_id/name/device_info/run-state/command plumbing for a control entity.

    Semantics come from generic_entities._IDENTITY_SPECS -- the same curated
    table the read-only generic sensor path reads -- so the read and control
    paths can never disagree about what a raw reading means. Never
    optimistic: this base writes nothing into coordinator data, and the
    run-state property it exposes only ever reflects what the coordinator's
    own decoded data already says.
    """

    _attr_should_poll = False
    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator,
        sensor_key: str,
        sensor_info: dict,
        base_slug: str,
        datapoint: ControlDatapoint,
        port_number: int | None,
    ) -> None:
        super().__init__(coordinator)
        self._sensor_key = sensor_key
        self._sensor_info = sensor_info
        self._base_slug = base_slug
        self._datapoint = datapoint
        self._port_number = port_number
        self._refresh_cancel = None

        identity = datapoint.identity
        # Built from the allowlist member, never from the raw catalog
        # identity string, so a hostile or corrupt catalog identity can
        # never reach a unique_id.
        self._attr_unique_id = (
            f"{UNIQUE_ID_PREFIX}{base_slug}{GENERIC_CONTROL_UNIQUE_ID_MARKER}{identity.lower()}_p{datapoint.dp_port}"
        )

        zone = ""
        if port_number is not None and port_number > 1 and datapoint.dp_port >= 1:
            zone = f"Zone {datapoint.dp_port} "
        self._attr_name = f"{zone}{identity} (unverified)"

        # Assigned last so the marker always wins over any domain default icon.
        self._attr_icon = GENERIC_CONTROL_MARKER_ICON

    @property
    def device_info(self) -> DeviceInfo:
        return build_sub_device_info(self._sensor_info)

    @property
    def _run_state_open(self) -> bool | None:
        """Read the curated run-state identity back through the shared table's transform.

        Returns True/False only when the coordinator's own decoded data
        carries an unambiguous reading; None otherwise (including "no data
        yet" and "the poll has not annotated this port"). This is the only
        thing that ever changes what a control entity reports.
        """
        sensors = (self.coordinator.data or {}).get("sensors", {})
        info = sensors.get(self._sensor_key)
        if not info:
            return None
        data = info.get("data")
        if not data:
            return None
        generic = data.get("generic") or {}
        if is_ascii_declined(generic):
            # An ASCII-framed payload had its body declined by the decoder, so
            # nothing in this result was read from a positional field, and a
            # reading that was never taken must read as unknown rather than as
            # a confirmation. This does not rest on the run-state field being
            # absent from a header-only parse: that absence is true today and
            # would stop being true the moment anyone added a body field, and
            # this control path is exactly where that change would silently
            # become a false write confirmation.
            return None
        fields = generic.get("fields") or []
        field = _matching_field(fields, RUN_STATE_IDENTITY, self._datapoint.dp_port)
        if field is None:
            return None
        raw = field.get("value")
        if not isinstance(raw, int) or isinstance(raw, bool):
            return None
        spec = _IDENTITY_SPECS[RUN_STATE_IDENTITY]
        if not has_declared_width(spec, field):
            # Same refusal the sensor path applies, and it matters more here:
            # this reading is what confirms a command actuated hardware, so a
            # record at an unvalidated width must read as unknown rather than
            # as a confirmation.
            return None
        value = spec.transform(raw)
        if not isinstance(value, (int, float)):
            # Unreachable while the run-state row masks bit zero to 0.0 or 1.0.
            # Guarded because a curated row may now yield a non-numeric reading,
            # and a control entity must report unknown rather than raise inside
            # a state property.
            return None
        # The run-state transform yields exactly 0.0 or 1.0 (a bit-zero mask),
        # so isclose is exact here; it also keeps a scaled future transform
        # from tripping on float representation while preserving the three-way
        # open / closed / unknown result.
        if math.isclose(value, 1.0):
            return True
        if math.isclose(value, 0.0):
            return False
        return None

    async def _async_send_command(self, mode: int, duration: int) -> None:
        """Issue one control_work_mode call and schedule the confirming refresh.

        Never decodes the response and never touches coordinator data, which
        is the no-optimistic-state rule. The client method is reused
        unchanged; nothing here (or anywhere in this module) modifies it.

        A raised RainPointApiError propagates unchanged after raising the
        one-shot repair issue, so Home Assistant still reports the action as
        failed: the issue is additional, never a substitute, and
        the confirming refresh below is never reached on a failure -- there
        is nothing to confirm.
        """
        mid = self._sensor_info["mid"]
        addr = self._sensor_info["addr"]
        device_name = self._sensor_info.get("device_name") or ""
        product_key = self._sensor_info.get("product_key") or ""

        client = self.coordinator._client
        try:
            await client.control_work_mode(
                mid=mid,
                addr=addr,
                device_name=device_name,
                product_key=product_key,
                port=self._datapoint.command_port,
                mode=mode,
                duration=duration,
            )
        except RainPointApiError as exc:
            _create_command_failed_issue(self.hass, self._sensor_info.get("model"), exc)
            raise
        self._schedule_refresh()

    def _schedule_refresh(self) -> None:
        """Schedule the delayed confirming refresh, superseding any pending one.

        An immediate refresh risks a mid-actuation device reporting the old
        state and the entity visibly flipping back; the normal 120s poll
        alone would leave up to two minutes of stale state after a command.
        """
        if self._refresh_cancel is not None:
            self._refresh_cancel()
            self._refresh_cancel = None
        self._refresh_cancel = async_call_later(self.hass, GENERIC_CONTROL_REFRESH_DELAY_SECONDS, self._handle_refresh)

    async def _handle_refresh(self, _now) -> None:
        """Fire the confirming refresh; the normal 120s poll remains the backstop."""
        self._refresh_cancel = None
        await self.coordinator.async_request_refresh()

    async def async_will_remove_from_hass(self) -> None:
        """Cancel any pending confirming refresh so a removed entity leaves no timer behind."""
        if self._refresh_cancel is not None:
            self._refresh_cancel()
            self._refresh_cancel = None
        await super().async_will_remove_from_hass()


class RainPointGenericValve(RainPointGenericControlBase, ValveEntity):
    """Opt-in, unverified generic valve entity for one allowlisted control datapoint.

    Never optimistic: ``is_closed`` is read only from the shared
    base's run-state property, so a command's effect only ever becomes
    visible once the coordinator's own data confirms it.
    """

    _attr_reports_position = False
    _attr_supported_features = ValveEntityFeature.OPEN | ValveEntityFeature.CLOSE

    @property
    def is_closed(self) -> bool | None:
        state = self._run_state_open
        if state is None:
            return None
        return not state

    def _get_configured_duration_seconds(self) -> int:
        """Look up the companion generic duration number entity for this zone.

        Mirrors valve.py's identically-shaped method: resolves this
        datapoint's companion unique_id (its own unique_id plus the locked
        duration suffix) through the entity registry's entity-id resolver,
        insensitive to Home Assistant's auto-generated entity_id naming.
        Falls back to DEFAULT_CONTROL_DURATION_SECONDS with a debug log on
        every miss -- a registry lookup that finds nothing, a state that is
        not yet available, or a state that cannot be parsed as a number.
        """
        from homeassistant.helpers import entity_registry as er

        unique_id = f"{self._attr_unique_id}{GENERIC_CONTROL_DURATION_SUFFIX}"
        registry = er.async_get(self.hass)
        entity_id = registry.async_get_entity_id("number", DOMAIN, unique_id)
        if entity_id:
            state = self.hass.states.get(entity_id)
            if state is not None:
                try:
                    minutes = float(state.state)
                    return max(1, int(minutes * 60))
                except (ValueError, TypeError):
                    pass
        _LOGGER.debug(
            "Generic duration entity for unique_id=%s not found, falling back to default %ss",
            unique_id,
            DEFAULT_CONTROL_DURATION_SECONDS,
        )
        return DEFAULT_CONTROL_DURATION_SECONDS

    async def async_open_valve(self, **kwargs: Any) -> None:
        duration = int(kwargs["duration"]) if "duration" in kwargs else self._get_configured_duration_seconds()
        await self._async_send_command(mode=1, duration=duration)

    async def async_close_valve(self, **kwargs: Any) -> None:
        await self._async_send_command(mode=0, duration=0)


class RainPointGenericSwitch(RainPointGenericControlBase, SwitchEntity):
    """Opt-in, unverified generic switch entity for one allowlisted CTL_SOCK control datapoint.

    CTL_SOCK is carried on the switch platform rather than silently
    collapsing into a valve entity, because it names a mains socket, not a
    valve. There is no existing
    per-device write-path switch in this repository to copy -- the two
    entities in switch.py are a hub-level broadcast switch and a debug
    switch, both structurally unrelated -- so this is built from the shared
    control base and the same coordinator-read-plus-actuate-through-client
    shape RainPointGenericValve already has.

    Never optimistic: ``is_on`` is read only from the shared base's
    run-state property, so a command's effect only ever becomes visible once
    the coordinator's own data confirms it.
    """

    @property
    def is_on(self) -> bool | None:
        return self._run_state_open

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Surface the fixed on-duration this switch always sends, for transparency.

        Not a configuration knob -- there is no companion number entity for
        CTL_SOCK and no way to override the value below -- just a documented
        fact so a user is not surprised if the connected device auto-offs.
        """
        return {"on_command_duration_seconds": DEFAULT_CONTROL_DURATION_SECONDS}

    async def async_turn_on(self, **kwargs: Any) -> None:
        # Same fixed duration valve.py's own DEFAULT_DURATION_SECONDS falls
        # back to for an unconfigured zone. Whether CTL_SOCK firmware treats
        # this as an auto-off timer the way a valve zone does is unverified;
        # see extra_state_attributes above for the value this exposes to the
        # user rather than silently guessing at the firmware's behavior.
        await self._async_send_command(mode=1, duration=DEFAULT_CONTROL_DURATION_SECONDS)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._async_send_command(mode=0, duration=0)
