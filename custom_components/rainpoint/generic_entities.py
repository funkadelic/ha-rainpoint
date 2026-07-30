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
    get_catalog_variant_codes,
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
    """Reinterpret the low byte of a reading as a signed int8 dBm value.

    The catalog declares STA_RSSI one byte wide on most models and two on the
    Bluetooth-capable ones, where the second byte carries the PHY the reading
    was taken on rather than part of the magnitude. Masking to the low byte
    first makes both widths decode the same way: a captured HTV210B frame reads
    b401, which the vendor app reports as -76 dBm at 1M PHY, and a one-byte 0xC4
    still reads -60 as the hand-written decoders have it.

    Without the mask the two-byte form arrived here as a little-endian word
    (b401 as 436), which the spec's valid_range then rejected, so the reading
    was dropped rather than shown wrong.
    """
    low = raw & 0xFF
    return float(low - 256 if low >= 128 else low)


def _temperature_c(raw: int) -> float | None:
    """Convert a raw Fahrenheit-times-ten reading to Celsius."""
    return _f10_to_c(raw)


def _wkstate_open(raw: int) -> float | None:
    """Mask bit zero: the open/closed reading both cited decoders agree on."""
    return float(raw & 0x01)


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
    "STA_WKSTATE": GenericSensorSpec(
        label="Run State",
        device_class=None,
        unit=None,
        state_class=None,
        transform=_wkstate_open,
        valid_range=(0.0, 1.0),
        precision=0,
        # Evidence: api/decoders.py:264 (decode_htv213frf_valve, the ASCII
        # HTV213FRF/HTV245FRF path) masks bit zero and notes the device
        # reports 0x21 and 0x20 rather than 0x01 and 0x00, and
        # api/decoders.py:821 (_extract_valve_hub_zone, the TLV valve-hub
        # path) compares the raw byte against 0x01 on hardware that reports
        # plain 0x01 and 0x00. Masking bit zero is the one reading that
        # satisfies both decoders at once.
        #
        # The bits above the lowest one are not explained by either decoder
        # and are deliberately left unread.
    ),
}


def _is_hashable(value: Any) -> bool:
    """Return True when value can key a set or dict.

    Checked by hashing rather than by isinstance against Hashable, because a
    tuple containing a list satisfies that check and still raises. Catalog
    values are vendor data reshaped from JSON, so a list or dict can appear
    anywhere a scalar was expected.
    """
    try:
        hash(value)
    except TypeError:
        return False
    return True


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


def _usable_port(dp_port: Any) -> bool:
    """True when a declared dpPort can address one entity's unique_id."""
    return isinstance(dp_port, int) and not isinstance(dp_port, bool)


def _bad_port_reason(dp_entries: list[dict]) -> str | None:
    """Rule 1: any entry with a missing or unusable dpPort fails the whole model.

    Failing the model rather than skipping the entry is what stops two
    entities ever contending for one unique_id.
    """
    offenders = sorted({str(entry.get("identity")) for entry in dp_entries if not _usable_port(entry.get("dpPort"))})
    if not offenders:
        return None
    return "these readings don't declare a usable port number, so they can't be turned into entities: " + ", ".join(offenders)


def _duplicate_port_reason(dp_entries: list[dict]) -> str | None:
    """Rule 2: two entries sharing an (identity, dpPort) pair fail the whole model.

    An entity built over either one could not tell which value to show.
    Entries rule 1 already rejected are skipped: their port is not an int, and
    a non-int port can be unhashable (a JSON array arrives as a list), which
    would raise on the set key and collapse the evaluation into the outer
    wrapper's single generic reason. Skipping loses nothing, since rule 1 has
    already named them.
    """
    seen: set[tuple[str | None, int]] = set()
    duplicates: set[tuple[str | None, int]] = set()
    for entry in dp_entries:
        dp_port = entry.get("dpPort")
        if not _usable_port(dp_port):
            continue
        key = (entry.get("identity"), dp_port)
        if key in seen:
            duplicates.add(key)
        seen.add(key)
    if not duplicates:
        return None
    formatted = ", ".join(
        f"{identity} on port {dp_port}" for identity, dp_port in sorted(duplicates, key=lambda pair: (str(pair[0]), str(pair[1])))
    )
    return "the catalog declares the same reading more than once for the same port, so its value would be ambiguous: " + formatted


def _dp_code_sort_key(code: Any) -> tuple[int, Any, str]:
    """Order integer codes numerically and anything else after them by text.

    Keeps a message reading 1, 2, 15 rather than the 1, 15, 2 a plain string
    sort produces, without assuming the catalog only ever carries integers.
    """
    if isinstance(code, int) and not isinstance(code, bool):
        return (0, code, "")
    return (1, 0, str(code))


