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
    # replay dedups
    again = c.create_appointment(
        CreateAppointmentCommand(patient=p, slot_id="s_other", idempotency_key=appt.idempotency_key)
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
