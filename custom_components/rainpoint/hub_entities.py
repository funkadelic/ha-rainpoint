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
from .coordinator import RainPointCoordinator, first_hub_record, hub_connected_flag, is_hub_record
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
    # No mid to match on (or no matching hub): fall back to the first real hub,
    # which is the one the client was built from. A Bluetooth wrapper record can
    # occupy slot 0 without being a hub at all.
    fallback = first_hub_record(hubs)
    return [fallback] if fallback is not None else []


def resolve_connectivity_hubs(coordinator: RainPointCoordinator) -> list[dict]:
    """Return every real hub, for building one cloud-connectivity entity each.

    Unlike resolve_push_diagnostic_hubs, which returns only the single hub
    the shared MQTT client is bound to, this returns every real hub: cloud
    connectivity is a per-hub fact, not a property of the single MQTT client.
    """
    hubs_cfg = (coordinator.data or {}).get("hubs", [])
    hubs = list(hubs_cfg.values()) if isinstance(hubs_cfg, dict) else list(hubs_cfg)
    return [hub for hub in hubs if is_hub_record(hub)]


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
        # `or {}` / `or []` also handle an explicit None value (not just a missing
        # key), which .get(key, default) would pass through and then crash on.
        mid_status = self.coordinator.data.get("status", {}).get(mid) or {}
        for entry in mid_status.get("subDeviceStatus") or []:
            if isinstance(entry, dict) and entry.get("id") == "state":
                return _parse_hub_rssi(entry.get("value"))
        return None


class RainPointHubDeviceIDSensor(RainPointHubSensorBase):
    """Device ID sensor for RainPoint hub."""

    _attr_icon = "mdi:identifier"

    def __init__(self, coordinator: RainPointCoordinator, hub_info: dict):
        """Name the entity after the hub and key the entity to its home id."""
        super().__init__(coordinator, hub_info)
        self._attr_unique_id = f"rainpoint_hub_{hub_info.get('hid', 'unknown')}_device_id"
        self._attr_name = f"{hub_info.get('name') or 'RainPoint Hub'} Device ID"

    @property
    def native_value(self) -> str | int | None:
        # `did` is the device ID the vendor app shows; the home id (hid) is only a
        # fallback for a hub record that omits it.
        return self._hub_info.get("did") or self._hub_info.get("hid")


class RainPointHubFirmwareSensor(RainPointHubSensorBase):
    """Firmware version sensor for RainPoint hub."""

    _attr_icon = "mdi:chip"

    def __init__(self, coordinator: RainPointCoordinator, hub_info: dict):
        """Name the entity after the hub and key the entity to its home id."""
        super().__init__(coordinator, hub_info)
        self._attr_unique_id = f"rainpoint_hub_{hub_info.get('hid', 'unknown')}_firmware"
        self._attr_name = f"{hub_info.get('name') or 'RainPoint Hub'} Firmware Version"

    @property
    def native_value(self) -> str | None:
        return self._hub_info.get("softVer")


class RainPointHubMACSensor(RainPointHubSensorBase):
    """MAC address sensor for RainPoint hub."""

    _attr_icon = "mdi:network-outline"

    def __init__(self, coordinator: RainPointCoordinator, hub_info: dict):
        """Name the entity after the hub and key the entity to its home id."""
        super().__init__(coordinator, hub_info)
        self._attr_unique_id = f"rainpoint_hub_{hub_info.get('hid', 'unknown')}_mac"
        self._attr_name = f"{hub_info.get('name') or 'RainPoint Hub'} MAC Address"

    @property
    def native_value(self) -> str | None:
        return self._hub_info.get("mac")


