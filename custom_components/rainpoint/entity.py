"""Shared plumbing for entities that represent one RainPoint sub-device.

Every platform binds its entities to a coordinator plus a sensor key, then
reports the same firmware and timestamp attributes and resolves the same device
page. Each platform used to carry its own copy of that plumbing, and the copies
drifted: firmware never reached the device page, only the sensor platform linked
a device to its hub, and two of the copies would raise on a sub-device that has
no reading yet.

The two pieces here are split by what a platform can actually reuse.
``RainPointSubDeviceEntity`` suits the platforms whose entities are constructed
from (coordinator, sensor_key, sensor_info, base_slug); the valve, number, and
generic control entities take a zone or datapoint instead, so they keep their
own constructors and call ``sub_device_attributes`` directly.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from homeassistant.core import callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .coordinator import (
    SILENT_DATA_TYPE,
    RainPointCoordinator,
    hub_connected_flag,
    hub_connectivity_record,
)
from .device import build_sub_device_info

_LOGGER = logging.getLogger(__name__)

# The hass.data[DOMAIN][entry_id] slot every platform publishes its adder into.
# The adders are otherwise local to each platform's async_setup_entry, and the
# removal sweep needs to ask them what they emitted.
LATE_ADDER_STORE_KEY = "late_adders"


class EmittedEntityLedger:
    """What one adder emitted, indexed by the sensor key that produced it.

    The adders' own bookkeeping cannot answer this: an emitted-unique_id set
    carries no key, and a key set carries no unique_ids. Removing exactly the
    rows a given key produced needs both halves in one structure, and asking
    for an exact list is what keeps the removal off string reasoning about
    unique_id prefixes.

    Also carries a small descriptor per key -- address, model, sub-device name
    and hub name -- so a card can still name the device after its key has left
    coordinator.data["sensors"] entirely. Those are raw cloud strings here;
    they are sanitized at the card boundary, not at record time, so nothing
    downstream can be handed an unsanitized value by accident.
    """

    def __init__(self) -> None:
        """Start empty; every entry is written by record and dropped by forget."""
        self._by_key: dict[str, set[str]] = {}
        self._descriptors: dict[str, dict] = {}

    def record(self, key: str, info: dict, entities: list) -> None:
        """Add the unique_ids of entities actually emitted for one sensor key.

        Appends rather than replaces, because a per-zone platform emits
        entities for the same key across several polls: a valve reporting zone
        1 now and zone 2 later must end with both ids under the one key.

        An entity with no unique_id is skipped: it has no registry row, so
        there is nothing to remove for it later.

        The descriptor is last-write-wins, so a device that gets renamed or
        re-modelled in the cloud is named by its most recent listing. It is
        written only for a key this ledger holds unique_ids for, which is the
        only population anything downstream reads a descriptor for. Writing it
        unconditionally would leave one entry per sensor key in the account on
        every adder, including keys that adder builds nothing for, and forget
        would never reach them because it only runs for keys with recorded ids.
        """
        for entity in entities:
            unique_id = getattr(entity, "_attr_unique_id", None)
            if unique_id is None:
                continue
            self._by_key.setdefault(key, set()).add(unique_id)
        if key not in self._by_key:
            return
        self._descriptors[key] = {
            "addr": info.get("addr"),
            "model": info.get("model"),
            "sub_name": info.get("sub_name"),
            "hub_name": info.get("hub_name"),
        }

    def unique_ids_for(self, key: str) -> frozenset[str]:
        """Return the unique_ids recorded for one sensor key."""
        return frozenset(self._by_key.get(key, ()))

    def descriptor_for(self, key: str) -> dict:
        """Return the cloud-supplied descriptor for one sensor key, or {}."""
        return dict(self._descriptors.get(key, {}))

    def keys(self) -> frozenset[str]:
        """Return every key with at least one recorded unique_id.

        An entry is created only once a non-None unique_id has been seen for
        the key, so membership here is exactly that condition. A key whose
        entities all lacked a unique_id has no registry row to remove and is
        therefore invisible to the removal sweep, which is the scope limit
        rather than an accident of this structure.
        """
        return frozenset(self._by_key)

    def forget(self, key: str) -> frozenset[str]:
        """Drop one key's entry and return the unique_ids it held.

        Reached only from an adder's own forget, which __init__'s
        _remove_orphaned_key_rows calls once those rows are actually gone.
        That coupling is what keeps the never-offer-twice property the adders'
        add-once bookkeeping exists for: forgetting on a key's absence instead
        would release ids whose registry rows still exist.
        """
        dropped = frozenset(self._by_key.pop(key, set()))
        self._descriptors.pop(key, None)
        return dropped


def register_late_adder(entry_store: dict, adder) -> None:
    """Publish one platform's adder so the removal sweep can reach it."""
    entry_store.setdefault(LATE_ADDER_STORE_KEY, []).append(adder)


