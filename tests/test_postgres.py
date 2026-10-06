"""Postgres adapter parity — runs only with DBI_PG_URL (CI service + local docker).

Every test here mirrors a SQLite guarantee: same seed, same atomicity,
same idempotency, same cross-patient isolation.
"""
from __future__ import annotations

import os
import uuid

import pytest

from clinic_adapter.postgres import PostgresClinic
from domain.commands import CreateAppointmentCommand, RescheduleCommand
from domain.errors import AppointmentNotFound, SlotUnavailable
from domain.models import PatientRef, Specialty

PG_URL = os.getenv("DBI_PG_URL", "")
needs_pg = pytest.mark.skipif(not PG_URL, reason="DBI_PG_URL not set")


def _clinic() -> PostgresClinic:
    return PostgresClinic(url=PG_URL)


@needs_pg
def test_pg_seed_and_reads():
    c = _clinic()
    docs = c.find_doctors(specialty=Specialty.CARDIOLOGY)
    assert {d.doctor_id for d in docs} == {"d_ivanov", "d_kozlov"}
    assert c.find_doctors(name_query="иванов")[0].doctor_id == "d_ivanov"
    slots = c.find_slots(specialty=Specialty.NEUROLOGY, time_pref="evening", limit=3)
    assert len(slots) == 3
    assert all(s.slot.start.hour >= 17 for s in slots)


@needs_pg
def test_pg_atomic_create_and_race():
    c = _clinic()
    p = PatientRef(patient_id=f"pg-{uuid.uuid4().hex[:6]}")
    slot = c.find_slots(limit=5)[0].slot.slot_id
    appt = c.create_appointment(
        CreateAppointmentCommand(patient=p, slot_id=slot, idempotency_key=f"ik-{uuid.uuid4().hex[:8]}")
    )
    assert appt.booking_id.startswith("A")
    assert slot not in {s.slot.slot_id for s in c.find_slots(limit=500)}
    # true retry (same key + same payload) dedups
    again = c.create_appointment(
        CreateAppointmentCommand(patient=p, slot_id=slot, idempotency_key=appt.idempotency_key)
    )
    assert again.booking_id == appt.booking_id
    # stolen slot raises
    victim = c.find_slots(limit=5)[0].slot.slot_id
    c.steal_slot(victim)
    with pytest.raises(SlotUnavailable):
        c.create_appointment(
            CreateAppointmentCommand(
                patient=p, slot_id=victim, idempotency_key=f"ik-{uuid.uuid4().hex[:8]}"
            )
        )


@needs_pg
def test_pg_reschedule_and_isolation():
    c = _clinic()
    alice = PatientRef(patient_id=f"alice-{uuid.uuid4().hex[:6]}")
    bob = PatientRef(patient_id=f"bob-{uuid.uuid4().hex[:6]}")
    slot = c.find_slots(limit=5)[0].slot.slot_id
    created = c.create_appointment(
        CreateAppointmentCommand(
            patient=alice, slot_id=slot, idempotency_key=f"ik-{uuid.uuid4().hex[:8]}"
        )
    )
    assert c.get_appointments(bob) == []
    target = next(s for s in c.find_slots(limit=20) if s.slot.start != created.start)
    moved = c.reschedule_appointment(
        RescheduleCommand(
            patient=alice,
            booking_id=created.booking_id,
            new_slot_id=target.slot.slot_id,
            idempotency_key=f"ik-{uuid.uuid4().hex[:8]}",
        )
    )
    assert moved.slot_id == target.slot.slot_id and moved.start != created.start
    with pytest.raises(AppointmentNotFound):
        c.reschedule_appointment(
            RescheduleCommand(
                patient=bob,
                booking_id=created.booking_id,
                new_slot_id=target.slot.slot_id,
                idempotency_key=f"ik-{uuid.uuid4().hex[:8]}",
            )
        )


@needs_pg
def test_pg_concurrent_same_key_create_and_reschedule():
    import threading

    c = _clinic()
    pid = f"conc-{uuid.uuid4().hex[:6]}"
    p = PatientRef(patient_id=pid)
    slot = c.find_slots(limit=5)[0].slot.slot_id
    ik = f"ik-conc-{uuid.uuid4().hex[:6]}"
    barrier = threading.Barrier(2)
    results: list = []
    errors: list = []

    def run_create():
        try:
            barrier.wait(timeout=15)
            results.append(
                c.create_appointment(
                    CreateAppointmentCommand(patient=p, slot_id=slot, idempotency_key=ik)
                ).booking_id
            )
        except Exception as e:
            errors.append(e)

    threads = [threading.Thread(target=run_create) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors, errors
    assert results[0] == results[1]
    assert len(c.get_appointments(p)) == 1

    # same for reschedule: both threads move the same booking with same key
    created = c.get_appointments(p)[0]
    target = next(s for s in c.find_slots(limit=30) if s.slot.start != created.start)
    rik = f"ik-conc-r-{uuid.uuid4().hex[:6]}"
    barrier2 = threading.Barrier(2)
    results2: list = []
    errors2: list = []

    def run_move():
        try:
            barrier2.wait(timeout=15)
            results2.append(
                c.reschedule_appointment(
                    RescheduleCommand(
                        patient=p, booking_id=created.booking_id,
                        new_slot_id=target.slot.slot_id, idempotency_key=rik,
                    )
                ).booking_id
            )
        except Exception as e:
            errors2.append(e)

    threads = [threading.Thread(target=run_move) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors2, errors2
    assert results2[0] == results2[1]
    assert len(c.get_appointments(p)) == 1


@needs_pg
def test_pg_scoped_idempotency():
    from domain.errors import IdempotencyConflict

    c = _clinic()
    key = f"shared-{uuid.uuid4().hex[:6]}"
    slots = [s.slot.slot_id for s in c.find_slots(limit=10)]
    alice = PatientRef(patient_id=f"alice-{uuid.uuid4().hex[:6]}")
    bob = PatientRef(patient_id=f"bob-{uuid.uuid4().hex[:6]}")
    r1 = c.create_appointment(
        CreateAppointmentCommand(patient=alice, slot_id=slots[0], idempotency_key=key)
    )
    # same key, other patient -> isolated namespace, both succeed
    r2 = c.create_appointment(
        CreateAppointmentCommand(
            patient=bob,
            slot_id=next(s for s in slots if s != slots[0]),
            idempotency_key=key,
        )
    )
    assert r1.booking_id != r2.booking_id
    # same key, same patient, other payload -> conflict, no extra booking
    other = next(s.slot.slot_id for s in c.find_slots(limit=50) if s.slot.slot_id != slots[0])
    with pytest.raises(IdempotencyConflict):
        c.create_appointment(
            CreateAppointmentCommand(patient=alice, slot_id=other, idempotency_key=key)
        )
    assert len(c.get_appointments(alice)) == 1
