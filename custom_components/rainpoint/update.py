"""Update entities for RainPoint hubs and sub-devices."""

from __future__ import annotations

import logging
from abc import abstractmethod
from datetime import timedelta

from aiohttp import ClientError
from homeassistant.components.update import UpdateDeviceClass, UpdateEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .api import RainPointApiError
from .const import DOMAIN, UNIQUE_ID_PREFIX
from .coordinator import RainPointCoordinator, is_hub_record
from .device import RainPointHubDevice, build_sub_device_info
from .entity import LateEntityAdder, register_late_adder

_LOGGER = logging.getLogger(__name__)

# One small call per device, against firmware that moves on the order of months,
# so this platform polls on its own slow cadence instead of riding the 120s device
# poll. The installed version is already on the device card either way; what this
# cadence governs is only how soon a newly published firmware is noticed.
SCAN_INTERVAL = timedelta(hours=6)


class RainPointFirmwareUpdate(UpdateEntity):
    """Read-only firmware update entity, shared by hubs and sub-devices.

    Deliberately reports rather than installs. The cloud does expose an upgrade
    trigger for hubs, but it names no target version (it applies whatever the
    check is currently offering), no failure response has ever been observed, and
    the failure mode is a half-flashed device rather than a wrong reading.
    Reporting carries nearly all of the value at none of that risk, so INSTALL
    stays off until there is more than one observation to build it on.

    The check also returns a changelog, and it is deliberately not surfaced. It
    arrives in Chinese no matter what locale is asked for: the app sends both
    lang: en and Accept-Language: en-US on the same request and still gets Chinese
    back, so the field is not localized server-side and there is no English to
    fetch. The version pair answers the only question this entity exists to
    answer, which is whether to go and open the app.

    Subclasses supply `_fetch_firmware_info` and their own identity; everything
    that reads the answer lives here, because the two endpoints return the same
    envelope.
    """

    _attr_device_class = UpdateDeviceClass.FIRMWARE
    _attr_has_entity_name = True
    _attr_name = "Firmware Update"

    @abstractmethod
    async def _fetch_firmware_info(self) -> dict:
        """Return this device's raw firmware check payload.

        Abstract rather than a NotImplementedError body, because that error is
        not in the tuple async_update catches and this platform adds with
        update_before_add: a subclass that forgot to supply one would raise out
        of its first update and be dropped for the run of the config entry. The
        marker moves that failure to construction, where a test sees it.
        """

    @property
    def available(self) -> bool:
        """Report unavailable until a check has actually answered.

        The device bases hardcode True, on the reasoning that a device row exists
        for as long as the config entry does. That is right for entities reading a
        poll snapshot and wrong here: every value this entity shows comes from one
        cloud call, and a stale version pair presented as live would be worse than
        presenting nothing.
        """
        return self._available

    async def async_update(self) -> None:
        """Refresh both versions from a single firmware check.

        Nothing raised out of here, by design. This platform is added with
        update_before_add, and Home Assistant aborts an entity whose first update
        raises, permanently for that run of the config entry. A transport blip at
        setup would therefore not delay the entity, it would delete it until the
        next restart, so the transport surface is caught rather than propagated.
        RainPointThrottledError is a RainPointApiError subclass and is covered by
        the first arm; ValueError carries the malformed-JSON case.
        """
        try:
            data = await self._fetch_firmware_info()
        except (RainPointApiError, ClientError, TimeoutError, ValueError) as err:
            self._available = False
            _LOGGER.debug("Firmware check failed: %s", err)
            return

        installed = data.get("softVer")
        info = data.get("info")
        if info is None:
            # The cloud saying this device is current. The envelope is byte-identical
            # either way, so a null "info" is the entire signal, and echoing the
            # installed version is what renders as "up to date".
            offered = installed
        elif isinstance(info, dict):
            # An offer with no version in it is malformed. Leaving latest unset
            # shows unknown, which beats echoing the installed version and
            # rendering a real upgrade as up to date.
            offered = info.get("versionName")
        else:
            # Non-null but not an object. Non-null is the contract's way of saying
            # an upgrade exists, so this must not fall back to the installed
            # version and report the device as current.
            offered = None

        # Truthiness rather than "is not None" throughout: this cloud sends ""
        # rather than omitting a key, which is the shape is_hub_record was written
        # for, and an empty version string is not a version.
        self._attr_installed_version = installed or None
        self._attr_latest_version = offered or None
        # Neither release_summary nor release_url is set. The changelog is
        # Chinese-only (see above) and the only URL in the response is a direct
        # firmware binary, which is not somewhere to send a user.
        self._available = bool(installed)


class RainPointHubFirmwareUpdate(RainPointFirmwareUpdate, RainPointHubDevice):
    """Firmware update entity for a RainPoint hub, addressed by mid."""

    def __init__(self, client, hub_info: dict) -> None:
        """Key the entity to the hub and start out with nothing to report."""
        RainPointHubDevice.__init__(self, hub_info)
        self._client = client
        # The hub already has a "Firmware Version" sensor on the plain _firmware
        # suffix, so this one cannot reuse it.
        self._attr_unique_id = f"{self._attr_unique_id}_firmware_update"
        # RainPointHubDevice.__init__ sets this False for the coordinator-driven
        # hub entities; this platform is the exception that fetches its own data.
        self._attr_should_poll = True
        self._available = False

    async def _fetch_firmware_info(self) -> dict:
        """Check this hub's firmware, addressed by its mid."""
        return await self._client.get_hub_firmware_info(self._hub_info.get("mid"))


