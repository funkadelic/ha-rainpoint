"""Opt-in, model-agnostic sensor entity factory for catalog-recognized models.

Turns the catalog-enriched ``decode_generic`` output into read-only, visibly
unverified sensor entities for models that have no hand-written decoder but
whose every declared status datapoint is backed by a curated identity row in
``_IDENTITY_SPECS``. Nothing here is inferred from the catalog's raw
``dpDataType``: every row in the table exists only because an existing
hand-written decoder proves its unit and scaling.

``_attr_state_class`` is intentionally ``None`` on every curated row and is
always assigned (never conditional): an explicitly unverified reading must
not enter Home Assistant long-term statistics, which a later correction to
the table cannot retroactively fix. Recent-state history and graphs are
unaffected.

The curated table carries no battery row. The hand-written battery
percentage is derived from a sixteen-bit composite status code read at fixed
trailing offsets and mapped through a lookup table, not from the single-byte
battery datapoint the catalog declares, so no raw-to-percent mapping is
proven for it.

The table carries no humidity row either. The only hand-written decoder that
reports a humidity percentage parses a decimal ASCII token, which is
evidence about the end value but not about the scale of the single raw byte
the catalog declares for that identity; the byte-level temperature/humidity
decoders extract signal strength only and never decode humidity at all. A
raw-byte-to-percent mapping is therefore unproven, and a systematic scale
error would land inside a plausible zero-to-one-hundred range where nothing
downstream would catch it.

Adding either row later is additive and only widens which models pass the
gate.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.sensor import SensorDeviceClass

from .api import (
    _f10_to_c,
    get_catalog_entry,
    get_catalog_port_number,
    is_hand_written_model,
)
from .const import GENERIC_UNIQUE_ID_MARKER, UNIQUE_ID_PREFIX
from .sensor import RainPointSensorBase

_LOGGER = logging.getLogger(__name__)

# The gate considers status identities only; control identities are reserved
# for a later, control-focused phase and must never gate a model out.
_STATUS_IDENTITY_PREFIX = "STA_"

GENERIC_MARKER_ICON = "mdi:flask-outline"


@dataclass(frozen=True)
class GenericSensorSpec:
    """Curated Home Assistant semantics for one catalog status identity."""

    label: str
    device_class: Any
    unit: str
    state_class: Any
    transform: Callable[[int], float | None]
    valid_range: tuple[float, float]
    precision: int


def _rssi_dbm(raw: int) -> float | None:
    """Reinterpret an unsigned byte as a signed int8 dBm reading."""
    value = raw - 256 if raw >= 128 else raw
    return float(value)


def _temperature_c(raw: int) -> float | None:
    """Convert a raw Fahrenheit-times-ten reading to Celsius."""
    return _f10_to_c(raw)


# Evidence-backed only: a row exists here only because an existing
# hand-written decoder proves both its unit and its scaling, on the same wire
# format the generic decode path reads. Nothing is inferred from the
# catalog's dpDataType. An identity whose only citable decoder works on a
# different encoding stays out of the table: see the humidity note in the
# module docstring above. Exactly one evidence marker per row - the
# absent-row notes in that docstring are deliberately worded without one, so
# the marker count and the row count stay equal.
_IDENTITY_SPECS: dict[str, GenericSensorSpec] = {
    "STA_RSSI": GenericSensorSpec(
        label="Signal Strength",
        device_class=SensorDeviceClass.SIGNAL_STRENGTH,
        unit="dBm",
        state_class=None,
        transform=_rssi_dbm,
        valid_range=(-120.0, -1.0),
        precision=0,
        # Evidence: api/validators.py:42-45 (_extract_rssi) reinterprets the
        # raw byte as a signed int8, and api/decoders.py:95 discards
        # non-negative values rather than reporting them.
    ),
    "STA_TEM": GenericSensorSpec(
        label="Temperature",
        device_class=SensorDeviceClass.TEMPERATURE,
        unit="°C",
        state_class=None,
        transform=_temperature_c,
        valid_range=(-40.0, 80.0),
        precision=1,
        # Evidence: api/utils.py:93-95 (_f10_to_c) treats the raw value as
        # Fahrenheit times ten, and api/decoders.py:582 and api/decoders.py:734
        # both show that scaling against real captured values.
    ),
}


def _filter_status_entries(dp_list: list) -> list[dict]:
    """Return only the well-formed STA_-prefixed dict entries from a catalog dp list."""
    declared: list[dict] = []
    for entry in dp_list:
        if not isinstance(entry, dict):
            continue
        identity = entry.get("identity")
        if not isinstance(identity, str) or not identity.startswith(_STATUS_IDENTITY_PREFIX):
            continue
        declared.append(entry)
    return declared


def _matching_field(fields: list[dict], identity: str, dp_port: int) -> dict | None:
    """Return the single decoded field matching (identity, dp_port), or None.

    Refuses to guess on zero or more than one match, the same way the catalog
    datapoint matcher this factory sits on top of already refuses on an
    ambiguous key.
    """
    matches = [f for f in fields if f.get("name") == identity and (f.get("catalog") or {}).get("dp_port") == dp_port]
    if len(matches) == 1:
        return matches[0]
    return None


@dataclass(frozen=True)
class GenericGateResult:
    """The single verdict evaluate_generic_gate produces for one model variant.

    ``datapoints`` is the ordered list of declared status datapoints that
    become entities - always empty when the gate fails, so a caller never
    needs to also check ``passed`` before iterating it.

    ``blocked_by`` is every independent reason the variant was rejected, not
    just the first one encountered. A variant can fail more than one rule at
    once (for example: it reuses a datapoint code across zones AND most of
    its readings have no curated definition), and fixing only the first
    reason reported would not, by itself, produce entities. An empty tuple
    means the gate passed.
    """

    datapoints: list[dict]
    unmapped_identities: tuple[str, ...]
    blocked_by: tuple[str, ...]
    port_number: int | None

    @property
    def passed(self) -> bool:
        """True only when the gate produced at least one entity's worth of datapoints."""
        return bool(self.datapoints)


