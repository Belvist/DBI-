"""Dialogue state snapshots — the source of truth across replicas.

- Ephemeral by default (:memory:): demos stay reproducible.
- Durable when DBI_SESSIONS_PATH points at a file (compose sets it).
- Every save is a compare-and-set on `revision`: a replica holding stale
  state gets StaleState instead of silently overwriting newer state.
- Corrupt / foreign-version snapshots are ignored (fresh state), never crash.
"""
from __future__ import annotations

import json
import logging
import sqlite3
import threading
from pathlib import Path

from dialogue.state import DialogueState
from domain.adapter_errors import DependencyUnavailable
from sessions.errors import StaleState

log = logging.getLogger("dbi.sessions")

SNAPSHOT_VERSION = 2


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
                     revision INTEGER NOT NULL DEFAULT 0,
                     updated_at TEXT NOT NULL
                   )"""
            )
            cols = [r[1] for r in self._conn.execute("PRAGMA table_info(snapshots)")]
            if "revision" not in cols:
                self._conn.execute("ALTER TABLE snapshots ADD COLUMN revision INTEGER NOT NULL DEFAULT 0")
            self._conn.commit()

    def save(self, state: DialogueState) -> int:
        """CAS-save. Returns the new revision; raises StaleState on conflict."""
        new_revision = state.revision + 1
        payload = json.dumps(
            {"v": SNAPSHOT_VERSION, "revision": new_revision,
             "state": state.model_dump(mode="json")},
            ensure_ascii=False,
        )
        try:
            with self._lock:
                if state.revision == 0:
                    # Check if row exists first - if it does, our base is stale
                    existing = self._conn.execute(
                        "SELECT revision FROM snapshots WHERE patient_id=?",
                        (state.patient.patient_id,),
                    ).fetchone()
                    if existing is not None:
                        self._conn.commit()
                        raise StaleState(state.patient.patient_id)
                    self._conn.execute(
                        """INSERT INTO snapshots(patient_id, state_json, revision, updated_at)
                           VALUES(?, ?, ?, datetime('now'))""",
                        (state.patient.patient_id, payload, new_revision),
                    )
                else:
                    cur = self._conn.execute(
                        """UPDATE snapshots SET state_json=?, revision=?, updated_at=datetime('now')
                           WHERE patient_id=? AND revision=?""",
                        (payload, new_revision, state.patient.patient_id, state.revision),
                    )
                    if cur.rowcount == 0:
                        self._conn.commit()
                        raise StaleState(state.patient.patient_id)
                self._conn.commit()
        except sqlite3.Error as e:
            raise DependencyUnavailable(f"snapshot save failed: {e}") from e
        state.revision = new_revision
        return new_revision

    def load(self, patient_id: str):
        try:
            with self._lock:
                row = self._conn.execute(
                    "SELECT state_json, revision FROM snapshots WHERE patient_id=?",
                    (patient_id,),
                ).fetchone()
        except sqlite3.Error as e:
            raise DependencyUnavailable(f"snapshot load failed: {e}") from e
        if row is None:
            return None
        try:
            payload = json.loads(row[0])
            if payload.get("v") not in (1, SNAPSHOT_VERSION):
                return None
            state = DialogueState(**payload["state"])
            state.revision = payload.get("revision", row[1] or 0)
            return state
        except Exception as e:
            log.warning("ignoring corrupt snapshot for %s: %s", patient_id, e)
            return None

    def drop(self, patient_id: str) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM snapshots WHERE patient_id=?", (patient_id,))
            self._conn.commit()

    def ping(self) -> None:
        try:
            with self._lock:
                self._conn.execute("SELECT 1").fetchone()
        except sqlite3.Error as e:
            raise DependencyUnavailable(f"snapshot ping failed: {e}") from e
