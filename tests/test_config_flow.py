"""Tests for custom_components.rainpoint.config_flow.

The real `homeassistant.config_entries.ConfigFlow` stand-in and
`aiohttp.ClientError` stand-in are installed by `tests/conftest.py` before
any test collection happens, so that subclassing and `except` clauses work
regardless of test collection order.
"""

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.rainpoint.api import RainPointApiError, RainPointThrottledError
from custom_components.rainpoint.config_flow import RainPointConfigFlow, RainPointOptionsFlow
from custom_components.rainpoint.const import (
    CONF_AREA_CODE,
    CONF_COUNTRY,
    CONF_EMAIL,
    CONF_GENERIC_CONTROL_ACKED_KEYS,
    CONF_GENERIC_CONTROL_ENABLED,
    CONF_GENERIC_ENTITIES_ENABLED,
    CONF_HIDS,
    CONF_PASSWORD,
    CONF_PUSH_ENABLED,
    DOMAIN,
)

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
    client.list_homes = AsyncMock(return_value=homes if homes is not None else [{"hid": 1, "homeName": "My Home"}])
    client.export_tokens = MagicMock(return_value={"token": "tok", "refresh_token": "ref", "token_expires_at": 9999999999})
    return client


_VALID_USER_INPUT = {
    CONF_COUNTRY: "US",
    CONF_EMAIL: "Test@Example.com",
    CONF_PASSWORD: "secret",
}


# ---------------------------------------------------------------------------
# User step tests
# ---------------------------------------------------------------------------


class TestConfigEntryVersion:
    """The version boundary the hub identity re-key runs at."""

    def test_config_flow_version_is_two(self):
        """Home Assistant runs async_migrate_entry only when this is ahead of
        the stored entry version, so lowering it back would silently skip the
        re-key on every install that has not yet migrated."""
        assert RainPointConfigFlow.VERSION == 2


class TestConfigFlowUserStep:
    """Tests for ConfigFlowUserStep."""

    @pytest.mark.asyncio
    async def test_user_step_no_input_shows_form(self):
        """User step no input shows form."""
        flow = _make_flow()
        await flow.async_step_user(None)
        flow.async_show_form.assert_called_once()
        call_kwargs = flow.async_show_form.call_args.kwargs
        assert call_kwargs["step_id"] == "user"

    @pytest.mark.asyncio
    async def test_user_step_success_proceeds_to_home_selection(self):
        """User step success proceeds to home selection."""
        flow = _make_flow()
        mock_client = _make_mock_client()

        with (
            patch(
                "custom_components.rainpoint.config_flow.async_get_clientsession",
                return_value=MagicMock(),
            ),
            patch(
                "custom_components.rainpoint.config_flow.RainPointClient",
                return_value=mock_client,
            ),
        ):
            # async_step_select_homes is called internally; stub it
            flow.async_step_select_homes = AsyncMock(return_value={"type": "form"})
            await flow.async_step_user(_VALID_USER_INPUT)

        assert flow._homes == [{"hid": 1, "homeName": "My Home"}]
        # Email must be normalised to lowercase + stripped
        assert flow._email == "test@example.com"

    @pytest.mark.asyncio
    async def test_user_step_auth_error(self):
        """User step auth error."""
        flow = _make_flow()
        mock_client = _make_mock_client()
        mock_client.ensure_logged_in = AsyncMock(side_effect=RainPointApiError("bad creds"))

        with (
            patch(
                "custom_components.rainpoint.config_flow.async_get_clientsession",
                return_value=MagicMock(),
            ),
            patch(
                "custom_components.rainpoint.config_flow.RainPointClient",
                return_value=mock_client,
            ),
        ):
            await flow.async_step_user(_VALID_USER_INPUT)

        flow.async_show_form.assert_called_once()
        errors = flow.async_show_form.call_args.kwargs.get("errors", {})
        assert errors.get("base") == "auth_failed"

    @pytest.mark.asyncio
    async def test_user_step_throttled_maps_to_rate_limited(self):
        """A throttle during setup surfaces rate_limited, not auth_failed."""
        flow = _make_flow()
        mock_client = _make_mock_client()
        mock_client.ensure_logged_in = AsyncMock(side_effect=RainPointThrottledError("cooling down 120s", 120))

        with (
            patch(
                "custom_components.rainpoint.config_flow.async_get_clientsession",
                return_value=MagicMock(),
            ),
            patch(
                "custom_components.rainpoint.config_flow.RainPointClient",
                return_value=mock_client,
            ),
        ):
            await flow.async_step_user(_VALID_USER_INPUT)

        flow.async_show_form.assert_called_once()
        errors = flow.async_show_form.call_args.kwargs.get("errors", {})
        assert errors.get("base") == "rate_limited"

    @pytest.mark.asyncio
    async def test_user_step_network_error(self):
        """User step network error."""
        flow = _make_flow()
        mock_client = _make_mock_client()
        mock_client.ensure_logged_in = AsyncMock(side_effect=TimeoutError())

        with (
            patch(
                "custom_components.rainpoint.config_flow.async_get_clientsession",
                return_value=MagicMock(),
            ),
            patch(
                "custom_components.rainpoint.config_flow.RainPointClient",
                return_value=mock_client,
            ),
        ):
            await flow.async_step_user(_VALID_USER_INPUT)

        flow.async_show_form.assert_called_once()
        errors = flow.async_show_form.call_args.kwargs.get("errors", {})
        assert errors.get("base") == "cannot_connect"

    @pytest.mark.asyncio
    async def test_user_step_no_homes(self):
        """User step no homes."""
        flow = _make_flow()
        mock_client = _make_mock_client(homes=[])

        with (
            patch(
                "custom_components.rainpoint.config_flow.async_get_clientsession",
                return_value=MagicMock(),
            ),
            patch(
                "custom_components.rainpoint.config_flow.RainPointClient",
                return_value=mock_client,
            ),
        ):
            await flow.async_step_user(_VALID_USER_INPUT)

        flow.async_show_form.assert_called_once()
        errors = flow.async_show_form.call_args.kwargs.get("errors", {})
        assert errors.get("base") == "no_homes"


