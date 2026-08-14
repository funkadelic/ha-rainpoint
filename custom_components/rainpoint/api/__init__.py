"""
RainPoint API module.

This module provides a clean, organized interface to the RainPoint API functionality.
"""

from .client import RainPointApiError, RainPointClient, RainPointThrottledError
from .decoders import (
    _USAGE_GALLONS_PER_COUNT,
    _decode_packed_timestamp,
    decode_co2,
    decode_display,
    decode_flow_meter,
    decode_flowmeter,  # Alias for backward compatibility
    decode_hcs005frf,
    decode_hcs014arf,
    decode_hcs015arf,
    decode_hcs024frf_v1,
    decode_hcs0528arf,
    decode_hic801w,
    decode_htv145frf,
    decode_htv210b,
    decode_htv210b_dp_state,
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
from .generic_decoder import decode_generic, is_ascii_declined
from .product_catalog import get_catalog_entry, get_catalog_port_number, get_catalog_variant_codes
from .trust import has_bluetooth_control_identity, is_hand_written_model
from .utils import (
    _base_decoder_dict,
    _decode_packed_report_time,
    _encode_dp_duration_param,
    _extract_report_time,
    _f10_to_c,
    _le16,
    _parse_hub_broadcast_flag,
    _parse_rainpoint_payload,
    _parse_sub_power_mode,
    _parse_tlv_payload,
    _redact_secret,
    _safe_key,
    _splice_hub_broadcast_param,
    _splice_sub_power_mode,
    _summarize_record,
)
from .validators import (
    _battery_flag_to_percent,
    _extract_battery_flag,
    _extract_rssi,
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
    # Utils and decoder helpers
    "_base_decoder_dict",
    "_battery_flag_to_percent",
    "_decode_packed_report_time",
    "_decode_packed_timestamp",
    "_encode_dp_duration_param",
    "_extract_battery_flag",
    "_extract_report_time",
    "_extract_rssi",
    "_f10_to_c",
    "_le16",
    "_parse_hub_broadcast_flag",
    "_parse_rainpoint_payload",
    "_parse_sub_power_mode",
    "_parse_tlv_payload",
    "_redact_secret",
    "_safe_key",
    "_splice_hub_broadcast_param",
    "_splice_sub_power_mode",
    "_summarize_record",
    "_validate_payload",
    "_validate_tag",
    # Decoders
    "decode_co2",
    "decode_display",
    "decode_flow_meter",
    "decode_flowmeter",  # Alias for backward compatibility
    "decode_generic",
    "decode_hcs005frf",
    "decode_hcs014arf",
    "decode_hcs015arf",
    "decode_hcs024frf_v1",
    "decode_hcs0528arf",
    "decode_hic801w",
    "decode_htv145frf",
    "decode_htv210b",
    "decode_htv210b_dp_state",
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
    "has_bluetooth_control_identity",
    "is_ascii_declined",
    "is_hand_written_model",
]
