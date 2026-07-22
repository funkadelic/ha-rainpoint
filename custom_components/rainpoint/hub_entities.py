"""Hub entities for RainPoint devices."""

from __future__ import annotations

import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.components.select import SelectEntity
from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
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
    _attr_icon = "mdi:wifi"

    def __init__(self, coordinator: RainPointCoordinator, hub_info: dict):
        super().__init__(coordinator, hub_info)
        self._attr_unique_id = f"{self._attr_unique_id}_rssi"
        self._attr_name = f"{self._attr_name} Signal Strength"

    @property
    def native_value(self) -> int | None:
        # Hub RSSI would come from coordinator data if available
        # For now, return None as this might not be directly available
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


class RainPointHubChannelSelect(CoordinatorEntity, SelectEntity, RainPointHubDevice):
    """RF Channel selector for RainPoint hub."""

    _attr_entity_category = EntityCategory.CONFIG
    _attr_icon = "mdi:radio-tower"

    def __init__(self, coordinator: RainPointCoordinator, hub_info: dict):
        CoordinatorEntity.__init__(self, coordinator)
        RainPointHubDevice.__init__(self, hub_info)
        self._attr_unique_id = f"rainpoint_hub_{hub_info.get('hid', 'unknown')}_channel"
        self._attr_name = f"{hub_info.get('name', 'RainPoint Hub')} RF Channel"
        self._attr_options = ["1", "2", "3", "4", "5", "6", "7", "8", "9", "10", "11", "12", "13", "14", "15", "16"]
        self._attr_current_option = None  # Unknown until API supports reading

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
        return self._mqtt_client is not None


class RainPointPushConnectedBinarySensor(_RainPointPushDiagnosticBase, BinarySensorEntity):
    """Hub-level push connection state (on when the MQTT client is connected)."""

    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_icon = "mdi:cloud-check-variant"

    def __init__(self, mqtt_client, hub_info: dict) -> None:
        super().__init__(mqtt_client, hub_info)
        self._attr_unique_id = f"{self._attr_unique_id}_{PUSH_CONNECTED_UNIQUE_ID_SUFFIX}"
        self._attr_name = f"{self._attr_name} Push Connected"

    @property
    def is_on(self) -> bool:
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
        super().__init__(mqtt_client, hub_info)
        self._time_source = time_source
        self._attr_unique_id = f"{self._attr_unique_id}_{PUSH_LAST_MESSAGE_UNIQUE_ID_SUFFIX}"
        self._attr_name = f"{self._attr_name} Push Last Message"

    @property
    def native_value(self) -> datetime | None:
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
