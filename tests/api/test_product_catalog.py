"""Tests for the import-time, failure-tolerant product catalog loader."""

import json
from typing import ClassVar

import custom_components.rainpoint.api.product_catalog as product_catalog_module
from custom_components.rainpoint.api import get_catalog_entry
from custom_components.rainpoint.api.product_catalog import (
    UNCODED_VARIANT,
    _fingerprint_catalog,
    _load_catalog,
    _load_catalog_and_fingerprint,
    _normalize_model_variants,
    _normalize_variant_record,
    _parse_catalog,
    _read_catalog_bytes,
    get_catalog_fingerprint,
    get_catalog_port_number,
)
from tests.payload_samples import CATALOG_ANCHOR_MODEL


class TestLoadCatalogValid:
    """_load_catalog against a well-formed fixture file."""

    def test_valid_fixture_returns_dict_keyed_by_model_and_code(self, tmp_path):
        """A well-formed nested catalog survives the load unchanged."""
        catalog_path = tmp_path / "product_catalog.json"
        catalog_path.write_text(
            json.dumps({"SOME_MODEL": {"278": {"portNumber": 4, "dp": [{"dpCode": 1}]}}}),
            encoding="utf-8",
        )

        loaded = _load_catalog(catalog_path)

        assert loaded == {"SOME_MODEL": {"278": {"portNumber": 4, "dp": [{"dpCode": 1}]}}}

    def test_bare_dp_list_is_read_as_the_uncoded_bucket(self, tmp_path):
        """A pre-split catalog file still loads, as the model-level default."""
        catalog_path = tmp_path / "product_catalog.json"
        catalog_path.write_text(json.dumps({"SOME_MODEL": [{"dpCode": 1}]}), encoding="utf-8")

        loaded = _load_catalog(catalog_path)

        assert loaded == {"SOME_MODEL": {UNCODED_VARIANT: {"portNumber": None, "dp": [{"dpCode": 1}]}}}

    def test_variant_written_as_a_bare_dp_list_still_loads(self, tmp_path):
        """A catalog written before portNumber was hoisted keeps working.

        The dp entries are still usable; only the port count is unknown, and an
        unknown port count must read as None rather than 0.
        """
        catalog_path = tmp_path / "product_catalog.json"
        catalog_path.write_text(json.dumps({"SOME_MODEL": {"278": [{"dpCode": 1}]}}), encoding="utf-8")

        loaded = _load_catalog(catalog_path)

        assert loaded == {"SOME_MODEL": {"278": {"portNumber": None, "dp": [{"dpCode": 1}]}}}

    def test_numeric_model_codes_are_normalized_to_strings(self):
        """Variant keys are coerced to str, so an int-keyed mapping still resolves.

        Driven through the normalizer rather than a file, because JSON object
        keys are always strings: a round trip through _load_catalog could never
        hand the coercion a non-string key to work on.
        """
        assert _normalize_model_variants({278: {"portNumber": 1, "dp": [{"dpCode": 1}]}}) == {
            "278": {"portNumber": 1, "dp": [{"dpCode": 1}]}
        }

    def test_unusable_model_entry_is_skipped_not_fatal(self, tmp_path):
        """One malformed model does not cost the whole catalog."""
        catalog_path = tmp_path / "product_catalog.json"
        catalog_path.write_text(
            json.dumps({"GOOD": {"1": [{"dpCode": 1}]}, "BAD": "not a list or object"}),
            encoding="utf-8",
        )

        loaded = _load_catalog(catalog_path)

        assert set(loaded) == {"GOOD"}

    def test_model_entry_with_no_list_variants_is_skipped(self, tmp_path):
        """An object whose values are not dp lists carries nothing usable."""
        catalog_path = tmp_path / "product_catalog.json"
        catalog_path.write_text(json.dumps({"BAD": {"278": "not a list"}}), encoding="utf-8")

        assert _load_catalog(catalog_path) == {}

    def test_get_catalog_entry_unknown_model_returns_none(self):
        """A model the catalog has never heard of is a plain miss."""
        assert get_catalog_entry("TOTALLY_UNKNOWN_MODEL") is None

    def test_get_catalog_entry_none_model_returns_none(self):
        """Devices that report no model at all must not raise."""
        assert get_catalog_entry(None) is None


