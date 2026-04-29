import logging
from typing import Literal

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import RainPointClient
from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

_RELOAD_FAILED_MSG = "Failed to reload RainPoint integration"

_NOTIF_SUCCESS = ("RainPoint Reload Complete", "rainpoint_reload_success")
_NOTIF_PARTIAL = ("RainPoint Reload Partial", "rainpoint_reload_partial")
_NOTIF_FAILED = ("RainPoint Reload Failed", "rainpoint_reload_error")

_ReloadStatus = Literal["success", "partial", "failed"]
_RELOAD_STATUS_NOTIFS: dict[_ReloadStatus, tuple[str, str]] = {
    "success": _NOTIF_SUCCESS,
    "partial": _NOTIF_PARTIAL,
    "failed": _NOTIF_FAILED,
}

PLATFORMS: list[str] = ["sensor", "select", "valve", "number", "switch"]

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    """Legacy YAML setup - not used."""
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up RainPoint from a config entry."""
    session = async_get_clientsession(hass)

    area_code = entry.data["area_code"]
    email = entry.data["email"]
    password = entry.data["password"]

    client = RainPointClient(area_code, email, password, session)
    # Restore tokens if present
    client.restore_tokens(entry.data)

    # Simple: one coordinator per config entry
    from .coordinator import RainPointCoordinator

    coordinator = RainPointCoordinator(hass, client, entry)

    await coordinator.async_config_entry_first_refresh()

    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][entry.entry_id] = {
        "client": client,
        "coordinator": coordinator,
    }

    # Set up services
    await async_setup_services(hass)

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id, None)
    return unload_ok


async def async_reload_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload a config entry."""
    await async_unload_entry(hass, entry)
    await async_setup_entry(hass, entry)


async def async_supports_reconfigure(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Return True if the integration supports reconfiguration."""
    return True


def _notify(hass: HomeAssistant, notif: tuple[str, str], message: str) -> None:
    """Emit a persistent notification using a (title, notification_id) pair."""
    from homeassistant.components import persistent_notification

    title, notification_id = notif
    persistent_notification.async_create(hass, message, title=title, notification_id=notification_id)


async def _reload_one_entry(hass: HomeAssistant, entry_id: str) -> tuple[bool, str]:
    """Reload a single config entry; return (success, user-facing message)."""
    if await async_reload_integration(hass, entry_id):
        _LOGGER.info("RainPoint integration reloaded successfully via service")
        return True, "RainPoint integration reloaded successfully"
    _LOGGER.error("Failed to reload RainPoint integration via service")
    return False, _RELOAD_FAILED_MSG


async def _reload_all_entries(hass: HomeAssistant, entries) -> tuple[_ReloadStatus, str]:
    """Reload every config entry; return (status, user-facing message).

    Status is "success" when all reloaded, "partial" when some failed, "failed"
    when none reloaded.
    """
    success_count = 0
    for entry in entries:
        if await async_reload_integration(hass, entry.entry_id):
            _LOGGER.info("RainPoint integration '%s' reloaded successfully", entry.title)
            success_count += 1
        else:
            _LOGGER.error("Failed to reload RainPoint integration '%s'", entry.title)

    total = len(entries)
    if success_count == total:
        return "success", f"Successfully reloaded {success_count} RainPoint integration(s)"
    if success_count == 0:
        return "failed", f"Failed to reload all {total} RainPoint integration(s)"
    return "partial", f"Only {success_count} of {total} integrations reloaded successfully"


async def async_setup_services(hass: HomeAssistant) -> None:
    """Set up the RainPoint services."""

    async def reload_service(call) -> dict:
        """Service to reload the RainPoint integration."""
        entry_id = call.data.get("entry_id")

        if entry_id is not None:
            success, message = await _reload_one_entry(hass, entry_id)
            _notify(hass, _NOTIF_SUCCESS if success else _NOTIF_FAILED, message)
            return {"success": success, "message": message}

        entries = hass.config_entries.async_entries(DOMAIN)
        if not entries:
            message = "No RainPoint integrations found to reload"
            _LOGGER.error("No RainPoint entries found to reload")
            _notify(hass, _NOTIF_FAILED, message)
            return {"success": False, "message": message}

        status, message = await _reload_all_entries(hass, entries)
        _notify(hass, _RELOAD_STATUS_NOTIFS[status], message)
        return {"success": status == "success", "message": message}

    hass.services.async_register(
        DOMAIN,
        "reload",
        reload_service,
        schema=vol.Schema({vol.Optional("entry_id"): vol.All(cv.string, vol.Length(min=1))}),
        supports_response=True,
    )


async def async_get_diagnostic_info(hass: HomeAssistant, entry: ConfigEntry) -> dict:
    """Return diagnostic information for this integration."""
    return {
        "entry_id": entry.entry_id,
        "title": entry.title,
        "domain": DOMAIN,
        "supports_reload": True,
    }


async def async_reload_integration(hass: HomeAssistant, entry_id: str) -> bool:
    """Reload the RainPoint integration."""
    _LOGGER.info("Reloading RainPoint integration: %s", entry_id)

    try:
        # Get the config entry
        entry = hass.config_entries.async_get_entry(entry_id)
        if not entry or entry.domain != DOMAIN:
            _LOGGER.error("Invalid entry for reload: %s", entry_id)
            return False

        # Reload the entry
        await hass.config_entries.async_reload(entry_id)
        _LOGGER.info("Successfully reloaded RainPoint integration")
        return True
    except Exception:
        _LOGGER.exception(_RELOAD_FAILED_MSG)
        return False
