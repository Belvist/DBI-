"""Minimal SQL migration runner (Postgres).

- `migrations/*.sql` applied in name order, each at most once.
- Applied versions tracked in `schema_migrations(filename)`.
- Files are idempotent (`IF NOT EXISTS`), so re-apply is always safe.
"""
from __future__ import annotations

from pathlib import Path

DEFAULT_DIR = Path(__file__).resolve().parent.parent / "migrations"


def pending(conn, directory: Path | str = DEFAULT_DIR) -> list[Path]:
    with conn.cursor() as cur:
        cur.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations"
            "(filename TEXT PRIMARY KEY, applied_at TEXT NOT NULL DEFAULT now()::text)"
        )
        cur.execute("SELECT filename FROM schema_migrations")
        applied = {r["filename"] for r in cur.fetchall()}
    files = sorted(Path(directory).glob("*.sql"))
    return [f for f in files if f.name not in applied]


def apply(url: str, directory: Path | str = DEFAULT_DIR) -> list[str]:
    """Connect, apply pending migrations, return applied filenames.

    Serialized by a Postgres advisory lock: N containers starting at once
    elect one applier instead of racing on the version table.
    """
    import psycopg
    from psycopg.rows import dict_row

    done: list[str] = []
    with psycopg.connect(url, row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT pg_advisory_lock(hashtext('dbi_migrations'))")
        try:
            for path in pending(conn, directory):
                with conn.cursor() as cur:
                    cur.execute(path.read_text(encoding="utf-8"))
                    cur.execute(
                        "INSERT INTO schema_migrations(filename) VALUES(%s)",
                        (path.name,),
                    )
                conn.commit()
                done.append(path.name)
        finally:
            with conn.cursor() as cur:
                cur.execute("SELECT pg_advisory_unlock(hashtext('dbi_migrations'))")
    return done