class TestVariantResolution:
    """get_catalog_entry must never attach one variant's metadata to another.

    RainPoint maps some model strings to several modelCodes whose port counts
    differ, so resolving a lookup to the wrong variant would put a bogus zone
    number on a diagnostic field. Every ambiguous case here resolves to None
    instead.
    """

    # These go straight into _CATALOG, which holds post-normalization state,
    # so they must be variant records rather than bare dp lists.
    _CODED: ClassVar[dict] = {"portNumber": 1, "dp": [{"dpCode": 1, "dpPort": 1}]}
    _OTHER_CODED: ClassVar[dict] = {"portNumber": 8, "dp": [{"dpCode": 1, "dpPort": 2}]}
    _UNCODED: ClassVar[dict] = {"portNumber": 3, "dp": [{"dpCode": 1, "dpPort": 9}]}

    def _install(self, monkeypatch, catalog):
        """Swap in a purpose-built catalog for the duration of one test."""
        monkeypatch.setattr(product_catalog_module, "_CATALOG", catalog)

    def test_exact_model_code_wins(self, monkeypatch):
        """A code listed in the catalog resolves to its own variant."""
        self._install(monkeypatch, {"HIC801W": {"278": self._CODED, "279": self._OTHER_CODED}})

        assert product_catalog_module.get_catalog_entry("HIC801W", 279) == self._OTHER_CODED["dp"]

    def test_integer_and_string_model_codes_resolve_alike(self, monkeypatch):
        """Devices report modelCode as an int; the catalog keys it as a string."""
        self._install(monkeypatch, {"HIC801W": {"278": self._CODED}})

        assert product_catalog_module.get_catalog_entry("HIC801W", 278) == self._CODED["dp"]
        assert product_catalog_module.get_catalog_entry("HIC801W", "278") == self._CODED["dp"]

    def test_unlisted_model_code_never_borrows_another_variant(self, monkeypatch):
        """The whole point: an unknown code must not resolve to a sibling's data."""
        self._install(monkeypatch, {"HIC801W": {"278": self._CODED, "279": self._OTHER_CODED}})

        assert product_catalog_module.get_catalog_entry("HIC801W", 999) is None

    def test_unlisted_model_code_falls_back_to_the_uncoded_bucket(self, monkeypatch):
        """A model-level default is a legitimate fallback; a sibling code is not."""
        self._install(monkeypatch, {"HIC801W": {"278": self._CODED, UNCODED_VARIANT: self._UNCODED}})

        assert product_catalog_module.get_catalog_entry("HIC801W", 999) == self._UNCODED["dp"]

    def test_missing_model_code_resolves_a_single_variant(self, monkeypatch):
        """With only one variant there is nothing to disambiguate."""
        self._install(monkeypatch, {"HCS702B": {"278": self._CODED}})

        assert product_catalog_module.get_catalog_entry("HCS702B") == self._CODED["dp"]

    def test_missing_model_code_refuses_to_guess_between_variants(self, monkeypatch):
        """A device that reports no modelCode gets no annotation, not a coin flip."""
        self._install(monkeypatch, {"HIC801W": {"278": self._CODED, "279": self._OTHER_CODED}})

        assert product_catalog_module.get_catalog_entry("HIC801W") is None

    def test_missing_model_code_uses_the_uncoded_bucket_when_present(self, monkeypatch):
        """The model-level default beats refusing to answer."""
        self._install(
            monkeypatch,
            {"HIC801W": {"278": self._CODED, "279": self._OTHER_CODED, UNCODED_VARIANT: self._UNCODED}},
        )

        assert product_catalog_module.get_catalog_entry("HIC801W") == self._UNCODED["dp"]

    def test_model_with_no_variants_returns_none(self, monkeypatch):
        """An empty variant map carries nothing to annotate with."""
        self._install(monkeypatch, {"HIC801W": {}})

        assert product_catalog_module.get_catalog_entry("HIC801W", 278) is None


