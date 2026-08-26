"""Test configuration for rainpoint integration tests.

This conftest stubs out Home Assistant and third-party HA dependencies so that
custom_components.rainpoint.api can be imported in plain pytest without a running
Home Assistant instance.
"""

import sys
from datetime import UTC
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
# Real stubs for update_coordinator: must be real classes so that
# RainPointCoordinator can inherit from DataUpdateCoordinator and be
# instantiated as a normal Python object.
#
# HomeAssistantError and ConfigEntryNotReady are defined here rather than with
# the rest of the homeassistant.exceptions stubs further down, because
# DataUpdateCoordinator.async_config_entry_first_refresh raises the latter.
# Their sys.modules registration still happens down there, once the stub
# modules exist to hang them on.
# ---------------------------------------------------------------------------


# HomeAssistantError must be a real exception class so `raise HomeAssistantError(...)` works.
class _HomeAssistantError(Exception):
    """_HomeAssistantError."""

    pass


class ConfigEntryNotReady(_HomeAssistantError):
    """Real ConfigEntryNotReady stub, raised by a failed first refresh.

    A subclass of the same HomeAssistantError stub the real exception
    derives from, so a test catching the base class still works.
    """

    pass


class DataUpdateCoordinator:
    """Minimal real DataUpdateCoordinator stub for tests."""

    def __init__(self, hass, logger, *, name, update_interval, config_entry=None):
        """Init helper.

        Mirrors the real signature closely enough that a drift like a missing
        config_entry kwarg fails here instead of at runtime in Home Assistant.
        last_update_success starts True, matching the real class, which is
        optimistic until the first refresh proves otherwise.
        """
        self.hass = hass
        self.logger = logger
        self.config_entry = config_entry
        self.data = None
        self.last_update_success = True
        self._listeners = []

    def async_add_listener(self, update_callback, context=None):
        """Register a listener and return the callable that removes it again."""
        self._listeners.append(update_callback)

        def _remove():
            """Remove the listener registered above."""
            if update_callback in self._listeners:
                self._listeners.remove(update_callback)

        return _remove

    def async_update_listeners(self):
        """Call every registered listener with no arguments."""
        for update_callback in list(self._listeners):
            update_callback()

    async def async_refresh(self):
        """Run one update cycle and notify listeners, as the real class does.

        Mirrors the real coordinator's failure handling: on success, data is
        replaced and last_update_success set True; on UpdateFailed,
        last_update_success is set False and data is left at its previous
        value rather than being cleared. Listeners are notified either way,
        unconditionally, matching the real class.

        The one deliberate narrowing: only UpdateFailed is caught here, where
        the real coordinator catches more broadly. RainPointCoordinator's own
        _async_update_data already funnels every error it can raise into
        UpdateFailed (coordinator.py's RainPointApiError and bare Exception
        except clauses both re-raise as UpdateFailed), so anything else
        escaping this stub is a harness bug in a test double or fixture and
        should stay loud rather than be swallowed into a false green.
        """
        try:
            self.data = await self._async_update_data()
        except UpdateFailed:
            self.last_update_success = False
        else:
            self.last_update_success = True
        self.async_update_listeners()

    async def async_config_entry_first_refresh(self):
        """Perform the first refresh of a config entry setup.

        Mirrors the real class's raise_on_entry_error path: a first refresh
        that fails raises ConfigEntryNotReady rather than leaving the config
        entry set up with no data. Checked via last_update_success rather
        than by re-catching UpdateFailed, since async_refresh above already
        swallows it before this method ever sees it.
        """
        await self.async_refresh()
        if not self.last_update_success:
            raise ConfigEntryNotReady("initial refresh failed")

    def async_set_updated_data(self, data) -> None:
        """Push data outside the poll cycle and notify listeners, as the real
        (synchronous, despite the name) coordinator method does.

        Used by a command response's optimistic-update path (valve.py). No
        _schedule_refresh reset here: nothing in this stub schedules a
        refresh to begin with.
        """
        self.data = data
        self.async_update_listeners()


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
    "homeassistant.helpers.selector",
    "homeassistant.helpers.entity",
    "homeassistant.components",
    "homeassistant.components.diagnostics",
    "homeassistant.components.persistent_notification",
    "homeassistant.components.select",
    "homeassistant.components.valve",
    "homeassistant.components.sensor",
    "homeassistant.components.binary_sensor",
    "homeassistant.components.number",
    "homeassistant.components.switch",
    "homeassistant.components.button",
    "homeassistant.components.update",
    "homeassistant.components.repairs",
    "homeassistant.const",
    "homeassistant.data_entry_flow",
    "homeassistant.exceptions",
    "homeassistant.helpers.device_registry",
    "homeassistant.helpers.entity_registry",
    "homeassistant.helpers.restore_state",
    "homeassistant.helpers.issue_registry",
    "homeassistant.helpers.event",
    "homeassistant.util",
    "homeassistant.util.dt",
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

