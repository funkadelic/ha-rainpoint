"""Country code mapping for the RainPoint Cloud integration."""

# ISO 3166-1 alpha-2 → phone country code
COUNTRY_TO_PHONE_CODE = {
    "US": "1",
    "CA": "1",
    "GB": "44",
    "AU": "61",
    "NZ": "64",
    "ZA": "27",
    "DE": "49",
    "FR": "33",
    "IT": "39",
    "ES": "34",
    "NL": "31",
    "BE": "32",
    "CH": "41",
    "AT": "43",
    "SE": "46",
    "NO": "47",
    "DK": "45",
    "FI": "358",
    "PL": "48",
    "CZ": "420",
    "IE": "353",
    "PT": "351",
    "GR": "30",
    "RU": "7",
    "CN": "86",
    "JP": "81",
    "KR": "82",
    "IN": "91",
    "BR": "55",
    "MX": "52",
    "AR": "54",
    "CL": "56",
    "CO": "57",
    "SG": "65",
    "MY": "60",
    "TH": "66",
    "ID": "62",
    "PH": "63",
    "VN": "84",
    "IL": "972",
    "AE": "971",
    "SA": "966",
    "TR": "90",
    "EG": "20",
    "NG": "234",
    "KE": "254",
    "HK": "852",
    "TW": "886",
}

# ISO 3166-1 alpha-2 → English country name (display label)
COUNTRY_NAMES = {
    "US": "United States",
    "CA": "Canada",
    "GB": "United Kingdom",
    "AU": "Australia",
    "NZ": "New Zealand",
    "ZA": "South Africa",
    "DE": "Germany",
    "FR": "France",
    "IT": "Italy",
    "ES": "Spain",
    "NL": "Netherlands",
    "BE": "Belgium",
    "CH": "Switzerland",
    "AT": "Austria",
    "SE": "Sweden",
    "NO": "Norway",
    "DK": "Denmark",
    "FI": "Finland",
    "PL": "Poland",
    "CZ": "Czech Republic",
    "IE": "Ireland",
    "PT": "Portugal",
    "GR": "Greece",
    "RU": "Russia",
    "CN": "China",
    "JP": "Japan",
    "KR": "South Korea",
    "IN": "India",
    "BR": "Brazil",
    "MX": "Mexico",
    "AR": "Argentina",
    "CL": "Chile",
    "CO": "Colombia",
    "SG": "Singapore",
    "MY": "Malaysia",
    "TH": "Thailand",
    "ID": "Indonesia",
    "PH": "Philippines",
    "VN": "Vietnam",
    "IL": "Israel",
    "AE": "United Arab Emirates",
    "SA": "Saudi Arabia",
    "TR": "Turkey",
    "EG": "Egypt",
    "NG": "Nigeria",
    "KE": "Kenya",
    "HK": "Hong Kong",
    "TW": "Taiwan",
}


_FALLBACK_COUNTRY = "ZA"


def get_default_country(hass) -> str:
    """Get ISO country code from HA's configured country, falling back to ZA."""
    try:
        country = hass.config.country
        if country and country in COUNTRY_TO_PHONE_CODE:
            return country
    except AttributeError:
        pass
    return _FALLBACK_COUNTRY


def get_default_country_code(hass) -> str:
    """Get phone country code from HA's configured country, falling back to '27' (ZA)."""
    return COUNTRY_TO_PHONE_CODE[get_default_country(hass)]


def resolve_country_from_phone_code(phone_code: str | None, preferred_iso: str | None = None) -> str:
    """Pick the ISO country that matches a stored phone code.

    Used when upgrading a pre-CONF_COUNTRY config entry: we know the phone
    code, need an ISO to pre-select in the dropdown. If `preferred_iso` maps
    to the same phone code (e.g. HA is configured for `US` and the entry has
    `"1"`), return it. Otherwise fall back to the first ISO with a matching
    phone code, or the default country when nothing matches.
    """
    if preferred_iso and COUNTRY_TO_PHONE_CODE.get(preferred_iso) == phone_code:
        return preferred_iso
    if phone_code:
        for iso, pc in COUNTRY_TO_PHONE_CODE.items():
            if pc == phone_code:
                return iso
    return preferred_iso or _FALLBACK_COUNTRY


def get_country_code_options() -> list[dict[str, str]]:
    """Return a list of dropdown options for the config-flow country picker.

    Each option is a ``{"value": iso, "label": "<Country Name> (+<phone>)"}``
    dict suitable for Home Assistant's ``SelectSelector``. Values are ISO
    codes so countries that share a dial code (US/CA on +1) stay distinct
    and render as separate rows. Sorted alphabetically by label.
    """
    options = [
        {
            "value": iso,
            "label": f"{COUNTRY_NAMES.get(iso, iso)} (+{phone_code})",
        }
        for iso, phone_code in COUNTRY_TO_PHONE_CODE.items()
    ]
    return sorted(options, key=lambda item: item["label"])
