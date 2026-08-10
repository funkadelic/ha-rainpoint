"""Diagnostics support for RainPoint config entries and devices.

Home Assistant discovers this module by file presence, the same way it
discovers a platform, and renders a "Download diagnostics" action on the config
entry and on every device page. The download is admin-only.

Two rules shape what goes in the dump, and they are separate on purpose.

Nothing that authenticates is put into the structure at all. The account
password and the persisted tokens are never read here: the entry section
carries the *names* of the keys `entry.data` holds and never their values, so a
credential cannot leak through a field nobody thought to redact.

Everything else is built from an explicit allow-list per record shape rather
than by handing over a cloud record and a list of keys to strip. A deny-list
has to be maintained against a payload nobody in this project controls, and it
silently ships whatever field the vendor adds next. The allow-list inverts
that: an unrecognised field contributes its *name* to `unlisted_keys` and never
its value, so a new vendor field shows up here as a prompt to review it rather
than as an unreviewed disclosure.

`async_redact_data` then runs over the finished structure as a second net for
the identity fields that are allow-listed deliberately (they are worth seeing
as present) but must not be readable. It matches on key name only, which is
exactly why it cannot be the whole story on its own.

The names a user chose are kept, and that is a decision rather than a field
nobody got to. A dump is read by a person working out which device misbehaved,
and a name like "Front Lawn Valve" is the only thing in the structure tying a
record to what its owner sees on screen; an addr and a mid do not. So the rule
here is narrower than the one the logs follow: redact what identifies (a MAC,
an iotId, a productKey, the MAC-derived `deviceName`, a barCode), keep what a
human wrote (the hub and sub-device `name`, the coordinator entry's `hub_name`
and `sub_name`, and the home's `homeName`). The logging house style is stricter
on purpose, because a log line is emitted without anyone asking for it, where a
dump is downloaded deliberately by someone who can open the file before sending
it. `tests/test_diagnostics.py::TestUserChosenNamesSurvive` pins this so the
next edit to `TO_REDACT` has to mean it.
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceEntry

from . import _domain_sensor_key, _hub_identity
from .const import (
    CONF_AREA_CODE,
    CONF_GENERIC_CONTROL_ENABLED,
    CONF_GENERIC_ENTITIES_ENABLED,
    CONF_HIDS,
    CONF_PUSH_ENABLED,
    DOMAIN,
    HUB_IDENTIFIER_PREFIX,
    VERSION,
)

# Values replaced with a redaction marker wherever they appear, at any depth.
#
# Two groups, and they are here for different reasons. Credentials (the first
# group) authenticate; disclosing one is the serious case. The rest identify:
# a MAC, an iotId, a productKey, the MAC-derived `deviceName`, a barCode. They
# address a device rather than describe it, they are worth something to somebody
# with no business on this account, and they read as noise to the person
# actually diagnosing the problem. This follows the decision already taken for
# the command logs, where the DEBUG line was narrowed precisely so deviceName
# and productKey stop travelling with support output.
#
# The names a user chose are deliberately absent from this set, which is the
# one place a reader is likely to expect them. The module docstring says why.
#
# Both spellings of the fields that appear camelCase in a cloud record and
# snake_case in a coordinator sensor entry are listed, because the redactor
# matches literal key names.
TO_REDACT = {
    "access_token",
    "auth",
    "barCode",
    "client_id",
    "clientId",
    "deviceName",
    "device_name",
    "deviceSecret",
    "device_secret",
    "email",
    "iotId",
    "mac",
    "mac1",
    "password",
    "phone",
    "productKey",
    "product_key",
    "refresh_token",
    "token",
    "username",
}

# The top-level (hub or Bluetooth wrapper) record fields carried verbatim.
# "subDevices" is deliberately absent: it is walked separately so each child
# goes through its own allow-list rather than riding along unreviewed.
_HUB_RECORD_FIELDS = frozenset(
    {
        "alerts",
        "attributeKv",
        "barCode",
        "brand",
        "createTime",
        "deviceName",
        "did",
        "displayModel",
        "enabled",
        "function",
        "hardwareVersion",
        "hid",
        "iotId",
        "mac",
        "mac1",
        "mid",
        "model",
        "modelCode",
        "name",
        "param",
        "paramVersion",
        "pcode",
        "planJson",
        "portDescribe",
        "productKey",
        "recich",
        "rid",
        "soft1Ver",
        "softVer",
        "state",
        "style",
        "transportVersion",
    }
)

# The sub-device record fields carried verbatim.
_SUB_DEVICE_FIELDS = frozenset(
    {
        "addr",
        "alerts",
        "attributeKv",
        "channel",
        "createTime",
        "did",
        "displayModel",
        "enabled",
        "function",
        "hardwareVersion",
        "lock",
        "m49",
        "mac",
        "mid",
        "model",
        "modelCode",
        "name",
        "param",
        "paramVersion",
        "pcode",
        "planJson",
        "portDescribe",
        "portNumber",
        "sid",
        "softVer",
        "style",
        "typeFlag",
    }
)

# The coordinator sensor-entry fields carried verbatim. Unlike the two above,
# this shape is built by this integration rather than by the cloud, so a new key
# here is a change someone in this repo made deliberately.
#
# "raw_status" is the exception and gets its own pass below: it is a cloud
# mapping stored whole, so allow-listing the entry that holds it says nothing
# about what is inside it.
_SENSOR_ENTRY_FIELDS = frozenset(
    {
        "addr",
        "data",
        "device_name",
        "firmware_version",
        "hid",
        "home_name",
        "hub_name",
        "hub_paired",
        "mid",
        "model",
        "model_code",
        "product_key",
        "raw_status",
        "sub_name",
    }
)

# The cloud status entry nested inside a sensor entry's "raw_status". Three
# fields, and the payload itself is the middle one.
_STATUS_ENTRY_FIELDS = frozenset({"id", "time", "value"})

# The options the options flow writes, all three of them booleans. Allow-listed
# for the same reason the cloud records are, one step removed: the options dict
# is whatever has ever been persisted against this entry, which includes
# anything a past version wrote and stopped using.
_OPTION_FIELDS = frozenset({CONF_GENERIC_CONTROL_ENABLED, CONF_GENERIC_ENTITIES_ENABLED, CONF_PUSH_ENABLED})


def _select_allowed(record: Any, allowed: frozenset[str]) -> dict:
    """Return the allow-listed fields of one record, naming the rest.

    A field outside `allowed` contributes its name to `unlisted_keys` and never
    its value. That list is the whole point of the function: it is how a vendor
    field nobody has reviewed becomes visible as a question instead of shipping
    as an answer.

    A record that is not a dict yields a shape-typed placeholder rather than
    raising, matching the coordinator's standing position that a cloud record is
    an untrusted shape. Diagnostics is the one surface a user reaches for when
    something is already wrong, so it must not be the thing that raises.
    """
    if not isinstance(record, dict):
        return {"unexpected_type": type(record).__name__}
    selected = {key: value for key, value in record.items() if key in allowed}
    unlisted = sorted(str(key) for key in record if key not in allowed)
    if unlisted:
        selected["unlisted_keys"] = unlisted
    return selected


def _hub_dump(hub: Any) -> dict:
    """Return one top-level record with its sub-device list walked separately."""
    dumped = _select_allowed(hub, _HUB_RECORD_FIELDS)
    sub_devices = hub.get("subDevices") if isinstance(hub, dict) else None
    if isinstance(sub_devices, list):
        dumped["subDevices"] = [_select_allowed(sub, _SUB_DEVICE_FIELDS) for sub in sub_devices]
    elif sub_devices is not None:
        dumped["subDevices"] = {"unexpected_type": type(sub_devices).__name__}
    return dumped


def _sensor_dump(entry: Any) -> dict:
    """Return one coordinator sensor entry.

    The undecoded payload and this integration's reading of it are both here,
    and they are the reason the dump is worth downloading: a bug report about a
    misread device is answerable from the pair without a follow-up request.

    They are treated differently, though, and the difference is the whole point
    of the second pass below. `data` is decoder output, so its keys are written
    in this repo and a new one is somebody's deliberate change. `raw_status` is
    a cloud mapping held whole, so allow-listing the entry that carries it says
    nothing about what is inside it: without this pass a field the vendor adds
    to a status entry would ship its value on the first poll after they add it.
    """
    dumped = _select_allowed(entry, _SENSOR_ENTRY_FIELDS)
    if "raw_status" in dumped:
        dumped["raw_status"] = _select_allowed(dumped["raw_status"], _STATUS_ENTRY_FIELDS)
    return dumped


def _coordinator_dump(coordinator: Any) -> dict:
    """Return the poll-level summary, independent of any one device."""
    data = getattr(coordinator, "data", None) or {}
    interval = getattr(coordinator, "update_interval", None)
    return {
        "last_update_success": getattr(coordinator, "last_update_success", None),
        "update_interval_seconds": interval.total_seconds() if interval else None,
        "hub_count": len(data.get("hubs") or []),
        "sensor_count": len(data.get("sensors") or {}),
    }


def _push_dump(entry_store: dict) -> dict:
    """Return the push channel's liveness, carrying no credential of any kind.

    The MQTT client holds per-session credentials, so this reads only the four
    public liveness properties by name rather than reflecting over the object.
    """
    mqtt_client = entry_store.get("mqtt_client")
    if mqtt_client is None:
        return {"client_built": False}
    return {
        "client_built": True,
        "connected": mqtt_client.connected,
        "message_count": mqtt_client.message_count,
        "last_message_at": mqtt_client.last_message_at,
        "hub_mid": mqtt_client.hub_mid,
    }


def _entry_dump(config_entry: ConfigEntry) -> dict:
    """Return the config entry, carrying key names for `data` and never values.

    `entry.data` holds the account password and the persisted API tokens. This
    is the one place where naming the keys is strictly better than redacting
    their values: a redaction pass over that dict would still have to be correct
    about every key it contains, where a key list cannot be wrong about a value
    it never reads.
    """
    return {
        "data_keys": sorted(str(key) for key in config_entry.data),
        "home_count": len(config_entry.data.get(CONF_HIDS) or []),
        "area_code": config_entry.data.get(CONF_AREA_CODE),
        "options": _select_allowed(dict(config_entry.options), _OPTION_FIELDS),
    }


def _entry_store(hass: HomeAssistant, config_entry: ConfigEntry) -> dict:
    """Return this entry's runtime store, or an empty dict if it has none."""
    return (hass.data.get(DOMAIN) or {}).get(config_entry.entry_id) or {}


