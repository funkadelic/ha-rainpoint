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
    CONF_GENERIC_CONTROL_ACKED_KEYS,
    CONF_GENERIC_CONTROL_ENABLED,
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

    VERSION = 2

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
    """Handle the RainPoint options flow -- push, generic-sensor, and generic-control toggles."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        """Show/handle the single form carrying all three toggles."""
        if user_input is not None:
            return self.async_create_entry(title="", data=self._with_control_consent(user_input))

        data_schema = vol.Schema(
            {
                vol.Required(
                    CONF_PUSH_ENABLED,
                    default=self.config_entry.options.get(CONF_PUSH_ENABLED, True),
                ): bool,
                vol.Required(
                    CONF_GENERIC_ENTITIES_ENABLED,
                    default=self.config_entry.options.get(CONF_GENERIC_ENTITIES_ENABLED, False),
                ): bool,
                vol.Required(
                    CONF_GENERIC_CONTROL_ENABLED,
                    default=self.config_entry.options.get(CONF_GENERIC_CONTROL_ENABLED, False),
                ): bool,
            }
        )
        eligible, unsupported = self._generic_eligibility()
        control_eligible, control_unsupported = self._generic_control_eligibility()
        return self.async_show_form(
            step_id="init",
            data_schema=data_schema,
            description_placeholders={
                "generic_eligible": str(eligible),
                "generic_unsupported": str(unsupported),
                "generic_control_eligible": str(control_eligible),
                "generic_control_unsupported": str(control_unsupported),
            },
        )

    def _with_control_consent(self, user_input: dict[str, Any]) -> dict[str, Any]:
        """Stamp the devices this save is consenting to control, or clear the stamp.

        Saving the control toggle on is the consent event, and this records
        what it covered so the new-controls notice has something durable to
        measure a later device against. The entity registry cannot serve:
        __init__._generic_control_row_removal_reason removes every
        control-namespace row for the entry while the toggle is off, so an
        off-and-on-again would read as a fleet of brand-new devices.

        Re-stamped on every save with the toggle on, not only on the
        transition, because a save is the user looking at the form: whatever
        is eligible at that moment is what they are agreeing to. Turning the
        toggle off drops the stamp, so a later re-enable consents afresh
        rather than against a stale list.

        When the devices cannot be enumerated at all, the stamp already in
        options is carried forward rather than replaced with an empty one.
        async_create_entry replaces the whole options dict, so doing nothing
        would drop it, and an empty stamp is not "consented to nothing" here:
        it would announce every device the user had just consented to the
        moment the entry came back. That happens for real, when Options is
        opened while the entry is retrying against a cloud outage.

        Returns a new dict; the caller's user_input is never mutated.
        """
        options = dict(user_input)
        if not options.get(CONF_GENERIC_CONTROL_ENABLED):
            options.pop(CONF_GENERIC_CONTROL_ACKED_KEYS, None)
            return options
        keys = self._control_eligible_keys()
        if keys is None:
            previous = self.config_entry.options.get(CONF_GENERIC_CONTROL_ACKED_KEYS)
            if previous is not None:
                options[CONF_GENERIC_CONTROL_ACKED_KEYS] = previous
            return options
        options[CONF_GENERIC_CONTROL_ACKED_KEYS] = sorted(keys)
        return options

    def _control_eligible_keys(self) -> set[str] | None:
        """Return the sub-device keys the control gate would admit, or None if unknowable.

        None and the empty set are different answers and the caller treats
        them differently. None means the devices could not be enumerated (the
        entry is not loaded, no poll has landed, or the records are
        unreadable), so the previous stamp stands. An empty set is a real
        observation: devices were enumerated and none is eligible.

        Records that are not dicts are skipped rather than allowed to raise,
        matching valve.py and switch.py, which filter the same way so one
        malformed sub-device cannot take out a whole builder loop. It matters
        more here: this runs inline in async_create_entry, so anything raising
        aborts the save with "Unknown error occurred", including a save that
        was turning generic control off.
        """
        entry_store = (self.hass.data.get(DOMAIN) or {}).get(self.config_entry.entry_id) or {}
        coordinator = entry_store.get("coordinator")
        sensors = (getattr(coordinator, "data", None) or {}).get("sensors")
        if not isinstance(sensors, dict):
            return None

        from .generic_control import evaluate_control_gate

        keys = set()
        for key, info in sensors.items():
            if not isinstance(info, dict):
                continue
            if (info.get("data") or {}).get("type") != "unknown":
                continue
            if evaluate_control_gate(info.get("model"), info.get("model_code")).passed:
                keys.add(key)
        return keys

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

    def _generic_control_eligibility(self) -> tuple[int, int]:
        """Return (eligible, unsupported_total) for the control toggle's real effect.

        Mirrors _generic_eligibility exactly, but counts against the control
        gate (generic_control.evaluate_control_gate) instead of the sensor
        gate, so the control toggle states its own real effect rather than
        borrowing the sensor toggle's numbers. Imported inside the function
        for the same reason _generic_eligibility defers its import --
        generic_control reaches the sensor platform transitively through
        generic_entities. Degrades to (0, 0) when the entry is not loaded.
        """
        from .generic_control import count_generic_control_eligible_devices

        entry_store = (self.hass.data.get(DOMAIN) or {}).get(self.config_entry.entry_id) or {}
        coordinator = entry_store.get("coordinator")
        return count_generic_control_eligible_devices(getattr(coordinator, "data", None))
