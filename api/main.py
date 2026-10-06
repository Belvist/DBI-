"""FastAPI entry — reproducible run for the jury: uvicorn api.main:app."""
from __future__ import annotations

import logging
from datetime import datetime

from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import HTMLResponse, PlainTextResponse
from pydantic import BaseModel

from api.booking_service import DialogueSession
from api.voice_ws import router as voice_router
from clinic_adapter.mock_sqlite import MockSqliteClinic
from clinic_adapter.resilient import ResilientAdapter
from config.settings import load as load_settings
from domain.models import PatientRef
from identity.deps import identify_patient, require_scope
from identity.providers import resolve_provider
from observability import metrics
from sessions.cache import SessionCache
from sessions.locks import patient_turn_lock
from sessions.store import SessionStore
from voice.stt import BrowserSTT
from voice.tts import BrowserTTS

log = logging.getLogger("dbi.api")

app = FastAPI(title="DBI Clinic Assistant", version="0.2.0")
app.include_router(voice_router)

_SETTINGS = load_settings()
if _SETTINGS.pg_url:
    from clinic_adapter.postgres import PostgresClinic

    _BASE = PostgresClinic(
        url=_SETTINGS.pg_url, seed=_SETTINGS.allow_seed, pool_max=_SETTINGS.pg_pool_max
    )
    log.info("clinic adapter=postgres seed=%s", _SETTINGS.allow_seed)
else:
    _BASE = MockSqliteClinic(path=_SETTINGS.db_path, seed=_SETTINGS.allow_seed)
    log.info("clinic adapter=sqlite path=%s seed=%s", _SETTINGS.db_path, _SETTINGS.allow_seed)
_ADAPTER = ResilientAdapter(_BASE)
_IDENTITY = resolve_provider(_SETTINGS.identity_mode, _SETTINGS.tokens_file)
if _SETTINGS.redis_url:
    from sessions.redis_store import RedisSessionStore

    _SNAPSHOTS = RedisSessionStore(_SETTINGS.redis_url)
    log.info("snapshots=redis")
else:
    _SNAPSHOTS = SessionStore(path=_SETTINGS.sessions_path)
    log.info("snapshots=sqlite path=%s", _SETTINGS.sessions_path)
_SESSIONS: SessionCache[DialogueSession] = SessionCache(maxsize=1000, idle_ttl_s=3600)
_DEMO_NOW = datetime(2026, 10, 13, 12, 0)
_STT = BrowserSTT()
_TTS = BrowserTTS()


def _session(patient: PatientRef) -> DialogueSession:
    def _build() -> DialogueSession:
        sess = DialogueSession(patient, _ADAPTER, now=_DEMO_NOW)
        restored = _SNAPSHOTS.load(patient.patient_id)
        if restored is not None and restored.patient.patient_id == patient.patient_id:
            sess.state = restored
        return sess

    return _SESSIONS.get_or_create(patient.patient_id, _build)


def _turn_locked(patient: PatientRef, text: str, idempotency_key: str | None) -> str:
    with patient_turn_lock(_SETTINGS.redis_url, patient.patient_id):
        sess = _session(patient)
        speech = sess.turn(text, idempotency_key=idempotency_key)
        _SNAPSHOTS.save(sess.state)
        return speech


app.state.session_factory = _session
app.state.identity = _IDENTITY
app.state.snapshots = _SNAPSHOTS
app.state.redis_url = _SETTINGS.redis_url


class TurnIn(BaseModel):
    patient_id: str | None = None
    text: str
    idempotency_key: str | None = None


@app.get("/health")
def health() -> dict:
    return {
        "status": "ok",
        "service": "dbi-assistant",
        "env": _SETTINGS.env,
        "identity": type(_IDENTITY).__name__,
        "stt": _STT.describe(),
        "tts": _TTS.describe(),
    }


@app.get("/ready")
def ready() -> dict:
    """Readiness: adapter reachable + pool stats. 503 when the backend is down."""
    from fastapi import HTTPException

    try:
        _ADAPTER.find_doctors()
        detail: dict = {"adapter": "ok"}
        pool = getattr(getattr(_ADAPTER, "_base", None), "_pool", None)
        if pool is not None:
            stats = pool.get_stats()
            detail["pool"] = {
                "available": stats.get("pool_available"),
                "used": stats.get("pool_used"),
            }
        return {"status": "ready", **detail}
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"backend down: {type(e).__name__}") from e


@app.get("/metrics", response_class=PlainTextResponse)
def prometheus_metrics() -> str:
    return metrics.render_prometheus()


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
    try:
        patient = require_scope(identify_patient(_IDENTITY, x_patient_token), patient_id)
    except HTTPException as e:
        if e.status_code in (401, 403):
            metrics.inc("auth_failures_total")
        raise
    return [a.model_dump(mode="json") for a in _ADAPTER.get_appointments(patient)]


@app.post("/dialogue/turn")
def dialogue_turn(
    inp: TurnIn, x_patient_token: str | None = Header(default=None, alias="X-Patient-Token")
):
    try:
        patient = require_scope(identify_patient(_IDENTITY, x_patient_token), inp.patient_id)
    except HTTPException as e:
        if e.status_code in (401, 403):
            metrics.inc("auth_failures_total")
        raise
    try:
        speech = _turn_locked(patient, inp.text, inp.idempotency_key)
    except TimeoutError as e:
        raise HTTPException(status_code=409, detail=str(e)) from None
    s = _session(patient)
    return {
        "speech": speech,
        "phase": s.state.phase.value,
        "flow": s.state.flow.value,
        "trace": [{"seq": e.seq, "kind": e.kind, "data": e.data} for e in s.trace.events[-8:]],
    }


@app.get("/voice", response_class=HTMLResponse)
def voice_client():
    from pathlib import Path

    page = Path(__file__).resolve().parent.parent / "voice" / "web" / "index.html"
    return HTMLResponse(page.read_text(encoding="utf-8"))


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
