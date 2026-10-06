"""FastAPI entry — reproducible run for the jury: uvicorn api.main:app."""
from __future__ import annotations

import logging
import os
from datetime import datetime

from fastapi import FastAPI, Header
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from api.booking_service import DialogueSession
from clinic_adapter.mock_sqlite import MockSqliteClinic
from domain.models import PatientRef
from identity.deps import identify_patient, require_scope
from identity.providers import resolve_provider
from sessions.store import SessionStore

log = logging.getLogger("dbi.api")

app = FastAPI(title="DBI Clinic Assistant", version="0.1.0")

_DB_PATH = os.getenv("DBI_DB_PATH", ":memory:")
_PG_URL = os.getenv("DBI_PG_URL", "")
if _PG_URL:
    from clinic_adapter.postgres import PostgresClinic

    _ADAPTER = PostgresClinic(url=_PG_URL)
    log.info("clinic adapter=postgres")
else:
    _ADAPTER = MockSqliteClinic(path=_DB_PATH)
    log.info("clinic adapter=sqlite path=%s", _DB_PATH)
_IDENTITY = resolve_provider()
_SNAPSHOTS = SessionStore(path=os.getenv("DBI_SESSIONS_PATH", ":memory:"))
_SESSIONS: dict[str, DialogueSession] = {}
_DEMO_NOW = datetime(2026, 10, 13, 12, 0)


def _session(patient: PatientRef) -> DialogueSession:
    if patient.patient_id not in _SESSIONS:
        sess = DialogueSession(patient, _ADAPTER, now=_DEMO_NOW)
        restored = _SNAPSHOTS.load(patient.patient_id)
        if restored is not None and restored.patient.patient_id == patient.patient_id:
            sess.state = restored
        _SESSIONS[patient.patient_id] = sess
    return _SESSIONS[patient.patient_id]


class TurnIn(BaseModel):
    patient_id: str | None = None
    text: str
    idempotency_key: str | None = None


@app.get("/health")
def health() -> dict:
    return {
        "status": "ok",
        "service": "dbi-assistant",
        "identity": type(_IDENTITY).__name__,
    }


@app.get("/doctors")
def doctors(specialty: str | None = None):
    from domain.models import Specialty as _S
    spec = _S(specialty) if specialty else None
    return [d.model_dump() for d in _ADAPTER.find_doctors(specialty=spec)]


@app.get("/slots")
def slots(specialty: str | None = None, time_pref: str | None = None):
    from domain.models import Specialty as _S
    spec = _S(specialty) if specialty else None
    out = _ADAPTER.find_slots(specialty=spec, time_pref=time_pref, limit=10)
    return [{"slot": s.slot.model_dump(mode="json"), "doctor": s.doctor.model_dump()} for s in out]


@app.get("/appointments/{patient_id}")
def appointments(
    patient_id: str, x_patient_token: str | None = Header(default=None, alias="X-Patient-Token")
):
    patient = require_scope(identify_patient(_IDENTITY, x_patient_token), patient_id)
    return [a.model_dump(mode="json") for a in _ADAPTER.get_appointments(patient)]


@app.post("/dialogue/turn")
def dialogue_turn(
    inp: TurnIn, x_patient_token: str | None = Header(default=None, alias="X-Patient-Token")
):
    patient = require_scope(identify_patient(_IDENTITY, x_patient_token), inp.patient_id)
    s = _session(patient)
    speech = s.turn(inp.text, idempotency_key=inp.idempotency_key)
    _SNAPSHOTS.save(s.state)
    return {
        "speech": speech,
        "phase": s.state.phase.value,
        "flow": s.state.flow.value,
        "trace": [{"seq": e.seq, "kind": e.kind, "data": e.data} for e in s.trace.events[-8:]],
    }


@app.get("/", response_class=HTMLResponse)
def index():
    return """<!doctype html><meta charset=utf-8><title>DBI Assistant demo</title>
<h2>DBI Assistant — text demo (voice вторым слоем)</h2>
<input id=t size=80 placeholder="Хочу записаться к неврологу на следующей неделе вечером">
<button onclick="go()">➤</button><pre id=o></pre>
<script>async function go(){const text=document.getElementById('t').value;
const r=await fetch('/dialogue/turn',{method:'POST',headers:{'Content-Type':'application/json','X-Patient-Token':'demo'},
body:JSON.stringify({text})});const j=await r.json();
document.getElementById('o').textContent=j.speech+'\\n\\n['+j.flow+'/'+j.phase+']';}</script>"""
