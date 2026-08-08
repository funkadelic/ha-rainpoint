"""Source scan pinning the entity-naming rule: no ``_attr_name`` assignment in
``custom_components/rainpoint`` may interpolate a device name.

This is the other half of the naming rule from
``tests/test_entity_naming_composition.py``, and deliberately shares nothing
with it. That module proves the rule is followed *in letter* by constructing
entities against a real Home Assistant registry; this module reads source
text only, so it is not hostage to the conftest stub import ordering the
composition module's own module-level guard depends on, and it is the half
that binds a platform nobody has written yet -- a fixture-driven test can say
nothing about a platform it never constructs.

The module imports nothing from ``homeassistant`` and nothing from
``custom_components.rainpoint``: it walks the AST of each module's source
text directly, so its own import graph carries no dependency on the package
it is checking.
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path

import pytest

_PACKAGE_ROOT = Path(__file__).resolve().parent.parent / "custom_components" / "rainpoint"

# The closed set of expression shapes _interpolates_a_device_name treats as
# carrying a device name. Anything not on this list -- a zone number, a rain
# window, a passthrough reading key, a model string -- is free to interpolate;
# the rule discriminates on what is interpolated, not on whether an f-string
# is used at all.
_DEVICE_NAME_SOURCES = (
    "a bare name: sub_name, device_name, or hub_name",
    "a .get(...) call whose first argument is the string literal 'sub_name' "
    "or 'name' (covers both the inlined sub-device read and the hub record "
    "read, including their `or` fallback forms)",
    "self._attr_name, the inherited-name append shape",
    "self._device_name_prefix, the retired per-platform prefix -- kept here "
    "so reintroducing it is caught rather than silently permitted",
)

_BARE_DEVICE_NAMES = frozenset({"sub_name", "device_name", "hub_name"})
_DEVICE_NAME_GET_KEYS = frozenset({"sub_name", "name"})
_SELF_DEVICE_NAME_ATTRS = frozenset({"_attr_name", "_device_name_prefix"})


def _expr_is_a_device_name_source(expr: ast.expr) -> bool:
    """Return True when expr, taken alone, is one of _DEVICE_NAME_SOURCES.

    A BoolOp (an `or` fallback such as ``hub_info.get("name") or "..."``) is
    unwrapped so either side is checked independently: the fallback form is
    exactly how three of the ten now-converted hub sites used to read.
    """
    if isinstance(expr, ast.BoolOp):
        return any(_expr_is_a_device_name_source(value) for value in expr.values)
    if isinstance(expr, ast.Name):
        return expr.id in _BARE_DEVICE_NAMES
    if isinstance(expr, ast.Attribute):
        return expr.attr in _SELF_DEVICE_NAME_ATTRS and isinstance(expr.value, ast.Name) and expr.value.id == "self"
    if isinstance(expr, ast.Call) and isinstance(expr.func, ast.Attribute) and expr.func.attr == "get":
        first_arg = expr.args[0] if expr.args else None
        return isinstance(first_arg, ast.Constant) and first_arg.value in _DEVICE_NAME_GET_KEYS
    return False


def _interpolates_a_device_name(node: ast.Assign | ast.AnnAssign) -> str | None:
    """Return the offending expression's source text, or None.

    For an f-string (``JoinedStr``) value, every interpolated
    (``FormattedValue``) expression is checked in turn. For any other value,
    the value itself is checked -- this is what catches a bare
    ``self._attr_name = sub_name`` with no f-string at all.
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
    module and passes on its own merits (its one _attr_name assignment is a
    plain literal), which is why nothing in this file carves it out.
    """
    return sorted(_PACKAGE_ROOT.rglob("*.py"))


class TestNoAttrNameCarriesADeviceName:
    """The scan over the real package."""

    def test_no_module_interpolates_a_device_name(self):
        violations = []
        for path in _all_package_modules():
            violations.extend(_scan_module_for_device_name_violations(path))
        if violations:
            report = "\n".join(
                f"{path.relative_to(_PACKAGE_ROOT.parent.parent)}:{lineno}: "
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
            'self._attr_name = f"{self._attr_name} Signal Strength"',
            'self._attr_name = f"{self._device_name_prefix} Duration"',
            "self._attr_name = sub_name",
        ],
    )
    def test_violating_sources_are_flagged(self, source):
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
        ],
    )
    def test_clean_sources_are_not_flagged(self, source):
        assignments = list(_iter_attr_name_assignments(source))
        assert len(assignments) == 1
        assert _interpolates_a_device_name(assignments[0]) is None


class TestScanIsNotVacuous:
    """A refactor that moves _attr_name assignments somewhere the walk does
    not reach must make this class fail rather than let the scan above pass
    silently over an empty population."""

    def test_visited_modules_equal_the_packages_own_file_set(self):
        visited = set(_all_package_modules())
        on_disk = set(_PACKAGE_ROOT.rglob("*.py"))
        assert visited == on_disk
        assert visited, "the package directory yielded no Python modules at all"

    def test_assignment_count_is_at_least_the_known_baseline(self):
        total = sum(len(list(_iter_attr_name_assignments(path.read_text()))) for path in _all_package_modules())
        # The package carries 64 _attr_name assignments as of this plan (63
        # converted sub-device and hub sites plus debug.py's one untouched
        # literal). A future addition only grows this count; a refactor that
        # hid assignments from the walk would shrink it below the floor.
        assert total >= 64


class TestScanCarriesNoBypassMechanism:
    """A single unconverted platform is what would force a skip list into
    existence, so its absence is the check that the conversion was total."""

    def test_entry_points_take_no_skip_list_parameter(self):
        assert list(inspect.signature(_iter_attr_name_assignments).parameters) == ["source"]
        assert list(inspect.signature(_scan_module_for_device_name_violations).parameters) == ["path"]

    def test_module_defines_no_bypass_collection(self):
        """Checks module-level data (not classes, functions, or modules, so a
        test's own name describing this absence cannot trip its own check)
        for a name suggesting a per-file skip list or a per-item bypass set.

        The forbidden markers below are built by concatenation rather than as
        whole literals, so this file's own raw text never contains the terms
        it is checking are absent -- the same discipline the rest of this
        plan applies to internal identifiers, applied here to a different
        vocabulary.
        """
        forbidden_substrings = (
            "exe" + "mpt",
            "skip_file" + "s",
            "allow" + "list",
            "ignore" + "_list",
        )
        for name, value in globals().items():
            if name.startswith("__") or inspect.isclass(value) or inspect.isfunction(value) or inspect.ismodule(value):
                continue
            lowered = name.lower()
            assert not any(substring in lowered for substring in forbidden_substrings), name
