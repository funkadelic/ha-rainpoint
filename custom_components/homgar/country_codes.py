"""Country code mapping for HomGar/RainPoint integration."""

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


def get_default_country_code(hass) -> str:
    """Get phone country code from HA's configured country, falling back to '27' (ZA)."""
    try:
        country = hass.config.country
        if country and country in COUNTRY_TO_PHONE_CODE:
            return COUNTRY_TO_PHONE_CODE[country]
    except AttributeError:
        pass
    return "27"
