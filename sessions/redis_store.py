"""Redis-backed dialogue snapshots — shared across API replicas.

Same save/load/drop contract as the SQLite store; entries expire via
Redis TTL so stale conversations disappear without a sweeper.
"""
from __future__ import annotations

import json
import logging

from dialogue.state import DialogueState
from sessions.store import SNAPSHOT_VERSION

log = logging.getLogger("dbi.sessions")

KEY_PREFIX = "dbi:snap:"
DEFAULT_TTL_S = 7 * 24 * 3600


class RedisSessionStore:
    def __init__(self, url: str, ttl_s: int = DEFAULT_TTL_S) -> None:
        import redis

        self._ttl = ttl_s
        self._redis = redis.Redis.from_url(
            url, socket_connect_timeout=5, socket_timeout=5, decode_responses=True
        )
        self._redis.ping()

    def _key(self, patient_id: str) -> str:
        return f"{KEY_PREFIX}{patient_id}"

    def save(self, state: DialogueState) -> None:
        payload = json.dumps(
            {"v": SNAPSHOT_VERSION, "state": state.model_dump(mode="json")},
            ensure_ascii=False,
        )
        self._redis.set(self._key(state.patient.patient_id), payload, ex=self._ttl)

    def load(self, patient_id: str):
        raw = self._redis.get(self._key(patient_id))
        if raw is None:
            return None
        try:
            payload = json.loads(raw)
            if payload.get("v") != SNAPSHOT_VERSION:
                return None
            return DialogueState(**payload["state"])
        except Exception as e:
            log.warning("ignoring corrupt snapshot for %s: %s", patient_id, e)
            return None

    def drop(self, patient_id: str) -> None:
        self._redis.delete(self._key(patient_id))