# ---------------------------------------------------------------------------
# Select homes step tests
# ---------------------------------------------------------------------------


class TestConfigFlowSelectHomes:
    """Tests for ConfigFlowSelectHomes."""

    @pytest.mark.asyncio
    async def test_select_homes_no_input_shows_form(self):
        """Select homes no input shows form."""
        flow = _make_flow()
        flow._homes = [{"hid": 1, "homeName": "Home1"}]
        flow._reconfigure = False

        await flow.async_step_select_homes(None)

        flow.async_show_form.assert_called_once()
        call_kwargs = flow.async_show_form.call_args.kwargs
        assert call_kwargs["step_id"] == "select_homes"

    @pytest.mark.asyncio
    async def test_select_homes_creates_entry(self):
        """Select homes creates entry."""
        flow = _make_flow()
        flow._homes = [{"hid": 1, "homeName": "Home1"}]
        flow._country = "US"
        flow._area_code = "1"
        flow._email = "test@example.com"
        flow._password = "secret"
        flow._client = _make_mock_client()
        flow._reconfigure = False

        await flow.async_step_select_homes({CONF_HIDS: "1"})

        flow.async_create_entry.assert_called_once()
        call_kwargs = flow.async_create_entry.call_args.kwargs
        assert "RainPoint" in call_kwargs.get("title", "")
        entry_data = call_kwargs.get("data", {})
        assert entry_data.get(CONF_COUNTRY) == "US"
        assert entry_data.get(CONF_AREA_CODE) == "1"

    @pytest.mark.asyncio
    async def test_select_homes_no_selection_shows_error(self):
        """Select homes no selection shows error."""
        flow = _make_flow()
        flow._homes = [{"hid": 1, "homeName": "Home1"}]
        flow._reconfigure = False

        await flow.async_step_select_homes({CONF_HIDS: ""})

        flow.async_show_form.assert_called_once()
        errors = flow.async_show_form.call_args.kwargs.get("errors", {})
        assert errors.get("base") == "select_at_least_one"

    @pytest.mark.asyncio
    async def test_select_homes_none_selection_shows_error(self):
        """Select homes none selection shows error."""
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
    """Tests for ConfigFlowReconfigure."""

    def _make_reconfigure_flow(self):
        """Create flow with reconfigure entry pre-wired."""
        flow = _make_flow()
        flow._reconfigure = True

        mock_entry = MagicMock()
        mock_entry.data = {
            CONF_COUNTRY: "US",
            CONF_AREA_CODE: "1",
            CONF_EMAIL: "existing@example.com",
            CONF_PASSWORD: "oldpass",
        }
        flow._get_reconfigure_entry = MagicMock(return_value=mock_entry)
        return flow

    @pytest.mark.asyncio
    async def test_reconfigure_no_input_shows_form(self):
        """Reconfigure no input shows form."""
        flow = self._make_reconfigure_flow()

        await flow.async_step_reconfigure(None)

        flow.async_show_form.assert_called_once()
        call_kwargs = flow.async_show_form.call_args.kwargs
        assert call_kwargs["step_id"] == "reconfigure"

    @pytest.mark.asyncio
    async def test_reconfigure_success_proceeds_to_home_selection(self):
        """Reconfigure success proceeds to home selection."""
        flow = self._make_reconfigure_flow()
        mock_client = _make_mock_client()

        with (
            patch(
                "custom_components.rainpoint.config_flow.async_get_clientsession",
                return_value=MagicMock(),
            ),
            patch(
                "custom_components.rainpoint.config_flow.RainPointClient",
                return_value=mock_client,
            ),
        ):
            flow.async_step_select_homes_reconfigure = AsyncMock(return_value={"type": "form"})
            await flow.async_step_reconfigure({CONF_COUNTRY: "US", CONF_EMAIL: "New@Example.com", CONF_PASSWORD: "newpass"})

        # Homes should be populated after successful login
        assert flow._homes == [{"hid": 1, "homeName": "My Home"}]
        # Email must be normalised (lowercased + stripped)
        assert flow._email == "new@example.com"
        # Unique ID must be set (and awaited) using the normalised email
        flow.async_set_unique_id.assert_awaited_once_with("rainpoint_new@example.com")

    @pytest.mark.asyncio
    async def test_reconfigure_auth_error(self):
        """Reconfigure auth error."""
        flow = self._make_reconfigure_flow()
        mock_client = _make_mock_client()
        mock_client.ensure_logged_in = AsyncMock(side_effect=RainPointApiError("bad"))

        with (
            patch(
                "custom_components.rainpoint.config_flow.async_get_clientsession",
                return_value=MagicMock(),
            ),
            patch(
                "custom_components.rainpoint.config_flow.RainPointClient",
                return_value=mock_client,
            ),
        ):
            await flow.async_step_reconfigure({CONF_COUNTRY: "US", CONF_EMAIL: "new@example.com", CONF_PASSWORD: "wrong"})

        flow.async_show_form.assert_called_once()
        last_call = flow.async_show_form.call_args.kwargs
        assert last_call.get("errors", {}).get("base") == "auth_failed"

    @pytest.mark.asyncio
    async def test_reconfigure_throttled_maps_to_rate_limited(self):
        """A throttle during reconfigure surfaces rate_limited, not auth_failed."""
        flow = self._make_reconfigure_flow()
        mock_client = _make_mock_client()
        mock_client.ensure_logged_in = AsyncMock(side_effect=RainPointThrottledError("cooling down 120s", 120))

        with (
            patch(
                "custom_components.rainpoint.config_flow.async_get_clientsession",
                return_value=MagicMock(),
            ),
            patch(
                "custom_components.rainpoint.config_flow.RainPointClient",
                return_value=mock_client,
            ),
        ):
            await flow.async_step_reconfigure({CONF_COUNTRY: "US", CONF_EMAIL: "new@example.com", CONF_PASSWORD: "wrong"})

        flow.async_show_form.assert_called_once()
        last_call = flow.async_show_form.call_args.kwargs
        assert last_call.get("errors", {}).get("base") == "rate_limited"

    @pytest.mark.asyncio
    async def test_reconfigure_network_error(self):
        """Reconfigure network error."""
        flow = self._make_reconfigure_flow()
        mock_client = _make_mock_client()
        mock_client.ensure_logged_in = AsyncMock(side_effect=TimeoutError())

        with (
            patch(
                "custom_components.rainpoint.config_flow.async_get_clientsession",
                return_value=MagicMock(),
            ),
            patch(
                "custom_components.rainpoint.config_flow.RainPointClient",
                return_value=mock_client,
            ),
        ):
            await flow.async_step_reconfigure({CONF_COUNTRY: "US", CONF_EMAIL: "new@example.com", CONF_PASSWORD: "pass"})

        flow.async_show_form.assert_called_once()
        last_call = flow.async_show_form.call_args.kwargs
        assert last_call.get("errors", {}).get("base") == "cannot_connect"

    @pytest.mark.asyncio
    async def test_reconfigure_legacy_entry_defaults_to_ha_country_on_matching_dial_code(self):
        """Legacy entry (CONF_AREA_CODE only, no CONF_COUNTRY) resolves the
        dropdown default to HA's configured ISO when its dial code matches,
        so e.g. a US user doesn't silently flip to CA on the first reconfigure.
        """
        flow = _make_flow()
        flow.hass.config.country = "US"
        flow._reconfigure = True

        mock_entry = MagicMock()
        mock_entry.data = {
            CONF_AREA_CODE: "1",
            CONF_EMAIL: "existing@example.com",
            CONF_PASSWORD: "oldpass",
        }
        flow._get_reconfigure_entry = MagicMock(return_value=mock_entry)

        await flow.async_step_reconfigure(None)

        schema = flow.async_show_form.call_args.kwargs["data_schema"]
        country_default = None
        for key in schema.schema:
            if getattr(key, "schema", None) == CONF_COUNTRY:
                raw = key.default
                country_default = raw() if callable(raw) else raw
                break

        assert country_default == "US"

    @pytest.mark.asyncio
    async def test_reconfigure_preserves_stored_country_on_shared_dial_code(self):
        """An entry with a stored CONF_COUNTRY that shares a dial code with others
        (e.g. Canada on +1) re-selects that exact country on reconfigure, not the
        fallback (US). The stored ISO wins over the dial-code resolution path.
        """
        flow = _make_flow()
        flow.hass.config.country = "US"  # HA is US; the entry is Canada
        flow._reconfigure = True

        mock_entry = MagicMock()
        mock_entry.data = {
            CONF_COUNTRY: "CA",
            CONF_AREA_CODE: "1",
            CONF_EMAIL: "existing@example.com",
            CONF_PASSWORD: "oldpass",
        }
        flow._get_reconfigure_entry = MagicMock(return_value=mock_entry)

        await flow.async_step_reconfigure(None)

        schema = flow.async_show_form.call_args.kwargs["data_schema"]
        country_default = None
        for key in schema.schema:
            if getattr(key, "schema", None) == CONF_COUNTRY:
                raw = key.default
                country_default = raw() if callable(raw) else raw
                break

        assert country_default == "CA"

    @pytest.mark.asyncio
    async def test_reconfigure_no_homes_shows_error(self):
        """Reconfigure with empty homes list surfaces a no_homes error on the form."""
        flow = self._make_reconfigure_flow()
        mock_client = _make_mock_client(homes=[])

        with (
            patch(
                "custom_components.rainpoint.config_flow.async_get_clientsession",
                return_value=MagicMock(),
            ),
            patch(
                "custom_components.rainpoint.config_flow.RainPointClient",
                return_value=mock_client,
            ),
        ):
            await flow.async_step_reconfigure({CONF_COUNTRY: "US", CONF_EMAIL: "new@example.com", CONF_PASSWORD: "pass"})

        flow.async_show_form.assert_called_once()
        last_call = flow.async_show_form.call_args.kwargs
        assert last_call.get("step_id") == "reconfigure"
        assert last_call.get("errors", {}).get("base") == "no_homes"


