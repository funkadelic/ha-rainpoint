"""Button entities for RainPoint integration."""

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import CONF_HIC_CONTROL_PROBE_ENABLED, DOMAIN, MODEL_HIC801W
from .control_probe_entities import RainPointProbeRainDelayButton, RainPointProbeStationButton
from .coordinator import RainPointCoordinator, is_hub_record
from .hub_entities import RainPointHubBroadcastButton

_LOGGER = logging.getLogger(__name__)


def _probe_entities(coordinator: RainPointCoordinator, entry) -> list:
    """Return the HIC encoding-probe buttons, or [] when they are not wanted.

    Two gates, both required. The option ships off, so an ordinary user never
    reaches this; and only a device whose model actually declares the
    single-datapoint control shape gets the buttons, so turning the option on
    does not scatter probe buttons across an account's other hardware.
    """
    if not entry.options.get(CONF_HIC_CONTROL_PROBE_ENABLED, False):
        return []

    entities = []
    for sensor_key, info in ((coordinator.data or {}).get("sensors") or {}).items():
        if not isinstance(info, dict) or info.get("model") != MODEL_HIC801W:
            continue
        base_slug = sensor_key
        entities.append(RainPointProbeRainDelayButton(coordinator, sensor_key, info, base_slug, entry.entry_id))
        entities.append(RainPointProbeStationButton(coordinator, sensor_key, info, base_slug, entry.entry_id))
    if entities:
        _LOGGER.warning(
            "HIC control-encoding probe is enabled: %d probe buttons added. These write commands to the controller.",
            len(entities),
        )
    return entities


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
    # platform. A non-dict list member is skipped rather than crashing
    # setup on hub.get(): is_hub_record already rejects it below, so
    # filtering here just moves that rejection earlier.
    if not isinstance(hubs_cfg, list):
        _LOGGER.error("Expected hubs to be a list, got %s; skipping button entity setup", type(hubs_cfg).__name__)
        return
    hubs_dict = {str(hub.get("mid", i)): hub for i, hub in enumerate(hubs_cfg) if isinstance(hub, dict)}

    for _hub_key, hub_info in hubs_dict.items():
        if not is_hub_record(hub_info):
            continue
        entities.append(RainPointHubBroadcastButton(coordinator, hub_info))

    entities.extend(_probe_entities(coordinator, entry))

    _LOGGER.info("Added %d button entities", len(entities))
    async_add_entities(entities)
