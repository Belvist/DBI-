"""Config gates: production never starts misconfigured."""
from __future__ import annotations

import pytest

from config.settings import load


def test_defaults_are_demo():
    s = load({})
    assert s.env == "demo" and s.allow_seed and s.identity_mode == "demo"


def test_production_forbids_demo_identity():
    with pytest.raises(RuntimeError):
        load({"DBI_ENV": "production"})


def test_production_forbids_seed_even_explicit():
    with pytest.raises(RuntimeError):
        load({"DBI_ENV": "production", "DBI_IDENTITY": "tokenfile",
              "DBI_TOKENS_FILE": "t.json", "DBI_ALLOW_SEED": "1"})


def test_production_ok_with_tokenfile():
    s = load({"DBI_ENV": "production", "DBI_IDENTITY": "tokenfile",
              "DBI_TOKENS_FILE": "t.json"})
    assert not s.allow_seed


def test_tokenfile_requires_file():
    with pytest.raises(RuntimeError):
        load({"DBI_IDENTITY": "tokenfile"})


def test_seed_opt_out():
    assert not load({"DBI_ALLOW_SEED": "0"}).allow_seed


def test_pool_max_parsed_and_validated():
    assert load({}).pg_pool_max == 10
    assert load({"DBI_PG_POOL_MAX": "3"}).pg_pool_max == 3
    import pytest

    with pytest.raises(RuntimeError):
        load({"DBI_PG_POOL_MAX": "0"})
    with pytest.raises(RuntimeError):
        load({"DBI_PG_POOL_MAX": "lots"})