async def async_get_config_entry_diagnostics(hass: HomeAssistant, config_entry: ConfigEntry) -> dict[str, Any]:
    """Return diagnostics for the whole config entry."""
    entry_store = _entry_store(hass, config_entry)
    coordinator = entry_store.get("coordinator")
    data = (getattr(coordinator, "data", None) or {}) if coordinator else {}

    payload = {
        "integration": {"domain": DOMAIN, "version": VERSION},
        "entry": _entry_dump(config_entry),
        "coordinator": _coordinator_dump(coordinator) if coordinator else {"set_up": False},
        "push": _push_dump(entry_store),
        "hubs": [_hub_dump(hub) for hub in data.get("hubs") or []],
        "sensors": {key: _sensor_dump(entry) for key, entry in (data.get("sensors") or {}).items()},
        "hub_connectivity": data.get("hub_connectivity") or {},
    }
    return async_redact_data(payload, TO_REDACT)


async def async_get_device_diagnostics(hass: HomeAssistant, config_entry: ConfigEntry, device: DeviceEntry) -> dict[str, Any]:
    """Return diagnostics scoped to one device page.

    The device's own DOMAIN identifier is what routes this: a hub row carries a
    `hub_`-prefixed identity and a sub-device row carries its `{hid}_{mid}_{addr}`
    sensor key, which is the same round trip `_domain_sensor_key` already
    performs for the orphaned-entity sweep. Reusing it keeps one reading of the
    identifier shape rather than a second spelling that could drift from it.

    The hub half reuses `_hub_identity` for the same reason, and it matters more
    there: that helper accepts both the migrated `hub_{hid}_{mid}` shape and the
    older `hub_{hid}` one, which still exists on any row the identity migration
    could not resolve a mid for. Matching the identifier as a string would hand
    such a row an empty dump on the one device page most likely to be opened
    when something is wrong with it.
    """
    entry_store = _entry_store(hass, config_entry)
    coordinator = entry_store.get("coordinator")
    data = (getattr(coordinator, "data", None) or {}) if coordinator else {}
    identifier = _domain_sensor_key(device)

    payload: dict[str, Any] = {
        "integration": {"domain": DOMAIN, "version": VERSION},
        "device": {"identifier": identifier, "kind": None},
    }

    if identifier is None:
        # A device row of this config entry carrying no DOMAIN identifier is not
        # a shape this integration produces, so say so rather than returning an
        # empty dump that reads as "nothing wrong here".
        payload["device"]["kind"] = "unrecognised"
        return async_redact_data(payload, TO_REDACT)

    if identifier.startswith(HUB_IDENTIFIER_PREFIX):
        payload["device"]["kind"] = "hub"
        return async_redact_data(_hub_scoped_payload(payload, _hub_identity(identifier), data), TO_REDACT)

    payload["device"]["kind"] = "sub_device"
    sensors = data.get("sensors") or {}
    payload["sensors"] = {identifier: _sensor_dump(sensors[identifier])} if identifier in sensors else {}
    return async_redact_data(payload, TO_REDACT)


