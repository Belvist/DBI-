"""Dialogue vertical slice: BOOK -> STATUS -> RESCHEDULE, grounded only."""
from __future__ import annotations

from datetime import datetime

from api.booking_service import DialogueSession
from clinic_adapter.mock_sqlite import MockSqliteClinic
from dialogue.state import Phase
from domain.models import PatientRef

NOW = datetime(2026, 10, 13, 12, 0)


def _sess(pid="p-demo"):
    return DialogueSession(PatientRef(patient_id=pid), MockSqliteClinic(), now=NOW)


def _book_one(s: DialogueSession, utterance: str = "Запиши меня к неврологу на следующей неделе вечером"):
    s.turn(utterance)
    assert s.state.candidates
    s.turn("Первый вариант")
    s.turn("Да")
    appts = s.adapter.get_appointments(s.patient)
    assert len(appts) == 1
    return appts[0]


def test_full_book_flow_creates_real_booking():
    s = _sess()
    first = s.turn("Запиши меня")
    assert "специалист" in first.lower()

    s2 = _sess("p-full-1")
    r1 = s2.turn("Запиши меня к неврологу на следующей неделе вечером")
    assert "вариант" in r1.lower() or "время" in r1.lower()

    r2 = s2.turn("Первый вариант")
    assert "подтверд" in r2.lower() or "верно" in r2.lower()

    r3 = s2.turn("Да")
    assert "записаны" in r3.lower()
    assert s2.state.active_booking_id

    free = {x.slot.slot_id for x in s2.adapter.find_slots(limit=100)}
    appts = s2.adapter.get_appointments(s2.patient)
    assert len(appts) == 1
    assert appts[0].slot_id not in free


def test_exact_doctor_restricts_candidates_to_that_doctor():
    s = _sess("p-doctor-1")
    r = s.turn("Хочу записаться к Иванову на следующей неделе")
    assert "Иванов" in r
    assert s.state.candidates
    assert {c.doctor.doctor_id for c in s.state.candidates} == {"d_ivanov"}


def test_status_finds_booking():
    s = _sess("p-status-1")
    _book_one(s, "Запиши меня к кардиологу на следующей неделе")
    bid = s.state.active_booking_id
    r = s.turn("Когда я записан?")
    assert bid
    assert bid in r


def test_reschedule_keeps_same_doctor_by_default():
    s = _sess("p-reschedule-same-doctor")
    old = _book_one(s)
    r1 = s.turn("Не смогу. Давай в четверг утром")
    assert s.state.candidates
    assert {c.doctor.doctor_id for c in s.state.candidates} == {old.doctor_id}
    assert "четверг" in r1.lower()

    s.turn("Первый вариант")
    r3 = s.turn("Да")
    assert "перенесена" in r3.lower()

    active = s.adapter.get_appointments(s.patient)
    assert len(active) == 1
    assert active[0].doctor_id == old.doctor_id
    assert active[0].start != old.start


def test_barge_in_denial_reelicits():
    s = _sess("p-deny-1")
    s.turn("Запиши меня к неврологу на следующей неделе")
    r = s.turn("Нет, вторник вообще не могу")
    assert "день" in r.lower() or "удобен" in r.lower()


def test_no_hallucinated_slot_without_backend():
    s = _sess("p-hall-1")
    r = s.turn("Запиши меня к неврологу на следующей неделе вечером")
    import re

    times = set(re.findall(r"\d{1,2}:\d{2}", r))
    real = {c.slot.start.strftime("%H:%M") for c in s.state.candidates}
    assert times <= real


def test_finished_booking_resets_search_context():
    s = _sess("p-reset-1")
    s.turn("Запиши меня к неврологу на следующей неделе вечером")
    s.turn("Первый вариант")
    s.turn("Да")
    assert s.state.active_booking_id
    # next bare request must start clean, not reuse stale neuro+dates
    r = s.turn("хочу записаться")
    assert "специалист" in r.lower()
    assert s.state.phase == Phase.ELICIT


def test_exact_time_narrows_or_reports_honestly():
    s = _sess("p-time-1")
    s.turn("Запиши меня к неврологу на следующей неделе")
    assert s.state.candidates
    have_20 = any(c.slot.start.strftime("%H:%M") == "20:00" for c in s.state.candidates)
    r = s.turn("мне надо в 20:00")
    if have_20:
        assert "20:00" in r
        assert all(
            c.slot.start.strftime("%H:%M") == "20:00" for c in s.state.candidates
        )
    else:
        assert "20:00" in r and "нет" in r.lower()
