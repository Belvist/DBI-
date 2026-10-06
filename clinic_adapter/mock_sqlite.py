"""SQLite-backed mock clinic. Production discipline, demo data.

Guarantees:
- WAL mode, foreign keys on.
- WRITE is atomic: BEGIN IMMEDIATE -> re-check slot free -> idempotency
  dedup -> insert booking + mark slot taken, all in one transaction.
- Idempotent replay: same idempotency_key returns the ORIGINAL appointment,
  never a duplicate (duplicate COMMIT scenario in eval).
- Thread-safe via a single lock (hackathon scope: one process).
"""
from __future__ import annotations

import sqlite3
import threading
import uuid
from datetime import datetime, timedelta
from pathlib import Path

from clinic_adapter.seed_data import SEED_BASE, SEED_DOCTORS, seed_slots
from clinic_adapter.seed_data import dt as _dt
from clinic_adapter.seed_data import ts as _s
from domain.commands import CreateAppointmentCommand, RescheduleCommand
from domain.errors import (
    AppointmentNotFound,
    SlotNotFound,
    SlotUnavailable,
)
from domain.models import (
    Appointment,
    AppointmentStatus,
    Doctor,
    PatientRef,
    Slot,
    SlotWithDoctor,
    Specialty,
)

_SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS doctors(
  doctor_id TEXT PRIMARY KEY,
  full_name TEXT NOT NULL,
  short_name TEXT NOT NULL,
  specialty TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS slots(
  slot_id TEXT PRIMARY KEY,
  doctor_id TEXT NOT NULL REFERENCES doctors(doctor_id),
  start TEXT NOT NULL,
  end TEXT NOT NULL,
  taken_by TEXT NULL
);
CREATE TABLE IF NOT EXISTS appointments(
  booking_id TEXT PRIMARY KEY,
  patient_id TEXT NOT NULL,
  doctor_id TEXT NOT NULL REFERENCES doctors(doctor_id),
  slot_id TEXT NOT NULL,
  start TEXT NOT NULL,
  end TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'active',
  idempotency_key TEXT NOT NULL UNIQUE,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_slots_doc_start ON slots(doctor_id, start);
CREATE INDEX IF NOT EXISTS idx_appt_patient ON appointments(patient_id, status);
"""

class MockSqliteClinic:
    def __init__(
        self,
        path: str | Path = ":memory:",
        seed_base: datetime | None = None,
        seed: bool = True,
    ) -> None:
        self._path = str(path)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(self._path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.executescript(_SCHEMA)
            if (
                seed
                and self._conn.execute("SELECT COUNT(*) FROM doctors").fetchone()[0] == 0
            ):
                self._seed(seed_base or SEED_BASE)

    # ---------- seed ----------
    def _seed(self, base_monday: datetime) -> None:
        # Every doctor gets every slot: guarantees demo/eval coverage for any
        # specialty on any weekday. Real sparsity comes from bookings.
        for doctor_id, full, short, spec in SEED_DOCTORS:
            self._conn.execute(
                "INSERT INTO doctors(doctor_id, full_name, short_name, specialty) VALUES(?,?,?,?)",
                (doctor_id, full, short, spec.value),
            )
        for sid, doctor_id, start, end in seed_slots(base_monday):
            self._conn.execute(
                "INSERT OR IGNORE INTO slots(slot_id, doctor_id, start, end, taken_by) VALUES(?,?,?,?,NULL)",
                (sid, doctor_id, start, end),
            )
        self._conn.commit()

    # ---------- READ ----------
    def find_doctors(
        self,
        specialty: Specialty | None = None,
        name_query: str | None = None,
    ) -> list[Doctor]:
        q = "SELECT * FROM doctors WHERE 1=1"
        args: list[str] = []
        if specialty is not None:
            q += " AND specialty=?/interp"
            args.append(specialty.value)
        # NOTE: placeholder replaced below to keep query readable
        q = q.replace("?/interp", "?")
        rows = self._conn.execute(q, args).fetchall()
        out = [
            Doctor(
                doctor_id=r["doctor_id"],
                full_name=r["full_name"],
                short_name=r["short_name"],
                specialty=Specialty(r["specialty"]),
            )
            for r in rows
        ]
        if name_query:
            # NLU currently supplies a surname stem ("иванов", "петров").
            # Match it against the surname token only; substring search across
            # the whole FIO makes "иванов" incorrectly match patronymics such
            # as "Ивановна".
            nq = name_query.lower().strip()
            matched: list[Doctor] = []
            for doctor in out:
                surname = doctor.full_name.split()[0].lower()
                if surname.startswith(nq) or nq.startswith(surname):
                    matched.append(doctor)
            out = matched
        return sorted(out, key=lambda d: d.doctor_id)

    def _row_to_swd(self, r: sqlite3.Row) -> SlotWithDoctor:
        return SlotWithDoctor(
            slot=Slot(
                slot_id=r["slot_id"],
                doctor_id=r["doctor_id"],
                start=_dt(r["start"]),
                end=_dt(r["end"]),
            ),
            doctor=Doctor(
                doctor_id=r["doctor_id"],
                full_name=r["full_name"],
                short_name=r["short_name"],
                specialty=Specialty(r["specialty"]),
            ),
        )

    def find_slots(
        self,
        doctor_id: str | None = None,
        specialty: Specialty | None = None,
        date_from: datetime | None = None,
        date_to: datetime | None = None,
        time_pref: str | None = None,
        limit: int = 5,
    ) -> list[SlotWithDoctor]:
        q = """SELECT s.slot_id, s.doctor_id, s.start, s.end,
                      d.full_name, d.short_name, d.specialty
               FROM slots s JOIN doctors d ON d.doctor_id=s.doctor_id
               WHERE s.taken_by IS NULL"""
        args: list = []
        if doctor_id:
            q += " AND s.doctor_id=?"
            args.append(doctor_id)
        if specialty:
            q += " AND d.specialty=?"
            args.append(specialty.value)
        if date_from:
            q += " AND s.start>=?"
            args.append(_s(date_from))
        if date_to:
            q += " AND s.start<?"
            args.append(_s(date_to))
        q += " ORDER BY s.start ASC LIMIT ?"
        args.append(limit * 4)  # type: ignore[arg-type] — sqlite accepts int
        rows = self._conn.execute(q, args).fetchall()
        out = [self._row_to_swd(r) for r in rows]
        if time_pref == "morning":
            out = [x for x in out if x.slot.start.hour < 13]
        elif time_pref == "evening":
            out = [x for x in out if x.slot.start.hour >= 17]
        return out[:limit]

    def get_appointments(self, patient: PatientRef) -> list[Appointment]:
        rows = self._conn.execute(
            "SELECT * FROM appointments WHERE patient_id=? AND status='active' ORDER BY start ASC",
            (patient.patient_id,),
        ).fetchall()
        return [self._row_to_appt(r) for r in rows]

    def get_schedule(
        self,
        doctor_id: str | None = None,
        date_from: datetime | None = None,
        date_to: datetime | None = None,
    ) -> list[SlotWithDoctor]:
        return self.find_slots(
            doctor_id=doctor_id, date_from=date_from, date_to=date_to, limit=50
        )

    @staticmethod
    def _row_to_appt(r: sqlite3.Row) -> Appointment:
        return Appointment(
            booking_id=r["booking_id"],
            patient_id=r["patient_id"],
            doctor_id=r["doctor_id"],
            slot_id=r["slot_id"],
            start=_dt(r["start"]),
            end=_dt(r["end"]),
            status=AppointmentStatus(r["status"]),
            idempotency_key=r["idempotency_key"],
            created_at=_dt(r["created_at"]),
        )

    # ---------- WRITE (atomic) ----------
    def create_appointment(self, cmd: CreateAppointmentCommand) -> Appointment:
        with self._lock:
            cur = self._conn.cursor()
            cur.execute("BEGIN IMMEDIATE")
            try:
                # idempotency first: replay returns original, no duplicate
                row = cur.execute(
                    "SELECT * FROM appointments WHERE idempotency_key=?",
                    (cmd.idempotency_key,),
                ).fetchone()
                if row is not None:
                    self._conn.commit()
                    return self._row_to_appt(row)
                s = cur.execute(
                    "SELECT slot_id, doctor_id, start, end, taken_by FROM slots WHERE slot_id=?",
                    (cmd.slot_id,),
                ).fetchone()
                if s is None:
                    self._conn.rollback()
                    raise SlotNotFound(f"unknown slot {cmd.slot_id}")
                if s["taken_by"] is not None:
                    self._conn.rollback()
                    raise SlotUnavailable(f"slot {cmd.slot_id} just taken")
                booking_id = "A" + uuid.uuid4().hex[:6].upper()
                now = _s(datetime.now().replace(second=0, microsecond=0))
                cur.execute(
                    """INSERT INTO appointments
                       (booking_id, patient_id, doctor_id, slot_id, start, end, status, idempotency_key, created_at)
                       VALUES(?,?,?,?,?,?,'active',?,?)""",
                    (
                        booking_id,
                        cmd.patient.patient_id,
                        s["doctor_id"],
                        s["slot_id"],
                        s["start"],
                        s["end"],
                        cmd.idempotency_key,
                        now,
                    ),
                )
                cur.execute(
                    "UPDATE slots SET taken_by=? WHERE slot_id=?",
                    (cmd.patient.patient_id, cmd.slot_id),
                )
                self._conn.commit()
                row2 = self._conn.execute(
                    "SELECT * FROM appointments WHERE booking_id=?", (booking_id,)
                ).fetchone()
                assert row2 is not None
                return self._row_to_appt(row2)
            except Exception:
                try:
                    self._conn.rollback()
                except Exception:
                    pass
                raise

    def reschedule_appointment(self, cmd: RescheduleCommand) -> Appointment:
        with self._lock:
            cur = self._conn.cursor()
            cur.execute("BEGIN IMMEDIATE")
            try:
                row = cur.execute(
                    "SELECT * FROM appointments WHERE idempotency_key=?",
                    (cmd.idempotency_key,),
                ).fetchone()
                if row is not None:
                    self._conn.commit()
                    return self._row_to_appt(row)
                old = cur.execute(
                    "SELECT * FROM appointments WHERE booking_id=? AND patient_id=?",
                    (cmd.booking_id, cmd.patient.patient_id),
                ).fetchone()
                if old is None:
                    self._conn.rollback()
                    raise AppointmentNotFound(f"unknown booking {cmd.booking_id}")
                if old["status"] != AppointmentStatus.ACTIVE.value:
                    self._conn.rollback()
                    raise AppointmentNotFound(f"booking {cmd.booking_id} not active")
                tgt = cur.execute(
                    "SELECT slot_id, doctor_id, start, end, taken_by FROM slots WHERE slot_id=?",
                    (cmd.new_slot_id,),
                ).fetchone()
                if tgt is None:
                    self._conn.rollback()
                    raise SlotNotFound(f"unknown slot {cmd.new_slot_id}")
                if tgt["taken_by"] is not None:
                    self._conn.rollback()
                    raise SlotUnavailable(f"slot {cmd.new_slot_id} just taken")
                # free old slot, take new one, mark old appt moved, insert new appt
                cur.execute("UPDATE slots SET taken_by=NULL WHERE slot_id=?", (old["slot_id"],))
                cur.execute(
                    "UPDATE slots SET taken_by=? WHERE slot_id=?",
                    (cmd.patient.patient_id, cmd.new_slot_id),
                )
                cur.execute(
                    "UPDATE appointments SET status='moved' WHERE booking_id=?",
                    (cmd.booking_id,),
                )
                new_id = "A" + uuid.uuid4().hex[:6].upper()
                now = _s(datetime.now().replace(second=0, microsecond=0))
                cur.execute(
                    """INSERT INTO appointments
                       (booking_id, patient_id, doctor_id, slot_id, start, end, status, idempotency_key, created_at)
                       VALUES(?,?,?,?,?,?,'active',?,?)""",
                    (
                        new_id,
                        cmd.patient.patient_id,
                        tgt["doctor_id"],
                        tgt["slot_id"],
                        tgt["start"],
                        tgt["end"],
                        cmd.idempotency_key,
                        now,
                    ),
                )
                self._conn.commit()
                row2 = self._conn.execute(
                    "SELECT * FROM appointments WHERE booking_id=?", (new_id,)
                ).fetchone()
                assert row2 is not None
                return self._row_to_appt(row2)
            except Exception:
                try:
                    self._conn.rollback()
                except Exception:
                    pass
                raise

    def purge_stale_idempotency_keys(self, older_than_days: int = 30) -> int:
        """Retire idempotency keys of NON-ACTIVE appointments older than N days.

        The appointment rows stay for audit; only the dedup key is rewritten,
        so the UNIQUE budget does not grow forever. ACTIVE bookings are never
        touched: a late retry must still dedup instead of double-booking.
        Returns the number of retired keys.
        """
        from datetime import datetime as _now

        cutoff = _s(_now.now() - timedelta(days=older_than_days))
        with self._lock:
            rows = self._conn.execute(
                """SELECT booking_id, idempotency_key FROM appointments
                   WHERE status != 'active' AND created_at < ?
                   AND idempotency_key NOT LIKE 'purged:%'""",
                (cutoff,),
            ).fetchall()
            for r in rows:
                self._conn.execute(
                    "UPDATE appointments SET idempotency_key=? WHERE booking_id=?",
                    (f"purged:{r['booking_id']}", r["booking_id"]),
                )
            self._conn.commit()
            return len(rows)

    def ping(self) -> None:
        """Lightweight health check - does not mutate state."""
        with self._lock:
            self._conn.execute("SELECT 1").fetchone()

    # test-only helper: simulate a race by taking a slot out-of-band
    def steal_slot(self, slot_id: str, by: str = "other_patient") -> None:
        with self._lock:
            self._conn.execute("UPDATE slots SET taken_by=? WHERE slot_id=?", (by, slot_id))
            self._conn.commit()
