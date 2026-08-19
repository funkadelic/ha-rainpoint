"""Binary sensor entities for RainPoint integration."""

import logging

from homeassistant.components.binary_sensor import BinarySensorDeviceClass, BinarySensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN, HIC801W_STATION_COUNT, MODEL_HIC801W
from .coordinator import SILENT_DATA_TYPE, RainPointCoordinator
from .entity import LateEntityAdder, RainPointSubDeviceEntity, register_late_adder
from .hub_entities import (
    RainPointHubConnectivityBinarySensor,
    RainPointPushConnectedBinarySensor,
    resolve_connectivity_hubs,
    resolve_push_diagnostic_hubs,
)

_LOGGER = logging.getLogger(__name__)


class RainPointHicStationWateringBinarySensor(RainPointSubDeviceEntity, BinarySensorEntity):
    """One HIC801W station's watering state, as a boolean per station.

    A plain ``binary_sensor`` with ``device_class`` RUNNING, not an ENUM
    open/closed sensor mirroring ``RainPointZoneStateSensor``: that precedent
    exists to hold valve vocabulary in reserve for a model whose control half
    was unproven, and it models an irrigation station as a valve it is not.
    Whether a station is watering is literally a boolean. Station control has
    since shipped, and its valve entities were added alongside these rather
    than replacing them, the same way the HTV210B keeps its read-only per-zone
    state sensor even after gaining a valve entity: deleting these would
    strand persisted registry rows rather than remove them.

    The eight entities are a fan-out this integration invents, not one the
    wire hands it: the 279 accessory sends one aggregate record carrying a
    single running-station number, even though its portNumber is 8, and the
    eight entities are that one number projected per station.
    """

    _attr_device_class = BinarySensorDeviceClass.RUNNING

    def __init__(
        self,
        coordinator: RainPointCoordinator,
        sensor_key: str,
        sensor_info: dict,
        base_slug: str,
        station_num: int,
    ) -> None:
        """Bind to one station number on an HIC801W sensor key."""
        super().__init__(coordinator, sensor_key, sensor_info, base_slug)
        self._station_num = station_num
        self._attr_unique_id = f"rainpoint_{base_slug}_station{station_num}_watering"
        # The name is the bare label, never prefixed with a device name:
        # Home Assistant's device page strips an exact device-name prefix,
        # and a device rename breaks that match. The only value interpolated
        # is the integer station number.
        self._attr_name = f"Station {station_num} Watering"

    @property
    def is_on(self) -> bool | None:
        """Return whether this station is the one currently running.

        None whenever the reading is missing or the frame failed its shape
        check: an automation must not be able to read a
        definite False as evidence that a station is off when the frame did
        not parse at all. `available` deliberately stays True on that path,
        because the device is reachable and still polling and it is the
        payload that did not parse, so `unavailable` would misreport the
        cause.

        A `current_station` outside 0 through 8 is treated the same way, for
        the same reason and on the same evidence as
        RainPointHicCurrentStationSensor's closed option list: the
        decoder's shape check rejects only on a non-zero b3 and does not
        itself exclude an out-of-range b0, so without this guard a single
        corrupt byte would make all eight stations report a confident False
        rather than no state.
        """
        data = self._sensor_data
        if not data:
            return None
        current_station = data.get("current_station")
        if not isinstance(current_station, int) or not (0 <= current_station <= HIC801W_STATION_COUNT):
            return None
        return current_station == self._station_num


def _build_hic801w_station_entities(coordinator: RainPointCoordinator, key: str, info: dict) -> list:
    """Return the eight station-watering entities for one HIC801W sensor key.

    Returns [] for any other model, and [] for a silent entry. The silent
    guard is not decorative: eight entities on a device that has never
    reported would read available-shaped with no state, the "looks wired up
    while reading nothing" outcome the sensor platform's own guard
    (sensor.py's silent-entry check in `_create_sensor_entities`) exists to
    prevent. Returning [] here is also what makes the late-add path do the
    right thing, since the adder records nothing for a poll that built
    nothing and offers all eight on the poll the device starts reporting.

    The count is fixed at eight from the model rather than derived from the
    payload, unlike the HTV valve factories which derive zones from the
    decoded frame: this frame carries one aggregate record and no
    per-station enumeration to derive from, and variant 279 declares
    portNumber 8.
    """
    if info.get("model") != MODEL_HIC801W:
        return []
    if (info.get("data") or {}).get("type") == SILENT_DATA_TYPE:
        return []

    base_slug = f"{info.get('hid', '')}_{info.get('mid', '')}_{info.get('addr', '')}"
    return [
        RainPointHicStationWateringBinarySensor(coordinator, key, info, base_slug, station_num)
        for station_num in range(1, HIC801W_STATION_COUNT + 1)
    ]


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

    A third population, the HIC801W's eight per-station watering sensors, is
    added through a LateEntityAdder the same way valve.py and number.py
    already do. Entity creation is otherwise one-shot from the single
    post-first-refresh snapshot, so a controller that is silent at setup
    would be unreachable rather than delayed without the adder also armed as
    a coordinator listener.
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

    def build(key: str, info: dict) -> list:
        return _build_hic801w_station_entities(coordinator, key, info)

    # The literal the PLATFORMS list and every entity_id prefix already use,
    # and what the removal sweep matches on alongside the unique ID, since
    # registry uniqueness is per domain.
    adder = LateEntityAdder(coordinator, async_add_entities, build, "binary_sensor")
    # Published before anything is emitted, so the removal sweep can ask this
    # adder what it created for a key that later vanishes.
    register_late_adder(data, adder)

    # Skip any record that is not a dict, matching the defensive filter
    # valve.py and sensor.py already apply at setup.
    for key, info in coordinator.data.get("sensors", {}).items():
        if not isinstance(info, dict):
            continue
        entities.extend(adder.collect(key, info))

    _LOGGER.debug("Added %d binary sensor entities", len(entities))
    if entities:
        async_add_entities(entities)

    # Registered unconditionally. An HIC801W that is silent at setup produces
    # nothing here, and that is exactly the install this path exists for:
    # its entities appear when it starts reporting, with no reload.
    entry.async_on_unload(coordinator.async_add_listener(adder.async_on_coordinator_update))
