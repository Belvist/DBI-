"""Identity enforcement: no credential -> no data, never cross-patient."""
from __future__ import annotations

import json

from fastapi.testclient import TestClient

from api.main import app
from domain.models import PatientRef
from identity.providers import (
    DemoIdentityProvider,
    IdentityContext,
    TokenFileIdentityProvider,
)

client = TestClient(app)
DEMO = {"X-Patient-Token": "demo"}


def _book(token: str) -> str:
    r = client.post(
        "/dialogue/turn",
        json={"text": "Запиши меня к неврологу на следующей неделе вечером"},
        headers={"X-Patient-Token": token},
    )
    assert r.status_code == 200, r.text
    r = client.post(
        "/dialogue/turn",
        json={"text": "Первый вариант"},
        headers={"X-Patient-Token": token},
    )
    assert r.status_code == 200
    r = client.post(
        "/dialogue/turn", json={"text": "Да"}, headers={"X-Patient-Token": token}
    )
    assert r.status_code == 200
    assert "записаны" in r.json()["speech"].lower()
    return r.json()["speech"]


def test_no_token_is_401():
    r = client.post("/dialogue/turn", json={"text": "привет"})
    assert r.status_code == 401
    r = client.get("/appointments/demo-patient")
    assert r.status_code == 401


def test_wrong_token_is_401():
    r = client.post(
        "/dialogue/turn",
        json={"text": "привет"},
        headers={"X-Patient-Token": "wrong-token"},
    )
    assert r.status_code == 401


def test_scope_mismatch_is_403():
    r = client.post(
        "/dialogue/turn",
        json={"text": "привет", "patient_id": "someone-else"},
        headers=DEMO,
    )
    assert r.status_code == 403
    r = client.get("/appointments/someone-else", headers=DEMO)
    assert r.status_code == 403


def test_demo_patient_sees_own_booking():
    _book("demo")
    r = client.get("/appointments/demo-patient", headers=DEMO)
    assert r.status_code == 200 and len(r.json()) >= 1


def test_tokenfile_provider(tmp_path):
    f = tmp_path / "tokens.json"
    f.write_text(
        json.dumps(
            {
                "tok-A": {"patient_id": "patient-A"},
                "tok-B": {"patient_id": "patient-B", "phone": "+7000"},
            }
        ),
        encoding="utf-8",
    )
    p = TokenFileIdentityProvider(f)
    assert p.identify(IdentityContext(token="tok-A")) == PatientRef(patient_id="patient-A")
    assert p.identify(IdentityContext(token="tok-B")).phone == "+7000"
    try:
        p.identify(IdentityContext(token="nope"))
        raise AssertionError("must raise")
    except Exception as e:
        assert type(e).__name__ == "UnknownIdentity"


def test_demo_provider_rejects_unknown():
    try:
        DemoIdentityProvider().identify(IdentityContext(token="nope"))
        raise AssertionError("must raise")
    except Exception as e:
        assert type(e).__name__ == "UnknownIdentity"


def test_patients_never_see_each_others_bookings():
    from clinic_adapter.mock_sqlite import MockSqliteClinic
    from domain.commands import CreateAppointmentCommand, RescheduleCommand
    from domain.errors import AppointmentNotFound

    a = MockSqliteClinic()
    alice = PatientRef(patient_id="alice")
    bob = PatientRef(patient_id="bob")
    slot = a.find_slots(limit=5)[0].slot.slot_id
    created = a.create_appointment(
        CreateAppointmentCommand(patient=alice, slot_id=slot, idempotency_key="ik-alice-1")
    )
    assert len(a.get_appointments(bob)) == 0
    assert a.get_appointments(alice)[0].booking_id == created.booking_id
    other = a.find_slots(limit=5)[0].slot.slot_id
    try:
        a.reschedule_appointment(
            RescheduleCommand(
                patient=bob,
                booking_id=created.booking_id,
                new_slot_id=other,
                idempotency_key="ik-bob-evil-1",
            )
        )
        raise AssertionError("bob must not move alice's booking")
    except AppointmentNotFound:
        pass
