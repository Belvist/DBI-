"""Test-suite hygiene: a PG-backed run always starts from a clean slate.

Bookings and taken flags are transactional dirt from previous runs; doctors
and slots (seed) are left intact. Opt out with DBI_TEST_WIPE_PG=0 — but only
if you enjoy order-dependent flakes.
"""
from __future__ import annotations

import os

import pytest


@pytest.fixture(scope="session", autouse=True)
def _fresh_pg_state():
    url = os.getenv("DBI_PG_URL", "")
    if not url or os.getenv("DBI_TEST_WIPE_PG", "1") == "0":
        return
    import psycopg
    from psycopg.rows import dict_row

    with psycopg.connect(url, row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT to_regclass('public.appointments') AS t")
            if cur.fetchone()["t"] is None:
                return  # migrations not applied yet; nothing to wipe
            cur.execute("TRUNCATE appointments")
            cur.execute("UPDATE slots SET taken_by=NULL")
        conn.commit()
    print("\n[pg] transactional slate wiped (seed kept)")
