"""Session snapshots survive restarts; corruption never crashes."""
from __future__ import annotations

from datetime import datetime

from api.booking_service import DialogueSession
from clinic_adapter.mock_sqlite import MockSqliteClinic
from dialogue.state import DialogueState, Flow, Phase
from domain.models import PatientRef
from sessions.store import SessionStore


def _sess(pid="p-snap", adapter=None):
    return DialogueSession(
        PatientRef(patient_id=pid),
        adapter or MockSqliteClinic(),
        now=datetime(2026, 10, 13, 12, 0),
    )


def test_save_restore_roundtrip(tmp_path):
    store = SessionStore(tmp_path / "s.sqlite")
    s = _sess()
    s.turn("Запиши меня к неврологу на следующей неделе вечером")
    assert s.state.phase == Phase.PROPOSE
    store.save(s.state)

    # "restart": brand-new session object, same patient, same file
    store2 = SessionStore(tmp_path / "s.sqlite")
    restored = store2.load("p-snap")
    assert restored is not None
    assert restored.phase == Phase.PROPOSE
    assert restored.flow == Flow.BOOK
    assert len(restored.candidates) == 3


def test_missing_snapshot_is_none(tmp_path):
    assert SessionStore(tmp_path / "s.sqlite").load("nobody") is None


def test_corrupt_snapshot_is_ignored_not_fatal(tmp_path):
    p = tmp_path / "s.sqlite"
    SessionStore(p)  # creates schema
    import sqlite3

    conn = sqlite3.connect(str(p))
    conn.execute(
        "INSERT INTO snapshots(patient_id, state_json, updated_at) VALUES(?, ?, ?)",
        ("p-bad", "{not json", "2026-10-06"),
    )
    conn.commit()
    conn.close()
    assert SessionStore(p).load("p-bad") is None


def test_corrupt_snapshot_is_quarantined_then_recoverable(tmp_path):
    import sqlite3

    from dialogue.state import DialogueState

    p = tmp_path / "s.sqlite"
    SessionStore(p)  # creates schema
    conn = sqlite3.connect(str(p))
    conn.execute(
        "INSERT INTO snapshots(patient_id, state_json, revision, updated_at) VALUES(?, ?, ?, ?)",
        ("p-q", "{not json", 5, "2026-10-06"),
    )
    conn.commit()
    conn.close()
    assert SessionStore(p).load("p-q") is None
    # quarantined: a fresh save starts clean at revision 0 -> 1
    fresh = DialogueState(patient=PatientRef(patient_id="p-q"))
    assert SessionStore(p).save(fresh) == 1
    assert SessionStore(p).load("p-q").revision == 1


def test_foreign_version_is_ignored(tmp_path):
    import json
    import sqlite3


    p = tmp_path / "s.sqlite"
    SessionStore(p)
    conn = sqlite3.connect(str(p))
    payload = json.dumps(
        {"v": 999, "state": DialogueState(patient=PatientRef(patient_id="x")).model_dump(mode="json")}
    )
    conn.execute(
        "INSERT INTO snapshots(patient_id, state_json, updated_at) VALUES(?, ?, ?)",
        ("p-v", payload, "2026-10-06"),
    )
    conn.commit()
    conn.close()
    assert SessionStore(p).load("p-v") is None


def test_dialogue_continues_after_restore(tmp_path):
    store = SessionStore(tmp_path / "s.sqlite")
    adapter = MockSqliteClinic()
    s = _sess("p-cont", adapter)
    s.turn("Запиши меня к неврологу на следующей неделе вечером")
    store.save(s.state)

    s2 = DialogueSession(PatientRef(patient_id="p-cont"), adapter, now=datetime(2026, 10, 13, 12, 0))
    s2.state = store.load("p-cont")
    r = s2.turn("Первый вариант")
    assert "подтверд" in r.lower() or "верно" in r.lower()
