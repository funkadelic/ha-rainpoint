import logging
from typing import Any, Literal

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
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
    HUB_IDENTIFIER_PREFIX,
    HUB_UNIQUE_ID_PREFIX,
    PUSH_CONNECTED_UNIQUE_ID_SUFFIX,
    PUSH_LAST_MESSAGE_UNIQUE_ID_SUFFIX,
    UNIQUE_ID_PREFIX,
)
from .coordinator import ORPHANED_KEY_DEBOUNCE_POLLS, first_hub_record, is_hub_record
from .entity import late_adders
from .repairs import OrphanedEntitiesRecord, RainPointOrphanedEntityIssues

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

# Hub identity is spelled {hid}_{mid} everywhere: hub entity unique ids as
# rainpoint_hub_{hid}_{mid}_{suffix}, the hub device identifier as
# hub_{hid}_{mid}, and the sub-device via_device tuple as the same identifier.
# Aliased from const.py rather than spelled again here: device.py and
# hub_entities.py (the writers) build from the same two constants, so this
# migration's matcher cannot silently drift from what the platforms actually
# write. tests/test_hub_identity.py pins the equality, in the same spirit as
# TestMigratableSuffixSet pins the suffix set below.
_HUB_UNIQUE_ID_PREFIX = HUB_UNIQUE_ID_PREFIX
_HUB_IDENTIFIER_PREFIX = HUB_IDENTIFIER_PREFIX

# A closed set, not a prefix-plus-remainder test. hid is shared by every hub in
# a home, so a two-hub home already holds two rows matching the hub unique-id
# prefix, and a rule of the form "migrate any remainder that does not already
# start with this hub's mid" would rewrite the sibling hub's
# {mid}_connectivity row into a row carrying a foreign mid segment, destroying
# that entity's identity and orphaning its recorder history. Exact membership
# cannot do that on any hub's pass. tests/test_hub_identity.py asserts this set
# equals the suffixes the platforms actually build, so it cannot drift.
_HUB_MIGRATABLE_SUFFIXES = frozenset(
    {
        "rssi",
        "device_id",
        "firmware",
        "mac",
        "channel",
        "broadcast",
        PUSH_CONNECTED_UNIQUE_ID_SUFFIX,
        PUSH_LAST_MESSAGE_UNIQUE_ID_SUFFIX,
    }
)

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


def _fetch_registry_rows(
    get_registry, entries_for_config_entry, hass: HomeAssistant, entry: ConfigEntry, sweep: str
) -> tuple[Any | None, list]:
    """Return (registry, rows) for one config entry, or (None, []) if unreadable.

    Shared by both registry sweeps. The registry accessors are passed in
    rather than chosen here so each sweep keeps its own registry (entity vs
    device) and its own patch surface, while the guard around them exists
    once. Failure returns a registry of None, which every caller treats as
    "skip this sweep entirely", rather than raising into config-entry setup.
    That nullable first element is the load-bearing part of the return type:
    both callers branch on it.

    `sweep` names the caller in the failure log and does nothing else.
    """
    try:
        registry = get_registry(hass)
        return registry, list(entries_for_config_entry(registry, entry.entry_id))
    except Exception as exc:
        _LOGGER.debug("Registry lookup failed; skipping %s: %s", sweep, exc)
        return None, []


def _read_current_sensors(coordinator, consequence: str) -> dict:
    """Return the current poll's sensor records, or {} if they cannot be read.

    Shared by both registry sweeps, which degrade to {} to opposite effect.
    Nothing here implements that difference: `consequence` is a log-only
    label, and the divergence lives entirely in what each caller does with an
    empty mapping. In the generic sweep, each row-removal reason function
    returns its toggle-off reason before it ever looks at `sensors`, so the
    toggle-off path still removes every row; in the parenting sweep, the
    membership guard fails for every row, so nothing is cleared.
    """
    try:
        return (coordinator.data or {}).get("sensors", {}) if coordinator is not None else {}
    except Exception as exc:
        _LOGGER.debug("Coordinator data unreadable; %s: %s", consequence, exc)
        return {}


