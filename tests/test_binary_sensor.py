"""Tests for the binary_sensor platform setup, the connectivity entity, and the push connection entity."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.rainpoint.binary_sensor import async_setup_entry
from custom_components.rainpoint.const import CONF_HIDS, DOMAIN, PUSH_CONNECTED_UNIQUE_ID_SUFFIX
from custom_components.rainpoint.coordinator import RainPointCoordinator
from custom_components.rainpoint.hub_entities import (
    RainPointHubConnectivityBinarySensor,
    RainPointPushConnectedBinarySensor,
)

_UNSET = object()


def _make_hass(hubs=None, mqtt_client=_UNSET):
    """Return a mock hass whose entry object graph mirrors __init__.async_setup_entry.

    A caller-supplied mqtt_client is used as-is (MagicMock instances are callable,
    so it must not be invoked); the default builds a fresh mock, and None means
    push is disabled.
    """
    coord = MagicMock()
    coord.data = {"hubs": hubs if hubs is not None else [], "sensors": {}, "hub_connectivity": {}}
    hass = MagicMock()
    entry = MagicMock()
    entry.entry_id = "test_entry"
    data = {"coordinator": coord}
    client = MagicMock() if mqtt_client is _UNSET else mqtt_client
    if client is not None:
        data["mqtt_client"] = client
    hass.data = {DOMAIN: {entry.entry_id: data}}
    return hass, entry, coord


def _hub(hid=100, name="Hub 1", mid=None):
    return {"hid": hid, "mid": mid if mid is not None else hid, "name": name, "model": "HTV0540FRF"}


def _bt_wrapper_hub(mid=999):
    """A Bluetooth wrapper record: every identity field present as an empty string."""
    return {"hid": 100, "mid": mid, "did": "", "mac": "", "productKey": "", "model": "", "name": ""}


class TestBinarySensorSetupEntry:
    """Tests for binary_sensor async_setup_entry."""

    @pytest.mark.asyncio
    async def test_no_mqtt_client_yields_only_the_connectivity_entity(self):
        """With push disabled (no mqtt_client), a push-disabled install with one
        real hub still yields exactly one entity: the cloud-connectivity sensor."""
        hass, entry, _coord = _make_hass(hubs=[_hub()], mqtt_client=None)
        add = MagicMock()

        await async_setup_entry(hass, entry, add)

        add.assert_called_once()
        entities = add.call_args[0][0]
        assert len(entities) == 1
        assert isinstance(entities[0], RainPointHubConnectivityBinarySensor)

    @pytest.mark.asyncio
    async def test_one_connected_entity_bound_to_the_clients_hub(self):
        """Exactly one push-connected sensor is created, for the hub the single
        MQTT client is bound to -- not one per configured hub (which would show
        unrelated hubs the shared client's state). Connectivity entities exist
        for every hub alongside it, so the assertion is by type, not by count."""
        hubs = [_hub(100, "Hub 1", mid=111), _hub(200, "Hub 2", mid=222)]
        client = MagicMock()
        client.hub_mid = 222  # client is bound to the second hub
        hass, entry, _coord = _make_hass(hubs=hubs, mqtt_client=client)
        add = MagicMock()

        await async_setup_entry(hass, entry, add)

        add.assert_called_once()
        entities = add.call_args[0][0]
        push_entities = [e for e in entities if isinstance(e, RainPointPushConnectedBinarySensor)]
        assert len(push_entities) == 1
        # Bound to the second hub (mid 222), not the first (mid 111).
        assert push_entities[0]._hub_info["mid"] == 222

    @pytest.mark.asyncio
    async def test_no_hubs_adds_no_entities(self):
        """No hubs at all -> no entities and no add call, push enabled or not."""
        hass, entry, _coord = _make_hass(hubs=[], mqtt_client=MagicMock())
        add = MagicMock()

        await async_setup_entry(hass, entry, add)

        add.assert_not_called()

    @pytest.mark.asyncio
    async def test_bluetooth_wrapper_record_yields_no_extra_connectivity_entity(self):
        """One real hub plus one Bluetooth wrapper record yields exactly one
        connectivity entity, not two."""
        hass, entry, _coord = _make_hass(hubs=[_hub(), _bt_wrapper_hub()], mqtt_client=None)
        add = MagicMock()

        await async_setup_entry(hass, entry, add)

        entities = add.call_args[0][0]
        connectivity_entities = [e for e in entities if isinstance(e, RainPointHubConnectivityBinarySensor)]
        assert len(connectivity_entities) == 1


class TestConnectivityRealTimeline:
    """Drives the real coordinator/platform-setup sequence rather than an injected snapshot."""

    @staticmethod
    def _build(connected_value="1"):
        """Return (coordinator, hass, entry, client) wired the way __init__.py wires them."""
        client = AsyncMock()
        client.get_devices_by_hid.return_value = [_hub(hid=100, mid=200)]
        client.get_multiple_device_status.return_value = [
            {"mid": 200, "subDeviceStatus": [{"id": "connected", "value": connected_value}]}
        ]

        entry = MagicMock()
        entry.entry_id = "test_entry"
        entry.data = {CONF_HIDS: [100]}

        hass = MagicMock()
        hass.data = {DOMAIN: {"test_entry": {}}}

        coordinator = RainPointCoordinator(hass, client, entry)
        hass.data[DOMAIN]["test_entry"]["coordinator"] = coordinator
        hass.data[DOMAIN]["test_entry"]["mqtt_client"] = None

        return coordinator, hass, entry, client

    @pytest.mark.asyncio
    async def test_connected_to_disconnected_transition_moves_is_on(self):
        """Construct, first refresh, platform setup, then a further refresh whose
        connected value has flipped -- asserted between each step, not from an
        injected coordinator.data snapshot."""
        coordinator, hass, entry, client = self._build(connected_value="1")

        await coordinator.async_config_entry_first_refresh()

        captured = []
        add = MagicMock(side_effect=lambda ents, **kw: captured.extend(ents))
        await async_setup_entry(hass, entry, add)

        connectivity_entities = [e for e in captured if isinstance(e, RainPointHubConnectivityBinarySensor)]
        assert len(connectivity_entities) == 1
        entity = connectivity_entities[0]
        assert entity.is_on is True

        client.get_multiple_device_status.return_value = [{"mid": 200, "subDeviceStatus": [{"id": "connected", "value": "0"}]}]
        await coordinator.async_refresh()

        # Same entity object, no second setup call, no reload.
        assert entity.is_on is False


class TestRainPointPushConnectedBinarySensor:
    """Tests for the push connection-state entity."""

    def _make(self, connected=True):
        mqtt_client = MagicMock()
        mqtt_client.connected = connected
        return RainPointPushConnectedBinarySensor(mqtt_client, _hub()), mqtt_client

    def test_is_on_tracks_connected(self):
        entity, mqtt_client = self._make(connected=True)
        assert entity.is_on is True
        mqtt_client.connected = False
        assert entity.is_on is False

    def test_unique_id_and_category_and_enabled_by_default(self):
        entity, _ = self._make()
        assert entity._attr_unique_id.endswith(f"_{PUSH_CONNECTED_UNIQUE_ID_SUFFIX}")
        assert entity._attr_entity_category == "diagnostic"
        # Enabled by default: the entity never opts out of the registry.
        assert getattr(entity, "_attr_entity_registry_enabled_default", True) is True

    def test_available_true_when_client_present(self):
        entity, _ = self._make()
        assert entity.available is True

    @pytest.mark.asyncio
    async def test_registers_and_unregisters_state_listener(self):
        """The entity subscribes to client state changes for its lifetime."""
        entity, mqtt_client = self._make()

        await entity.async_added_to_hass()
        mqtt_client.add_state_listener.assert_called_once_with(entity._handle_client_state)

        await entity.async_will_remove_from_hass()
        mqtt_client.remove_state_listener.assert_called_once_with(entity._handle_client_state)

    def test_handle_client_state_writes_ha_state(self):
        entity, _ = self._make()
        entity.async_write_ha_state = MagicMock()
        entity._handle_client_state()
        entity.async_write_ha_state.assert_called_once_with()