def late_adders(entry_store: dict) -> list:
    """Return the registered adders, or an empty list if the slot is unreadable.

    Never raises: the callers are a coordinator listener and a Repairs flow
    step, and an exception in either breaks something far larger than this
    read. Matches the degrade-to-empty contract the registry sweeps' own
    guards carry.
    """
    try:
        return list(entry_store.get(LATE_ADDER_STORE_KEY) or [])
    except Exception as exc:
        _LOGGER.debug("Late adder registry unreadable; treating this entry as having none: %s", exc)
        return []


class LateEntityAdder:
    """Add entities for sensor keys that only become eligible after setup.

    Entity creation is otherwise one-shot: each platform builds its list from
    the single coordinator snapshot taken right after the first refresh, so
    anything needing a later poll to exist is unreachable rather than merely
    delayed. A device that is silent from the first poll, one that pairs
    mid-session, and one whose zones only appear once it starts reporting all
    fall in that gap. Registering this as a coordinator listener closes it.

    Bookkeeping is on emitted unique_id rather than on sensor key, which is
    what lets one adder serve a per-key platform and a per-zone one. A valve
    that reports zone 1 now and zone 2 later must gain the second entity
    without being handed the first again, and a repeated unique_id is an error
    in Home Assistant.

    The emitted set is pruned only in lockstep with an actual removal of the
    rows it names, through ``forget``, and never on key absence alone. A key
    vanishing from the coordinator does not remove the entities already
    registered for it, so forgetting it there would let a later reappearance
    offer the same unique_id a second time, which is an error in Home
    Assistant. Once those rows genuinely no longer exist that reasoning
    inverts, and holding the ids would leave a returning key with no entities
    until a reload. It is bounded by the number of distinct entities the
    installation has produced in one session.
    """

    def __init__(
        self,
        coordinator: RainPointCoordinator,
        async_add_entities: Callable[[list], None],
        build: Callable[[str, dict], list],
        domain: str,
    ) -> None:
        """Wrap a platform's per-key builder in add-once bookkeeping.

        domain is the entity domain this adder emits into. It is supplied by
        the platform rather than read off an entity because an entity has no
        domain until Home Assistant has registered it, and these are recorded
        before they are added. One adder instance serves exactly one platform's
        setup, so the value is fixed for the life of the adder. The removal
        sweep matches on it alongside the unique_id, since registry uniqueness
        is per domain and two domains may legitimately carry the same id.
        """
        self._coordinator = coordinator
        self._async_add_entities = async_add_entities
        self._build = build
        self.domain = domain
        self._emitted: set[str] = set()
        self.ledger = EmittedEntityLedger()

    def collect(self, key: str, info: dict) -> list:
        """Return the not-yet-emitted entities for one sensor key.

        The single place the bookkeeping is written, so the setup path and the
        listener path cannot disagree about what already exists.
        """
        fresh = []
        for entity in self._build(key, info):
            unique_id = getattr(entity, "_attr_unique_id", None)
            if unique_id is not None and unique_id in self._emitted:
                continue
            if unique_id is not None:
                self._emitted.add(unique_id)
            fresh.append(entity)
        # Records what was actually handed to Home Assistant, never the
        # builder's full output: an entity suppressed as already-emitted was
        # recorded on the poll that did emit it, and recording the full output
        # would be indistinguishable until the day the builder stopped being
        # deterministic.
        self.ledger.record(key, info, fresh)
        return fresh

    def forget(self, key: str) -> None:
        """Drop one key's ledger entry and the unique_ids it held.

        Called only alongside an actual removal of those registry rows, so the
        add-once guarantee still holds for every row that exists.
        """
        for unique_id in self.ledger.forget(key):
            self._emitted.discard(unique_id)

    @callback
    def async_on_coordinator_update(self) -> None:
        """Offer any entity that has become eligible since the last update."""
        sensors_cfg = (self._coordinator.data or {}).get("sensors", {})
        new: list = []
        for key, info in sensors_cfg.items():
            # One malformed record must not raise inside a listener, which
            # would break the update for every other key rather than skip one.
            if not isinstance(info, dict):
                continue
            new.extend(self.collect(key, info))
        if new:
            self._async_add_entities(new)