# homeassistant.components.diagnostics.async_redact_data is a real function in
# Home Assistant, not a class or a constant, and diagnostics.py calls it on the
# structure it returns. A MagicMock attribute would make every diagnostics test
# assert against a mock's return value rather than against a payload, so this
# mirrors the real helper's semantics (HA 2026.2.3,
# components/diagnostics/util.py): recurse through mappings and lists, leave
# None and empty strings alone, replace a matched key's value with the marker.
#
# What this does NOT do is stand in as evidence that the real redactor works.
# The diagnostics tests are written to assert the shape handed to it and the
# key set it is given, which are this repo's to get right; the recursion itself
# is Home Assistant's.
_REDACTED_MARKER = "**REDACTED**"


def _stub_async_redact_data(data, to_redact):
    """Mirror homeassistant.components.diagnostics.util.async_redact_data."""
    if not isinstance(data, (dict, list)):
        return data
    if isinstance(data, list):
        return [_stub_async_redact_data(item, to_redact) for item in data]
    redacted = {**data}
    for key, value in redacted.items():
        if value is None or (isinstance(value, str) and not value):
            continue
        if key in to_redact:
            redacted[key] = _REDACTED_MARKER
        elif isinstance(value, dict):
            redacted[key] = _stub_async_redact_data(value, to_redact)
        elif isinstance(value, list):
            redacted[key] = [_stub_async_redact_data(item, to_redact) for item in value]
    return redacted


sys.modules["homeassistant.components.diagnostics"].async_redact_data = _stub_async_redact_data
sys.modules["homeassistant.components.diagnostics"].REDACTED = _REDACTED_MARKER

# Register the real update_coordinator stub (must come after the loop so that
# the parent "homeassistant.helpers" stub is already in sys.modules). Bind it
# as an attribute on the parent so ``from homeassistant.helpers import
# update_coordinator`` resolves to the same stub object.
_update_coordinator_stub = _make_update_coordinator_stub()
sys.modules["homeassistant.helpers.update_coordinator"] = _update_coordinator_stub
sys.modules["homeassistant.helpers"].update_coordinator = _update_coordinator_stub


# homeassistant.generated.countries.COUNTRIES is HA's authoritative ISO set that
# CountrySelector validates against. Stub it with a container that accepts any
# code so config-flow tests exercise the full picker; the real intersection is
# tested directly in test_country_codes.py with explicit sets.
class _AllCountries:
    def __contains__(self, item):
        return True


_generated_stub = _make_stub("homeassistant.generated")
sys.modules["homeassistant"].generated = _generated_stub
_countries_stub = _make_stub("homeassistant.generated.countries")
_generated_stub.countries = _countries_stub
_countries_stub.COUNTRIES = _AllCountries()


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
# directly from object, no shared root with Entity/CoordinatorEntity.
# This lets Python resolve any multi-inheritance combo without deadlock.
# ---------------------------------------------------------------------------


