"""Tests for custom_components.rainpoint.__init__ (integration lifecycle)."""

import asyncio
import contextlib
import inspect
from types import SimpleNamespace
from typing import ClassVar
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.helpers import issue_registry as ir

from custom_components.rainpoint import (
    DOMAIN,
    _generic_control_row_removal_reason,
    _generic_row_removal_reason,
    _reconcile_sub_device_parents,
    _reconcile_sub_device_parents_on_updates,
    _remove_stale_generic_entities,
    async_reload_integration,
    async_setup,
    async_setup_entry,
    async_unload_entry,
)
from custom_components.rainpoint.const import (
    CONF_GENERIC_CONTROL_ENABLED,
    CONF_GENERIC_ENTITIES_ENABLED,
    CONF_PUSH_ENABLED,
    MODEL_HCS026FRF,
    PUSH_HUB_IDENTITY_ISSUE_ID,
)
from custom_components.rainpoint.repairs import push_hub_identity_issue_id
from tests.helpers import make_silent_wrapper_hub_record


def _make_entry(entry_id="test_entry_id"):
    """Make entry helper."""
    entry = MagicMock()
    entry.entry_id = entry_id
    entry.data = {
        "area_code": "1",
        "email": "test@example.com",
        "password": "secret",
        "hids": [42],
        "token": "tok",
        "refresh_token": "ref",
        "token_expires_at": 9999999999,
    }
    entry.options = {}
    return entry


def _make_hass():
    """Make hass helper."""
    hass = MagicMock()
    hass.data = {}
    hass.config_entries = MagicMock()
    hass.services = MagicMock()
    return hass


class TestAsyncSetup:
    """Tests for AsyncSetup."""

    @pytest.mark.asyncio
    async def test_async_setup_returns_true(self):
        """Async setup returns true."""
        hass = _make_hass()
        result = await async_setup(hass, {})
        assert result is True


class TestAsyncSetupEntry:
    """Tests for AsyncSetupEntry."""

    @pytest.mark.asyncio
    async def test_async_setup_entry_creates_coordinator(self):
        """Async setup entry creates coordinator."""
        hass = _make_hass()
        entry = _make_entry()

        mock_client = MagicMock()
        mock_client.restore_tokens = MagicMock()

        mock_coordinator = MagicMock()
        mock_coordinator.async_config_entry_first_refresh = AsyncMock()

        hass.config_entries.async_forward_entry_setups = AsyncMock()

        with (
            patch("custom_components.rainpoint.RainPointClient", return_value=mock_client),
            patch(
                "custom_components.rainpoint.coordinator.RainPointCoordinator",
                return_value=mock_coordinator,
            ),
        ):
            result = await async_setup_entry(hass, entry)

        assert result is True
        assert DOMAIN in hass.data
        assert entry.entry_id in hass.data[DOMAIN]
        stored = hass.data[DOMAIN][entry.entry_id]
        assert "client" in stored
        assert "coordinator" in stored

    @pytest.mark.asyncio
    async def test_async_setup_entry_reuses_existing_client(self):
        """A retry (client already stored) reuses it instead of building a new one.

        Keeping one client across ConfigEntryNotReady retries preserves its login
        cooldown, so a throttle does not get reset into a fresh login every retry.
        """
        hass = _make_hass()
        entry = _make_entry()

        existing_client = MagicMock()
        hass.data = {DOMAIN: {entry.entry_id: {"client": existing_client}}}

        mock_coordinator = MagicMock()
        mock_coordinator.async_config_entry_first_refresh = AsyncMock()
        hass.config_entries.async_forward_entry_setups = AsyncMock()

        with (
            patch("custom_components.rainpoint.RainPointClient") as mock_client_cls,
            patch(
                "custom_components.rainpoint.coordinator.RainPointCoordinator",
                return_value=mock_coordinator,
            ),
        ):
            result = await async_setup_entry(hass, entry)

        assert result is True
        # The stored client was reused, not reconstructed or re-registered.
        mock_client_cls.assert_not_called()
        existing_client.restore_tokens.assert_not_called()
        existing_client.register_relogin_listener.assert_not_called()
        assert hass.data[DOMAIN][entry.entry_id]["client"] is existing_client

    @pytest.mark.asyncio
    async def test_async_setup_entry_registers_reload_listener(self):
        """An update listener is registered so an options change reloads the entry."""
        hass = _make_hass()
        entry = _make_entry()

        mock_client = MagicMock()
        mock_client.restore_tokens = MagicMock()
        mock_coordinator = MagicMock()
        mock_coordinator.async_config_entry_first_refresh = AsyncMock()
        hass.config_entries.async_forward_entry_setups = AsyncMock()

        with (
            patch("custom_components.rainpoint.RainPointClient", return_value=mock_client),
            patch(
                "custom_components.rainpoint.coordinator.RainPointCoordinator",
                return_value=mock_coordinator,
            ),
        ):
            from custom_components.rainpoint import async_reload_entry

            result = await async_setup_entry(hass, entry)

        assert result is True
        entry.add_update_listener.assert_called_once_with(async_reload_entry)
        entry.async_on_unload.assert_any_call(entry.add_update_listener.return_value)

    @pytest.mark.asyncio
    async def test_async_setup_entry_push_disabled_no_mqtt_client(self):
        """With push disabled (default), no mqtt_client is stored and no background task runs."""
        hass = _make_hass()
        entry = _make_entry()
        # entry.options == {} => push_enabled defaults to False

        mock_client = MagicMock()
        mock_client.restore_tokens = MagicMock()
        mock_coordinator = MagicMock()
        mock_coordinator.async_config_entry_first_refresh = AsyncMock()
        mock_coordinator.data = {"hubs": [{"deviceName": "hub-dev", "productKey": "hub-pk"}]}
        hass.config_entries.async_forward_entry_setups = AsyncMock()
        hass.async_create_background_task = MagicMock()

        with (
            patch("custom_components.rainpoint.RainPointClient", return_value=mock_client),
            patch(
                "custom_components.rainpoint.coordinator.RainPointCoordinator",
                return_value=mock_coordinator,
            ),
        ):
            result = await async_setup_entry(hass, entry)

        assert result is True
        assert "mqtt_client" not in hass.data[DOMAIN][entry.entry_id]
        hass.async_create_background_task.assert_not_called()

    @pytest.mark.asyncio
    async def test_async_setup_entry_push_enabled_creates_and_backgrounds_mqtt_client(self):
        """With push enabled, an mqtt_client is stored and async_start is backgrounded, not awaited."""
        hass = _make_hass()
        entry = _make_entry()
        entry.options = {CONF_PUSH_ENABLED: True}

        mock_client = MagicMock()
        mock_client.restore_tokens = MagicMock()
        mock_coordinator = MagicMock()
        mock_coordinator.async_config_entry_first_refresh = AsyncMock()
        mock_coordinator.data = {"hubs": [{"deviceName": "hub-dev", "productKey": "hub-pk"}]}
        hass.config_entries.async_forward_entry_setups = AsyncMock()

        mock_mqtt_client = MagicMock()
        mock_mqtt_client.async_start = AsyncMock()
        mock_mqtt_client.async_disconnect = AsyncMock()

        created_tasks = []

        def _create_background_task(coro, name=None):
            """Schedule the coroutine like the real HA helper would, without awaiting it here."""
            task = asyncio.ensure_future(coro)
            created_tasks.append(task)
            return task

        hass.async_create_background_task = MagicMock(side_effect=_create_background_task)

        with (
            patch("custom_components.rainpoint.RainPointClient", return_value=mock_client),
            patch(
                "custom_components.rainpoint.coordinator.RainPointCoordinator",
                return_value=mock_coordinator,
            ),
            patch(
                "custom_components.rainpoint.RainPointMqttClient",
                return_value=mock_mqtt_client,
            ),
        ):
            result = await async_setup_entry(hass, entry)

        assert result is True
        assert hass.data[DOMAIN][entry.entry_id]["mqtt_client"] is mock_mqtt_client
        hass.async_create_background_task.assert_called_once()
        # Not awaited synchronously by setup itself.
        assert mock_mqtt_client.async_start.await_count == 0
        entry.async_on_unload.assert_any_call(mock_mqtt_client.async_disconnect)

        for task in created_tasks:
            with contextlib.suppress(Exception):
                await task
        assert mock_mqtt_client.async_start.await_count == 1

    @pytest.mark.asyncio
    async def test_async_setup_entry_push_enabled_broker_unreachable_still_returns_true(self):
        """A broker-unreachable async_start failure never blocks/fails setup."""
        hass = _make_hass()
        entry = _make_entry()
        entry.options = {CONF_PUSH_ENABLED: True}

        mock_client = MagicMock()
        mock_client.restore_tokens = MagicMock()
        mock_coordinator = MagicMock()
        mock_coordinator.async_config_entry_first_refresh = AsyncMock()
        mock_coordinator.data = {"hubs": [{"deviceName": "hub-dev", "productKey": "hub-pk"}]}
        hass.config_entries.async_forward_entry_setups = AsyncMock()

        mock_mqtt_client = MagicMock()
        mock_mqtt_client.async_start = AsyncMock(side_effect=RuntimeError("broker unreachable"))
        mock_mqtt_client.async_disconnect = AsyncMock()

        created_tasks = []

        def _create_background_task(coro, name=None):
            """Create background task helper."""
            task = asyncio.ensure_future(coro)
            created_tasks.append(task)
            return task

        hass.async_create_background_task = MagicMock(side_effect=_create_background_task)

        with (
            patch("custom_components.rainpoint.RainPointClient", return_value=mock_client),
            patch(
                "custom_components.rainpoint.coordinator.RainPointCoordinator",
                return_value=mock_coordinator,
            ),
            patch(
                "custom_components.rainpoint.RainPointMqttClient",
                return_value=mock_mqtt_client,
            ),
        ):
            result = await async_setup_entry(hass, entry)

        assert result is True
        assert hass.data[DOMAIN][entry.entry_id]["coordinator"] is mock_coordinator

        for task in created_tasks:
            with contextlib.suppress(RuntimeError):
                await task

    @pytest.mark.asyncio
    async def test_async_setup_entry_push_enabled_no_hub_found_skips_mqtt(self):
        """Push enabled but no hub record available logs a warning and skips MQTT entirely."""
        hass = _make_hass()
        entry = _make_entry()
        entry.options = {CONF_PUSH_ENABLED: True}

        mock_client = MagicMock()
        mock_client.restore_tokens = MagicMock()
        mock_coordinator = MagicMock()
        mock_coordinator.async_config_entry_first_refresh = AsyncMock()
        mock_coordinator.data = {"hubs": []}
        hass.config_entries.async_forward_entry_setups = AsyncMock()
        hass.async_create_background_task = MagicMock()

        with (
            patch("custom_components.rainpoint.RainPointClient", return_value=mock_client),
            patch(
                "custom_components.rainpoint.coordinator.RainPointCoordinator",
                return_value=mock_coordinator,
            ),
        ):
            result = await async_setup_entry(hass, entry)

        assert result is True
        assert "mqtt_client" not in hass.data[DOMAIN][entry.entry_id]
        hass.async_create_background_task.assert_not_called()

    @pytest.mark.asyncio
    async def test_async_setup_entry_push_enabled_registers_relogin_listener(self):
        """When push is enabled, the client's re-login listener is wired to the mqtt client."""
        hass = _make_hass()
        entry = _make_entry()
        entry.options = {CONF_PUSH_ENABLED: True}

        mock_client = MagicMock()
        mock_client.restore_tokens = MagicMock()
        mock_coordinator = MagicMock()
        mock_coordinator.async_config_entry_first_refresh = AsyncMock()
        mock_coordinator.data = {"hubs": [{"deviceName": "hub-dev", "productKey": "hub-pk"}]}
        hass.config_entries.async_forward_entry_setups = AsyncMock()
        hass.async_create_background_task = MagicMock()

        mock_mqtt_client = MagicMock()
        mock_mqtt_client.async_start = AsyncMock()
        mock_mqtt_client.async_disconnect = AsyncMock()

        with (
            patch("custom_components.rainpoint.RainPointClient", return_value=mock_client),
            patch(
                "custom_components.rainpoint.coordinator.RainPointCoordinator",
                return_value=mock_coordinator,
            ),
            patch(
                "custom_components.rainpoint.RainPointMqttClient",
                return_value=mock_mqtt_client,
            ),
        ):
            result = await async_setup_entry(hass, entry)

        assert result is True
        # Two listeners are registered: token persistence (first, on client
        # creation) and the mqtt credential re-fetch (when push is enabled).
        assert mock_client.register_relogin_listener.call_count == 2
        mock_client.register_relogin_listener.assert_any_call(mock_mqtt_client.on_http_relogin)

    @pytest.mark.asyncio
    async def test_async_setup_entry_push_enabled_starts_watchdog(self):
        """When push is enabled, a watchdog is constructed, started, stored, and torn down on unload."""
        hass = _make_hass()
        entry = _make_entry()
        entry.options = {CONF_PUSH_ENABLED: True}

        mock_client = MagicMock()
        mock_client.restore_tokens = MagicMock()
        mock_coordinator = MagicMock()
        mock_coordinator.async_config_entry_first_refresh = AsyncMock()
        mock_coordinator.data = {"hubs": [{"deviceName": "hub-dev", "productKey": "hub-pk"}]}
        hass.config_entries.async_forward_entry_setups = AsyncMock()

        created_tasks = []

        def _create_background_task(coro, name=None):
            """Schedule the coroutine like the real HA helper would, so the mocked
            async_start is actually consumed rather than left un-awaited."""
            task = asyncio.ensure_future(coro)
            created_tasks.append(task)
            return task

        hass.async_create_background_task = MagicMock(side_effect=_create_background_task)

        mock_mqtt_client = MagicMock()
        mock_mqtt_client.async_start = AsyncMock()
        mock_mqtt_client.async_disconnect = AsyncMock()

        mock_watchdog = MagicMock()

        with (
            patch("custom_components.rainpoint.RainPointClient", return_value=mock_client),
            patch(
                "custom_components.rainpoint.coordinator.RainPointCoordinator",
                return_value=mock_coordinator,
            ),
            patch(
                "custom_components.rainpoint.RainPointMqttClient",
                return_value=mock_mqtt_client,
            ),
            patch(
                "custom_components.rainpoint.repairs.RainPointPushWatchdog",
                return_value=mock_watchdog,
            ),
        ):
            result = await async_setup_entry(hass, entry)

        assert result is True
        assert hass.data[DOMAIN][entry.entry_id]["watchdog"] is mock_watchdog
        mock_watchdog.start.assert_called_once_with()
        entry.async_on_unload.assert_any_call(mock_watchdog.async_stop)

    @pytest.mark.asyncio
    async def test_async_setup_entry_push_disabled_no_watchdog(self):
        """With push disabled, no watchdog is constructed or stored."""
        hass = _make_hass()
        entry = _make_entry()  # options == {} => push disabled

        mock_client = MagicMock()
        mock_client.restore_tokens = MagicMock()
        mock_coordinator = MagicMock()
        mock_coordinator.async_config_entry_first_refresh = AsyncMock()
        mock_coordinator.data = {"hubs": [{"deviceName": "hub-dev", "productKey": "hub-pk"}]}
        hass.config_entries.async_forward_entry_setups = AsyncMock()

        with (
            patch("custom_components.rainpoint.RainPointClient", return_value=mock_client),
            patch(
                "custom_components.rainpoint.coordinator.RainPointCoordinator",
                return_value=mock_coordinator,
            ),
        ):
            result = await async_setup_entry(hass, entry)

        assert result is True
        assert "watchdog" not in hass.data[DOMAIN][entry.entry_id]