def _read_current_hubs(coordinator) -> list | None:
    """Return the current poll's top-level hub records, or None if unreadable.

    Sibling of _read_current_sensors with one deliberate difference in the
    return type: None means only that the read raised, and nothing else. A poll
    that legitimately carried no hub records returns an empty list, which is a
    successful observation of zero candidates.

    That distinction is load-bearing rather than pedantic. A getDeviceByHid
    response can omit a hub the previous poll listed, and the coordinator builds
    its hub list purely from that response, so a real install can poll an empty
    or hub-less list repeatedly. Collapsing that into None would make the
    residual re-key's caller treat every such poll as "could not look" and
    re-run a full two-registry sweep on every poll and every pushed frame,
    indefinitely, on exactly the installs that already hold a residual. Callers
    must test `is None`, never `not hubs`.

    A None coordinator raises into the same handler and gets the same verdict,
    so no separate None test is needed here.

    The shape is normalized here rather than at each consumer. Every caller
    reaches straight for hub.get(...), and is_hub_record is typed for a dict, so
    a record that is not one raises AttributeError inside the caller rather than
    inside this guard. That escapes the residual re-key, which documents that it
    never raises, and aborts config entry setup. The records come from cloud
    JSON that nothing validates on the way in, so their type is assumed rather
    than known. select.py already refuses a non-list hubs value for the same
    reason.
    """
    try:
        hubs = (coordinator.data or {}).get("hubs", []) or []
        if not isinstance(hubs, list):
            _LOGGER.debug("Coordinator hub records are not a list; treating this pass as zero candidates")
            return []
        return [hub for hub in hubs if isinstance(hub, dict)]
    except Exception as exc:
        _LOGGER.debug("Coordinator hub records unreadable; treating this pass as unable to look: %s", exc)
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
    the registry lookup (_fetch_registry_rows), the read of the coordinator's
    current sensors (_read_current_sensors), and each row's keep-or-remove
    decision and removal are guarded independently, so none of them can
    propagate out of config-entry setup.
    """
    generic_enabled = entry.options.get(CONF_GENERIC_ENTITIES_ENABLED, False)
    control_enabled = entry.options.get(CONF_GENERIC_CONTROL_ENABLED, False)

    registry, rows = _fetch_registry_rows(
        er.async_get, er.async_entries_for_config_entry, hass, entry, "the generic entity sweep"
    )
    if registry is None:
        return

    # Degrades to no sensors rather than aborting the sweep. This data only
    # feeds the graduation check on the toggle-on path, where an unresolvable
    # model already means "leave the row alone"; aborting instead would also
    # abandon the toggle-off path, which must remove every generic row and
    # needs none of this data to do it.
    sensors = _read_current_sensors(coordinator, "sweeping without graduation data")

    for row in rows:
        # The reason lookup reads the coordinator's sensor records, which come
        # from the RainPoint payload and are only assumed to be dicts. A row whose
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


def _domain_sensor_key(row) -> str | None:
    """Return the row's DOMAIN-scoped identifier value, or None.

    None covers both a row carrying no DOMAIN identifier and a row whose
    identifiers value is not a collection of 2-tuples, so the malformed case
    has one named home rather than reaching the caller's broad guard.
    """
    for identifier in row.identifiers:
        if isinstance(identifier, tuple) and len(identifier) == 2 and identifier[0] == DOMAIN:
            return identifier[1]
    return None


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

    Only the clearing direction is swept here, and the opposite direction is
    repaired on a narrower schedule than this once claimed. A device that
    should gain a link it does not have is handled by the ordinary DeviceInfo
    path, because a real tuple value is not UNDEFINED, but Home Assistant
    writes a DeviceInfo only while adding an entity. So the gaining direction
    lands at first registration or after a reload, not on any sweep: when
    _reconcile_sub_device_parents_on_updates calls this from its listener,
    there is no platform setup following it to repair anything. Nothing here
    ever writes a via_device_id other than None.

    Idempotent, and run on every setup plus, through that listener, on an
    update that surfaces a new sensor key. It re-evaluates every time rather
    than running once at a boundary, and it has to: a later cloud re-key can
    mis-parent a device again, so this sweep must self-heal. Hub identity is
    the opposite case and is handled by async_migrate_entry instead, because
    nothing upstream has any say in this integration's id scheme, so it can
    only be wrong once.

    Scoped to devices present in the current poll on purpose. A registry row
    whose sensor key the current poll does not mention is left alone rather
    than swept: widening this to every registry row regardless of the
    current poll would treat a device absent for a single poll as
    parentless, deciding by side effect a question this phase deliberately
    leaves open. This scope also makes a hub device row unreachable without
    any explicit exclusion: a hub's identifier carries no addr segment and is
    therefore never a sensor key, so the lookup below simply never finds it.

    Synchronous and never raises, for the same reasons
    _remove_stale_generic_entities is, and through the same two shared
    guards: the registry fetch (_fetch_registry_rows), the coordinator data
    read (_read_current_sensors), and each row's decision are guarded
    independently, so a registry or payload problem can never abort
    config-entry setup or leave the remaining rows unswept.
    """
    registry, rows = _fetch_registry_rows(
        dr.async_get, dr.async_entries_for_config_entry, hass, entry, "the sub-device parenting reconcile"
    )
    if registry is None:
        return

    # Degrades to no sensors rather than aborting the sweep. This is the
    # opposite degradation direction from the generic-entity sweep: there,
    # empty data must still let the toggle-off path remove rows; here, the
    # only mutation available is destructive, so empty data must mean "clear
    # nothing".
    sensors = _read_current_sensors(coordinator, "clearing nothing this setup")

    for row in rows:
        try:
            via_device_id = getattr(row, "via_device_id", None)
            if not via_device_id:
                # Nothing to clear means no call, which is what makes a
                # repeat sweep a genuine no-op rather than a no-op-shaped
                # rewrite.
                continue

            candidate_key = _domain_sensor_key(row)
            if candidate_key is None:
                continue

            if candidate_key not in sensors:
                # Not in this poll: deliberately leave it alone, so a device
                # absent for a single poll is never treated as parentless.
                continue
            record = sensors[candidate_key]

            # The record shape is only assumed, not guaranteed: it is built
            # from a cloud payload. A non-dict is a payload problem, and
            # skipping it here says so, rather than letting an AttributeError
            # reach the guard below and be logged as a reconcile failure. Same
            # defensive filter sensor.py and valve.py already apply.
            if not isinstance(record, dict) or record.get("hub_paired", True):
                continue

            registry.async_update_device(row.id, via_device_id=None)
            _LOGGER.debug("Cleared stale via_device_id on device %s (sensor key %s)", row.id, candidate_key)
        except Exception as exc:
            _LOGGER.debug("Could not reconcile device registry row %s: %s", getattr(row, "id", None), exc)
            continue


def _reconcile_sub_device_parents_on_updates(hass: HomeAssistant, entry: ConfigEntry, coordinator) -> None:
    """Run the parenting reconcile now, then again on every coordinator update.

    A setup-time sweep alone cannot reach the device this whole path exists
    for. The Bluetooth-only sub-device reports no status at all, so it is a
    silent device: _build_silent_subdevice returns None until the addr has
    been omitted for SILENT_DEBOUNCE_POLLS consecutive polls, which means its
    sensor key is not in coordinator.data["sensors"] on the first refresh.
    The sweep's scope guard reads that as "not in this poll, leave it alone",
    and nothing sweeps again, so the stale via_device_id survives forever.
    The DeviceInfo half cannot rescue it either: the entity the late-add
    listener creates several polls later omits via_device, and an omitted
    via_device is UNDEFINED, which Home Assistant's device-update path skips.

    Re-running on coordinator updates is the same mechanism, and the same
    reason, as the late entity adders in sensor.py, valve.py and number.py:
    anything that depends on a device appearing after the first poll needs a
    listener, because setup runs exactly once.

    The listener sweeps only when a sensor key has appeared that was not
    there at the previous sweep, and this narrowing is deliberate. The sweep's
    only mutation is destructive and irreversible within the session: Home
    Assistant writes a DeviceInfo's via_device only while adding an entity, so
    a link cleared for a device whose entities already exist stays cleared
    until a reload, no matter what later polls report. The verdict driving the
    clear, is_hub_record, reads identity fields straight off the cloud
    response, so a single degraded response that blanks did, mac, productKey
    and model on a real hub is indistinguishable from the Bluetooth wrapper
    record. Sweeping on every update would let any one such response
    permanently unparent a genuine sub-device. A newly surfaced key is the
    only case this listener exists to serve, and restricting to it keeps the
    exposure close to the setup-only sweep's while still reaching the silent
    device. It also keeps the sweep off the hub-connectivity push path
    entirely, which never changes sensors at all.

    What this deliberately does not do is correct a device whose parenting
    changes while it is already present and unchanged in the poll. That is the
    same case the setup-only design left to the next reload, and settling it
    is the removal counterpart tracked separately, not this listener's job.

    Within a sweep there is no throttling and no memo: a row with no
    via_device_id short-circuits before any registry write, so a repeat sweep
    over settled devices makes no calls at all, and the clearing write cannot
    re-arm itself, because the row it clears reads back cleared.
    """
    _reconcile_sub_device_parents(hass, entry, coordinator)
    # Seeded from the same snapshot the sweep above just acted on, so the
    # first update cannot re-present an already-swept key as new.
    swept_keys = set(_read_current_sensors(coordinator, "seeding an empty swept-key set").keys())

    @callback
    def _on_coordinator_update() -> None:
        """Re-sweep only when this update surfaced a sensor key the last one did not."""
        nonlocal swept_keys
        current_keys = set(_read_current_sensors(coordinator, "treating this update as surfacing nothing").keys())
        if current_keys - swept_keys:
            _reconcile_sub_device_parents(hass, entry, coordinator)
        # Assigned on every update, not only on a sweep, so a key that
        # disappears and returns counts as newly surfaced again.
        swept_keys = current_keys

    entry.async_on_unload(coordinator.async_add_listener(_on_coordinator_update))


