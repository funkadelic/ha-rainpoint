from __future__ import annotations

import logging
from typing import Any

import aiohttp
import voluptuous as vol

from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import (
    DOMAIN,
    CONF_AREA_CODE,
    CONF_EMAIL,
    CONF_PASSWORD,
    CONF_HIDS,
)
from .country_codes import get_default_country_code
from .api import RainPointClient
from .api.client import RainPointApiError

_LOGGER = logging.getLogger(__name__)


class RainPointConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for RainPoint Smart+ devices."""

    VERSION = 1

    def __init__(self) -> None:
        """Initialize the config flow."""
        self._reconfigure = False

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        errors: dict[str, str] = {}

        if user_input is not None:
            area_code = user_input[CONF_AREA_CODE]
            email = user_input[CONF_EMAIL]
            password = user_input[CONF_PASSWORD]

            # Single account per HA instance
            await self.async_set_unique_id(f"{DOMAIN}_{email}")
            if self._reconfigure:
                self._abort_if_unique_id_mismatch()
            else:
                self._abort_if_unique_id_configured()

            session = async_get_clientsession(self.hass)
            client = RainPointClient(area_code, email, password, session)

            try:
                await client.ensure_logged_in()
                homes = await client.list_homes()
                _LOGGER.info("Found %d homes", len(homes))
                _LOGGER.debug("Homes data: %s", homes)
            except RainPointApiError:
                _LOGGER.exception("Error logging in to RainPoint")
                errors["base"] = "auth_failed"
            except aiohttp.ClientError:
                _LOGGER.exception("Network error talking to RainPoint")
                errors["base"] = "cannot_connect"
            else:
                if not homes:
                    errors["base"] = "no_homes"
                else:
                    # Store temp values for the next step
                    self._area_code = area_code
                    self._email = email
                    self._password = password
                    self._homes = homes
                    self._client = client
                    return await self.async_step_select_homes()

        default_country_code = get_default_country_code(self.hass)

        data_schema = vol.Schema(
            {
                vol.Required(CONF_AREA_CODE, default=default_country_code): str,
                vol.Required(CONF_EMAIL): str,
                vol.Required(CONF_PASSWORD): str,
            }
        )

        return self.async_show_form(
            step_id="user",
            data_schema=data_schema,
            errors=errors,
        )

    async def async_step_select_homes(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        errors: dict[str, str] = {}

        home_options = {str(h["hid"]): h["homeName"] for h in self._homes}
        _LOGGER.info("Available homes: %s", home_options)

        if user_input is not None:
            selected = user_input.get(CONF_HIDS)
            if not selected:
                errors["base"] = "select_at_least_one"
            else:
                # single home for now
                hids = [int(selected)]

                token_data = self._client.export_tokens()

                data = {
                    CONF_AREA_CODE: self._area_code,
                    CONF_EMAIL: self._email,
                    CONF_PASSWORD: self._password,
                    CONF_HIDS: hids,
                    **token_data,
                }

                if self._reconfigure:
                    return self.async_update_reload_and_abort(
                        self._get_reconfigure_entry(),
                        data=data,
                        title=f"RainPoint ({self._email})",
                    )
                else:
                    return self.async_create_entry(
                        title=f"RainPoint ({self._email})",
                        data=data,
                    )

        # single-select dropdown – keys are HIDs, labels come from options dict
        data_schema = vol.Schema(
            {
                vol.Required(CONF_HIDS): vol.In(home_options)
            }
        )

        return self.async_show_form(
            step_id="select_homes",
            data_schema=data_schema,
            errors=errors,
        )

    async def async_step_reconfigure(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        """Handle reconfiguration of the integration."""
        self._reconfigure = True
        
        # Get current entry data
        entry = self._get_reconfigure_entry()
        current_data = entry.data
        
        default_country_code = get_default_country_code(self.hass)

        # Pre-fill form with current values
        data_schema = vol.Schema(
            {
                vol.Required(CONF_AREA_CODE, default=current_data.get(CONF_AREA_CODE, default_country_code)): str,
                vol.Required(CONF_EMAIL, default=current_data.get(CONF_EMAIL, "")): str,
                vol.Required(CONF_PASSWORD, default=current_data.get(CONF_PASSWORD, "")): str,
            }
        )

        if user_input is not None:
            area_code = user_input[CONF_AREA_CODE]
            email = user_input[CONF_EMAIL]
            password = user_input[CONF_PASSWORD]

            # Test new credentials
            session = async_get_clientsession(self.hass)
            client = RainPointClient(area_code, email, password, session)

            try:
                await client.ensure_logged_in()
                homes = await client.list_homes()
                _LOGGER.info("Found %d homes for reconfigure", len(homes))
            except RainPointApiError:
                _LOGGER.exception("Error logging in to RainPoint during reconfigure")
                return self.async_show_form(
                    step_id="reconfigure",
                    data_schema=data_schema,
                    errors={"base": "auth_failed"},
                )
            except aiohttp.ClientError:
                _LOGGER.exception("Network error during reconfigure")
                return self.async_show_form(
                    step_id="reconfigure",
                    data_schema=data_schema,
                    errors={"base": "cannot_connect"},
                )
            else:
                if not homes:
                    return self.async_show_form(
                        step_id="reconfigure",
                        data_schema=data_schema,
                        errors={"base": "no_homes"},
                    )
                else:
                    # Update unique_id for account deduplication
                    entry = self._get_reconfigure_entry()
                    await self.async_set_unique_id(f"{DOMAIN}_{email}")
                    self._abort_if_unique_id_mismatch()

                    # Store temp values for the next step
                    self._area_code = area_code
                    self._email = email
                    self._password = password
                    self._homes = homes
                    self._client = client
                    return await self.async_step_select_homes_reconfigure()

        return self.async_show_form(
            step_id="reconfigure",
            data_schema=data_schema,
        )

    async def async_step_select_homes_reconfigure(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        """Handle home selection during reconfiguration."""
        errors: dict[str, str] = {}
        
        home_options = {str(h["hid"]): h["homeName"] for h in self._homes}
        current_entry = self._get_reconfigure_entry()
        current_hids = current_entry.data.get(CONF_HIDS, [])
        
        if user_input is not None:
            selected = user_input.get(CONF_HIDS)
            if not selected:
                errors["base"] = "select_at_least_one"
            else:
                # single home for now
                hids = [int(selected)]

                token_data = self._client.export_tokens()

                data = {
                    CONF_AREA_CODE: self._area_code,
                    CONF_EMAIL: self._email,
                    CONF_PASSWORD: self._password,
                    CONF_HIDS: hids,
                    **token_data,
                }

                return self.async_update_reload_and_abort(
                    current_entry,
                    data=data,
                    title=f"RainPoint ({self._email})",
                )

        # Pre-select current home
        current_hid = str(current_hids[0]) if current_hids else None
        
        data_schema = vol.Schema(
            {
                vol.Required(CONF_HIDS, default=current_hid): vol.In(home_options)
            }
        )

        return self.async_show_form(
            step_id="select_homes_reconfigure",
            data_schema=data_schema,
            errors=errors,
        )