class TestPushHubIdentityIssue:
    """The push-hub-identity Repairs card, driven through real async_setup_entry runs."""

    @pytest.mark.asyncio
    async def test_no_hub_record_raises_the_push_identity_issue(self):
        """No hub record at all is one of the two conditions the card covers."""
        hass = _make_hass()
        entry = _make_entry()
        entry.options = {CONF_PUSH_ENABLED: True}

        mock_client = MagicMock()
        mock_client.restore_tokens = MagicMock()
        mock_coordinator = MagicMock()
        mock_coordinator.async_config_entry_first_refresh = AsyncMock()
        mock_coordinator.data = {"hubs": []}
        hass.config_entries.async_forward_entry_setups = AsyncMock()
        hass.async_create_background_task = MagicMock()

        with (
            patch("custom_components.rainpoint.RainPointClient", return_value=mock_client),
            patch(
                "custom_components.rainpoint.coordinator.RainPointCoordinator",
                return_value=mock_coordinator,
            ),
            patch("custom_components.rainpoint.repairs.ir.async_create_issue") as create,
            patch("custom_components.rainpoint.repairs.ir.async_delete_issue") as delete,
        ):
            result = await async_setup_entry(hass, entry)

        assert result is True
        create.assert_called_once()
        assert create.call_args.args[0] is hass
        assert create.call_args.args[1] == DOMAIN
        assert create.call_args.args[2] == push_hub_identity_issue_id(entry.entry_id)
        assert create.call_args.kwargs["is_fixable"] is False
        assert create.call_args.kwargs["severity"] == ir.IssueSeverity.WARNING
        assert create.call_args.kwargs["translation_key"] == PUSH_HUB_IDENTITY_ISSUE_ID
        delete.assert_not_called()

    @pytest.mark.asyncio
    async def test_hub_missing_device_name_raises_the_push_identity_issue(self):
        """A hub record present but missing deviceName falls through the same else branch."""
        hass = _make_hass()
        entry = _make_entry()
        entry.options = {CONF_PUSH_ENABLED: True}

        mock_client = MagicMock()
        mock_client.restore_tokens = MagicMock()
        mock_coordinator = MagicMock()
        mock_coordinator.async_config_entry_first_refresh = AsyncMock()
        mock_coordinator.data = {"hubs": [{"productKey": "hub-pk"}]}
        hass.config_entries.async_forward_entry_setups = AsyncMock()
        hass.async_create_background_task = MagicMock()

        with (
            patch("custom_components.rainpoint.RainPointClient", return_value=mock_client),
            patch(
                "custom_components.rainpoint.coordinator.RainPointCoordinator",
                return_value=mock_coordinator,
            ),
            patch("custom_components.rainpoint.repairs.ir.async_create_issue") as create,
            patch("custom_components.rainpoint.repairs.ir.async_delete_issue") as delete,
        ):
            result = await async_setup_entry(hass, entry)

        assert result is True
        create.assert_called_once()
        assert create.call_args.args[2] == push_hub_identity_issue_id(entry.entry_id)
        delete.assert_not_called()
        assert "mqtt_client" not in hass.data[DOMAIN][entry.entry_id]

    @pytest.mark.asyncio
    async def test_hub_missing_product_key_raises_the_push_identity_issue(self):
        """A hub record present but missing productKey falls through the same else branch."""
        hass = _make_hass()
        entry = _make_entry()
        entry.options = {CONF_PUSH_ENABLED: True}

        mock_client = MagicMock()
        mock_client.restore_tokens = MagicMock()
        mock_coordinator = MagicMock()
        mock_coordinator.async_config_entry_first_refresh = AsyncMock()
        mock_coordinator.data = {"hubs": [{"deviceName": "hub-dev"}]}
        hass.config_entries.async_forward_entry_setups = AsyncMock()
        hass.async_create_background_task = MagicMock()

        with (
            patch("custom_components.rainpoint.RainPointClient", return_value=mock_client),
            patch(
                "custom_components.rainpoint.coordinator.RainPointCoordinator",
                return_value=mock_coordinator,
            ),
            patch("custom_components.rainpoint.repairs.ir.async_create_issue") as create,
            patch("custom_components.rainpoint.repairs.ir.async_delete_issue") as delete,
        ):
            result = await async_setup_entry(hass, entry)

        assert result is True
        create.assert_called_once()
        assert create.call_args.args[2] == push_hub_identity_issue_id(entry.entry_id)
        delete.assert_not_called()
        assert "mqtt_client" not in hass.data[DOMAIN][entry.entry_id]

    @pytest.mark.asyncio
    async def test_hub_empty_device_name_raises_the_push_identity_issue(self):
        """A present-but-falsy deviceName takes the same else branch as a missing key.

        `_resolve_hub_identity`'s guard is `if hub_device_name and hub_product_key`,
        a truthiness test rather than a None check, so an empty string must fall
        through identically to an absent key. Untested until now, a future
        refactor to `is not None` would silently break this case with nothing to
        catch it.
        """
        hass = _make_hass()
        entry = _make_entry()
        entry.options = {CONF_PUSH_ENABLED: True}

        mock_client = MagicMock()
        mock_client.restore_tokens = MagicMock()
        mock_coordinator = MagicMock()
        mock_coordinator.async_config_entry_first_refresh = AsyncMock()
        mock_coordinator.data = {"hubs": [{"deviceName": "", "productKey": "hub-pk"}]}
        hass.config_entries.async_forward_entry_setups = AsyncMock()
        hass.async_create_background_task = MagicMock()

        with (
            patch("custom_components.rainpoint.RainPointClient", return_value=mock_client),
            patch(
                "custom_components.rainpoint.coordinator.RainPointCoordinator",
                return_value=mock_coordinator,
            ),
            patch("custom_components.rainpoint.repairs.ir.async_create_issue") as create,
            patch("custom_components.rainpoint.repairs.ir.async_delete_issue") as delete,
        ):
            result = await async_setup_entry(hass, entry)

        assert result is True
        create.assert_called_once()
        assert create.call_args.args[2] == push_hub_identity_issue_id(entry.entry_id)
        delete.assert_not_called()
        assert "mqtt_client" not in hass.data[DOMAIN][entry.entry_id]

    @pytest.mark.asyncio
    async def test_hub_empty_product_key_raises_the_push_identity_issue(self):
        """A present-but-falsy productKey takes the same else branch as a missing key.

        Mirrors test_hub_empty_device_name_raises_the_push_identity_issue for
        the other half of the truthiness guard.
        """
        hass = _make_hass()
        entry = _make_entry()
        entry.options = {CONF_PUSH_ENABLED: True}

        mock_client = MagicMock()
        mock_client.restore_tokens = MagicMock()
        mock_coordinator = MagicMock()
        mock_coordinator.async_config_entry_first_refresh = AsyncMock()
        mock_coordinator.data = {"hubs": [{"deviceName": "hub-dev", "productKey": ""}]}
        hass.config_entries.async_forward_entry_setups = AsyncMock()
        hass.async_create_background_task = MagicMock()

        with (
            patch("custom_components.rainpoint.RainPointClient", return_value=mock_client),
            patch(
                "custom_components.rainpoint.coordinator.RainPointCoordinator",
                return_value=mock_coordinator,
            ),
            patch("custom_components.rainpoint.repairs.ir.async_create_issue") as create,
            patch("custom_components.rainpoint.repairs.ir.async_delete_issue") as delete,
        ):
            result = await async_setup_entry(hass, entry)

        assert result is True
        create.assert_called_once()
        assert create.call_args.args[2] == push_hub_identity_issue_id(entry.entry_id)
        delete.assert_not_called()
        assert "mqtt_client" not in hass.data[DOMAIN][entry.entry_id]

    @pytest.mark.asyncio
    async def test_resolved_hub_identity_clears_the_push_identity_issue(self):
        """A resolving setup pass explicitly clears any card a prior pass may have raised."""
        hass = _make_hass()
        entry = _make_entry()
        entry.options = {CONF_PUSH_ENABLED: True}

        mock_client = MagicMock()
        mock_client.restore_tokens = MagicMock()
        mock_coordinator = MagicMock()
        mock_coordinator.async_config_entry_first_refresh = AsyncMock()
        mock_coordinator.data = {"hubs": [{"deviceName": "hub-dev", "productKey": "hub-pk"}]}
        hass.config_entries.async_forward_entry_setups = AsyncMock()

        mock_mqtt_client = MagicMock()
        mock_mqtt_client.async_start = AsyncMock()
        mock_mqtt_client.async_disconnect = AsyncMock()
        hass.async_create_background_task = MagicMock()

        with (
            patch("custom_components.rainpoint.RainPointClient", return_value=mock_client),
            patch(
                "custom_components.rainpoint.coordinator.RainPointCoordinator",
                return_value=mock_coordinator,
            ),
            patch(
                "custom_components.rainpoint.RainPointMqttClient",
                return_value=mock_mqtt_client,
            ),
            patch("custom_components.rainpoint.repairs.RainPointPushWatchdog"),
            patch("custom_components.rainpoint.repairs.ir.async_create_issue") as create,
            patch("custom_components.rainpoint.repairs.ir.async_delete_issue") as delete,
        ):
            result = await async_setup_entry(hass, entry)

        assert result is True
        delete.assert_called_once()
        assert delete.call_args.args == (hass, DOMAIN, push_hub_identity_issue_id(entry.entry_id))
        create.assert_not_called()

    @pytest.mark.asyncio
    async def test_push_disabled_clears_the_push_identity_issue(self):
        """Turning push off must clear a card raised while push was on.

        Without this, a card raised while push was on becomes unclearable the
        moment the user turns push off -- and turning push off is one of the
        two remedies the card itself offers.
        """
        hass = _make_hass()
        entry = _make_entry()
        # entry.options == {} => push_enabled defaults to False

        mock_client = MagicMock()
        mock_client.restore_tokens = MagicMock()
        mock_coordinator = MagicMock()
        mock_coordinator.async_config_entry_first_refresh = AsyncMock()
        mock_coordinator.data = {"hubs": [{"deviceName": "hub-dev", "productKey": "hub-pk"}]}
        hass.config_entries.async_forward_entry_setups = AsyncMock()
        hass.async_create_background_task = MagicMock()

        with (
            patch("custom_components.rainpoint.RainPointClient", return_value=mock_client),
            patch(
                "custom_components.rainpoint.coordinator.RainPointCoordinator",
                return_value=mock_coordinator,
            ),
            patch("custom_components.rainpoint.repairs.ir.async_create_issue") as create,
            patch("custom_components.rainpoint.repairs.ir.async_delete_issue") as delete,
        ):
            result = await async_setup_entry(hass, entry)

        assert result is True
        delete.assert_called_once()
        assert delete.call_args.args == (hass, DOMAIN, push_hub_identity_issue_id(entry.entry_id))
        create.assert_not_called()

    @pytest.mark.asyncio
    async def test_entry_unload_withdraws_this_entrys_push_identity_card(self):
        """Unloading the entry clears this entry's card, entry-scoped.

        The issue registry is not per config entry, so a card raised before
        removal would otherwise survive it with nothing left to clear it. The
        clear is registered via entry.async_on_unload, the same mechanism
        every other teardown in this function uses, and it targets this
        entry's own scoped id so a second RainPoint entry's card is
        untouched.
        """
        hass = _make_hass()
        entry = _make_entry()
        entry.options = {CONF_PUSH_ENABLED: True}

        mock_client = MagicMock()
        mock_client.restore_tokens = MagicMock()
        mock_coordinator = MagicMock()
        mock_coordinator.async_config_entry_first_refresh = AsyncMock()
        mock_coordinator.data = {"hubs": []}
        hass.config_entries.async_forward_entry_setups = AsyncMock()
        hass.async_create_background_task = MagicMock()

        with (
            patch("custom_components.rainpoint.RainPointClient", return_value=mock_client),
            patch(
                "custom_components.rainpoint.coordinator.RainPointCoordinator",
                return_value=mock_coordinator,
            ),
            patch("custom_components.rainpoint.repairs.ir.async_create_issue"),
            patch("custom_components.rainpoint.repairs.ir.async_delete_issue") as delete,
        ):
            await async_setup_entry(hass, entry)

            # The unload-clear is the last entry.async_on_unload registration
            # in async_setup_entry, so this is it, not a coincidental last
            # call. Invoked inside the same patch scope so the callback's own
            # ir.async_delete_issue call still lands on this mock.
            unload_callback = entry.async_on_unload.call_args.args[0]
            delete.reset_mock()

            unload_callback()

            delete.assert_called_once_with(hass, DOMAIN, push_hub_identity_issue_id(entry.entry_id))

    @pytest.mark.asyncio
    async def test_the_existing_warning_line_is_still_emitted_alongside_the_card(self, caplog):
        """The card is additive to the log line, not a replacement (D-03)."""
        hass = _make_hass()
        entry = _make_entry()
        entry.options = {CONF_PUSH_ENABLED: True}

        mock_client = MagicMock()
        mock_client.restore_tokens = MagicMock()
        mock_coordinator = MagicMock()
        mock_coordinator.async_config_entry_first_refresh = AsyncMock()
        mock_coordinator.data = {"hubs": []}
        hass.config_entries.async_forward_entry_setups = AsyncMock()
        hass.async_create_background_task = MagicMock()

        with (
            caplog.at_level("WARNING"),
            patch("custom_components.rainpoint.RainPointClient", return_value=mock_client),
            patch(
                "custom_components.rainpoint.coordinator.RainPointCoordinator",
                return_value=mock_coordinator,
            ),
            patch("custom_components.rainpoint.repairs.ir.async_create_issue") as create,
            patch("custom_components.rainpoint.repairs.ir.async_delete_issue"),
        ):
            result = await async_setup_entry(hass, entry)

        assert result is True
        create.assert_called_once()
        assert "Push enabled but no hub was found; skipping MQTT connect" in caplog.text
        assert any(
            record.levelname == "WARNING" and "Push enabled but no hub was found" in record.message for record in caplog.records
        )

    @pytest.mark.asyncio
    async def test_an_unreadable_issue_registry_never_fails_setup(self):
        """A registry write failure must never fail config entry setup."""
        hass = _make_hass()
        entry = _make_entry()
        entry.options = {CONF_PUSH_ENABLED: True}

        mock_client = MagicMock()
        mock_client.restore_tokens = MagicMock()
        mock_coordinator = MagicMock()
        mock_coordinator.async_config_entry_first_refresh = AsyncMock()
        mock_coordinator.data = {"hubs": []}
        hass.config_entries.async_forward_entry_setups = AsyncMock()
        hass.async_create_background_task = MagicMock()

        with (
            patch("custom_components.rainpoint.RainPointClient", return_value=mock_client),
            patch(
                "custom_components.rainpoint.coordinator.RainPointCoordinator",
                return_value=mock_coordinator,
            ),
            patch(
                "custom_components.rainpoint.repairs.ir.async_create_issue",
                side_effect=RuntimeError("registry unavailable"),
            ),
        ):
            result = await async_setup_entry(hass, entry)

        assert result is True
        assert hass.data[DOMAIN][entry.entry_id]["coordinator"] is mock_coordinator