class TestPortNumberResolution:
    """portNumber is a per-model property and must resolve per variant.

    Eight models in the shipped catalog map to two modelCodes whose port counts
    genuinely differ (HIC801W is 0 ports under 278 and 8 under 279), so reading
    the wrong variant's count would put a bogus zone count on a device.
    """

    _A: ClassVar[dict] = {"portNumber": 0, "dp": [{"dpCode": 1}]}
    _B: ClassVar[dict] = {"portNumber": 8, "dp": [{"dpCode": 1}]}

    def _install(self, monkeypatch, catalog):
        monkeypatch.setattr(product_catalog_module, "_CATALOG", catalog)

    def test_each_variant_reports_its_own_port_count(self, monkeypatch):
        """The two codes of one model string must not share a port count."""
        self._install(monkeypatch, {"HIC801W": {"278": self._A, "279": self._B}})

        assert get_catalog_port_number("HIC801W", 278) == 0
        assert get_catalog_port_number("HIC801W", 279) == 8

    def test_ambiguous_model_refuses_to_guess(self, monkeypatch):
        """No modelCode plus several variants is a miss, not a coin flip."""
        self._install(monkeypatch, {"HIC801W": {"278": self._A, "279": self._B}})

        assert get_catalog_port_number("HIC801W") is None

    def test_unknown_model_returns_none(self, monkeypatch):
        """A model the catalog never heard of has no declared port count."""
        self._install(monkeypatch, {"HIC801W": {"278": self._A}})

        assert get_catalog_port_number("TOTALLY_UNKNOWN") is None
        assert get_catalog_port_number(None) is None

    def test_zero_ports_is_distinct_from_unknown(self, monkeypatch):
        """0 means "RainPoint declares no ports"; None means "the catalog does not say".

        Collapsing the two would let a caller read a missing value as a real
        zero-port declaration.
        """
        self._install(monkeypatch, {"M": {"1": self._A, "2": {"portNumber": None, "dp": []}}})

        assert get_catalog_port_number("M", 1) == 0
        assert get_catalog_port_number("M", 2) is None


class TestNormalizeVariantRecord:
    """One malformed variant must degrade to a miss, never raise at import."""

    def test_dp_list_is_required(self):
        """A record with no usable dp list carries nothing to annotate with."""
        assert _normalize_variant_record({"portNumber": 4}) is None
        assert _normalize_variant_record({"portNumber": 4, "dp": "not a list"}) is None

    def test_non_integer_port_number_degrades_to_none(self):
        """A junk port count is dropped rather than propagated to callers."""
        assert _normalize_variant_record({"portNumber": "4", "dp": []}) == {"portNumber": None, "dp": []}

    def test_boolean_port_number_is_not_an_integer(self):
        """bool is an int subclass in Python; True must not become 1 port."""
        assert _normalize_variant_record({"portNumber": True, "dp": []}) == {"portNumber": None, "dp": []}

    def test_unusable_shapes_are_rejected(self):
        """Anything that is neither a record nor a bare dp list is a miss."""
        assert _normalize_variant_record("not a record") is None
        assert _normalize_variant_record(None) is None


class TestShippedCatalog:
    """Assertions against the committed snapshot itself, not a purpose-built fixture.

    These are what catch a regeneration that silently changes shape or scope;
    the fixture-driven tests above would keep passing through such a change.
    """

    def test_get_catalog_entry_returns_a_real_model_dp_list(self):
        """The shipped catalog resolves a real model to a usable dp list."""
        entry = get_catalog_entry(CATALOG_ANCHOR_MODEL)
        assert isinstance(entry, list)
        assert len(entry) > 0
        assert all("dpCode" in dp for dp in entry)

    def test_shipped_catalog_declares_only_status_and_control_identities(self):
        """The trim drops the provisioning/config namespaces the integration never reads.

        Shipping P_/C_/S_/ATTR_ entries would bloat a file that goes out to
        every user with metadata no code path consumes.
        """
        entry = get_catalog_entry(CATALOG_ANCHOR_MODEL)

        assert all(dp["identity"].startswith(("STA_", "CTL_")) for dp in entry)

    def test_shipped_catalog_reports_a_real_port_count(self):
        """End-to-end port-count resolution against the committed snapshot."""
        assert get_catalog_port_number(CATALOG_ANCHOR_MODEL) == 1


