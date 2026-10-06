"""Migrations + pool — env-gated (DBI_PG_URL), real Postgres only."""
from __future__ import annotations

import os
import threading

import pytest

from clinic_adapter.migrate import apply
from clinic_adapter.postgres import PostgresClinic

PG_URL = os.getenv("DBI_PG_URL", "")
needs_pg = pytest.mark.skipif(not PG_URL, reason="DBI_PG_URL not set")


@needs_pg
def test_migrations_apply_once_and_are_idempotent():
    apply(PG_URL)
    assert apply(PG_URL) == []
    assert "001_init.sql" in _applied_names()


def _applied_names() -> list[str]:
    import psycopg
    from psycopg.rows import dict_row

    with psycopg.connect(PG_URL, row_factory=dict_row) as conn, conn.cursor() as cur:
        cur.execute("SELECT filename FROM schema_migrations")
        return [r["filename"] for r in cur.fetchall()]


@needs_pg
def test_concurrent_migration_runners_elect_one_applier():
    from clinic_adapter.migrate import apply

    results: list = []
    errors: list = []

    def run():
        try:
            results.append(apply(PG_URL))
        except Exception as e:
            errors.append(e)

    threads = [threading.Thread(target=run) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    assert "001_init.sql" in _applied_names()


@needs_pg
def test_pool_serves_concurrent_readers():
    c = PostgresClinic(url=PG_URL, pool_max=4)
    try:
        errors: list[Exception] = []

        def read_many():
            try:
                for _ in range(10):
                    assert c.find_slots(limit=3)
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=read_many) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert not errors
    finally:
        c.close()


@needs_pg
def test_pool_checkout_respects_max():
    from psycopg_pool.errors import PoolTimeout

    c = PostgresClinic(url=PG_URL, pool_max=2)
    import contextlib

    try:
        with contextlib.ExitStack() as stack:
            stack.enter_context(c._pool.connection())
            stack.enter_context(c._pool.connection())
            with pytest.raises(PoolTimeout), c._pool.connection(timeout=1):
                pass
    finally:
        c.close()