class TestAsyncUnloadEntry:
    """Tests for AsyncUnloadEntry."""

    @pytest.mark.asyncio
    async def test_async_unload_entry_success(self):
        """Async unload entry success."""
        entry = _make_entry()
        hass = _make_hass()
        hass.data[DOMAIN] = {entry.entry_id: {"client": MagicMock(), "coordinator": MagicMock()}}
        hass.config_entries.async_unload_platforms = AsyncMock(return_value=True)

        result = await async_unload_entry(hass, entry)

        assert result is True
        assert entry.entry_id not in hass.data[DOMAIN]

    @pytest.mark.asyncio
    async def test_async_unload_entry_failure(self):
        """Async unload entry failure."""
        entry = _make_entry()
        hass = _make_hass()
        hass.data[DOMAIN] = {entry.entry_id: {"client": MagicMock(), "coordinator": MagicMock()}}
        hass.config_entries.async_unload_platforms = AsyncMock(return_value=False)

        result = await async_unload_entry(hass, entry)

        assert result is False
        assert entry.entry_id in hass.data[DOMAIN]


class TestAsyncReloadIntegration:
    """Tests for AsyncReloadIntegration."""

    @pytest.mark.asyncio
    async def test_async_reload_integration_success(self):
        """Async reload integration success."""
        hass = _make_hass()
        mock_entry = MagicMock()
        mock_entry.domain = DOMAIN
        hass.config_entries.async_get_entry = MagicMock(return_value=mock_entry)
        hass.config_entries.async_reload = AsyncMock()

        result = await async_reload_integration(hass, "test_id")

        assert result is True
        hass.config_entries.async_reload.assert_awaited_once_with("test_id")

    @pytest.mark.asyncio
    async def test_async_reload_integration_invalid_entry_none(self):
        """Async reload integration invalid entry none."""
        hass = _make_hass()
        hass.config_entries.async_get_entry = MagicMock(return_value=None)

        result = await async_reload_integration(hass, "bad_id")

        assert result is False

    @pytest.mark.asyncio
    async def test_async_reload_integration_wrong_domain(self):
        """Async reload integration wrong domain."""
        hass = _make_hass()
        mock_entry = MagicMock()
        mock_entry.domain = "other_domain"
        hass.config_entries.async_get_entry = MagicMock(return_value=mock_entry)

        result = await async_reload_integration(hass, "some_id")

        assert result is False

    @pytest.mark.asyncio
    async def test_async_reload_integration_exception_returns_false(self):
        """Async reload integration exception returns false."""
        hass = _make_hass()
        mock_entry = MagicMock()
        mock_entry.domain = DOMAIN
        hass.config_entries.async_get_entry = MagicMock(return_value=mock_entry)
        hass.config_entries.async_reload = AsyncMock(side_effect=RuntimeError("boom"))

        result = await async_reload_integration(hass, "test_id")

        assert result is False


class TestAsyncReloadEntry:
    """Cover async_reload_entry helper (lines 67-68)."""

    @pytest.mark.asyncio
    async def test_async_reload_entry_goes_through_config_entries(self):
        """The reload runs through async_reload, not a hand-rolled unload/setup pair.

        Calling async_unload_entry/async_setup_entry directly bypasses Home
        Assistant's entry state machine: the entry never reaches LOADED, so
        every platform forwarded afterwards raises "Config entry was never
        loaded!" on the next unload.
        """
        from custom_components.rainpoint import async_reload_entry

        hass = _make_hass()
        hass.config_entries.async_reload = AsyncMock()
        entry = _make_entry()

        with (
            patch("custom_components.rainpoint.async_unload_entry", new=AsyncMock(return_value=True)) as mu,
            patch("custom_components.rainpoint.async_setup_entry", new=AsyncMock(return_value=True)) as ms,
        ):
            await async_reload_entry(hass, entry)

        hass.config_entries.async_reload.assert_awaited_once_with(entry.entry_id)
        mu.assert_not_awaited()
        ms.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_async_reload_entry_skips_reload_on_data_only_change(self):
        """A data-only update (e.g. token persistence) must not reload the entry."""
        from custom_components.rainpoint import async_reload_entry

        hass = _make_hass()
        hass.config_entries.async_reload = AsyncMock()
        entry = _make_entry()
        entry.options = {CONF_PUSH_ENABLED: True}
        hass.data = {DOMAIN: {entry.entry_id: {"options_snapshot": {CONF_PUSH_ENABLED: True}}}}

        await async_reload_entry(hass, entry)

        hass.config_entries.async_reload.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_async_reload_entry_reloads_on_options_change(self):
        """An options change reloads the entry."""
        from custom_components.rainpoint import async_reload_entry

        hass = _make_hass()
        hass.config_entries.async_reload = AsyncMock()
        entry = _make_entry()
        entry.options = {CONF_PUSH_ENABLED: True}
        hass.data = {DOMAIN: {entry.entry_id: {"options_snapshot": {CONF_PUSH_ENABLED: False}}}}

        await async_reload_entry(hass, entry)

        hass.config_entries.async_reload.assert_awaited_once_with(entry.entry_id)


class TestPersistTokens:
    """Cover _persist_tokens: write-on-change, and the two early returns."""

    def test_persist_tokens_writes_changed_token(self):
        """A rotated token is written back to the config entry data."""
        from custom_components.rainpoint import _persist_tokens

        hass = _make_hass()
        entry = _make_entry()
        client = MagicMock()
        client.export_tokens.return_value = {
            "token": "NEW",
            "refresh_token": "ref2",
            "token_expires_at": 123,
        }

        _persist_tokens(hass, entry, client)

        hass.config_entries.async_update_entry.assert_called_once()
        _, kwargs = hass.config_entries.async_update_entry.call_args
        assert kwargs["data"]["token"] == "NEW"
        # Existing (non-token) data is preserved.
        assert kwargs["data"]["email"] == "test@example.com"

    def test_persist_tokens_noop_without_token(self):
        """No token to persist -> no write."""
        from custom_components.rainpoint import _persist_tokens

        hass = _make_hass()
        entry = _make_entry()
        client = MagicMock()
        client.export_tokens.return_value = {"token": None}

        _persist_tokens(hass, entry, client)

        hass.config_entries.async_update_entry.assert_not_called()

    def test_persist_tokens_noop_when_unchanged(self):
        """An unchanged token is not rewritten (avoids a needless entry update)."""
        from custom_components.rainpoint import _persist_tokens

        hass = _make_hass()
        entry = _make_entry()
        client = MagicMock()
        client.export_tokens.return_value = {
            "token": "tok",
            "refresh_token": "ref",
            "token_expires_at": 9999999999,
        }

        _persist_tokens(hass, entry, client)

        hass.config_entries.async_update_entry.assert_not_called()


class TestAsyncSupportsReconfigure:
    """Cover async_supports_reconfigure (line 73)."""

    @pytest.mark.asyncio
    async def test_supports_reconfigure_returns_true(self):
        """async_supports_reconfigure always returns True for this integration."""
        from custom_components.rainpoint import async_supports_reconfigure

        hass = _make_hass()
        entry = _make_entry()

        result = await async_supports_reconfigure(hass, entry)

        assert result is True


class TestAsyncGetDiagnosticInfo:
    """Cover async_get_diagnostic_info (line 158)."""

    @pytest.mark.asyncio
    async def test_diagnostic_info_payload(self):
        """Diagnostic info includes entry_id, title, domain, supports_reload."""
        from custom_components.rainpoint import async_get_diagnostic_info

        hass = _make_hass()
        entry = MagicMock()
        entry.entry_id = "e42"
        entry.title = "RainPoint (test)"

        info = await async_get_diagnostic_info(hass, entry)

        assert info == {
            "entry_id": "e42",
            "title": "RainPoint (test)",
            "domain": DOMAIN,
            "supports_reload": True,
        }


class TestReloadService:
    """Cover async_setup_services + the nested reload_service closure (lines 79-153)."""

    @pytest.mark.asyncio
    async def test_setup_services_registers_reload(self):
        """async_setup_services registers the 'reload' service."""
        from custom_components.rainpoint import async_setup_services

        hass = _make_hass()
        hass.services.async_register = MagicMock()

        await async_setup_services(hass)

        # Service registration is the sole side effect; verify it was called
        # with domain + "reload".
        assert hass.services.async_register.called
        args, _kwargs = hass.services.async_register.call_args
        assert args[0] == DOMAIN
        assert args[1] == "reload"

    @pytest.mark.asyncio
    async def test_reload_service_no_entry_id_no_entries_errors(self):
        """Reload called without entry_id and no registered entries emits error."""
        from custom_components.rainpoint import async_setup_services

        hass = _make_hass()
        captured = {}

        def _register(domain, name, handler, **kw):
            captured["handler"] = handler

        hass.services.async_register = MagicMock(side_effect=_register)
        hass.config_entries.async_entries = MagicMock(return_value=[])

        await async_setup_services(hass)

        call = MagicMock()
        call.data = {}  # no entry_id

        with patch("homeassistant.components.persistent_notification.async_create") as pn:
            result = await captured["handler"](call)

        assert result == {"success": False, "message": "No RainPoint integrations found to reload"}
        pn.assert_called_once()

    @pytest.mark.asyncio
    async def test_reload_service_no_entry_id_all_succeed(self):
        """Reload with no entry_id + all entries succeed emits success notification."""
        from custom_components.rainpoint import async_setup_services

        hass = _make_hass()
        captured = {}
        hass.services.async_register = MagicMock(side_effect=lambda d, n, h, **kw: captured.update(handler=h))

        e1 = MagicMock()
        e1.entry_id = "a"
        e1.title = "Home"
        e2 = MagicMock()
        e2.entry_id = "b"
        e2.title = "Cabin"
        hass.config_entries.async_entries = MagicMock(return_value=[e1, e2])

        await async_setup_services(hass)

        call = MagicMock()
        call.data = {}

        with (
            patch(
                "custom_components.rainpoint.async_reload_integration",
                new=AsyncMock(return_value=True),
            ),
            patch("homeassistant.components.persistent_notification.async_create"),
        ):
            result = await captured["handler"](call)

        assert result["success"] is True
        assert "Successfully reloaded 2" in result["message"]

    @pytest.mark.asyncio
    async def test_reload_service_no_entry_id_partial_success(self):
        """Reload with no entry_id where only some entries reload emits partial notification."""
        from custom_components.rainpoint import async_setup_services

        hass = _make_hass()
        captured = {}
        hass.services.async_register = MagicMock(side_effect=lambda d, n, h, **kw: captured.update(handler=h))

        e1 = MagicMock()
        e1.entry_id = "a"
        e1.title = "Home"
        e2 = MagicMock()
        e2.entry_id = "b"
        e2.title = "Cabin"
        hass.config_entries.async_entries = MagicMock(return_value=[e1, e2])

        await async_setup_services(hass)

        call = MagicMock()
        call.data = {}

        # First reload succeeds, second fails
        async def mixed_reload(hass_, entry_id):
            return entry_id == "a"

        with (
            patch(
                "custom_components.rainpoint.async_reload_integration",
                new=AsyncMock(side_effect=mixed_reload),
            ),
            patch("homeassistant.components.persistent_notification.async_create"),
        ):
            result = await captured["handler"](call)

        assert result["success"] is False
        assert "1 of 2" in result["message"]

    @pytest.mark.asyncio
    async def test_reload_service_no_entry_id_all_fail(self):
        """Reload with no entry_id where every entry fails emits the failed notification."""
        from custom_components.rainpoint import async_setup_services

        hass = _make_hass()
        captured = {}
        hass.services.async_register = MagicMock(side_effect=lambda d, n, h, **kw: captured.update(handler=h))

        e1 = MagicMock()
        e1.entry_id = "a"
        e1.title = "Home"
        e2 = MagicMock()
        e2.entry_id = "b"
        e2.title = "Cabin"
        hass.config_entries.async_entries = MagicMock(return_value=[e1, e2])

        await async_setup_services(hass)

        call = MagicMock()
        call.data = {}

        with (
            patch(
                "custom_components.rainpoint.async_reload_integration",
                new=AsyncMock(return_value=False),
            ),
            patch("homeassistant.components.persistent_notification.async_create") as pn,
        ):
            result = await captured["handler"](call)

        assert result["success"] is False
        assert "Failed to reload all 2" in result["message"]
        # Total failure should surface as the "Failed" notification, not "Partial".
        assert pn.call_args.kwargs["notification_id"] == "rainpoint_reload_error"

    @pytest.mark.asyncio
    async def test_reload_service_specific_entry_success(self):
        """Reload with an explicit entry_id that reloads OK emits the success message."""
        from custom_components.rainpoint import async_setup_services

        hass = _make_hass()
        captured = {}
        hass.services.async_register = MagicMock(side_effect=lambda d, n, h, **kw: captured.update(handler=h))

        await async_setup_services(hass)

        call = MagicMock()
        call.data = {"entry_id": "X"}

        with (
            patch(
                "custom_components.rainpoint.async_reload_integration",
                new=AsyncMock(return_value=True),
            ),
            patch("homeassistant.components.persistent_notification.async_create"),
        ):
            result = await captured["handler"](call)

        assert result == {"success": True, "message": "RainPoint integration reloaded successfully"}

    @pytest.mark.asyncio
    async def test_reload_service_specific_entry_failure(self):
        """Reload with an explicit entry_id that fails emits the failure notification."""
        from custom_components.rainpoint import async_setup_services

        hass = _make_hass()
        captured = {}
        hass.services.async_register = MagicMock(side_effect=lambda d, n, h, **kw: captured.update(handler=h))

        await async_setup_services(hass)

        call = MagicMock()
        call.data = {"entry_id": "X"}

        with (
            patch(
                "custom_components.rainpoint.async_reload_integration",
                new=AsyncMock(return_value=False),
            ),
            patch("homeassistant.components.persistent_notification.async_create"),
        ):
            result = await captured["handler"](call)

        assert result["success"] is False
        assert "Failed" in result["message"]

    @pytest.mark.asyncio
    async def test_reload_service_schema_rejects_empty_entry_id(self):
        """Schema rejects empty entry_id so a blank input cannot silently reload all entries."""
        import voluptuous as vol

        from custom_components.rainpoint import async_setup_services

        hass = _make_hass()
        captured = {}
        hass.services.async_register = MagicMock(side_effect=lambda d, n, h, **kw: captured.update(schema=kw.get("schema")))

        await async_setup_services(hass)

        schema = captured["schema"]
        assert schema({}) == {}
        assert schema({"entry_id": "abc"}) == {"entry_id": "abc"}
        with pytest.raises(vol.Invalid):
            schema({"entry_id": ""})


