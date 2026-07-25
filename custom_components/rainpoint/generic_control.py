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
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from homeassistant.components.valve import ValveEntity, ValveEntityFeature
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .api import get_catalog_entry, get_catalog_port_number, is_hand_written_model
from .api.product_catalog import UNCODED_VARIANT
from .const import (
    DOMAIN,
    GENERIC_CONTROL_MARKER_ICON,
    GENERIC_CONTROL_OVERRIDE_DISABLED,
    GENERIC_CONTROL_REFRESH_DELAY_SECONDS,
    GENERIC_CONTROL_UNIQUE_ID_MARKER,
    UNIQUE_ID_PREFIX,
)
from .generic_entities import (
    _IDENTITY_SPECS,
    _matching_field,
    _unresolved_variant_reason,
    _usable_port,
)

_LOGGER = logging.getLogger(__name__)

# Used only until the registry-resolved per-zone duration companion (a later
# plan) exists; mirrors valve.py's own DEFAULT_DURATION_SECONDS.
DEFAULT_CONTROL_DURATION_SECONDS = 600  # 10 minutes

# The narrow set of catalog control identities this factory ever considers.
# Never widened by inference -- this is a closed literal set in source, so
# "generic control is never a wildcard" is a structural property provable by
# reading it, not a runtime edge case.
CONTROL_IDENTITY_ALLOWLIST = frozenset({"CTL_WATER", "CTL_BT_WATER", "CTL_SOCK"})
VALVE_CONTROL_IDENTITIES = frozenset({"CTL_WATER", "CTL_BT_WATER"})
SWITCH_CONTROL_IDENTITIES = frozenset({"CTL_SOCK"})

# The single curated row (generic_entities._IDENTITY_SPECS) both the sensor
# and control paths read the open/closed reading from.
RUN_STATE_IDENTITY = "STA_WKSTATE"


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
    from ever reaching the client -- GCTL-04's confirm-by-re-poll is
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
    # valve hub, which is the hardware this phase exists for.
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


def build_generic_control_entities(coordinator, sensor_key: str, sensor_info: dict, base_slug: str) -> list:
    """Return the generic control entities for one sub-device, or [] for every rejection path.

    Never raises: a malformed catalog entry must not abort valve platform
    setup for the whole integration.
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

        entities: list = []
        for datapoint in result.datapoints:
            if datapoint.identity in VALVE_CONTROL_IDENTITIES:
                entities.append(
                    RainPointGenericValve(coordinator, sensor_key, sensor_info, base_slug, datapoint, result.port_number)
                )
            elif datapoint.identity in SWITCH_CONTROL_IDENTITIES:
                # A later plan fills this branch in with a real switch
                # entity; every CTL_SOCK model in the committed catalog is
                # already hand-written, so this is expected to be inert.
                _LOGGER.debug(
                    "Skipping generic switch control for identity=%s sensor_key=%s: not implemented yet",
                    datapoint.identity,
                    sensor_key,
                )
        return entities
    except Exception as exc:
        _LOGGER.debug("build_generic_control_entities failed for sensor_key=%s: %s", sensor_key, exc)
        return []


class RainPointGenericControlBase(CoordinatorEntity):
    """Shared unique_id/name/device_info/run-state/command plumbing for a control entity.

    Semantics come from generic_entities._IDENTITY_SPECS -- the same curated
    table the read-only generic sensor path reads -- so the read and control
    paths can never disagree about what a raw reading means. Never
    optimistic: this base writes nothing into coordinator data, and the
    run-state property it exposes only ever reflects what the coordinator's
    own decoded data already says.
    """

    _attr_should_poll = False

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

        sub_name = sensor_info.get("sub_name") or "Device"
        zone = ""
        if port_number is not None and port_number > 1 and datapoint.dp_port >= 1:
            zone = f" Zone {datapoint.dp_port}"
        self._attr_name = f"{sub_name}{zone} (unverified)"

        # Assigned last so the marker always wins over any domain default icon.
        self._attr_icon = GENERIC_CONTROL_MARKER_ICON

    @property
    def device_info(self) -> dict[str, Any]:
        hid = self._sensor_info["hid"]
        mid = self._sensor_info["mid"]
        addr = self._sensor_info["addr"]
        sub_name = self._sensor_info.get("sub_name") or f"Device {addr}"
        model = self._sensor_info.get("model") or "Unknown"
        return {
            "identifiers": {(DOMAIN, f"{hid}_{mid}_{addr}")},
            "name": sub_name,
            "manufacturer": "RainPoint",
            "model": model,
        }

    @property
    def _run_state_open(self) -> bool | None:
        """Read the curated run-state identity back through the shared table's transform.

        Returns True/False only when the coordinator's own decoded data
        carries an unambiguous reading; None otherwise (including "no data
        yet" and "the poll has not annotated this port"). This is the only
        thing that ever changes what a control entity reports.
        """
        sensors = self.coordinator.data.get("sensors", {})
        info = sensors.get(self._sensor_key)
        if not info:
            return None
        data = info.get("data")
        if not data:
            return None
        generic = data.get("generic") or {}
        fields = generic.get("fields") or []
        field = _matching_field(fields, RUN_STATE_IDENTITY, self._datapoint.dp_port)
        if field is None:
            return None
        raw = field.get("value")
        if not isinstance(raw, int) or isinstance(raw, bool):
            return None
        value = _IDENTITY_SPECS[RUN_STATE_IDENTITY].transform(raw)
        if value == 1.0:
            return True
        if value == 0.0:
            return False
        return None

    async def _async_send_command(self, mode: int, duration: int) -> None:
        """Issue one control_work_mode call and schedule the confirming refresh.

        Never decodes the response and never touches coordinator data --
        D-14's no-optimistic-state rule. The client method is reused
        unchanged; nothing here (or anywhere in this module) modifies it.
        """
        mid = self._sensor_info["mid"]
        addr = self._sensor_info["addr"]
        device_name = self._sensor_info.get("device_name") or ""
        product_key = self._sensor_info.get("product_key") or ""

        client = self.coordinator._client
        await client.control_work_mode(
            mid=mid,
            addr=addr,
            device_name=device_name,
            product_key=product_key,
            port=self._datapoint.command_port,
            mode=mode,
            duration=duration,
        )
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


class RainPointGenericValve(RainPointGenericControlBase, ValveEntity):
    """Opt-in, unverified generic valve entity for one allowlisted control datapoint.

    Never optimistic (D-14): ``is_closed`` is read only from the shared
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

    async def async_open_valve(self, **kwargs: Any) -> None:
        duration = int(kwargs["duration"]) if "duration" in kwargs else DEFAULT_CONTROL_DURATION_SECONDS
        await self._async_send_command(mode=1, duration=duration)

    async def async_close_valve(self, **kwargs: Any) -> None:
        await self._async_send_command(mode=0, duration=0)