def _read_aged_out_keys(coordinator) -> frozenset[str]:
    """Return the sensor keys the coordinator says have aged out, or an empty set.

    Sibling of _read_current_sensors and degrades the same way: an unreadable
    coordinator means this update offers nothing for removal, which is the
    safe direction for a surface whose only outcome is a deletion offer.

    The guard is not decorative. Coordinator stand-ins are common on this
    path, and one that predates the accessor or answers with something that
    is not iterable raises here rather than at some later, less obvious point.
    """
    try:
        return frozenset(coordinator.aged_out_sensor_keys()) if coordinator is not None else frozenset()
    except Exception as exc:
        _LOGGER.debug("Aged-out sensor keys unreadable; offering nothing for removal this update: %s", exc)
        return frozenset()


def _build_orphaned_entity_records(entry_store: dict, entry_id: str, aged_out: frozenset[str]) -> list:
    """Translate this session's adder ledgers into plain records for the card.

    Scope rule, stated as a prohibition rather than a description: a key with
    no recorded unique_id yields no record, so a registry row this session's
    adders did not emit can never be named by a card, never reach the fix
    flow, and never be removed. That protects two populations by
    construction -- every row from a previous session, and every row written
    under a previous unique_id shape -- neither of which any ledger holds.

    A record is built for every session key, not only the aged-out ones, with
    `orphaned` carrying the verdict. That is what lets the manager clear a
    card for a key that came back, and the clear has to be unconditional on
    the manager's side because a fresh manager after a reload has no memory of
    what a prior session raised; deleting an id it never raised is already a
    no-op, while skipping the delete would strand that card forever.

    Each adder is read behind its own guard, so one malformed adder cannot
    abort the sweep for the others.
    """
    unique_ids: dict[str, set[str]] = {}
    descriptors: dict[str, dict] = {}
    for adder in late_adders(entry_store):
        try:
            ledger = adder.ledger
            # The ledger is a class, not a mapping: keys() is its own named
            # accessor and there is nothing to iterate directly, so SIM118's
            # "drop the .keys()" advice does not apply here.
            for key in ledger.keys():  # noqa: SIM118
                unique_ids.setdefault(key, set()).update(ledger.unique_ids_for(key))
                descriptors.setdefault(key, ledger.descriptor_for(key))
        except Exception as exc:
            _LOGGER.debug("Skipping an unreadable late adder while building orphaned entity records: %s", exc)

    return [
        OrphanedEntitiesRecord(
            entry_id=entry_id,
            sensor_key=key,
            addr=descriptors[key].get("addr"),
            model=descriptors[key].get("model"),
            sub_name=descriptors[key].get("sub_name"),
            hub_name=descriptors[key].get("hub_name"),
            entity_count=len(ids),
            missed_polls=ORPHANED_KEY_DEBOUNCE_POLLS,
            orphaned=key in aged_out,
        )
        for key, ids in unique_ids.items()
    ]


def _sync_orphaned_entity_issues(hass: HomeAssistant, entry: ConfigEntry, coordinator, manager) -> None:
    """Reconcile the orphaned-entity cards against the coordinator's verdict.

    Never raises. Every read below is guarded, and this runs inside a
    coordinator listener where an exception would break the update for every
    other consumer of that notification.
    """
    try:
        entry_store = (hass.data.get(DOMAIN) or {}).get(entry.entry_id) or {}
        records = _build_orphaned_entity_records(entry_store, entry.entry_id, _read_aged_out_keys(coordinator))
        manager.async_sync(records)
    except Exception as exc:
        _LOGGER.debug("Orphaned entity sweep failed; leaving every card exactly as it is: %s", exc)


def _sync_orphaned_entity_issues_on_updates(hass: HomeAssistant, entry: ConfigEntry, coordinator) -> None:
    """Publish the removal executor, sweep once, then sweep on every update.

    This listener is its own rather than a share of the parenting reconcile's.
    That one arms on sensor keys *appearing*; this one cares about keys
    *disappearing*, so sharing would mean widening that gate and changing its
    exposure profile, on the one surface in this integration that can delete.

    Registered before the platform forward, alongside the two existing
    wrappers, so it holds a defined position relative to the late adders that
    register from inside that forward: coordinator listeners fire in
    registration order, so this always runs ahead of every adder on an update.

    Unlike the two existing wrappers it walks no registry at all on the update
    path. Every registry mutation lives in the fix flow, behind a human's
    confirmation, so the per-update cost is a read of in-memory bookkeeping
    and the accepted cost of a third listener is smaller here than for either
    of them. That is also why it needs no arming gate: both the raise and the
    clear are idempotent, so running on every update, including every pushed
    frame, is safe.
    """
    manager = RainPointOrphanedEntityIssues(hass)

    def _remove(sensor_key: str) -> int:
        """Remove this config entry's rows for one sensor key."""
        return _remove_orphaned_key_rows(hass, entry, sensor_key)

    try:
        hass.data[DOMAIN][entry.entry_id]["orphan_entity_remover"] = _remove
    except Exception as exc:
        _LOGGER.debug("Could not publish the orphaned entity remover; its cards will remove nothing: %s", exc)

    _sync_orphaned_entity_issues(hass, entry, coordinator, manager)

    @callback
    def _on_coordinator_update() -> None:
        """Re-reconcile every card against this update's verdict."""
        _sync_orphaned_entity_issues(hass, entry, coordinator, manager)

    entry.async_on_unload(coordinator.async_add_listener(_on_coordinator_update))

    @callback
    def _withdraw_cards() -> None:
        """Withdraw this entry's orphan cards as it unloads.

        The issue registry is not per config entry and only is_persistent
        decides survival across a restart, so a card raised before a reload
        survives the reload while every structure that could clear it is
        rebuilt empty: the manager's active set, the adder ledgers whose
        contents are the only record source here, and the coordinator's
        absence counters. A departed key is absent from the hub's enumeration,
        so no fresh ledger can record it and no fresh record can mention it,
        which leaves the stale-set sweep with nothing to sweep. The card would
        then sit there for the life of the install offering a Submit that
        resolves to an executor with nothing in scope.

        Never raises: this runs on the unload path, where an exception would
        block the teardown of everything registered after it.
        """
        try:
            manager.async_clear_all()
        except Exception as exc:
            _LOGGER.debug("Could not withdraw the orphaned entity cards on unload: %s", exc)

    entry.async_on_unload(_withdraw_cards)


