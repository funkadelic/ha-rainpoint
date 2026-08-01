import logging
from typing import Literal

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import RainPointClient, is_hand_written_model
from .api.mqtt import RainPointMqttClient
from .const import (
    CONF_GENERIC_CONTROL_ENABLED,
    CONF_GENERIC_ENTITIES_ENABLED,
    CONF_PUSH_ENABLED,
    CONF_TOKEN,
    DOMAIN,
    GENERIC_CONTROL_OVERRIDE_DISABLED,
    GENERIC_CONTROL_UNIQUE_ID_MARKER,
    GENERIC_UNIQUE_ID_MARKER,
    UNIQUE_ID_PREFIX,
)
from .coordinator import first_hub_record

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
    hub = first_hub_record(hubs)
    if hub is None:
        return None, None, None, None
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


def _generic_row_removal_reason(unique_id, generic_enabled: bool, sensors: dict) -> str | None:
    """Return why this registry row should be removed, or None to keep it.

    The prefix-and-marker match is the second of the sweep's two independent
    scoping guards, so a row belonging to another integration is rejected
    here even if the config-entry-scoped lookup that produced it ever
    regressed.
    """
    if not isinstance(unique_id, str):
        return None
    if not unique_id.startswith(UNIQUE_ID_PREFIX) or GENERIC_UNIQUE_ID_MARKER not in unique_id:
        return None
    if not generic_enabled:
        return "generic entities are disabled"

    base_slug = unique_id[len(UNIQUE_ID_PREFIX) :].split(GENERIC_UNIQUE_ID_MARKER, 1)[0]
    model = (sensors.get(base_slug) or {}).get("model")
    # A base slug absent from the current sensor data resolves to no model,
    # which is not evidence that the model graduated. This falls out of
    # is_hand_written_model(None) being False on its own, but is stated here
    # so a later reader does not "fix" it into a removal.
    if not is_hand_written_model(model):
        return None
    return "the model now has a hand-written decoder"


def _generic_control_row_removal_reason(unique_id, control_enabled: bool, sensors: dict) -> str | None:
    """Return why this control-namespace registry row should be removed, or None to keep it.

    Shaped exactly like _generic_row_removal_reason, and scoped by the same
    prefix-and-marker guard -- a row belonging to another integration is
    rejected here even if the config-entry-scoped lookup that produced it
    ever regressed. A companion duration row matches the same guard: it
    carries the control marker too, so it is decided by this function
    alongside the control row it companions.

    Three removal conditions, in order: the control option is off, which
    removes every control-namespace row for this entry regardless of
    resolvability; the base slug's model is now in the hand-written set
    (graduation, mirroring the sensor branch's rule); and the base slug's
    model paired with its modelCode is in the committed override set, so a
    maintainer force-disabling a misrouted variant cannot leave its
    actuating entities behind even while the option stays on. A base slug
    absent from the current sensor data resolves to no model and no
    modelCode, which is not evidence of graduation or of an override, and
    keeps the row -- same reasoning the sensor branch already documents.
    """
    if not isinstance(unique_id, str):
        return None
    if not unique_id.startswith(UNIQUE_ID_PREFIX) or GENERIC_CONTROL_UNIQUE_ID_MARKER not in unique_id:
        return None
    if not control_enabled:
        return "generic control is disabled"

    base_slug = unique_id[len(UNIQUE_ID_PREFIX) :].split(GENERIC_CONTROL_UNIQUE_ID_MARKER, 1)[0]
    record = sensors.get(base_slug) or {}
    model = record.get("model")
    if is_hand_written_model(model):
        return "the model now has a hand-written decoder"

    # Deferred import: generic_control reaches sensor.py's RainPointSensorBase
    # transitively through generic_entities, so a top-level import here would
    # pull the whole sensor platform into this module's import graph.
    from .generic_control import _override_key

    if _override_key(model, record.get("model_code")) in GENERIC_CONTROL_OVERRIDE_DISABLED:
        return "generic control for this variant has been force-disabled by the maintainer"
    return None


