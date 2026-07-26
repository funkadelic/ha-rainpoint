"""Hub entities for RainPoint devices."""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.components.select import SelectEntity
from homeassistant.components.sensor import SensorDeviceClass, SensorEntity, SensorStateClass
from homeassistant.components.switch import SwitchEntity
from homeassistant.const import EntityCategory
from homeassistant.core import callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    PUSH_CONNECTED_UNIQUE_ID_SUFFIX,
    PUSH_LAST_MESSAGE_UNIQUE_ID_SUFFIX,
)
from .coordinator import RainPointCoordinator
from .device import RainPointHubDevice

_LOGGER = logging.getLogger(__name__)


def resolve_push_diagnostic_hubs(coordinator: RainPointCoordinator, mqtt_client) -> list[dict]:
    """Return the hub(s) the push diagnostics belong to.

    The MQTT client is built for exactly one hub, so the connection and
    last-message diagnostics are created only for that bound hub. Creating one
    per configured hub would make every hub on a multi-hub account display the
    shared client's state, even though push only ever targets the bound hub.
    """
    hubs_cfg = (coordinator.data or {}).get("hubs", [])
    hubs = list(hubs_cfg.values()) if isinstance(hubs_cfg, dict) else list(hubs_cfg)
    if not hubs:
        return []
    bound_mid = getattr(mqtt_client, "hub_mid", None)
    if bound_mid is not None:
        match = next((hub for hub in hubs if hub.get("mid") == bound_mid), None)
        if match is not None:
            return [match]
    # No mid to match on (or no matching hub): fall back to the first hub, which
    # is the one the client was built from.
    return [hubs[0]]


class RainPointHubSensorBase(CoordinatorEntity, SensorEntity, RainPointHubDevice):
    """Base class for RainPoint hub sensors."""

    _attr_should_poll = False
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(
        self,
        coordinator: RainPointCoordinator,
        hub_info: dict,
    ) -> None:
        CoordinatorEntity.__init__(self, coordinator)
        RainPointHubDevice.__init__(self, hub_info)

    @property
    def available(self) -> bool:
        return True


class RainPointHubRSSISensor(RainPointHubSensorBase):
    """RSSI sensor for RainPoint hub."""

    _attr_device_class = SensorDeviceClass.SIGNAL_STRENGTH
    _attr_native_unit_of_measurement = "dBm"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:wifi"

    def __init__(self, coordinator: RainPointCoordinator, hub_info: dict):
        super().__init__(coordinator, hub_info)
        self._attr_unique_id = f"{self._attr_unique_id}_rssi"
        self._attr_name = f"{self._attr_name} Signal Strength"

    @property
    def native_value(self) -> int | None:
        mid = self._hub_info.get("mid")
        mid_status = self.coordinator.data.get("status", {}).get(mid, {})
        for entry in mid_status.get("subDeviceStatus", []):
            if isinstance(entry, dict) and entry.get("id") == "state":
                return _parse_hub_rssi(entry.get("value"))
        return None


class RainPointHubDeviceIDSensor(RainPointHubSensorBase):
    """Device ID sensor for RainPoint hub."""

    _attr_icon = "mdi:identifier"

    def __init__(self, coordinator: RainPointCoordinator, hub_info: dict):
        super().__init__(coordinator, hub_info)
        self._attr_unique_id = f"rainpoint_hub_{hub_info.get('hid', 'unknown')}_device_id"
        self._attr_name = f"{hub_info.get('name', 'RainPoint Hub')} Device ID"

    @property
    def native_value(self) -> str | int | None:
        return self._hub_info.get("hid")


class RainPointHubFirmwareSensor(RainPointHubSensorBase):
    """Firmware version sensor for RainPoint hub."""

    _attr_icon = "mdi:chip"

    def __init__(self, coordinator: RainPointCoordinator, hub_info: dict):
        super().__init__(coordinator, hub_info)
        self._attr_unique_id = f"rainpoint_hub_{hub_info.get('hid', 'unknown')}_firmware"
        self._attr_name = f"{hub_info.get('name', 'RainPoint Hub')} Firmware Version"

    @property
    def native_value(self) -> str | None:
        return self._hub_info.get("softVer")


