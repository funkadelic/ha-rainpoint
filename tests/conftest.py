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


# ---------------------------------------------------------------------------
# Real stubs for update_coordinator — must be real classes so that
# RainPointCoordinator can inherit from DataUpdateCoordinator and be
# instantiated as a normal Python object.
# ---------------------------------------------------------------------------

class DataUpdateCoordinator:
    """Minimal real DataUpdateCoordinator stub for tests."""

    def __init__(self, hass, logger, name, update_interval):
        """Init helper."""
        self.hass = hass
        self.logger = logger


class UpdateFailed(Exception):
    """Real UpdateFailed exception stub for tests."""


def _make_update_coordinator_stub() -> ModuleType:
    """Make update coordinator stub helper."""
    mod = ModuleType("homeassistant.helpers.update_coordinator")
    mod.__name__ = "homeassistant.helpers.update_coordinator"
    mod.__spec__ = None
    mod.DataUpdateCoordinator = DataUpdateCoordinator
    mod.UpdateFailed = UpdateFailed
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
    # Bind the submodule as an attribute on its parent so that
    # ``from homeassistant import config_entries`` returns the stub in
    # sys.modules rather than a fresh auto-attribute on the parent MagicMock.
    # Normally Python's import machinery sets this attribute as a side-effect
    # of ``import pkg.sub``, but our parents are MagicMocks (not packages),
    # and when the submodule is already cached in sys.modules the side-effect
    # does not fire. Setting it explicitly makes the binding deterministic.
    if len(_parts) > 1:
        _parent_mod = sys.modules[".".join(_parts[:-1])]
        setattr(_parent_mod, _parts[-1], sys.modules[_stub_name])

# Register the real update_coordinator stub (must come after the loop so that
# the parent "homeassistant.helpers" stub is already in sys.modules). Bind it
# as an attribute on the parent so ``from homeassistant.helpers import
# update_coordinator`` resolves to the same stub object.
_update_coordinator_stub = _make_update_coordinator_stub()
sys.modules["homeassistant.helpers.update_coordinator"] = _update_coordinator_stub
sys.modules["homeassistant.helpers"].update_coordinator = _update_coordinator_stub


# ---------------------------------------------------------------------------
# Provide real Python base classes for HA entity hierarchy.
#
# MagicMock-backed stubs work fine for *attribute access* on instances, but
# multi-inheritance from several MagicMock objects fails at class-definition
# time with "metaclass conflict" or MRO errors.
#
# The entity platform modules inherit from combinations of:
#   CoordinatorEntity, ValveEntity, SensorEntity, NumberEntity,
#   SelectEntity, SwitchEntity, RestoreEntity, and device.py classes
#   (RainPointHubDevice) which themselves inherit from Entity.
#
# Key MRO constraint: hub_entities.py has
#   class RainPointHubSensorBase(CoordinatorEntity, SensorEntity, RainPointHubDevice)
# where RainPointHubDevice inherits Entity.  For C3 to succeed,
# SensorEntity must NOT share a common ancestor with Entity/RainPointHubDevice
# (otherwise the ordering constraint is circular).
#
# Solution: Entity, CoordinatorEntity, and RestoreEntity all share
# _HABaseEntity as root.  Platform entity types (ValveEntity, SensorEntity,
# NumberEntity, SelectEntity, SwitchEntity) are FLAT classes that inherit
# directly from object — no shared root with Entity/CoordinatorEntity.
# This lets Python resolve any multi-inheritance combo without deadlock.
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
        """Init helper."""
        self.coordinator = coordinator


class _RestoreEntity:
    """Minimal RestoreEntity stand-in.

    Inherits from object (not _HABaseEntity) to avoid MRO conflicts when
    combined with CoordinatorEntity and platform entity types.
    """

    async def async_added_to_hass(self):
        """Async added to hass."""
        pass

    async def async_get_last_state(self):
        """Async get last state."""
        return None


