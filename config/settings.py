"""Central runtime config. Production refuses to start misconfigured.

Rules:
- DBI_ENV=production + demo identity  -> startup error (no silent demo patient).
- DBI_ENV=production                  -> seeding forbidden, always.
- DBI_IDENTITY=tokenfile              -> DBI_TOKENS_FILE required.
"""
from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    env: str  # "demo" | "test" | "production"
    identity_mode: str  # "demo" | "tokenfile"
    tokens_file: str
    pg_url: str
    pg_pool_max: int
    db_path: str
    sessions_path: str
    redis_url: str
    single_replica: bool
    metrics_token: str
    allow_seed: bool


def load(env: dict[str, str] | None = None) -> Settings:
    src = env if env is not None else os.environ
    mode = src.get("DBI_ENV", "demo")
    if mode not in ("demo", "test", "production"):
        raise RuntimeError(f"unknown DBI_ENV: {mode!r}")
    identity_mode = src.get("DBI_IDENTITY", "demo")
    if identity_mode not in ("demo", "tokenfile"):
        raise RuntimeError(f"unknown DBI_IDENTITY: {identity_mode!r}")
    if mode == "production" and identity_mode == "demo":
        raise RuntimeError("DBI_ENV=production forbids DBI_IDENTITY=demo")
    tokens_file = src.get("DBI_TOKENS_FILE", "")
    if identity_mode == "tokenfile" and not tokens_file:
        raise RuntimeError("DBI_IDENTITY=tokenfile requires DBI_TOKENS_FILE")
    explicit_seed = src.get("DBI_ALLOW_SEED", "")
    if mode == "production":
        if explicit_seed == "1":
            raise RuntimeError("DBI_ENV=production forbids DBI_ALLOW_SEED=1")
        allow_seed = False
    else:
        allow_seed = explicit_seed != "0"
    try:
        pool_max = int(src.get("DBI_PG_POOL_MAX", "10"))
    except ValueError:
        raise RuntimeError("DBI_PG_POOL_MAX must be an integer") from None
    if pool_max < 1:
        raise RuntimeError("DBI_PG_POOL_MAX must be >= 1")
    pg_url = src.get("DBI_PG_URL", "")
    redis_url = src.get("DBI_REDIS_URL", "")
    single_replica = src.get("DBI_SINGLE_REPLICA", "") == "1"
    metrics_token = src.get("DBI_METRICS_TOKEN", "")
    if mode == "production":
        if not pg_url:
            raise RuntimeError("DBI_ENV=production requires DBI_PG_URL (no ephemeral DB)")
        if not redis_url and not single_replica:
            raise RuntimeError(
                "DBI_ENV=production requires DBI_REDIS_URL "
                "or explicit DBI_SINGLE_REPLICA=1"
            )
        if not metrics_token:
            raise RuntimeError(
                "DBI_ENV=production requires DBI_METRICS_TOKEN (/metrics must not be public)"
            )
    return Settings(
        env=mode,
        identity_mode=identity_mode,
        tokens_file=tokens_file,
        pg_url=pg_url,
        pg_pool_max=pool_max,
        db_path=src.get("DBI_DB_PATH", ":memory:"),
        sessions_path=src.get("DBI_SESSIONS_PATH", ":memory:"),
        redis_url=redis_url,
        single_replica=single_replica,
        metrics_token=metrics_token,
        allow_seed=allow_seed,
    )
