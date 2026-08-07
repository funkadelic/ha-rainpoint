"""Hub entities for RainPoint devices."""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.components.button import ButtonEntity
from homeassistant.components.select import SelectEntity
from homeassistant.components.sensor import SensorDeviceClass, SensorEntity, SensorStateClass
from homeassistant.components.switch import SwitchEntity
from homeassistant.const import EntityCategory
from homeassistant.core import callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .api import _parse_hub_broadcast_flag, _splice_hub_broadcast_param
from .const import (
    HUB_UNIQUE_ID_PREFIX,
    PUSH_CONNECTED_UNIQUE_ID_SUFFIX,
    PUSH_LAST_MESSAGE_UNIQUE_ID_SUFFIX,
)
from .coordinator import (
    RainPointCoordinator,
    first_hub_record,
    hub_connected_flag,
    hub_connectivity_record,
    is_hub_record,
)
from .device import RainPointHubDevice

_LOGGER = logging.getLogger(__name__)


def _hub_records(coordinator: RainPointCoordinator) -> list[dict]:
    """Return the coordinator's hub records as a list, tolerating a dict snapshot.

    The dict-or-list tolerance lives here alone so both hub resolvers below
    agree on what an empty or oddly shaped snapshot means.
    """
    hubs_cfg = (coordinator.data or {}).get("hubs", [])
    return list(hubs_cfg.values()) if isinstance(hubs_cfg, dict) else list(hubs_cfg)


def hub_record_for_mid(coordinator: RainPointCoordinator, mid) -> dict:
    """Return one hub's own live record from coordinator.data["hubs"] by mid, or {}.

    Reuses _hub_records for the dict-or-list snapshot tolerance rather than
    re-deriving it, so the two resolvers cannot disagree about what an odd
    snapshot shape means. Public-named because more than one hub entity needs
    a hub's own live record by mid, not just the broadcast switch below.
    """
    for hub in _hub_records(coordinator):
        if isinstance(hub, dict) and hub.get("mid") == mid:
            return hub
    return {}


def resolve_push_diagnostic_hubs(coordinator: RainPointCoordinator, mqtt_client) -> list[dict]:
    """Return the hub(s) the push diagnostics belong to.

    The MQTT client is built for exactly one hub, so the connection and
    last-message diagnostics are created only for that bound hub. Creating one
    per configured hub would make every hub on a multi-hub account display the
    shared client's state, even though push only ever targets the bound hub.
    """
    hubs = _hub_records(coordinator)
    if not hubs:
        return []
    bound_mid = getattr(mqtt_client, "hub_mid", None)
    if bound_mid is not None:
        match = next((hub for hub in hubs if hub.get("mid") == bound_mid), None)
        if match is not None:
            return [match]
    # No mid to match on (or no matching hub): fall back to the first real hub,
    # which is the one the client was built from. A Bluetooth wrapper record can
    # occupy slot 0 without being a hub at all.
    fallback = first_hub_record(hubs)
    return [fallback] if fallback is not None else []


def resolve_connectivity_hubs(coordinator: RainPointCoordinator) -> list[dict]:
    """Return every real hub, for building one cloud-connectivity entity each.

    Unlike resolve_push_diagnostic_hubs, which returns only the single hub
    the shared MQTT client is bound to, this returns every real hub: cloud
    connectivity is a per-hub fact, not a property of the single MQTT client.
    """
    return [hub for hub in _hub_records(coordinator) if is_hub_record(hub)]


