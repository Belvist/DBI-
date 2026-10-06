"""Postgres-backed clinic. Same contract as the SQLite mock, real durability.

- One connection per operation (hackathon scale; pool lands with load work).
- WRITE runs in a transaction with SELECT ... FOR UPDATE on the slot row,
  so cross-process races serialize in the database, not in a process lock.
- Cross-process duplicate COMMIT is closed by the UNIQUE idempotency_key:
  on UniqueViolation we re-read by key and return the ORIGINAL booking.
- Datetimes stored as the same TEXT codec as SQLite (see seed_data), so
  filtering semantics are identical across adapters.
- connect_timeout=5s + statement_timeout=5s: a hung DB fails fast instead
  of hanging the dialogue (resilience groundwork for PR4).
"""
from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta

import psycopg
from psycopg.rows import dict_row

from clinic_adapter.seed_data import SEED_BASE, SEED_DOCTORS, seed_slots
from clinic_adapter.seed_data import dt as _dt
from clinic_adapter.seed_data import ts as _s
from domain.commands import CreateAppointmentCommand, RescheduleCommand
from domain.errors import (
    AppointmentNotFound,
    NotConfigured,
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

_DDL = """
CREATE TABLE IF NOT EXISTS doctors(
  doctor_id TEXT PRIMARY KEY,
  full_name TEXT NOT NULL,
  short_name TEXT NOT NULL,
  specialty TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS slots(
  slot_id TEXT PRIMARY KEY,
  doctor_id TEXT NOT NULL REFERENCES doctors(doctor_id),
  start_ts TEXT NOT NULL,
  end_ts TEXT NOT NULL,
  taken_by TEXT NULL
);
CREATE TABLE IF NOT EXISTS appointments(
  booking_id TEXT PRIMARY KEY,
  patient_id TEXT NOT NULL,
  doctor_id TEXT NOT NULL REFERENCES doctors(doctor_id),
  slot_id TEXT NOT NULL,
  start_ts TEXT NOT NULL,
  end_ts TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'active',
  idempotency_key TEXT NOT NULL UNIQUE,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_slots_doc_start ON slots(doctor_id, start_ts);
CREATE INDEX IF NOT EXISTS idx_appt_patient ON appointments(patient_id, status);
"""

_CONNECT_TIMEOUT = 5
_STATEMENT_TIMEOUT_MS = 5000


class PostgresClinic:
    def __init__(self, url: str | None = None, seed_base: datetime | None = None) -> None:
        self._url = url or os.getenv("DBI_PG_URL", "")
        if not self._url:
            raise NotConfigured("DBI_PG_URL is not set")
        self._seed_base = seed_base or SEED_BASE
        self._init_schema()

    def _connect(self):
        return psycopg.connect(
            self._url,
            row_factory=dict_row,
            connect_timeout=_CONNECT_TIMEOUT,
            options=f"-c statement_timeout={_STATEMENT_TIMEOUT_MS}",
        )

    def _init_schema(self) -> None:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(_DDL)
                cur.execute("SELECT COUNT(*) AS n FROM doctors")
                if cur.fetchone()["n"] == 0:  # type: ignore[index]
                    for doctor_id, full, short, spec in SEED_DOCTORS:
                        cur.execute(
                            "INSERT INTO doctors(doctor_id, full_name, short_name, specialty)"
                            " VALUES(%s,%s,%s,%s)",
                            (doctor_id, full, short, spec.value),
                        )
                    for sid, doctor_id, start, end in seed_slots(self._seed_base):
                        cur.execute(
                            "INSERT INTO slots(slot_id, doctor_id, start_ts, end_ts, taken_by)"
                            " VALUES(%s,%s,%s,%s,NULL) ON CONFLICT DO NOTHING",
                            (sid, doctor_id, start, end),
                        )
            conn.commit()

    # ---------- READ ----------
    def find_doctors(
        self,
        specialty: Specialty | None = None,
        name_query: str | None = None,
    ) -> list[Doctor]:
        q = "SELECT * FROM doctors WHERE 1=1"
        args: list = []
        if specialty is not None:
            q += " AND specialty=%s"
            args.append(specialty.value)
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(q, args)
            rows = cur.fetchall()
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
            nq = name_query.lower().strip()
            out = [
                d
                for d in out
                if (s := d.full_name.split()[0].lower()).startswith(nq)
                or nq.startswith(s)
            ]
        return sorted(out, key=lambda d: d.doctor_id)

    @staticmethod
    def _to_swd(r: dict) -> SlotWithDoctor:
        return SlotWithDoctor(
            slot=Slot(
                slot_id=r["slot_id"],
                doctor_id=r["doctor_id"],
                start=_dt(r["start_ts"]),
                end=_dt(r["end_ts"]),
            ),
            doctor=Doctor(
                doctor_id=r["doctor_id"],
                full_name=r["full_name"],
                short_name=r["short_name"],
                specialty=Specialty(r["specialty"]),
            ),
        )

    @staticmethod
    def _to_appt(r: dict) -> Appointment:
        return Appointment(
            booking_id=r["booking_id"],
            patient_id=r["patient_id"],
            doctor_id=r["doctor_id"],
            slot_id=r["slot_id"],
            start=_dt(r["start_ts"]),
            end=_dt(r["end_ts"]),
            status=AppointmentStatus(r["status"]),
            idempotency_key=r["idempotency_key"],
            created_at=_dt(r["created_at"]),
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
        q = """SELECT s.slot_id, s.doctor_id, s.start_ts, s.end_ts,
                      d.full_name, d.short_name, d.specialty
               FROM slots s JOIN doctors d ON d.doctor_id=s.doctor_id
               WHERE s.taken_by IS NULL"""
        args: list = []
        if doctor_id:
            q += " AND s.doctor_id=%s"
            args.append(doctor_id)
        if specialty:
            q += " AND d.specialty=%s"
            args.append(specialty.value)
        if date_from:
            q += " AND s.start_ts>=%s"
            args.append(_s(date_from))
        if date_to:
            q += " AND s.start_ts<%s"
            args.append(_s(date_to))
        q += " ORDER BY s.start_ts ASC LIMIT %s"
        args.append(limit * 4)
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(q, args)
            out = [self._to_swd(r) for r in cur.fetchall()]
        if time_pref == "morning":
            out = [x for x in out if x.slot.start.hour < 13]
        elif time_pref == "evening":
            out = [x for x in out if x.slot.start.hour >= 17]
        return out[:limit]

    def get_appointments(self, patient: PatientRef) -> list[Appointment]:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM appointments WHERE patient_id=%s AND status='active'"
                " ORDER BY start_ts ASC",
                (patient.patient_id,),
            )
            return [self._to_appt(r) for r in cur.fetchall()]

    def get_schedule(
        self,
        doctor_id: str | None = None,
        date_from: datetime | None = None,
        date_to: datetime | None = None,
    ) -> list[SlotWithDoctor]:
        return self.find_slots(
            doctor_id=doctor_id, date_from=date_from, date_to=date_to, limit=50
        )

    # ---------- WRITE (atomic, cross-process safe) ----------
    def _by_key(self, cur, key: str) -> dict | None:
        cur.execute("SELECT * FROM appointments WHERE idempotency_key=%s", (key,))
        return cur.fetchone()

    def create_appointment(self, cmd: CreateAppointmentCommand) -> Appointment:
        booking_id = "A" + uuid.uuid4().hex[:6].upper()
        now = _s(datetime.now().replace(second=0, microsecond=0))
        try:
            with self._connect() as conn, conn.cursor() as cur, conn.transaction():
                if (row := self._by_key(cur, cmd.idempotency_key)) is not None:
                    return self._to_appt(row)
                cur.execute(
                    "SELECT slot_id, doctor_id, start_ts, end_ts, taken_by"
                    " FROM slots WHERE slot_id=%s FOR UPDATE",
                    (cmd.slot_id,),
                )
                s = cur.fetchone()
                if s is None:
                    raise SlotNotFound(f"unknown slot {cmd.slot_id}")
                if s["taken_by"] is not None:
                    raise SlotUnavailable(f"slot {cmd.slot_id} just taken")
                cur.execute(
                    """INSERT INTO appointments
                               (booking_id, patient_id, doctor_id, slot_id,
                                start_ts, end_ts, status, idempotency_key, created_at)
                               VALUES(%s,%s,%s,%s,%s,%s,'active',%s,%s)""",
                    (
                        booking_id, cmd.patient.patient_id, s["doctor_id"],
                        s["slot_id"], s["start_ts"], s["end_ts"],
                        cmd.idempotency_key, now,
                    ),
                )
                cur.execute(
                    "UPDATE slots SET taken_by=%s WHERE slot_id=%s",
                    (cmd.patient.patient_id, cmd.slot_id),
                )
        except psycopg.errors.UniqueViolation:
            # Lost a cross-process insert race on idempotency_key: the winner's
            # row is the truth — return it instead of a duplicate.
            with self._connect() as conn, conn.cursor() as cur:
                row = self._by_key(cur, cmd.idempotency_key)
                assert row is not None
                return self._to_appt(row)
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM appointments WHERE booking_id=%s", (booking_id,)
            )
            return self._to_appt(cur.fetchone())

    def reschedule_appointment(self, cmd: RescheduleCommand) -> Appointment:
        new_id = "A" + uuid.uuid4().hex[:6].upper()
        now = _s(datetime.now().replace(second=0, microsecond=0))
        try:
            with self._connect() as conn, conn.cursor() as cur, conn.transaction():
                if (row := self._by_key(cur, cmd.idempotency_key)) is not None:
                    return self._to_appt(row)
                cur.execute(
                    "SELECT * FROM appointments WHERE booking_id=%s"
                    " AND patient_id=%s FOR UPDATE",
                    (cmd.booking_id, cmd.patient.patient_id),
                )
                old = cur.fetchone()
                if old is None or old["status"] != AppointmentStatus.ACTIVE.value:
                    raise AppointmentNotFound(
                        f"booking {cmd.booking_id} not active"
                    )
                cur.execute(
                    "SELECT slot_id, doctor_id, start_ts, end_ts, taken_by"
                    " FROM slots WHERE slot_id=%s FOR UPDATE",
                    (cmd.new_slot_id,),
                )
                tgt = cur.fetchone()
                if tgt is None:
                    raise SlotNotFound(f"unknown slot {cmd.new_slot_id}")
                if tgt["taken_by"] is not None:
                    raise SlotUnavailable(f"slot {cmd.new_slot_id} just taken")
                cur.execute(
                    "UPDATE slots SET taken_by=NULL WHERE slot_id=%s",
                    (old["slot_id"],),
                )
                cur.execute(
                    "UPDATE slots SET taken_by=%s WHERE slot_id=%s",
                    (cmd.patient.patient_id, cmd.new_slot_id),
                )
                cur.execute(
                    "UPDATE appointments SET status='moved' WHERE booking_id=%s",
                    (cmd.booking_id,),
                )
                cur.execute(
                    """INSERT INTO appointments
                               (booking_id, patient_id, doctor_id, slot_id,
                                start_ts, end_ts, status, idempotency_key, created_at)
                               VALUES(%s,%s,%s,%s,%s,%s,'active',%s,%s)""",
                    (
                        new_id, cmd.patient.patient_id, tgt["doctor_id"],
                        tgt["slot_id"], tgt["start_ts"], tgt["end_ts"],
                        cmd.idempotency_key, now,
                    ),
                )
        except psycopg.errors.UniqueViolation:
            with self._connect() as conn, conn.cursor() as cur:
                row = self._by_key(cur, cmd.idempotency_key)
                assert row is not None
                return self._to_appt(row)
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM appointments WHERE booking_id=%s", (new_id,)
            )
            return self._to_appt(cur.fetchone())

    def purge_stale_idempotency_keys(self, older_than_days: int = 30) -> int:
        cutoff = _s(datetime.now() - timedelta(days=older_than_days))
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """SELECT booking_id FROM appointments
                       WHERE status != 'active' AND created_at < %s
                       AND idempotency_key NOT LIKE 'purged:%%'""",
                    (cutoff,),
                )
                ids = [r["booking_id"] for r in cur.fetchall()]
                for bid in ids:
                    cur.execute(
                        "UPDATE appointments SET idempotency_key=%s WHERE booking_id=%s",
                        (f"purged:{bid}", bid),
                    )
            conn.commit()
            return len(ids)

    def steal_slot(self, slot_id: str, by: str = "other_patient") -> None:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE slots SET taken_by=%s WHERE slot_id=%s", (by, slot_id)
                )
            conn.commit()
