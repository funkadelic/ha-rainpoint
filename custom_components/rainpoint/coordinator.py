import logging
from datetime import UTC, datetime, timedelta

import aiohttp
from homeassistant.components.persistent_notification import async_create
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import (
    DataUpdateCoordinator,
    UpdateFailed,
)

from .api import (
    RainPointApiError,
    RainPointClient,
    decode_co2,
    decode_flowmeter,
    decode_hcs003frf,
    # New HCS decoder functions
    decode_hcs005frf,
    decode_hcs015arf,
    decode_hcs016arf,
    decode_hcs024frf_v1,
    decode_hcs027arf,
    decode_hcs044frf,
    decode_hcs048b,
    decode_hcs0528arf,
    decode_hcs0600arf,
    decode_hcs596wb,
    decode_hcs596wb_v4,
    decode_hcs666frf,
    decode_hcs666frf_x,
    decode_hcs666rfr_p,
    decode_hcs701b,
    decode_hcs706arf,
    decode_hcs802arf,
    decode_hcs888arf_v1,
    decode_hcs999frf,
    decode_hcs999frf_p,
    decode_htv213frf_valve,
    decode_hws019wrf_v2,
    decode_moisture_full,
    decode_moisture_simple,
    decode_pool,
    decode_pool_plus,
    decode_rain,
    decode_temphum,
    decode_valve_hub,
)
from .const import (
    CONF_HIDS,
    DEFAULT_SCAN_INTERVAL,
    ISSUE_URL,
    MODEL_CO2,
    MODEL_DISPLAY_HUB,
    MODEL_FLOWMETER,
    MODEL_HCS003FRF,
    # New HCS sensor models
    MODEL_HCS005FRF,
    MODEL_HCS015ARF,
    MODEL_HCS016ARF,
    MODEL_HCS024FRF_V1,
    MODEL_HCS027ARF,
    MODEL_HCS044FRF,
    MODEL_HCS048B,
    MODEL_HCS0528ARF,
    MODEL_HCS0600ARF,
    MODEL_HCS596WB,
    MODEL_HCS596WB_V4,
    MODEL_HCS666FRF,
    MODEL_HCS666FRF_X,
    MODEL_HCS666RFR_P,
    MODEL_HCS701B,
    MODEL_HCS706ARF,
    MODEL_HCS802ARF,
    MODEL_HCS888ARF_V1,
    MODEL_HCS999FRF,
    MODEL_HCS999FRF_P,
    MODEL_MOISTURE_FULL,
    MODEL_MOISTURE_SIMPLE,
    MODEL_POOL,
    MODEL_POOL_PLUS,
    MODEL_RAIN,
    MODEL_TEMPHUM,
    MODEL_VALVE_213,  # HTV213FRF support
    MODEL_VALVE_245,  # HTV245FRF support
    MODEL_VALVE_HUB,
    debug_with_version,
)

_LOGGER = logging.getLogger(__name__)

# Decoder registry - maps device models to their decoder functions
DECODER_REGISTRY = {
    MODEL_MOISTURE_SIMPLE: decode_moisture_simple,
    MODEL_MOISTURE_FULL: decode_moisture_full,
    MODEL_RAIN: decode_rain,
    MODEL_TEMPHUM: decode_temphum,
    MODEL_FLOWMETER: decode_flowmeter,
    MODEL_CO2: decode_co2,
    MODEL_POOL: decode_pool,
    MODEL_POOL_PLUS: decode_pool_plus,
    MODEL_VALVE_HUB: decode_valve_hub,
    MODEL_VALVE_213: decode_htv213frf_valve,  # HTV213FRF uses custom decoder
    MODEL_VALVE_245: decode_htv213frf_valve,  # HTV245FRF uses custom decoder
    # HCS sensor models (v1.3.0)
    MODEL_HCS005FRF: decode_hcs005frf,
    MODEL_HCS003FRF: decode_hcs003frf,
    MODEL_HCS024FRF_V1: decode_hcs024frf_v1,
    MODEL_HCS015ARF: decode_hcs015arf,
    MODEL_HCS0528ARF: decode_hcs0528arf,
    MODEL_HCS027ARF: decode_hcs027arf,
    MODEL_HCS016ARF: decode_hcs016arf,
    MODEL_HCS044FRF: decode_hcs044frf,
    MODEL_HCS666FRF: decode_hcs666frf,
    MODEL_HCS666RFR_P: decode_hcs666rfr_p,
    MODEL_HCS999FRF: decode_hcs999frf,
    MODEL_HCS999FRF_P: decode_hcs999frf_p,
    MODEL_HCS666FRF_X: decode_hcs666frf_x,
    MODEL_HCS701B: decode_hcs701b,
    MODEL_HCS596WB: decode_hcs596wb,
    MODEL_HCS596WB_V4: decode_hcs596wb_v4,
    MODEL_HCS706ARF: decode_hcs706arf,
    MODEL_HCS802ARF: decode_hcs802arf,
    MODEL_HCS048B: decode_hcs048b,
    MODEL_HCS888ARF_V1: decode_hcs888arf_v1,
    MODEL_HCS0600ARF: decode_hcs0600arf,
}