class RainPointHubMACSensor(RainPointHubSensorBase):
    """MAC address sensor for RainPoint hub."""

    _attr_icon = "mdi:network-outline"

    def __init__(self, coordinator: RainPointCoordinator, hub_info: dict):
        super().__init__(coordinator, hub_info)
        self._attr_unique_id = f"rainpoint_hub_{hub_info.get('hid', 'unknown')}_mac"
        self._attr_name = f"{hub_info.get('name', 'RainPoint Hub')} MAC Address"

    @property
    def native_value(self) -> str | None:
        return self._hub_info.get("mac")


def _parse_hub_rssi(state_value) -> int | None:
    """Extract the hub's own RSSI (dBm) from its `state` status value.

    The hub reports `state` as a comma-separated string like `0,-52` whose second
    field is the signed WiFi RSSI (distinct from the per-valve RF link RSSI in the
    device payload). Returns None when the value is absent or unparseable.
    """
    if not isinstance(state_value, str):
        return None
    parts = state_value.split(",")
    if len(parts) < 2:
        return None
    try:
        return int(parts[1])
    except ValueError:
        return None


_RF_CHANNEL_FALLBACK = list(range(1, 17))


def _hub_function(hub_info: dict) -> dict:
    """Parse the hub record's `function` JSON blob into a dict, or {} on any problem.

    The blob looks like `{"model":"...","childMax":40,"RF":7,"SM":7,...}`; it is
    a string in the API response, so it is parsed here rather than indexed.
    """
    raw = hub_info.get("function")
    if not isinstance(raw, str) or not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except (ValueError, TypeError):
        _LOGGER.debug("Hub %s has an unparseable function blob: %r", hub_info.get("hid"), raw)
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _hub_rf_current_channel(hub_info: dict) -> int | None:
    """Return the hub's current RF channel from its `recich` field, or None.

    `recich` is the receive channel the hub is tuned to (matches the value the
    vendor app shows). bool is excluded because it is an int subclass.
    """
    value = hub_info.get("recich")
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _hub_rf_channel_options(hub_info: dict) -> list[str]:
    """Return the selectable RF channels for the hub as strings.

    The hub's `function.RF` value is a bitmask of supported channels (bit i set
    means channel i+1 is available); e.g. 7 (0b111) -> channels 1, 2, 3. Falls
    back to the full 1-16 range when the field is missing or unusable, and always
    includes the current channel so a live value outside the mask still renders.
    """
    rf = _hub_function(hub_info).get("RF")
    if isinstance(rf, bool) or not isinstance(rf, int) or rf <= 0:
        channels = set(_RF_CHANNEL_FALLBACK)
    else:
        channels = {bit + 1 for bit in range(rf.bit_length()) if rf >> bit & 1}
    current = _hub_rf_current_channel(hub_info)
    if current is not None:
        channels.add(current)
    return [str(channel) for channel in sorted(channels)]


class RainPointHubChannelSelect(CoordinatorEntity, SelectEntity, RainPointHubDevice):
    """RF Channel selector for RainPoint hub."""

    _attr_entity_category = EntityCategory.CONFIG
    _attr_icon = "mdi:radio-tower"

    def __init__(self, coordinator: RainPointCoordinator, hub_info: dict):
        CoordinatorEntity.__init__(self, coordinator)
        RainPointHubDevice.__init__(self, hub_info)
        self._attr_unique_id = f"rainpoint_hub_{hub_info.get('hid', 'unknown')}_channel"
        self._attr_name = f"{hub_info.get('name', 'RainPoint Hub')} RF Channel"
        self._attr_options = _hub_rf_channel_options(hub_info)
        # Current channel comes from the hub record; None renders as unknown when
        # the field is absent. Selecting a channel is still unsupported (below).
        current = _hub_rf_current_channel(hub_info)
        self._attr_current_option = str(current) if current is not None else None

    @property
    def available(self) -> bool:
        return True

    @property
    def current_option(self) -> str | None:
        return self._attr_current_option

    async def async_select_option(self, option: str) -> None:
        """Change the RF channel."""
        raise HomeAssistantError("RF channel selection is not yet supported by the RainPoint API")


