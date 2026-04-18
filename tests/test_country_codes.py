"""Tests for custom_components.rainpoint.country_codes."""

from unittest.mock import MagicMock

from custom_components.rainpoint.country_codes import (
    COUNTRY_NAMES,
    COUNTRY_TO_PHONE_CODE,
    get_country_code_options,
    get_default_country_code,
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


class TestCountryNames:
    """Tests for COUNTRY_NAMES mapping."""

    def test_every_phone_code_has_a_country_name(self):
        """Each ISO code in COUNTRY_TO_PHONE_CODE must have a display name."""
        missing = [iso for iso in COUNTRY_TO_PHONE_CODE if iso not in COUNTRY_NAMES]
        assert not missing, f"Missing display names for: {missing}"

    def test_us_name(self):
        """US maps to United States."""
        assert COUNTRY_NAMES["US"] == "United States"

    def test_gb_name(self):
        """GB maps to United Kingdom."""
        assert COUNTRY_NAMES["GB"] == "United Kingdom"


class TestGetCountryCodeOptions:
    """Tests for get_country_code_options dropdown helper."""

    def test_returns_list_of_dicts(self):
        """Returns a non-empty list of {value, label} dicts."""
        options = get_country_code_options()
        assert isinstance(options, list)
        assert options
        assert all(set(o.keys()) == {"value", "label"} for o in options)

    def test_values_are_iso_codes(self):
        """Every option value is one of the known ISO country codes."""
        options = get_country_code_options()
        values = {o["value"] for o in options}
        assert values == set(COUNTRY_TO_PHONE_CODE.keys())

    def test_us_and_ca_are_separate_rows(self):
        """US and CA share dial code +1 but appear as two distinct options."""
        options = get_country_code_options()
        labels_by_value = {o["value"]: o["label"] for o in options}
        assert "United States" in labels_by_value["US"]
        assert "Canada" in labels_by_value["CA"]
        assert "+1" in labels_by_value["US"]
        assert "+1" in labels_by_value["CA"]

    def test_label_format_includes_plus_prefix(self):
        """Labels show the dial code with a '+' prefix."""
        options = {o["value"]: o["label"] for o in get_country_code_options()}
        assert "+44" in options["GB"]
        assert "United Kingdom" in options["GB"]

    def test_options_sorted_alphabetically_by_label(self):
        """Options are sorted by label so dropdown entries are browsable."""
        labels = [o["label"] for o in get_country_code_options()]
        assert labels == sorted(labels)

    def test_fallback_country_is_in_options(self):
        """US fallback used by get_default_country must be selectable."""
        options = {o["value"]: o["label"] for o in get_country_code_options()}
        assert "US" in options
        assert "United States" in options["US"]


class TestResolveCountryFromPhoneCode:
    """Tests for resolve_country_from_phone_code, used for pre-CONF_COUNTRY upgrades."""

    def test_preferred_iso_matches_phone_code(self):
        """When HA's configured country matches the stored dial code, prefer it."""
        assert resolve_country_from_phone_code("1", preferred_iso="US") == "US"

    def test_preferred_iso_mismatch_finds_matching_iso(self):
        """If preferred_iso's dial code doesn't match, fall through to any matching ISO."""
        assert resolve_country_from_phone_code("44", preferred_iso="US") == "GB"

    def test_unknown_phone_code_returns_fallback_not_preferred(self):
        """Bogus stored dial codes should not silently pre-select the preferred ISO."""
        # preferred_iso="GB" (dial code "44") does not match "999"; returning
        # GB would imply a match that doesn't exist. Use the explicit fallback
        # (US) instead. Using GB here keeps the test discriminative vs. the
        # fallback, which is itself US.
        assert resolve_country_from_phone_code("999", preferred_iso="GB") == "US"

    def test_no_phone_code_returns_preferred(self):
        """Fresh entries with no legacy phone_code should use the preferred ISO."""
        assert resolve_country_from_phone_code(None, preferred_iso="GB") == "GB"

    def test_no_phone_code_no_preferred_returns_fallback(self):
        """With nothing to go on, use the fallback country."""
        assert resolve_country_from_phone_code(None, preferred_iso=None) == "US"

    def test_empty_phone_code_treated_as_no_phone_code(self):
        """Empty-string phone_code behaves like None (pre-upgrade with no stored code)."""
        assert resolve_country_from_phone_code("", preferred_iso="GB") == "GB"
