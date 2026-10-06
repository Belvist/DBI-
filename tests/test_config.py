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


def _prod_base():
    return {"DBI_ENV": "production", "DBI_IDENTITY": "tokenfile",
            "DBI_TOKENS_FILE": "t.json", "DBI_PG_URL": "postgresql://x/y",
            "DBI_REDIS_URL": "redis://x/0", "DBI_METRICS_TOKEN": "tok"}


def test_production_requires_metrics_token():
    env = _prod_base()
    del env["DBI_METRICS_TOKEN"]
    with pytest.raises(RuntimeError):
        load(env)


def test_production_ok_with_tokenfile():
    s = load(_prod_base())
    assert not s.allow_seed


def test_production_requires_postgres():
    env = _prod_base()
    del env["DBI_PG_URL"]
    with pytest.raises(RuntimeError):
        load(env)


def test_production_requires_redis_or_single_replica():
    env = _prod_base()
    del env["DBI_REDIS_URL"]
    with pytest.raises(RuntimeError):
        load(env)
    env["DBI_SINGLE_REPLICA"] = "1"
    assert load(env).single_replica


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
