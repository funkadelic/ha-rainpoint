"""Two scans pinning the entity-naming rule over the whole package.

The rule has two halves and each half is bound here by its own scan, because
either half alone lets the display defect back in:

1. No ``_attr_name`` assignment in ``custom_components/rainpoint`` may
   interpolate a device name, judged against the closed set of expression
   shapes ``_expr_is_a_device_name_source`` recognises. That set is named
   rather than absolute: a spelling nobody has thought of can still slip
   past, so a new shape found in review belongs in the predicate and in
   ``TestTheScanItselfHasTeeth`` alongside the ones already there.
2. Every entity class in the package must resolve ``has_entity_name`` to
   True. A platform added later with a correct short name but no flag would
   satisfy the first scan and still display with no device context at all,
   which is the same defect from the other direction.

This is the other half of the naming rule from
``tests/test_entity_naming_composition.py``, and deliberately shares nothing
with it. That module proves the rule is followed *in letter* by constructing
entities against a real Home Assistant registry; this module reads source
text only and runs its flag sweep out of process, so it is not hostage to the
conftest stub import ordering the composition module's own module-level guard
depends on, and it is the half that binds a platform nobody has written yet
-- a fixture-driven test can say nothing about a platform it never
constructs.

This module itself imports nothing from ``homeassistant`` and nothing from
``custom_components.rainpoint``: the source scan walks each module's AST from
its source text, and the flag sweep runs in a child interpreter. That child
is where the real Home Assistant base classes are in play, which is the
point: the repository conftest replaces ``Entity`` and every platform base
with lightweight stubs, so an in-process sweep would read a class hierarchy
that does not exist in production.
"""

from __future__ import annotations

import ast
import inspect
import json
import os
import subprocess
import sys
from functools import lru_cache
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
_PACKAGE_ROOT = _REPO_ROOT / "custom_components" / "rainpoint"

# Every module that composes an entity display name. Used by two independent
# checks: that the file walk reaches each of them, and that each still
# contributes at least one _attr_name assignment, so the scan cannot quietly
# come to sweep an empty population from either direction. A platform joins
# this list the day it gains its first _attr_name assignment.
_NAMING_MODULES = (
    "sensor.py",
    "hub_entities.py",
    "diagnostic_sensors.py",
    "number.py",
    "valve.py",
    "select.py",
    "generic_entities.py",
    "generic_control.py",
    "binary_sensor.py",
)

_BARE_DEVICE_NAMES = frozenset({"sub_name", "device_name", "hub_name"})
_DEVICE_NAME_KEYS = frozenset({"sub_name", "name"})
_SELF_DEVICE_NAME_ATTRS = frozenset({"_attr_name", "_device_name_prefix"})


def _expr_is_a_device_name_source(expr: ast.expr) -> bool:
    """Return True when expr, taken alone, reads as a device name.

    The recognised shapes, and why each is on the list:

    - a bare name, one of ``sub_name``, ``device_name`` or ``hub_name``;
    - a ``.get(...)`` call whose first argument is the string literal
      ``sub_name`` or ``name``, covering both the inlined sub-device read and
      the hub record read;
    - a subscript with either of those two string keys, ``info["sub_name"]``,
      which is an ordinary way to write the same read;
    - ``self._attr_name``, the inherited-name append shape the hub entities
      used before the conversion;
    - ``self._device_name_prefix``, the retired per-platform prefix, kept
      here so reintroducing it is caught rather than silently permitted.

    Two composites are unwrapped and their operands checked independently: a
    ``BoolOp`` (an ``or`` fallback such as ``hub_info.get("name") or "..."``,
    exactly how three of the ten converted hub sites used to read) and a
    ``BinOp`` (string concatenation such as ``sub_name + " Battery"``, which
    reaches the same result without an f-string).

    Anything not on this list -- a zone number, a rain window, a passthrough
    reading key, a model string -- is free to interpolate; the rule
    discriminates on what is interpolated, not on whether an f-string is used
    at all.
    """
    if isinstance(expr, ast.BoolOp):
        return any(_expr_is_a_device_name_source(value) for value in expr.values)
    if isinstance(expr, ast.BinOp):
        return any(_expr_is_a_device_name_source(side) for side in (expr.left, expr.right))
    if isinstance(expr, ast.Name):
        return expr.id in _BARE_DEVICE_NAMES
    if isinstance(expr, ast.Subscript):
        return isinstance(expr.slice, ast.Constant) and expr.slice.value in _DEVICE_NAME_KEYS
    if isinstance(expr, ast.Attribute):
        return expr.attr in _SELF_DEVICE_NAME_ATTRS and isinstance(expr.value, ast.Name) and expr.value.id == "self"
    if isinstance(expr, ast.Call) and isinstance(expr.func, ast.Attribute) and expr.func.attr == "get":
        first_arg = expr.args[0] if expr.args else None
        return isinstance(first_arg, ast.Constant) and first_arg.value in _DEVICE_NAME_KEYS
    return False