# ---------------------------------------------------------------------------
# Select homes reconfigure step tests
# ---------------------------------------------------------------------------


class TestConfigFlowSelectHomesReconfigure:
    """Tests for the reconfigure variant of the home-selection step."""

    def _make_flow_with_reconfigure_context(self):
        """Return a flow wired for async_step_select_homes_reconfigure."""
        flow = _make_flow()
        flow._reconfigure = True
        flow._homes = [{"hid": 1, "homeName": "Home A"}]

        mock_entry = MagicMock()
        mock_entry.data = {CONF_HIDS: [1]}
        flow._get_reconfigure_entry = MagicMock(return_value=mock_entry)
        return flow

    @pytest.mark.asyncio
    async def test_select_homes_reconfigure_no_input_shows_form(self):
        """No user_input should render the select_homes_reconfigure form."""
        flow = self._make_flow_with_reconfigure_context()

        await flow.async_step_select_homes_reconfigure(user_input=None)

        flow.async_show_form.assert_called_once()
        call_kwargs = flow.async_show_form.call_args.kwargs
        assert call_kwargs["step_id"] == "select_homes_reconfigure"

    @pytest.mark.asyncio
    async def test_select_homes_reconfigure_updates_entry(self):
        """Submitting a selection drives async_update_reload_and_abort with the new data."""
        flow = self._make_flow_with_reconfigure_context()
        flow._country = "US"
        flow._area_code = "1"
        flow._email = "test@example.com"
        flow._password = "pw"
        flow._client = MagicMock()
        flow._client.export_tokens = MagicMock(return_value={"token": "T"})
        flow.async_update_reload_and_abort = MagicMock(return_value={"type": "abort", "reason": "reconfigure_successful"})

        await flow.async_step_select_homes_reconfigure(user_input={CONF_HIDS: "1"})

        flow.async_update_reload_and_abort.assert_called_once()
        call_kwargs = flow.async_update_reload_and_abort.call_args.kwargs
        assert call_kwargs["title"] == "RainPoint (test@example.com)"
        assert call_kwargs["data"][CONF_HIDS] == [1]
        assert call_kwargs["data"][CONF_EMAIL] == "test@example.com"
        assert call_kwargs["data"]["token"] == "T"

    @pytest.mark.asyncio
    async def test_select_homes_reconfigure_no_selection_shows_error(self):
        """Empty CONF_HIDS selection surfaces select_at_least_one on the form."""
        flow = self._make_flow_with_reconfigure_context()

        await flow.async_step_select_homes_reconfigure(user_input={CONF_HIDS: None})

        flow.async_show_form.assert_called_once()
        errors = flow.async_show_form.call_args.kwargs.get("errors", {})
        assert errors.get("base") == "select_at_least_one"