def _dp_code_reasons(raw_entry: list) -> list[str]:
    """Rule 3: unusable and reused dpCodes, scanned across the variant's full dp list.

    The runtime catalog matcher keys on dpCode alone and refuses to annotate a
    field whose dpCode is ambiguous, so an entity built over such an entry
    would never resolve a value. The scan spans control entries too, because
    the matcher searches the full list: a control entry sharing a status
    entry's dpCode makes that status entry just as unresolvable. Multi-zone
    variants repeating one dpCode across ports are the common shape here, so
    this rejects real catalog models by design rather than as an edge case.

    An unhashable dpCode cannot key the counter and would raise. Unlike a bad
    dpPort, no earlier rule reports it, so it earns a reason of its own.
    """
    counts: dict[Any, int] = {}
    identities: dict[Any, set[str]] = {}
    unusable: set[str] = set()
    for entry in raw_entry:
        if not isinstance(entry, dict):
            continue
        dp_code = entry.get("dpCode")
        if not _is_hashable(dp_code):
            unusable.add(str(entry.get("identity")))
            continue
        counts[dp_code] = counts.get(dp_code, 0) + 1
        identities.setdefault(dp_code, set()).add(str(entry.get("identity")))

    found: list[str] = []
    if unusable:
        found.append(
            "these readings don't declare a usable datapoint code, so they can't be matched to a decoded value: "
            + ", ".join(sorted(unusable))
        )
    duplicates = sorted((code for code, count in counts.items() if count > 1), key=_dp_code_sort_key)
    if duplicates:
        formatted = ", ".join(f"dpCode {code} ({', '.join(sorted(identities[code]))})" for code in duplicates)
        found.append(
            "the catalog reuses one datapoint code for more than one reading, so those readings can't be told apart: " + formatted
        )
    return found


def _uncurated_reason(dp_entries: list[dict], unmapped_identities: tuple[str, ...]) -> str | None:
    """Rule 4: declared status identities with no curated row.

    unmapped_identities is reported as its own attribute regardless of
    outcome, so this reason summarises it rather than repeating it.
    """
    if not unmapped_identities:
        return None
    total = len({entry.get("identity") for entry in dp_entries})
    return (
        f"{len(unmapped_identities)} of this device's {total} status readings have no verified "
        "definition yet, so they can't be turned into entities (see the unmapped identities list)"
    )


def _unresolved_variant_reason(model: str | None, model_code: int | str | None) -> str:
    """Return why a catalog lookup for this model and modelCode resolved to nothing.

    Three outcomes, because they call for three different fixes: a model the
    catalog has never heard of needs the snapshot extended, a device reporting
    a modelCode the catalog does not list needs that variant added, and a
    device reporting no modelCode at all against several listed variants needs
    the code from the device rather than any catalog change.
    """
    known_codes = get_catalog_variant_codes(model)
    if not known_codes:
        return f"{model} is not in the product catalog, so nothing is known about what it reports"
    if model_code is not None:
        return (
            f"the product catalog has no entry for this device's hardware variant of {model} "
            f"(it reports {model_code}; the catalog lists {', '.join(known_codes)})"
        )
    return (
        f"the catalog lists more than one hardware variant for {model} "
        f"({', '.join(known_codes)}) and this device did not report which one it is"
    )


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

    # Distinct lookup outcomes, deliberately not collapsed into one "not in
    # the catalog" message. They call for different fixes: an unresolved
    # variant is broken down further by _unresolved_variant_reason, while a
    # model the vendor describes with no datapoints at all cannot be helped
    # by any of those. Roughly a third of the committed catalog is that last
    # case, so reporting it as absent would misdirect most reports about
    # those models.
    raw_entry = get_catalog_entry(model, model_code)
    if raw_entry is None:
        return GenericGateResult(
            datapoints=[],
            unmapped_identities=(),
            blocked_by=(_unresolved_variant_reason(model, model_code),),
            port_number=None,
        )
    if not raw_entry:
        return GenericGateResult(
            datapoints=[],
            unmapped_identities=(),
            blocked_by=(f"{model} is in the product catalog, but the catalog lists no readings for it",),
            port_number=get_catalog_port_number(model, model_code),
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

    uncurated = {entry.get("identity") for entry in dp_entries if entry.get("identity") not in _IDENTITY_SPECS}
    unmapped_identities = tuple(sorted(uncurated))

    # Every rule is evaluated; none short-circuits another. A variant failing
    # several independent rules reports all of them rather than just the first.
    reasons = [
        reason
        for reason in (
            _bad_port_reason(dp_entries),
            _duplicate_port_reason(dp_entries),
            *_dp_code_reasons(raw_entry),
            _uncurated_reason(dp_entries, unmapped_identities),
        )
        if reason
    ]

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
