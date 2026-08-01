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

    The emitted set is deliberately never pruned. A key vanishing from the
    coordinator does not remove the entities already registered for it, so
    forgetting the key would let a later reappearance offer the same unique_id
    a second time. It is bounded by the number of distinct entities the
    installation has ever produced in one session.
    """

    def __init__(
        self,
        coordinator: RainPointCoordinator,
        async_add_entities: Callable[[list], None],
        build: Callable[[str, dict], list],
    ) -> None:
        """Wrap a platform's per-key builder in add-once bookkeeping."""
        self._coordinator = coordinator
        self._async_add_entities = async_add_entities
        self._build = build
        self._emitted: set[str] = set()

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
        return fresh

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


class RainPointSubDeviceEntity(CoordinatorEntity):
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
