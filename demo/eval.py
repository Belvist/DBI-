"""Scenario eval — 14 cases including failures and race conditions.

Run: python -m demo.eval
Exit 0 = all pass.
"""
from __future__ import annotations

import json
import re
import sys
import uuid
from datetime import datetime
from pathlib import Path

from api.booking_service import DialogueSession
from clinic_adapter.mock_sqlite import MockSqliteClinic
from domain.commands import CreateAppointmentCommand
from domain.models import PatientRef

NOW = datetime(2026, 10, 13, 12, 0)
TIME_RE = re.compile(r"(\d{1,2}:\d{2})")


def _fresh(pid: str | None = None) -> tuple[DialogueSession, MockSqliteClinic]:
    adapter = MockSqliteClinic()
    sess = DialogueSession(
        PatientRef(patient_id=pid or ("p-" + uuid.uuid4().hex[:6])),
        adapter,
        now=NOW,
    )
    return sess, adapter


def _book_one(sess: DialogueSession):
    sess.turn("Запиши меня к неврологу на следующей неделе вечером")
    assert sess.state.candidates, "booking flow must propose real candidates"
    sess.turn("Первый вариант")
    result = sess.turn("Да")
    appts = sess.adapter.get_appointments(sess.patient)
    assert len(appts) == 1
    assert "записаны" in result.lower()
    return appts[0]


def run() -> int:
    path = Path(__file__).with_name("scenarios.json")
    scenarios = json.loads(path.read_text(encoding="utf-8"))
    passed = 0
    failed: list[str] = []

    for sc in scenarios:
        sid = sc["id"]
        try:
            ok, detail = _run_one(sid)
        except Exception as exc:
            ok, detail = False, f"EXC {type(exc).__name__}: {exc}"

        print(f"{'PASS' if ok else 'FAIL'}  {sid:28s} {detail}")
        if ok:
            passed += 1
        else:
            failed.append(sid)

    print(f"\n{passed}/{len(scenarios)} passed")
    return 0 if not failed else 1


