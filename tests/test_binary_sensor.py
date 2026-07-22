"""Tests for the binary_sensor platform setup and the push connection entity."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from custom_components.rainpoint.binary_sensor import async_setup_entry
from custom_components.rainpoint.const import DOMAIN, PUSH_CONNECTED_UNIQUE_ID_SUFFIX
from custom_components.rainpoint.hub_entities import RainPointPushConnectedBinarySensor


def _make_hass(hubs=None, mqtt_client=MagicMock):
    """Return a mock hass whose entry object graph mirrors __init__.async_setup_entry."""
    coord = MagicMock()
    coord.data = {"hubs": hubs if hubs is not None else [], "sensors": {}}
    hass = MagicMock()
    entry = MagicMock()
    entry.entry_id = "test_entry"
    data = {"coordinator": coord}
    if mqtt_client is not None:
        data["mqtt_client"] = mqtt_client() if callable(mqtt_client) else mqtt_client
    hass.data = {DOMAIN: {entry.entry_id: data}}
    return hass, entry, coord


def _hub(hid=100, name="Hub 1", mid=None):
    return {"hid": hid, "mid": mid if mid is not None else hid, "name": name, "model": "HTV0540FRF"}


class TestBinarySensorSetupEntry:
    """Tests for binary_sensor async_setup_entry."""

    @pytest.mark.asyncio
    async def test_no_mqtt_client_registers_nothing(self):
        """With push disabled (no mqtt_client), the platform adds no entities."""
        hass, entry, _coord = _make_hass(hubs=[_hub()], mqtt_client=None)
        add = MagicMock()

        await async_setup_entry(hass, entry, add)

        add.assert_not_called()

    @pytest.mark.asyncio
    async def test_one_connected_entity_bound_to_the_clients_hub(self):
        """Exactly one push-connected sensor is created, for the hub the single
        MQTT client is bound to -- not one per configured hub (which would show
        unrelated hubs the shared client's state)."""
        hubs = [_hub(100, "Hub 1", mid=111), _hub(200, "Hub 2", mid=222)]
        client = MagicMock()
        client.hub_mid = 222  # client is bound to the second hub
        hass, entry, _coord = _make_hass(hubs=hubs, mqtt_client=client)
        add = MagicMock()

        await async_setup_entry(hass, entry, add)

        add.assert_called_once()
        entities = add.call_args[0][0]
        assert len(entities) == 1
        assert isinstance(entities[0], RainPointPushConnectedBinarySensor)

    @pytest.mark.asyncio
    async def test_no_hubs_adds_no_entities(self):
        """Push enabled but no hubs -> no entities and no add call."""
        hass, entry, _coord = _make_hass(hubs=[], mqtt_client=MagicMock())
        add = MagicMock()

        await async_setup_entry(hass, entry, add)

        add.assert_not_called()


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
