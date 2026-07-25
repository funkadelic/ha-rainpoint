"""Tests for custom_components.rainpoint.__init__ (integration lifecycle)."""

import asyncio
import contextlib
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.rainpoint import (
    DOMAIN,
    _remove_stale_generic_entities,
    async_reload_integration,
    async_setup,
    async_setup_entry,
    async_unload_entry,
)
from custom_components.rainpoint.const import CONF_GENERIC_ENTITIES_ENABLED, CONF_PUSH_ENABLED, MODEL_HCS026FRF


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
    async def test_async_reload_entry_calls_unload_then_setup(self):
        """async_reload_entry unloads then re-sets up the entry."""
        from custom_components.rainpoint import async_reload_entry

        hass = _make_hass()
        entry = _make_entry()

        tracker = MagicMock()
        with (
            patch("custom_components.rainpoint.async_unload_entry", new=AsyncMock(return_value=True)) as mu,
            patch("custom_components.rainpoint.async_setup_entry", new=AsyncMock(return_value=True)) as ms,
        ):
            tracker.attach_mock(mu, "unload")
            tracker.attach_mock(ms, "setup")
            await async_reload_entry(hass, entry)

        mu.assert_awaited_once_with(hass, entry)
        ms.assert_awaited_once_with(hass, entry)
        assert [c[0] for c in tracker.mock_calls] == ["unload", "setup"]

    @pytest.mark.asyncio
    async def test_async_reload_entry_skips_reload_on_data_only_change(self):
        """A data-only update (e.g. token persistence) must not reload the entry."""
        from custom_components.rainpoint import async_reload_entry

        hass = _make_hass()
        entry = _make_entry()
        entry.options = {CONF_PUSH_ENABLED: True}
        hass.data = {DOMAIN: {entry.entry_id: {"options_snapshot": {CONF_PUSH_ENABLED: True}}}}

        with (
            patch("custom_components.rainpoint.async_unload_entry", new=AsyncMock()) as mu,
            patch("custom_components.rainpoint.async_setup_entry", new=AsyncMock()) as ms,
        ):
            await async_reload_entry(hass, entry)

        mu.assert_not_awaited()
        ms.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_async_reload_entry_reloads_on_options_change(self):
        """An options change reloads the entry through unload then setup."""
        from custom_components.rainpoint import async_reload_entry

        hass = _make_hass()
        entry = _make_entry()
        entry.options = {CONF_PUSH_ENABLED: True}
        hass.data = {DOMAIN: {entry.entry_id: {"options_snapshot": {CONF_PUSH_ENABLED: False}}}}

        with (
            patch("custom_components.rainpoint.async_unload_entry", new=AsyncMock(return_value=True)) as mu,
            patch("custom_components.rainpoint.async_setup_entry", new=AsyncMock(return_value=True)) as ms,
        ):
            await async_reload_entry(hass, entry)

        mu.assert_awaited_once_with(hass, entry)
        ms.assert_awaited_once_with(hass, entry)


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


