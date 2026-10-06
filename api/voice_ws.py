"""Voice WebSocket — live dialogue over a socket, same DialogueSession core.

Protocol (JSON, client <-> server):
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
"""
from __future__ import annotations

import time

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from api.booking_service import DialogueSession
from voice.runtime import VoiceRuntime

router = APIRouter()


@router.websocket("/voice/ws/{patient_id}")
async def voice_ws(ws: WebSocket, patient_id: str) -> None:
    await ws.accept()
    session: DialogueSession = ws.app.state.session_factory(patient_id)
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
                text = str(msg.get("text", ""))
                t0 = time.monotonic()
                speech, gen = runtime.on_user_text(text)
                t_ms = int((time.monotonic() - t0) * 1000)
                if gen.cancelled or not speech:
                    await ws.send_json({"type": "superseded", "gen_id": gen.gen_id})
                else:
                    await ws.send_json(
                        {
                            "type": "speech",
                            "gen_id": gen.gen_id,
                            "text": speech,
                            "phase": session.state.phase.value,
                            "flow": session.state.flow.value,
                            "booking_id": session.state.active_booking_id,
                            "t_ms": t_ms,
                        }
                    )
            else:
                await ws.send_json({"type": "error", "detail": f"unknown: {kind}"})
    except WebSocketDisconnect:
        return