# ---------------------------------------------------------------------------
# Options flow tests
# ---------------------------------------------------------------------------


def _make_options_flow(
    current_push_enabled: bool = False,
    current_generic_enabled: bool = False,
    current_control_enabled: bool = False,
    sensors: dict | None = None,
    entry_loaded: bool = True,
) -> RainPointOptionsFlow:
    """Create a RainPointOptionsFlow with a fake config_entry and HA stub methods wired up.

    ``sensors`` seeds the coordinator data the eligibility count reads; ``entry_loaded``
    False models the entry being absent from hass.data when the form is opened.
    """
    flow = RainPointOptionsFlow()
    flow.config_entry = MagicMock()
    flow.config_entry.entry_id = "test_entry"
    flow.config_entry.options = {
        CONF_PUSH_ENABLED: current_push_enabled,
        CONF_GENERIC_ENTITIES_ENABLED: current_generic_enabled,
        CONF_GENERIC_CONTROL_ENABLED: current_control_enabled,
    }
    flow.hass = MagicMock()
    if entry_loaded:
        coordinator = MagicMock()
        coordinator.data = {"sensors": sensors or {}}
        flow.hass.data = {DOMAIN: {"test_entry": {"coordinator": coordinator}}}
    else:
        flow.hass.data = {}
    flow.async_show_form = MagicMock(return_value={"type": "form"})
    flow.async_create_entry = MagicMock(return_value={"type": "create_entry"})
    return flow