def _remove_stale_generic_entities(hass: HomeAssistant, entry: ConfigEntry, coordinator) -> None:
    """Remove generic-namespace registry rows that should no longer exist.

    Runs on every config-entry setup, not only on a toggle transition, so a
    row orphaned by a crash mid-toggle-off is cleaned on the next start.
    Covers two independently governed unique_id namespaces sharing one sweep:
    the read-only sensor namespace (CONF_GENERIC_ENTITIES_ENABLED) and the
    control namespace (CONF_GENERIC_CONTROL_ENABLED), the latter also
    covering its companion duration rows. When a namespace's own toggle is
    off, every row in that namespace for this entry is removed; when it is
    on, a row is removed only when its model has since gained a hand-written
    decoder (graduation) or -- control namespace only -- its (model,
    modelCode) variant is in the committed override list, so a graduated or
    force-disabled model can never keep a stale or actuating unverified
    entity beside its new trusted state. Neither toggle can reach the
    other's namespace: the control marker nests inside the sensor marker,
    so every row is dispatched to the control reason
    function first and only falls through to the sensor reason function when
    the control marker is absent -- reversing that order would let the
    sensor toggle govern control rows it was never supposed to touch.

    Scoped by two independent guards -- the config-entry-scoped registry
    lookup and a unique_id prefix-and-marker match -- so the sweep can never
    reach another config entry or another integration even if the prefix
    logic itself has a bug. There is no whole-registry scan.

    Synchronous on purpose: both registry helpers it uses are callbacks, so
    there is nothing to await and no suspension point at which a reload
    could interleave with a partially completed removal set. Never raises:
    the registry lookup, the read of the coordinator's current sensors, and
    each row's keep-or-remove decision and removal are guarded independently,
    so none of them can propagate out of config-entry setup.
    """
    generic_enabled = entry.options.get(CONF_GENERIC_ENTITIES_ENABLED, False)
    control_enabled = entry.options.get(CONF_GENERIC_CONTROL_ENABLED, False)

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
        # The reason lookup reads the coordinator's sensor records, which come
        # from the vendor payload and are only assumed to be dicts. A row whose
        # record is malformed is skipped like any other unremovable row rather
        # than abandoning the rest of the sweep.
        try:
            unique_id = getattr(row, "unique_id", None)
            # Dispatch order is load-bearing: the control marker nests
            # inside the sensor marker, so a control-namespace unique_id also
            # contains the sensor marker substring. Testing for the control
            # marker first, and only falling through to the sensor reason
            # function when it is absent, is what keeps each option confined
            # to its own namespace -- reversed, the sensor toggle could delete
            # or spare control rows it was never supposed to touch.
            if isinstance(unique_id, str) and GENERIC_CONTROL_UNIQUE_ID_MARKER in unique_id:
                reason = _generic_control_row_removal_reason(unique_id, control_enabled, sensors)
            else:
                reason = _generic_row_removal_reason(unique_id, generic_enabled, sensors)
        except Exception as exc:
            _LOGGER.debug("Could not decide on generic entity row %s: %s", getattr(row, "entity_id", None), exc)
            continue
        if reason is None:
            continue
        try:
            registry.async_remove(row.entity_id)
            _LOGGER.debug("Removed stale generic entity %s: %s", row.entity_id, reason)
        except Exception as exc:
            _LOGGER.debug("Failed to remove stale generic entity %s: %s", row.entity_id, exc)


