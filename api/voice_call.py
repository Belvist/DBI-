"""Phone-call mode: continuous PCM in, endpointed turns, audio back.

Route: `/voice/ws-call` (auth identical to /voice/ws: subprotocol
`dbi-voice, <token>`, no patient id in URL).

Binary frames from the client are int16 16kHz mono PCM chunks. The server
endpoint-detects utterances (VAD), transcribes via voiseup GigaAM, runs the
normal authorized DialogueSession turn, synthesizes via voiseup Qwen-TTS
and streams the audio back as: JSON header {"type":"audio",...} + one binary
PCM frame. Barge-in: user speech while assistant audio is playing cancels
the server TTS generation and tells the client to stop ("stop" event).
"""
from __future__ import annotations

import logging

import anyio.to_thread
from fastapi import APIRouter, WebSocket, WebSocketDisconnect

log = logging.getLogger("dbi.voice")

from api.voice_ws import CLOSE_UNAUTHORIZED, PROTOCOL, _bearer_from_scope
from domain.errors import UnknownIdentity
from domain.models import PatientRef
from identity.providers import IdentityContext
from sessions.coordinator import run_turn
from sessions.errors import OperationInProgress, StaleState
from voice.call import CallSession, new_call_id
from voice.voiseup import VoiceEngineError, VoiseupSTT, VoiseupTTS

router = APIRouter()


def _turn_fn(app_state, patient: PatientRef, now):
    def run(text: str):
        speech, sess = run_turn(
            patient=patient, text=text, idempotency_key=None,
            adapter=app_state.session_adapter(), snapshots=app_state.snapshots,
            redis_url=app_state.redis_url, now=now(),
        )
        return speech, {
            "booking_id": sess.state.active_booking_id,
            "flow": sess.state.flow.value, "phase": sess.state.phase.value,
        }

    return run


@router.websocket("/voice/ws-call")
async def voice_call(ws: WebSocket) -> None:
    from domain.adapter_errors import DependencyUnavailable
    from domain.errors import IdempotencyConflict

    token = _bearer_from_scope(ws)
    try:
        patient = ws.app.state.identity.identify(IdentityContext(token=token or ""))
    except UnknownIdentity:
        await ws.close(code=CLOSE_UNAUTHORIZED)
        return
    await ws.accept(subprotocol=PROTOCOL)
    call_id = new_call_id()
    stt = VoiseupSTT(ws.app.state.voice_stt_url)
    tts = VoiseupTTS(ws.app.state.voice_tts_url)
    call = CallSession(
        stt, tts,
        _turn_fn(ws.app.state, patient, lambda: ws.app.state.clock.now()),
    )
    await ws.send_json({"type": "ready", "call_id": call_id})
    try:
        while True:
            msg = await ws.receive()
            # NOTE: low-level receive() returns the disconnect message as a
            # dict instead of raising (unlike receive_json) — check explicitly.
            if msg["type"] == "websocket.disconnect":
                return
            if msg.get("bytes"):
                started, utt = call.feed_pcm(msg["bytes"])
                if started and call.barged_in():
                    gen = call.stop_playback()
                    call.seq += 1
                    await ws.send_json({"type": "stop", "gen": gen})
                if utt is not None:
                    call.seq += 1
                    my = call.seq
                    try:
                        reply = await anyio.to_thread.run_sync(
                            call.process_utterance, utt, my, call_id
                        )
                    except (VoiceEngineError, DependencyUnavailable):
                        await ws.send_json({
                            "type": "error",
                            "detail": "service temporarily unavailable, please retry",
                        })
                        continue
                    except (StaleState, OperationInProgress, IdempotencyConflict):
                        await ws.send_json({
                            "type": "error",
                            "detail": "concurrent turn, please repeat",
                        })
                        continue
                    if reply is None:
                        continue  # superseded by barge-in
                    if reply["type"] == "heard":
                        await ws.send_json({"type": "heard", "text": ""})
                        continue
                    await ws.send_json({
                        "type": "turn", "heard": reply["heard"],
                        "text": reply["text"], "flow": reply["flow"],
                        "phase": reply["phase"], "booking_id": reply["booking_id"],
                    })
                    await ws.send_json({
                        "type": "audio", "rate": reply["rate"],
                        "bytes": reply["bytes"], "gen": reply["gen"],
                    })
                    await ws.send_bytes(reply["_pcm"])
            elif msg.get("text"):
                import json as _json

                try:
                    ctl = _json.loads(msg["text"])
                except Exception as e:
                    log.debug("ignoring malformed ws text frame: %s", e)
                    continue
                if ctl.get("type") == "ping":
                    await ws.send_json({"type": "pong"})
    except WebSocketDisconnect:
        return