class TestAsyncGetOptionsFlow:
    """RainPointConfigFlow.async_get_options_flow returns a RainPointOptionsFlow."""

    def test_returns_options_flow_instance(self):
        """async_get_options_flow returns a RainPointOptionsFlow instance."""
        result = RainPointConfigFlow.async_get_options_flow(MagicMock())

        assert isinstance(result, RainPointOptionsFlow)


class TestOptionsFlowInitStep:
    """RainPointOptionsFlow.async_step_init shows/handles the two-toggle form."""

    @pytest.mark.asyncio
    async def test_no_input_shows_form_with_current_default(self):
        """No input shows the form, defaulted from the current entry.options value."""
        flow = _make_options_flow(current_push_enabled=True)

        await flow.async_step_init(None)

        flow.async_show_form.assert_called_once()
        call_kwargs = flow.async_show_form.call_args.kwargs
        assert call_kwargs["step_id"] == "init"
        schema = call_kwargs["data_schema"]
        # The vol.Required marker's default is a callable; invoke it to read the value.
        (marker,) = [k for k in schema.schema if k == CONF_PUSH_ENABLED]
        assert marker.default() is True

    @pytest.mark.asyncio
    async def test_no_input_shows_form_defaulting_true_when_unset(self):
        """Form pre-checks push when entry.options has never stored the key."""
        flow = _make_options_flow(current_push_enabled=False)
        flow.config_entry.options = {}

        await flow.async_step_init(None)

        call_kwargs = flow.async_show_form.call_args.kwargs
        schema = call_kwargs["data_schema"]
        (marker,) = [k for k in schema.schema if k == CONF_PUSH_ENABLED]
        assert marker.default() is True

    @pytest.mark.asyncio
    async def test_stored_false_still_defaults_the_form_off(self):
        """A stored opt-out is what the form shows the user back.

        That is the whole reason no migration is written when push flips on
        by default: an entry holding an explicit false keeps it.
        """
        flow = _make_options_flow(current_push_enabled=False)

        await flow.async_step_init(None)

        call_kwargs = flow.async_show_form.call_args.kwargs
        schema = call_kwargs["data_schema"]
        (marker,) = [k for k in schema.schema if k == CONF_PUSH_ENABLED]
        assert marker.default() is False

    @pytest.mark.asyncio
    async def test_submitting_true_writes_to_entry_options(self):
        """Submitting {push_enabled: True} produces a create-entry result with that data."""
        flow = _make_options_flow(current_push_enabled=False)

        result = await flow.async_step_init({CONF_PUSH_ENABLED: True})

        flow.async_create_entry.assert_called_once_with(title="", data={CONF_PUSH_ENABLED: True})
        assert result == {"type": "create_entry"}

    @pytest.mark.asyncio
    async def test_schema_exposes_both_toggle_keys_in_one_step(self):
        """The single form schema carries both the push and generic-entities keys."""
        flow = _make_options_flow()

        await flow.async_step_init(None)

        call_kwargs = flow.async_show_form.call_args.kwargs
        assert call_kwargs["step_id"] == "init"
        schema = call_kwargs["data_schema"]
        keys = set(schema.schema)
        assert CONF_PUSH_ENABLED in keys
        assert CONF_GENERIC_ENTITIES_ENABLED in keys

    @pytest.mark.asyncio
    async def test_generic_toggle_defaults_false_when_absent(self):
        """The generic-entities marker defaults to False when entry.options is empty."""
        flow = _make_options_flow()
        flow.config_entry.options = {}

        await flow.async_step_init(None)

        schema = flow.async_show_form.call_args.kwargs["data_schema"]
        (marker,) = [k for k in schema.schema if k == CONF_GENERIC_ENTITIES_ENABLED]
        assert marker.default() is False

    @pytest.mark.asyncio
    async def test_generic_toggle_defaults_to_stored_value(self):
        """The generic-entities marker defaults to the stored entry.options value."""
        flow = _make_options_flow(current_generic_enabled=True)

        await flow.async_step_init(None)

        schema = flow.async_show_form.call_args.kwargs["data_schema"]
        (marker,) = [k for k in schema.schema if k == CONF_GENERIC_ENTITIES_ENABLED]
        assert marker.default() is True

    @pytest.mark.asyncio
    async def test_submitting_both_booleans_writes_both_to_entry_options(self):
        """Submitting both keys reaches async_create_entry carrying both."""
        flow = _make_options_flow()

        payload = {CONF_PUSH_ENABLED: True, CONF_GENERIC_ENTITIES_ENABLED: True}
        result = await flow.async_step_init(payload)

        flow.async_create_entry.assert_called_once_with(title="", data=payload)
        assert result == {"type": "create_entry"}

    @pytest.mark.asyncio
    async def test_schema_exposes_the_control_toggle_key(self):
        """The single form schema also carries the third, control-toggle key."""
        flow = _make_options_flow()

        await flow.async_step_init(None)

        schema = flow.async_show_form.call_args.kwargs["data_schema"]
        assert CONF_GENERIC_CONTROL_ENABLED in set(schema.schema)

    @pytest.mark.asyncio
    async def test_control_toggle_defaults_false_when_absent(self):
        """The control-toggle marker defaults to False when entry.options is empty."""
        flow = _make_options_flow()
        flow.config_entry.options = {}

        await flow.async_step_init(None)

        schema = flow.async_show_form.call_args.kwargs["data_schema"]
        (marker,) = [k for k in schema.schema if k == CONF_GENERIC_CONTROL_ENABLED]
        assert marker.default() is False

    @pytest.mark.asyncio
    async def test_control_toggle_defaults_to_stored_value(self):
        """The control-toggle marker defaults to the stored entry.options value."""
        flow = _make_options_flow(current_control_enabled=True)

        await flow.async_step_init(None)

        schema = flow.async_show_form.call_args.kwargs["data_schema"]
        (marker,) = [k for k in schema.schema if k == CONF_GENERIC_CONTROL_ENABLED]
        assert marker.default() is True

    @pytest.mark.asyncio
    async def test_submitting_all_three_booleans_writes_all_three_to_entry_options(self):
        """Submitting all three keys reaches async_create_entry carrying all three."""
        flow = _make_options_flow()

        payload = {
            CONF_PUSH_ENABLED: True,
            CONF_GENERIC_ENTITIES_ENABLED: True,
            CONF_GENERIC_CONTROL_ENABLED: True,
        }
        result = await flow.async_step_init(payload)

        flow.async_create_entry.assert_called_once_with(title="", data={**payload, CONF_GENERIC_CONTROL_ACKED_KEYS: []})
        assert result == {"type": "create_entry"}