def _evaluate_generic_gate(model: str | None, model_code: int | str | None) -> GenericGateResult:
    """Body of evaluate_generic_gate, without the never-raise wrapper.

    Kept separate so the outer wrapper's except clause is the single place
    that decides what a raising catalog lookup degrades to.

    Three checks below are terminal, single-reason early returns because each
    genuinely precludes analysing the dp list any further: a hand-written
    decoder means this factory is never consulted at all, an uncatalogued
    model has no dp list to analyse, and a variant with no status datapoints
    has nothing to check the other rules against. The remaining four checks
    are collected: each is evaluated over the full dp list and every
    violated rule contributes one reason, so a variant that fails several
    independent rules at once reports all of them together instead of
    stopping at the first.
    """
    # Load-bearing, not defence in depth: the sensor model-factory map in
    # sensor.py is a strict subset of the hand-written set, because every
    # hand-written valve model gets its entities from the valve and number
    # platforms and therefore has no entry in that map. Without this check a
    # hand-written valve model would reach this factory.
    if is_hand_written_model(model):
        return GenericGateResult(
            datapoints=[],
            unmapped_identities=(),
            blocked_by=("this model already has a hand-written decoder, so it never uses generic entities",),
            port_number=None,
        )

    raw_entry = get_catalog_entry(model, model_code)
    if not raw_entry:
        return GenericGateResult(
            datapoints=[],
            unmapped_identities=(),
            blocked_by=(f"{model} is not in the product catalog, so nothing is known about what it reports",),
            port_number=None,
        )

    dp_entries = _filter_status_entries(raw_entry)
    port_number = get_catalog_port_number(model, model_code)
    if not dp_entries:
        # A control-only variant hits this branch too. Control identities are
        # reserved for a later phase and never gate a model out, so this is
        # never reported as an unmapped-identity problem.
        return GenericGateResult(
            datapoints=[],
            unmapped_identities=(),
            blocked_by=(f"{model} does not report any readings in the catalog, so there is nothing to expose",),
            port_number=port_number,
        )

    reasons: list[str] = []

    # Rule 1: any dp entry with a missing/unusable dpPort fails the whole
    # model's gate rather than being skipped, so no two entities can ever
    # contend for one unique_id.
    bad_port_identities = sorted(
        {
            str(entry.get("identity"))
            for entry in dp_entries
            if not isinstance(entry.get("dpPort"), int) or isinstance(entry.get("dpPort"), bool)
        }
    )
    if bad_port_identities:
        reasons.append(
            "these readings don't declare a usable port number, so they can't be turned into entities: "
            + ", ".join(bad_port_identities)
        )

    # Rule 2: two entries sharing the same (identity, dpPort) fail the whole
    # model's gate, since an entity built over either one could not tell
    # which of the two values it should show. dpPort may be a non-int here
    # (rule 1 above does not stop this rule from running on the same data),
    # but a tuple key works regardless of the port's type.
    seen_keys: set[tuple[str | None, Any]] = set()
    duplicate_keys: set[tuple[str | None, Any]] = set()
    for entry in dp_entries:
        key = (entry.get("identity"), entry.get("dpPort"))
        if key in seen_keys:
            duplicate_keys.add(key)
        seen_keys.add(key)
    if duplicate_keys:
        formatted_pairs = ", ".join(
            f"{identity} on port {dp_port}"
            for identity, dp_port in sorted(duplicate_keys, key=lambda pair: (str(pair[0]), str(pair[1])))
        )
        reasons.append(
            "the catalog declares the same reading more than once for the same port, so its value would be "
            "ambiguous: " + formatted_pairs
        )

    # Rule 3: a dpCode shared by two entries anywhere in the variant's dp
    # list fails the whole model's gate. The runtime catalog matcher keys on
    # dpCode alone and refuses to annotate a field whose dpCode is ambiguous,
    # so an entity built over one of those entries would never resolve a
    # value and would sit at None forever. The check spans the full dp list,
    # not just the status entries, because the matcher searches the full
    # list too: a control entry sharing a status entry's dpCode makes that
    # status entry just as unresolvable. Multi-zone variants that repeat one
    # dpCode across ports are the common shape here, so this rejects real
    # catalog models by design rather than as an edge case.
    code_counts: dict[Any, int] = {}
    code_identities: dict[Any, set[str]] = {}
    for entry in raw_entry:
        if not isinstance(entry, dict):
            continue
        dp_code = entry.get("dpCode")
        code_counts[dp_code] = code_counts.get(dp_code, 0) + 1
        code_identities.setdefault(dp_code, set()).add(str(entry.get("identity")))
    # Integer codes sort numerically so the message reads 1, 2, 15 rather than
    # the 1, 15, 2 a plain string sort would produce; anything non-integer the
    # catalog might carry sorts after them by its text form, which keeps the
    # order total without assuming the codes are always integers.
    duplicate_codes = sorted(
        (code for code, count in code_counts.items() if count > 1),
        key=lambda code: (0, code, "") if isinstance(code, int) and not isinstance(code, bool) else (1, 0, str(code)),
    )
    if duplicate_codes:
        formatted_codes = ", ".join(f"dpCode {code} ({', '.join(sorted(code_identities[code]))})" for code in duplicate_codes)
        reasons.append(
            "the catalog reuses one datapoint code for more than one reading, so those readings can't be told "
            "apart: " + formatted_codes
        )

    # Rule 4: declared status identities with no curated row in
    # _IDENTITY_SPECS. unmapped_identities is reported as its own attribute
    # regardless of outcome (see describe_generic_gate), so this reason is a
    # summary rather than a repeat of that list.
    uncurated = {entry.get("identity") for entry in dp_entries if entry.get("identity") not in _IDENTITY_SPECS}
    unmapped_identities = tuple(sorted(uncurated))
    if unmapped_identities:
        total_identities = len({entry.get("identity") for entry in dp_entries})
        reasons.append(
            f"{len(unmapped_identities)} of this device's {total_identities} status readings have no verified "
            "definition yet, so they can't be turned into entities (see the unmapped identities list)"
        )

    if reasons:
        return GenericGateResult(
            datapoints=[],
            unmapped_identities=unmapped_identities,
            blocked_by=tuple(reasons),
            port_number=port_number,
        )

    # Safe only because reaching here means the bad-dpPort check above found
    # nothing: every dpPort is a plain int, so sorting on it can't raise.
    ordered = sorted(dp_entries, key=lambda entry: (entry.get("dpPort"), entry.get("identity")))
    return GenericGateResult(datapoints=ordered, unmapped_identities=(), blocked_by=(), port_number=port_number)


