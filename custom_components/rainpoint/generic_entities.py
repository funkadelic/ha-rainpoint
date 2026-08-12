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

Most rows are magnitudes. The event-time row is not: it publishes the
device's own naive wall-clock stamp as a string, with no device class.
SensorDeviceClass.TIMESTAMP is deliberately not used, because it requires a
timezone-aware value and the stamp carries no offset; attaching Home
Assistant's own timezone would assume it matches the device's and shift every
reading whenever it does not.

The battery row reports the same coarse reading the hand-written decoders
report, by delegating to the same mapping: a normal flag reads one hundred
percent and every other flag value reads nothing at all. No capture pairs a
non-normal flag with a known charge level, so a finer scale would be invented
rather than proven, and an invented percentage is indistinguishable from a
real one downstream.

The humidity row carries no device class, and that is the whole of what is
unproven about it. Its scale is proven twice over: RainPoint uses this
identity for a soil sensor's moisture percentage, and both hand-written soil
decoders read the byte carrying it as a percentage directly, on the same
framing the generic decode path reads. What no capture settles is which
physical quantity an unrecognized model reports through it, since one
identity covers both a soil moisture percentage and an air relative humidity.
The magnitude is therefore published and the semantics are not claimed.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.sensor import SensorDeviceClass

from .api import (
    _battery_flag_to_percent,
    _decode_packed_report_time,
    _decode_packed_timestamp,
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
    """Curated Home Assistant semantics for one catalog status identity.

    Most rows are numeric and carry a valid_range and a precision. A row whose
    reading is not a magnitude (a wall-clock stamp) sets both to None and
    returns a string from its transform: there is no range to bound and
    nothing to round, and inventing either would only look like a check that
    was performed.

    ``widths`` is the set of record byte widths the row's cited evidence
    actually covers, and it is mandatory. The record stream is
    self-describing, so a truncated or foreign frame can present this
    identity at a width no decoder has ever read: a three-byte stamp still
    unpacks to a plausible date, and roughly half of all three-byte words do.
    Every trusted path enforces a width before reading, so every row here
    declares one too, and a record of any other width reads as no state.
    """

    label: str
    device_class: Any
    unit: str | None
    state_class: Any
    transform: Callable[[int], float | str | None]
    valid_range: tuple[float, float] | None
    precision: int | None
    widths: frozenset[int]


def record_width_bytes(field: dict) -> int | None:
    """Return how many bytes the decoded record occupied, or None when unreadable.

    Read from the field's own hex, which is the only place the width survives:
    the parsed integer cannot distinguish a one-byte 0x01 from a four-byte
    0x00000001. An absent, non-string, or odd-length hex body returns None,
    which every caller treats as "refuse the reading" rather than as a width
    that happens to match.
    """
    raw_hex = field.get("raw")
    if not isinstance(raw_hex, str) or not raw_hex or len(raw_hex) % 2:
        return None
    return len(raw_hex) // 2


def has_declared_width(spec: GenericSensorSpec, field: dict) -> bool:
    """True when the record's width is one the row's evidence actually covers.

    The gate that stops a truncated or foreign record from being read as a
    reading no decoder has ever validated at that width. Shared by the sensor
    path and by generic control's state readback so the two cannot diverge on
    which records they are willing to believe.
    """
    width = record_width_bytes(field)
    return width is not None and width in spec.widths


def _rssi_dbm(raw: int) -> float | None:
    """Reinterpret the low byte of a reading as a signed int8 dBm value.

    The catalog declares STA_RSSI one byte wide on most models and two on the
    Bluetooth-capable ones, where the second byte carries the PHY the reading
    was taken on rather than part of the magnitude. Masking to the low byte
    first makes both widths decode the same way: a captured HTV210B frame reads
    b401, which the RainPoint app reports as -76 dBm at 1M PHY, and a one-byte 0xC4
    still reads -60 as the hand-written decoders have it.

    Without the mask the two-byte form arrived here as a little-endian word
    (b401 as 436), which the spec's valid_range then rejected, so the reading
    was dropped rather than shown wrong.
    """
    low = raw & 0xFF
    return float(low - 256 if low >= 128 else low)


def _battery_percent(raw: int) -> float | None:
    """Map the low byte of a raw STA_BAT reading to a battery percentage.

    Delegates to the mapping the hand-written decoders already use rather than
    restating it, so this row cannot drift from the trusted path: a normal flag
    reads 100 and every unmapped flag reads None, which the caller turns into
    no state at all rather than an invented charge level.

    The low byte is exactly what the hand-written extraction reads (the first
    value byte of the record), and every multi-byte value in these framings is
    little-endian, so the mask and that extraction agree byte for byte. The row
    still declares a single-byte width: the trusted function would read the
    first byte of a wider record without complaint, but no capture shows one,
    and accepting a width nothing has demonstrated is what this table avoids.
    """
    percent = _battery_flag_to_percent(raw & 0xFF)
    return None if percent is None else float(percent)


def _temperature_c(raw: int) -> float | None:
    """Convert a raw Fahrenheit-times-ten reading to Celsius."""
    return _f10_to_c(raw)


def _percent_byte(raw: int) -> float | None:
    """Read a percentage stored as one byte, unscaled.

    Both hand-written soil decoders read this identity's byte exactly this
    way, so no arithmetic is applied here either. A byte above 100 is left for
    the row's valid_range to reject rather than being clamped, so an
    out-of-range frame reads as no state instead of as a plausible 100.
    """
    return float(raw)


def _duration_seconds(raw: int) -> float | None:
    """Read a duration stored as an unsigned little-endian word of seconds.

    Both hand-written valve decoders read this identity's record exactly this
    way, at either observed width, so no arithmetic is applied here either.
    """
    return float(raw)


def _packed_report_wall_clock(raw: int) -> str | None:
    """Unpack a packed report-time stamp to a naive ISO-8601 string.

    Delegates to the unpacking proven for this identity specifically, rather
    than to the event-time one. The two are currently identical over the whole
    32-bit space, so this buys no different result today; what it buys is that
    each row's citation points at the evidence for its own identity, so
    correcting one stamp's layout cannot silently redefine the other. If the
    two are ever consolidated, the citations are what say whether one function
    is proven for both identities or merely reused across them.

    Naive and without a device class for the same reason the event-time row
    is; see that transform.
    """
    return _decode_packed_report_time(raw)


def _packed_wall_clock(raw: int) -> str | None:
    """Unpack a packed wall-clock stamp to a naive ISO-8601 string.

    Delegates to the hand-written unpacking rather than restating the bit
    layout, so this row cannot drift from it. A zero word means "no event" and
    a word whose fields do not form a real date reads as no state at all,
    both of which that function already returns as None.

    Deliberately not SensorDeviceClass.TIMESTAMP: that class requires a
    timezone-aware value, the stamp carries no offset, and the device reports
    its own local wall clock. Attaching Home Assistant's timezone would assume
    the two agree and shift every reading whenever they do not, which is the
    kind of inference the rest of this table exists to avoid. The reading is
    therefore a plain string state, the same naive ISO form the hand-written
    valve decoders already expose as a zone attribute.
    """
    return _decode_packed_timestamp(raw)


def _wkstate_open(raw: int) -> float | None:
    """Mask bit zero: the open/closed reading every cited decoder agrees on.

    See the STA_WKSTATE row's Evidence note below for the decoder paths this
    reading rests on and the decision governing a model no capture backs.
    """
    return float(raw & 0x01)


# Evidence-backed only: a row exists here only because an existing
# hand-written decoder proves both its unit and its scaling, on the same wire
# format the generic decode path reads. Nothing is inferred from the catalog's
# dpDataType, and an identity whose only citable decoder works on a different
# encoding stays out of the table entirely.
#
# Every row carries an Evidence note naming the decoder and the capture it
# rests on, and a Width note for the record widths that evidence covers. Cite
# what a decoder demonstrably does, never what the catalog declares: the
# battery and humidity notes in the module docstring above both replaced
# earlier claims that had gone stale against the decoders they described.
_IDENTITY_SPECS: dict[str, GenericSensorSpec] = {
    "STA_BAT": GenericSensorSpec(
        label="Battery",
        device_class=SensorDeviceClass.BATTERY,
        unit="%",
        state_class=None,
        transform=_battery_percent,
        valid_range=(0.0, 100.0),
        precision=0,
        widths=frozenset({1}),
        # Width: every capture carries this flag as a single byte, and the
        # trusted extraction reads only the first byte of the record.
        # Evidence: api/validators.py (_extract_battery_flag) locates STA_BAT
        # structurally and reads its first value byte, on the same record walk
        # the generic decode path uses, and api/validators.py
        # (_battery_flag_to_percent) is the percentage mapping every
        # hand-written decoder that reports a battery level already applies to
        # that byte. This row calls that same function, so the unit and the
        # scaling are the trusted path's own, not a reading of the catalog's
        # dpDataType.
    ),
    "STA_DURATION": GenericSensorSpec(
        label="Duration",
        device_class=SensorDeviceClass.DURATION,
        unit="s",
        state_class=None,
        transform=_duration_seconds,
        valid_range=(0.0, 4294967295.0),
        precision=0,
        widths=frozenset({2, 4}),
        # Width: the two the captures and the trusted decoders agree on, 2
        # bytes on the HTV213 family and 4 on the HTV210B, which is the same
        # pair api/decoders.py enforces before reading seconds.
        # Evidence: the captured HTV245 zone-2-active frame proves the unit
        # internally, with no external reading needed. Its report time unpacks
        # to 17:40:51, its zone-2 event time to 18:29:51, and the difference of
        # 2940 seconds is exactly the raw value this identity carries on that
        # zone. api/decoders.py (_extract_htv213_zones) and api/decoders.py
        # (_extract_htv210b_zones) both read the record as little-endian
        # seconds, at the two widths captures show (2 bytes on the HTV213
        # family, 4 on the HTV210B).
        #
        # The valid_range is the widest of those record widths, not a claim
        # about how long a run can be: an unsigned duration has no ceiling the
        # payload contradicts, and inventing one would drop a long but real
        # reading rather than catch anything.
        #
        # Labelled "Duration" rather than "Time Remaining". On the valve hub
        # the delta above shows it is the remainder of the current run, but a
        # controller may report a configured duration through the same
        # identity; seconds are seconds either way, so only the narrower name
        # would be a guess.
    ),
    "STA_EVTIME": GenericSensorSpec(
        label="Event Time",
        device_class=None,
        unit=None,
        state_class=None,
        transform=_packed_wall_clock,
        valid_range=None,
        precision=None,
        widths=frozenset({4}),
        # Width: exactly 4, the width api/decoders.py requires before it will
        # unpack an event time, and the only width any capture shows.
        # Evidence: api/decoders.py (_decode_packed_timestamp) unpacks this
        # word, and its docstring records the two independent captures that
        # confirm the bit layout: one frame's trailing stamp decodes to the day
        # that capture was taken, and a mid-run frame's zone event time is
        # exactly that frame's report time plus the zone's remaining duration.
        # api/decoders.py reads the same field index (21) off the same
        # little-endian four-byte record the generic decode path hands over.
    ),
    "STA_REPTIME": GenericSensorSpec(
        label="Report Time",
        device_class=None,
        unit=None,
        state_class=None,
        transform=_packed_report_wall_clock,
        valid_range=None,
        precision=None,
        widths=frozenset({4}),
        # Width: exactly 4, the width api/utils.py (_extract_report_time)
        # requires before unpacking, and the only width any capture shows.
        # Evidence: api/utils.py (_decode_packed_report_time) unpacks this
        # identity's word, and its year base is confirmed against captures
        # whose decoded stamp matched the moment they were pulled, where a base
        # of 2000 would have placed those same frames in 2006. api/utils.py
        # (_extract_report_time) reads it off the same little-endian four-byte
        # record the generic decode path hands over, and the hand-written
        # decoders already surface the result on trusted devices.
    ),
    "STA_RH": GenericSensorSpec(
        label="Humidity",
        device_class=None,
        unit="%",
        state_class=None,
        transform=_percent_byte,
        valid_range=(0.0, 100.0),
        precision=0,
        widths=frozenset({1}),
        # Width: a single byte, which is what both cited soil decoders read.
        # Evidence: api/decoders.py (decode_moisture_simple, HCS026FRF) reads
        # the byte this identity carries as a moisture percentage with no
        # scaling, against a captured frame whose 0x1A reads as 26%, and
        # api/decoders.py (_decode_moisture_full_hex, HCS021FRF) reads the byte
        # behind the same 0x88 tag the same unscaled way, with 0x1F reading as
        # 31%. Both are the 10# framing the generic decode path reads, and
        # decoding that same captured frame generically resolves that byte to
        # this identity.
        #
        # No device class: both proofs are soil moisture readings, while
        # RainPoint also uses this identity for air relative humidity, so the
        # percentage is proven but which quantity an unrecognized model reports
        # is not. Claiming SensorDeviceClass.HUMIDITY would assert the part
        # that is unproven, on exactly the models with no decoder to check it
        # against.
    ),
    "STA_RSSI": GenericSensorSpec(
        label="Signal Strength",
        device_class=SensorDeviceClass.SIGNAL_STRENGTH,
        unit="dBm",
        state_class=None,
        transform=_rssi_dbm,
        valid_range=(-120.0, -1.0),
        precision=0,
        widths=frozenset({1, 2}),
        # Width: 1 on most models and 2 on the Bluetooth-capable ones, both
        # covered by the low-byte mask the transform applies.
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
        widths=frozenset({2}),
        # Width: 2 bytes, the width the cited decoders read through _le16
        # before scaling.
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
        widths=frozenset({1}),
        # Width: a single state byte, the width every cited decoder checks
        # for before masking bit zero.
        # Evidence: the claim is about the shape of the record, not a list of
        # blessed model names -- a single-byte STA_WKSTATE record whose bit 0
        # is the running flag. widths=frozenset({1}) above is what enforces
        # that shape, so the claim has a mechanism behind it rather than
        # sitting as prose beside an unrelated check, and a newly catalogued
        # model presenting the same single-byte record is covered by
        # construction.
        #
        # A model-name allowlist is not available here: every evidenced
        # family below is in HAND_WRITTEN_MODELS, and having a hand-written
        # decoder is exactly what api/trust.py's is_hand_written_model uses to
        # keep a model out of this generic path, so a model-name gate would
        # refuse every model that can actually reach this row. Trusting only
        # proven models and keeping the feature are not both available.
        #
        # Provenance, not a gate, naming each decoder path in api/decoders.py
        # by symbol. Line numbers are deliberately omitted: the two this note
        # used to carry both drifted onto unrelated code, a symbol does not,
        # and TestRunStateEvidenceNoteDriftGuard below checks the symbol.
        # - decode_htv213frf_valve, the HTV213FRF/HTV245FRF datapoint-map
        #   branch, masking through _extract_htv213_zones, device reporting
        #   0x21 and 0x20 rather than 0x01 and 0x00. Its sibling branch,
        #   _decode_htv213frf_ascii, is deliberately not evidence for this
        #   row: it reads a decimal field out of a comma-separated payload,
        #   not this status byte, and calls a zone open on any non-zero value
        #   rather than on bit zero. The two readings disagree on 0x20, which
        #   is why only the datapoint-map branch is cited here.
        # - decode_valve_hub, the TLV valve-hub path, through
        #   _extract_valve_hub_zone, comparing the raw byte against 0x01 on
        #   hardware that reports plain 0x01 and 0x00.
        # - decode_htv145frf, the flat marker stream, 0xD8 zone marker, bit 0
        #   set means open, device reporting 0x21 and 0x20. Its other bit-0
        #   read, the 0xDC marker, is the hub online flag and is not evidence
        #   for this row.
        # - decode_htv210b, the structural record walk, masking bit 0, with
        #   the same mask applied on the command-response path
        #   (decode_htv210b_dp_state).
        # - decode_hic801w. Its own evidence, stated inline: STA_WKSTATE reads
        #   0x21 whenever any station runs and 0x00 when idle, 12 of 12
        #   frames on the reporting owner's unit, settled 2026-08-10.
        #   decode_hic801w itself reads no STA_WKSTATE byte and derives idle
        #   from STA_WATER_ZONES b0 instead, so this row's evidence for
        #   HIC801W comes from its capture corpus rather than from a byte the
        #   decoder reads, and the decoder stays independent of this row.
        #
        # These five paths span RainPoint's framings (the datapoint map, TLV,
        # the flat marker stream and the structural record walk) and both raw
        # encodings, and masking bit zero is the single reading that
        # satisfies all of them at once. No capture in either corpus
        # contradicts it. That is the reasoning, not a headcount: a count of
        # families expires the moment a sixth arrives.
        #
        # Correction: a frame captured 2026-07-17 was once recorded as an
        # idle capture reading 0x21. It was not idle. It carries WATER_ZONES
        # 01 03 00 00 and DURATION 1800, so station 1 was mid-run, and 0x21
        # meaning running is consistent with the other paths rather than in
        # tension with them. That earlier idle reading is void and must not
        # be cited again.
        #
        # Decision: the row keeps reading bit zero for a model it has no
        # captures for, and does not refuse the way widths above refuses an
        # unevidenced record width. Reverses on a captured frame from any
        # model showing bit zero meaning something other than running -- not
        # a suspicion, and not the absence of a capture, an actual
        # counterexample.
        #
        # generic_control._run_state_open reads this same row for its write
        # confirmation, so a wrong bit-zero semantic would both display a
        # wrong state and confirm a command that never moved hardware. It was
        # examined for this decision and inherits no new refusal: its
        # declared-width and ASCII-declined refusals stand unchanged.
        #
        # This provenance list is kept complete by
        # TestRunStateEvidenceNoteDriftGuard in tests/test_generic_entities.py.
        #
        # The bits above the lowest one are not explained by any cited
        # decoder and are deliberately left unread.
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
    # model RainPoint describes with no datapoints at all cannot be helped
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
        # Annotated int rather than int | None on an invariant the gate owns,
        # not on a local check: _bad_port_reason refuses any datapoint whose
        # dpPort is not a plain int, and _duplicate_port_reason refuses the
        # ambiguous rest, both before build_generic_entities constructs
        # anything. Deliberately unguarded here -- a defensive branch would
        # pin behaviour for a state the factory cannot produce, and read as
        # though the gate might not hold.
        self._dp_port: int = dp_entry.get("dpPort")
        self._dp_code = dp_entry.get("dpCode")
        self._dp_data_type = dp_entry.get("dpDataType")

        # Built from the curated table key, never from the raw catalog
        # identity string, so a hostile or corrupt catalog identity can never
        # reach a unique_id. The port suffix is unconditional - including
        # single-port models - so a later catalog refresh that corrects the
        # port count cannot silently change an existing entity's unique_id.
        self._attr_unique_id = f"{UNIQUE_ID_PREFIX}{base_slug}{GENERIC_UNIQUE_ID_MARKER}{identity.lower()}_p{self._dp_port}"

        zone = ""
        if port_number is not None and port_number > 1 and self._dp_port >= 1:
            zone = f"Zone {self._dp_port} "
        self._attr_name = f"{zone}{spec.label} (unverified)"

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
    def native_value(self) -> float | str | None:
        """Publish the row's reading, or no state when any check refuses it.

        Four independent refusals, each returning None rather than a
        substitute: no decoded data for this sub-device, no field matching
        this identity and port, a raw value that is not a plain int, and a
        record whose width the row has no evidence for. Only then does the
        transform run, and a numeric result must still fall inside the row's
        declared range.
        """
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
        if not has_declared_width(self._spec, field):
            # A record at a width this row has no evidence for. Refused before
            # the transform runs, because several transforms would return a
            # perfectly plausible reading from a truncated word.
            return None
        value = self._spec.transform(raw)
        if value is None:
            return None
        # A non-numeric reading is returned as its transform produced it. The
        # range check and the rounding below are the numeric rows' guards, and
        # a row that declares neither has nothing for them to check: the
        # transform itself is what rejects an unusable word, by returning None.
        if isinstance(value, str):
            return value
        if self._spec.valid_range is None or self._spec.precision is None:
            # Unreachable through the committed table, where every numeric row
            # declares both. Fails closed rather than publishing an unbounded,
            # unrounded magnitude if a later row is added without them.
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
