"""Switch entities for RainPoint integration."""

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import CONF_GENERIC_CONTROL_ENABLED, DEBUG_WORKER_URL, DOMAIN
from .coordinator import RainPointCoordinator
from .hub_entities import RainPointHubBroadcastSwitch

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

    # Hub broadcast switches
    hubs_cfg = coordinator.data.get("hubs", [])
    hubs_dict = {str(hub.get("hid", i)): hub for i, hub in enumerate(hubs_cfg)} if isinstance(hubs_cfg, list) else hubs_cfg

    for _hub_key, hub_info in hubs_dict.items():
        entities.append(RainPointHubBroadcastSwitch(coordinator, hub_info))

    # Only register the debug switch when the worker URL is configured
    if DEBUG_WORKER_URL:
        from .debug import RainPointDebugSwitchEntity

        debug_switch = RainPointDebugSwitchEntity(hass, coordinator, entry)
        entities.append(debug_switch)

    if entry.options.get(CONF_GENERIC_CONTROL_ENABLED, False):
        # Deferred import: generic_control reaches sensor.py's
        # RainPointSensorBase transitively through generic_entities, so a
        # top-level import here would pull the whole sensor platform into
        # this module's import graph. Mirrors valve.py's identical branch and
        # justification for the same import.
        from .generic_control import build_generic_switch_entities

        # Skip any record that is not a dict so one malformed sub-device entry
        # cannot raise here and drop the hub broadcast switches already added.
        sensors_cfg = {key: info for key, info in coordinator.data.get("sensors", {}).items() if isinstance(info, dict)}
        for key, info in sensors_cfg.items():
            hid = info.get("hid", "")
            mid = info.get("mid", "")
            addr = info.get("addr", "")
            base_slug = f"{hid}_{mid}_{addr}"
            entities.extend(build_generic_switch_entities(coordinator, key, info, base_slug))

    _LOGGER.info("Added %d switch entities", len(entities))
    async_add_entities(entities)
