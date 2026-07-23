"""Tests for custom_components.rainpoint.country_codes."""

from unittest.mock import MagicMock

from custom_components.rainpoint.country_codes import (
    COUNTRY_TO_PHONE_CODE,
    get_default_country_code,
    get_supported_countries,
    resolve_country_from_phone_code,
)


def _make_hass(country):
    """Make hass helper."""
    hass = MagicMock()
    hass.config.country = country
    return hass


class TestGetDefaultCountryCode:
    """Tests for GetDefaultCountryCode."""

    def test_known_country_us(self):
        """Known country us."""
        hass = _make_hass("US")
        assert get_default_country_code(hass) == "1"

    def test_known_country_gb(self):
        """Known country gb."""
        hass = _make_hass("GB")
        assert get_default_country_code(hass) == "44"

    def test_known_country_ca(self):
        """Known country ca."""
        hass = _make_hass("CA")
        assert get_default_country_code(hass) == "1"

    def test_unknown_country_falls_back(self):
        """Unknown country falls back."""
        hass = _make_hass("XX")
        assert get_default_country_code(hass) == "1"

    def test_no_country_attribute(self):
        """No country attribute."""
        hass = MagicMock(spec=[])  # no attributes at all
        assert get_default_country_code(hass) == "1"

    def test_none_country(self):
        """None country."""
        hass = _make_hass(None)
        assert get_default_country_code(hass) == "1"

    def test_empty_string_country(self):
        """Empty string country."""
        hass = _make_hass("")
        assert get_default_country_code(hass) == "1"


class TestCountryToPhoneCodeMap:
    """Tests for CountryToPhoneCodeMap."""

    def test_us_is_1(self):
        """Us is 1."""
        assert COUNTRY_TO_PHONE_CODE["US"] == "1"

    def test_ca_is_1(self):
        """Ca is 1."""
        assert COUNTRY_TO_PHONE_CODE["CA"] == "1"

    def test_gb_is_44(self):
        """Gb is 44."""
        assert COUNTRY_TO_PHONE_CODE["GB"] == "44"

    def test_za_is_27(self):
        """Za is 27."""
        assert COUNTRY_TO_PHONE_CODE["ZA"] == "27"

    def test_de_is_49(self):
        """De is 49."""
        assert COUNTRY_TO_PHONE_CODE["DE"] == "49"

    def test_au_is_61(self):
        """Au is 61."""
        assert COUNTRY_TO_PHONE_CODE["AU"] == "61"

    def test_hu_is_36(self):
        """Hu is 36."""
        assert COUNTRY_TO_PHONE_CODE["HU"] == "36"

    def test_covers_all_countries(self):
        """The generated map is comprehensive, not the old ~50-entry curated list."""
        assert len(COUNTRY_TO_PHONE_CODE) > 200

    def test_includes_previously_missing_countries(self):
        """Countries absent from the old curated list are now present, incl. the
        Malta (+356) entry this change was filed to add."""
        assert COUNTRY_TO_PHONE_CODE["MT"] == "356"
        assert COUNTRY_TO_PHONE_CODE["PK"] == "92"
        assert COUNTRY_TO_PHONE_CODE["UA"] == "380"

    def test_every_value_is_a_digit_string(self):
        """Every dial code is a non-empty string of digits (no '+' prefix, no spaces)."""
        assert all(code.isdigit() for code in COUNTRY_TO_PHONE_CODE.values())

    def test_every_key_is_iso_alpha2(self):
        """Every key is a two-letter uppercase ISO 3166-1 alpha-2 code."""
        assert all(len(iso) == 2 and iso.isupper() and iso.isalpha() for iso in COUNTRY_TO_PHONE_CODE)


class TestGetSupportedCountries:
    """Tests for get_supported_countries, backing the config-flow country picker."""

    def test_returns_sorted_iso_codes(self):
        """Returns the ISO codes sorted so the picker restriction is stable."""
        countries = get_supported_countries()
        assert countries == sorted(COUNTRY_TO_PHONE_CODE)
        assert countries == sorted(countries)

    def test_covers_every_mapped_country(self):
        """Every country with a dial code is offered in the picker."""
        assert set(get_supported_countries()) == set(COUNTRY_TO_PHONE_CODE)

    def test_fallback_country_is_selectable(self):
        """US fallback used by get_default_country must be selectable."""
        assert "US" in get_supported_countries()

    def test_filters_against_valid_country_set(self):
        """When given HA's supported set, only its intersection is offered, sorted."""
        assert get_supported_countries({"US", "GB", "MT"}) == ["GB", "MT", "US"]

    def test_excludes_codes_home_assistant_rejects(self):
        """Codes we carry a dial code for but HA's CountrySelector rejects (AC, TA,
        XK) are filtered out so the picker never offers a submit that would fail."""
        # Simulate HA's set as everything we map except the three HA does not support.
        ha_countries = set(COUNTRY_TO_PHONE_CODE) - {"AC", "TA", "XK"}
        offered = get_supported_countries(ha_countries)
        assert "AC" not in offered
        assert "TA" not in offered
        assert "XK" not in offered
        assert "MT" in offered  # a real country HA supports stays selectable


class TestResolveCountryFromPhoneCode:
    """Tests for resolve_country_from_phone_code, used for pre-CONF_COUNTRY upgrades."""

    def test_preferred_iso_matches_phone_code(self):
        """When HA's configured country matches the stored dial code, prefer it."""
        assert resolve_country_from_phone_code("1", preferred_iso="US") == "US"

    def test_preferred_iso_mismatch_finds_matching_iso(self):
        """If preferred_iso's dial code doesn't match, fall through to any matching ISO."""
        assert resolve_country_from_phone_code("44", preferred_iso="US") == "GB"

    def test_shared_dial_code_prefers_fallback_country(self):
        """+1 is shared by ~20 territories; a legacy +1 entry resolves to US, not the
        first territory alphabetically (Antigua), even when preferred_iso doesn't match."""
        assert resolve_country_from_phone_code("1", preferred_iso="GB") == "US"

    def test_unknown_phone_code_returns_fallback_not_preferred(self):
        """Bogus stored dial codes should not silently pre-select the preferred ISO."""
        # preferred_iso="GB" (dial code "44") does not match "9999"; returning
        # GB would imply a match that doesn't exist. Use the explicit fallback
        # (US) instead. "9999" maps to no country in the table.
        assert resolve_country_from_phone_code("9999", preferred_iso="GB") == "US"

    def test_no_phone_code_returns_preferred(self):
        """Fresh entries with no legacy phone_code should use the preferred ISO."""
        assert resolve_country_from_phone_code(None, preferred_iso="GB") == "GB"

    def test_no_phone_code_no_preferred_returns_fallback(self):
        """With nothing to go on, use the fallback country."""
        assert resolve_country_from_phone_code(None, preferred_iso=None) == "US"

    def test_empty_phone_code_treated_as_no_phone_code(self):
        """Empty-string phone_code behaves like None (pre-upgrade with no stored code)."""
        assert resolve_country_from_phone_code("", preferred_iso="GB") == "GB"
