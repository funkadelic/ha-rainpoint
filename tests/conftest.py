"""Test configuration for rainpoint integration tests.

This conftest stubs out Home Assistant and third-party HA dependencies so that
custom_components.rainpoint.api can be imported in plain pytest without a running
Home Assistant instance.
"""

import sys
from types import ModuleType
from unittest.mock import MagicMock


def _make_stub(name: str) -> ModuleType:
    """Return a MagicMock-backed module stub registered under *name*."""
    mod = MagicMock()
    mod.__name__ = name
    mod.__spec__ = None
    sys.modules[name] = mod
    return mod


# All HA / third-party modules pulled in transitively when
# custom_components.rainpoint (the package __init__) loads.
# Must be registered BEFORE any test module is imported so that the package
# __init__.py sees them on sys.modules instead of trying a real import.
_HA_STUBS = [
    "voluptuous",
    "homeassistant",
    "homeassistant.config_entries",
    "homeassistant.core",
    "homeassistant.helpers",
    "homeassistant.helpers.aiohttp_client",
    "homeassistant.helpers.entity_platform",
    "homeassistant.helpers.update_coordinator",
    "homeassistant.helpers.entity",
    "homeassistant.components",
    "homeassistant.components.persistent_notification",
    "homeassistant.components.select",
    "homeassistant.components.valve",
    "homeassistant.components.sensor",
    "homeassistant.components.number",
    "homeassistant.components.switch",
    "homeassistant.const",
    "homeassistant.data_entry_flow",
    "homeassistant.exceptions",
    "homeassistant.helpers.device_registry",
    "homeassistant.helpers.restore_state",
    "aiohttp",
]

for _stub_name in _HA_STUBS:
    # Ensure every ancestor package is present so that
    # `from homeassistant.config_entries import ConfigEntry` resolves the
    # parent package first without KeyError.
    _parts = _stub_name.split(".")
    for _i in range(1, len(_parts)):
        _parent = ".".join(_parts[:_i])
        if _parent not in sys.modules:
            _make_stub(_parent)
    if _stub_name not in sys.modules:
        _make_stub(_stub_name)

# ---------------------------------------------------------------------------
# Shared test payload constants (Phase 3)
# ---------------------------------------------------------------------------

# Real ASCII payload from maintainer's HTV245FRF device.
# Format: [flags],[rssi],[flags];[zone1_data]|[zone2_data]
SAMPLE_HTV245_ASCII_PAYLOAD = "1,-84,1;0,149,0,0,0,0|0,6,0,0,0,0"

# Synthetic TLV payload for HTV245FRF device (11# prefix).
# Constructed from TLV spec to exercise all code paths:
#   DP 0x18 type 0xDC value 0x01 (hub online)
#   DP 0x19 type 0xD8 value 0x01 (zone 1 open)
#   DP 0x1A type 0xD8 value 0x00 (zone 2 closed)
#   DP 0x25 type 0xAD value 0x3C00 (zone 1 duration = 60s, little-endian)
#   DP 0x26 type 0xAD value 0x0000 (zone 2 duration = 0s)
SAMPLE_HTV245_TLV_PAYLOAD = "11#18dc0119d8011ad80025ad3c0026ad0000"
