"""Binary sensor entities for RainPoint integration."""

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .coordinator import RainPointCoordinator
from .hub_entities import (
    RainPointHubConnectivityBinarySensor,
    RainPointPushConnectedBinarySensor,
    resolve_connectivity_hubs,
    resolve_push_diagnostic_hubs,
)

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up RainPoint binary sensor entities.

    The cloud-connectivity entity is built for every real hub unconditionally:
    a hub that has fallen off the cloud is worth surfacing whether or not the
    user opted into push. The push connection-state entity is still gated on
    push being enabled, so it is only added when mqtt_client is present in
    the entry's object graph.
    """
    data = hass.data[DOMAIN][entry.entry_id]
    coordinator: RainPointCoordinator = data["coordinator"]
    mqtt_client = data.get("mqtt_client")

    entities = [
        RainPointHubConnectivityBinarySensor(coordinator, hub_info) for hub_info in resolve_connectivity_hubs(coordinator)
    ]
    if mqtt_client is not None:
        entities.extend(
            RainPointPushConnectedBinarySensor(mqtt_client, hub_info)
            for hub_info in resolve_push_diagnostic_hubs(coordinator, mqtt_client)
        )

    _LOGGER.debug("Added %d binary sensor entities", len(entities))
    if entities:
        async_add_entities(entities)
