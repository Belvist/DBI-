"""FastAPI entry — reproducible run for the jury: uvicorn api.main:app."""
from __future__ import annotations

import logging

from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import HTMLResponse, PlainTextResponse
from pydantic import BaseModel

from api.booking_service import DialogueSession
from api.voice_ws import router as voice_router
from clinic_adapter.mock_sqlite import MockSqliteClinic
from clinic_adapter.postgres import PostgresClinic
from clinic_adapter.resilient import ResilientAdapter
from config.settings import load as load_settings
from domain.models import PatientRef
from identity.deps import identify_patient, require_scope
from identity.providers import resolve_provider
from observability import metrics
from observability.clock import resolve_clock
from sessions.coordinator import run_turn
from sessions.store import SessionStore
from voice.stt import BrowserSTT
from voice.tts import BrowserTTS

log = logging.getLogger("dbi.api")

app = FastAPI(title="DBI Clinic Assistant", version="0.2.0")
app.include_router(voice_router)

_SETTINGS = load_settings()
if _SETTINGS.pg_url:
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
_CLOCK = resolve_clock(_SETTINGS.env, _SETTINGS.clock_override)
log.info("clock=%s", type(_CLOCK).__name__)
_STT = BrowserSTT()
_TTS = BrowserTTS()


def _session(patient: PatientRef) -> DialogueSession:
    # Fresh build from the authoritative snapshot on every call: no process
    # ever trusts a cached DialogueSession across turns (see coordinator).
    sess = DialogueSession(patient, _ADAPTER, now=_CLOCK.now())
    restored = _SNAPSHOTS.load(patient.patient_id)
    if restored is not None and restored.patient.patient_id == patient.patient_id:
        sess.state = restored
    return sess


def _turn_locked(
    patient: PatientRef, text: str, idempotency_key: str | None
) -> tuple[str, DialogueSession]:
    from domain.adapter_errors import DependencyUnavailable
    from sessions.errors import OperationInProgress, StaleState

    try:
        return run_turn(
            patient=patient,
            text=text,
            idempotency_key=idempotency_key,
            adapter=_ADAPTER,
            snapshots=_SNAPSHOTS,
            redis_url=_SETTINGS.redis_url,
            now=_CLOCK.now(),
        )
    except StaleState as e:
        raise HTTPException(status_code=409, detail=f"concurrent turn: {e}") from None
    except TimeoutError as e:
        raise HTTPException(status_code=409, detail=str(e)) from None
    except DependencyUnavailable:
        # Generic client message; full cause stays in server logs/traces.
        raise HTTPException(status_code=503, detail="service temporarily unavailable") from None
    except OperationInProgress as e:
        raise HTTPException(status_code=409, detail=f"operation_in_progress: {e}") from None


app.state.session_factory = _session
app.state.identity = _IDENTITY
app.state.snapshots = _SNAPSHOTS
app.state.redis_url = _SETTINGS.redis_url
app.state.clock = _CLOCK
app.state.base_adapter = _BASE


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
    """Readiness probe. Minimal output by design (no pool internals).

    Probes bypass ResilientAdapter so health checks never mutate breaker
    state or business metrics.
    """
    from fastapi import HTTPException

    try:
        _BASE.ping()
        _SNAPSHOTS.ping()
        return {"status": "ready"}
    except Exception as e:
        raise HTTPException(status_code=503, detail="backend down") from e


@app.get("/metrics", response_class=PlainTextResponse)
def prometheus_metrics(
    x_metrics_token: str | None = Header(default=None, alias="X-Metrics-Token"),
):
    """Internal counters. Gated by DBI_METRICS_TOKEN when configured
    (else open, demo-grade). Unknown token -> 404 to hide existence."""
    from fastapi import HTTPException

    if _SETTINGS.metrics_token and x_metrics_token != _SETTINGS.metrics_token:
        raise HTTPException(status_code=404, detail="not found")
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
    from domain.errors import IdempotencyConflict

    try:
        speech, sess = _turn_locked(patient, inp.text, inp.idempotency_key)
    except IdempotencyConflict as e:
        raise HTTPException(status_code=409, detail=str(e)) from None
    return {
        "speech": speech,
        "phase": sess.state.phase.value,
        "flow": sess.state.flow.value,
        "trace": [{"seq": e.seq, "kind": e.kind, "data": e.data} for e in sess.trace.events[-8:]],
    }


@app.get("/voice", response_class=HTMLResponse)
def voice_client():
    # importlib.resources: works from a wheel install where no source
    # checkout exists next to the package.
    from importlib.resources import files

    page = files("voice").joinpath("web/index.html").read_text(encoding="utf-8")
    return HTMLResponse(page)


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