# The four hub classes below deliberately leave CoordinatorEntity
# unparameterized, unlike the sub-device bases in entity.py, valve.py, number.py
# and generic_control.py. Each initializes every base in its inheritance chain
# explicitly rather than cooperatively, through the unbound
# CoordinatorEntity.__init__(self, coordinator), and a subscripted base makes
# that call's self parameter unsatisfiable. They read nothing off the
# coordinator that the narrower type would have caught.
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
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:wifi"

    def __init__(self, coordinator: RainPointCoordinator, hub_info: dict):
        super().__init__(coordinator, hub_info)
        self._attr_unique_id = f"{self._attr_unique_id}_rssi"
        self._attr_name = f"{self._attr_name} Signal Strength"

    @property
    def native_value(self) -> int | None:
        mid = self._hub_info.get("mid")
        # `or {}` / `or []` also handle an explicit None value (not just a missing
        # key), which .get(key, default) would pass through and then crash on.
        mid_status = self.coordinator.data.get("status", {}).get(mid) or {}
        for entry in mid_status.get("subDeviceStatus") or []:
            if isinstance(entry, dict) and entry.get("id") == "state":
                return _parse_hub_rssi(entry.get("value"))
        return None


class RainPointHubDeviceIDSensor(RainPointHubSensorBase):
    """Device ID sensor for RainPoint hub."""

    _attr_icon = "mdi:identifier"

    def __init__(self, coordinator: RainPointCoordinator, hub_info: dict):
        """Name the entity after the hub and key the entity to its home id and mid."""
        super().__init__(coordinator, hub_info)
        # The five inline ids in this file keep the defaulted mid lookup so the
        # file carries one spelling of the segment. It cannot actually default:
        # RainPointHubDevice.__init__ ran first and direct-indexes mid, so a
        # record without one raises before this line executes.
        self._attr_unique_id = (
            f"{HUB_UNIQUE_ID_PREFIX}{hub_info.get('hid', 'unknown')}_{hub_info.get('mid', 'unknown')}_device_id"
        )
        self._attr_name = f"{hub_info.get('name') or 'RainPoint Hub'} Device ID"

    @property
    def native_value(self) -> str | int | None:
        # `did` is the device ID the RainPoint app shows; the home id (hid) is only a
        # fallback for a hub record that omits it.
        return self._hub_info.get("did") or self._hub_info.get("hid")


class RainPointHubFirmwareSensor(RainPointHubSensorBase):
    """Firmware version sensor for RainPoint hub."""

    _attr_icon = "mdi:chip"

    def __init__(self, coordinator: RainPointCoordinator, hub_info: dict):
        """Name the entity after the hub and key the entity to its home id and mid."""
        super().__init__(coordinator, hub_info)
        self._attr_unique_id = f"{HUB_UNIQUE_ID_PREFIX}{hub_info.get('hid', 'unknown')}_{hub_info.get('mid', 'unknown')}_firmware"
        self._attr_name = f"{hub_info.get('name') or 'RainPoint Hub'} Firmware Version"

    @property
    def native_value(self) -> str | None:
        return self._hub_info.get("softVer")


class RainPointHubMACSensor(RainPointHubSensorBase):
    """MAC address sensor for RainPoint hub."""

    _attr_icon = "mdi:network-outline"

    def __init__(self, coordinator: RainPointCoordinator, hub_info: dict):
        """Name the entity after the hub and key the entity to its home id and mid."""
        super().__init__(coordinator, hub_info)
        self._attr_unique_id = f"{HUB_UNIQUE_ID_PREFIX}{hub_info.get('hid', 'unknown')}_{hub_info.get('mid', 'unknown')}_mac"
        self._attr_name = f"{hub_info.get('name') or 'RainPoint Hub'} MAC Address"

    @property
    def native_value(self) -> str | None:
        return self._hub_info.get("mac")


