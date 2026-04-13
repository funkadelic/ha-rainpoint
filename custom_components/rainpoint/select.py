"""Select entities for RainPoint integration."""

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .coordinator import RainPointCoordinator
from .hub_entities import RainPointHubChannelSelect

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up RainPoint select entities."""
    data = hass.data[DOMAIN][entry.entry_id]
    coordinator: RainPointCoordinator = data["coordinator"]

    entities = []

    hubs_cfg = coordinator.data.get("hubs", [])
    if isinstance(hubs_cfg, list):
        hubs_dict = {str(hub.get("hid", i)): hub for i, hub in enumerate(hubs_cfg)}
    else:
        hubs_dict = hubs_cfg

    for hub_key, hub_info in hubs_dict.items():
        entities.append(RainPointHubChannelSelect(coordinator, hub_info))

    _LOGGER.info("Added %d select entities", len(entities))
    async_add_entities(entities)
