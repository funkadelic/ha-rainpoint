"""Tests for the import-time, failure-tolerant product catalog loader."""

import json
from typing import ClassVar

import custom_components.rainpoint.api.product_catalog as product_catalog_module
from custom_components.rainpoint.api import get_catalog_entry
from custom_components.rainpoint.api.product_catalog import UNCODED_VARIANT, _load_catalog


class TestLoadCatalogValid:
    """_load_catalog against a well-formed fixture file."""

    def test_valid_fixture_returns_dict_keyed_by_model_and_code(self, tmp_path):
        """A well-formed nested catalog survives the load unchanged."""
        catalog_path = tmp_path / "product_catalog.json"
        catalog_path.write_text(json.dumps({"SOME_MODEL": {"278": [{"dpCode": 1}]}}), encoding="utf-8")

        loaded = _load_catalog(catalog_path)

        assert loaded == {"SOME_MODEL": {"278": [{"dpCode": 1}]}}

    def test_bare_dp_list_is_read_as_the_uncoded_bucket(self, tmp_path):
        """A pre-split catalog file still loads, as the model-level default."""
        catalog_path = tmp_path / "product_catalog.json"
        catalog_path.write_text(json.dumps({"SOME_MODEL": [{"dpCode": 1}]}), encoding="utf-8")

        loaded = _load_catalog(catalog_path)

        assert loaded == {"SOME_MODEL": {UNCODED_VARIANT: [{"dpCode": 1}]}}

    def test_numeric_model_codes_are_normalized_to_strings(self, tmp_path):
        """JSON keys are always strings, but a hand-edited file may not be."""
        catalog_path = tmp_path / "product_catalog.json"
        catalog_path.write_text('{"SOME_MODEL": {"278": [{"dpCode": 1}]}}', encoding="utf-8")

        assert set(_load_catalog(catalog_path)["SOME_MODEL"]) == {"278"}

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

    def test_get_catalog_entry_returns_seeded_model_dp_list(self):
        """The shipped catalog carries the seeded bootstrap model."""
        entry = get_catalog_entry("HCS777ARF")
        assert isinstance(entry, list)
        assert len(entry) > 0
        assert all("dpCode" in dp for dp in entry)

    def test_get_catalog_entry_unknown_model_returns_none(self):
        """A model the catalog has never heard of is a plain miss."""
        assert get_catalog_entry("TOTALLY_UNKNOWN_MODEL") is None

    def test_get_catalog_entry_none_model_returns_none(self):
        """Devices that report no model at all must not raise."""
        assert get_catalog_entry(None) is None


class TestVariantResolution:
    """get_catalog_entry must never attach one variant's metadata to another.

    The vendor maps some model strings to several modelCodes whose port counts
    differ, so resolving a lookup to the wrong variant would put a bogus zone
    number on a diagnostic field. Every ambiguous case here resolves to None
    instead.
    """

    _CODED: ClassVar[list[dict]] = [{"dpCode": 1, "dpPort": 1}]
    _OTHER_CODED: ClassVar[list[dict]] = [{"dpCode": 1, "dpPort": 2}]
    _UNCODED: ClassVar[list[dict]] = [{"dpCode": 1, "dpPort": 9}]

    def _install(self, monkeypatch, catalog):
        """Swap in a purpose-built catalog for the duration of one test."""
        monkeypatch.setattr(product_catalog_module, "_CATALOG", catalog)

    def test_exact_model_code_wins(self, monkeypatch):
        """A code listed in the catalog resolves to its own variant."""
        self._install(monkeypatch, {"HIC801W": {"278": self._CODED, "279": self._OTHER_CODED}})

        assert product_catalog_module.get_catalog_entry("HIC801W", 279) == self._OTHER_CODED

    def test_integer_and_string_model_codes_resolve_alike(self, monkeypatch):
        """Devices report modelCode as an int; the catalog keys it as a string."""
        self._install(monkeypatch, {"HIC801W": {"278": self._CODED}})

        assert product_catalog_module.get_catalog_entry("HIC801W", 278) == self._CODED
        assert product_catalog_module.get_catalog_entry("HIC801W", "278") == self._CODED

    def test_unlisted_model_code_never_borrows_another_variant(self, monkeypatch):
        """The whole point: an unknown code must not resolve to a sibling's data."""
        self._install(monkeypatch, {"HIC801W": {"278": self._CODED, "279": self._OTHER_CODED}})

        assert product_catalog_module.get_catalog_entry("HIC801W", 999) is None

    def test_unlisted_model_code_falls_back_to_the_uncoded_bucket(self, monkeypatch):
        """A model-level default is a legitimate fallback; a sibling code is not."""
        self._install(monkeypatch, {"HIC801W": {"278": self._CODED, UNCODED_VARIANT: self._UNCODED}})

        assert product_catalog_module.get_catalog_entry("HIC801W", 999) == self._UNCODED

    def test_missing_model_code_resolves_a_single_variant(self, monkeypatch):
        """With only one variant there is nothing to disambiguate."""
        self._install(monkeypatch, {"HCS777ARF": {"278": self._CODED}})

        assert product_catalog_module.get_catalog_entry("HCS777ARF") == self._CODED

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

        assert product_catalog_module.get_catalog_entry("HIC801W") == self._UNCODED

    def test_model_with_no_variants_returns_none(self, monkeypatch):
        """An empty variant map carries nothing to annotate with."""
        self._install(monkeypatch, {"HIC801W": {}})

        assert product_catalog_module.get_catalog_entry("HIC801W", 278) is None


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