class _GenericSweepFixtures:
    """Seeded fake-registry fixtures shared by the sweep test classes.

    Held in a plain mixin rather than a base test class so a second class can
    reuse the fixtures without also inheriting and re-running every case.
    """

    ENTRY_ID = "this_entry"
    OTHER_ENTRY_ID = "other_entry"

    # Realistic {hid}_{mid}_{addr} base slugs, matching the coordinator's own
    # sensor-record keying.
    SLUG_A = "42_100_1"  # never gains a hand-written decoder in these tests
    SLUG_B = "42_100_2"  # the graduation case
    SLUG_UNRESOLVED = "42_100_9"  # never present in any seeded sensors mapping

    GENERIC_A = SimpleNamespace(
        entity_id="sensor.rainpoint_42_100_1_generic_sta_rh_p0",
        unique_id="rainpoint_42_100_1_generic_sta_rh_p0",
        config_entry_id=ENTRY_ID,
    )
    GENERIC_B = SimpleNamespace(
        entity_id="sensor.rainpoint_42_100_2_generic_sta_tem_p0",
        unique_id="rainpoint_42_100_2_generic_sta_tem_p0",
        config_entry_id=ENTRY_ID,
    )
    # Control-namespace rows: the control marker nests inside the sensor
    # marker, so these unique_ids also contain GENERIC_UNIQUE_ID_MARKER
    # -- exactly the ambiguity the dispatch-order guard in
    # _remove_stale_generic_entities exists to resolve correctly.
    CONTROL_A = SimpleNamespace(
        entity_id="valve.rainpoint_42_100_1_generic_ctl_ctl_water_p1",
        unique_id="rainpoint_42_100_1_generic_ctl_ctl_water_p1",
        config_entry_id=ENTRY_ID,
    )
    DURATION_A = SimpleNamespace(
        entity_id="number.rainpoint_42_100_1_generic_ctl_ctl_water_p1_duration",
        unique_id="rainpoint_42_100_1_generic_ctl_ctl_water_p1_duration",
        config_entry_id=ENTRY_ID,
    )
    CONTROL_B = SimpleNamespace(
        entity_id="valve.rainpoint_42_100_2_generic_ctl_ctl_water_p1",
        unique_id="rainpoint_42_100_2_generic_ctl_ctl_water_p1",
        config_entry_id=ENTRY_ID,
    )
    DURATION_B = SimpleNamespace(
        entity_id="number.rainpoint_42_100_2_generic_ctl_ctl_water_p1_duration",
        unique_id="rainpoint_42_100_2_generic_ctl_ctl_water_p1_duration",
        config_entry_id=ENTRY_ID,
    )
    UNRESOLVABLE_CONTROL = SimpleNamespace(
        entity_id="valve.rainpoint_42_100_9_generic_ctl_ctl_water_p1",
        unique_id="rainpoint_42_100_9_generic_ctl_ctl_water_p1",
        config_entry_id=ENTRY_ID,
    )
    TRUSTED_UNKNOWN = SimpleNamespace(
        entity_id="sensor.rainpoint_42_100_1_unknown",
        unique_id="rainpoint_42_100_1_unknown_HCS777ARF",
        config_entry_id=ENTRY_ID,
    )
    RAW_PAYLOAD = SimpleNamespace(
        entity_id="sensor.rainpoint_42_100_1_raw_payload",
        unique_id="rainpoint_42_100_1_raw_payload",
        config_entry_id=ENTRY_ID,
    )
    FOREIGN_INTEGRATION = SimpleNamespace(
        entity_id="sensor.other_integration_generic",
        unique_id="other_integration_42_100_1_generic_sta_rh_p0",
        config_entry_id=ENTRY_ID,
    )
    FOREIGN_ENTRY_GENERIC = SimpleNamespace(
        entity_id="sensor.rainpoint_99_1_1_generic_sta_rh_p0",
        unique_id="rainpoint_99_1_1_generic_sta_rh_p0",
        config_entry_id=OTHER_ENTRY_ID,
    )
    FOREIGN_ENTRY_CONTROL = SimpleNamespace(
        entity_id="valve.rainpoint_99_1_1_generic_ctl_ctl_water_p1",
        unique_id="rainpoint_99_1_1_generic_ctl_ctl_water_p1",
        config_entry_id=OTHER_ENTRY_ID,
    )
    MALFORMED_UNIQUE_ID = SimpleNamespace(
        entity_id="sensor.malformed",
        unique_id=None,
        config_entry_id=ENTRY_ID,
    )

    def _all_rows(self):
        """Return every seeded row across both config entries."""
        return [
            self.GENERIC_A,
            self.GENERIC_B,
            self.CONTROL_A,
            self.DURATION_A,
            self.CONTROL_B,
            self.DURATION_B,
            self.UNRESOLVABLE_CONTROL,
            self.TRUSTED_UNKNOWN,
            self.RAW_PAYLOAD,
            self.FOREIGN_INTEGRATION,
            self.FOREIGN_ENTRY_GENERIC,
            self.FOREIGN_ENTRY_CONTROL,
            self.MALFORMED_UNIQUE_ID,
        ]

    def _all_control_namespace_entity_ids(self) -> set[str]:
        """Every seeded control-namespace (control + companion duration) row for this entry."""
        return {
            self.CONTROL_A.entity_id,
            self.DURATION_A.entity_id,
            self.CONTROL_B.entity_id,
            self.DURATION_B.entity_id,
            self.UNRESOLVABLE_CONTROL.entity_id,
        }

    def _make_fake_registry(self, raise_on_lookup=False, raise_once_for=None):
        """Build patchable stand-ins for er.async_get / er.async_entries_for_config_entry.

        `async_entries_for_config_entry` re-filters out already-removed rows
        on every call, mirroring how a real registry lookup would no longer
        return a row this sweep already removed - this is what makes the
        idempotency case (two consecutive sweeps) meaningful.
        """
        removed: list[str] = []
        raise_once_for = set(raise_once_for or ())

        class _FakeRegistry:
            def async_remove(self, entity_id):
                if entity_id in raise_once_for:
                    raise_once_for.discard(entity_id)
                    raise RuntimeError(f"boom removing {entity_id}")
                removed.append(entity_id)

        fake_registry = _FakeRegistry()

        def _async_get(hass):
            if raise_on_lookup:
                raise RuntimeError("registry unavailable")
            return fake_registry

        def _async_entries_for_config_entry(registry, entry_id):
            return [row for row in self._all_rows() if row.config_entry_id == entry_id and row.entity_id not in removed]

        return removed, _async_get, _async_entries_for_config_entry

    def _make_coordinator_with_raising_data(self):
        """Build a coordinator whose .data property raises when read."""

        class _RaisingCoordinator:
            @property
            def data(self):
                raise RuntimeError("coordinator data unavailable")

        return _RaisingCoordinator()

    def _sensors(
        self,
        slug_a_model="HCS777ARF",
        slug_b_model="HCS777ARF",
        include_slug_a=True,
        slug_a_model_code=None,
        slug_b_model_code=None,
    ):
        """Build a coordinator.data['sensors'] mapping for the two seeded sub-devices."""
        sensors = {self.SLUG_B: {"model": slug_b_model, "model_code": slug_b_model_code}}
        if include_slug_a:
            sensors[self.SLUG_A] = {"model": slug_a_model, "model_code": slug_a_model_code}
        return sensors

    def _make_entry_and_coordinator(self, options, sensors):
        entry = MagicMock()
        entry.entry_id = self.ENTRY_ID
        entry.options = options
        coordinator = MagicMock()
        coordinator.data = {"sensors": sensors}
        return entry, coordinator


