"""Phone-call session: continuous PCM in, endpointed utterances, barge-in.

One CallSession per WebSocket connection. Audio transport (WS frames) lives
in api/voice_call.py; everything decision-shaped lives here and is unit
testable with fake STT/TTS/turn functions.

Barge-in: when user speech starts while assistant audio is still playing,
the in-flight TTS generation is cancelled server-side (voiseup /v1/cancel)
and the client is told to stop playback immediately.
"""
from __future__ import annotations

import logging
import time
import uuid

log = logging.getLogger("dbi.voice")

MAX_UTTERANCE_S = 20


class CallSession:
    def __init__(self, stt, tts, turn_fn, vad=None) -> None:
        from voice.vad import make_vad

        self.stt = stt
        self.tts = tts
        self.turn_fn = turn_fn  # (text) -> (speech, state-ish dict)
        self.vad = vad or make_vad()
        self._pcm = bytearray()
        self.seq = 0
        self.playing_gen: str | None = None
        self.playing_until = 0.0

    def feed_pcm(self, chunk: bytes) -> tuple[bool, bytes | None]:
        """Feed 16kHz int16 mono PCM. Returns (speech_started, utterance)."""
        started, ended = self.vad.feed(chunk)
        if started:
            self._pcm = bytearray()
        if self.vad.in_speech:
            self._pcm += chunk
        utterance = None
        if ended or len(self._pcm) > MAX_UTTERANCE_S * 16000 * 2:
            utterance = bytes(self._pcm)
            self._pcm = bytearray()
            self.vad.reset()
        return started, utterance

    def barged_in(self) -> bool:
        """True when the user started speaking over assistant playback."""
        return self.playing_gen is not None and time.monotonic() < self.playing_until

    def mark_playing(self, gen_id: str, duration_s: float) -> None:
        self.playing_gen = gen_id
        self.playing_until = time.monotonic() + duration_s

    def stop_playback(self) -> str | None:
        """Cancel server-side TTS. Returns the cancelled generation id."""
        gen, self.playing_gen = self.playing_gen, None
        self.playing_until = 0.0
        if gen is not None:
            try:
                self.tts.cancel(gen)
            except Exception as e:
                log.debug("tts cancel failed: %s", e)
        return gen

    def process_utterance(self, pcm: bytes, seq: int, call_id: str) -> dict | None:
        """Blocking pipeline: STT -> dialogue -> buffered TTS. Legacy path
        used by unit tests; the live WS streams via run_pipeline_streaming."""
        text = self.stt.transcribe_pcm16(pcm, call_id=call_id)
        if seq != self.seq:
            return None
        if not text.strip():
            return {"type": "heard", "text": ""}
        speech, info = self.turn_fn(text)
        if seq != self.seq:
            return None
        rate, audio, gen_id = self.tts.synthesize(speech, call_id=call_id)
        if seq != self.seq:
            try:
                self.tts.cancel(gen_id)
            except Exception:
                pass
            return None
        duration = len(audio) / 2 / max(rate, 1)
        self.mark_playing(gen_id, duration)
        return {
            "type": "audio", "rate": rate, "bytes": len(audio),
            "gen": gen_id, "text": speech,
            "booking_id": info.get("booking_id"),
            "flow": info.get("flow"), "phase": info.get("phase"),
            "heard": text, "_pcm": audio,
        }


def run_pipeline_streaming(call, utt: bytes, seq: int, call_id: str, out,
                           is_live=None, on_speak=None) -> None:
    """One utterance through STT -> dialogue -> chunk-streamed TTS.

    Runs in a worker thread. Posts ordered messages to the thread-safe
    `out` queue: ("turn", dict), ("speak", {...}), ("audio", {...})*,
    ("heard",) | ("error", dict). Every step re-checks liveness: a barge-in
    abandons the turn mid-flight. `on_speak(gen)` fires on the first chunk
    so the connection can track (and cancel) the live TTS generation.
    """

    def live() -> bool:
        if is_live is not None:
            return is_live()
        return seq == call.seq

    try:
        text = call.stt.transcribe_pcm16(utt, call_id=call_id)
    except Exception:
        log.exception("voice STT failed call_id=%s", call_id)
        out.put(("error", {"type": "error", "detail": "speech recognition unavailable"}))
        return
    if not live():
        return
    if not text.strip():
        out.put(("heard", {"type": "heard", "text": ""}))
        return
    try:
        speech, info = call.turn_fn(text)
    except Exception:
        log.exception("voice dialogue turn failed call_id=%s", call_id)
        out.put(("error", {"type": "error", "detail": "service temporarily unavailable"}))
        return
    if not live():
        return
    out.put(("turn", {
        "type": "turn", "heard": text, "text": speech,
        "flow": info.get("flow"), "phase": info.get("phase"),
        "booking_id": info.get("booking_id"),
    }))
    try:
        stream = call.tts.synthesize_stream(speech, call_id=call_id)
        first = True
        for rate, chunk, gen_id, _done in stream:
            if not live():
                try:
                    call.tts.cancel(gen_id)
                except Exception:
                    pass
                return
            if first:
                if on_speak is not None:
                    on_speak(gen_id)
                out.put(("speak", {"gen": gen_id, "rate": rate}))
                first = False
            out.put(("audio", {
                "gen": gen_id, "rate": rate, "bytes": len(chunk),
                "chunk": chunk,
            }))
    except Exception:
        if live():
            log.exception("voice TTS failed call_id=%s", call_id)
            out.put(("error", {"type": "error", "detail": "speech synthesis unavailable"}))


def new_call_id() -> str:
    return f"call-{uuid.uuid4().hex[:8]}"