def _interpolates_a_device_name(node: ast.Assign | ast.AnnAssign) -> str | None:
    """Return the offending expression's source text, or None.

    For an f-string (``JoinedStr``) value, every interpolated
    (``FormattedValue``) expression is checked in turn. For any other value,
    the value itself is checked -- this is what catches a bare
    ``self._attr_name = sub_name`` with no f-string at all, and a
    concatenation with no f-string either.
    """
    value = node.value
    if value is None:
        return None
    if isinstance(value, ast.JoinedStr):
        for part in value.values:
            if isinstance(part, ast.FormattedValue) and _expr_is_a_device_name_source(part.value):
                return ast.unparse(part.value)
        return None
    if _expr_is_a_device_name_source(value):
        return ast.unparse(value)
    return None


def _iter_attr_name_assignments(source: str):
    """Yield every assignment node whose target is self._attr_name or a
    class-level _attr_name.

    Takes no skip-list parameter and applies no per-file or per-class
    exclusion: a single unconverted platform is exactly what would force
    such a parameter into existence, so its absence is part of the proof
    that the conversion was total.
    """
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):
            continue
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        for target in targets:
            is_class_level = isinstance(target, ast.Name) and target.id == "_attr_name"
            is_self_attr = (
                isinstance(target, ast.Attribute)
                and target.attr == "_attr_name"
                and isinstance(target.value, ast.Name)
                and target.value.id == "self"
            )
            if is_class_level or is_self_attr:
                yield node


def _scan_module_for_device_name_violations(path: Path) -> list[tuple[Path, int, str]]:
    """Return (path, lineno, offending expression) for every violation in one module.

    Takes no skip-list parameter, matching _iter_attr_name_assignments: the
    module this reads and the module it is defined in are the same
    population, no subset of either is ever excluded by name.
    """
    source = path.read_text()
    return [
        (path, node.lineno, offending)
        for node in _iter_attr_name_assignments(source)
        if (offending := _interpolates_a_device_name(node)) is not None
    ]


def _all_package_modules() -> list[Path]:
    """Return every Python module under the package, including debug.py.

    debug.py is dead in shipped builds but is scanned like every other
    module and passes on its own merits, which is why nothing in this file
    carves it out.
    """
    return sorted(_PACKAGE_ROOT.rglob("*.py"))


# Runs in a child interpreter, so it is written as source text rather than as
# a function: the parent process is under the repository conftest, whose stub
# Entity carries no has_entity_name at all, and reading the flag off a stub
# hierarchy would prove nothing about the shipped one. Discovery is by
# package walk rather than by a hand-written module list, because a
# hand-written list is precisely what a newly added platform would be missing
# from.
_REAL_HA_FLAG_SWEEP = """
import importlib
import inspect
import json
import pkgutil
import sys

from homeassistant.helpers.entity import Entity

import custom_components.rainpoint as package

scanned = []
offenders = []
for info in pkgutil.walk_packages(package.__path__, package.__name__ + "."):
    module = importlib.import_module(info.name)
    for name, obj in vars(module).items():
        if not inspect.isclass(obj) or obj.__module__ != module.__name__:
            continue
        if not issubclass(obj, Entity):
            continue
        qualified = info.name + "." + name
        scanned.append(qualified)
        # __new__ without __init__: has_entity_name reads only the class
        # attribute, so no constructor arguments have to be invented for 70+
        # classes, and the value read is the one the resolved MRO supplies.
        try:
            resolved = object.__new__(obj).has_entity_name
        except Exception as err:
            resolved = repr(err)
        if resolved is not True:
            offenders.append(qualified + " resolves has_entity_name to " + repr(resolved))

json.dump({"scanned": scanned, "offenders": offenders}, sys.stdout)
"""


@lru_cache(maxsize=1)
def _real_ha_flag_sweep() -> dict:
    """Run the flag sweep in a child interpreter and return its result.

    Cached because two tests read the same result and the child pays a real
    Home Assistant import each time it starts.
    """
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(filter(None, [str(_REPO_ROOT), env.get("PYTHONPATH", "")]))
    completed = subprocess.run(
        [sys.executable, "-c", _REAL_HA_FLAG_SWEEP],
        cwd=_REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=300,
    )
    if completed.returncode != 0:
        pytest.fail(
            "The has_entity_name sweep could not run against the real Home Assistant.\n"
            f"exit code: {completed.returncode}\nstdout:\n{completed.stdout}\nstderr:\n{completed.stderr}"
        )
    return json.loads(completed.stdout)