class _HABaseEntity:
    """Lightweight stand-in for homeassistant.helpers.entity.Entity."""

    _attr_should_poll = False
    _attr_entity_category = None
    _attr_unique_id = None
    _attr_name = None

    # _attr_has_entity_name is deliberately absent, matching Entity, which
    # only annotates it. The property below branches on whether a subclass
    # supplied one, so giving the stub a default here would make that branch
    # unreachable and let a class that never sets the flag read as if it had.

    @property
    def has_entity_name(self) -> bool:
        """Mirror Entity.has_entity_name, the surface Home Assistant reads.

        Home Assistant resolves the display-name rule through this property
        and never through the _attr_ backing attribute, so a test asserting
        on the backing attribute exercises a surface production does not
        consult. Real Entity generates this through a metaclass; a plain
        property is close enough for a stub as long as the resolution order
        matches, which is why the entity-description fallback is carried too.
        """
        if hasattr(self, "_attr_has_entity_name"):
            return self._attr_has_entity_name
        description = getattr(self, "entity_description", None)
        if description is not None:
            return description.has_entity_name
        return False

    @property
    def unique_id(self):
        """Mirror Entity.unique_id, the surface Home Assistant reads.

        Carried for the same reason as has_entity_name above: the registry
        consults the property, never the _attr_ backing attribute, so a test
        that asserts on the backing attribute is not testing what production
        reads. Purely additive, since without it any access raised.
        """
        return self._attr_unique_id

    @property
    def should_poll(self) -> bool:
        """Mirror Entity.should_poll, which decides whether a platform is polled."""
        return self._attr_should_poll

    async def async_will_remove_from_hass(self):
        """No-op teardown hook, matching Entity's awaitable base implementation."""


class _CoordinatorEntity(_HABaseEntity):
    """Minimal CoordinatorEntity stand-in.

    Real signature: CoordinatorEntity.__init__(self, coordinator, context=None).
    We capture the coordinator and ignore the rest so that sub-classes that
    call super().__init__(coordinator) work without error.
    """

    def __init__(self, coordinator=None, context=None):
        """Init helper."""
        self.coordinator = coordinator

    def _handle_coordinator_update(self) -> None:
        """Match the real hook's default body: write state on every coordinator update.

        Added when the hub broadcast switch became the package's first
        subclass to override this hook and call super() from its override;
        the stand-in previously had nothing for that super() call to reach.
        """
        self.async_write_ha_state()

    async def async_added_to_hass(self) -> None:
        """Register the coordinator listener, matching the real class's default wiring.

        Needed so a driven-timeline test's coordinator.async_refresh() calls
        actually invoke a subclass's _handle_coordinator_update, the way
        production's entity-platform add-entity flow does when it calls this
        hook on every added entity.
        """
        if self.coordinator is not None:
            self.coordinator.async_add_listener(self._handle_coordinator_update)

    def __class_getitem__(cls, _item):
        """Accept the generic parameter the real CoordinatorEntity takes.

        The platform entity bases subscript it (CoordinatorEntity[
        RainPointCoordinator]) so a type checker knows self.coordinator is a
        RainPointCoordinator rather than the bare DataUpdateCoordinator. That
        subscript is evaluated at class-definition time, so a stand-in that is
        not subscriptable fails every import in the suite. Returns the class
        itself, which is what Generic.__class_getitem__ resolves to for
        inheritance purposes.
        """
        return cls


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


# Platform entity base types: FLAT classes (object root only).
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


class _BinarySensorEntity:
    """_BinarySensorEntity."""

    pass


class _ButtonEntity:
    """Flat stand-in for homeassistant.components.button.ButtonEntity.

    Button was the one platform base this harness left resolving to the real
    Home Assistant class while the other six were stubbed, so the hub's button
    entity carried a different root from every one of its siblings. Stubbing
    it puts all seven platform bases on the same footing, which is what the
    flat-class scheme above depends on. The shipped hierarchy is proven
    elsewhere: tests/test_entity_naming.py sweeps every entity class in a
    child interpreter where the real bases are in play.
    """

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
sys.modules["homeassistant.components.binary_sensor"].BinarySensorEntity = _BinarySensorEntity
sys.modules["homeassistant.components.binary_sensor"].BinarySensorDeviceClass = MagicMock()