def _device_row_for_sensor_key(rows, sensor_key: str):
    """Return the device row whose DOMAIN identifier is this sensor key, or None.

    Resolved through _domain_sensor_key rather than by building an identifier
    tuple and testing membership. That function already owns the malformed
    identifiers case and already proves the device-row-to-sensor-key round
    trip both existing sweeps rely on, so a second spelling of the same match
    would only be somewhere for the two to drift apart.

    Each row is guarded on its own, matching the per-row discipline of the
    sweeps beside this one: a row whose identifiers cannot be read at all is
    skipped rather than aborting the search for the rest.
    """
    for row in rows:
        try:
            if _domain_sensor_key(row) == sensor_key:
                return row
        except Exception as exc:
            _LOGGER.debug("Could not read identifiers on device row %s: %s", getattr(row, "id", None), exc)
    return None


def _device_row_is_empty(entity_rows, device_id) -> bool:
    """Return True when no row in entity_rows sits on this device row.

    Scope is exactly what the caller hands in: entity_rows is the
    config-entry-scoped set, so this answers "empty for this config entry" and
    deliberately not "empty". Both halves of that are consequences worth
    naming. A row also carrying an entity from another config entry reads as
    empty here, which is why the caller releases the row with a config-entry
    scoped update rather than an unscoped removal: this predicate establishes
    nothing about a foreign entry's entities and must never be relied on to.
    A row carrying any entity from this entry does not read as empty,
    including one this session never emitted and including one that is
    disabled.

    device_id is read with getattr so a row shape that carries no device_id at
    all reads as "not on this device" rather than raising.
    """
    return not any(getattr(row, "device_id", None) == device_id for row in entity_rows)


