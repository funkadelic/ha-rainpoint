"""Button entities for RainPoint integration."""

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .coordinator import RainPointCoordinator, is_hub_record
from .hub_entities import RainPointHubBroadcastButton

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up RainPoint button entities."""
    data = hass.data[DOMAIN][entry.entry_id]
    coordinator: RainPointCoordinator = data["coordinator"]

    entities = []

    hubs_cfg = coordinator.data.get("hubs", [])
    # Rejecting a non-list outright (rather than switch.py's dict-tolerant
    # fallback) matches select.py, the closest existing hub-record-walk
    # platform: the strict form is the right one for new surface, and
    # matching an existing file exactly means the reader has one shape to
    # learn rather than two.
    if not isinstance(hubs_cfg, list):
        _LOGGER.error("Expected hubs to be a list, got %s; skipping button entity setup", type(hubs_cfg).__name__)
        return
    hubs_dict = {str(hub.get("mid", i)): hub for i, hub in enumerate(hubs_cfg)}

    for _hub_key, hub_info in hubs_dict.items():
        if not is_hub_record(hub_info):
            continue
        entities.append(RainPointHubBroadcastButton(coordinator, hub_info))

    _LOGGER.info("Added %d button entities", len(entities))
    async_add_entities(entities)