def evaluate_generic_gate(model: str | None, model_code: int | str | None = None) -> GenericGateResult:
    """Decide whether a model variant yields generic entities, and why not.

    The single producer of the verdict, consumed by both
    build_generic_entities (the create-or-not decision) and
    describe_generic_gate (the human-readable reason), so the two can never
    disagree. Never raises: any exception degrades to a fail-closed result.
    """
    try:
        return _evaluate_generic_gate(model, model_code)
    except Exception as exc:
        _LOGGER.debug("evaluate_generic_gate failed for model=%s model_code=%s: %s", model, model_code, exc)
        return GenericGateResult(
            datapoints=[],
            unmapped_identities=(),
            blocked_by=("the product catalog could not be read",),
            port_number=None,
        )


def count_generic_eligible_devices(coordinator_data: dict | None) -> tuple[int, int]:
    """Return (eligible, unsupported_total) across the devices in coordinator data.

    Only devices the trusted decoders could not decode are counted, since
    those are the only ones the generic path ever considers. ``eligible`` is
    how many of them would actually produce entities if the option were
    turned on, so the options form can state the real effect of the toggle
    instead of implying every unsupported device benefits.

    Never raises: malformed or absent coordinator data degrades to (0, 0),
    which reads on the form as "this adds nothing", the same conservative
    answer the gate itself gives when it cannot tell.
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
            if evaluate_generic_gate(info.get("model"), info.get("model_code")).passed:
                eligible += 1
    except Exception as exc:
        _LOGGER.debug("count_generic_eligible_devices failed: %s", exc)
        return (0, 0)
    return (eligible, unsupported)


def describe_generic_gate(model: str | None, model_code: int | str | None = None) -> dict:
    """Project evaluate_generic_gate's result to the two keys a diagnostic sensor needs.

    Holds no logic of its own and never raises. Computed from the catalog
    and the curated table alone, so it can be reported regardless of the
    options toggle.
    """
    result = evaluate_generic_gate(model, model_code)
    return {
        "unmapped_generic_identities": list(result.unmapped_identities),
        "generic_gate_blocked_by": list(result.blocked_by),
    }


def build_generic_entities(coordinator, sensor_key: str, sensor_info: dict, base_slug: str) -> list:
    """Return the generic sensors for one sub-device, or [] for every rejection path.

    Never raises: a malformed catalog entry must not abort sensor platform
    setup for the whole integration.
    """
    try:
        # A per-poll fact, not a static model property, so it stays here
        # rather than moving into evaluate_generic_gate.
        data = sensor_info.get("data") or {}
        if data.get("type") != "unknown":
            return []

        model = sensor_info.get("model")
        model_code = sensor_info.get("model_code")
        result = evaluate_generic_gate(model, model_code)
        if not result.passed:
            return []

        return [
            RainPointGenericSensor(coordinator, sensor_key, sensor_info, base_slug, entry, result.port_number)
            for entry in result.datapoints
        ]
    except Exception as exc:
        _LOGGER.debug("build_generic_entities failed for sensor_key=%s: %s", sensor_key, exc)
        return []


class RainPointGenericSensor(RainPointSensorBase):
    """Provisional, visibly-unverified sensor for a curated catalog status identity.

    Normal (non-diagnostic) sensor, enabled by default in the entity
    registry: the off-by-default options toggle is the opt-in, so a second
    layer of hiding here would make the feature look broken. Trust is
    conveyed by marking, not by burying. device_info is inherited from
    RainPointSensorBase unchanged, so this entity lands on the same device
    card as the existing Unsupported and Raw Payload sensors.
    """

    def __init__(
        self,
        coordinator,
        sensor_key: str,
        sensor_info: dict,
        base_slug: str,
        dp_entry: dict,
        port_number: int | None,
    ) -> None:
        super().__init__(coordinator, sensor_key, sensor_info, base_slug)
        identity = dp_entry.get("identity")
        spec = _IDENTITY_SPECS[identity]
        self._spec = spec
        self._identity = identity
        self._dp_port: int = dp_entry.get("dpPort")
        self._dp_code = dp_entry.get("dpCode")
        self._dp_data_type = dp_entry.get("dpDataType")

        # Built from the curated table key, never from the raw catalog
        # identity string, so a hostile or corrupt catalog identity can never
        # reach a unique_id. The port suffix is unconditional - including
        # single-port models - so a later catalog refresh that corrects the
        # port count cannot silently change an existing entity's unique_id.
        self._attr_unique_id = f"{UNIQUE_ID_PREFIX}{base_slug}{GENERIC_UNIQUE_ID_MARKER}{identity.lower()}_p{self._dp_port}"

        sub_name = sensor_info.get("sub_name") or "Sensor"
        zone = ""
        if port_number is not None and port_number > 1 and self._dp_port >= 1:
            zone = f" Zone {self._dp_port}"
        self._attr_name = f"{sub_name}{zone} {spec.label} (unverified)"

        self._attr_device_class = spec.device_class
        self._attr_native_unit_of_measurement = spec.unit
        # Always assigned, never conditional: see the module docstring for
        # why an unverified reading must never enter long-term statistics.
        self._attr_state_class = spec.state_class
        self._attr_suggested_display_precision = spec.precision
        # Assigned last so the marker always wins over the device_class
        # default icon.
        self._attr_icon = GENERIC_MARKER_ICON

    @property
    def native_value(self) -> float | None:
        data = self._sensor_data
        if not data:
            return None
        generic = data.get("generic") or {}
        fields = generic.get("fields") or []
        field = _matching_field(fields, self._identity, self._dp_port)
        if field is None:
            return None
        raw = field.get("value")
        if not isinstance(raw, int) or isinstance(raw, bool):
            return None
        value = self._spec.transform(raw)
        if value is None:
            return None
        low, high = self._spec.valid_range
        if not (low <= value <= high):
            return None
        return round(value, self._spec.precision)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Explicit six-key provenance allowlist; never spreads a source dict.

        Neither the decoded payload nor the sensor info record is ever
        spread into attributes here - the sensor info record carries fields
        that must not reach an entity attribute or a bug-report surface.
        """
        attrs = dict(super().extra_state_attributes)
        data = self._sensor_data or {}
        generic = data.get("generic") or {}
        fields = generic.get("fields") or []
        field = _matching_field(fields, self._identity, self._dp_port)
        width_mismatch = None
        if field is not None:
            width_mismatch = (field.get("catalog") or {}).get("width_mismatch")

        attrs["catalog_derived"] = True
        attrs["identity"] = self._identity
        attrs["dp_code"] = self._dp_code
        attrs["dp_port"] = self._dp_port
        attrs["dp_data_type"] = self._dp_data_type
        attrs["width_mismatch"] = width_mismatch
        return attrs
