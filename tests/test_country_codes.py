"""Tests for custom_components.rainpoint.country_codes."""

from unittest.mock import MagicMock

import pytest

from custom_components.rainpoint.country_codes import (
    COUNTRY_TO_PHONE_CODE,
    get_default_country_code,
)


def _make_hass(country):
    hass = MagicMock()
    hass.config.country = country
    return hass


class TestGetDefaultCountryCode:
    def test_known_country_us(self):
        hass = _make_hass("US")
        assert get_default_country_code(hass) == "1"

    def test_known_country_gb(self):
        hass = _make_hass("GB")
        assert get_default_country_code(hass) == "44"

    def test_known_country_ca(self):
        hass = _make_hass("CA")
        assert get_default_country_code(hass) == "1"

    def test_unknown_country_falls_back(self):
        hass = _make_hass("XX")
        assert get_default_country_code(hass) == "27"

    def test_no_country_attribute(self):
        hass = MagicMock(spec=[])  # no attributes at all
        assert get_default_country_code(hass) == "27"

    def test_none_country(self):
        hass = _make_hass(None)
        assert get_default_country_code(hass) == "27"

    def test_empty_string_country(self):
        hass = _make_hass("")
        assert get_default_country_code(hass) == "27"


class TestCountryToPhoneCodeMap:
    def test_us_is_1(self):
        assert COUNTRY_TO_PHONE_CODE["US"] == "1"

    def test_ca_is_1(self):
        assert COUNTRY_TO_PHONE_CODE["CA"] == "1"

    def test_gb_is_44(self):
        assert COUNTRY_TO_PHONE_CODE["GB"] == "44"

    def test_za_is_27(self):
        assert COUNTRY_TO_PHONE_CODE["ZA"] == "27"

    def test_de_is_49(self):
        assert COUNTRY_TO_PHONE_CODE["DE"] == "49"

    def test_au_is_61(self):
        assert COUNTRY_TO_PHONE_CODE["AU"] == "61"