class TestLoadCatalogFailSoft:
    """_load_catalog must degrade to {} on every failure mode, never raise."""

    def test_missing_file_returns_empty_dict(self, tmp_path):
        """No catalog shipped is not an error, just no enrichment."""
        missing_path = tmp_path / "does-not-exist.json"

        assert _load_catalog(missing_path) == {}

    def test_corrupt_json_returns_empty_dict(self, tmp_path):
        """Unparseable JSON degrades instead of breaking component import."""
        corrupt_path = tmp_path / "corrupt.json"
        corrupt_path.write_text("{not valid json", encoding="utf-8")

        assert _load_catalog(corrupt_path) == {}

    def test_bytes_that_are_not_utf8_return_empty_dict(self, tmp_path):
        """One ValueError arm covers the decode too, since UnicodeDecodeError
        subclasses it. Pinned because that rests on the subclass relationship
        rather than on the exception being named."""
        undecodable_path = tmp_path / "undecodable.json"
        undecodable_path.write_bytes(b"\xff\xfe{}")

        assert _load_catalog(undecodable_path) == {}
        assert _parse_catalog(b"\xff\xfe{}") == {}

    def test_non_dict_json_returns_empty_dict(self, tmp_path):
        """A syntactically valid JSON array is rejected - the catalog must be an object."""
        non_dict_path = tmp_path / "list.json"
        non_dict_path.write_text(json.dumps([1, 2, 3]), encoding="utf-8")

        assert _load_catalog(non_dict_path) == {}

    def test_oversized_file_returns_empty_dict_without_parsing(self, tmp_path, monkeypatch):
        """A file over the size cap is rejected by its size, before any JSON parse."""
        monkeypatch.setattr(product_catalog_module, "_CATALOG_MAX_BYTES", 10)
        oversized_path = tmp_path / "oversized.json"
        oversized_path.write_text(json.dumps({"MODEL": [{"dpCode": 1}]}), encoding="utf-8")

        assert _load_catalog(oversized_path) == {}


