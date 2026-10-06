"""Voice WebSocket — live dialogue over an AUTHORIZED socket.

Route has NO patient id: `/voice/ws`. The patient comes ONLY from the
identity provider, never from client-controlled URL parts.

Auth transport: `Sec-WebSocket-Protocol: dbi-voice, <bearer-token>`.
Rationale: a query-string `?token=` leaks into access logs, proxy logs and
browser history; a subprotocol element does not. Bearer tokens must be
subprotocol-safe (operator rule: hex, see docs/PERSISTENCE.md).

Handshake:
  valid credential   -> accept(subprotocol="dbi-voice"), dialogue starts
  missing/invalid    -> close(4401), no session, no data

Message protocol (JSON, client <-> server):
  C->S {"type": "user_text", "text": "..."}   final STT hypothesis
  C->S {"type": "barge_in"}                   user started speaking over TTS
  C->S {"type": "ping"}
  S->C {"type": "speech", "gen_id", "text", "phase", "flow", "t_ms"}
  S->C {"type": "superseded", "gen_id"}       turn result dropped (interrupted)
  S->C {"type": "stopped", "gen_id"}          barge-in acknowledged
  S->C {"type": "pong"}

Barge-in semantics mirror `voice/runtime.py`: the in-flight generation is
cancelled server-side; the browser ALSO cancels speechSynthesis locally so
the stop is instant even before the round-trip completes.

Concurrency: the dialogue turn (lock + session.turn + adapter I/O + save) is
blocking sync code and runs in a worker thread via anyio.to_thread, so a
slow DB never stalls the event loop. `Generation.cancel` is a single
GIL-atomic flag store, safe to flip from the loop thread mid-turn.
"""
from __future__ import annotations

import re
import time

import anyio.to_thread
from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from api.booking_service import DialogueSession
from domain.adapter_errors import DependencyUnavailable
from domain.errors import IdempotencyConflict, UnknownIdentity
from domain.models import PatientRef
from identity.providers import IdentityContext
from sessions.coordinator import run_turn
from sessions.errors import OperationInProgress, StaleState
from voice.runtime import VoiceRuntime

router = APIRouter()

PROTOCOL = "dbi-voice"
_TOKEN_RE = re.compile(r"^[!#$%&'*+\-.^_`|~0-9A-Za-z]+$")
CLOSE_UNAUTHORIZED = 4401


def _bearer_from_scope(ws: WebSocket) -> str | None:
    offered: list[str] = ws.scope.get("subprotocols", [])
    if len(offered) >= 2 and offered[0] == PROTOCOL and _TOKEN_RE.fullmatch(offered[1] or ""):
        return offered[1]
    return None


@router.websocket("/voice/ws")
async def voice_ws(ws: WebSocket) -> None:
    token = _bearer_from_scope(ws)
    try:
        patient = ws.app.state.identity.identify(IdentityContext(token=token or ""))
    except UnknownIdentity:
        await ws.close(code=CLOSE_UNAUTHORIZED)
        return
    await ws.accept(subprotocol=PROTOCOL)
    session: DialogueSession = ws.app.state.session_factory(patient)
    runtime = VoiceRuntime(session)
    try:
        while True:
            msg = await ws.receive_json()
            kind = msg.get("type")
            if kind == "ping":
                await ws.send_json({"type": "pong"})
            elif kind == "barge_in":
                runtime.on_user_start()
                gen = runtime.current.gen_id if runtime.current else None
                await ws.send_json({"type": "stopped", "gen_id": gen})
            elif kind == "user_text":
                reply = await anyio.to_thread.run_sync(
                    _do_turn, ws.app.state, patient, runtime, str(msg.get("text", ""))
                )
                await ws.send_json(reply)
            else:
                await ws.send_json({"type": "error", "detail": f"unknown: {kind}"})
    except WebSocketDisconnect:
        return


def _do_turn(app_state, patient: PatientRef, runtime: VoiceRuntime, text: str) -> dict:
    """One blocking dialogue turn on authoritative state. Runs in a worker thread.

    The session is rebuilt from the snapshot store on EVERY turn: a
    long-lived WS connection never trusts a stale local DialogueSession.
    """
    from voice.runtime import Generation

    t0 = time.monotonic()
    gen = Generation()
    runtime.current = gen
    runtime.user_is_speaking = False
    try:
        speech, sess = run_turn(
            patient=patient,
            text=text,
            idempotency_key=None,
            adapter=runtime.session.adapter,
            snapshots=app_state.snapshots,
            redis_url=app_state.redis_url,
            now=runtime.session.now,
        )
    except StaleState:
        return {"type": "error", "detail": "concurrent turn, please repeat"}
    except DependencyUnavailable:
        return {"type": "error", "detail": "service temporarily unavailable, please retry"}
    except IdempotencyConflict:
        return {"type": "error", "detail": "operation key conflict, use a fresh key"}
    except OperationInProgress:
        return {"type": "error", "detail": "operation in progress, retry with the same key"}
    runtime.session = sess  # rebind: barge-in trace follows the latest session
    runtime.trace = sess.trace
    t_ms = int((time.monotonic() - t0) * 1000)
    if gen.cancelled or not speech:
        runtime.trace.log("gen_superseded", gen_id=gen.gen_id)
        return {"type": "superseded", "gen_id": gen.gen_id}
    runtime.trace.log("assistant_speech", gen_id=gen.gen_id, text=speech[:160])
    return {
        "type": "speech",
        "gen_id": gen.gen_id,
        "text": speech,
        "phase": sess.state.phase.value,
        "flow": sess.state.flow.value,
        "booking_id": sess.state.active_booking_id,
        "t_ms": t_ms,
    }
