"""Phone-call mode: continuous PCM in, endpointed turns, audio back.

Route: `/voice/ws-call` (auth identical to /voice/ws: subprotocol
`dbi-voice, <token>`, no patient id in URL).

Concurrency model — three independent pieces, one shared state:
- receiver (async): reads WS frames, runs VAD inline (milliseconds), fires
  barge-in the moment user speech starts, dispatches utterances.
- turn pipeline (worker thread per utterance): STT -> dialogue ->
  chunk-streamed TTS. Never blocks the receiver.
- sender (async): the ONLY writer to the socket. Serializes turn/audio/
  stop/error messages; drops audio from superseded generations.

Barge-in path: receiver sees speech while `speaking` -> bump seq (dooms the
in-flight turn), close the TTS stream + POST /v1/cancel in the background,
queue ("stop") for instant client playback halt.

One `call_id` threads the whole call: WS -> STT -> dialogue trace -> TTS
requests -> cancel diagnostics.
"""
from __future__ import annotations

import logging
import queue
import threading
from dataclasses import dataclass, field

import anyio.to_thread
from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from api.voice_ws import CLOSE_UNAUTHORIZED, PROTOCOL, _bearer_from_scope
from domain.errors import UnknownIdentity
from identity.providers import IdentityContext
from observability.trace import Trace
from sessions.coordinator import run_turn
from voice.call import CallSession, new_call_id
from voice.voiseup import VoiseupSTT, VoiseupTTS

log = logging.getLogger("dbi.voice")

router = APIRouter()


@dataclass
class _Shared:
    seq: int = 0
    speaking: bool = False
    tts_gen: str | None = None
    lock: threading.Lock = field(default_factory=threading.Lock)


@router.websocket("/voice/ws-call")
async def voice_call(ws: WebSocket) -> None:
    token = _bearer_from_scope(ws)
    try:
        patient = ws.app.state.identity.identify(IdentityContext(token=token or ""))
    except UnknownIdentity:
        await ws.close(code=CLOSE_UNAUTHORIZED)
        return
    await ws.accept(subprotocol=PROTOCOL)
    call_id = new_call_id()
    trace = Trace(call_id=call_id)
    stt = VoiseupSTT(ws.app.state.voice_stt_url)
    tts = VoiseupTTS(ws.app.state.voice_tts_url)
    call = CallSession(stt, tts, turn_fn=None, vad=None)  # turn bound per turn below
    shared = _Shared()
    out: queue.Queue = queue.Queue()
    app_state = ws.app.state

    def make_turn(text: str):
        speech, sess = run_turn(
            patient=patient, text=text, idempotency_key=None,
            adapter=app_state.session_adapter(), snapshots=app_state.snapshots,
            redis_url=app_state.redis_url, now=app_state.clock.now(), trace=trace,
        )
        return speech, {
            "booking_id": sess.state.active_booking_id,
            "flow": sess.state.flow.value, "phase": sess.state.phase.value,
        }

    call.turn_fn = make_turn
    await ws.send_json({"type": "ready", "call_id": call_id})

    async with anyio.create_task_group() as tg:
        tg.start_soon(_sender, ws, out, shared)
        try:
            await _receiver(ws, call, shared, out, tts, tg, call_id)
        finally:
            # Invalidate any in-flight pipeline before tearing the socket down.
            # If TTS is active, close the stream and notify the worker so a
            # disconnected caller cannot leave expensive synthesis running.
            with shared.lock:
                shared.seq += 1
                gen = shared.tts_gen
                shared.speaking = False
                shared.tts_gen = None
            if gen is not None:
                await anyio.to_thread.run_sync(tts.cancel, gen)
            out.put(("closed",))
            tg.cancel_scope.cancel()


async def _sender(ws: WebSocket, out: queue.Queue, shared: _Shared) -> None:
    """Sole socket writer. Drops audio from non-current generations."""
    active_gen: str | None = None
    while True:
        item = await anyio.to_thread.run_sync(out.get)
        kind = item[0]
        if kind == "closed":
            return
        if kind == "turn":
            await ws.send_json(item[1])
        elif kind == "speak":
            active_gen = item[1]["gen"]
        elif kind == "audio":
            if item[1]["gen"] != active_gen:
                continue
            await ws.send_json({
                "type": "audio", "rate": item[1]["rate"],
                "bytes": item[1]["bytes"], "gen": item[1]["gen"],
            })
            await ws.send_bytes(item[1]["chunk"])
        elif kind in ("stop", "heard", "error", "pong"):
            if kind == "stop":
                active_gen = None
            await ws.send_json(item[1])


async def _receiver(ws, call, shared: _Shared, out: queue.Queue, tts, tg, call_id: str) -> None:
    try:
        while True:
            msg = await ws.receive()
            if msg["type"] == "websocket.disconnect":
                return
            if msg.get("bytes"):
                started, utt = call.feed_pcm(msg["bytes"])
                if started:
                    log.debug("call=%s vad speech started", call_id)
                if utt is not None:
                    log.debug("call=%s vad utterance bytes=%d", call_id, len(utt))
                if started:
                    with shared.lock:
                        speaking = shared.speaking
                    if speaking:
                        _barge(shared, out, tts, tg)
                if utt is not None:
                    with shared.lock:
                        shared.seq += 1
                        my = shared.seq
                    tg.start_soon(_pipeline, call, shared, out, tts, utt, my, call_id)
            elif msg.get("text"):
                import json as _json

                try:
                    ctl = _json.loads(msg["text"])
                except Exception as e:
                    log.debug("ignoring malformed ws text frame: %s", e)
                    continue
                if ctl.get("type") == "ping":
                    out.put(("pong", {"type": "pong"}))
    except WebSocketDisconnect:
        return


def _barge(shared: _Shared, out: queue.Queue, tts, tg) -> None:
    """Immediate barge-in: doom the turn, stop the client, cancel TTS."""
    with shared.lock:
        # Invalidate the current pipeline at speech START, not when the new
        # utterance eventually endpoints. Otherwise a cancelled old pipeline
        # can still consider itself live and emit stale audio/errors.
        shared.seq += 1
        gen = shared.tts_gen
        shared.speaking = False
        shared.tts_gen = None
    out.put(("stop", {"type": "stop", "gen": gen}))
    if gen is not None:
        tg.start_soon(anyio.to_thread.run_sync, tts.cancel, gen)


async def _pipeline(call, shared: _Shared, out: queue.Queue, tts, utt: bytes,
                    my: int, call_id: str) -> None:
    def is_live() -> bool:
        with shared.lock:
            return my == shared.seq

    def _on_speak(gen_id: str) -> None:
        # Publish generation activation while holding the same lock used by
        # _barge(). This guarantees queue order: "speak" is visible before a
        # competing "stop"; stale audio queued afterwards is then dropped.
        with shared.lock:
            if my == shared.seq:
                shared.speaking = True
                shared.tts_gen = gen_id
                out.put(("speak", {"gen": gen_id}))

    def produce() -> None:
        from voice.call import run_pipeline_streaming

        run_pipeline_streaming(
            call, utt, my, call_id, out, is_live=is_live, on_speak=_on_speak
        )

    await anyio.to_thread.run_sync(produce)
    with shared.lock:
        if my == shared.seq and shared.speaking:
            shared.speaking = False