def _reconcile_sub_device_parents(hass: HomeAssistant, entry: ConfigEntry, coordinator) -> None:
    """Clear a stale via_device_id on an already-registered sub-device.

    Exists because dropping a DeviceInfo's via_device key cannot, on its own,
    correct a device that is already in the registry: Home Assistant resolves
    both an omitted via_device and an explicit via_device=None to UNDEFINED
    when building a DeviceInfo, and its own device-update path skips every
    UNDEFINED value. A registry row that already carries a via_device_id
    therefore keeps it forever unless something writes an explicit None over
    it -- only DeviceRegistry.async_update_device, called through the object
    dr.async_get(hass) returns, with via_device_id passed as None explicitly,
    does that.

    Only the clearing direction needs a sweep. The opposite direction, a
    device that should gain a link it does not have, is already handled by
    the ordinary DeviceInfo path: a real tuple value is not UNDEFINED, so
    Home Assistant's own device-update path writes it during the platform
    setup that follows this call. Nothing here ever writes a via_device_id
    other than None.

    Idempotent and run on every setup, not gated behind a config-entry
    version bump or an async_migrate_entry path: a version-boundary migration
    only ever runs once, so it could not self-heal if a later cloud re-key
    mis-parented a device again, and this sweep can, because it re-evaluates
    every time.

    Scoped to devices present in the current poll on purpose. A registry row
    whose sensor key the current poll does not mention is left alone rather
    than swept: widening this to every registry row regardless of the
    current poll would treat a device absent for a single poll as
    parentless, deciding by side effect a question this phase deliberately
    leaves open. This scope also makes a hub device row unreachable without
    any explicit exclusion: a hub's identifier carries no addr segment and is
    therefore never a sensor key, so the lookup below simply never finds it.

    Synchronous and never raises, for the same reasons
    _remove_stale_generic_entities is: the registry fetch, the coordinator
    data read, and each row's decision are guarded independently, so a
    registry or payload problem can never abort config-entry setup or leave
    the remaining rows unswept.
    """
    try:
        registry = dr.async_get(hass)
        rows = list(dr.async_entries_for_config_entry(registry, entry.entry_id))
    except Exception as exc:
        _LOGGER.debug("Device registry lookup failed; skipping sub-device parenting reconcile: %s", exc)
        return

    # Degrades to no sensors rather than aborting the sweep. This is the
    # opposite degradation direction from the generic-entity sweep: there,
    # empty data must still let the toggle-off path remove rows; here, the
    # only mutation available is destructive, so empty data must mean "clear
    # nothing".
    try:
        sensors = (coordinator.data or {}).get("sensors", {}) if coordinator is not None else {}
    except Exception as exc:
        _LOGGER.debug("Coordinator data unreadable; clearing nothing this setup: %s", exc)
        sensors = {}

    for row in rows:
        try:
            via_device_id = getattr(row, "via_device_id", None)
            if not via_device_id:
                # Nothing to clear means no call, which is what makes a
                # repeat sweep a genuine no-op rather than a no-op-shaped
                # rewrite.
                continue

            candidate_key = None
            for identifier in row.identifiers:
                if isinstance(identifier, tuple) and len(identifier) == 2 and identifier[0] == DOMAIN:
                    candidate_key = identifier[1]
                    break
            if candidate_key is None:
                continue

            if candidate_key not in sensors:
                # Not in this poll: under D-06 this means leave it alone.
                continue
            record = sensors[candidate_key]

            if record.get("hub_paired", True):
                continue

            registry.async_update_device(row.id, via_device_id=None)
            _LOGGER.debug("Cleared stale via_device_id on device %s (sensor key %s)", row.id, candidate_key)
        except Exception as exc:
            _LOGGER.debug("Could not reconcile device registry row %s: %s", getattr(row, "id", None), exc)
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

    # Both passes below run before any platform is forwarded, so no platform
    # is adding entities while a registry is being mutated, and after the
    # coordinator's first refresh, so the sensor records they read are
    # already populated.
    _remove_stale_generic_entities(hass, entry, coordinator)
    _reconcile_sub_device_parents(hass, entry, coordinator)

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

    Reload through async_reload rather than calling unload/setup directly.
    Calling them in sequence bypasses Home Assistant's entry state machine, so
    the entry never reaches LOADED, every platform forwarded afterwards raises
    "Config entry was never loaded!" on the next unload, and setup runs outside
    the config-entry context.
    """
    store = hass.data.get(DOMAIN, {}).get(entry.entry_id)
    if store is not None and store.get("options_snapshot") == dict(entry.options):
        return
    await hass.config_entries.async_reload(entry.entry_id)


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