class TestRemoveStaleGenericEntities(_GenericSweepFixtures):
    """Cover _remove_stale_generic_entities against a seeded fake registry.

    A mocked registry is used rather than a live Home Assistant one: the
    repository conftest replaces the whole homeassistant package tree with
    stubs before any test module is imported, so the installed live-registry
    fixtures cannot be mixed in without unpicking that, and a seeded fake
    keeps every assertion explicit about exactly which entity ids were
    removed.
    """

    def test_toggle_absent_removes_every_generic_row_for_this_entry(self):
        """No CONF_GENERIC_ENTITIES_ENABLED key at all behaves like toggle-off.

        The control option is explicitly enabled here so this test's scope
        stays on the sensor toggle alone; it also doubles as one direction of
        the namespace independence guarantee -- the sensor option, off, removes only
        sensor-namespace rows and leaves every control-namespace row
        (including the always-unresolvable one) untouched.
        """
        removed, async_get, async_entries = self._make_fake_registry()
        entry, coordinator = self._make_entry_and_coordinator({CONF_GENERIC_CONTROL_ENABLED: True}, self._sensors())

        with (
            patch("custom_components.rainpoint.er.async_get", side_effect=async_get),
            patch("custom_components.rainpoint.er.async_entries_for_config_entry", side_effect=async_entries),
        ):
            _remove_stale_generic_entities(MagicMock(), entry, coordinator)

        assert set(removed) == {self.GENERIC_A.entity_id, self.GENERIC_B.entity_id}

    def test_toggle_explicitly_false_removes_every_generic_row_for_this_entry(self):
        """An explicit False behaves identically to the key being absent."""
        removed, async_get, async_entries = self._make_fake_registry()
        entry, coordinator = self._make_entry_and_coordinator(
            {CONF_GENERIC_ENTITIES_ENABLED: False, CONF_GENERIC_CONTROL_ENABLED: True}, self._sensors()
        )

        with (
            patch("custom_components.rainpoint.er.async_get", side_effect=async_get),
            patch("custom_components.rainpoint.er.async_entries_for_config_entry", side_effect=async_entries),
        ):
            _remove_stale_generic_entities(MagicMock(), entry, coordinator)

        assert set(removed) == {self.GENERIC_A.entity_id, self.GENERIC_B.entity_id}

    def test_toggle_true_no_graduated_model_removes_nothing(self):
        """Toggle on, and no model has gained a hand-written decoder: nothing is removed."""
        removed, async_get, async_entries = self._make_fake_registry()
        entry, coordinator = self._make_entry_and_coordinator(
            {CONF_GENERIC_ENTITIES_ENABLED: True, CONF_GENERIC_CONTROL_ENABLED: True},
            self._sensors(slug_b_model="HCS888ARF-V2"),
        )

        with (
            patch("custom_components.rainpoint.er.async_get", side_effect=async_get),
            patch("custom_components.rainpoint.er.async_entries_for_config_entry", side_effect=async_entries),
        ):
            _remove_stale_generic_entities(MagicMock(), entry, coordinator)

        assert removed == []

    def test_toggle_true_graduated_model_removes_only_that_models_rows(self):
        """Toggle on, one model now hand-written: only its rows are removed.

        Both options are enabled here, so the graduated model's rows in
        *both* namespaces (the sensor row and its control + companion
        duration rows) are removed together -- graduation is a model
        property, not a namespace-specific one.
        """
        removed, async_get, async_entries = self._make_fake_registry()
        entry, coordinator = self._make_entry_and_coordinator(
            {CONF_GENERIC_ENTITIES_ENABLED: True, CONF_GENERIC_CONTROL_ENABLED: True},
            self._sensors(slug_a_model="HCS777ARF", slug_b_model=MODEL_HCS026FRF),
        )

        with (
            patch("custom_components.rainpoint.er.async_get", side_effect=async_get),
            patch("custom_components.rainpoint.er.async_entries_for_config_entry", side_effect=async_entries),
        ):
            _remove_stale_generic_entities(MagicMock(), entry, coordinator)

        assert set(removed) == {self.GENERIC_B.entity_id, self.CONTROL_B.entity_id, self.DURATION_B.entity_id}

    def test_toggle_true_unresolvable_base_slug_survives(self):
        """A row whose base slug is absent from the sensor records is left alone, in both namespaces."""
        removed, async_get, async_entries = self._make_fake_registry()
        entry, coordinator = self._make_entry_and_coordinator(
            {CONF_GENERIC_ENTITIES_ENABLED: True, CONF_GENERIC_CONTROL_ENABLED: True},
            self._sensors(slug_b_model=MODEL_HCS026FRF, include_slug_a=False),
        )

        with (
            patch("custom_components.rainpoint.er.async_get", side_effect=async_get),
            patch("custom_components.rainpoint.er.async_entries_for_config_entry", side_effect=async_entries),
        ):
            _remove_stale_generic_entities(MagicMock(), entry, coordinator)

        # Slug B still graduates in both namespaces; slug A's rows (and the
        # always-unresolvable control row) survive because they resolve to
        # no model at all, which is not evidence of graduation.
        assert set(removed) == {self.GENERIC_B.entity_id, self.CONTROL_B.entity_id, self.DURATION_B.entity_id}

    def test_second_consecutive_sweep_removes_nothing_more(self):
        """Idempotency: a repeat run over the post-removal registry state is a no-op."""
        removed, async_get, async_entries = self._make_fake_registry()
        entry, coordinator = self._make_entry_and_coordinator({}, self._sensors())

        with (
            patch("custom_components.rainpoint.er.async_get", side_effect=async_get),
            patch("custom_components.rainpoint.er.async_entries_for_config_entry", side_effect=async_entries),
        ):
            _remove_stale_generic_entities(MagicMock(), entry, coordinator)
            first_run_removed = list(removed)
            _remove_stale_generic_entities(MagicMock(), entry, coordinator)

        assert (
            set(first_run_removed)
            == {
                self.GENERIC_A.entity_id,
                self.GENERIC_B.entity_id,
            }
            | self._all_control_namespace_entity_ids()
        )
        assert removed == first_run_removed

    def test_foreign_integration_row_is_never_removed(self):
        """A row belonging to another integration is returned by the lookup but skipped."""
        removed, async_get, async_entries = self._make_fake_registry()
        entry, coordinator = self._make_entry_and_coordinator({}, self._sensors())

        with (
            patch("custom_components.rainpoint.er.async_get", side_effect=async_get),
            patch("custom_components.rainpoint.er.async_entries_for_config_entry", side_effect=async_entries),
        ):
            _remove_stale_generic_entities(MagicMock(), entry, coordinator)

        assert self.FOREIGN_INTEGRATION.entity_id not in removed

    def test_foreign_config_entry_row_is_never_touched(self):
        """A row belonging to another config entry of this integration is never returned or removed."""
        removed, async_get, async_entries = self._make_fake_registry()
        entry, coordinator = self._make_entry_and_coordinator({}, self._sensors())

        with (
            patch("custom_components.rainpoint.er.async_get", side_effect=async_get),
            patch("custom_components.rainpoint.er.async_entries_for_config_entry", side_effect=async_entries),
        ):
            _remove_stale_generic_entities(MagicMock(), entry, coordinator)

        assert self.FOREIGN_ENTRY_GENERIC.entity_id not in removed
        assert self.FOREIGN_ENTRY_CONTROL.entity_id not in removed

    def test_trusted_and_raw_payload_rows_are_never_removed(self):
        """A trusted unsupported-diagnostic row and a raw-payload row never match the marker guard."""
        removed, async_get, async_entries = self._make_fake_registry()
        entry, coordinator = self._make_entry_and_coordinator({}, self._sensors())

        with (
            patch("custom_components.rainpoint.er.async_get", side_effect=async_get),
            patch("custom_components.rainpoint.er.async_entries_for_config_entry", side_effect=async_entries),
        ):
            _remove_stale_generic_entities(MagicMock(), entry, coordinator)

        assert self.TRUSTED_UNKNOWN.entity_id not in removed
        assert self.RAW_PAYLOAD.entity_id not in removed

    def test_row_with_non_string_unique_id_is_skipped_defensively(self):
        """A row whose unique_id reads defensively as non-string is skipped, not crashed on."""
        removed, async_get, async_entries = self._make_fake_registry()
        entry, coordinator = self._make_entry_and_coordinator({}, self._sensors())

        with (
            patch("custom_components.rainpoint.er.async_get", side_effect=async_get),
            patch("custom_components.rainpoint.er.async_entries_for_config_entry", side_effect=async_entries),
        ):
            _remove_stale_generic_entities(MagicMock(), entry, coordinator)

        assert self.MALFORMED_UNIQUE_ID.entity_id not in removed

    def test_raising_registry_lookup_is_fail_soft(self):
        """A raising er.async_get degrades to a no-op sweep rather than propagating."""
        removed, async_get, async_entries = self._make_fake_registry(raise_on_lookup=True)
        entry, coordinator = self._make_entry_and_coordinator({}, self._sensors())

        with (
            patch("custom_components.rainpoint.er.async_get", side_effect=async_get),
            patch("custom_components.rainpoint.er.async_entries_for_config_entry", side_effect=async_entries),
        ):
            _remove_stale_generic_entities(MagicMock(), entry, coordinator)  # must not raise

        assert removed == []

    def test_raising_removal_of_one_row_does_not_abandon_the_rest(self):
        """A row whose removal raises does not prevent the remaining rows from being removed."""
        removed, async_get, async_entries = self._make_fake_registry(raise_once_for={self.GENERIC_A.entity_id})
        entry, coordinator = self._make_entry_and_coordinator({}, self._sensors())

        with (
            patch("custom_components.rainpoint.er.async_get", side_effect=async_get),
            patch("custom_components.rainpoint.er.async_entries_for_config_entry", side_effect=async_entries),
        ):
            _remove_stale_generic_entities(MagicMock(), entry, coordinator)

        assert self.GENERIC_A.entity_id not in removed
        assert set(removed) == {self.GENERIC_B.entity_id} | self._all_control_namespace_entity_ids()

    @pytest.mark.asyncio
    async def test_async_setup_entry_removes_generic_row_with_toggle_off(self):
        """End-to-end: async_setup_entry still returns True and sweeps generic rows."""
        removed, async_get, async_entries = self._make_fake_registry()

        hass = _make_hass()
        entry = _make_entry()
        entry.entry_id = self.ENTRY_ID
        # options == {} => generic entities disabled

        mock_client = MagicMock()
        mock_client.restore_tokens = MagicMock()
        mock_coordinator = MagicMock()
        mock_coordinator.async_config_entry_first_refresh = AsyncMock()
        mock_coordinator.data = {"sensors": self._sensors(), "hubs": []}
        hass.config_entries.async_forward_entry_setups = AsyncMock()

        with (
            patch("custom_components.rainpoint.RainPointClient", return_value=mock_client),
            patch(
                "custom_components.rainpoint.coordinator.RainPointCoordinator",
                return_value=mock_coordinator,
            ),
            patch("custom_components.rainpoint.er.async_get", side_effect=async_get),
            patch("custom_components.rainpoint.er.async_entries_for_config_entry", side_effect=async_entries),
        ):
            result = await async_setup_entry(hass, entry)

        assert result is True
        assert self.GENERIC_A.entity_id in removed


class TestSweepSurvivesUnreadableCoordinatorData(_GenericSweepFixtures):
    """A raising coordinator.data must not abort the sweep or escape setup.

    That read sits between the guarded registry lookup and the guarded
    per-row removal, and only feeds the graduation check on the toggle-on
    path. Aborting on it would also abandon the toggle-off path, which must
    remove every generic row and needs none of that data to decide.
    """

    def test_toggle_off_still_removes_every_generic_row(self):
        """The removal set is unchanged: toggle-off never consulted the coordinator anyway.

        Both namespace toggles are off here (control absent, same as the
        sensor toggle's explicit False), which also proves the control-off
        removal condition needs no coordinator data at all.
        """
        removed, async_get, async_entries = self._make_fake_registry()
        entry = MagicMock()
        entry.entry_id = self.ENTRY_ID
        entry.options = {CONF_GENERIC_ENTITIES_ENABLED: False}

        with (
            patch("custom_components.rainpoint.er.async_get", side_effect=async_get),
            patch("custom_components.rainpoint.er.async_entries_for_config_entry", side_effect=async_entries),
        ):
            _remove_stale_generic_entities(MagicMock(), entry, self._make_coordinator_with_raising_data())

        assert (
            set(removed)
            == {
                self.GENERIC_A.entity_id,
                self.GENERIC_B.entity_id,
            }
            | self._all_control_namespace_entity_ids()
        )

    def test_toggle_on_removes_nothing_rather_than_raising(self):
        """Without graduation data no model resolves, so no row in either namespace is evidence of graduation."""
        removed, async_get, async_entries = self._make_fake_registry()
        entry = MagicMock()
        entry.entry_id = self.ENTRY_ID
        entry.options = {CONF_GENERIC_ENTITIES_ENABLED: True, CONF_GENERIC_CONTROL_ENABLED: True}

        with (
            patch("custom_components.rainpoint.er.async_get", side_effect=async_get),
            patch("custom_components.rainpoint.er.async_entries_for_config_entry", side_effect=async_entries),
        ):
            _remove_stale_generic_entities(MagicMock(), entry, self._make_coordinator_with_raising_data())

        assert removed == []


class TestSweepSurvivesMalformedSensorRecords(_GenericSweepFixtures):
    """A sensor record that is not a dict must not abort the sweep or escape setup.

    Records come from the RainPoint payload, so their shape is an assumption
    rather than a guarantee. Reading a model out of one is the last step in
    this function that can raise, and it runs once per row: without a guard,
    a single malformed record would abandon every row after it.
    """

    def test_a_row_whose_record_is_not_a_dict_is_skipped_and_the_rest_still_sweep(self):
        """The graduated rows are still removed, in both namespaces, even though slug A's record could not be judged."""
        removed, async_get, async_entries = self._make_fake_registry()
        entry, coordinator = self._make_entry_and_coordinator(
            {CONF_GENERIC_ENTITIES_ENABLED: True, CONF_GENERIC_CONTROL_ENABLED: True},
            {self.SLUG_A: "a string where a record should be", self.SLUG_B: {"model": MODEL_HCS026FRF}},
        )

        with (
            patch("custom_components.rainpoint.er.async_get", side_effect=async_get),
            patch("custom_components.rainpoint.er.async_entries_for_config_entry", side_effect=async_entries),
        ):
            _remove_stale_generic_entities(MagicMock(), entry, coordinator)

        assert set(removed) == {self.GENERIC_B.entity_id, self.CONTROL_B.entity_id, self.DURATION_B.entity_id}

    def test_a_sensors_mapping_that_is_not_a_dict_leaves_every_row_alone(self):
        """Every per-row lookup, in either namespace, raises -- so no row is judged and none is removed."""
        removed, async_get, async_entries = self._make_fake_registry()
        entry, coordinator = self._make_entry_and_coordinator(
            {CONF_GENERIC_ENTITIES_ENABLED: True, CONF_GENERIC_CONTROL_ENABLED: True}, "not a mapping"
        )

        with (
            patch("custom_components.rainpoint.er.async_get", side_effect=async_get),
            patch("custom_components.rainpoint.er.async_entries_for_config_entry", side_effect=async_entries),
        ):
            _remove_stale_generic_entities(MagicMock(), entry, coordinator)

        assert removed == []


class TestGenericControlRowRemovalReasonGuards:
    """Direct coverage of _generic_control_row_removal_reason's own scoping guards.

    The sweep's dispatch already filters to (str, contains-control-marker)
    unique_ids before ever calling this function, so these two guard clauses
    are unreachable through _remove_stale_generic_entities itself; they exist
    on the function directly (mirroring _generic_row_removal_reason's shape)
    so a future direct caller gets the same defensive behaviour without
    depending on the dispatch's own filtering.
    """

    def test_non_string_unique_id_is_kept(self):
        assert _generic_control_row_removal_reason(None, True, {}) is None
        assert _generic_control_row_removal_reason(12345, True, {}) is None

    def test_unique_id_without_the_control_marker_is_kept(self):
        assert _generic_control_row_removal_reason("rainpoint_42_100_1_generic_sta_rh_p0", False, {}) is None
        assert _generic_control_row_removal_reason("not_the_right_prefix_generic_ctl_ctl_water_p1", False, {}) is None


