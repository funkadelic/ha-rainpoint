"""Tests for custom_components.rainpoint.__init__ (integration lifecycle)."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.rainpoint import (
    DOMAIN,
    async_reload_integration,
    async_setup,
    async_setup_entry,
    async_unload_entry,
)


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
