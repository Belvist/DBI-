"""Redis-backed dialogue snapshots — the source of truth across replicas.

Same CAS contract as the SQLite store: save() compares the stored revision
atomically (Lua + cjson) and raises StaleState instead of overwriting newer
state. Entries expire via Redis TTL so stale conversations disappear.
"""
from __future__ import annotations

import json
import logging

from dialogue.state import DialogueState
from sessions.errors import StaleState
from sessions.store import SNAPSHOT_VERSION

log = logging.getLogger("dbi.sessions")

KEY_PREFIX = "dbi:snap:"
DEFAULT_TTL_S = 7 * 24 * 3600

_CAS_LUA = """
local raw = redis.call('get', KEYS[1])
local exp = tonumber(ARGV[1])
if raw then
  if cjson.decode(raw).revision ~= exp then return -1 end
elseif exp ~= 0 then return -1 end
redis.call('set', KEYS[1], ARGV[2], 'EX', ARGV[3])
return exp + 1
"""


class RedisSessionStore:
    def __init__(self, url: str, ttl_s: int = DEFAULT_TTL_S) -> None:
        import redis

        self._ttl = ttl_s
        self._redis = redis.Redis.from_url(
            url, socket_connect_timeout=5, socket_timeout=5, decode_responses=True
        )
        self._redis.ping()
        self._cas = self._redis.register_script(_CAS_LUA)

    def _key(self, patient_id: str) -> str:
        return f"{KEY_PREFIX}{patient_id}"

    def save(self, state: DialogueState) -> int:
        """CAS-save. Returns the new revision; raises StaleState on conflict."""
        new_revision = state.revision + 1
        payload = json.dumps(
            {"v": SNAPSHOT_VERSION, "revision": new_revision,
             "state": state.model_dump(mode="json")},
            ensure_ascii=False,
        )
        result = self._cas(
            keys=[self._key(state.patient.patient_id)],
            args=[state.revision, payload, self._ttl],
        )
        if int(result) < 0:
            raise StaleState(state.patient.patient_id)
        state.revision = new_revision
        return new_revision

    def load(self, patient_id: str):
        raw = self._redis.get(self._key(patient_id))
        if raw is None:
            return None
        try:
            payload = json.loads(raw)
            if payload.get("v") not in (1, SNAPSHOT_VERSION):
                return None
            state = DialogueState(**payload["state"])
            state.revision = payload.get("revision", 0)
            return state
        except Exception as e:
            log.warning("ignoring corrupt snapshot for %s: %s", patient_id, e)
            return None

    def drop(self, patient_id: str) -> None:
        self._redis.delete(self._key(patient_id))

    def ping(self) -> None:
        self._redis.ping()