class _RainPointPushDiagnosticBase(RainPointHubDevice):
    """Shared wiring for the hub-level push diagnostic entities.

    These read their live state directly from the MQTT client (not from
    coordinator.data), so they register a state listener on the client and
    re-render on every connect/disconnect/message transition.
    """

    _attr_should_poll = False
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, mqtt_client, hub_info: dict) -> None:
        """Bind the diagnostic to the MQTT client it reads its live state from."""
        RainPointHubDevice.__init__(self, hub_info)
        self._mqtt_client = mqtt_client

    async def async_added_to_hass(self) -> None:
        """Register for MQTT state-change notifications while the entity lives."""
        self._mqtt_client.add_state_listener(self._handle_client_state)

    async def async_will_remove_from_hass(self) -> None:
        """Stop receiving MQTT state-change notifications."""
        self._mqtt_client.remove_state_listener(self._handle_client_state)

    @callback
    def _handle_client_state(self) -> None:
        """Re-render on a connect/disconnect/message transition."""
        self.async_write_ha_state()

    @property
    def available(self) -> bool:
        """Available whenever the push MQTT client is present."""
        return self._mqtt_client is not None


class RainPointPushConnectedBinarySensor(_RainPointPushDiagnosticBase, BinarySensorEntity):
    """Hub-level push connection state (on when the MQTT client is connected)."""

    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_icon = "mdi:cloud-check-variant"

    def __init__(self, mqtt_client, hub_info: dict) -> None:
        """Build the connection-state entity with a stable per-hub unique id."""
        super().__init__(mqtt_client, hub_info)
        self._attr_unique_id = f"{self._attr_unique_id}_{PUSH_CONNECTED_UNIQUE_ID_SUFFIX}"
        self._attr_name = f"{self._attr_name} Push Connected"

    @property
    def is_on(self) -> bool:
        """Return True while the MQTT client is connected."""
        return self._mqtt_client.connected


class RainPointPushLastMessageSensor(_RainPointPushDiagnosticBase, SensorEntity):
    """Hub-level timestamp of the last received push message.

    The client's liveness clock is a monotonic value (immune to wall-clock
    steps), so it is converted to an absolute UTC datetime for display by
    subtracting its age from the current wall-clock time. The rendered timestamp
    is stable between reads because the age grows in lockstep with the clock.
    """

    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_icon = "mdi:clock-check-outline"

    def __init__(self, mqtt_client, hub_info: dict, *, time_source: Callable[[], float] = time.monotonic) -> None:
        """Build the last-message entity; time_source is injectable for tests."""
        super().__init__(mqtt_client, hub_info)
        self._time_source = time_source
        self._attr_unique_id = f"{self._attr_unique_id}_{PUSH_LAST_MESSAGE_UNIQUE_ID_SUFFIX}"
        self._attr_name = f"{self._attr_name} Push Last Message"

    @property
    def native_value(self) -> datetime | None:
        """Return the last-message time as an absolute UTC datetime, or None."""
        last = self._mqtt_client.last_message_at
        if last is None:
            return None
        age = self._time_source() - last
        if age < 0:
            age = 0.0
        return datetime.now(UTC) - timedelta(seconds=age)


class RainPointHubBroadcastSwitch(CoordinatorEntity, SwitchEntity, RainPointHubDevice):
    """Automatic Broadcast Time switch for RainPoint hub."""

    _attr_entity_category = EntityCategory.CONFIG
    _attr_icon = "mdi:clock-outline"

    def __init__(self, coordinator: RainPointCoordinator, hub_info: dict):
        CoordinatorEntity.__init__(self, coordinator)
        RainPointHubDevice.__init__(self, hub_info)
        self._attr_unique_id = f"rainpoint_hub_{hub_info.get('hid', 'unknown')}_broadcast"
        self._attr_name = f"{hub_info.get('name', 'RainPoint Hub')} Automatic Broadcast"
        self._attr_is_on = None  # Unknown until API supports reading

    @property
    def available(self) -> bool:
        return True

    @property
    def is_on(self) -> bool | None:
        return self._attr_is_on

    async def async_turn_on(self) -> None:
        """Turn on automatic broadcast."""
        raise HomeAssistantError("Automatic broadcast control is not yet supported by the RainPoint API")

    async def async_turn_off(self) -> None:
        """Turn off automatic broadcast."""
        raise HomeAssistantError("Automatic broadcast control is not yet supported by the RainPoint API")