def _control_eligible_sensors() -> dict:
    """One sub-device the control gate admits, keyed the way the coordinator keys them."""
    return {
        "100_200_1": {
            "model": "HTV103FRF",
            "model_code": 31,
            "data": {"type": "unknown", "model": "HTV103FRF"},
        }
    }


def _control_ineligible_sensors() -> dict:
    """Unsupported, but the control gate admits none of its datapoints."""
    return {
        "100_200_9": {
            "model": "HCS003ARF-V1",
            "model_code": None,
            "data": {"type": "unknown", "model": "HCS003ARF-V1"},
        }
    }


class TestOptionsFlowControlConsentStamp:
    """Saving the control toggle on records which devices that save covered.

    The entity registry cannot serve as this baseline: __init__ deletes every
    control-namespace row for the entry while the toggle is off, so an
    off-and-on-again would read as a fleet of brand-new devices.
    """

    @pytest.mark.asyncio
    async def test_saving_control_on_stamps_the_eligible_keys(self):
        flow = _make_options_flow(sensors=_control_eligible_sensors())

        await flow.async_step_init(
            {CONF_PUSH_ENABLED: True, CONF_GENERIC_ENTITIES_ENABLED: False, CONF_GENERIC_CONTROL_ENABLED: True}
        )

        saved = flow.async_create_entry.call_args.kwargs["data"]
        assert saved[CONF_GENERIC_CONTROL_ACKED_KEYS] == ["100_200_1"]

    @pytest.mark.asyncio
    async def test_a_device_the_control_gate_refuses_is_not_stamped(self):
        """Stamping it would silently consent to controls it does not have yet,
        so a later catalog refresh that admits it would appear unannounced."""
        flow = _make_options_flow(sensors=_control_ineligible_sensors())

        await flow.async_step_init(
            {CONF_PUSH_ENABLED: True, CONF_GENERIC_ENTITIES_ENABLED: False, CONF_GENERIC_CONTROL_ENABLED: True}
        )

        assert flow.async_create_entry.call_args.kwargs["data"][CONF_GENERIC_CONTROL_ACKED_KEYS] == []

    @pytest.mark.asyncio
    async def test_saving_control_off_drops_the_stamp(self):
        """So a later re-enable consents afresh rather than against a stale list."""
        flow = _make_options_flow(current_control_enabled=True, sensors=_control_eligible_sensors())

        await flow.async_step_init(
            {CONF_PUSH_ENABLED: True, CONF_GENERIC_ENTITIES_ENABLED: False, CONF_GENERIC_CONTROL_ENABLED: False}
        )

        assert CONF_GENERIC_CONTROL_ACKED_KEYS not in flow.async_create_entry.call_args.kwargs["data"]

    @pytest.mark.asyncio
    async def test_an_unloaded_entry_stamps_nothing_rather_than_everything(self):
        """The conservative direction: announce the first device that appears,
        rather than silently consenting to devices this form never saw."""
        flow = _make_options_flow(entry_loaded=False)

        await flow.async_step_init(
            {CONF_PUSH_ENABLED: True, CONF_GENERIC_ENTITIES_ENABLED: False, CONF_GENERIC_CONTROL_ENABLED: True}
        )

        assert flow.async_create_entry.call_args.kwargs["data"][CONF_GENERIC_CONTROL_ACKED_KEYS] == []

    @pytest.mark.asyncio
    async def test_a_decoded_device_is_not_stamped(self):
        """Only devices the trusted decoders could not read reach the generic path."""
        sensors = _control_eligible_sensors()
        sensors["100_200_1"]["data"] = {"type": "valve"}
        flow = _make_options_flow(sensors=sensors)

        await flow.async_step_init(
            {CONF_PUSH_ENABLED: True, CONF_GENERIC_ENTITIES_ENABLED: False, CONF_GENERIC_CONTROL_ENABLED: True}
        )

        assert flow.async_create_entry.call_args.kwargs["data"][CONF_GENERIC_CONTROL_ACKED_KEYS] == []