class RainPointHubConnectivityBinarySensor(CoordinatorEntity, BinarySensorEntity, RainPointHubDevice):
    """Hub-level cloud connectivity: on while the RainPoint cloud reports this hub connected.

    Reads the coordinator's already-shaped `hub_connectivity` field rather
    than re-scanning subDeviceStatus itself; RainPointHubRSSISensor is the
    precedent for reading a non-D status id out of the status response, not
    the implementation this class copies. `state_raw` is carried undecoded:
    its first field read '0' in every observed condition on both sides of a
    real power cycle, so assigning it a meaning would be a guess shipped as
    fact. Hub-level entities, this one included, are created once from the
    first refresh snapshot like every other hub entity; state refresh after
    setup still happens normally through the CoordinatorEntity listener this
    base class registers -- only entity creation is one-shot.
    """

    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_should_poll = False

    def __init__(self, coordinator: RainPointCoordinator, hub_info: dict) -> None:
        """Build the connectivity entity with a per-hub unique id."""
        CoordinatorEntity.__init__(self, coordinator)
        RainPointHubDevice.__init__(self, hub_info)
        # Hub identity carries both the home id and the hub's mid, here and at
        # every other hub-level site, because a home can hold more than one hub.
        self._attr_unique_id = (
            f"{HUB_UNIQUE_ID_PREFIX}{hub_info.get('hid', 'unknown')}_{hub_info.get('mid', 'unknown')}_connectivity"
        )
        self._attr_name = f"{hub_info.get('name') or 'RainPoint Hub'} Cloud Connection"

    @property
    def _record(self) -> dict:
        """Return this hub's connectivity record, or {} when none exists yet.

        The partial-snapshot tolerance lives in hub_connectivity_record, shared
        with the sub-device attributes and valve availability.
        """
        return hub_connectivity_record(self.coordinator, self._hub_info.get("mid"))

    @property
    def is_on(self) -> bool | None:
        """Return True/False/None through the shared tri-state mapping."""
        return hub_connected_flag(self._record)

    @property
    def icon(self) -> str:
        """Return an icon that tracks the state rather than fixing one glyph.

        A class-level _attr_icon would override the CONNECTIVITY device
        class's own on/off pair, leaving a connected hub showing a
        cloud-offline glyph, which reads as a false alarm on the one entity
        whose whole job is an at-a-glance health check. Unknown shares the
        offline glyph deliberately: is_on is None only when the cloud has not
        said either way, and claiming a healthy cloud there would overstate
        what is known.
        """
        return "mdi:cloud-check-variant" if self.is_on else "mdi:cloud-off-outline"

    @property
    def extra_state_attributes(self) -> dict:
        """Return the cloud change timestamp and the raw, undecoded state value.

        Both keys are always present, with None values when the underlying
        fields are absent, so the attribute never simply vanishes.
        """
        record = self._record
        return {
            "changed_at": record.get("changed_at"),
            "state_raw": record.get("state_raw"),
        }

    @property
    def available(self) -> bool:
        """Always available: an unknown connectivity state renders as unknown, not missing."""
        return True


def _parse_hub_rssi(state_value) -> int | None:
    """Extract the hub's own RSSI (dBm) from its `state` status value.

    The hub reports `state` as a comma-separated string like `0,-52` whose second
    field is the signed WiFi RSSI (distinct from the per-valve RF link RSSI in the
    device payload). Returns None when the value is absent or unparseable.
    """
    if not isinstance(state_value, str):
        return None
    parts = state_value.split(",")
    if len(parts) < 2:
        return None
    try:
        return int(parts[1])
    except ValueError:
        return None


_RF_CHANNEL_FALLBACK = range(1, 17)