class TestRemoveStaleGenericEntities:
    """Cover _remove_stale_generic_entities against a seeded fake registry.

    A mocked registry is used rather than a live Home Assistant one: the
    repository conftest replaces the whole homeassistant package tree with
    stubs before any test module is imported, so the installed live-registry
    fixtures cannot be mixed in without unpicking that, and a seeded fake
    keeps every assertion explicit about exactly which entity ids were
    removed.
    """

    ENTRY_ID = "this_entry"
    OTHER_ENTRY_ID = "other_entry"

    # Realistic {hid}_{mid}_{addr} base slugs, matching the coordinator's own
    # sensor-record keying.
    SLUG_A = "42_100_1"  # never gains a hand-written decoder in these tests
    SLUG_B = "42_100_2"  # the graduation case

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
            self.TRUSTED_UNKNOWN,
            self.RAW_PAYLOAD,
            self.FOREIGN_INTEGRATION,
            self.FOREIGN_ENTRY_GENERIC,
            self.MALFORMED_UNIQUE_ID,
        ]

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

    def _sensors(self, slug_a_model="HCS777ARF", slug_b_model="HCS777ARF", include_slug_a=True):
        """Build a coordinator.data['sensors'] mapping for the two seeded sub-devices."""
        sensors = {self.SLUG_B: {"model": slug_b_model}}
        if include_slug_a:
            sensors[self.SLUG_A] = {"model": slug_a_model}
        return sensors

    def _make_entry_and_coordinator(self, options, sensors):
        entry = MagicMock()
        entry.entry_id = self.ENTRY_ID
        entry.options = options
        coordinator = MagicMock()
        coordinator.data = {"sensors": sensors}
        return entry, coordinator

    def test_toggle_absent_removes_every_generic_row_for_this_entry(self):
        """No CONF_GENERIC_ENTITIES_ENABLED key at all behaves like toggle-off."""
        removed, async_get, async_entries = self._make_fake_registry()
        entry, coordinator = self._make_entry_and_coordinator({}, self._sensors())

        with (
            patch("custom_components.rainpoint.er.async_get", side_effect=async_get),
            patch("custom_components.rainpoint.er.async_entries_for_config_entry", side_effect=async_entries),
        ):
            _remove_stale_generic_entities(MagicMock(), entry, coordinator)

        assert set(removed) == {self.GENERIC_A.entity_id, self.GENERIC_B.entity_id}

    def test_toggle_explicitly_false_removes_every_generic_row_for_this_entry(self):
        """An explicit False behaves identically to the key being absent."""
        removed, async_get, async_entries = self._make_fake_registry()
        entry, coordinator = self._make_entry_and_coordinator({CONF_GENERIC_ENTITIES_ENABLED: False}, self._sensors())

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
            {CONF_GENERIC_ENTITIES_ENABLED: True}, self._sensors(slug_b_model="HCS888ARF-V2")
        )

        with (
            patch("custom_components.rainpoint.er.async_get", side_effect=async_get),
            patch("custom_components.rainpoint.er.async_entries_for_config_entry", side_effect=async_entries),
        ):
            _remove_stale_generic_entities(MagicMock(), entry, coordinator)

        assert removed == []

    def test_toggle_true_graduated_model_removes_only_that_models_row(self):
        """Toggle on, one model now hand-written: only its generic row is removed."""
        removed, async_get, async_entries = self._make_fake_registry()
        entry, coordinator = self._make_entry_and_coordinator(
            {CONF_GENERIC_ENTITIES_ENABLED: True},
            self._sensors(slug_a_model="HCS777ARF", slug_b_model=MODEL_HCS026FRF),
        )

        with (
            patch("custom_components.rainpoint.er.async_get", side_effect=async_get),
            patch("custom_components.rainpoint.er.async_entries_for_config_entry", side_effect=async_entries),
        ):
            _remove_stale_generic_entities(MagicMock(), entry, coordinator)

        assert removed == [self.GENERIC_B.entity_id]

    def test_toggle_true_unresolvable_base_slug_survives(self):
        """A generic row whose base slug is absent from the sensor records is left alone."""
        removed, async_get, async_entries = self._make_fake_registry()
        entry, coordinator = self._make_entry_and_coordinator(
            {CONF_GENERIC_ENTITIES_ENABLED: True},
            self._sensors(slug_b_model=MODEL_HCS026FRF, include_slug_a=False),
        )

        with (
            patch("custom_components.rainpoint.er.async_get", side_effect=async_get),
            patch("custom_components.rainpoint.er.async_entries_for_config_entry", side_effect=async_entries),
        ):
            _remove_stale_generic_entities(MagicMock(), entry, coordinator)

        # Slug B still graduates; slug A's row survives because it resolves
        # to no model at all, which is not evidence of graduation.
        assert removed == [self.GENERIC_B.entity_id]

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

        assert set(first_run_removed) == {self.GENERIC_A.entity_id, self.GENERIC_B.entity_id}
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

        assert removed == [self.GENERIC_B.entity_id]

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


class TestSweepSurvivesUnreadableCoordinatorData(TestRemoveStaleGenericEntities):
    """A raising coordinator.data must not abort the sweep or escape setup.

    That read sits between the guarded registry lookup and the guarded
    per-row removal, and only feeds the graduation check on the toggle-on
    path. Aborting on it would also abandon the toggle-off path, which must
    remove every generic row and needs none of that data to decide.
    """

    def test_toggle_off_still_removes_every_generic_row(self):
        """The removal set is unchanged: toggle-off never consulted the coordinator anyway."""
        removed, async_get, async_entries = self._make_fake_registry()
        entry = MagicMock()
        entry.entry_id = self.ENTRY_ID
        entry.options = {CONF_GENERIC_ENTITIES_ENABLED: False}

        with (
            patch("custom_components.rainpoint.er.async_get", side_effect=async_get),
            patch("custom_components.rainpoint.er.async_entries_for_config_entry", side_effect=async_entries),
        ):
            _remove_stale_generic_entities(MagicMock(), entry, self._make_coordinator_with_raising_data())

        assert set(removed) == {self.GENERIC_A.entity_id, self.GENERIC_B.entity_id}

    def test_toggle_on_removes_nothing_rather_than_raising(self):
        """Without graduation data no model resolves, so no row is evidence of graduation."""
        removed, async_get, async_entries = self._make_fake_registry()
        entry = MagicMock()
        entry.entry_id = self.ENTRY_ID
        entry.options = {CONF_GENERIC_ENTITIES_ENABLED: True}

        with (
            patch("custom_components.rainpoint.er.async_get", side_effect=async_get),
            patch("custom_components.rainpoint.er.async_entries_for_config_entry", side_effect=async_entries),
        ):
            _remove_stale_generic_entities(MagicMock(), entry, self._make_coordinator_with_raising_data())

        assert removed == []