class RainPointHubConnectivityBinarySensor(CoordinatorEntity, BinarySensorEntity, RainPointHubDevice):
    """Hub-level cloud connectivity: on while the RainPoint cloud reports this hub connected.

    Reads the coordinator's already-shaped `hub_connectivity` field rather
    than re-scanning subDeviceStatus itself; RainPointHubRSSISensor is the
    precedent for reading a non-D status id out of the status response, not
    the implementation this class copies. `state_raw` is carried undecoded:
    its first field read '0' in every observed condition on both sides of a
    real power cycle, so assigning it a meaning would be a guess shipped as
    fact. Hub-level entities, this one included, are created once from the
    first refresh snapshot like every other hub entity; state refresh after
    setup still happens normally through the CoordinatorEntity listener this
    base class registers -- only entity creation is one-shot.
    """

    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_should_poll = False

    def __init__(self, coordinator: RainPointCoordinator, hub_info: dict) -> None:
        """Build the connectivity entity with a per-hub unique id."""
        CoordinatorEntity.__init__(self, coordinator)
        RainPointHubDevice.__init__(self, hub_info)
        # Carries both hid and mid, unlike the hid-only hub siblings above
        # (device id, firmware, MAC, RF channel): a home can hold more than
        # one hub, and those siblings would contend for one id. This
        # divergence is deliberate; do not "fix" it back to match them.
        self._attr_unique_id = f"rainpoint_hub_{hub_info.get('hid', 'unknown')}_{hub_info.get('mid', 'unknown')}_connectivity"
        self._attr_name = f"{hub_info.get('name') or 'RainPoint Hub'} Cloud Connection"

    @property
    def _record(self) -> dict:
        """Return this hub's connectivity record, or {} when none exists yet.

        Tolerates a coordinator snapshot carrying no "hub_connectivity" key at
        all, which is what every pre-existing test fake in this suite supplies.
        """
        return (self.coordinator.data or {}).get("hub_connectivity", {}).get(self._hub_info.get("mid")) or {}

    @property
    def is_on(self) -> bool | None:
        """Return True/False/None through the shared tri-state mapping."""
        return hub_connected_flag(self._record)

    @property
    def icon(self) -> str:
        """Return an icon that tracks the state rather than fixing one glyph.

        A class-level _attr_icon would override the CONNECTIVITY device
        class's own on/off pair, leaving a connected hub showing a
        cloud-offline glyph, which reads as a false alarm on the one entity
        whose whole job is an at-a-glance health check. Unknown shares the
        offline glyph deliberately: is_on is None only when the cloud has not
        said either way, and claiming a healthy cloud there would overstate
        what is known.
        """
        return "mdi:cloud-check-variant" if self.is_on else "mdi:cloud-off-outline"

    @property
    def extra_state_attributes(self) -> dict:
        """Return the cloud change timestamp and the raw, undecoded state value.

        Both keys are always present, with None values when the underlying
        fields are absent, so the attribute never simply vanishes.
        """
        record = self._record
        return {
            "changed_at": record.get("changed_at"),
            "state_raw": record.get("state_raw"),
        }

    @property
    def available(self) -> bool:
        """Always available: an unknown connectivity state renders as unknown, not missing."""
        return True


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


_RF_CHANNEL_FALLBACK = range(1, 17)


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
        """Build the RF channel selector from the hub's supported-channel bitmask.

        Options come from the hub's function blob and the current option from
        its recich field; both resolve to nothing selectable when absent.
        """
        CoordinatorEntity.__init__(self, coordinator)
        RainPointHubDevice.__init__(self, hub_info)
        self._attr_unique_id = f"rainpoint_hub_{hub_info.get('hid', 'unknown')}_channel"
        self._attr_name = f"{hub_info.get('name') or 'RainPoint Hub'} RF Channel"
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
        """Build the broadcast switch with an unknown initial state.

        The API exposes no way to read the current setting, so the state stays
        None rather than asserting a value that was never reported.
        """
        CoordinatorEntity.__init__(self, coordinator)
        RainPointHubDevice.__init__(self, hub_info)
        self._attr_unique_id = f"rainpoint_hub_{hub_info.get('hid', 'unknown')}_broadcast"
        self._attr_name = f"{hub_info.get('name') or 'RainPoint Hub'} Automatic Broadcast"
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
