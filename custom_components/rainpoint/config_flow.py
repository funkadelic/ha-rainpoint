from __future__ import annotations

import logging
from typing import Any

import aiohttp
import voluptuous as vol
from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.data_entry_flow import FlowResult
from homeassistant.generated.countries import COUNTRIES
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    CountrySelector,
    CountrySelectorConfig,
)

from .api import RainPointApiError, RainPointClient, RainPointThrottledError
from .const import (
    CONF_AREA_CODE,
    CONF_COUNTRY,
    CONF_EMAIL,
    CONF_GENERIC_ENTITIES_ENABLED,
    CONF_HIDS,
    CONF_PASSWORD,
    CONF_PUSH_ENABLED,
    DOMAIN,
)
from .country_codes import (
    COUNTRY_TO_PHONE_CODE,
    get_default_country,
    get_supported_countries,
    resolve_country_from_phone_code,
)

_LOGGER = logging.getLogger(__name__)


def _country_selector() -> CountrySelector:
    """Build the country picker (HA's localized country dropdown, ISO value)."""
    return CountrySelector(
        CountrySelectorConfig(
            countries=get_supported_countries(COUNTRIES),
        )
    )


class RainPointConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for RainPoint Smart+ devices."""

    VERSION = 1

    def __init__(self) -> None:
        """Initialize the config flow."""
        self._reconfigure = False

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: config_entries.ConfigEntry) -> RainPointOptionsFlow:
        """Return the options flow handler."""
        return RainPointOptionsFlow()

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        errors: dict[str, str] = {}

        if user_input is not None:
            country = user_input[CONF_COUNTRY]
            area_code = COUNTRY_TO_PHONE_CODE[country]
            email = user_input[CONF_EMAIL]
            password = user_input[CONF_PASSWORD]

            # Normalize email for consistent deduplication
            email = email.strip().lower()

            # Single account per HA instance
            await self.async_set_unique_id(f"{DOMAIN}_{email}")
            if self._reconfigure:
                self._abort_if_unique_id_mismatch()  # pragma: no cover -- HA framework wrapper; raises AbortFlow at runtime only
            else:
                self._abort_if_unique_id_configured()

            session = async_get_clientsession(self.hass)
            client = RainPointClient(area_code, email, password, session)

            try:
                await client.ensure_logged_in()
                homes = await client.list_homes()
                _LOGGER.info("Found %d homes", len(homes))
                _LOGGER.debug("Homes data: %s", homes)
            except RainPointThrottledError:
                _LOGGER.warning("RainPoint login is rate-limited; asking the user to retry later")
                errors["base"] = "rate_limited"
            except RainPointApiError:
                _LOGGER.exception("Error logging in to RainPoint")
                errors["base"] = "auth_failed"
            except (TimeoutError, aiohttp.ClientError):
                _LOGGER.exception("Network error talking to RainPoint")
                errors["base"] = "cannot_connect"
            else:
                if not homes:
                    errors["base"] = "no_homes"
                else:
                    # Store temp values for the next step
                    self._country = country
                    self._area_code = area_code
                    self._email = email
                    self._password = password
                    self._homes = homes
                    self._client = client
                    return await self.async_step_select_homes()

        default_country = get_default_country(self.hass)

        data_schema = vol.Schema(
            {
                vol.Required(CONF_COUNTRY, default=default_country): _country_selector(),
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
                    CONF_COUNTRY: self._country,
                    CONF_AREA_CODE: self._area_code,
                    CONF_EMAIL: self._email,
                    CONF_PASSWORD: self._password,
                    CONF_HIDS: hids,
                    **token_data,
                }

                if self._reconfigure:  # pragma: no cover -- HA framework wrapper; requires real ConfigEntry runtime
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

        # single-select dropdown - keys are HIDs, labels come from options dict
        data_schema = vol.Schema({vol.Required(CONF_HIDS): vol.In(home_options)})

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

        # Prefer an explicitly stored ISO; otherwise derive one from the
        # legacy phone code, preferring HA's configured country when its
        # dial code matches. Keeps pre-upgrade entries from silently
        # switching dial codes on a no-op reconfigure submit.
        default_country = current_data.get(CONF_COUNTRY) or resolve_country_from_phone_code(
            current_data.get(CONF_AREA_CODE),
            preferred_iso=get_default_country(self.hass),
        )

        # Pre-fill form with current values
        data_schema = vol.Schema(
            {
                vol.Required(CONF_COUNTRY, default=default_country): _country_selector(),
                vol.Required(CONF_EMAIL, default=current_data.get(CONF_EMAIL, "")): str,
                vol.Required(CONF_PASSWORD, default=current_data.get(CONF_PASSWORD, "")): str,
            }
        )

        if user_input is not None:
            country = user_input[CONF_COUNTRY]
            area_code = COUNTRY_TO_PHONE_CODE[country]
            email = user_input[CONF_EMAIL].strip().lower()
            password = user_input[CONF_PASSWORD]

            # Test new credentials
            session = async_get_clientsession(self.hass)
            client = RainPointClient(area_code, email, password, session)

            try:
                await client.ensure_logged_in()
                homes = await client.list_homes()
                _LOGGER.info("Found %d homes for reconfigure", len(homes))
            except RainPointThrottledError:
                _LOGGER.warning("RainPoint login is rate-limited during reconfigure; asking the user to retry later")
                return self.async_show_form(
                    step_id="reconfigure",
                    data_schema=data_schema,
                    errors={"base": "rate_limited"},
                )
            except RainPointApiError:
                _LOGGER.exception("Error logging in to RainPoint during reconfigure")
                return self.async_show_form(
                    step_id="reconfigure",
                    data_schema=data_schema,
                    errors={"base": "auth_failed"},
                )
            except (TimeoutError, aiohttp.ClientError):
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
                    # Email is already normalized above; update unique_id for account deduplication.
                    await self.async_set_unique_id(f"{DOMAIN}_{email}")
                    self._abort_if_unique_id_mismatch()

                    # Store temp values for the next step
                    self._country = country
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
                    CONF_COUNTRY: self._country,
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

        data_schema = vol.Schema({vol.Required(CONF_HIDS, default=current_hid): vol.In(home_options)})

        return self.async_show_form(
            step_id="select_homes_reconfigure",
            data_schema=data_schema,
            errors=errors,
        )


class RainPointOptionsFlow(config_entries.OptionsFlow):
    """Handle the RainPoint options flow -- push and generic-sensor toggles."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        """Show/handle the single form carrying both toggles."""
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        data_schema = vol.Schema(
            {
                vol.Required(
                    CONF_PUSH_ENABLED,
                    default=self.config_entry.options.get(CONF_PUSH_ENABLED, False),
                ): bool,
                vol.Required(
                    CONF_GENERIC_ENTITIES_ENABLED,
                    default=self.config_entry.options.get(CONF_GENERIC_ENTITIES_ENABLED, False),
                ): bool,
            }
        )
        eligible, unsupported = self._generic_eligibility()
        return self.async_show_form(
            step_id="init",
            data_schema=data_schema,
            description_placeholders={
                "generic_eligible": str(eligible),
                "generic_unsupported": str(unsupported),
            },
        )

    def _generic_eligibility(self) -> tuple[int, int]:
        """Return (eligible, unsupported_total) for this entry's devices.

        Reported on the form so the generic-sensor toggle states its real
        effect: the curated identity table is deliberately narrow, so a user
        can otherwise enable it, see nothing appear, and reasonably conclude
        the integration is broken.

        Imported inside the function to keep the whole sensor platform out of
        this module's import, since generic_entities pulls in sensor at load
        time and nothing else here needs it. Note this is not the cycle-breaking
        case sensor.py has: nothing imports config_flow, so a top-level import
        would resolve fine. Degrades to (0, 0) when the entry is not loaded,
        which reads as "this adds nothing".
        """
        from .generic_entities import count_generic_eligible_devices

        entry_store = (self.hass.data.get(DOMAIN) or {}).get(self.config_entry.entry_id) or {}
        coordinator = entry_store.get("coordinator")
        return count_generic_eligible_devices(getattr(coordinator, "data", None))
