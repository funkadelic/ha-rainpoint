import logging
from typing import Literal

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import RainPointClient, is_hand_written_model
from .api.mqtt import RainPointMqttClient
from .const import (
    CONF_GENERIC_ENTITIES_ENABLED,
    CONF_PUSH_ENABLED,
    CONF_TOKEN,
    DOMAIN,
    GENERIC_UNIQUE_ID_MARKER,
    UNIQUE_ID_PREFIX,
)

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

PLATFORMS: list[str] = ["sensor", "binary_sensor", "select", "valve", "number", "switch"]

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    """Legacy YAML setup - not used."""
    return True


def _resolve_hub_identity(coordinator) -> tuple[str | None, str | None, int | None, int | None]:
    """Resolve the first hub's deviceName/productKey/mid/hid for the MQTT client.

    Hub discovery already scans all configured homes; this just picks the first
    hub record the coordinator collected. The mid and hid are returned alongside
    the credential-fetch identity because the push payload does not carry the mid
    and the subscribeStatus envelope needs the hub's home id, while the observer
    topic's deviceName is ephemeral, so the client must be told which hub it
    belongs to at construction.
    """
    hubs = (coordinator.data or {}).get("hubs", [])
    if not hubs:
        return None, None, None, None
    hub = hubs[0]
    return hub.get("deviceName"), hub.get("productKey"), hub.get("mid"), hub.get("hid")


def _persist_tokens(hass: HomeAssistant, entry: ConfigEntry, client: RainPointClient) -> None:
    """Write the client's current token back to the config entry.

    The login endpoint is aggressively rate-limited: a single login succeeds,
    but repeated logins in quick succession escalate to a sustained HTTP 403
    block. Runtime re-logins rotate the in-memory token, so persisting it lets a
    later restart, reload, or setup retry reuse a valid token instead of logging
    in again. Only token fields change here, so this must not trigger a reload
    (async_reload_entry ignores data-only changes).
    """
    token_data = client.export_tokens()
    if not token_data.get(CONF_TOKEN):
        return
    if all(entry.data.get(key) == value for key, value in token_data.items()):
        return
    hass.config_entries.async_update_entry(entry, data={**entry.data, **token_data})