class TestOptionsFlowGenericEligibilityCopy:
    """The form reports how many of this account's devices the generic toggle would actually affect.

    The curated identity table is deliberately narrow, so a user can enable the
    toggle, see nothing appear, and reasonably conclude the integration is
    broken. The form states the real effect up front instead.
    """

    @staticmethod
    def _unsupported(model: str) -> dict:
        return {"model": model, "data": {"type": "unknown", "model": model}}

    @staticmethod
    def _supported(model: str) -> dict:
        return {"model": model, "data": {"type": "valve", "model": model}}

    @pytest.mark.asyncio
    async def test_placeholders_report_zero_eligible_of_the_unsupported_devices(self):
        """Unsupported devices that no curated row covers are counted, but none are eligible."""
        flow = _make_options_flow(
            sensors={
                "a": self._unsupported("HTV245FRF"),
                "b": self._unsupported("HCS003ARF-V1"),
            }
        )

        await flow.async_step_init(None)

        placeholders = flow.async_show_form.call_args.kwargs["description_placeholders"]
        assert placeholders == {
            "generic_eligible": "0",
            "generic_unsupported": "2",
            "generic_control_eligible": "0",
            "generic_control_unsupported": "2",
        }

    @pytest.mark.asyncio
    async def test_devices_with_a_hand_written_decoder_are_not_counted(self):
        """A decoded device is not an unsupported device, so it never appears in the denominator."""
        flow = _make_options_flow(
            sensors={
                "a": self._supported("HTV245FRF"),
                "b": self._unsupported("HCS003ARF-V1"),
            }
        )

        await flow.async_step_init(None)

        placeholders = flow.async_show_form.call_args.kwargs["description_placeholders"]
        assert placeholders["generic_unsupported"] == "1"

    @pytest.mark.asyncio
    async def test_unloaded_entry_degrades_to_zero_rather_than_raising(self):
        """Opening the form while the entry is absent from hass.data reports zero, not a traceback."""
        flow = _make_options_flow(entry_loaded=False)

        await flow.async_step_init(None)

        placeholders = flow.async_show_form.call_args.kwargs["description_placeholders"]
        assert placeholders == {
            "generic_eligible": "0",
            "generic_unsupported": "0",
            "generic_control_eligible": "0",
            "generic_control_unsupported": "0",
        }

    @pytest.mark.asyncio
    async def test_english_description_consumes_both_placeholders(self):
        """The shipped copy references both placeholder names, so neither silently goes unused."""
        translations = json.loads((Path(__file__).parent.parent / "custom_components/rainpoint/translations/en.json").read_text())
        description = translations["options"]["step"]["init"]["data_description"]["generic_entities_enabled"]

        assert "{generic_eligible}" in description
        assert "{generic_unsupported}" in description