class TestRemoveStaleGenericControlEntities(_GenericSweepFixtures):
    """Control-namespace-specific coverage: the two independence directions,
    the override removal condition, and foreign-entry isolation under every
    option combination.

    The single-namespace behaviours already covered symmetrically by
    TestRemoveStaleGenericEntities (toggle absent/false/true, graduation,
    unresolvable survival, idempotency, malformed data resilience) are not
    repeated here -- both reason functions share the same shape and the same
    sweep loop, so those cases are proven once, generically, by exercising
    both namespaces together in that class.
    """

    def test_sensor_option_true_control_option_false_removes_only_control_namespace(self):
        """Exact converse of the sensor-only tests: control off removes every
        control-namespace row for this entry (control + companion duration)
        and no sensor-namespace row, regardless of graduation state."""
        removed, async_get, async_entries = self._make_fake_registry()
        entry, coordinator = self._make_entry_and_coordinator(
            {CONF_GENERIC_ENTITIES_ENABLED: True, CONF_GENERIC_CONTROL_ENABLED: False}, self._sensors()
        )

        with (
            patch("custom_components.rainpoint.er.async_get", side_effect=async_get),
            patch("custom_components.rainpoint.er.async_entries_for_config_entry", side_effect=async_entries),
        ):
            _remove_stale_generic_entities(MagicMock(), entry, coordinator)

        assert set(removed) == self._all_control_namespace_entity_ids()
        assert self.GENERIC_A.entity_id not in removed
        assert self.GENERIC_B.entity_id not in removed

    def test_sensor_option_false_control_option_true_removes_only_sensor_namespace(self):
        """Exact converse of the previous test: sensor off removes every
        sensor-namespace row for this entry and no control-namespace row."""
        removed, async_get, async_entries = self._make_fake_registry()
        entry, coordinator = self._make_entry_and_coordinator(
            {CONF_GENERIC_ENTITIES_ENABLED: False, CONF_GENERIC_CONTROL_ENABLED: True}, self._sensors()
        )

        with (
            patch("custom_components.rainpoint.er.async_get", side_effect=async_get),
            patch("custom_components.rainpoint.er.async_entries_for_config_entry", side_effect=async_entries),
        ):
            _remove_stale_generic_entities(MagicMock(), entry, coordinator)

        assert set(removed) == {self.GENERIC_A.entity_id, self.GENERIC_B.entity_id}
        assert removed
        assert not (set(removed) & self._all_control_namespace_entity_ids())

    def test_override_rule_removes_only_the_overridden_variant(self, monkeypatch):
        """A committed override matching one seeded row's (model, modelCode) removes
        that row's control and companion duration entities; a sibling variant of the
        same model under a different modelCode is untouched."""
        monkeypatch.setattr("custom_components.rainpoint.GENERIC_CONTROL_OVERRIDE_DISABLED", frozenset({("HCS777ARF", "1")}))
        removed, async_get, async_entries = self._make_fake_registry()
        # The sensor option is also enabled here, matching the sibling-graduation
        # tests' pattern, so this test's assertion isolates the override
        # condition without the sensor-off condition also contributing rows.
        entry, coordinator = self._make_entry_and_coordinator(
            {CONF_GENERIC_ENTITIES_ENABLED: True, CONF_GENERIC_CONTROL_ENABLED: True},
            self._sensors(
                slug_a_model="HCS777ARF",
                slug_a_model_code=1,
                slug_b_model="HCS777ARF",
                slug_b_model_code=2,
            ),
        )

        with (
            patch("custom_components.rainpoint.er.async_get", side_effect=async_get),
            patch("custom_components.rainpoint.er.async_entries_for_config_entry", side_effect=async_entries),
        ):
            _remove_stale_generic_entities(MagicMock(), entry, coordinator)

        assert set(removed) == {self.CONTROL_A.entity_id, self.DURATION_A.entity_id}

    def test_override_rule_is_inert_when_the_variant_is_not_listed(self, monkeypatch):
        """An override set that names a different variant entirely removes nothing on its account."""
        monkeypatch.setattr(
            "custom_components.rainpoint.GENERIC_CONTROL_OVERRIDE_DISABLED", frozenset({("SOME_OTHER_MODEL", "7")})
        )
        removed, async_get, async_entries = self._make_fake_registry()
        entry, coordinator = self._make_entry_and_coordinator(
            {CONF_GENERIC_ENTITIES_ENABLED: True, CONF_GENERIC_CONTROL_ENABLED: True},
            self._sensors(slug_a_model="HCS777ARF", slug_a_model_code=1, slug_b_model="HCS777ARF", slug_b_model_code=2),
        )

        with (
            patch("custom_components.rainpoint.er.async_get", side_effect=async_get),
            patch("custom_components.rainpoint.er.async_entries_for_config_entry", side_effect=async_entries),
        ):
            _remove_stale_generic_entities(MagicMock(), entry, coordinator)

        assert removed == []

    def test_foreign_config_entry_control_row_is_never_removed_under_any_option_combination(self):
        """A control row belonging to another config entry is never removed, whichever option state applies."""
        option_combinations = (
            {},
            {CONF_GENERIC_CONTROL_ENABLED: True},
            {CONF_GENERIC_CONTROL_ENABLED: False},
            {CONF_GENERIC_ENTITIES_ENABLED: True, CONF_GENERIC_CONTROL_ENABLED: True},
        )
        for options in option_combinations:
            removed, async_get, async_entries = self._make_fake_registry()
            entry, coordinator = self._make_entry_and_coordinator(options, self._sensors())

            with (
                patch("custom_components.rainpoint.er.async_get", side_effect=async_get),
                patch("custom_components.rainpoint.er.async_entries_for_config_entry", side_effect=async_entries),
            ):
                _remove_stale_generic_entities(MagicMock(), entry, coordinator)

            assert self.FOREIGN_ENTRY_CONTROL.entity_id not in removed, f"leaked under options={options}"


class TestGenericSensorNamespaceNeverCollidesWithControlNamespace:
    """Guards the naming convention _remove_stale_generic_entities' dispatch relies on.

    The sweep routes a row to the control-namespace reason function purely by
    testing whether GENERIC_CONTROL_UNIQUE_ID_MARKER ("_generic_ctl_")
    appears in its unique_id, falling through to the sensor-namespace reason
    function otherwise. That is only correct because every curated sensor
    identity in generic_entities._IDENTITY_SPECS happens to lower-case to
    something that does not start with "ctl_". Nothing enforces that at
    runtime, so this locks it in: a future curated sensor identity added to
    that table without checking this would silently misroute its rows
    through the control toggle instead of the sensor toggle.
    """

    def test_no_identity_spec_key_collides_with_the_control_marker(self):
        from custom_components.rainpoint.const import GENERIC_CONTROL_UNIQUE_ID_MARKER, GENERIC_UNIQUE_ID_MARKER
        from custom_components.rainpoint.generic_entities import _IDENTITY_SPECS

        assert _IDENTITY_SPECS, "the identity table must not be empty, or this test proves nothing"
        for identity in _IDENTITY_SPECS:
            unique_id_fragment = f"{GENERIC_UNIQUE_ID_MARKER}{identity.lower()}"
            assert GENERIC_CONTROL_UNIQUE_ID_MARKER not in unique_id_fragment, (
                f"sensor identity {identity!r} would collide with the control namespace marker"
            )


class TestHubConnectivityEntityUnaffectedByGenericRegistrySweeps:
    """The hub-level Cloud Connection entity's unique_id carries neither the
    sensor nor the control generic marker, so neither registry sweep toggle
    can ever remove it, proven against the real reason functions rather
    than a restated constant.
    """

    HUB_CONNECTIVITY_UNIQUE_ID = "rainpoint_hub_42_100_connectivity"

    def test_sensor_toggle_off_does_not_match_the_connectivity_unique_id(self):
        assert _generic_row_removal_reason(self.HUB_CONNECTIVITY_UNIQUE_ID, False, {}) is None

    def test_sensor_toggle_on_does_not_match_the_connectivity_unique_id(self):
        assert _generic_row_removal_reason(self.HUB_CONNECTIVITY_UNIQUE_ID, True, {}) is None

    def test_control_toggle_off_does_not_match_the_connectivity_unique_id(self):
        assert _generic_control_row_removal_reason(self.HUB_CONNECTIVITY_UNIQUE_ID, False, {}) is None

    def test_control_toggle_on_does_not_match_the_connectivity_unique_id(self):
        assert _generic_control_row_removal_reason(self.HUB_CONNECTIVITY_UNIQUE_ID, True, {}) is None

    def test_full_sweep_with_both_toggles_off_leaves_the_connectivity_row_in_place(self):
        """End-to-end through _remove_stale_generic_entities: both toggles off
        would remove every generic-namespace row, and this row is not one."""
        connectivity_row = SimpleNamespace(
            entity_id="binary_sensor.rainpoint_hub_42_100_connectivity",
            unique_id=self.HUB_CONNECTIVITY_UNIQUE_ID,
            config_entry_id="this_entry",
        )
        removed: list[str] = []

        class _FakeRegistry:
            """Minimal registry stand-in recording every async_remove call."""

            def async_remove(self, entity_id):
                """Record the removed entity_id."""
                removed.append(entity_id)

        entry = MagicMock()
        entry.entry_id = "this_entry"
        entry.options = {}
        coordinator = MagicMock()
        coordinator.data = {"sensors": {}}

        with (
            patch("custom_components.rainpoint.er.async_get", return_value=_FakeRegistry()),
            patch("custom_components.rainpoint.er.async_entries_for_config_entry", return_value=[connectivity_row]),
        ):
            _remove_stale_generic_entities(MagicMock(), entry, coordinator)

        assert removed == []


class _DeviceSweepFixtures:
    """Seeded fake device-registry fixtures for _reconcile_sub_device_parents.

    One seeded registry, following _GenericSweepFixtures' shape: SimpleNamespace
    device rows as class constants with realistic {hid}_{mid}_{addr} identifiers,
    plus a _make_fake_device_registry helper mirroring _make_fake_registry's
    per-call re-filtering so a row cleared by one sweep presents as cleared on
    the next.
    """

    ENTRY_ID = "this_entry"
    OTHER_ENTRY_ID = "other_entry"

    # Two permanently-eligible wrapper-carried rows: hub_paired False, a
    # via_device_id already set. Two of them (not one) so the
    # raise-on-one-row test can prove the other still clears.
    WRAPPER_ROW = SimpleNamespace(
        id="device_wrapper_1",
        identifiers={(DOMAIN, "100_201_1")},
        via_device_id="hub_100",
        config_entry_id=ENTRY_ID,
    )
    WRAPPER_ROW_2 = SimpleNamespace(
        id="device_wrapper_4",
        identifiers={(DOMAIN, "100_201_4")},
        via_device_id="hub_100",
        config_entry_id=ENTRY_ID,
    )
    # Two distinct real hubs, each carrying its own sub-device row. No second
    # real hub exists on maintainer hardware; this proves the case against a
    # constructed fixture only (see test_two_real_hubs_... docstring below).
    HUB_A_SUB_ROW = SimpleNamespace(
        id="device_hub_a_sub_1",
        identifiers={(DOMAIN, "100_200_1")},
        via_device_id="hub_100",
        config_entry_id=ENTRY_ID,
    )
    HUB_B_SUB_ROW = SimpleNamespace(
        id="device_hub_b_sub_1",
        identifiers={(DOMAIN, "100_300_1")},
        via_device_id="hub_300",
        config_entry_id=ENTRY_ID,
    )
    ALREADY_CLEARED_ROW = SimpleNamespace(
        id="device_wrapper_2",
        identifiers={(DOMAIN, "100_201_2")},
        via_device_id=None,
        config_entry_id=ENTRY_ID,
    )
    NOT_IN_POLL_ROW = SimpleNamespace(
        id="device_wrapper_3",
        identifiers={(DOMAIN, "100_201_3")},
        via_device_id="hub_100",
        config_entry_id=ENTRY_ID,
    )
    HUB_DEVICE_ROW = SimpleNamespace(
        id="device_hub_100",
        identifiers={(DOMAIN, "hub_100")},
        via_device_id="hub_100",
        config_entry_id=ENTRY_ID,
    )
    MALFORMED_ROW = SimpleNamespace(
        id="device_malformed",
        identifiers="not-a-set-of-tuples",
        via_device_id="hub_100",
        config_entry_id=ENTRY_ID,
    )
    NO_DOMAIN_ROW = SimpleNamespace(
        id="device_no_domain",
        identifiers={("other_integration", "100_201_9")},
        via_device_id="hub_100",
        config_entry_id=ENTRY_ID,
    )
    MISSING_IDENTIFIERS_ROW = SimpleNamespace(
        id="device_missing_identifiers",
        via_device_id="hub_100",
        config_entry_id=ENTRY_ID,
    )
    # Its sensor record below is a string rather than a dict, the one shape a
    # cloud payload can produce that the hub_paired read cannot survive.
    NON_DICT_RECORD_ROW = SimpleNamespace(
        id="device_non_dict_record",
        identifiers={(DOMAIN, "100_201_5")},
        via_device_id="hub_100",
        config_entry_id=ENTRY_ID,
    )
    # Belongs to a different config entry. The current-poll sensors mapping
    # below deliberately makes this row look eligible if it ever leaked
    # through the config-entry-scoped lookup, so a regression there would be
    # caught rather than passing by accident.
    FOREIGN_ENTRY_ROW = SimpleNamespace(
        id="device_foreign_entry",
        identifiers={(DOMAIN, "99_500_1")},
        via_device_id="hub_99",
        config_entry_id=OTHER_ENTRY_ID,
    )

    def _all_rows(self):
        """Return every seeded device row across both config entries."""
        return [
            self.WRAPPER_ROW,
            self.WRAPPER_ROW_2,
            self.HUB_A_SUB_ROW,
            self.HUB_B_SUB_ROW,
            self.ALREADY_CLEARED_ROW,
            self.NOT_IN_POLL_ROW,
            self.HUB_DEVICE_ROW,
            self.MALFORMED_ROW,
            self.NO_DOMAIN_ROW,
            self.MISSING_IDENTIFIERS_ROW,
            self.NON_DICT_RECORD_ROW,
            self.FOREIGN_ENTRY_ROW,
        ]

    def _sensors(self):
        """Sensor keys present in the current poll, with a stamped hub_paired verdict.

        "100_201_3" (NOT_IN_POLL_ROW) is deliberately absent. "99_500_1"
        (FOREIGN_ENTRY_ROW) is present and eligible-looking so a leak past
        the config-entry scope guard would be caught, not accidentally missed.
        """
        return {
            "100_201_1": {"hub_paired": False},
            "100_201_4": {"hub_paired": False},
            "100_200_1": {"hub_paired": True},
            "100_300_1": {"hub_paired": True},
            "100_201_2": {"hub_paired": False},
            "100_201_5": "not-a-record",
            "99_500_1": {"hub_paired": False},
        }

    def _make_fake_device_registry(self, raise_on_lookup=False, raise_once_for=None):
        """Build patchable stand-ins for dr.async_get / dr.async_entries_for_config_entry.

        async_entries_for_config_entry re-filters on each call: the returned
        rows are fresh copies whose via_device_id reflects every update this
        registry has recorded so far, rather than mutating the shared class-
        level SimpleNamespace constants (which would leak state across
        tests). This is what makes the idempotency assertion (two
        consecutive sweeps) meaningful rather than incidental.
        """
        updated: list[tuple] = []
        cleared: set[str] = set()
        raise_once_for = set(raise_once_for or ())

        class _FakeDeviceRegistry:
            """Records every clearing call, optionally raising once per seeded id."""

            def async_update_device(self, device_id, *, via_device_id):
                """Record one clearing call, or raise if this id is armed to fail."""
                if device_id in raise_once_for:
                    raise_once_for.discard(device_id)
                    raise RuntimeError(f"boom updating {device_id}")
                updated.append((device_id, via_device_id))
                cleared.add(device_id)

        fake_registry = _FakeDeviceRegistry()

        def _async_get(hass):
            """Return the fake registry, or raise to drive the lookup guard."""
            if raise_on_lookup:
                raise RuntimeError("registry unavailable")
            return fake_registry

        def _copy_row(row):
            """Return a fresh row reflecting the clears recorded so far.

            Copies rather than mutating the class-level seed rows, so state
            cannot leak between tests.
            """
            # Built field-by-field, omitting "identifiers" entirely when the
            # seed row omits it, so MISSING_IDENTIFIERS_ROW still raises
            # AttributeError inside the function under test rather than here.
            kwargs = {"id": row.id, "via_device_id": None if row.id in cleared else row.via_device_id}
            if hasattr(row, "identifiers"):
                kwargs["identifiers"] = row.identifiers
            return SimpleNamespace(**kwargs)

        def _async_entries_for_config_entry(registry, entry_id):
            """Return this config entry's seeded rows, re-derived on every call."""
            return [_copy_row(row) for row in self._all_rows() if row.config_entry_id == entry_id]

        return updated, _async_get, _async_entries_for_config_entry

    def _make_entry_and_coordinator(self, sensors, entry_id=None):
        """Return an (entry, coordinator) pair whose poll data is the given sensors."""
        entry = MagicMock()
        entry.entry_id = entry_id or self.ENTRY_ID
        coordinator = MagicMock()
        coordinator.data = {"sensors": sensors}
        return entry, coordinator


