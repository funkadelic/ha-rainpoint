"""Switch entities for RainPoint integration."""

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DEBUG_WORKER_URL, DOMAIN
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

    # Only register the debug switch when the worker URL is configured
    if DEBUG_WORKER_URL:
        from .debug import RainPointDebugSwitchEntity
        debug_switch = RainPointDebugSwitchEntity(hass, coordinator, entry)
        entities.append(debug_switch)

    _LOGGER.info("Added %d switch entities", len(entities))
    async_add_entities(entities)