class TestNoAttrNameCarriesADeviceName:
    """The scan over the real package."""

    def test_no_module_interpolates_a_device_name(self):
        """Run the violation scan over every module the package walk reaches
        and fail with a per-line report if any _attr_name assignment still
        interpolates a device name."""
        violations = []
        for path in _all_package_modules():
            violations.extend(_scan_module_for_device_name_violations(path))
        if violations:
            report = "\n".join(
                f"{path.relative_to(_REPO_ROOT)}:{lineno}: "
                f"_attr_name interpolates {expr!r}, which reads as a device name. "
                "Carry only the entity's own short name and let Home Assistant "
                "compose the device name."
                for path, lineno, expr in violations
            )
            pytest.fail(report)


class TestTheScanItselfHasTeeth:
    """Fail-first proof: the predicate is run over synthetic sources first, so
    a clean scan of the real package above is evidence rather than a
    tautology. Deliberately its own test class so a reviewer can see the
    teeth are proven before trusting the verdict on the package."""

    @pytest.mark.parametrize(
        "source",
        [
            'self._attr_name = f"{sub_name} Zone 1"',
            "self._attr_name = f\"{sensor_info.get('sub_name', 'Sensor')} Battery\"",
            "self._attr_name = f\"{hub_info.get('name') or 'RainPoint Hub'} Device ID\"",
            "self._attr_name = f\"{sensor_info['sub_name']} Zone 1\"",
            "self._attr_name = f\"{self._sensor_info['name']} Battery\"",
            'self._attr_name = sub_name + " Battery"',
            "self._attr_name = sensor_info['sub_name'] + f\" Zone {zone_num}\"",
            'self._attr_name = f"{self._attr_name} Signal Strength"',
            'self._attr_name = f"{self._device_name_prefix} Duration"',
            "self._attr_name = sub_name",
        ],
    )
    def test_violating_sources_are_flagged(self, source):
        """Each parametrized source is an _attr_name assignment that reads as
        a device name in one of the recognised shapes; assert the predicate
        catches every one of them."""
        assignments = list(_iter_attr_name_assignments(source))
        assert len(assignments) == 1
        assert _interpolates_a_device_name(assignments[0]) is not None

    @pytest.mark.parametrize(
        "source",
        [
            'self._attr_name = "Transmission Power"',
            'self._attr_name = f"Rain ({window_fmt})"',
            'self._attr_name = f"Zone {zone_num} Duration"',
            'self._attr_name = f"Unsupported ({model})"',
            'self._attr_name = f"{zone}{spec.label} (unverified)"',
            "self._attr_name = str(reading_key)",
            "self._attr_name = f\"{readings['temperature']} Outside\"",
            'self._attr_name = "Zone " + str(zone_num)',
        ],
    )
    def test_clean_sources_are_not_flagged(self, source):
        """Each parametrized source assigns _attr_name from something that is
        not a device name -- a zone number, a rain window, a model string, a
        raw reading key; assert the predicate leaves all of them unflagged,
        the companion direction that keeps the scan from just flagging
        everything it sees."""
        assignments = list(_iter_attr_name_assignments(source))
        assert len(assignments) == 1
        assert _interpolates_a_device_name(assignments[0]) is None


