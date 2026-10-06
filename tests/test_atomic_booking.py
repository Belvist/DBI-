"""Atomic booking: race + idempotency are first-class guarantees."""
from __future__ import annotations

import uuid

import pytest

from clinic_adapter.mock_sqlite import MockSqliteClinic
from domain.commands import CreateAppointmentCommand, RescheduleCommand
from domain.errors import SlotUnavailable
from domain.models import PatientRef


def _cmd(adapter, patient_id="p1", slot_idx=0, ik=None):
    slots = adapter.find_slots(limit=10)
    assert slots, "seed must provide slots"
    return CreateAppointmentCommand(
        patient=PatientRef(patient_id=patient_id),
        slot_id=slots[slot_idx].slot.slot_id,
        idempotency_key=ik or f"ik-{uuid.uuid4().hex[:10]}",
    )


def test_create_then_slot_not_free():
    a = MockSqliteClinic()
    cmd = _cmd(a)
    appt = a.create_appointment(cmd)
    assert appt.booking_id.startswith("A")
    free_ids = {s.slot.slot_id for s in a.find_slots(limit=100)}
    assert cmd.slot_id not in free_ids


def test_duplicate_idempotency_returns_same_booking():
    a = MockSqliteClinic()
    ik = "ik-dup-test-1"
    c1 = _cmd(a, ik=ik)
    r1 = a.create_appointment(c1)
    c2 = CreateAppointmentCommand(patient=c1.patient, slot_id="s_other", idempotency_key=ik)
    # same key, different slot -> still original (replay, not new booking)
    r2 = a.create_appointment(c2)
    assert r1.booking_id == r2.booking_id
    assert len(a.get_appointments(c1.patient)) == 1


def test_race_stolen_slot_raises():
    a = MockSqliteClinic()
    cmd = _cmd(a)
    a.steal_slot(cmd.slot_id)
    with pytest.raises(SlotUnavailable):
        a.create_appointment(cmd)


def test_reschedule_moves_atomically():
    a = MockSqliteClinic()
    p = PatientRef(patient_id="p9")
    created = a.create_appointment(_cmd(a, patient_id="p9"))
    target = next(s for s in a.find_slots(limit=20) if s.slot.start != created.start)
    moved = a.reschedule_appointment(
        RescheduleCommand(
            patient=p, booking_id=created.booking_id,
            new_slot_id=target.slot.slot_id, idempotency_key=f"ik-{uuid.uuid4().hex[:10]}",
        )
    )
    assert moved.start == target.slot.start
    active = a.get_appointments(p)
    assert len(active) == 1 and active[0].booking_id == moved.booking_id