class RainPointSubFirmwareUpdate(RainPointFirmwareUpdate):
    """Firmware update entity for one sub-device, addressed by sid.

    Deliberately not a RainPointSubDeviceEntity, unlike every other sub-device
    platform. That base is a CoordinatorEntity, and CoordinatorEntity's own
    should_poll hard-returns False from ahead of the _attr_-reading one in the
    MRO, so an entity mixed onto it is never polled no matter what it sets. This
    entity reads nothing from a poll snapshot anyway: every value it shows comes
    from its own cloud call, so it takes the device page from the same helper
    that base uses and keeps its own polling.

    Built for every sub-device carrying a sid, with no model gate. The RainPoint
    app offers a "check for updates" link on the HTV210B alone, but the endpoint
    answers for the RF models too (verified against an HTV245FRF and an HCS026FRF
    on 2026-08-25), so a model list here would only be a list to maintain. A model
    RainPoint never publishes firmware for simply reads as up to date forever.

    No silence gate: the check is a cloud call keyed on sid, so it answers the
    same for a sub-device that has stopped reporting as for one that has not, and
    such a device keeps a usable version pair while its readings are gone. That
    only holds because the late adder below reaches it at all -- a silent device
    is absent from the sensors snapshot for its first SILENT_DEBOUNCE_POLLS polls,
    so the setup pass alone would miss exactly the population this paragraph is
    about.
    """

    def __init__(self, client, sensor_key: str, sensor_info: dict) -> None:
        """Key the entity to the sub-device and start out with nothing to report."""
        self._client = client
        self._sensor_info = sensor_info
        self._sid = sensor_info.get("sid")
        # sensor_key rather than a slug rebuilt from the record's identity
        # fields: it is already "{hid}_{mid}_{addr}", and rebuilding it here
        # would put a malformed record under a different id than the one
        # build_sub_device_info raises on.
        self._attr_unique_id = f"{UNIQUE_ID_PREFIX}{sensor_key}_firmware_update"
        # The sub-device already carries a "Firmware Version" diagnostic sensor
        # on the _firmware_version suffix, so this one cannot reuse it.
        self._attr_should_poll = True
        self._available = False

    @property
    def device_info(self) -> DeviceInfo:
        """Put this entity on the sub-device's own page, not its hub's."""
        return build_sub_device_info(self._sensor_info)

    async def _fetch_firmware_info(self) -> dict:
        """Check this sub-device's firmware, addressed by its sid."""
        return await self._client.get_sub_firmware_info(self._sid)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up one firmware update entity per hub and per addressable sub-device."""
    data = hass.data[DOMAIN][entry.entry_id]
    coordinator: RainPointCoordinator = data["coordinator"]
    client = data.get("client")
    if client is None:
        _LOGGER.error("No API client available; skipping update entity setup")
        return

    hubs_cfg = coordinator.data.get("hubs", [])
    # Mirrors button.py: a non-list snapshot is rejected outright, and a non-dict
    # member is skipped early rather than crashing setup on hub.get().
    if not isinstance(hubs_cfg, list):
        _LOGGER.error("Expected hubs to be a list, got %s; skipping update entity setup", type(hubs_cfg).__name__)
        return

    entities: list[RainPointFirmwareUpdate] = [
        RainPointHubFirmwareUpdate(client, hub_info)
        for hub_info in hubs_cfg
        if isinstance(hub_info, dict) and is_hub_record(hub_info)
    ]

    def build(key: str, info: dict) -> list:
        """Return the update entity for one sub-device, or none if it has no sid."""
        if info.get("sid") is None:
            return []
        return [RainPointSubFirmwareUpdate(client, key, info)]

    # Wrapped so a late-added entity gets the same first check the setup pass
    # gets. LateEntityAdder calls its adder with the entities alone, and this
    # platform's SCAN_INTERVAL is measured in hours, so an entity added without
    # it would sit unavailable until the next tick rather than until the next
    # poll.
    adder = LateEntityAdder(
        coordinator,
        lambda new: async_add_entities(new, update_before_add=True),
        build,
        "update",
    )
    # Published before anything is emitted, so the removal sweep can ask this
    # adder what it created for a key that later vanishes. Without it the rows
    # are in no ledger, which leaves them out of the departed-key removal and
    # keeps the sub-device's device registry row from ever emptying.
    register_late_adder(data, adder)

    sensors_cfg = coordinator.data.get("sensors", {})
    if isinstance(sensors_cfg, dict):
        for key, info in sensors_cfg.items():
            if isinstance(info, dict):
                entities.extend(adder.collect(key, info))
    else:
        _LOGGER.error("Expected sensors to be a dict, got %s; skipping sub-device update entities", type(sensors_cfg).__name__)

    # Registered unconditionally, for the same reason select.py, number.py and
    # valve.py do: a sub-device silent at setup is absent from the sensors
    # snapshot for its first SILENT_DEBOUNCE_POLLS polls and produces nothing
    # here, and it must gain its entity once it starts reporting rather than on
    # the next restart.
    entry.async_on_unload(coordinator.async_add_listener(adder.async_on_coordinator_update))

    _LOGGER.info("Added %d update entities", len(entities))
    async_add_entities(entities, update_before_add=True)
