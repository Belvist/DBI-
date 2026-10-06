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
        """Blocking pipeline: STT -> dialogue -> TTS. Returns a reply dict,
        or None when this turn was superseded by a newer utterance."""
        text = self.stt.transcribe_pcm16(pcm)
        if seq != self.seq:
            return None
        if not text.strip():
            return {"type": "heard", "text": ""}
        speech, info = self.turn_fn(text)
        if seq != self.seq:
            return None
        rate, audio, gen_id = self.tts.synthesize(speech)
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


def new_call_id() -> str:
    return f"call-{uuid.uuid4().hex[:8]}"