class TestEveryEntityClassResolvesHasEntityName:
    """The second half of the rule: a short name with no flag is still wrong.

    Read through the public ``has_entity_name`` property rather than through
    the ``_attr_has_entity_name`` backing attribute, because the property is
    the surface Home Assistant itself consults and the two are not the same
    read: under the real base the property is generated by a metaclass and
    resolves through the whole MRO, which is what makes the four hub families
    interesting -- their flag-bearing base sits *last* in the MRO, so
    correctness there depends on no Home Assistant mixin ahead of it defining
    the same name.
    """

    def test_no_entity_class_leaves_the_flag_unresolved(self):
        """Assert the child-interpreter sweep, run against the real Home
        Assistant base classes, found no entity class in the package that
        resolves has_entity_name to anything other than True."""
        offenders = _real_ha_flag_sweep()["offenders"]
        assert not offenders, "\n".join(
            [
                "Every entity class must resolve has_entity_name to True, so its display",
                "name composes under the device rather than standing alone:",
                *offenders,
            ]
        )

    def test_the_sweep_reached_every_platform_the_rule_binds(self):
        """Non-vacuity: a sweep that discovered nothing would pass the check above.

        The named classes are one per entity family, including the button,
        whose real base is replaced by a flat stub inside the test process and
        so is only ever exercised against its shipped base here.
        """
        scanned = set(_real_ha_flag_sweep()["scanned"])
        required = (
            "custom_components.rainpoint.valve.RainPointValveEntity",
            "custom_components.rainpoint.number.RainPointZoneDurationNumber",
            "custom_components.rainpoint.select.RainPointSubDevicePowerSelect",
            "custom_components.rainpoint.sensor.RainPointNotReportingSensor",
            "custom_components.rainpoint.diagnostic_sensors.RainPointBatterySensor",
            "custom_components.rainpoint.generic_entities.RainPointGenericSensor",
            "custom_components.rainpoint.generic_control.RainPointGenericSwitch",
            "custom_components.rainpoint.hub_entities.RainPointHubRSSISensor",
            "custom_components.rainpoint.hub_entities.RainPointHubChannelSelect",
            "custom_components.rainpoint.hub_entities.RainPointHubBroadcastSwitch",
            "custom_components.rainpoint.hub_entities.RainPointHubBroadcastButton",
            "custom_components.rainpoint.hub_entities.RainPointHubConnectivityBinarySensor",
        )
        assert set(required) <= scanned, sorted(set(required) - scanned)


class TestScanIsNotVacuous:
    """A refactor that moves _attr_name assignments somewhere the walk does
    not reach must make this class fail rather than let the scan above pass
    silently over an empty population."""

    def test_visited_modules_cover_the_packages_own_files(self):
        """The file walk is compared against an independent enumeration.

        ``_all_package_modules`` walks recursively; the comparison set is one
        flat directory listing, built without calling the function under
        test, so a skip introduced inside that function shows up here as a
        missing name rather than being mirrored into both sides.
        """
        visited = {path.name for path in _all_package_modules()}
        on_disk = {path.name for path in _PACKAGE_ROOT.iterdir() if path.suffix == ".py"}

        assert on_disk, "the package directory yielded no Python modules at all"
        assert on_disk <= visited, sorted(on_disk - visited)
        for required in (*_NAMING_MODULES, "device.py", "entity.py"):
            assert required in visited, required

    def test_every_naming_module_still_contributes_an_assignment(self):
        """Asserts on the population the rule cares about, not a raw total.

        A raw floor breaks on a legitimate consolidation (moving a family
        onto an entity description drops the count without weakening
        anything), while a module that stopped contributing entirely is the
        drift worth catching, from either direction.
        """
        for module_name in _NAMING_MODULES:
            source = (_PACKAGE_ROOT / module_name).read_text()
            assert list(_iter_attr_name_assignments(source)), module_name


class TestScanCarriesNoBypassMechanism:
    """A single unconverted platform is what would force a skip list into
    existence, so its absence is the check that the conversion was total."""

    def test_entry_points_take_no_skip_list_parameter(self):
        """Assert both scan entry points accept only their one
        file-identifying parameter, so no caller can thread in a skip list
        to exempt a module or class from the scan."""
        assert list(inspect.signature(_iter_attr_name_assignments).parameters) == ["source"]
        assert list(inspect.signature(_scan_module_for_device_name_violations).parameters) == ["path"]

    @pytest.mark.parametrize("file_name", ["sensor.py", "hub_entities.py", "some_new_platform.py"])
    def test_a_bad_assignment_is_reported_whatever_the_file_is_called(self, tmp_path, file_name):
        """Behavioural check that no file is excluded by name.

        The parametrised names include the two largest real modules, so a
        guard such as ``if path.name == "sensor.py": continue`` inside the
        scan fails here rather than silently shrinking the population the
        package scan runs over.
        """
        path = tmp_path / file_name
        path.write_text('class Thing:\n    def __init__(self, sub_name):\n        self._attr_name = f"{sub_name} Battery"\n')

        violations = _scan_module_for_device_name_violations(path)

        assert [(lineno, expr) for _, lineno, expr in violations] == [(3, "sub_name")]

    def test_a_clean_file_is_reported_clean(self, tmp_path):
        """The companion direction, so the check above cannot pass by reporting everything."""
        path = tmp_path / "sensor.py"
        path.write_text('class Thing:\n    def __init__(self, zone_num):\n        self._attr_name = f"Zone {zone_num}"\n')

        assert _scan_module_for_device_name_violations(path) == []
