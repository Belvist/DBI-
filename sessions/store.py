"""Dialogue state snapshots — a restart must not silently kill a conversation.

- Ephemeral by default (:memory:): demos stay reproducible.
- Durable when DBI_SESSIONS_PATH points at a file (compose sets it):
  state survives process restarts.
- Corrupt / foreign-version snapshots are ignored (fresh state), never crash.
"""
from __future__ import annotations

import json
import logging
import sqlite3
import threading
from pathlib import Path

from dialogue.state import DialogueState

log = logging.getLogger("dbi.sessions")

SNAPSHOT_VERSION = 1


class SessionStore:
    def __init__(self, path: str | Path = ":memory:") -> None:
        self._path = str(path)
        if self._path != ":memory:":
            parent = Path(self._path).parent
            parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(self._path, check_same_thread=False)
        with self._lock:
            self._conn.execute(
                """CREATE TABLE IF NOT EXISTS snapshots(
                     patient_id TEXT PRIMARY KEY,
                     state_json TEXT NOT NULL,
                     updated_at TEXT NOT NULL
                   )"""
            )
            self._conn.commit()

    def save(self, state: DialogueState) -> None:
        payload = json.dumps(
            {"v": SNAPSHOT_VERSION, "state": state.model_dump(mode="json")},
            ensure_ascii=False,
        )
        with self._lock:
            self._conn.execute(
                """INSERT INTO snapshots(patient_id, state_json, updated_at)
                   VALUES(?, ?, datetime('now'))
                   ON CONFLICT(patient_id) DO UPDATE SET
                     state_json=excluded.state_json, updated_at=excluded.updated_at""",
                (state.patient.patient_id, payload),
            )
            self._conn.commit()

    def load(self, patient_id: str):
        with self._lock:
            row = self._conn.execute(
                "SELECT state_json FROM snapshots WHERE patient_id=?", (patient_id,)
            ).fetchone()
        if row is None:
            return None
        try:
            payload = json.loads(row[0])
            if payload.get("v") != SNAPSHOT_VERSION:
                return None
            return DialogueState(**payload["state"])
        except Exception as e:
            log.warning("ignoring corrupt snapshot for %s: %s", patient_id, e)
            return None

    def drop(self, patient_id: str) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM snapshots WHERE patient_id=?", (patient_id,))
            self._conn.commit()

    def ping(self) -> None:
        with self._lock:
            self._conn.execute("SELECT 1").fetchone()
