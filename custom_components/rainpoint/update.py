"""Update entities for RainPoint hubs."""

from __future__ import annotations

import logging
from datetime import timedelta

from aiohttp import ClientError
from homeassistant.components.update import UpdateDeviceClass, UpdateEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .api import RainPointApiError
from .const import DOMAIN
from .coordinator import RainPointCoordinator, is_hub_record
from .device import RainPointHubDevice

_LOGGER = logging.getLogger(__name__)

# One small call per hub, against firmware that moves on the order of months, so
# this platform polls on its own slow cadence instead of riding the 120s device
# poll. The installed version is already on the device card either way; what this
# cadence governs is only how soon a newly published firmware is noticed.
SCAN_INTERVAL = timedelta(hours=6)


class RainPointHubFirmwareUpdate(UpdateEntity, RainPointHubDevice):
    """Read-only firmware update entity for a RainPoint hub.

    Deliberately reports rather than installs. The cloud does expose an upgrade
    trigger, but it names no target version (it applies whatever the check is
    currently offering), no failure response has ever been observed, and the
    failure mode is a half-flashed hub rather than a wrong reading. Reporting
    carries nearly all of the value at none of that risk, so INSTALL stays off
    until there is more than one observation to build it on.

    Hubs only. The RF sub-devices carry a firmware revision in the poll snapshot,
    already surfaced as their device sw_version, but the RainPoint app offers them no
    update check at all.

    The check also returns a changelog, and it is deliberately not surfaced. It
    arrives in Chinese no matter what locale is asked for: the app sends both
    lang: en and Accept-Language: en-US on the same request and still gets Chinese
    back, so the field is not localized server-side and there is no English to
    fetch. The version pair answers the only question this entity exists to
    answer, which is whether to go and open the app.
    """

    _attr_device_class = UpdateDeviceClass.FIRMWARE
    _attr_name = "Firmware Update"

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

    @property
    def available(self) -> bool:
        """Report unavailable until a check has actually answered.

        RainPointHubDevice hardcodes True, on the reasoning that a hub row exists
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
            data = await self._client.get_hub_firmware_info(self._hub_info.get("mid"))
        except (RainPointApiError, ClientError, TimeoutError, ValueError) as err:
            self._available = False
            _LOGGER.debug("Hub firmware check failed: %s", err)
            return

        installed = data.get("softVer")
        info = data.get("info")
        # Keyed on "info" being present rather than on versionName being truthy.
        # A null "info" is the cloud saying this hub is current, and the envelope
        # is byte-identical either way, so its presence is the entire signal;
        # echoing the installed version there is what renders as "up to date".
        # An offer that arrives without a version is malformed, and falling back
        # to the installed version for it would render a real upgrade as up to
        # date, so it is left unset and shows as unknown instead.
        offered = info.get("versionName") if isinstance(info, dict) else installed

        # Truthiness rather than "is not None" throughout: this cloud sends ""
        # rather than omitting a key, which is the shape is_hub_record was written
        # for, and an empty version string is not a version.
        self._attr_installed_version = installed or None
        self._attr_latest_version = offered or None
        # Neither release_summary nor release_url is set. The changelog is
        # Chinese-only (see the class docstring) and the only URL in the response
        # is a direct firmware binary, which is not somewhere to send a user.
        self._available = bool(installed)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up one firmware update entity per hub."""
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

    entities = [
        RainPointHubFirmwareUpdate(client, hub_info)
        for hub_info in hubs_cfg
        if isinstance(hub_info, dict) and is_hub_record(hub_info)
    ]

    _LOGGER.info("Added %d update entities", len(entities))
    async_add_entities(entities, update_before_add=True)
