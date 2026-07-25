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
proven for it. Adding a row later is additive and only widens which models
pass the gate.
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


def _rh_percent(raw: int) -> float | None:
    """Identity mapping: the catalog datapoint is already a direct percent."""
    return float(raw)


def _rssi_dbm(raw: int) -> float | None:
    """Reinterpret an unsigned byte as a signed int8 dBm reading."""
    value = raw - 256 if raw >= 128 else raw
    return float(value)


def _temperature_c(raw: int) -> float | None:
    """Convert a raw Fahrenheit-times-ten reading to Celsius."""
    return _f10_to_c(raw)


# Evidence-backed only: a row exists here only because an existing
# hand-written decoder proves both its unit and its scaling. Nothing is
# inferred from the catalog's dpDataType. Exactly one evidence marker per row
# - the battery-absence note in the module docstring above is deliberately
# worded without one, so the marker count and the row count stay equal.
_IDENTITY_SPECS: dict[str, GenericSensorSpec] = {
    "STA_RH": GenericSensorSpec(
        label="Humidity",
        device_class=SensorDeviceClass.HUMIDITY,
        unit="%",
        state_class=None,
        transform=_rh_percent,
        valid_range=(0.0, 100.0),
        precision=0,
        # Evidence: api/decoders.py:734 documents "42 = current humidity
        # (42%)" for the HWS019WRF-V2 display-hub decoder - a direct integer
        # percent, no scale factor - and _apply_hws019_positional_item
        # (api/decoders.py:692-701) stores that value unmodified. This is an
        # ASCII-format route rather than the single unsigned byte the catalog
        # declares for STA_RH, but the mapping it proves (raw value equals
        # percent) is the same one this row applies.
    ),
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


def _declared_status_datapoints(model: str | None, model_code: int | str | None) -> list[dict]:
    """Return the model variant's declared STA_* catalog dp entries, or [] on any miss."""
    try:
        dp_list = get_catalog_entry(model, model_code)
    except Exception as exc:
        _LOGGER.debug("get_catalog_entry failed for model=%s model_code=%s: %s", model, model_code, exc)
        return []
    if not dp_list:
        return []
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


def build_generic_entities(coordinator, sensor_key: str, sensor_info: dict, base_slug: str) -> list:
    """Return the generic sensors for one sub-device, or [] for every rejection path.

    Never raises: a malformed catalog entry must not abort sensor platform
    setup for the whole integration.
    """
    try:
        model = sensor_info.get("model")

        # Load-bearing, not defence in depth: the sensor model-factory map in
        # sensor.py is a strict subset of the hand-written set, because every
        # hand-written valve model gets its entities from the valve and
        # number platforms and therefore has no entry in that map. Without
        # this check a hand-written valve model would reach this factory.
        if is_hand_written_model(model):
            return []

        data = sensor_info.get("data") or {}
        if data.get("type") != "unknown":
            return []

        model_code = sensor_info.get("model_code")
        dp_entries = _declared_status_datapoints(model, model_code)
        if not dp_entries:
            return []

        # Any dp entry with a missing/unusable dpPort, or two entries sharing
        # the same (identity, dpPort), fails the whole model's gate rather
        # than being skipped, so no two entities can ever contend for one
        # unique_id.
        seen_keys: set[tuple[str | None, int]] = set()
        for entry in dp_entries:
            dp_port = entry.get("dpPort")
            if not isinstance(dp_port, int) or isinstance(dp_port, bool):
                return []
            key = (entry.get("identity"), dp_port)
            if key in seen_keys:
                return []
            seen_keys.add(key)

        # All-or-nothing per model, never a best-effort subset.
        for entry in dp_entries:
            if entry.get("identity") not in _IDENTITY_SPECS:
                return []

        port_number = get_catalog_port_number(model, model_code)
        ordered = sorted(dp_entries, key=lambda entry: (entry.get("dpPort"), entry.get("identity")))
        return [RainPointGenericSensor(coordinator, sensor_key, sensor_info, base_slug, entry, port_number) for entry in ordered]
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