class _UpdateEntity:
    """Flat stand-in for homeassistant.components.update.UpdateEntity.

    Added for the same reason _ButtonEntity was: update arrived after that
    conversion and was then the only platform base still resolving to the real
    Home Assistant class, which left the hub's update entity rooted somewhere
    none of its siblings are. Worse than cosmetic here, because real UpdateEntity
    drags in real Entity and the resulting MRO skipped _HABaseEntity outright, so
    the harness was proving something about a hierarchy the flat-class scheme
    does not describe. The three value properties are carried because production
    reads them, not the _attr_ backing attributes.
    """

    _attr_installed_version = None
    _attr_latest_version = None
    _attr_release_summary = None

    @property
    def installed_version(self):
        """Mirror UpdateEntity.installed_version."""
        return self._attr_installed_version

    @property
    def latest_version(self):
        """Mirror UpdateEntity.latest_version."""
        return self._attr_latest_version

    @property
    def release_summary(self):
        """Mirror UpdateEntity.release_summary."""
        return self._attr_release_summary


sys.modules["homeassistant.components.button"].ButtonEntity = _ButtonEntity
sys.modules["homeassistant.components.update"].UpdateEntity = _UpdateEntity
sys.modules["homeassistant.components.update"].UpdateDeviceClass = MagicMock()
sys.modules["homeassistant.components.update"].UpdateEntityFeature = MagicMock()


# ---------------------------------------------------------------------------
# homeassistant.util.dt: real as_local, not a MagicMock.
#
# "homeassistant.util" is absent from the stub loop everywhere else in this
# package, and its parent "homeassistant" is a MagicMock, which refuses
# dunder attributes and so exposes no __path__ for the import machinery to
# walk. That means `from homeassistant.util import dt as dt_util` fails with
# ModuleNotFoundError unless "homeassistant.util" and "homeassistant.util.dt"
# are both named in _HA_STUBS above (giving the ancestor-package pass
# something to create and bind) -- and a MagicMock stand-in for as_local
# would return a MagicMock rather than a datetime, so a test asserting a
# timezone-aware result would pass while proving nothing. Both halves are
# required, the same two-part fix the
# homeassistant.components.repairs stub already needed.
#
# The behaviour mirrors the installed Home Assistant version (util/dt.py):
# a naive input gets DEFAULT_TIME_ZONE attached (never converted from UTC,
# since a naive value carries no "UTC" claim to convert from), an
# already-matching-zone input is returned unchanged, and anything else is
# converted with astimezone().
DEFAULT_TIME_ZONE = UTC


def _as_local(dattim):
    """Real stand-in for homeassistant.util.dt.as_local."""
    if dattim.tzinfo == DEFAULT_TIME_ZONE:
        return dattim
    if dattim.tzinfo is None:
        return dattim.replace(tzinfo=DEFAULT_TIME_ZONE)
    return dattim.astimezone(DEFAULT_TIME_ZONE)


sys.modules["homeassistant.util.dt"].DEFAULT_TIME_ZONE = DEFAULT_TIME_ZONE
sys.modules["homeassistant.util.dt"].as_local = _as_local


# issue_registry: real functions (MagicMock) so tests can assert create/delete
# calls, plus an IssueSeverity namespace accessed as IssueSeverity.WARNING.
class _IssueSeverity:
    """Stand-in for homeassistant.helpers.issue_registry.IssueSeverity."""

    CRITICAL = "critical"
    ERROR = "error"
    WARNING = "warning"


sys.modules["homeassistant.helpers.issue_registry"].IssueSeverity = _IssueSeverity
sys.modules["homeassistant.helpers.issue_registry"].async_create_issue = MagicMock()
sys.modules["homeassistant.helpers.issue_registry"].async_delete_issue = MagicMock()

# event.async_track_time_interval returns a cancel callback; the watchdog stores
# it and calls it on stop.
sys.modules["homeassistant.helpers.event"].async_track_time_interval = MagicMock(return_value=MagicMock())

# event.async_call_later likewise returns a cancel callback; the generic
# control path stores it and calls it before scheduling a new one / on
# entity removal.
sys.modules["homeassistant.helpers.event"].async_call_later = MagicMock(return_value=MagicMock())


# DeviceInfo: callable that stores kwargs as a dict subclass.
class _DeviceInfo(dict):
    """_DeviceInfo."""

    def __init__(self, **kwargs):
        """Init helper."""
        super().__init__(**kwargs)


sys.modules["homeassistant.helpers.device_registry"].DeviceInfo = _DeviceInfo