def _remove_orphaned_key_rows(hass: HomeAssistant, entry: ConfigEntry, sensor_key: str) -> int:
    """Remove this session's rows for one sensor key, and drop the bookkeeping.

    The only removal executor on this path, reached only from the fix flow's
    confirm step, which is reached only after a human submits the form.

    Scoped by two independent guards: the registry lookup is config-entry
    scoped, so there is no whole-registry scan, and a row must additionally
    carry a unique_id this session's adders recorded for this exact key, which
    no foreign row can satisfy.

    The sub-device's own device registry row goes too, but only once it carries
    no entity for this config entry. Removing the entity rows alone would leave
    an empty device card on the user's device page, which is a different
    cosmetic defect rather than a fix.

    Never raises: the entry store read, both registry lookups, each row
    removal and each adder's bookkeeping drop are guarded independently,
    because this runs inside a Repairs flow step.
    """
    try:
        entry_store = hass.data[DOMAIN][entry.entry_id]
    except Exception as exc:
        _LOGGER.debug("Entry store unreadable; removing nothing for sensor key %s: %s", sensor_key, exc)
        return 0

    adders = late_adders(entry_store)
    # (domain, unique_id) rather than unique_id alone. Entity registry
    # uniqueness is per domain, so two rows in different domains may
    # legitimately carry the same id and matching on the id alone would take
    # both. No such collision exists across the four id shapes this integration
    # builds today, so this closes a latent hole rather than a live one, but an
    # exact-pair list is the whole reason this path does not reason about
    # unique_id prefixes. The domain comes from the adder that recorded the id,
    # which is fixed per platform.
    doomed: set[tuple[str, str]] = set()
    for adder in adders:
        try:
            doomed.update((adder.domain, unique_id) for unique_id in adder.ledger.unique_ids_for(sensor_key))
        except Exception as exc:
            _LOGGER.debug("Skipping an unreadable late adder while resolving sensor key %s: %s", sensor_key, exc)

    if not doomed:
        # No adder in this session emitted anything for this key, so there is
        # nothing in scope and no bookkeeping to drop. Removing nothing is the
        # correct answer rather than an error: the ledgers are this path's only
        # authority on what it may take, and a card that outlived the session
        # whose ledgers named its rows has no scope left.
        #
        # Logged at warning rather than returned silently, because Home
        # Assistant deletes a fixable issue itself once its flow finishes: the
        # user pressed a button that said it would remove leftover entities,
        # the card disappeared, and nothing happened. That needs a breadcrumb
        # in the log, and the surviving rows still need removing by hand.
        _LOGGER.warning(
            "Nothing in scope for sensor key %s; its leftover entity rows were not removed. "
            "The card outlived the session that recorded them, so remove them from the entity registry by hand",
            sensor_key,
        )
        return 0

    registry, rows = _fetch_registry_rows(
        er.async_get, er.async_entries_for_config_entry, hass, entry, "the orphaned entity sweep"
    )
    if registry is None:
        return 0

    removed = 0
    # Ids whose registry row this sweep tried and failed to take. They are what
    # decides whether the bookkeeping may be dropped below: a row that raised
    # is still registered, still holds its unique_id, and releasing that id
    # would let a returning key offer it a second time.
    failed: set[tuple[str, str]] = set()
    for row in rows:
        entity_id = getattr(row, "entity_id", "") or ""
        # Derived from the entity_id rather than read off a domain attribute,
        # because that is how Home Assistant itself derives it and it holds for
        # any row shape the registry hands back. A row with no dot yields the
        # whole string, which matches no adder domain and is therefore skipped.
        row_key = (entity_id.split(".", 1)[0], getattr(row, "unique_id", None))
        if row_key not in doomed:
            continue
        try:
            registry.async_remove(row.entity_id)
            removed += 1
        except Exception as exc:
            failed.add(row_key)
            _LOGGER.debug("Failed to remove orphaned entity %s: %s", getattr(row, "entity_id", None), exc)

    # The emptiness test below has to read the registry as it stands after the
    # removals above, so the rows are re-fetched rather than reused. The list
    # the loop just iterated still names every row it removed, so testing
    # against it would conclude the device row always still carries entities:
    # a silent no-op that a test asserting only the end state would not catch.
    surviving_registry, surviving_rows = _fetch_registry_rows(
        er.async_get, er.async_entries_for_config_entry, hass, entry, "the orphaned device row check"
    )
    # An unreadable device registry yields no rows, so this answers None and
    # the device half is skipped behind the debug line _fetch_registry_rows has
    # already written.
    _device_registry, device_rows = _fetch_registry_rows(
        dr.async_get, dr.async_entries_for_config_entry, hass, entry, "the orphaned device row sweep"
    )
    candidate = _device_row_for_sensor_key(device_rows, sensor_key)
    candidate_id = getattr(candidate, "id", None) if candidate is not None else None

    # A surviving_registry of None means the re-read could not be made at all.
    # Emptiness is a claim that has to be positively established, and a failed
    # lookup establishes nothing, so the row stays exactly where it is.
    # _device_registry is tested directly rather than relied on transitively.
    # A failed device-registry fetch already yields no rows, so no candidate can
    # resolve and this branch is unreachable with a None registry, but that is
    # three inferences away from the call below. Stating it here keeps the guard
    # local to the thing it guards.
    if surviving_registry is not None and _device_registry is not None and candidate_id:
        if _device_row_is_empty(surviving_rows, candidate_id):
            # Scoped to this config entry, deliberately, because the emptiness
            # test above cannot make the unscoped call safe: it answers "empty
            # for this config entry", while async_remove_device's cascade
            # deletes every entity on the row whose own config entry is in the
            # row's config_entries set. A row shared with a second RainPoint
            # entry -- two accounts resolving the same home -- reads as empty
            # here while still carrying that entry's entities, and the cascade
            # would take them and their recorder history without them ever
            # having been named on the card the user confirmed.
            #
            # async_update_device drops only this entry's link. The device
            # registry removes the row itself when this entry was its last one,
            # so the single-entry case is unchanged, and merely unlinks when
            # another entry still owns it, which takes only the entities of
            # entries that were dropped -- none, since this entry has none left.
            try:
                _device_registry.async_update_device(candidate_id, remove_config_entry_id=entry.entry_id)
                _LOGGER.info("Released the emptied device row %s for sensor key %s", candidate_id, sensor_key)
            except Exception as exc:
                _LOGGER.debug("Failed to release the emptied device row %s: %s", candidate_id, exc)
        else:
            # Info rather than debug: this is the one outcome a user sees as a
            # device page that survived a confirmed removal, carrying whatever
            # rows no adder ledger named -- the generic control switches, which
            # have no late adder, are the known case. Making that visible
            # without turning debug logging on is the difference between an
            # explicable leftover and an apparent failure.
            _LOGGER.info(
                "Device row %s still carries entities for this config entry; leaving it in place",
                candidate_id,
            )

    # Ordering above is load bearing, in both directions. Moving this forget
    # ahead of the device half would drop the ledger while the emptiness test
    # still needs the entity ids it resolved from; moving the device removal
    # ahead of the entity removals would make the emptiness test trivially
    # false and take nothing at all.
    #
    # Forgotten at the same moment the rows go, and only then. It still runs
    # when this sweep removed nothing, because a row that was already absent
    # from the fetch leaves the bookkeeping as the only thing left to correct.
    # It does not run when a removal was attempted and raised: that row is
    # still registered and still holds its unique_id, and both adders' add-once
    # bookkeeping is per key rather than per id, so there is no partial forget
    # to make. Releasing the key would let a returning device offer a live
    # unique_id a second time, which Home Assistant rejects outright.
    if failed:
        _LOGGER.warning(
            "Kept the bookkeeping for sensor key %s: %d leftover row(s) could not be removed and still hold their unique ids",
            sensor_key,
            len(failed),
        )
    else:
        for adder in adders:
            try:
                adder.forget(sensor_key)
            except Exception as exc:
                _LOGGER.debug("Could not forget sensor key %s on a late adder: %s", sensor_key, exc)

    # Carries the key and an integer count only, never a cloud-supplied
    # string, matching the log discipline the coordinator's counting side uses.
    _LOGGER.info("Removed %d leftover entity row(s) for sensor key %s", removed, sensor_key)
    return removed


def _hub_identity(identifier: str) -> tuple[str, str | None] | None:
    """Split a hub device identifier into (hid, mid), or None if it is not one.

    Accepts both shapes on purpose. The old shape hub_{hid} yields a mid of
    None; the migrated shape hub_{hid}_{mid} yields both. Anything else,
    including every sub-device identifier (which carries no hub_ prefix) and a
    hub_-prefixed value that is neither shape, returns None.

    Recognising the migrated shape is what makes an interrupted run
    recoverable. The two registries save on independent debounced timers, so a
    crash can flush the device half without the entity half; a helper that only
    knew the old shape would skip that hub on the retry and strand its entity
    rows in the old shape permanently, with the version boundary already burned.

    Takes a str. Both call sites pass `_domain_sensor_key(row) or ""`, which is
    a str on every path, so no isinstance test is needed or wanted here.
    """
    if not identifier.startswith(_HUB_IDENTIFIER_PREFIX):
        return None
    remainder = identifier[len(_HUB_IDENTIFIER_PREFIX) :]
    if remainder and "_" not in remainder:
        return remainder, None
    parts = remainder.split("_")
    if len(parts) == 2 and all(parts):
        return parts[0], parts[1]
    return None


def _hub_row_identity(row) -> tuple[str, str | None] | None:
    """Return the (hid, mid) a device row's DOMAIN identifier carries, or None."""
    return _hub_identity(_domain_sensor_key(row) or "")


