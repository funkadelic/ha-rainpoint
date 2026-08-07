"""Select entities for RainPoint integration."""

import logging

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .api import _parse_sub_power_mode, _splice_sub_power_mode
from .const import DOMAIN, MODEL_HTV210B, UNIQUE_ID_PREFIX
from .coordinator import (
    SILENT_DATA_TYPE,
    RainPointCoordinator,
    _sub_devices_by_addr,
    is_hub_record,
)
from .entity import LateEntityAdder, RainPointSubDeviceEntity, register_late_adder
from .hub_entities import RainPointHubChannelSelect

_LOGGER = logging.getLogger(__name__)

# The only device this key-5 blob shape has ever been observed on. Widening
# this set later is a one-line addition once someone captures a second
# device's traffic and confirms key 5 means the same thing there -- the
# parse/splice contract in api/utils.py does not need to change (D-01).
SUB_POWER_MODE_MODELS = frozenset({MODEL_HTV210B})

# Canonical key-5 mode digit -> display label, insertion-ordered so
# _attr_options renders Power Saving, Standard, Enhance in that order.
POWER_MODE_LABELS = {
    "0": "Power Saving",
    "1": "Standard",
    "2": "Enhance",
}
_LABEL_TO_MODE = {label: mode for mode, label in POWER_MODE_LABELS.items()}