def _resolve_addr_from_sid(sid: str) -> int | None:
    """Return integer addr from a 'D'-prefixed sid (e.g. 'D1' -> 1).

    Returns None if sid does not start with 'D' or the suffix is not a base-10 integer.
    """
    if not sid.startswith("D"):
        return None
    try:
        return int(sid[1:])
    except ValueError:
        return None


def _decode_subdevice_payload(model: str | None, raw_value: str) -> dict:
    """Dispatch raw_value through DECODER_REGISTRY or the MODEL_DISPLAY_HUB special case.

    Returns the decoded dict, or an {"type": "unknown", ...} shape if no decoder is
    registered. Raises whatever the underlying decoder raises - callers handle the
    try/except and any side-effects (logging, persistent notifications).
    """
    if model == MODEL_DISPLAY_HUB:
        return decode_hws019wrf_v2(raw_value)
    decoder_func = DECODER_REGISTRY.get(model)
    if decoder_func:
        return decoder_func(raw_value)
    return {
        "type": "unknown",
        "model": model,
        "raw_value": raw_value,
    }


def _attach_device_timestamp(decoded: dict | None, status_entry: dict) -> None:
    """Mutate decoded in place to add device_timestamp / timestamp_source.

    No-op when decoded is falsy or status_entry["time"] is missing (None). A
    "time" of 0 is treated as a valid epoch-ms (1970-01-01). Silently swallows
    ValueError, TypeError, OSError, and OverflowError raised while parsing.
    """
    device_time = status_entry.get("time")
    if device_time is None:
        return
    try:
        dt = datetime.fromtimestamp(device_time / 1000, tz=UTC)
        if decoded:
            decoded["device_timestamp"] = dt.isoformat()
            decoded["timestamp_source"] = "device"
    except (ValueError, TypeError, OSError, OverflowError):
        pass


def _build_sensor_entry(
    hub: dict,
    sub: dict,
    mid: int,
    addr: int,
    status_entry: dict,
    decoded: dict | None,
) -> dict:
    """Build the per-sensor metadata dict that goes into the coordinator's sensors output."""
    return {
        "hid": hub["hid"],
        "mid": mid,
        "addr": addr,
        "home_name": hub.get("homeName"),
        "hub_name": hub.get("name", "Hub"),
        "sub_name": sub.get("name"),
        "model": sub.get("model"),
        "firmware_version": sub.get("softVer"),
        "device_name": hub.get("deviceName"),
        "product_key": hub.get("productKey"),
        "raw_status": status_entry,
        "data": decoded,
    }