def sub_device_attributes(coordinator: RainPointCoordinator, sensor_key: str) -> dict[str, Any]:
    """Return the firmware and timestamp attributes shared by every platform.

    Reads through the coordinator entry rather than a cached copy so a firmware
    change after a reload is picked up on the next state write.

    A sub-device with no reading yet has a ``data`` of None, which the previous
    per-platform copies in valve.py and number.py fed straight into a membership
    test and raised on. An absent or None reading yields the firmware attribute
    alone here.

    Also carries a ``hub_connected`` marker (``True``/``False``/``None``),
    resolved from the same ``hub_connected_flag`` helper the hub connectivity
    entity uses, so the two surfaces cannot disagree. This is what lets a
    dashboard card or a template gate on a known-stale reading without the
    integration deciding to hide it. It deliberately does not affect
    availability: a hub outage self-heals within seconds of reattachment, and
    hiding every reading would cost history gaps and template errors for a
    transient condition. The key is always present, even when nothing else
    is, so a template can test it without first testing for its existence.
    """
    attrs: dict[str, Any] = {}
    info = (coordinator.data or {}).get("sensors", {}).get(sensor_key) or {}

    firmware_version = info.get("firmware_version")
    if firmware_version:
        attrs["firmware_version"] = firmware_version

    data = info.get("data") or {}
    if "device_timestamp" in data:
        attrs["device_timestamp"] = data["device_timestamp"]
        attrs["timestamp_method"] = data.get("timestamp_method")
        attrs["timestamp_source"] = data.get("timestamp_source", "server")
    elif "server_timestamp" in data:
        attrs["device_timestamp"] = data["server_timestamp"]
        attrs["timestamp_source"] = data.get("timestamp_source", "server")

    attrs["hub_connected"] = hub_connected_flag(hub_connectivity_record(coordinator, info.get("mid")))

    return attrs


class RainPointSubDeviceEntity(CoordinatorEntity[RainPointCoordinator]):
    """Coordinator-backed entity bound to a single sub-device.

    ``_device_name_prefix`` only ever reaches a user for a sub-device the cloud
    gave no name, so a subclass changing it would rename those devices in place.
    """

    _attr_should_poll = False
    _device_name_prefix = "Device"

    def __init__(
        self,
        coordinator: RainPointCoordinator,
        sensor_key: str,
        sensor_info: dict,
        base_slug: str,
    ) -> None:
        super().__init__(coordinator)
        self._sensor_key = sensor_key
        self._sensor_info = sensor_info
        self._base_slug = base_slug

    @property
    def _sensor_data(self) -> dict | None:
        sensors = (self.coordinator.data or {}).get("sensors", {})
        info = sensors.get(self._sensor_key)
        if not info:
            return None
        return info.get("data")

    @property
    def available(self) -> bool:
        """Return False while the reading is missing or the sensor is silent.

        A silent entry's data is truthy (it carries silent_state/last_seen), so
        the plain "is not None" check used to read this as available with a
        native_value of None once a previously-reporting device went silent.
        RainPointNotReportingSensor is the one deliberate exception and
        overrides this back to True.
        """
        data = self._sensor_data
        if data is None:
            return False
        return data.get("type") != SILENT_DATA_TYPE

    @property
    def device_info(self) -> DeviceInfo:
        """Represent each subDevice as its own HA device, child of hub."""
        return build_sub_device_info(
            self._sensor_info,
            name_fallback=f"{self._device_name_prefix} {self._sensor_info['addr']}",
        )