def _sub_device_record(hub_records: list, mid, addr) -> dict:
    """Return one sub-device's raw record from a list of top-level hub records, or {}.

    Serves both `coordinator.data["hubs"]` (the polled snapshot) and a fresh
    `get_devices_by_hid` response (D-04's pre-write read) -- both are lists of
    the same top-level hub-record shape, so one function can resolve either.
    Matches the hub on its `mid` field, skipping non-dict entries, then
    reuses `_sub_devices_by_addr` for the inner index rather than walking
    `subDevices` itself, so this inherits the malformed-record tolerance
    every other walk in the package already has.
    """
    if not isinstance(hub_records, list):
        return {}
    for hub in hub_records:
        if not isinstance(hub, dict):
            continue
        if hub.get("mid") != mid:
            continue
        return _sub_devices_by_addr(hub).get(addr, {})
    return {}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up RainPoint select entities."""
    data = hass.data[DOMAIN][entry.entry_id]
    coordinator: RainPointCoordinator = data["coordinator"]

    # Sub-device transmission-power select, per D-07: discovery and
    # governance come from coordinator.data["sensors"] through the
    # LateEntityAdder / add-once ledger machinery, the same pattern
    # number.py's async_setup_entry uses -- not the raw hub-record walk
    # below, which the add-once ledger and the orphaned-entity Repairs sweep
    # cannot see. Position is load-bearing: this block owns its own
    # async_add_entities call and must run above the hub-record walk, since
    # that walk returns early on a malformed hubs snapshot and a sub-device
    # block placed after it would be silently skipped on that path.
    sensors_cfg = {key: info for key, info in coordinator.data.get("sensors", {}).items() if isinstance(info, dict)}

    def build(key: str, info: dict) -> list:
        """Return the power-mode select a sensor key currently supports, or []."""
        if info.get("model") not in SUB_POWER_MODE_MODELS:
            return []
        decoded = info.get("data") or {}
        # A hub-paired but silent HTV210B still reaches the sensors dict
        # carrying its model string, the same trap number.py's and valve.py's
        # own silent-device guards were written for, so the model check
        # alone would admit it. This guard states the invariant rather than
        # resting on a data shape.
        if decoded.get("type") == SILENT_DATA_TYPE:
            return []
        return [RainPointSubDevicePowerSelect(coordinator, key, info)]

    adder = LateEntityAdder(coordinator, async_add_entities, build, "select")
    # Published before anything is emitted, so the removal sweep can ask this
    # adder what it created for a key that later vanishes.
    register_late_adder(data, adder)

    power_entities: list = []
    for key, info in sensors_cfg.items():
        power_entities.extend(adder.collect(key, info))

    if power_entities:
        async_add_entities(power_entities)

    # Registered unconditionally, for the same reason number.py and valve.py
    # do: a silent HTV210B at setup produces nothing here, and it must still
    # gain the entity once it starts reporting, without a reload.
    entry.async_on_unload(coordinator.async_add_listener(adder.async_on_coordinator_update))

    entities = []

    hubs_cfg = coordinator.data.get("hubs", [])
    if not isinstance(hubs_cfg, list):
        _LOGGER.error("Expected hubs to be a list, got %s; skipping select entity setup", type(hubs_cfg).__name__)
        return
    hubs_dict = {str(hub.get("mid", i)): hub for i, hub in enumerate(hubs_cfg)}

    for _hub_key, hub_info in hubs_dict.items():
        if not is_hub_record(hub_info):
            continue
        entities.append(RainPointHubChannelSelect(coordinator, hub_info))

    _LOGGER.info("Added %d select entities", len(entities))
    async_add_entities(entities)


class RainPointSubDevicePowerSelect(RainPointSubDeviceEntity, SelectEntity):
    """Transmission power for a hub-paired HTV210B, read and written over `param` key 5.

    `EntityCategory.CONFIG` matches this repo's unbroken convention that every
    writable settings entity carries CONFIG, never DIAGNOSTIC (D-06).
    Default-disabled (`entity_registry_enabled_default = False`) for the same
    D-06 reason `RainPointHubBroadcastSwitch` is not: nothing inside Home
    Assistant can confirm a write actually landed on the device, only the
    vendor app can, so this ships hidden until the maintainer verifies the
    write path on real hardware. Flipping the flag later changes the default
    for newly-added entities only -- the unique_id shape below does not
    depend on it.
    """

    _attr_entity_category = EntityCategory.CONFIG
    _attr_entity_registry_enabled_default = False
    _attr_icon = "mdi:antenna"

    def __init__(self, coordinator: RainPointCoordinator, sensor_key: str, sensor_info: dict) -> None:
        """Build the select; is_on-equivalent state is derived live, never stored here."""
        hid = sensor_info.get("hid")
        mid = sensor_info.get("mid")
        addr = sensor_info.get("addr")
        base_slug = f"{hid}_{mid}_{addr}"
        super().__init__(coordinator, sensor_key, sensor_info, base_slug)
        self._attr_options = list(POWER_MODE_LABELS.values())
        self._attr_unique_id = f"{UNIQUE_ID_PREFIX}{base_slug}_power_mode"
        self._attr_name = "Transmission Power"
        # The post-write override: set only after a write's cloud
        # acknowledgment, cleared by the next real poll so a poll that
        # contradicts the command always wins. Mirrors
        # RainPointHubBroadcastSwitch's _optimistic / _hubs_snapshot_id pair
        # and the reasoning in its _handle_coordinator_update override:
        # seeded from current data rather than left None, since
        # CoordinatorEntity.async_added_to_hass never calls
        # _handle_coordinator_update itself, so a None seed would make the
        # very first push after setup look like a poll.
        self._optimistic: str | None = None
        current_hubs = coordinator.data.get("hubs") if coordinator.data else None
        self._hubs_snapshot_id: int | None = id(current_hubs) if current_hubs is not None else None

    @property
    def _record(self) -> dict:
        """Return this sub-device's own live record, or {} when none exists yet."""
        hub_records = self.coordinator.data.get("hubs", []) if self.coordinator.data else []
        return _sub_device_record(hub_records, self._sensor_info.get("mid"), self._sensor_info.get("addr"))

    @property
    def current_option(self) -> str | None:
        """Return the optimistic label if one is pending, else the live poll value."""
        if self._optimistic is not None:
            return POWER_MODE_LABELS.get(self._optimistic)
        mode = _parse_sub_power_mode(self._record.get("param"))
        return POWER_MODE_LABELS.get(mode) if mode is not None else None

    @callback
    def _handle_coordinator_update(self) -> None:
        """Clear the optimistic override only on a real poll, not on every push.

        Mirrors RainPointHubBroadcastSwitch's override, watching this
        sub-device's own hub list rather than re-deriving a separate signal:
        a REST poll is the only thing that allocates a fresh "hubs" list,
        while both push entry points carry it forward by reference, so
        without this identity check any unrelated push would clear a
        just-written value before the poll meant to confirm it.
        """
        current_hubs = self.coordinator.data.get("hubs") if self.coordinator.data else None
        current_id = id(current_hubs) if current_hubs is not None else None
        if current_id != self._hubs_snapshot_id:
            self._hubs_snapshot_id = current_id
            self._optimistic = None
        super()._handle_coordinator_update()

    async def async_select_option(self, option: str) -> None:
        """Splice, write, and (only on success) apply the requested power mode.

        D-04's fresh pre-write read: the coordinator's copy of `param` can be
        up to 120s stale, and keys 11, 12, 50 and 51 are unidentified settings
        this phase must never blank, so the write is spliced against a
        `get_devices_by_hid` call issued immediately before it, never against
        `coordinator.data`. If that read raises, the exception propagates as
        the write's own refusal -- there is no fallback to the stale copy.
        """
        mode = _LABEL_TO_MODE.get(option)
        if mode is None:
            raise HomeAssistantError(f"Unknown transmission power option: {option}")
        client = self.coordinator._client
        hub_records = await client.get_devices_by_hid(self._sensor_info.get("hid"))
        record = _sub_device_record(hub_records, self._sensor_info.get("mid"), self._sensor_info.get("addr"))
        sid = record.get("sid")
        if sid is None:
            raise HomeAssistantError("The device could not be addressed, so this setting cannot be changed")
        spliced = _splice_sub_power_mode(record.get("param"), mode)
        if spliced is None:
            raise HomeAssistantError("The device's settings could not be read, so this setting cannot be changed")
        await client.update_sub_param(mid=self._sensor_info.get("mid"), sid=sid, param=spliced)
        # Optimistic, deliberately diverging from generic_control.py's
        # never-optimistic rule for the same reason
        # RainPointHubBroadcastSwitch._async_set_broadcast does: this write
        # receives a genuine cloud acknowledgment (code 0), not an unread
        # hardware actuation.
        self._optimistic = mode
        self.async_write_ha_state()
