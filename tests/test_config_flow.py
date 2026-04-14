"""Tests for custom_components.rainpoint.config_flow.

The real `homeassistant.config_entries.ConfigFlow` stand-in and
`aiohttp.ClientError` stand-in are installed by `tests/conftest.py` before
any test collection happens, so that subclassing and `except` clauses work
regardless of test collection order.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.rainpoint.api import RainPointApiError
from custom_components.rainpoint.config_flow import RainPointConfigFlow
from custom_components.rainpoint.const import CONF_AREA_CODE, CONF_EMAIL, CONF_HIDS, CONF_PASSWORD

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_flow():
    """Create a RainPointConfigFlow with HA stub methods wired up."""
    flow = RainPointConfigFlow()
    flow.hass = MagicMock()
    flow.hass.config.country = "US"

    # Async HA methods (these don't exist on _FakeConfigFlow so set them directly)
    flow.async_set_unique_id = AsyncMock()
    flow._abort_if_unique_id_configured = MagicMock()
    flow._abort_if_unique_id_mismatch = MagicMock()

    # Sync result methods
    flow.async_show_form = MagicMock(return_value={"type": "form"})
    flow.async_create_entry = MagicMock(return_value={"type": "create_entry"})
    flow.async_update_reload_and_abort = MagicMock(return_value={"type": "abort"})

    return flow


def _make_mock_client(homes=None):
    """Return a mock RainPointClient that succeeds by default."""
    client = MagicMock()
    client.ensure_logged_in = AsyncMock()
    client.list_homes = AsyncMock(
        return_value=homes if homes is not None else [{"hid": 1, "homeName": "My Home"}]
    )
    client.export_tokens = MagicMock(
        return_value={"token": "tok", "refresh_token": "ref", "token_expires_at": 9999999999}
    )
    return client


_VALID_USER_INPUT = {
    CONF_AREA_CODE: "1",
    CONF_EMAIL: "Test@Example.com",
    CONF_PASSWORD: "secret",
}


# ---------------------------------------------------------------------------
# User step tests
# ---------------------------------------------------------------------------

class TestConfigFlowUserStep:
    @pytest.mark.asyncio
    async def test_user_step_no_input_shows_form(self):
        flow = _make_flow()
        await flow.async_step_user(None)
        flow.async_show_form.assert_called_once()
        call_kwargs = flow.async_show_form.call_args.kwargs
        assert call_kwargs["step_id"] == "user"

    @pytest.mark.asyncio
    async def test_user_step_success_proceeds_to_home_selection(self):
        flow = _make_flow()
        mock_client = _make_mock_client()

        with patch(
            "custom_components.rainpoint.config_flow.async_get_clientsession",
            return_value=MagicMock(),
        ), patch(
            "custom_components.rainpoint.config_flow.RainPointClient",
            return_value=mock_client,
        ):
            # async_step_select_homes is called internally; stub it
            flow.async_step_select_homes = AsyncMock(return_value={"type": "form"})
            await flow.async_step_user(_VALID_USER_INPUT)

        assert flow._homes == [{"hid": 1, "homeName": "My Home"}]
        # Email must be normalised to lowercase + stripped
        assert flow._email == "test@example.com"

    @pytest.mark.asyncio
    async def test_user_step_auth_error(self):
        flow = _make_flow()
        mock_client = _make_mock_client()
        mock_client.ensure_logged_in = AsyncMock(side_effect=RainPointApiError("bad creds"))

        with patch(
            "custom_components.rainpoint.config_flow.async_get_clientsession",
            return_value=MagicMock(),
        ), patch(
            "custom_components.rainpoint.config_flow.RainPointClient",
            return_value=mock_client,
        ):
            await flow.async_step_user(_VALID_USER_INPUT)

        flow.async_show_form.assert_called_once()
        errors = flow.async_show_form.call_args.kwargs.get("errors", {})
        assert errors.get("base") == "auth_failed"

    @pytest.mark.asyncio
    async def test_user_step_network_error(self):
        flow = _make_flow()
        mock_client = _make_mock_client()
        mock_client.ensure_logged_in = AsyncMock(side_effect=TimeoutError())

        with patch(
            "custom_components.rainpoint.config_flow.async_get_clientsession",
            return_value=MagicMock(),
        ), patch(
            "custom_components.rainpoint.config_flow.RainPointClient",
            return_value=mock_client,
        ):
            await flow.async_step_user(_VALID_USER_INPUT)

        flow.async_show_form.assert_called_once()
        errors = flow.async_show_form.call_args.kwargs.get("errors", {})
        assert errors.get("base") == "cannot_connect"

    @pytest.mark.asyncio
    async def test_user_step_no_homes(self):
        flow = _make_flow()
        mock_client = _make_mock_client(homes=[])

        with patch(
            "custom_components.rainpoint.config_flow.async_get_clientsession",
            return_value=MagicMock(),
        ), patch(
            "custom_components.rainpoint.config_flow.RainPointClient",
            return_value=mock_client,
        ):
            await flow.async_step_user(_VALID_USER_INPUT)

        flow.async_show_form.assert_called_once()
        errors = flow.async_show_form.call_args.kwargs.get("errors", {})
        assert errors.get("base") == "no_homes"


# ---------------------------------------------------------------------------
# Select homes step tests
# ---------------------------------------------------------------------------

class TestConfigFlowSelectHomes:
    @pytest.mark.asyncio
    async def test_select_homes_no_input_shows_form(self):
        flow = _make_flow()
        flow._homes = [{"hid": 1, "homeName": "Home1"}]
        flow._reconfigure = False

        await flow.async_step_select_homes(None)

        flow.async_show_form.assert_called_once()
        call_kwargs = flow.async_show_form.call_args.kwargs
        assert call_kwargs["step_id"] == "select_homes"

    @pytest.mark.asyncio
    async def test_select_homes_creates_entry(self):
        flow = _make_flow()
        flow._homes = [{"hid": 1, "homeName": "Home1"}]
        flow._area_code = "1"
        flow._email = "test@example.com"
        flow._password = "secret"
        flow._client = _make_mock_client()
        flow._reconfigure = False

        await flow.async_step_select_homes({CONF_HIDS: "1"})

        flow.async_create_entry.assert_called_once()
        call_kwargs = flow.async_create_entry.call_args.kwargs
        assert "RainPoint" in call_kwargs.get("title", "")

    @pytest.mark.asyncio
    async def test_select_homes_no_selection_shows_error(self):
        flow = _make_flow()
        flow._homes = [{"hid": 1, "homeName": "Home1"}]
        flow._reconfigure = False

        await flow.async_step_select_homes({CONF_HIDS: ""})

        flow.async_show_form.assert_called_once()
        errors = flow.async_show_form.call_args.kwargs.get("errors", {})
        assert errors.get("base") == "select_at_least_one"

    @pytest.mark.asyncio
    async def test_select_homes_none_selection_shows_error(self):
        flow = _make_flow()
        flow._homes = [{"hid": 1, "homeName": "Home1"}]
        flow._reconfigure = False

        await flow.async_step_select_homes({CONF_HIDS: None})

        flow.async_show_form.assert_called_once()
        errors = flow.async_show_form.call_args.kwargs.get("errors", {})
        assert errors.get("base") == "select_at_least_one"


# ---------------------------------------------------------------------------
# Reconfigure step tests
# ---------------------------------------------------------------------------

class TestConfigFlowReconfigure:
    def _make_reconfigure_flow(self):
        """Create flow with reconfigure entry pre-wired."""
        flow = _make_flow()
        flow._reconfigure = True

        mock_entry = MagicMock()
        mock_entry.data = {
            CONF_AREA_CODE: "1",
            CONF_EMAIL: "existing@example.com",
            CONF_PASSWORD: "oldpass",
        }
        flow._get_reconfigure_entry = MagicMock(return_value=mock_entry)
        return flow

    @pytest.mark.asyncio
    async def test_reconfigure_no_input_shows_form(self):
        flow = self._make_reconfigure_flow()

        await flow.async_step_reconfigure(None)

        flow.async_show_form.assert_called_once()
        call_kwargs = flow.async_show_form.call_args.kwargs
        assert call_kwargs["step_id"] == "reconfigure"

    @pytest.mark.asyncio
    async def test_reconfigure_success_proceeds_to_home_selection(self):
        flow = self._make_reconfigure_flow()
        mock_client = _make_mock_client()

        with patch(
            "custom_components.rainpoint.config_flow.async_get_clientsession",
            return_value=MagicMock(),
        ), patch(
            "custom_components.rainpoint.config_flow.RainPointClient",
            return_value=mock_client,
        ):
            flow.async_step_select_homes_reconfigure = AsyncMock(return_value={"type": "form"})
            await flow.async_step_reconfigure(
                {CONF_AREA_CODE: "1", CONF_EMAIL: "new@example.com", CONF_PASSWORD: "newpass"}
            )

        # Homes should be populated after successful login
        assert flow._homes == [{"hid": 1, "homeName": "My Home"}]

    @pytest.mark.asyncio
    async def test_reconfigure_auth_error(self):
        flow = self._make_reconfigure_flow()
        mock_client = _make_mock_client()
        mock_client.ensure_logged_in = AsyncMock(side_effect=RainPointApiError("bad"))

        with patch(
            "custom_components.rainpoint.config_flow.async_get_clientsession",
            return_value=MagicMock(),
        ), patch(
            "custom_components.rainpoint.config_flow.RainPointClient",
            return_value=mock_client,
        ):
            await flow.async_step_reconfigure(
                {CONF_AREA_CODE: "1", CONF_EMAIL: "new@example.com", CONF_PASSWORD: "wrong"}
            )

        flow.async_show_form.assert_called()
        last_call = flow.async_show_form.call_args.kwargs
        assert last_call.get("errors", {}).get("base") == "auth_failed"

    @pytest.mark.asyncio
    async def test_reconfigure_network_error(self):
        flow = self._make_reconfigure_flow()
        mock_client = _make_mock_client()
        mock_client.ensure_logged_in = AsyncMock(side_effect=TimeoutError())

        with patch(
            "custom_components.rainpoint.config_flow.async_get_clientsession",
            return_value=MagicMock(),
        ), patch(
            "custom_components.rainpoint.config_flow.RainPointClient",
            return_value=mock_client,
        ):
            await flow.async_step_reconfigure(
                {CONF_AREA_CODE: "1", CONF_EMAIL: "new@example.com", CONF_PASSWORD: "pass"}
            )

        flow.async_show_form.assert_called()
        last_call = flow.async_show_form.call_args.kwargs
        assert last_call.get("errors", {}).get("base") == "cannot_connect"
