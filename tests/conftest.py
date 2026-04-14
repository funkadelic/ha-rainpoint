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
# Provide real Python base classes for HA entity hierarchy.
#
# MagicMock-backed stubs work fine for *attribute access* on instances, but
# multi-inheritance from several MagicMock objects fails at class-definition
# time with "metaclass conflict".  The entity platform modules
# (valve.py, number.py, hub_entities.py, etc.) inherit from combinations of
# CoordinatorEntity, ValveEntity, SensorEntity, NumberEntity, SelectEntity,
# SwitchEntity, and RestoreEntity.  We replace those specific attributes with
# minimal real Python classes so Python's class machinery is happy.
# ---------------------------------------------------------------------------


class _HABaseEntity:
    """Lightweight stand-in for homeassistant.helpers.entity.Entity."""

    _attr_should_poll = False
    _attr_entity_category = None
    _attr_unique_id = None
    _attr_name = None


class _CoordinatorEntity(_HABaseEntity):
    """Minimal CoordinatorEntity stand-in.

    Real signature: CoordinatorEntity.__init__(self, coordinator, context=None).
    We capture the coordinator and ignore the rest so that sub-classes that
    call super().__init__(coordinator) work without error.
    """

    def __init__(self, coordinator=None, context=None):
        self.coordinator = coordinator


class _RestoreEntity:
    """Minimal RestoreEntity stand-in.

    Inherits from object (not _HABaseEntity) to avoid MRO conflicts when
    combined with CoordinatorEntity and NumberEntity/SensorEntity which also
    both trace back to _HABaseEntity.
    """

    async def async_added_to_hass(self):
        pass

    async def async_get_last_state(self):
        return None


# Patch the stub modules with real classes so multi-inheritance works.
sys.modules["homeassistant.helpers.update_coordinator"].CoordinatorEntity = _CoordinatorEntity
sys.modules["homeassistant.helpers.entity"].Entity = _HABaseEntity
sys.modules["homeassistant.helpers.restore_state"].RestoreEntity = _RestoreEntity

# Entity platform base classes — all get the same lightweight base so that
# multi-inheritance combos like (CoordinatorEntity, ValveEntity) are clean.
sys.modules["homeassistant.components.valve"].ValveEntity = _HABaseEntity
sys.modules["homeassistant.components.valve"].ValveEntityFeature = MagicMock()
sys.modules["homeassistant.components.sensor"].SensorEntity = _HABaseEntity
sys.modules["homeassistant.components.sensor"].SensorDeviceClass = MagicMock()
sys.modules["homeassistant.components.sensor"].SensorStateClass = MagicMock()
sys.modules["homeassistant.components.number"].NumberEntity = _HABaseEntity
sys.modules["homeassistant.components.number"].NumberMode = MagicMock()
sys.modules["homeassistant.components.select"].SelectEntity = _HABaseEntity
sys.modules["homeassistant.components.switch"].SwitchEntity = _HABaseEntity

# DeviceInfo is used as a callable that returns its kwargs dict — use identity.
class _DeviceInfo(dict):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)

sys.modules["homeassistant.helpers.device_registry"].DeviceInfo = _DeviceInfo

# HomeAssistantError must be a real exception class so `raise HomeAssistantError(...)` works.
class _HomeAssistantError(Exception):
    pass

sys.modules["homeassistant.exceptions"].HomeAssistantError = _HomeAssistantError

# EntityCategory is accessed as EntityCategory.DIAGNOSTIC / .CONFIG — use a simple namespace.
class _EntityCategory:
    DIAGNOSTIC = "diagnostic"
    CONFIG = "config"

sys.modules["homeassistant.const"].EntityCategory = _EntityCategory
sys.modules["homeassistant.const"].PERCENTAGE = "%"
sys.modules["homeassistant.const"].SIGNAL_STRENGTH_DECIBELS_MILLIWATT = "dBm"
sys.modules["homeassistant.const"].UnitOfTime = MagicMock()

