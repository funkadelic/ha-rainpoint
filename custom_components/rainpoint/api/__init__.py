"""
RainPoint API module.

This module provides a clean, organized interface to the RainPoint API functionality.
"""

from .client import RainPointApiError, RainPointClient, RainPointThrottledError
from .decoders import (
    _USAGE_GALLONS_PER_COUNT,
    decode_co2,
    decode_display,
    decode_flow_meter,
    decode_flowmeter,  # Alias for backward compatibility
    decode_hcs003frf,
    decode_hcs005frf,
    decode_hcs014arf,
    decode_hcs015arf,
    decode_hcs016arf,
    decode_hcs024frf_v1,
    decode_hcs027arf,
    decode_hcs044frf,
    decode_hcs048b,
    decode_hcs0528arf,
    decode_hcs0600arf,
    decode_hcs596wb,
    decode_hcs596wb_v4,
    decode_hcs666frf,
    decode_hcs666frf_x,
    decode_hcs666rfr_p,
    decode_hcs701b,
    decode_hcs706arf,
    decode_hcs802arf,
    decode_hcs888arf_v1,
    decode_hcs999frf,
    decode_hcs999frf_p,
    decode_htv145frf,
    decode_htv213frf_valve,
    decode_hws019wrf_v2,
    decode_moisture_full,
    decode_moisture_simple,
    decode_pool,
    decode_pool_plus,
    decode_rain,
    decode_soil,
    decode_temp_hum,
    decode_temp_hum_full,
    decode_temphum,
    decode_unknown,
    decode_valve_hub,
)
from .generic_decoder import decode_generic
from .product_catalog import get_catalog_entry, get_catalog_port_number, get_catalog_variant_codes
from .trust import is_hand_written_model
from .utils import (
    _base_decoder_dict,
    _f10_to_c,
    _le16,
    _parse_rainpoint_payload,
    _parse_tlv_payload,
)
from .validators import (
    _battery_status_to_percent,
    _extract_rssi,
    _extract_status_code,
    _validate_payload,
    _validate_tag,
)

__all__ = [
    # Decoder constants
    "_USAGE_GALLONS_PER_COUNT",
    # Client
    "RainPointApiError",
    "RainPointClient",
    "RainPointThrottledError",
    # Utils
    "_base_decoder_dict",
    # Validators
    "_battery_status_to_percent",
    "_extract_rssi",
    "_extract_status_code",
    "_f10_to_c",
    "_le16",
    "_parse_rainpoint_payload",
    "_parse_tlv_payload",
    "_validate_payload",
    "_validate_tag",
    # Decoders
    "decode_co2",
    "decode_display",
    "decode_flow_meter",
    "decode_flowmeter",  # Alias for backward compatibility
    "decode_generic",
    "decode_hcs003frf",
    "decode_hcs005frf",
    "decode_hcs014arf",
    "decode_hcs015arf",
    "decode_hcs016arf",
    "decode_hcs024frf_v1",
    "decode_hcs027arf",
    "decode_hcs044frf",
    "decode_hcs048b",
    "decode_hcs0528arf",
    "decode_hcs0600arf",
    "decode_hcs596wb",
    "decode_hcs596wb_v4",
    "decode_hcs666frf",
    "decode_hcs666frf_x",
    "decode_hcs666rfr_p",
    "decode_hcs701b",
    "decode_hcs706arf",
    "decode_hcs802arf",
    "decode_hcs888arf_v1",
    "decode_hcs999frf",
    "decode_hcs999frf_p",
    "decode_htv145frf",
    "decode_htv213frf_valve",
    "decode_hws019wrf_v2",
    "decode_moisture_full",
    "decode_moisture_simple",
    "decode_pool",
    "decode_pool_plus",
    "decode_rain",
    "decode_soil",
    "decode_temp_hum",
    "decode_temp_hum_full",
    "decode_temphum",
    "decode_unknown",
    "decode_valve_hub",
    "get_catalog_entry",
    "get_catalog_port_number",
    "get_catalog_variant_codes",
    "is_hand_written_model",
]
