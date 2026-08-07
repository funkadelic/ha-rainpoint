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
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceEntry

from . import _domain_sensor_key
from .const import CONF_AREA_CODE, CONF_HIDS, DOMAIN, HUB_IDENTIFIER_PREFIX, VERSION

# Values replaced with a redaction marker wherever they appear, at any depth.
#
# Two groups, and both are here for the same reason rather than for the same
# risk. Credentials (the first group) authenticate; disclosing one is the
# serious case. The rest address rather than authenticate, and are redacted to
# follow the decision already taken for the command logs, where the DEBUG line
# was narrowed to keys and integers precisely so deviceName and productKey stop
# travelling with support output. A dump a user pastes into a public issue is
# the same disclosure surface as a log they paste into one.
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
    "homeName",
    "home_name",
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
# here is a change someone in this repo made deliberately. It still goes through
# the same allow-list so that "raw_status" and "data" are the only places a
# cloud value reaches the dump, and both are wanted.
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

    `raw_status` and `data` are carried whole and are the reason this dump is
    worth downloading: the first is the undecoded payload the cloud sent, the
    second is what this integration made of it. A bug report about a misread
    device is answerable from the pair without a follow-up request.
    """
    return _select_allowed(entry, _SENSOR_ENTRY_FIELDS)


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
        "options": dict(config_entry.options),
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

    The device's own DOMAIN identifier is what routes this: a hub row carries
    `hub_{hid}_{mid}` and a sub-device row carries its `{hid}_{mid}_{addr}`
    sensor key, which is the same round trip `_domain_sensor_key` already
    performs for the orphaned-entity sweep. Reusing it keeps one reading of the
    identifier shape rather than a second spelling that could drift from it.
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
        hub_key = identifier[len(HUB_IDENTIFIER_PREFIX) :]
        payload["hubs"] = [hub for hub in (_hub_dump(hub) for hub in data.get("hubs") or []) if _hub_key(hub) == hub_key]
        payload["hub_connectivity"] = {
            mid: record for mid, record in (data.get("hub_connectivity") or {}).items() if f"{mid}" == hub_key.rsplit("_", 1)[-1]
        }
        payload["sensors"] = {
            key: _sensor_dump(entry)
            for key, entry in (data.get("sensors") or {}).items()
            if isinstance(entry, dict) and f"{entry.get('hid')}_{entry.get('mid')}" == hub_key
        }
        return async_redact_data(payload, TO_REDACT)

    payload["device"]["kind"] = "sub_device"
    sensors = data.get("sensors") or {}
    payload["sensors"] = {identifier: _sensor_dump(sensors[identifier])} if identifier in sensors else {}
    return async_redact_data(payload, TO_REDACT)


def _hub_key(dumped_hub: dict) -> str:
    """Return `{hid}_{mid}` for an already-dumped hub record.

    Reads the dumped copy rather than the raw record so the key is built from
    the same allow-listed fields the caller is filtering, and a record missing
    either field yields a key no device identifier can match instead of raising.
    """
    return f"{dumped_hub.get('hid')}_{dumped_hub.get('mid')}"