class TestCatalogFingerprint:
    """The snapshot fingerprint identifies which catalog produced a reading."""

    @staticmethod
    def _fingerprint_path(path):
        """Fingerprint a file the way import time does: one read, then hash it."""
        return _fingerprint_catalog(_read_catalog_bytes(path))

    def test_fingerprint_is_twelve_hex_characters(self, tmp_path):
        """Short enough to sit in an entity attribute, and hex so it renders anywhere."""
        path = tmp_path / "catalog.json"
        path.write_text(json.dumps({"MODEL": {"1": {"portNumber": 1, "dp": []}}}), encoding="utf-8")

        fingerprint = self._fingerprint_path(path)

        assert len(fingerprint) == 12
        assert all(char in "0123456789abcdef" for char in fingerprint)

    def test_same_bytes_fingerprint_the_same_and_different_bytes_do_not(self, tmp_path):
        """The property the label rests on: it identifies one snapshot, not the release it shipped in."""
        first = tmp_path / "a.json"
        second = tmp_path / "b.json"
        third = tmp_path / "c.json"
        first.write_text('{"MODEL": {}}', encoding="utf-8")
        second.write_text('{"MODEL": {}}', encoding="utf-8")
        third.write_text('{"OTHER": {}}', encoding="utf-8")

        assert self._fingerprint_path(first) == self._fingerprint_path(second)
        assert self._fingerprint_path(first) != self._fingerprint_path(third)

    def test_unparseable_catalog_still_fingerprints(self, tmp_path):
        """Hashes raw bytes, so a file this loader degrades to {} is still identifiable.

        A report against a corrupt catalog has to be distinguishable from one
        against no catalog at all, and the parsed value cannot tell them apart.
        """
        path = tmp_path / "corrupt.json"
        path.write_text("{not valid json", encoding="utf-8")

        assert _load_catalog(path) == {}
        assert self._fingerprint_path(path) is not None

    def test_missing_file_fingerprints_as_none(self, tmp_path):
        """No bytes is not an empty hash, and a reading from no catalog carries no label."""
        assert self._fingerprint_path(tmp_path / "does-not-exist.json") is None

    def test_oversized_file_fingerprints_as_none_without_reading(self, tmp_path, monkeypatch):
        """The size cap is checked before the read, so an oversized file is never hashed."""
        monkeypatch.setattr(product_catalog_module, "_CATALOG_MAX_BYTES", 10)
        oversized_path = tmp_path / "oversized.json"
        oversized_path.write_text(json.dumps({"MODEL": [{"dpCode": 1}]}), encoding="utf-8")

        assert self._fingerprint_path(oversized_path) is None

    def test_the_fingerprint_describes_the_bytes_the_catalog_was_parsed_from(self, tmp_path):
        """One read serves both, so the label cannot describe a different read of the file."""
        path = tmp_path / "catalog.json"
        path.write_text(json.dumps({"MODEL": {"1": {"portNumber": 1, "dp": []}}}), encoding="utf-8")

        catalog, fingerprint = _load_catalog_and_fingerprint(path)

        assert catalog == _parse_catalog(path.read_bytes())
        assert fingerprint == _fingerprint_catalog(path.read_bytes())

    def test_a_missing_file_loads_as_an_empty_catalog_with_no_fingerprint(self, tmp_path):
        """Both halves degrade together, so nothing claims a snapshot that was never read."""
        catalog, fingerprint = _load_catalog_and_fingerprint(tmp_path / "nope.json")

        assert catalog == {}
        assert fingerprint is None

    def test_the_catalog_bytes_are_not_retained_at_module_level(self):
        """Up to _CATALOG_MAX_BYTES would otherwise be pinned for the process
        lifetime to produce twelve characters."""
        assert not hasattr(product_catalog_module, "_CATALOG_RAW")

    def test_a_path_with_an_embedded_null_degrades_rather_than_raising(self, tmp_path):
        """stat() raises ValueError, not OSError, and this runs at import time."""
        assert _read_catalog_bytes(tmp_path / "bad\x00name.json") is None

    def test_shipped_catalog_exposes_a_fingerprint(self):
        """The committed snapshot is readable, so the accessor is never None in a real install."""
        assert get_catalog_fingerprint() is not None


class TestGetCatalogVariantCodes:
    """get_catalog_variant_codes distinguishes an absent model from an unresolved variant.

    A plain lookup miss cannot tell those apart, but they need different fixes:
    extending the catalog snapshot versus getting the device's modelCode.
    """

    _CODED: ClassVar[dict] = {"portNumber": 2, "dp": [{"dpCode": 1, "identity": "STA_TEM", "dpPort": 1}]}

    @staticmethod
    def _install(monkeypatch, catalog):
        monkeypatch.setattr(product_catalog_module, "_CATALOG", catalog)

    def test_returns_empty_for_none_model(self):
        """A None model is a miss, not a crash."""
        assert product_catalog_module.get_catalog_variant_codes(None) == ()

    def test_returns_empty_for_unknown_model(self, monkeypatch):
        """A model the catalog does not carry reports no variants."""
        self._install(monkeypatch, {"HIC801W": {"278": self._CODED}})

        assert product_catalog_module.get_catalog_variant_codes("NOPE") == ()

    def test_returns_sorted_codes_for_a_known_model(self, monkeypatch):
        """Codes come back sorted so a message built from them is deterministic."""
        self._install(monkeypatch, {"HIC801W": {"279": self._CODED, "278": self._CODED}})

        assert product_catalog_module.get_catalog_variant_codes("HIC801W") == ("278", "279")

    def test_uncoded_bucket_is_reported_rather_than_omitted(self, monkeypatch):
        """A model carried only under the uncoded bucket still reports as present."""
        self._install(monkeypatch, {"HIC801W": {UNCODED_VARIANT: self._CODED}})

        assert product_catalog_module.get_catalog_variant_codes("HIC801W") == (UNCODED_VARIANT,)