class RainPointCoordinator(DataUpdateCoordinator):
    """Coordinator for RainPoint polling."""

    def __init__(self, hass: HomeAssistant, client: RainPointClient, entry):
        super().__init__(
            hass,
            _LOGGER,
            name="RainPoint coordinator",
            update_interval=timedelta(seconds=DEFAULT_SCAN_INTERVAL),
        )
        self._client = client
        self._entry = entry
        self._hids = entry.data.get(CONF_HIDS, [])
        self._notified_unknown_models: set[str] = set()

    async def _async_update_data(self):
        """Fetch and decode data from RainPoint."""
        try:
            # Dispatch helpers via the class (not self.<method>) so the existing
            # SimpleNamespace-based test pattern in tests/test_coordinator.py
            # continues to work without modification.
            hubs = await RainPointCoordinator._collect_hubs(self)
            status_by_mid: dict[int, dict] = {}
            decoded_sensors: dict[str, dict] = {}

            if hubs:
                status_by_mid = await RainPointCoordinator._fetch_status_by_mid(self, hubs)

            for hub in hubs:
                mid = hub["mid"]
                status = status_by_mid.get(mid, {"subDeviceStatus": []})
                decoded_sensors.update(RainPointCoordinator._decode_hub_subdevices(self, hub, status))

            _LOGGER.info("Coordinator update complete: %d hubs, %d sensors", len(hubs), len(decoded_sensors))
            _LOGGER.debug(debug_with_version("Final data: hubs=%s, sensors=%s"), hubs, list(decoded_sensors.keys()))

            return {
                "hubs": hubs,
                "status": status_by_mid,
                "sensors": decoded_sensors,
            }
        except RainPointApiError as err:
            raise UpdateFailed(f"RainPoint API error: {err}") from err
        except Exception as err:
            _LOGGER.exception("Unexpected RainPoint error while refreshing")
            raise UpdateFailed(f"Unexpected RainPoint error: {err}") from err

    async def _collect_hubs(self) -> list[dict]:
        """Fetch hubs for every configured hid and inject hid + brand metadata."""
        homes = self._hids
        hubs: list[dict] = []
        _LOGGER.info("Updating data for HIDs: %s", homes)
        for hid in homes:
            devices = await self._client.get_devices_by_hid(hid)
            _LOGGER.info("Found %d devices for HID %s: %s", len(devices), hid, [d.get("model", "unknown") for d in devices])
            for hub in devices:
                hub_copy = dict(hub)
                hub_copy["hid"] = hid
                # All devices are RainPoint hardware
                hub_copy["brand"] = "RainPoint"
                hubs.append(hub_copy)
        return hubs

    async def _fetch_status_by_mid(self, hubs: list[dict]) -> dict[int, dict]:
        """Try multipleDeviceStatus first, fall back to per-hub get_device_status on empty
        response or transport-level errors (aiohttp.ClientError / TimeoutError).
        RainPointApiError surfaces to UpdateFailed; programming bugs propagate to the outer
        Exception handler instead of being silently masked by the fallback."""
        # Prepare device list for multipleDeviceStatus API
        device_list = [
            {"mid": hub["mid"], "deviceName": hub.get("deviceName", ""), "productKey": hub.get("productKey", "")} for hub in hubs
        ]

        # Try multipleDeviceStatus first (more efficient)
        multiple_status: list | None = None
        try:
            multiple_status = await self._client.get_multiple_device_status(device_list)
            _LOGGER.debug(
                debug_with_version("multipleDeviceStatus successful, got data for %d devices"),
                len(multiple_status) if multiple_status else 0,
            )
        except RainPointApiError:
            # Surface API errors to the outer except RainPointApiError -> UpdateFailed wrapper
            # so HA marks entities unavailable instead of silently falling back.
            raise
        except (aiohttp.ClientError, TimeoutError) as e:
            # Only treat transport-level errors as transient. Programming bugs
            # (KeyError, AttributeError, etc.) propagate to the outer handler so
            # they surface as UpdateFailed and are not silently masked by the fallback.
            _LOGGER.warning("multipleDeviceStatus transport error, falling back to individual calls: %s", e)

        # Convert response to status_by_mid format when populated.
        # Note: get_multiple_device_status already converts "status" to "subDeviceStatus".
        if multiple_status:
            status_by_mid: dict[int, dict] = {}
            for device_data in multiple_status:
                mid = device_data["mid"]
                status_array = device_data.get("subDeviceStatus", [])
                status_by_mid[mid] = {"subDeviceStatus": status_array}
                _LOGGER.debug(debug_with_version("Fetched status for mid=%s using multipleDeviceStatus"), mid)
            return status_by_mid

        # Plain conditional fallback path: empty / None / transient-error multi-status all
        # converge here, replacing the prior raised-exception sentinel that doubled as
        # control flow.
        _LOGGER.warning("multipleDeviceStatus returned empty data, falling back to individual calls")
        # Class-level dispatch matches the orchestrator pattern in _async_update_data so the
        # SimpleNamespace-based test fixture in tests/test_coordinator.py keeps working
        # without modification.
        return await RainPointCoordinator._fallback_per_hub_status(self, hubs)

    async def _fallback_per_hub_status(self, hubs: list[dict]) -> dict[int, dict]:
        """Per-hub fallback fetch. RainPointApiError surfaces to UpdateFailed; transport
        errors (aiohttp.ClientError / TimeoutError) record an empty subDeviceStatus list
        and continue with the next hub so a single transient hub failure does not wipe a
        multi-hub poll. Programming bugs propagate to the outer Exception handler."""
        status_by_mid: dict[int, dict] = {}
        for hub in hubs:
            mid = hub["mid"]
            try:
                status = await self._client.get_device_status(mid)
                status_by_mid[mid] = status
                _LOGGER.debug(debug_with_version("Fetched status for mid=%s using individual call"), mid)
            except RainPointApiError:
                # Surface API errors to the outer except RainPointApiError -> UpdateFailed wrapper.
                raise
            except (aiohttp.ClientError, TimeoutError) as individual_e:
                _LOGGER.error("Transport error getting status for mid=%s: %s", mid, individual_e)
                status_by_mid[mid] = {"subDeviceStatus": []}
        return status_by_mid

    def _notify_unknown_model(self, model: str | None, mid: int, addr: int, raw_value: str) -> None:
        """Log the unsupported-sensor warning and fire a once-per-model persistent notification."""
        _LOGGER.warning(
            "=" * 60 + "\n"
            "UNSUPPORTED SENSOR MODEL DETECTED\n"
            "Please report this to: %s\n"
            "Include the following information:\n"
            "  Model: %s\n"
            "  Device ID (mid): %s\n"
            "  Address: %s\n"
            "  Raw Payload: %s\n" + "=" * 60,
            ISSUE_URL,
            model,
            mid,
            addr,
            raw_value,
        )
        # Send persistent notification (once per model)
        if model and model not in self._notified_unknown_models:
            self._notified_unknown_models.add(model)
            async_create(
                self.hass,
                f"RainPoint detected an unsupported sensor model: **{model}**\n\n"
                f"To help add support for this sensor, please open an issue at:\n"
                f"{ISSUE_URL}\n\n"
                f"Include the following raw payload data:\n"
                f"```\n{raw_value}\n```\n\n"
                f"You can also find this data in the sensor's attributes in Home Assistant.",
                title="RainPoint: Unsupported Sensor Detected",
                notification_id=f"rainpoint_unsupported_{model}",
            )

    def _decode_one_subdevice(
        self,
        hub: dict,
        mid: int,
        addr: int,
        sub: dict,
        status_entry: dict,
    ) -> tuple[str, dict]:
        """Decode a single sub-device and return (sensor_key, sensor_entry_dict)."""
        sid = status_entry.get("id", "")
        raw_value = status_entry.get("value")
        model = sub.get("model")

        if not raw_value:
            # No reading / offline
            decoded: dict | None = None
            _LOGGER.debug("No raw_value for mid=%s addr=%s (sid=%s)", mid, addr, sid)
        else:
            try:
                _LOGGER.debug(
                    debug_with_version("Decoding payload for model=%s mid=%s addr=%s: %s"),
                    model,
                    mid,
                    addr,
                    raw_value,
                )
                decoded = _decode_subdevice_payload(model, raw_value)
                if decoded.get("type") == "unknown":
                    RainPointCoordinator._notify_unknown_model(self, model, mid, addr, raw_value)
                _LOGGER.debug(debug_with_version("Decoded data for mid=%s addr=%s: %s"), mid, addr, decoded)
            except Exception as ex:
                _LOGGER.warning(
                    "Failed to decode payload for %s addr=%s: %s",
                    model,
                    addr,
                    ex,
                )
                decoded = None

        _attach_device_timestamp(decoded, status_entry)

        sensor_key = f"{hub['hid']}_{mid}_{addr}"
        sensor_entry = _build_sensor_entry(hub, sub, mid, addr, status_entry, decoded)
        _LOGGER.debug(debug_with_version("Sensor entity key=%s info=%s"), sensor_key, sensor_entry)
        return sensor_key, sensor_entry

    def _decode_hub_subdevices(self, hub: dict, status: dict) -> dict[str, dict]:
        """Walk the sub_status entries for one hub and return a {sensor_key: sensor_entry} dict."""
        mid = hub["mid"]
        _LOGGER.debug(debug_with_version("Processing hub mid=%s with status"), mid)

        sub_status = {s["id"]: s for s in status.get("subDeviceStatus", [])}
        _LOGGER.debug(debug_with_version("Parsed sub_status for mid=%s: %s keys"), mid, len(sub_status))

        # Map addr -> subDevice
        addr_map = {sd["addr"]: sd for sd in hub.get("subDevices", [])}

        decoded_sensors: dict[str, dict] = {}
        for sid, s in sub_status.items():
            addr = _resolve_addr_from_sid(sid)
            if addr is None:
                continue

            sub = addr_map.get(addr)
            if not sub:
                continue

            sensor_key, sensor_entry = RainPointCoordinator._decode_one_subdevice(self, hub, mid, addr, sub, s)
            decoded_sensors[sensor_key] = sensor_entry

        return decoded_sensors