def _hub_function(hub_info: dict) -> dict:
    """Parse the hub record's `function` JSON blob into a dict, or {} on any problem.

    The blob looks like `{"model":"...","childMax":40,"RF":7,"SM":7,...}`; it is
    a string in the API response, so it is parsed here rather than indexed.
    """
    raw = hub_info.get("function")
    if not isinstance(raw, str) or not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except (ValueError, TypeError):
        _LOGGER.debug("Hub %s has an unparseable function blob: %r", hub_info.get("hid"), raw)
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _hub_rf_current_channel(hub_info: dict) -> int | None:
    """Return the hub's current RF channel from its `recich` field, or None.

    `recich` is the receive channel the hub is tuned to (matches the value the
    RainPoint app shows). bool is excluded because it is an int subclass.
    """
    value = hub_info.get("recich")
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _hub_rf_channel_options(hub_info: dict) -> list[str]:
    """Return the selectable RF channels for the hub as strings.

    The hub's `function.RF` value is a bitmask of supported channels (bit i set
    means channel i+1 is available); e.g. 7 (0b111) -> channels 1, 2, 3. Falls
    back to the full 1-16 range when the field is missing or unusable, and always
    includes the current channel so a live value outside the mask still renders.
    """
    rf = _hub_function(hub_info).get("RF")
    if isinstance(rf, bool) or not isinstance(rf, int) or rf <= 0:
        channels = set(_RF_CHANNEL_FALLBACK)
    else:
        channels = {bit + 1 for bit in range(rf.bit_length()) if rf >> bit & 1}
    current = _hub_rf_current_channel(hub_info)
    if current is not None:
        channels.add(current)
    return [str(channel) for channel in sorted(channels)]


class RainPointHubChannelSelect(CoordinatorEntity, SelectEntity, RainPointHubDevice):
    """RF Channel selector for RainPoint hub."""

    _attr_entity_category = EntityCategory.CONFIG
    _attr_icon = "mdi:radio-tower"

    def __init__(self, coordinator: RainPointCoordinator, hub_info: dict):
        """Build the RF channel selector from the hub's supported-channel bitmask.

        Options come from the hub's function blob and the current option from
        its recich field; both resolve to nothing selectable when absent.
        """
        CoordinatorEntity.__init__(self, coordinator)
        RainPointHubDevice.__init__(self, hub_info)
        self._attr_unique_id = f"{HUB_UNIQUE_ID_PREFIX}{hub_info.get('hid', 'unknown')}_{hub_info.get('mid', 'unknown')}_channel"
        self._attr_name = f"{hub_info.get('name') or 'RainPoint Hub'} RF Communication Channel"
        self._attr_options = _hub_rf_channel_options(hub_info)
        # Current channel comes from the hub record; None renders as unknown when
        # the field is absent. Selecting a channel is still unsupported (below).
        current = _hub_rf_current_channel(hub_info)
        self._attr_current_option = str(current) if current is not None else None

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
        """Bind the diagnostic to the MQTT client it reads its live state from."""
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
        """Available whenever the push MQTT client is present."""
        return self._mqtt_client is not None


class RainPointPushConnectedBinarySensor(_RainPointPushDiagnosticBase, BinarySensorEntity):
    """Hub-level push connection state (on when the MQTT client is connected)."""

    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_icon = "mdi:cloud-check-variant"

    def __init__(self, mqtt_client, hub_info: dict) -> None:
        """Build the connection-state entity with a stable per-hub unique id."""
        super().__init__(mqtt_client, hub_info)
        self._attr_unique_id = f"{self._attr_unique_id}_{PUSH_CONNECTED_UNIQUE_ID_SUFFIX}"
        self._attr_name = f"{self._attr_name} Push Connected"

    @property
    def is_on(self) -> bool:
        """Return True while the MQTT client is connected."""
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
        """Build the last-message entity; time_source is injectable for tests."""
        super().__init__(mqtt_client, hub_info)
        self._time_source = time_source
        self._attr_unique_id = f"{self._attr_unique_id}_{PUSH_LAST_MESSAGE_UNIQUE_ID_SUFFIX}"
        self._attr_name = f"{self._attr_name} Push Last Message"

    @property
    def native_value(self) -> datetime | None:
        """Return the last-message time as an absolute UTC datetime, or None."""
        last = self._mqtt_client.last_message_at
        if last is None:
            return None
        age = self._time_source() - last
        if age < 0:
            age = 0.0
        return datetime.now(UTC) - timedelta(seconds=age)