def _resolve_hub_mid(hub_row, hid: str, entity_rows: list, device_rows: list) -> str | None:
    """Recover a hub's mid from registry state alone, or None.

    The config entry does not store the mid and no cloud call is available
    here, because the migration runs before any coordinator or platform exists.
    Two ordered sources, both already on disk:

      1. A sub-device parented to this exact device row. Its identifier is
         {hid}_{mid}_{addr}, and the mid in it is this hub's. Scoped to this
         row, so it stays unambiguous in a home holding several hubs, and it
         names the hub the sub-devices actually hang off.
      2. The connectivity entity's unique id, which has carried both segments
         since it shipped. This source is second because it is ambiguous where
         the first is not: both hubs in a two-hub home wrote one shared
         hub_{hid} device row but two connectivity rows, so this source alone
         would assign the surviving row to whichever hub registry iteration
         reached first.

    Every candidate must be a decimal integer before it is used at all, and the
    tie-break is numeric. Both rules matter. The candidates from source 2 are
    substrings sliced out of persisted unique ids, so a numeric sort key over
    an unfiltered candidate raises, and this function is called outside the
    per-row guards, which would turn the whole entry into a migration error. A
    lexical sort would meanwhile order "10" before "9" and disagree with the
    residual sweep's tie-break over the same home. A dropped candidate simply
    does not exist as far as the caller is concerned.

    What the losing hub in a two-hub home loses, and what it does not:

      - The device row. It loses nothing it had, because it never had a row of
        its own; both hubs shared this one. On the next setup it registers a
        fresh row under its own identifier with a default name and no area, and
        its sub-devices re-parent through the ordinary DeviceInfo path. Two
        device pages either way.
      - The entity rows. These were not shared. rainpoint_hub_{hid}_mac and its
        siblings were registered by whichever hub Home Assistant accepted
        first, and that is the hub whose readings sit in the recorder history
        behind them. Neither source knows which hub wrote them, so when this
        picks the other one, all eight rows are silently re-attributed: each row
        keeps its identity and its history, but the hub it represents changes,
        and the hub that produced that history gets fresh empty rows on the next
        start. Nothing detects this and nothing repairs it. It is accepted
        because the registry carries no record of which hub wrote a row, which
        is the defect this migration exists to stop creating.
    """
    for row in device_rows:
        parts = (_domain_sensor_key(row) or "").split("_")
        if getattr(row, "via_device_id", None) == hub_row.id and len(parts) == 3 and parts[0] == hid and parts[1].isdigit():
            return parts[1]

    prefix = f"{_HUB_UNIQUE_ID_PREFIX}{hid}_"
    suffix = "_connectivity"
    candidates = []
    for row in entity_rows:
        unique_id = getattr(row, "unique_id", "") or ""
        middle = unique_id[len(prefix) : -len(suffix)]
        if unique_id.startswith(prefix) and unique_id.endswith(suffix) and middle.isdigit():
            candidates.append(middle)
    return min(candidates, key=int) if candidates else None


def _migrate_hub_entity_unique_ids(registry, entity_rows: list, hid: str, mid: str) -> None:
    """Move this hub's old-shape entity rows onto the {hid}_{mid} spelling.

    Rows are selected by exact membership in _HUB_MIGRATABLE_SUFFIXES. That
    closed set covers a repeat run (whose remainder is already {mid}_{suffix}),
    a partially completed run, the sibling hub's connectivity row, and any
    future id shape added to this namespace, all by construction rather than by
    a rule about what a remainder looks like.

    A collision skips the row and continues. The registry raises ValueError
    when the target unique id is already taken; the losing row is left intact
    rather than removed, because removing it would destroy the recorder history
    and customizations this migration exists to preserve. The exception is
    caught narrowly rather than as a bare Exception: this is a known outcome
    with a known meaning, not a degraded sweep.
    """
    prefix = f"{_HUB_UNIQUE_ID_PREFIX}{hid}_"
    for row in entity_rows:
        unique_id = getattr(row, "unique_id", "") or ""
        if not unique_id.startswith(prefix):
            continue
        remainder = unique_id[len(prefix) :]
        if remainder not in _HUB_MIGRATABLE_SUFFIXES:
            continue
        target = f"{prefix}{mid}_{remainder}"
        try:
            registry.async_update_entity(row.entity_id, new_unique_id=target)
            _LOGGER.debug("Re-keyed hub entity %s to %s", row.entity_id, target)
        except ValueError as exc:
            _LOGGER.warning(
                "Could not re-key hub entity %s to %s; leaving it on its old id: %s",
                row.entity_id,
                target,
                exc,
            )


def _move_hub_device_row(registry, row, hid: str, mid: str) -> bool:
    """Re-key one hub device row in place; return whether it now carries the pair.

    Re-keying identifiers leaves device.id unchanged, so every already
    registered sub-device's via_device_id keeps resolving and no via_device
    sweep is needed anywhere.

    The failure log is at warning, not debug, and names the row it left behind.
    The realistic cause is another row already holding the target identifier,
    which leaves the user with two device pages for one hub: the original
    keeping its area, its user-set name and every parented sub-device while
    carrying an identifier nothing writes any more, and the new one carrying
    the live entities. The device registry offers no in-place merge, so nothing
    repairs that automatically, and a debug line would make it invisible on a
    default install.
    """
    target = f"{_HUB_IDENTIFIER_PREFIX}{hid}_{mid}"
    try:
        registry.async_update_device(row.id, new_identifiers={(DOMAIN, target)})
        return True
    except Exception as exc:
        _LOGGER.warning(
            "Could not re-key hub device row %s (device id %s) to %s; it stays on its old identifier: %s",
            _domain_sensor_key(row),
            getattr(row, "id", None),
            target,
            exc,
        )
        return False


