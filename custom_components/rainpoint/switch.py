"""Switch entities for RainPoint integration."""

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .debug import RainPointDebugSwitchEntity
from .coordinator import RainPointCoordinator

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up RainPoint switch entities."""
    data = hass.data[DOMAIN][entry.entry_id]
    coordinator: RainPointCoordinator = data["coordinator"]

    entities = []

    # Add debug switch
    debug_switch = RainPointDebugSwitchEntity(hass, coordinator, entry)
    entities.append(debug_switch)

    _LOGGER.info(f"Added {len(entities)} switch entities")
    async_add_entities(entities)