class RainPointHubBroadcastSwitch(CoordinatorEntity, SwitchEntity, RainPointHubDevice):
    """Automatic Broadcast Time switch for RainPoint hub.

    is_on is read live off coordinator.data["hubs"] on every access rather
    than off self._hub_info: _collect_hubs allocates a fresh dict(hub) every
    poll and nothing in this package reassigns self._hub_info after
    construction, so a flag read from it would appear stuck at whatever the
    entity's first refresh happened to see, for the entity's entire lifetime.
    """

    _attr_entity_category = EntityCategory.CONFIG
    _attr_icon = "mdi:clock-outline"

    def __init__(self, coordinator: RainPointCoordinator, hub_info: dict):
        """Build the broadcast switch; is_on is derived live, never stored here."""
        CoordinatorEntity.__init__(self, coordinator)
        RainPointHubDevice.__init__(self, hub_info)
        self._attr_unique_id = (
            f"{HUB_UNIQUE_ID_PREFIX}{hub_info.get('hid', 'unknown')}_{hub_info.get('mid', 'unknown')}_broadcast"
        )
        self._attr_name = f"{hub_info.get('name') or 'RainPoint Hub'} Automatic Broadcast Time"
        # The post-write override: set only after a write's cloud
        # acknowledgment, cleared by the next real poll so a poll that
        # contradicts the command always wins.
        self._optimistic: bool | None = None
        # Identity of coordinator.data["hubs"] as of the last time it was
        # observed -- see _handle_coordinator_update for why this, and not
        # the optimistic flag itself, is what a push notification must not
        # be allowed to disturb. Seeded from the coordinator's current data
        # rather than left None: CoordinatorEntity.async_added_to_hass only
        # registers the listener, it never calls _handle_coordinator_update
        # itself, so a None seed would make the very first push after setup
        # look like a poll and clear an optimistic value that was never
        # actually confirmed or contradicted.
        current_hubs = coordinator.data.get("hubs") if coordinator.data else None
        self._hubs_snapshot_id: int | None = id(current_hubs) if current_hubs is not None else None

    @property
    def available(self) -> bool:
        return True

    @property
    def _record(self) -> dict:
        """Return this hub's own live record, or {} when none exists yet."""
        return hub_record_for_mid(self.coordinator, self._hub_info.get("mid"))

    @property
    def is_on(self) -> bool | None:
        """Return the optimistic override if one is pending, else the live poll value.

        Reads only the coordinator-backed record, never the frozen snapshot
        attribute -- see the class docstring for why that distinction matters.
        """
        if self._optimistic is not None:
            return self._optimistic
        return _parse_hub_broadcast_flag(self._record.get("param"))

    @callback
    def _handle_coordinator_update(self) -> None:
        """Clear the optimistic override only on a real poll, not on every push.

        The first override of this CoordinatorEntity hook anywhere in this
        package. Without it, a command's optimistic value would outlive the
        poll that contradicts it, rather than acting as a bridge across the
        poll interval until the next real read. Does not reassign
        self._hub_info -- that snapshot stays frozen deliberately, since
        changing it would change every other hub entity reading it.

        This hook fires on every coordinator listener notification, not only
        a genuine 120s poll: apply_push_update and apply_hub_push_update both
        call async_update_listeners() for any unrelated sub-device reading or
        hub connectivity edge, and neither one rebuilds coordinator.data --
        both shallow-copy the top-level dict and carry "hubs" forward by
        reference. A REST poll's _collect_hubs is the only thing that ever
        allocates a fresh "hubs" list. So the identity of coordinator.data
        ["hubs"] is what distinguishes "the hub record may actually have
        changed" from "some other device pushed a reading" -- without this
        check, any push on a push-enabled install (the default) clears a
        just-set optimistic value within moments, well before the poll that
        is supposed to be the one to confirm or correct it.
        """
        current_hubs = self.coordinator.data.get("hubs") if self.coordinator.data else None
        current_id = id(current_hubs) if current_hubs is not None else None
        if current_id != self._hubs_snapshot_id:
            self._hubs_snapshot_id = current_id
            self._optimistic = None
        super()._handle_coordinator_update()

    async def _async_set_broadcast(self, enabled: bool) -> None:
        """Splice, write, and (only on success) apply the requested flag.

        Ordering is load-bearing: the optimistic value is set only after the
        client's write returns successfully, so a raised write leaves no
        optimistic state behind.
        """
        spliced = _splice_hub_broadcast_param(self._record.get("param"), enabled)
        if spliced is None:
            raise HomeAssistantError("The hub's settings could not be read, so this setting cannot be changed")
        client = self.coordinator._client
        await client.update_main_param(mid=self._hub_info["mid"], param=spliced)
        # Optimistic, deliberately diverging from generic_control.py's
        # never-optimistic rule: that rule guards against showing an unread
        # hardware actuation state, while this write receives a genuine cloud
        # acknowledgment (code 0), which is the same case Home Assistant
        # core's own template and MQTT switches treat this way.
        self._optimistic = enabled
        self.async_write_ha_state()

    async def async_turn_on(self) -> None:
        """Turn on automatic broadcast."""
        await self._async_set_broadcast(True)

    async def async_turn_off(self) -> None:
        """Turn off automatic broadcast."""
        await self._async_set_broadcast(False)


