"""Tests for custom_components.rainpoint.country_codes."""

from unittest.mock import MagicMock

from custom_components.rainpoint.country_codes import (
    COUNTRY_TO_PHONE_CODE,
    get_default_country_code,
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
        assert get_default_country_code(hass) == "27"

    def test_no_country_attribute(self):
        """No country attribute."""
        hass = MagicMock(spec=[])  # no attributes at all
        assert get_default_country_code(hass) == "27"

    def test_none_country(self):
        """None country."""
        hass = _make_hass(None)
        assert get_default_country_code(hass) == "27"

    def test_empty_string_country(self):
        """Empty string country."""
        hass = _make_hass("")
        assert get_default_country_code(hass) == "27"


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
