"""Postgres-backed clinic. Same contract as the SQLite mock, real durability.

- Pooled connections (psycopg_pool); schema managed by migrations/*.sql.
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
from psycopg_pool import ConnectionPool

from clinic_adapter.migrate import apply as apply_migrations
from clinic_adapter.seed_data import SEED_BASE, SEED_DOCTORS, seed_slots
from clinic_adapter.seed_data import dt as _dt
from clinic_adapter.seed_data import ts as _s
from domain.commands import CreateAppointmentCommand, RescheduleCommand
from domain.errors import (
    AppointmentNotFound,
    IdempotencyConflict,
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

_CONNECT_TIMEOUT = 5
_STATEMENT_TIMEOUT_MS = 5000


class PostgresClinic:
    def __init__(
        self,
        url: str | None = None,
        seed_base: datetime | None = None,
        seed: bool = True,
        pool_max: int = 10,
    ) -> None:
        self._url = url or os.getenv("DBI_PG_URL", "")
        if not self._url:
            raise NotConfigured("DBI_PG_URL is not set")
        self._seed_base = seed_base or SEED_BASE
        self._allow_seed = seed
        self._pool = ConnectionPool(
            self._url,
            min_size=1,
            max_size=max(1, pool_max),
            timeout=10,
            open=True,
            kwargs={
                "row_factory": dict_row,
                "connect_timeout": _CONNECT_TIMEOUT,
                "options": f"-c statement_timeout={_STATEMENT_TIMEOUT_MS}",
            },
        )
        self._init_schema()

    def close(self) -> None:
        self._pool.close()

    def _init_schema(self) -> None:
        apply_migrations(self._url)
        with self._pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT COUNT(*) AS n FROM doctors")
                if self._allow_seed and cur.fetchone()["n"] == 0:  # type: ignore[index]
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
        with self._pool.connection() as conn, conn.cursor() as cur:
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
        with self._pool.connection() as conn, conn.cursor() as cur:
            cur.execute(q, args)
            out = [self._to_swd(r) for r in cur.fetchall()]
        if time_pref == "morning":
            out = [x for x in out if x.slot.start.hour < 13]
        elif time_pref == "evening":
            out = [x for x in out if x.slot.start.hour >= 17]
        return out[:limit]

    def get_appointments(self, patient: PatientRef) -> list[Appointment]:
        with self._pool.connection() as conn, conn.cursor() as cur:
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
    # Idempotency is scoped by (patient_id, operation_kind, key): one
    # patient's key never aliases another patient's booking, and a key
    # reused for a different payload raises IdempotencyConflict (409).
    def _find_op(self, cur, patient_id: str, kind: str, key: str) -> dict | None:
        cur.execute(
            "SELECT * FROM idempotency_operations"
            " WHERE patient_id=%s AND operation_kind=%s AND idempotency_key=%s",
            (patient_id, kind, key),
        )
        return cur.fetchone()

    def _booking_by_id(self, cur, booking_id: str) -> Appointment:
        cur.execute("SELECT * FROM appointments WHERE booking_id=%s", (booking_id,))
        return self._to_appt(cur.fetchone())

    def _resolve_op(self, cur, patient_id: str, kind: str, key: str, fp: str) -> Appointment:
        op = self._find_op(cur, patient_id, kind, key)
        if op is None:
            raise AssertionError("op row vanished mid-race")
        if op["request_hash"] != fp:
            raise IdempotencyConflict(
                f"key {key!r} already used for a different {kind} operation"
            )
        return self._booking_by_id(cur, op["result_booking_id"])

    def create_appointment(self, cmd: CreateAppointmentCommand) -> Appointment:
        from clinic_adapter.idempotency import fingerprint_create

        booking_id = "A" + uuid.uuid4().hex[:6].upper()
        now = _s(datetime.now().replace(second=0, microsecond=0))
        fp = fingerprint_create(cmd)
        pid = cmd.patient.patient_id
        try:
            with self._pool.connection() as conn, conn.cursor() as cur, conn.transaction():
                if self._find_op(cur, pid, "create", cmd.idempotency_key) is not None:
                    return self._resolve_op(cur, pid, "create", cmd.idempotency_key, fp)
                cur.execute(
                    "SELECT slot_id, doctor_id, start_ts, end_ts, taken_by"
                    " FROM slots WHERE slot_id=%s FOR UPDATE",
                    (cmd.slot_id,),
                )
                s = cur.fetchone()
                if s is None:
                    raise SlotNotFound(f"unknown slot {cmd.slot_id}")
                if s["taken_by"] is not None:
                    # A concurrent SAME operation may have committed while we
                    # waited on the row lock: re-check before crying conflict.
                    if self._find_op(cur, pid, "create", cmd.idempotency_key) is not None:
                        return self._resolve_op(cur, pid, "create", cmd.idempotency_key, fp)
                    raise SlotUnavailable(f"slot {cmd.slot_id} just taken")
                cur.execute(
                    """INSERT INTO appointments
                               (booking_id, patient_id, doctor_id, slot_id,
                                start_ts, end_ts, status, idempotency_key, created_at)
                               VALUES(%s,%s,%s,%s,%s,%s,'active',%s,%s)""",
                    (
                        booking_id, pid, s["doctor_id"],
                        s["slot_id"], s["start_ts"], s["end_ts"],
                        cmd.idempotency_key, now,
                    ),
                )
                cur.execute(
                    """INSERT INTO idempotency_operations
                       (patient_id, operation_kind, idempotency_key,
                        request_hash, result_booking_id, created_at)
                       VALUES(%s,'create',%s,%s,%s,%s)""",
                    (pid, cmd.idempotency_key, fp, booking_id, now),
                )
                cur.execute(
                    "UPDATE slots SET taken_by=%s WHERE slot_id=%s",
                    (pid, cmd.slot_id),
                )
        except psycopg.errors.UniqueViolation:
            # Lost a cross-process insert race: the winner's op row is the
            # truth — resolve it (or conflict on payload mismatch).
            with self._pool.connection() as conn, conn.cursor() as cur:
                return self._resolve_op(cur, pid, "create", cmd.idempotency_key, fp)
        with self._pool.connection() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM appointments WHERE booking_id=%s", (booking_id,)
            )
            return self._to_appt(cur.fetchone())

    def reschedule_appointment(self, cmd: RescheduleCommand) -> Appointment:
        from clinic_adapter.idempotency import fingerprint_reschedule

        new_id = "A" + uuid.uuid4().hex[:6].upper()
        now = _s(datetime.now().replace(second=0, microsecond=0))
        fp = fingerprint_reschedule(cmd)
        pid = cmd.patient.patient_id
        try:
            with self._pool.connection() as conn, conn.cursor() as cur, conn.transaction():
                if self._find_op(cur, pid, "reschedule", cmd.idempotency_key) is not None:
                    return self._resolve_op(cur, pid, "reschedule", cmd.idempotency_key, fp)
                cur.execute(
                    "SELECT * FROM appointments WHERE booking_id=%s"
                    " AND patient_id=%s FOR UPDATE",
                    (cmd.booking_id, pid),
                )
                old = cur.fetchone()
                if old is None or old["status"] != AppointmentStatus.ACTIVE.value:
                    # The winner of a concurrent same-key retry marks the old
                    # booking moved: re-check before reporting it missing.
                    if self._find_op(cur, pid, "reschedule", cmd.idempotency_key) is not None:
                        return self._resolve_op(cur, pid, "reschedule", cmd.idempotency_key, fp)
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
                    if self._find_op(cur, pid, "reschedule", cmd.idempotency_key) is not None:
                        return self._resolve_op(cur, pid, "reschedule", cmd.idempotency_key, fp)
                    raise SlotUnavailable(f"slot {cmd.new_slot_id} just taken")
                cur.execute(
                    "UPDATE slots SET taken_by=NULL WHERE slot_id=%s",
                    (old["slot_id"],),
                )
                cur.execute(
                    "UPDATE slots SET taken_by=%s WHERE slot_id=%s",
                    (pid, cmd.new_slot_id),
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
                        new_id, pid, tgt["doctor_id"],
                        tgt["slot_id"], tgt["start_ts"], tgt["end_ts"],
                        cmd.idempotency_key, now,
                    ),
                )
                cur.execute(
                    """INSERT INTO idempotency_operations
                       (patient_id, operation_kind, idempotency_key,
                        request_hash, result_booking_id, created_at)
                       VALUES(%s,'reschedule',%s,%s,%s,%s)""",
                    (pid, cmd.idempotency_key, fp, new_id, now),
                )
        except psycopg.errors.UniqueViolation:
            with self._pool.connection() as conn, conn.cursor() as cur:
                return self._resolve_op(cur, pid, "reschedule", cmd.idempotency_key, fp)
        with self._pool.connection() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM appointments WHERE booking_id=%s", (new_id,)
            )
            return self._to_appt(cur.fetchone())

    def purge_stale_idempotency_keys(self, older_than_days: int = 30) -> int:
        """Delete op rows whose result booking is non-active and old.

        ACTIVE bookings are never touched: a late retry must still dedup.
        """
        cutoff = _s(datetime.now() - timedelta(days=older_than_days))
        with self._pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """DELETE FROM idempotency_operations WHERE result_booking_id IN (
                         SELECT booking_id FROM appointments
                         WHERE status != 'active' AND created_at < %s
                       )""",
                    (cutoff,),
                )
                n = cur.rowcount
            conn.commit()
            return n

    def steal_slot(self, slot_id: str, by: str = "other_patient") -> None:
        with self._pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE slots SET taken_by=%s WHERE slot_id=%s", (by, slot_id)
                )
            conn.commit()

    def ping(self) -> None:
        """Lightweight health check - does not mutate state."""
        with self._pool.connection() as conn:
            conn.execute("SELECT 1").fetchone()
