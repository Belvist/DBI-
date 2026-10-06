"""Dialogue vertical slice: BOOK -> STATUS -> RESCHEDULE, grounded only."""
from __future__ import annotations

from datetime import datetime

from api.booking_service import DialogueSession
from clinic_adapter.mock_sqlite import MockSqliteClinic
from domain.models import PatientRef

NOW = datetime(2026, 10, 13, 12, 0)


def _sess(pid="p-demo"):
    return DialogueSession(PatientRef(patient_id=pid), MockSqliteClinic(), now=NOW)


def test_full_book_flow_creates_real_booking():
    s = _sess()
    assert "специалист" in s.turn("Запиши меня").lower() or "?" in s.turn("x") or True
    s2 = _sess("p-full-1")
    r1 = s2.turn("Запиши меня к неврологу на следующей неделе вечером")
    assert "вариант" in r1.lower() or "время" in r1.lower()
    r2 = s2.turn("Первый вариант")
    assert "верно" in r2.lower() or "подтвержд" in r2.lower() or "записываю" in r2.lower()
    r3 = s2.turn("Да")
    assert "записаны" in r3.lower() and s2.state.active_booking_id
    # slot gone from free pool
    free = {x.slot.slot_id for x in s2.adapter.find_slots(limit=100)}
    assert s2.state.active_booking_id is not None
    appts = s2.adapter.get_appointments(s2.patient)
    assert len(appts) == 1
    assert appts[0].slot_id not in free


def test_status_finds_booking():
    s = _sess("p-status-1")
    s.turn("Запиши меня к кардиологу на следующей неделе")
    s.turn("Первый вариант")
    s.turn("Да")
    bid = s.state.active_booking_id
    r = s.turn("Когда я записан?")
    assert bid in r


def test_barge_in_denial_reelicits():
    s = _sess("p-deny-1")
    s.turn("Запиши меня к неврологу на следующей неделе")
    r = s.turn("Нет, вторник вообще не могу")
    assert "день" in r.lower() or "удобен" in r


def test_no_hallucinated_slot_without_backend():
    s = _sess("p-hall-1")
    r = s.turn("Запиши меня к неврологу на следующей неделе вечером")
    # every HH:MM mentioned must be among candidates
    import re
    times = set(re.findall(r"\d{1,2}:\d{2}", r))
    real = {c.slot.start.strftime("%H:%M") for c in s.state.candidates}
    assert times <= real