# Platform entity base types — FLAT classes (object root only).
# They must NOT share _HABaseEntity as a root because device.py's
# RainPointHubDevice inherits Entity (= _HABaseEntity), and combining
# (CoordinatorEntity→_HABaseEntity, PlatformType→_HABaseEntity,
# RainPointHubDevice→_HABaseEntity) creates an unresolvable C3 cycle.
class _ValveEntity:
    """_ValveEntity."""
    pass


class _SensorEntity:
    """_SensorEntity."""
    pass


class _NumberEntity:
    """_NumberEntity."""
    pass


class _SelectEntity:
    """_SelectEntity."""
    pass


class _SwitchEntity:
    """_SwitchEntity."""
    pass


# Patch the stub modules with real classes so multi-inheritance works.
sys.modules["homeassistant.helpers.update_coordinator"].CoordinatorEntity = _CoordinatorEntity
sys.modules["homeassistant.helpers.entity"].Entity = _HABaseEntity
sys.modules["homeassistant.helpers.restore_state"].RestoreEntity = _RestoreEntity

# Platform entity classes
sys.modules["homeassistant.components.valve"].ValveEntity = _ValveEntity
sys.modules["homeassistant.components.valve"].ValveEntityFeature = MagicMock()
sys.modules["homeassistant.components.sensor"].SensorEntity = _SensorEntity
sys.modules["homeassistant.components.sensor"].SensorDeviceClass = MagicMock()
sys.modules["homeassistant.components.sensor"].SensorStateClass = MagicMock()
sys.modules["homeassistant.components.number"].NumberEntity = _NumberEntity
sys.modules["homeassistant.components.number"].NumberMode = MagicMock()
sys.modules["homeassistant.components.select"].SelectEntity = _SelectEntity
sys.modules["homeassistant.components.switch"].SwitchEntity = _SwitchEntity

# DeviceInfo: callable that stores kwargs as a dict subclass.
class _DeviceInfo(dict):
    """_DeviceInfo."""
    def __init__(self, **kwargs):
        """Init helper."""
        super().__init__(**kwargs)


sys.modules["homeassistant.helpers.device_registry"].DeviceInfo = _DeviceInfo

# HomeAssistantError must be a real exception class so `raise HomeAssistantError(...)` works.
class _HomeAssistantError(Exception):
    """_HomeAssistantError."""
    pass


sys.modules["homeassistant.exceptions"].HomeAssistantError = _HomeAssistantError

# EntityCategory is accessed as EntityCategory.DIAGNOSTIC / .CONFIG — use a simple namespace.
class _EntityCategory:
    """_EntityCategory."""
    DIAGNOSTIC = "diagnostic"
    CONFIG = "config"


sys.modules["homeassistant.const"].EntityCategory = _EntityCategory
sys.modules["homeassistant.const"].PERCENTAGE = "%"
sys.modules["homeassistant.const"].SIGNAL_STRENGTH_DECIBELS_MILLIWATT = "dBm"
sys.modules["homeassistant.const"].UnitOfTime = MagicMock()


# ---------------------------------------------------------------------------
# Real ConfigFlow base + aiohttp.ClientError so that config_flow.py can be
# imported as a proper Python class (not a MagicMock subclass) in any test
# collection order. Applied here (before any test module is collected) rather
# than at the top of test_config_flow.py so that pytest ordering via -k,
# --last-failed, or pytest-xdist cannot change whether these mutations are
# visible to sibling test modules.
# ---------------------------------------------------------------------------
class _FakeConfigFlow:
    """Minimal stand-in for homeassistant.config_entries.ConfigFlow."""

    def __init_subclass__(cls, domain=None, **kwargs):
        """Init subclass helper."""
        super().__init_subclass__(**kwargs)


sys.modules["homeassistant.config_entries"].ConfigFlow = _FakeConfigFlow


class _FakeClientError(OSError):
    """Stand-in for aiohttp.ClientError."""


sys.modules["aiohttp"].ClientError = _FakeClientError

import tests.helpers  # noqa: E402, F401 — ensures helpers are importable in tests
