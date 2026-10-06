"""Live jury dashboard — shows the call + DB truth side by side.

Run: python -m demo.dashboard  (prints one full BOOK->STATUS->RESCHEDULE run)
Serve: uvicorn api.main:app serves / + /dialogue/turn for interactive demo.
"""
from __future__ import annotations

from datetime import datetime

from api.booking_service import DialogueSession
from clinic_adapter.mock_sqlite import MockSqliteClinic
from domain.models import PatientRef


def main() -> None:
    adapter = MockSqliteClinic()
    now = datetime(2026, 10, 13, 12, 0)
    sess = DialogueSession(PatientRef(patient_id="+7***42", phone="+7***42"), adapter, now=now)
    script = [
        "Здравствуйте, хочу записаться к неврологу.",
        "На следующей неделе, желательно вечером.",
        "Первый вариант",
        "Да",
        "У меня вроде была запись к неврологу. Когда?",
        "Я не смогу. Давай в четверг утром.",
    ]
    print("ACTIVE CALL  patient=+7***42")
    print("=" * 60)
    for u in script:
        print(f"\nПациент: {u}")
        speech = sess.turn(u)
        print(f"Ассистент: {speech}")
        print(f"--- [{sess.state.flow.value}/{sess.state.phase.value}] "
              f"booking={sess.state.active_booking_id}")
    appts = adapter.get_appointments(sess.patient)
    free = adapter.find_slots(limit=3)
    print("\n" + "=" * 60)
    print(f"BOOKINGS IN DB: {len(appts)}")
    for a in appts:
        print(f"  {a.booking_id} {a.start} doctor={a.doctor_id} status={a.status.value}")
    print(f"FREE SLOTS SAMPLE: {len(free)} shown (chosen slot no longer free = OK)")


if __name__ == "__main__":
    main()