def _remove_stale_generic_entities(hass: HomeAssistant, entry: ConfigEntry, coordinator) -> None:
    """Remove generic-namespace registry rows that should no longer exist.

    Runs on every config-entry setup, not only on a toggle transition, so a
    row orphaned by a crash mid-toggle-off is cleaned on the next start. When
    the toggle is off, every generic row for this entry is removed; when it
    is on, only rows whose model has since gained a hand-written decoder are
    removed, so a graduated model can never keep a stale unverified entity
    beside its new trusted one.

    Scoped by two independent guards -- the config-entry-scoped registry
    lookup and a unique_id prefix-and-marker match -- so the sweep can never
    reach another config entry or another integration even if the prefix
    logic itself has a bug. There is no whole-registry scan.

    Synchronous on purpose: both registry helpers it uses are callbacks, so
    there is nothing to await and no suspension point at which a reload
    could interleave with a partially completed removal set. Never raises:
    the registry lookup, the read of the coordinator's current sensors, and
    each single-row removal are guarded independently, so none of them can
    propagate out of config-entry setup.
    """
    generic_enabled = entry.options.get(CONF_GENERIC_ENTITIES_ENABLED, False)

    try:
        registry = er.async_get(hass)
        rows = list(er.async_entries_for_config_entry(registry, entry.entry_id))
    except Exception as exc:
        _LOGGER.debug("Entity registry lookup failed; skipping generic entity sweep: %s", exc)
        return

    # Degrades to no sensors rather than aborting the sweep. This data only
    # feeds the graduation check on the toggle-on path, where an unresolvable
    # model already means "leave the row alone"; aborting instead would also
    # abandon the toggle-off path, which must remove every generic row and
    # needs none of this data to do it.
    try:
        sensors = (coordinator.data or {}).get("sensors", {}) if coordinator is not None else {}
    except Exception as exc:
        _LOGGER.debug("Coordinator data unreadable; sweeping without graduation data: %s", exc)
        sensors = {}

    for row in rows:
        unique_id = getattr(row, "unique_id", None)
        if not isinstance(unique_id, str):
            continue
        if not unique_id.startswith(UNIQUE_ID_PREFIX) or GENERIC_UNIQUE_ID_MARKER not in unique_id:
            continue

        if not generic_enabled:
            reason = "generic entities are disabled"
        else:
            base_slug = unique_id[len(UNIQUE_ID_PREFIX) :].split(GENERIC_UNIQUE_ID_MARKER, 1)[0]
            model = (sensors.get(base_slug) or {}).get("model")
            # A base slug absent from the current sensor data resolves to no
            # model, which is not evidence that the model graduated. This
            # falls out of is_hand_written_model(None) being False on its
            # own, but is stated here so a later reader does not "fix" it
            # into a removal.
            if not is_hand_written_model(model):
                continue
            reason = "the model now has a hand-written decoder"

        try:
            registry.async_remove(row.entity_id)
            _LOGGER.debug("Removed stale generic entity %s: %s", row.entity_id, reason)
        except Exception as exc:
            _LOGGER.debug("Failed to remove stale generic entity %s: %s", row.entity_id, exc)
            continue


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up RainPoint from a config entry."""
    session = async_get_clientsession(hass)

    area_code = entry.data["area_code"]
    email = entry.data["email"]
    password = entry.data["password"]

    domain_data = hass.data.setdefault(DOMAIN, {})
    entry_store = domain_data.setdefault(entry.entry_id, {})
    # Snapshot options so the update listener can distinguish an options change
    # (which needs a reload) from a data-only change like token persistence.
    entry_store["options_snapshot"] = dict(entry.options)

    # Reuse the client across ConfigEntryNotReady retries. Home Assistant does
    # not unload the entry between retries, so keeping one client preserves its
    # login cooldown: a throttle armed on one attempt makes the next attempts
    # fast-fail without a network call, instead of a fresh client hammering the
    # rate-limited login endpoint every few seconds.
    client = entry_store.get("client")
    if client is None:
        client = RainPointClient(area_code, email, password, session)
        client.restore_tokens(entry.data)
        # Persist rotated tokens so a later restart/reload/retry reuses a valid
        # token rather than logging in again. Registered once on the client we
        # keep; retries reuse it without re-registering.
        client.register_relogin_listener(lambda: _persist_tokens(hass, entry, client))
        entry_store["client"] = client

    # Simple: one coordinator per config entry
    from .coordinator import RainPointCoordinator

    coordinator = RainPointCoordinator(hass, client, entry)

    await coordinator.async_config_entry_first_refresh()

    # A brand-new client that had to log in here holds a fresh token the relogin
    # listener did not persist (it only fires when a prior token was already
    # held), so persist whatever we now hold.
    _persist_tokens(hass, entry, client)

    entry_store["coordinator"] = coordinator

    # Sweep before any platform is forwarded, so no platform is adding
    # entities while a removal runs, and after the coordinator's first
    # refresh, so the sensor records the graduation branch reads are
    # already populated.
    _remove_stale_generic_entities(hass, entry, coordinator)

    # An options change (e.g. toggling push) reloads through the existing
    # unload->setup path, no bespoke start/stop code path needed.
    entry.async_on_unload(entry.add_update_listener(async_reload_entry))

    if entry.options.get(CONF_PUSH_ENABLED, False):
        hub_device_name, hub_product_key, hub_mid, hub_hid = _resolve_hub_identity(coordinator)
        if hub_device_name and hub_product_key:
            mqtt_client = RainPointMqttClient(
                hass,
                client,
                entry,
                hub_device_name,
                hub_product_key,
                coordinator=coordinator,
                hub_mid=hub_mid,
                hub_hid=hub_hid,
            )
            hass.data[DOMAIN][entry.entry_id]["mqtt_client"] = mqtt_client
            # Registered immediately after construction so it fires even if a
            # later setup step raises.
            entry.async_on_unload(mqtt_client.async_disconnect)
            # An HTTP re-login must trigger an immediate MQTT credential
            # re-fetch + reconnect -- the supervisor never keeps running on
            # credentials the HTTP layer has superseded.
            client.register_relogin_listener(mqtt_client.on_http_relogin)
            # Liveness watchdog: surfaces a silently dead push channel as a
            # repair issue. Detection-only -- it never reconnects and never
            # changes the poll cadence. Torn down alongside the client on unload.
            from .repairs import RainPointPushWatchdog

            watchdog = RainPointPushWatchdog(hass, entry, mqtt_client)
            hass.data[DOMAIN][entry.entry_id]["watchdog"] = watchdog
            entry.async_on_unload(watchdog.async_stop)
            watchdog.start()
            # Backgrounded and never awaited: a broker-unreachable failure must
            # never block or fail config-entry setup. Polling
            # already has entities covered via async_config_entry_first_refresh.
            hass.async_create_background_task(mqtt_client.async_start(), name="rainpoint_mqtt_start")
        else:
            _LOGGER.warning("Push enabled but no hub was found; skipping MQTT connect")

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
    """Reload the entry when its options change.

    Registered as the update listener. Token persistence and the debug
    last-submission timestamp write to entry.data without touching options; a
    reload rebuilds the client and forces a fresh login against the
    rate-limited endpoint, so reload only when the options actually change.
    """
    store = hass.data.get(DOMAIN, {}).get(entry.entry_id)
    if store is not None and store.get("options_snapshot") == dict(entry.options):
        return
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