# Both classes are defined at the top of this file, next to the
# DataUpdateCoordinator stub that raises ConfigEntryNotReady.
sys.modules["homeassistant.exceptions"].HomeAssistantError = _HomeAssistantError
sys.modules["homeassistant.exceptions"].ConfigEntryNotReady = ConfigEntryNotReady


# EntityCategory is accessed as EntityCategory.DIAGNOSTIC / .CONFIG: use a simple namespace.
class _EntityCategory:
    """_EntityCategory."""

    DIAGNOSTIC = "diagnostic"
    CONFIG = "config"


sys.modules["homeassistant.const"].EntityCategory = _EntityCategory
# The literal Home Assistant writes onto the state of a registry row that no
# live entity object holds. Pinned to its real value rather than left as a
# MagicMock attribute because the leftover-row liveness gate compares the
# attribute this names against an exact True, and a MagicMock key would never
# match the one a test double puts in its attributes mapping.
sys.modules["homeassistant.const"].ATTR_RESTORED = "restored"
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


class _FakeOptionsFlow:
    """Minimal stand-in for homeassistant.config_entries.OptionsFlow.

    Real instances get `config_entry` assigned by the flow manager; tests
    set it directly on the instance before calling a step.
    """

    pass


sys.modules["homeassistant.config_entries"].OptionsFlow = _FakeOptionsFlow


# ---------------------------------------------------------------------------
# Real RepairsFlow base so repairs.py can subclass it. Both halves of this are
# load bearing, for the same two reasons the _FakeConfigFlow block above is: a
# MagicMock cannot be subclassed, and a MagicMock parent package exposes no
# __path__, so `from homeassistant.components.repairs import RepairsFlow` fails
# at import time unless the submodule is in _HA_STUBS *and* carries a real
# class. hass, issue_id and data are assigned by the real flow manager; they
# default to None here so a test can construct a flow directly and set only
# what it needs. The two step helpers return plain dicts carrying their
# arguments, so a test can assert on a returned step with no flow manager
# running.
# ---------------------------------------------------------------------------
class _FakeRepairsFlow:
    """Minimal stand-in for homeassistant.components.repairs.RepairsFlow."""

    hass = None
    issue_id = None
    data = None

    def async_show_form(self, *, step_id, data_schema=None, description_placeholders=None):
        """Return the form step as plain data."""
        return {
            "type": "form",
            "step_id": step_id,
            "data_schema": data_schema,
            "description_placeholders": description_placeholders,
        }

    def async_create_entry(self, *, title="", data=None):
        """Return the created entry as plain data."""
        return {"type": "create_entry", "title": title, "data": data}

    def async_abort(self, *, reason, description_placeholders=None):
        """Return the abort as plain data.

        Carries the same keyword-only signature as
        data_entry_flow.FlowHandler.async_abort, so a flow that aborts is
        exercised at the real call shape rather than at a stub's. The
        distinction between this and async_create_entry is behavioural rather
        than cosmetic: Home Assistant's repairs flow manager deletes a fixable
        issue on any non-abort result, so a step that aborts is a step whose
        card survives.
        """
        return {"type": "abort", "reason": reason, "description_placeholders": description_placeholders}


sys.modules["homeassistant.components.repairs"].RepairsFlow = _FakeRepairsFlow


class _FakeClientError(OSError):
    """Stand-in for aiohttp.ClientError."""


sys.modules["aiohttp"].ClientError = _FakeClientError


# ---------------------------------------------------------------------------
# homeassistant.core.callback: real identity decorator, not a MagicMock.
#
# `homeassistant.core` is a bare MagicMock stub (see _HA_STUBS above), so
# `from homeassistant.core import callback` would otherwise resolve to a
# MagicMock. Decorating a function with a MagicMock replaces it with the
# mock's return value instead of the original function, silently breaking
# any `@callback`-decorated method (e.g. api/mqtt.py's _handle_message).
# ---------------------------------------------------------------------------
def _identity_callback(func):
    """Real stand-in for homeassistant.core.callback -- returns func unchanged."""
    return func


sys.modules["homeassistant.core"].callback = _identity_callback

import tests.helpers  # noqa: E402, F401 (ensures helpers are importable in tests)