class RainPointHubBroadcastButton(CoordinatorEntity, ButtonEntity, RainPointHubDevice):
    """The hub's one-shot time broadcast action.

    Stateless by Home Assistant's own design: ButtonEntity.async_press
    returns None, and this override writes no entity state, sets no
    attribute, creates no Repairs issue and fires no notification. A
    successful call means only that the cloud accepted the command --
    whether a broadcast actually reached any sub-device is unobservable from
    anything this integration reads, so nothing here may imply otherwise.
    """

    _attr_entity_category = EntityCategory.CONFIG
    _attr_icon = "mdi:broadcast"

    def __init__(self, coordinator: RainPointCoordinator, hub_info: dict):
        """Build the broadcast button with a unique id distinct from the switch's _broadcast id."""
        CoordinatorEntity.__init__(self, coordinator)
        RainPointHubDevice.__init__(self, hub_info)
        self._attr_unique_id = (
            f"{HUB_UNIQUE_ID_PREFIX}{hub_info.get('hid', 'unknown')}_{hub_info.get('mid', 'unknown')}_broadcast_now"
        )
        self._attr_name = f"{hub_info.get('name') or 'RainPoint Hub'} Broadcast Time Now"

    @property
    def available(self) -> bool:
        return True

    @property
    def _record(self) -> dict:
        """Return this hub's own live record, or {} when none exists yet."""
        return hub_record_for_mid(self.coordinator, self._hub_info.get("mid"))

    async def async_press(self) -> None:
        """Send the one-shot broadcast command.

        deviceName and productKey are read off the live hub record rather
        than self._hub_info: self._hub_info is the snapshot from first
        build, and this is a write, so an identity that changed under the
        entity would produce a request the cloud rejects with nothing to
        show the user. A raised RainPointApiError is left to propagate --
        the client already raises on any body code other than 0 or 4 and on
        any transport failure, which is the verdict this button wants, and
        Home Assistant surfaces a raised exception from a button press to
        the user synchronously.

        A momentarily absent record (hub not yet in coordinator.data) is
        refused outright, the same way the sibling switch's
        _async_set_broadcast refuses to write from an unreadable param:
        sending deviceName="" and productKey="" would not raise here on its
        own, and it is exactly the kind of request the cloud might silently
        misroute rather than reject.
        """
        record = self._record
        if not record.get("deviceName") or not record.get("productKey"):
            raise HomeAssistantError("The hub's device identity could not be read, so this command cannot be sent")
        client = self.coordinator._client
        await client.control_work_mode(
            mid=self._hub_info["mid"],
            addr=0,
            device_name=record.get("deviceName") or "",
            product_key=record.get("productKey") or "",
            port=1,
            mode=0,
        )
        _LOGGER.info("Broadcast time now pressed for hub mid=%s", self._hub_info["mid"])