def _run_one(sid: str) -> tuple[bool, str]:
    if sid == "BOOK_exact_doctor":
        s, a = _fresh()
        r = s.turn("Хочу записаться к Иванову на следующей неделе")
        ok = (
            bool(s.state.candidates)
            and {c.doctor.doctor_id for c in s.state.candidates} == {"d_ivanov"}
            and "Иванов" in r
            and _grounded(r, a, s)
        )
        return ok, r[:100]

    if sid == "BOOK_specialty_only":
        s, _ = _fresh()
        r = s.turn("Хочу записаться к неврологу")
        return (
            s.state.phase.value == "elicit"
            and ("день" in r.lower() or "удобен" in r.lower()),
            r[:100],
        )

    if sid == "BOOK_next_week_evening":
        s, a = _fresh()
        r = s.turn("Запиши меня к кардиологу на следующей неделе вечером")
        ok = (
            bool(s.state.candidates)
            and all(c.doctor.specialty.value == "cardiology" for c in s.state.candidates)
            and all(c.slot.start.hour >= 17 for c in s.state.candidates)
            and _grounded(r, a, s)
        )
        return ok, r[:100]

    if sid == "BOOK_no_slots":
        s, _ = _fresh()
        r = s.turn("Запиши меня к дерматологу 1 января")
        return (
            not s.state.candidates
            and ("нет" in r.lower() or "свободного" in r.lower()),
            r[:100],
        )

    if sid == "BOOK_race_before_commit":
        s, a = _fresh()
        s.turn("Запиши меня к неврологу на следующей неделе вечером")
        assert s.state.candidates
        victim = s.state.candidates[0].slot.slot_id
        s.turn("Первый вариант")
        a.steal_slot(victim)
        r = s.turn("Да")
        ok = (
            ("недоступно" in r.lower() or "другой вариант" in r.lower())
            and not a.get_appointments(s.patient)
        )
        return ok, r[:110]

    if sid == "GET_STATUS":
        s, _ = _fresh()
        appt = _book_one(s)
        r = s.turn("Когда я записан?")
        return (
            appt.booking_id in r
            and appt.start.strftime("%H:%M") in r,
            r[:110],
        )

    if sid == "RESCHEDULE":
        s, _ = _fresh()
        old = _book_one(s)
        r1 = s.turn("Не смогу. Давай в четверг утром")
        assert s.state.candidates
        same_doctor = {c.doctor.doctor_id for c in s.state.candidates} == {old.doctor_id}
        s.turn("Первый вариант")
        r3 = s.turn("Да")
        active = s.adapter.get_appointments(s.patient)
        ok = (
            same_doctor
            and len(active) == 1
            and active[0].doctor_id == old.doctor_id
            and active[0].start != old.start
            and active[0].start.weekday() == 3
            and active[0].start.hour < 13
            and "перенесена" in r3.lower()
        )
        return ok, (r1 + " | " + r3)[:120]

    if sid == "RESCHEDULE_no_alternatives":
        s, _ = _fresh()
        _book_one(s)
        r = s.turn("Перенеси на 1 января")
        return (
            not s.state.candidates
            and ("нет" in r.lower() or "другие дни" in r.lower()),
            r[:100],
        )

    if sid == "GET_SCHEDULE":
        s, a = _fresh()
        r = s.turn("Кто принимает на следующей неделе?")
        return (
            bool(s.state.candidates)
            and _grounded(r, a, s),
            r[:100],
        )

    if sid == "DENY_confirmation":
        s, _ = _fresh()
        s.turn("Запиши меня к неврологу на следующей неделе")
        r = s.turn("Нет, вторник вообще не могу")
        return (
            s.state.phase.value == "elicit"
            and not s.state.candidates
            and ("день" in r.lower() or "удобен" in r.lower()),
            r[:100],
        )

    if sid == "CORRECTION_wed_thu":
        s, a = _fresh()
        s.turn("Запиши меня к неврологу в среду")
        r = s.turn("Не среда, четверг")
        ok = (
            bool(s.state.candidates)
            and all(c.slot.start.weekday() == 3 for c in s.state.candidates)
            and _grounded(r, a, s)
        )
        return ok, r[:110]

    if sid == "AMBIGUOUS_date":
        s, _ = _fresh()
        r = s.turn("Запиши меня к врачу")
        return (
            s.state.phase.value == "elicit"
            and ("специалист" in r.lower() or "?" in r),
            r[:100],
        )

    if sid == "UNKNOWN_doctor":
        s, _ = _fresh()
        r = s.turn("Запиши меня к астрологу")
        return (
            not s.state.candidates
            and ("специалист" in r.lower() or "поняла" in r.lower() or "?" in r),
            r[:100],
        )

    if sid == "DUPLICATE_commit":
        s, a = _fresh()
        s.turn("Запиши меня к неврологу на следующей неделе вечером")
        assert s.state.candidates
        s.turn("Первый вариант")
        assert s.state.selected_slot_id
        selected_slot = s.state.selected_slot_id
        ik = "ik-dup-" + uuid.uuid4().hex[:8]
        s.turn("Да", idempotency_key=ik)

        first = a.get_appointments(s.patient)
        assert len(first) == 1

        replay = a.create_appointment(
            CreateAppointmentCommand(
                patient=s.patient,
                slot_id=selected_slot,
                idempotency_key=ik,
            )
        )
        after = a.get_appointments(s.patient)
        ok = (
            len(after) == 1
            and replay.booking_id == first[0].booking_id
        )
        return ok, f"bookings={len(after)} booking_id={replay.booking_id}"

    return False, "unknown scenario"


def _grounded(
    speech: str,
    adapter: MockSqliteClinic,
    sess: DialogueSession,
) -> bool:
    """Every spoken time must exist in current candidates or active bookings."""
    times = set(TIME_RE.findall(speech))
    if not times:
        return True

    real = {c.slot.start.strftime("%H:%M") for c in sess.state.candidates}
    for appt in adapter.get_appointments(sess.patient):
        real.add(appt.start.strftime("%H:%M"))
    return times <= real


if __name__ == "__main__":
    sys.exit(run())
