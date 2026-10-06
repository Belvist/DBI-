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
    # true retry: same key + same payload -> original, no duplicate
    c2 = CreateAppointmentCommand(
        patient=c1.patient, slot_id=c1.slot_id, idempotency_key=ik
    )
    r2 = a.create_appointment(c2)
    assert r1.booking_id == r2.booking_id
    assert len(a.get_appointments(c1.patient)) == 1


def test_same_key_different_payload_conflicts():
    from domain.errors import IdempotencyConflict

    a = MockSqliteClinic()
    ik = "ik-reuse-test-1"
    r1 = a.create_appointment(_cmd(a, ik=ik))
    other_slot = next(
        s.slot.slot_id
        for s in a.find_slots(limit=20)
        if s.slot.slot_id != r1.slot_id
    )
    with pytest.raises(IdempotencyConflict):
        a.create_appointment(
            CreateAppointmentCommand(
                patient=PatientRef(patient_id="p1"),
                slot_id=other_slot,
                idempotency_key=ik,
            )
        )
    assert len(a.get_appointments(PatientRef(patient_id="p1"))) == 1


def test_same_key_is_isolated_across_patients():
    a = MockSqliteClinic()
    ik = "ik-shared-1"
    slots = [s.slot.slot_id for s in a.find_slots(limit=10)]
    r1 = a.create_appointment(_cmd(a, patient_id="alice", slot_idx=0, ik=ik))
    r2 = a.create_appointment(
        CreateAppointmentCommand(
            patient=PatientRef(patient_id="bob"),
            slot_id=next(s for s in slots if s != r1.slot_id),
            idempotency_key=ik,
        )
    )
    assert r1.booking_id != r2.booking_id
    assert len(a.get_appointments(PatientRef(patient_id="alice"))) == 1
    assert len(a.get_appointments(PatientRef(patient_id="bob"))) == 1


def test_same_key_isolated_across_kinds():
    from domain.errors import IdempotencyConflict

    a = MockSqliteClinic()
    p = PatientRef(patient_id="p-kinds")
    created = a.create_appointment(_cmd(a, patient_id="p-kinds", ik="ik-kind-1"))
    target = next(s for s in a.find_slots(limit=20) if s.slot.start != created.start)
    # same key for a DIFFERENT kind is a separate namespace, not a conflict
    moved = a.reschedule_appointment(
        RescheduleCommand(
            patient=p, booking_id=created.booking_id,
            new_slot_id=target.slot.slot_id, idempotency_key="ik-kind-1",
        )
    )
    assert moved.booking_id != created.booking_id
    # ...but reusing a create-key for another create payload still conflicts
    other = next(
        s.slot.slot_id
        for s in a.find_slots(limit=50)
        if s.slot.slot_id != created.slot_id
    )
    with pytest.raises(IdempotencyConflict):
        a.create_appointment(
            CreateAppointmentCommand(
                patient=p, slot_id=other, idempotency_key="ik-kind-1"
            )
        )


def test_race_stolen_slot_raises():
    a = MockSqliteClinic()
    cmd = _cmd(a)
    a.steal_slot(cmd.slot_id)
    with pytest.raises(SlotUnavailable):
        a.create_appointment(cmd)


def test_purge_touches_only_stale_non_active_keys():
    a = MockSqliteClinic()
    p = PatientRef(patient_id="p-purge")
    created = a.create_appointment(_cmd(a, patient_id="p-purge"))
    target = next(s for s in a.find_slots(limit=20) if s.slot.start != created.start)
    moved = a.reschedule_appointment(
        RescheduleCommand(
            patient=p, booking_id=created.booking_id,
            new_slot_id=target.slot.slot_id, idempotency_key=f"ik-{uuid.uuid4().hex[:10]}",
        )
    )
    # backdate the moved (non-active) record beyond TTL
    a._conn.execute(
        "UPDATE appointments SET created_at=? WHERE booking_id=?",
        ("2020-01-01T00:00", created.booking_id),
    )
    a._conn.commit()
    assert a.purge_stale_idempotency_keys(older_than_days=30) == 1
    # active booking op untouched: true retry of the same reschedule dedups
    again = a.reschedule_appointment(
        RescheduleCommand(
            patient=p, booking_id=created.booking_id,
            new_slot_id=target.slot.slot_id, idempotency_key=moved.idempotency_key,
        )
    )
    assert again.booking_id == moved.booking_id
    assert len(a.get_appointments(p)) == 1


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