class TestReconcileSubDeviceParents(_DeviceSweepFixtures):
    """Cover _reconcile_sub_device_parents against one seeded fake device registry.

    A mocked registry is used rather than a live Home Assistant one, for the
    same reason TestRemoveStaleGenericEntities gives: the repository conftest
    replaces the whole homeassistant package tree with stubs before any test
    module is imported.
    """

    def test_wrapper_carried_rows_with_via_device_id_are_cleared(self):
        """Both wrapper-carried rows (hub_paired False, sensor key in the
        current poll, via_device_id already set) are cleared exactly once
        each. Every other seeded row for this entry -- both real hubs' own
        sub-devices, the already-cleared row, the row absent from the poll,
        and the hub device row -- produces zero calls, together in one
        sweep."""
        updated, async_get, async_entries = self._make_fake_device_registry()
        entry, coordinator = self._make_entry_and_coordinator(self._sensors())

        with (
            patch("custom_components.rainpoint.dr.async_get", side_effect=async_get),
            patch("custom_components.rainpoint.dr.async_entries_for_config_entry", side_effect=async_entries),
        ):
            _reconcile_sub_device_parents(MagicMock(), entry, coordinator)

        assert updated == [(self.WRAPPER_ROW.id, None), (self.WRAPPER_ROW_2.id, None)]

    def test_two_real_hubs_each_keep_their_own_sub_devices_link(self):
        """No second real hub exists on maintainer hardware, so this case is
        proven against a constructed fixture only and is recorded here as a
        backstop rather than a verified behaviour. Two distinct real hubs in
        one home, each carrying its own hub_paired=True sub-device, must both
        keep their via_device_id through the sweep."""
        updated, async_get, async_entries = self._make_fake_device_registry()
        entry, coordinator = self._make_entry_and_coordinator(self._sensors())

        with (
            patch("custom_components.rainpoint.dr.async_get", side_effect=async_get),
            patch("custom_components.rainpoint.dr.async_entries_for_config_entry", side_effect=async_entries),
        ):
            _reconcile_sub_device_parents(MagicMock(), entry, coordinator)

        cleared_ids = {device_id for device_id, _ in updated}
        assert self.HUB_A_SUB_ROW.id not in cleared_ids
        assert self.HUB_B_SUB_ROW.id not in cleared_ids

    def test_malformed_and_missing_identifier_rows_are_skipped_and_sweep_continues(self):
        """A row with no DOMAIN identifier, a malformed identifiers value, or
        a missing attribute is skipped, and the sweep still clears the
        eligible rows around them."""
        updated, async_get, async_entries = self._make_fake_device_registry()
        entry, coordinator = self._make_entry_and_coordinator(self._sensors())

        with (
            patch("custom_components.rainpoint.dr.async_get", side_effect=async_get),
            patch("custom_components.rainpoint.dr.async_entries_for_config_entry", side_effect=async_entries),
        ):
            _reconcile_sub_device_parents(MagicMock(), entry, coordinator)

        cleared_ids = {device_id for device_id, _ in updated}
        assert self.MALFORMED_ROW.id not in cleared_ids
        assert self.NO_DOMAIN_ROW.id not in cleared_ids
        assert self.MISSING_IDENTIFIERS_ROW.id not in cleared_ids
        assert self.WRAPPER_ROW.id in cleared_ids

    def test_a_row_whose_sensor_record_is_not_a_dict_is_skipped(self):
        """A cloud payload that yields a non-dict record is a payload problem,
        not a registry one: the row is skipped by the isinstance filter rather
        than raising into the per-row guard, and the rows around it still
        clear."""
        updated, async_get, async_entries = self._make_fake_device_registry()
        entry, coordinator = self._make_entry_and_coordinator(self._sensors())

        with (
            patch("custom_components.rainpoint.dr.async_get", side_effect=async_get),
            patch("custom_components.rainpoint.dr.async_entries_for_config_entry", side_effect=async_entries),
        ):
            _reconcile_sub_device_parents(MagicMock(), entry, coordinator)

        cleared_ids = {device_id for device_id, _ in updated}
        assert self.NON_DICT_RECORD_ROW.id not in cleared_ids
        assert self.WRAPPER_ROW.id in cleared_ids

    def test_foreign_entry_row_is_never_returned_or_updated(self):
        """A row belonging to a different config entry is never returned by
        the fixture's config-entry-scoped lookup and is never updated, even
        though its sensor key looks eligible in the seeded poll data."""
        updated, async_get, async_entries = self._make_fake_device_registry()
        entry, coordinator = self._make_entry_and_coordinator(self._sensors())

        registry = async_get(MagicMock())
        returned_ids = {row.id for row in async_entries(registry, self.ENTRY_ID)}
        assert self.FOREIGN_ENTRY_ROW.id not in returned_ids

        with (
            patch("custom_components.rainpoint.dr.async_get", side_effect=async_get),
            patch("custom_components.rainpoint.dr.async_entries_for_config_entry", side_effect=async_entries),
        ):
            _reconcile_sub_device_parents(MagicMock(), entry, coordinator)

        assert self.FOREIGN_ENTRY_ROW.id not in {device_id for device_id, _ in updated}

    def test_raising_registry_lookup_returns_without_updating_anything(self):
        """A registry that cannot be fetched skips the sweep instead of raising
        into config-entry setup, and clears nothing on the way out."""
        updated, async_get, async_entries = self._make_fake_device_registry(raise_on_lookup=True)
        entry, coordinator = self._make_entry_and_coordinator(self._sensors())

        with (
            patch("custom_components.rainpoint.dr.async_get", side_effect=async_get),
            patch("custom_components.rainpoint.dr.async_entries_for_config_entry", side_effect=async_entries),
        ):
            _reconcile_sub_device_parents(MagicMock(), entry, coordinator)

        assert updated == []

    def test_unreadable_coordinator_data_clears_nothing(self):
        """Degradation is the opposite direction from the generic-entity sweep:
        the only mutation here is destructive, so unreadable data must clear
        nothing rather than fall back to acting on empty data."""
        updated, async_get, async_entries = self._make_fake_device_registry()
        entry = MagicMock()
        entry.entry_id = self.ENTRY_ID

        class _RaisingCoordinator:
            """A coordinator whose data read raises, driving the read guard."""

            @property
            def data(self):
                """Raise, standing in for an unreadable coordinator snapshot."""
                raise RuntimeError("coordinator data unavailable")

        with (
            patch("custom_components.rainpoint.dr.async_get", side_effect=async_get),
            patch("custom_components.rainpoint.dr.async_entries_for_config_entry", side_effect=async_entries),
        ):
            _reconcile_sub_device_parents(MagicMock(), entry, _RaisingCoordinator())

        assert updated == []

    def test_raising_update_on_one_row_does_not_block_the_remaining_rows(self):
        """A registry update that raises on one eligible row must not prevent
        the other eligible row from being cleared."""
        updated, async_get, async_entries = self._make_fake_device_registry(raise_once_for={self.WRAPPER_ROW.id})
        entry, coordinator = self._make_entry_and_coordinator(self._sensors())

        with (
            patch("custom_components.rainpoint.dr.async_get", side_effect=async_get),
            patch("custom_components.rainpoint.dr.async_entries_for_config_entry", side_effect=async_entries),
        ):
            _reconcile_sub_device_parents(MagicMock(), entry, coordinator)

        assert updated == [(self.WRAPPER_ROW_2.id, None)]

    def test_repeat_sweep_is_a_genuine_no_op(self):
        """A second consecutive sweep, against a registry that re-filters on
        every call, issues zero calls: the first sweep's clears are reflected
        the second time the rows are fetched."""
        updated, async_get, async_entries = self._make_fake_device_registry()
        entry, coordinator = self._make_entry_and_coordinator(self._sensors())

        with (
            patch("custom_components.rainpoint.dr.async_get", side_effect=async_get),
            patch("custom_components.rainpoint.dr.async_entries_for_config_entry", side_effect=async_entries),
        ):
            _reconcile_sub_device_parents(MagicMock(), entry, coordinator)
            first_run = list(updated)
            _reconcile_sub_device_parents(MagicMock(), entry, coordinator)

        assert first_run == [(self.WRAPPER_ROW.id, None), (self.WRAPPER_ROW_2.id, None)]
        assert updated == first_run


class TestSubDeviceParentReconcileRealTimeline:
    """Drives construct -> first refresh -> setup -> further polls for the
    device the parenting fix actually exists for: the Bluetooth-only HTV210B,
    which reports no status at all.

    A silent sub-device is absent from coordinator.data["sensors"] until it
    has been omitted for SILENT_DEBOUNCE_POLLS consecutive polls, so at the
    moment the setup-time sweep runs its sensor key does not exist yet and
    the scope guard leaves its row alone. Every earlier test for the sweep
    injected a coordinator.data snapshot that already listed the key, which
    is how a suite at full branch coverage still shipped a fix that could
    not reach the reported hardware. This drives the real order instead.
    """

    ENTRY_ID = "test_entry"
    ROW_ID = "device_bt_valve"
    SENSOR_KEY = "100_200_1"

    @classmethod
    def _make_single_row_device_registry(cls, row_id, sensor_key):
        """Return (updated, async_get, async_entries) over one pre-existing row.

        The row stands for a sub-device registered by an earlier version, so
        it already carries the stale via_device_id that the DeviceInfo path
        alone cannot clear.

        Named apart from _DeviceSweepFixtures._make_fake_device_registry
        rather than shadowing it, because that one seeds a whole registry and
        this one seeds exactly one row. It keeps the two properties that make
        the difference observable: rows are re-derived per call and keyed on
        this row's own id, so "cleared the right row" cannot be confused with
        "cleared any row", and the config-entry scope is honoured, so the
        sweep's config-entry-scoped lookup is exercised here too rather than
        being a no-op the fixture papers over.
        """
        updated: list[tuple] = []
        cleared: set[str] = set()

        class _FakeDeviceRegistry:
            """Records each clearing call and remembers which rows are cleared."""

            def async_update_device(self, device_id, *, via_device_id):
                """Record one clearing call against this row's own id."""
                updated.append((device_id, via_device_id))
                cleared.add(device_id)

        registry = _FakeDeviceRegistry()

        def _async_get(hass):
            """Return the single-row fake registry."""
            return registry

        def _async_entries_for_config_entry(reg, entry_id):
            """Return the one seeded row for this config entry, re-derived per call."""
            if entry_id != cls.ENTRY_ID:
                return []
            via = None if row_id in cleared else "hub_100"
            return [SimpleNamespace(id=row_id, identifiers={(DOMAIN, sensor_key)}, via_device_id=via)]

        return updated, _async_get, _async_entries_for_config_entry

    @classmethod
    def _build_coordinator(cls):
        """Build a real coordinator over one wrapper record whose only child is silent."""
        from custom_components.rainpoint.coordinator import RainPointCoordinator

        client = AsyncMock()
        client.get_devices_by_hid.return_value = [make_silent_wrapper_hub_record()]
        # Arrived but named nobody: the silent debounce increments rather
        # than the status-outage path firing.
        client.get_multiple_device_status.return_value = [{"mid": 200, "subDeviceStatus": []}]

        entry = MagicMock()
        entry.entry_id = cls.ENTRY_ID
        entry.data = {"hids": [100]}
        entry.options = {}

        hass = MagicMock()
        hass.data = {DOMAIN: {cls.ENTRY_ID: {}}}

        return RainPointCoordinator(hass, client, entry), hass, entry

    @pytest.mark.asyncio
    async def test_silent_wrapper_child_is_reconciled_once_the_debounce_lands(self):
        """The reported symptom, as a timeline: nothing to sweep at setup,
        and the stale link cleared on the poll that first surfaces the
        device. Asserting only the end state would pass against a
        setup-only sweep that never reaches this device at all."""
        coordinator, hass, entry = self._build_coordinator()
        updated, async_get, async_entries = self._make_single_row_device_registry(self.ROW_ID, self.SENSOR_KEY)

        await coordinator.async_config_entry_first_refresh()

        with (
            patch("custom_components.rainpoint.dr.async_get", side_effect=async_get),
            patch("custom_components.rainpoint.dr.async_entries_for_config_entry", side_effect=async_entries),
        ):
            _reconcile_sub_device_parents_on_updates(hass, entry, coordinator)

            # Poll 1: the key is still below the debounce, so the scope guard
            # skips the row rather than clearing it.
            assert self.SENSOR_KEY not in coordinator.data["sensors"]
            assert updated == []

            await coordinator.async_refresh()
            await coordinator.async_refresh()

            assert self.SENSOR_KEY in coordinator.data["sensors"]
            assert updated == [(self.ROW_ID, None)]

            # Further polls change nothing: the row now reads back cleared.
            await coordinator.async_refresh()
            assert updated == [(self.ROW_ID, None)]

    @pytest.mark.asyncio
    async def test_an_update_that_surfaces_no_new_key_does_not_sweep(self):
        """The listener's narrowing, asserted on the registry lookup rather
        than only on its outcome.

        An update that surfaces nothing new must not reach the registry at
        all, because the sweep's only mutation is irreversible within the
        session: a link cleared off one degraded poll would stay cleared
        until a reload. Counting lookups is what distinguishes "swept and
        found nothing to do" from "did not sweep"; the cleared-row count
        cannot tell those apart."""
        coordinator, hass, entry = self._build_coordinator()
        _updated, async_get, async_entries = self._make_single_row_device_registry(self.ROW_ID, self.SENSOR_KEY)
        lookups = []

        def _counting_async_get(hass_arg):
            """Count each registry lookup, then defer to the fake registry."""
            lookups.append(hass_arg)
            return async_get(hass_arg)

        await coordinator.async_config_entry_first_refresh()

        with (
            patch("custom_components.rainpoint.dr.async_get", side_effect=_counting_async_get),
            patch("custom_components.rainpoint.dr.async_entries_for_config_entry", side_effect=async_entries),
        ):
            _reconcile_sub_device_parents_on_updates(hass, entry, coordinator)
            assert len(lookups) == 1

            # Poll 2: still below the debounce, so no key surfaced.
            await coordinator.async_refresh()
            assert len(lookups) == 1

            # Poll 3: the silent key appears, which is the one case the
            # listener exists for.
            await coordinator.async_refresh()
            assert len(lookups) == 2

            # Poll 4: same keys as poll 3, so no sweep.
            await coordinator.async_refresh()
            assert len(lookups) == 2

    @pytest.mark.asyncio
    async def test_the_update_listener_stops_sweeping_once_unregistered(self):
        """The listener goes through entry.async_on_unload like every other
        one, so a reload does not accumulate a second sweep per update.

        Asserted through the observable contract (does a later update still
        sweep) rather than by counting coordinator._listeners, which is a
        conftest-stub private whose list shape diverges from real Home
        Assistant's dict and would break on a purely additive stub
        hardening."""
        coordinator, hass, entry = self._build_coordinator()
        _updated, async_get, async_entries = self._make_single_row_device_registry(self.ROW_ID, self.SENSOR_KEY)
        lookups = []

        def _counting_async_get(hass_arg):
            """Count each registry lookup, then defer to the fake registry."""
            lookups.append(hass_arg)
            return async_get(hass_arg)

        await coordinator.async_config_entry_first_refresh()

        with (
            patch("custom_components.rainpoint.dr.async_get", side_effect=_counting_async_get),
            patch("custom_components.rainpoint.dr.async_entries_for_config_entry", side_effect=async_entries),
        ):
            _reconcile_sub_device_parents_on_updates(hass, entry, coordinator)
            remove = entry.async_on_unload.call_args[0][0]

            # Armed: a newly surfaced key sweeps.
            coordinator.data["sensors"]["100_200_9"] = {"hub_paired": False}
            coordinator.async_update_listeners()
            assert len(lookups) == 2

            remove()

            # Disarmed: the same trigger now does nothing at all.
            coordinator.data["sensors"]["100_200_8"] = {"hub_paired": False}
            coordinator.async_update_listeners()
            assert len(lookups) == 2