class TestOptionsFlowGenericControlEligibilityCopy:
    """The form also reports how many devices the control toggle would actually affect.

    Mirrors TestOptionsFlowGenericEligibilityCopy, against the control gate's
    own counter instead of the sensor gate's, so the two toggles never share
    (or silently borrow) each other's numbers.
    """

    @staticmethod
    def _unsupported(model: str) -> dict:
        return {"model": model, "data": {"type": "unknown", "model": model}}

    @staticmethod
    def _supported(model: str) -> dict:
        return {"model": model, "data": {"type": "valve", "model": model}}

    @pytest.mark.asyncio
    async def test_placeholders_report_zero_eligible_of_the_unsupported_devices(self):
        """Unsupported devices the control gate rejects are counted, but none are eligible."""
        flow = _make_options_flow(
            sensors={
                "a": self._unsupported("HTV245FRF"),
                "b": self._unsupported("HCS003ARF-V1"),
            }
        )

        await flow.async_step_init(None)

        placeholders = flow.async_show_form.call_args.kwargs["description_placeholders"]
        assert placeholders["generic_control_eligible"] == "0"
        assert placeholders["generic_control_unsupported"] == "2"

    @pytest.mark.asyncio
    async def test_devices_with_a_hand_written_decoder_are_not_counted(self):
        """A decoded device is not an unsupported device, so it never appears in the denominator."""
        flow = _make_options_flow(
            sensors={
                "a": self._supported("HTV245FRF"),
                "b": self._unsupported("HCS003ARF-V1"),
            }
        )

        await flow.async_step_init(None)

        placeholders = flow.async_show_form.call_args.kwargs["description_placeholders"]
        assert placeholders["generic_control_unsupported"] == "1"

    @pytest.mark.asyncio
    async def test_an_eligible_control_variant_is_counted(self):
        """A device whose catalog variant passes the control gate is reported as eligible."""
        flow = _make_options_flow(sensors={"a": self._unsupported("HTV103FRF")})

        await flow.async_step_init(None)

        placeholders = flow.async_show_form.call_args.kwargs["description_placeholders"]
        assert placeholders["generic_control_eligible"] == "1"
        assert placeholders["generic_control_unsupported"] == "1"

    @pytest.mark.asyncio
    async def test_unloaded_entry_degrades_to_zero_rather_than_raising(self):
        """Opening the form while the entry is absent from hass.data reports zero, not a traceback."""
        flow = _make_options_flow(entry_loaded=False)

        await flow.async_step_init(None)

        placeholders = flow.async_show_form.call_args.kwargs["description_placeholders"]
        assert placeholders["generic_control_eligible"] == "0"
        assert placeholders["generic_control_unsupported"] == "0"

    @pytest.mark.asyncio
    async def test_english_description_consumes_both_control_placeholders(self):
        """The shipped copy references both control placeholder names, so neither goes unused."""
        translations = json.loads((Path(__file__).parent.parent / "custom_components/rainpoint/translations/en.json").read_text())
        description = translations["options"]["step"]["init"]["data_description"]["generic_control_enabled"]

        assert "{generic_control_eligible}" in description
        assert "{generic_control_unsupported}" in description
        assert "valve" in description
        assert "hardware" in description
