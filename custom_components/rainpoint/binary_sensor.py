"""Binary sensor entities for RainPoint integration."""

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .coordinator import RainPointCoordinator
from .hub_entities import RainPointPushConnectedBinarySensor

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up RainPoint binary sensor entities.

    The push connection-state entity only exists when push is enabled, so it is
    gated on the mqtt_client being present in the entry's object graph.
    """
    data = hass.data[DOMAIN][entry.entry_id]
    mqtt_client = data.get("mqtt_client")
    if mqtt_client is None:
        return

    coordinator: RainPointCoordinator = data["coordinator"]
    hubs_cfg = coordinator.data.get("hubs", [])
    hubs_dict = {str(hub.get("hid", i)): hub for i, hub in enumerate(hubs_cfg)} if isinstance(hubs_cfg, list) else hubs_cfg

    entities = [RainPointPushConnectedBinarySensor(mqtt_client, hub_info) for hub_info in hubs_dict.values()]

    _LOGGER.info("Added %d binary sensor entities", len(entities))
    if entities:
        async_add_entities(entities)