class TestReconcileSubDeviceParentsUnpatchedRegistryStub:
    """Pins the conftest MagicMock device-registry stub's no-op behaviour.

    Every pre-existing async_setup_entry test in this file now runs the
    reconcile without patching dr, so what happens there needs to be
    deliberate rather than incidental. homeassistant.helpers.device_registry
    is stubbed as a MagicMock module by conftest, and MagicMock implements
    __iter__, returning an empty iterator by default -- so
    list(dr.async_entries_for_config_entry(...)) evaluates to [] without
    raising, and the per-row loop never executes. The registry-fetch
    exception guard is never entered; this is not the swallowed-exception
    path.

    The real hazard this pins is a vacuous pass: a future test that forgets
    to patch dr would see zero rows and go green while asserting nothing at
    all. This test asserts zero update calls rather than merely "did not
    raise", precisely so that difference stays visible.
    """

    def test_unpatched_dr_stub_walks_zero_rows_and_updates_nothing(self):
        """The unpatched conftest stub yields zero rows and updates nothing, so
        a test that forgets to patch dr cannot pass vacuously."""
        from custom_components.rainpoint import dr as unpatched_dr

        entry = MagicMock()
        entry.entry_id = "some_entry_never_patched"
        coordinator = MagicMock()
        coordinator.data = {"sensors": {"1_2_3": {"hub_paired": False}}}

        registry = unpatched_dr.async_get(MagicMock())
        calls_before = registry.async_update_device.call_count

        _reconcile_sub_device_parents(MagicMock(), entry, coordinator)

        assert registry.async_update_device.call_count == calls_before


class TestReconcileSubDeviceParentsCallSiteOrdering:
    """Pins which reconcile entry point async_setup_entry calls, and when.

    Patches _reconcile_sub_device_parents_on_updates, the wrapper, not
    _reconcile_sub_device_parents, the bare sweep. The distinction is the
    whole point of this test: the wrapper calls the sweep by the same module
    global, so patching the sweep is satisfied identically whether setup
    calls the wrapper or the sweep, and reverting the call site to the bare
    sweep -- which is exactly the v1.12.0b3 state that failed hardware UAT as
    a critical issue -- left the suite green at 100% branch coverage.
    """

    @pytest.mark.asyncio
    async def test_reconcile_runs_once_after_first_refresh_and_before_forward_setups(self):
        """Ordering here is not cosmetic: running before the first refresh
        would read empty sensor data and, under the current-poll membership
        guard in _reconcile_sub_device_parents, clear nothing at all, which is
        a silent total feature kill that a state-only assertion would not
        catch."""
        hass = _make_hass()
        entry = _make_entry()
        order: list[str] = []

        mock_client = MagicMock()
        mock_client.restore_tokens = MagicMock()

        mock_coordinator = MagicMock()

        async def _first_refresh():
            """Record that the coordinator's first refresh ran."""
            order.append("first_refresh")

        mock_coordinator.async_config_entry_first_refresh = AsyncMock(side_effect=_first_refresh)

        async def _forward_setups(entry_arg, platforms):
            """Record that the platforms were forwarded."""
            order.append("forward_setups")

        hass.config_entries.async_forward_entry_setups = AsyncMock(side_effect=_forward_setups)

        def _reconcile(hass_arg, entry_arg, coordinator_arg):
            """Record that the parenting reconcile ran."""
            order.append("reconcile")

        with (
            patch("custom_components.rainpoint.RainPointClient", return_value=mock_client),
            patch("custom_components.rainpoint.coordinator.RainPointCoordinator", return_value=mock_coordinator),
            patch(
                "custom_components.rainpoint._reconcile_sub_device_parents_on_updates",
                side_effect=_reconcile,
            ) as mock_reconcile,
        ):
            await async_setup_entry(hass, entry)

        assert mock_reconcile.call_count == 1
        assert order == ["first_refresh", "reconcile", "forward_setups"]

    @pytest.mark.asyncio
    async def test_setup_arms_the_update_listener_through_async_on_unload(self):
        """Setup must leave the reconcile armed for later updates, not merely
        run it once, which is what reaches a device that surfaces after the
        first refresh. Asserted against the real
        _reconcile_sub_device_parents_on_updates (unpatched) so the listener
        registration is the production one, with only the sweep it calls
        stubbed out.

        The hub identity re-key's wrapper also runs here, unpatched, with only
        its sweep stubbed to the cleanly-migrated verdict. That is what an
        install with no residual returns, and such an install must arm no
        listener at all, so it contributes none of the registrations counted
        below.

        The orphaned entity sweep's wrapper is the second registration. It
        arms unconditionally, unlike the re-key's, because both its raise and
        its clear are idempotent and it walks no registry on the update path.
        Counting both rather than loosening the assertion keeps this test able
        to catch an accidental extra listener."""
        hass = _make_hass()
        entry = _make_entry()

        mock_client = MagicMock()
        mock_client.restore_tokens = MagicMock()

        mock_coordinator = MagicMock()
        mock_coordinator.data = {"sensors": {}}
        mock_coordinator.async_config_entry_first_refresh = AsyncMock()
        hass.config_entries.async_forward_entry_setups = AsyncMock()

        with (
            patch("custom_components.rainpoint.RainPointClient", return_value=mock_client),
            patch("custom_components.rainpoint.coordinator.RainPointCoordinator", return_value=mock_coordinator),
            patch("custom_components.rainpoint._reconcile_sub_device_parents"),
            patch("custom_components.rainpoint._complete_hub_identity_rekey", return_value=frozenset()),
        ):
            await async_setup_entry(hass, entry)

        assert mock_coordinator.async_add_listener.call_count == 2
        entry.async_on_unload.assert_any_call(mock_coordinator.async_add_listener.return_value)


class _OrderingCoordinator:
    """A coordinator stand-in that fires its listeners in registration order.

    Home Assistant's DataUpdateCoordinator notifies in registration order, and
    that is the only reason the orphan sweep can be said to run ahead of the
    late adders. Modelling the ordering explicitly here keeps the behavioural
    half of that claim honest rather than resting on a MagicMock.
    """

    def __init__(self):
        """Start with the empty poll shape every consumer here reads."""
        self.data = {"sensors": {}, "hubs": []}
        self.listeners = []

    def async_add_listener(self, callback_fn):
        """Register one listener and return its remover."""
        self.listeners.append(callback_fn)
        return lambda: None

    def async_update_listeners(self):
        """Notify every listener in the order it registered."""
        for callback_fn in list(self.listeners):
            callback_fn()

    async def async_config_entry_first_refresh(self):
        """Stand in for the first refresh, which needs to do nothing here."""

    def aged_out_sensor_keys(self):
        """No key has aged out in this fixture."""
        return frozenset()


class TestOrphanSweepCallSiteOrdering:
    """Pins where the orphaned-entity sweep's wrapper sits in setup.

    Two independent assertions, because either alone is satisfiable by a
    broken call site. The source assertion catches a wrapper moved after the
    platform forward even on an install where no adder happens to register;
    the behavioural one catches a source order that reads correctly while the
    listener is armed somewhere else entirely.
    """

    def test_the_wrapper_is_called_after_the_parenting_pass_and_before_the_forward(self):
        """A wrapper registered after the platform forward would run after
        every late adder on every update, inverting the isolation this
        listener has its own registration for."""
        source = inspect.getsource(async_setup_entry)

        parenting = source.index("_reconcile_sub_device_parents_on_updates")
        orphan = source.index("_sync_orphaned_entity_issues_on_updates")
        forward = source.index("async_forward_entry_setups")

        assert parenting < orphan < forward

    @pytest.mark.asyncio
    async def test_the_sweep_listener_fires_before_a_late_adder_registered_in_the_forward(self):
        """The same claim as a behaviour: the adders register from inside the
        platform forward, so a wrapper registered before it always notifies
        first."""
        hass = _make_hass()
        entry = _make_entry()
        order: list[str] = []

        mock_client = MagicMock()
        mock_client.restore_tokens = MagicMock()
        coordinator = _OrderingCoordinator()

        async def _forward_setups(entry_arg, platforms):
            """Register a late adder's listener the way a platform setup does."""
            coordinator.async_add_listener(lambda: order.append("adder"))

        hass.config_entries.async_forward_entry_setups = AsyncMock(side_effect=_forward_setups)

        with (
            patch("custom_components.rainpoint.RainPointClient", return_value=mock_client),
            patch("custom_components.rainpoint.coordinator.RainPointCoordinator", return_value=coordinator),
            patch("custom_components.rainpoint._reconcile_sub_device_parents"),
            patch("custom_components.rainpoint._complete_hub_identity_rekey", return_value=frozenset()),
            patch(
                "custom_components.rainpoint._sync_orphaned_entity_issues",
                side_effect=lambda *args: order.append("sweep"),
            ),
        ):
            await async_setup_entry(hass, entry)

            # The setup-time pass, before any listener has fired.
            assert order == ["sweep"]

            order.clear()
            coordinator.async_update_listeners()

        assert order == ["sweep", "adder"]


class TestPriorPhaseSweepsUnchanged:
    """The leftover-entity removal path widened no sweep that came before it.

    Its removals reach a row through one session's own ledger for one key, and
    nothing else in this module learned a new removal reason to make that
    possible. The eight functions below are the whole prior surface that
    deletes, moves or rewrites a registry row, and the load-bearing assertion
    is that none of them has acquired any knowledge of the removal path: not
    the coordinator's aged-out accessor, not the adder store, not the ledger,
    not the remover, and not the executor.

    Asserted on the source rather than on behaviour on purpose. A behavioural
    regression suite proves each function still does what it did; it cannot
    prove that none of them grew a second reason nobody exercised yet. The
    distinctive markers are the other half: a function gutted down to a stub
    would satisfy the negative assertion trivially.
    """

    # Every symbol the leftover-entity removal path introduced. "forget" is the
    # broadest of them deliberately: it is a substring test, so any spelling of
    # the lockstep drop reaching one of these functions trips it.
    REMOVAL_PATH_SYMBOLS = (
        "aged_out_sensor_keys",
        "late_adders",
        "orphan_entity_remover",
        "EmittedEntityLedger",
        "forget",
        "_remove_orphaned_key_rows",
        "_device_row_for_sensor_key",
        "_device_row_is_empty",
    )

    # One marker per function that no other function in this module carries, so
    # "unchanged" cannot be satisfied by an empty body.
    DISTINCTIVE_MARKERS: ClassVar[dict[str, str]] = {
        "_remove_stale_generic_entities": "the generic entity sweep",
        "_generic_row_removal_reason": "generic entities are disabled",
        "_generic_control_row_removal_reason": "generic control is disabled",
        "_reconcile_sub_device_parents": "the sub-device parenting reconcile",
        "_reconcile_sub_device_parents_on_updates": "seeding an empty swept-key set",
        "_complete_hub_identity_rekey": "the residual hub identity re-key",
        "_complete_hub_identity_rekey_on_updates": "_complete_hub_identity_rekey(",
        "async_migrate_entry": "async_update_entry(entry, version=2)",
    }

    @pytest.mark.parametrize("name", sorted(DISTINCTIVE_MARKERS))
    def test_the_prior_sweep_still_carries_its_own_distinctive_marker(self, name):
        """Each function is still the function it was, not a stub that would
        pass the negative assertion below for the wrong reason."""
        import custom_components.rainpoint as package

        source = inspect.getsource(getattr(package, name))

        assert self.DISTINCTIVE_MARKERS[name] in source

    @pytest.mark.parametrize("name", sorted(DISTINCTIVE_MARKERS))
    def test_the_prior_sweep_references_nothing_the_removal_path_added(self, name):
        """No prior sweep or migration learned about the removal path.

        The removal reaches a generic-namespace row through the key's ledger,
        and it never widened the generic sweep to do it. The same holds for the
        parenting reconcile, the residual hub re-key and the version-boundary
        migration."""
        import custom_components.rainpoint as package

        source = inspect.getsource(getattr(package, name))

        offenders = [symbol for symbol in self.REMOVAL_PATH_SYMBOLS if symbol in source]
        assert offenders == []