def _hub_scoped_payload(payload: dict, identity: tuple[str, str | None] | None, data: dict) -> dict:
    """Add the hub's record, connectivity and children to a device payload.

    `identity` is `_hub_identity`'s answer: `(hid, mid)` for a migrated row,
    `(hid, None)` for a row still on the older hid-only identity, and None for a
    `hub_`-prefixed value that is neither shape.

    A hid-only row matches every hub in that home rather than none of them. That
    is the honest answer to an ambiguous identifier: a home with one hub, which
    is every install that has ever been reported here, gets exactly its own hub,
    and a two-hub home gets both rather than silently getting neither. Both
    beat an empty dump on a device page opened because something is wrong.
    """
    if identity is None:
        payload["hubs"] = []
        payload["hub_connectivity"] = {}
        payload["sensors"] = {}
        return payload

    hid, mid = identity
    hubs = [hub for hub in (_hub_dump(hub) for hub in data.get("hubs") or []) if _matches_hub(hub, hid, mid)]
    mids = {f"{hub.get('mid')}" for hub in hubs}
    payload["hubs"] = hubs
    payload["hub_connectivity"] = {
        record_mid: record for record_mid, record in (data.get("hub_connectivity") or {}).items() if f"{record_mid}" in mids
    }
    payload["sensors"] = {
        key: _sensor_dump(entry)
        for key, entry in (data.get("sensors") or {}).items()
        if isinstance(entry, dict) and f"{entry.get('hid')}" == hid and f"{entry.get('mid')}" in mids
    }
    return payload


def _matches_hub(dumped_hub: dict, hid: str, mid: str | None) -> bool:
    """Return whether an already-dumped hub record answers to this identity.

    Reads the dumped copy rather than the raw record, so the match is made
    against the same allow-listed fields the caller is filtering and a record
    missing either field simply fails to match instead of raising.
    """
    if f"{dumped_hub.get('hid')}" != hid:
        return False
    return mid is None or f"{dumped_hub.get('mid')}" == mid