async def async_migrate_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Move hub identity from the home id to the {hid}_{mid} pair, in place.

    Three hub-level platforms build one entity per real hub while the id inside
    carries only the home id, so a second hub in one home produces duplicate
    unique ids and Home Assistant drops the second hub's entities silently.
    This runs once, at the config-entry version boundary, before any platform
    sets up.

    Ordering is load-bearing: the device row moves before that hub's entity
    rows. Reversing it creates a second hub device row and abandons the
    original along with its area, its user-set name and every sub-device
    parented to it.

    The entity pass is reachable from either identifier shape, and is a sibling
    of the device pass rather than nested inside it. The two registries save on
    independent debounced timers, so a crash can flush the device half without
    the entity half; a retry that required the row to still be old-shape would
    strand those entity rows forever, with the version already burned.

    A unique-id collision skips that row and logs; it never deletes the losing
    row and never aborts the loop. The mid is recovered from registry state
    because the config entry does not carry it and no cloud call is available
    this early.

    A False return is expensive and is reserved for the one case where nothing
    at all was read. Home Assistant sets the entry to MIGRATION_ERROR and does
    not call async_setup_entry, so the integration does not load for that
    session at all, every entity goes unavailable, and the integrations page
    shows a migration failure. It is a retry, but not a quiet one, which is
    exactly why a hub whose mid cannot be resolved does not use it: that hub is
    left untouched and finished later by _complete_hub_identity_rekey, from the
    coordinator's own hub record.
    """
    entity_registry, entity_rows = _fetch_registry_rows(
        er.async_get, er.async_entries_for_config_entry, hass, entry, "the hub identity migration"
    )
    device_registry, device_rows = _fetch_registry_rows(
        dr.async_get, dr.async_entries_for_config_entry, hass, entry, "the hub identity migration"
    )
    # One combined test rather than two sequential ones: either registry being
    # unreadable means the same thing and takes the same exit.
    if entity_registry is None or device_registry is None:
        return False

    working_set = []
    for row in device_rows:
        identity = _hub_row_identity(row)
        if identity is not None:
            working_set.append((row, identity[0], identity[1]))

    for row, hid, mid in working_set:
        if mid is None:
            mid = _resolve_hub_mid(row, hid, entity_rows, device_rows)
            if mid is None:
                _LOGGER.warning(
                    "Could not resolve the mid for hub device %s; leaving it on its old identity until a poll supplies one",
                    _domain_sensor_key(row),
                )
                continue
            if not _move_hub_device_row(device_registry, row, hid, mid):
                # Leave this hub's entity rows alone so the row set stays
                # internally consistent.
                continue
        _migrate_hub_entity_unique_ids(entity_registry, entity_rows, hid, mid)

    # Home Assistant does not write the version itself; an integration that
    # omits this re-runs its migration on every restart.
    hass.config_entries.async_update_entry(entry, version=2)
    return True


def _resolve_residual_hub_mid(row, hid: str, real_hubs: list, entity_rows, device_rows) -> str | None:
    """Resolve one old-shape hub row's mid, preferring the registry-backed sources.

    The coordinator's own record is the authoritative source and the one the
    migration could not reach, because it ran before any coordinator existed. It
    is consulted only as a fallback so the registry-backed answer wins where
    both exist, which keeps this path and _resolve_hub_mid from ever picking
    different hubs for the same home.

    Same isdigit filter and same numeric tie-break as _resolve_hub_mid. The
    filter earns its place twice over: it stops a non-numeric segment reaching
    key=int, and it stops a negative mid winning a minimum tie-break outright.
    hid is an int on a coordinator record and a str off an identifier, so both
    sides are normalized before they meet.
    """
    candidates = [str(hub.get("mid")) for hub in real_hubs if str(hub.get("hid")) == hid and str(hub.get("mid")).isdigit()]
    return _resolve_hub_mid(row, hid, entity_rows, device_rows) or (min(candidates, key=int) if candidates else None)


def _log_residual_mid_decline(hid: str, warned_hids: set[str] | None) -> None:
    """Log a hub whose mid no source could supply, loudly once and quietly after.

    warned_hids is the caller's closure-local record of which hids have already
    been announced. The first decline for a hid is a WARNING, because a hub that
    never resolves stays split across two device rows permanently and the
    version boundary is already burned; every later decline is DEBUG, because
    there can be one per coordinator update, forever.

    A caller passing None owns no closure to dedupe against (a direct test call,
    say), so it gets the WARNING every time rather than silent DEBUG. Announcing
    twice is a much cheaper failure than announcing never.
    """
    if warned_hids is not None and hid in warned_hids:
        _LOGGER.debug("No mid available for hub %s this pass; leaving it for a later poll", hid)
        return
    if warned_hids is not None:
        warned_hids.add(hid)
    _LOGGER.warning(
        "No mid available yet for hub %s; its identity re-key is deferred until a later poll supplies one",
        hid,
    )


def _residual_old_shape_hids(device_registry, examined: list) -> frozenset[str]:
    """Re-read the rows this pass touched and report which are still old-shape.

    Derived by re-reading what the registry actually holds, never from a tally
    of attempted moves. A hub whose device move raised was attempted and did not
    move; a set built from intent would omit it, report nothing outstanding, and
    let the caller latch shut with that hub's identity permanently split across
    two device rows.
    """
    residual = set()
    for row in examined:
        identity = _hub_row_identity(device_registry.async_get(row.id) or row)
        if identity is not None and identity[1] is None:
            residual.add(identity[0])
    return frozenset(residual)


def _complete_hub_identity_rekey(
    hass: HomeAssistant,
    entry: ConfigEntry,
    coordinator,
    warned_hids: set[str] | None = None,
) -> frozenset[str] | None:
    """Finish the hub identity re-key for any hub the migration could not resolve.

    This is the residual half of the version-boundary migration, not a
    replacement for it: async_migrate_entry still owns the mechanism and does
    the work on every install whose mid it can read off the registries. This
    exists because ConfigEntry.async_migrate returns early once the version
    matches, so a hub left unresolved there would keep its old ids permanently,
    behind one warning line nobody reads.

    warned_hids tracks, per config entry, which hids have already had their
    decline logged at WARNING. The caller (_complete_hub_identity_rekey_on_updates)
    owns this set as closure-local state and passes it in on every call, so the
    first decline for a given hid is loud and every later one -- of which there
    can be one per coordinator update, forever, until a poll supplies a mid --
    stays at DEBUG. A caller that passes None (e.g. a direct test call) gets a
    WARNING on every decline instead of silent DEBUG; there is no closure to
    dedupe against.

    The first pass must stay ahead of the platform forward. Placed after it, the
    platforms would already have created a second hub device row under the new
    identifier and fresh entity rows under the new unique ids, losing exactly
    the history the migration exists to preserve.

    Candidate mids come only from hub records that pass is_hub_record. The
    coordinator injects hid into every top-level record the cloud returns and
    filters nothing, and the Bluetooth wrapper record is kept in that list on
    purpose so its sub-devices stay discoverable. Wrapper records carry a mid,
    so an unfiltered candidate list would re-key the hub device row to the
    wrapper's mid on a single-hub home with a paired Bluetooth valve, and the
    lowest-mid tie-break would make it deterministic rather than rare.

    Returns three states, and the two falsy ones must never be collapsed:

      - None: could not look. A registry was unreadable, or the read of the
        coordinator's hub records raised. Nothing was decided, so nothing may
        be concluded.
      - A non-empty frozenset: looked, and these hids are still old-shape.
      - An empty frozenset: looked, and there is nothing outstanding. This is
        the only state that may close the caller's latch.

    An empty frozenset is a positive claim that this pass found nothing left to
    do; None asserts only that it could not look. Treating the second as the
    first would close the latch on an observation that was never made, with the
    version boundary already burned.

    Never raises, like the two sweeps beside it.
    """
    device_registry, device_rows = _fetch_registry_rows(
        dr.async_get, dr.async_entries_for_config_entry, hass, entry, "the residual hub identity re-key"
    )
    entity_registry, entity_rows = _fetch_registry_rows(
        er.async_get, er.async_entries_for_config_entry, hass, entry, "the residual hub identity re-key"
    )
    if device_registry is None or entity_registry is None:
        return None

    hub_records = _read_current_hubs(coordinator)
    if hub_records is None:
        return None
    real_hubs = [hub for hub in hub_records if is_hub_record(hub)]

    examined = []
    for row in device_rows:
        identity = _hub_row_identity(row)
        if identity is None or identity[1] is not None:
            continue
        hid = identity[0]
        examined.append(row)

        mid = _resolve_residual_hub_mid(row, hid, real_hubs, entity_rows, device_rows)
        if mid is None:
            _log_residual_mid_decline(hid, warned_hids)
            continue
        if _move_hub_device_row(device_registry, row, hid, mid):
            _migrate_hub_entity_unique_ids(entity_registry, entity_rows, hid, mid)

    return _residual_old_shape_hids(device_registry, examined)


def _complete_hub_identity_rekey_on_updates(hass: HomeAssistant, entry: ConfigEntry, coordinator) -> None:
    """Run the residual re-key now, then again on updates until it is settled.

    A setup-time pass alone is one-shot per setup, and this one's input can be
    absent at first-refresh time: a getDeviceByHid response can omit a hub the
    previous poll listed, and the coordinator builds its hub list purely from
    that response. A first refresh landing inside such an outage would strand
    the residual hub until the next restart, which on a Home Assistant install
    can be months.

    Diverges from the parenting reconcile's listener in one direction and
    converges on it in another. It arms less often: that one arms
    unconditionally, this one arms nothing at all on an install whose setup pass
    positively found no residual, which is essentially every install. Once
    armed it gates the same way, on the input having actually changed, rather
    than re-reading both registries on every notification. Both matter because
    listeners fire on every pushed frame as well as every poll.

    Gating on the hub mapping loses nothing; it is not a cost-for-correctness
    trade. Neither registry-backed source can newly resolve while the hub row is
    still old-shape. A sub-device cannot acquire a link to it, because the
    via_device tuple it emits names the migrated identifier and Home Assistant
    writes UNDEFINED for a via device that does not exist. The connectivity
    entity cannot appear either, because it is built only by one-shot platform
    setup from a record that would already have resolved the hid on the setup
    pass. So the coordinator's hub list gaining a real record for that hid is
    the only event that can change the answer, and that is what the gate
    watches.

    The listener's later runs may land after the platform forward even though
    the first pass must not, and that is safe by registration order rather than
    by any blanket claim that entity creation is one-shot: it is one-shot for
    the hub platforms, but sensor.py, valve.py and number.py all carry late
    adders that create sub-device entities on later polls. This wrapper
    registers before the forward and those adders register from inside it, and
    coordinator listeners fire in registration order, so the re-key runs ahead
    of every adder on every update. If one ever did run first anyway, an
    unresolvable via_device is UNDEFINED, which the device-update path skips, so
    an existing parent link is preserved rather than cleared; and a competing
    new-shape row would make async_update_device raise into the per-row guard,
    leaving both rows intact rather than merging or dropping either.

    All per-setup state is closure-local. This integration permits more than one
    config entry, and a module-level latch would let one entry's clean pass
    permanently silence another's residual sweep, invisibly on every
    single-account install. warned_hids joins pending and last_hub_mids as a
    third piece of that closure-local state: it is what turns the very first
    decline for a given hid, on this entry, into a WARNING instead of a DEBUG
    line silent from its first run, without making every later update log one
    too.
    """
    warned_hids: set[str] = set()
    pending = _complete_hub_identity_rekey(hass, entry, coordinator, warned_hids)
    # Seeded from the same snapshot the pass above acted on, so the first update
    # cannot re-present an unchanged mapping as changed.
    last_hub_mids = frozenset(
        (str(hub.get("hid")), str(hub.get("mid"))) for hub in (_read_current_hubs(coordinator) or []) if is_hub_record(hub)
    )

    @callback
    def _on_coordinator_update() -> None:
        """Re-run the residual re-key only when its one possible input changed."""
        nonlocal pending, last_hub_mids
        if pending == frozenset():
            return
        current_hub_mids = frozenset(
            (str(hub.get("hid")), str(hub.get("mid"))) for hub in (_read_current_hubs(coordinator) or []) if is_hub_record(hub)
        )
        # `pending is None` means the previous pass could not look, so no change
        # to the hub list would signal that a retry is worthwhile.
        if pending is None or current_hub_mids != last_hub_mids:
            pending = _complete_hub_identity_rekey(hass, entry, coordinator, warned_hids)
        # Assigned on every update, not only on the ones that swept, so a hub
        # that vanishes and returns counts as changed again.
        last_hub_mids = current_hub_mids

    # Armed unless this pass positively observed that nothing is left to do. A
    # bare truthiness test here would leave a pass that could not read a
    # registry unarmed, concluding from an observation it never made.
    if pending != frozenset():
        entry.async_on_unload(coordinator.async_add_listener(_on_coordinator_update))


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

    # Both passes below run after the coordinator's first refresh, so the
    # sensor records they read are already populated, and before any platform
    # is forwarded, so this first pass of each cannot race a platform adding
    # entities. That is the whole story for the generic sweep, which runs
    # once. The parenting pass also arms a listener, and its later runs do
    # share updates with the late entity adders in sensor.py, valve.py and
    # number.py. They are ordered by listener registration, not by platform
    # forwarding: this listener is registered first, so it runs first, and the
    # adders' DeviceInfo omits via_device for an unparented record anyway.
    # Ahead of the platform forward for the same reason, and more sharply: this
    # is the window in which no DeviceInfo has been written yet this session, so
    # a hub the version-boundary migration could not resolve is re-keyed before
    # any platform can create a second device row under the new identifier.
    # The third wrapper belongs here rather than after the forward for the
    # ordering reason above: the late adders register their listeners from
    # inside that forward, so registering this one first is what puts the
    # orphaned-entity sweep ahead of every adder on every later update, and it
    # is also where each adder publishes itself for the sweep to read.
    _complete_hub_identity_rekey_on_updates(hass, entry, coordinator)
    _remove_stale_generic_entities(hass, entry, coordinator)
    _reconcile_sub_device_parents_on_updates(hass, entry, coordinator)
    _sync_orphaned_entity_issues_on_updates(hass, entry, coordinator)

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
