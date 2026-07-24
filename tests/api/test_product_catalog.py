"""Tests for the import-time, failure-tolerant product catalog loader."""

import json

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
