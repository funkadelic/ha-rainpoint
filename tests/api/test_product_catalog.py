"""Tests for the import-time, failure-tolerant product catalog loader."""

import json

import custom_components.rainpoint.api.product_catalog as product_catalog_module
from custom_components.rainpoint.api import get_catalog_entry
from custom_components.rainpoint.api.product_catalog import _load_catalog


class TestLoadCatalogValid:
    """_load_catalog against a well-formed fixture file."""

    def test_valid_fixture_returns_dict_keyed_by_model(self, tmp_path):
        catalog_path = tmp_path / "product_catalog.json"
        catalog_path.write_text(json.dumps({"SOME_MODEL": [{"dpCode": 1}]}), encoding="utf-8")

        loaded = _load_catalog(catalog_path)

        assert loaded == {"SOME_MODEL": [{"dpCode": 1}]}

    def test_get_catalog_entry_returns_seeded_model_dp_list(self):
        """The shipped catalog carries the seeded bootstrap model."""
        entry = get_catalog_entry("HCS777ARF")
        assert isinstance(entry, list)
        assert len(entry) > 0
        assert all("dpCode" in dp for dp in entry)

    def test_get_catalog_entry_unknown_model_returns_none(self):
        assert get_catalog_entry("TOTALLY_UNKNOWN_MODEL") is None

    def test_get_catalog_entry_none_model_returns_none(self):
        assert get_catalog_entry(None) is None


class TestLoadCatalogFailSoft:
    """_load_catalog must degrade to {} on every failure mode, never raise."""

    def test_missing_file_returns_empty_dict(self, tmp_path):
        missing_path = tmp_path / "does-not-exist.json"

        assert _load_catalog(missing_path) == {}

    def test_corrupt_json_returns_empty_dict(self, tmp_path):
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
