"""Phone-call mode: VAD, endpointing, barge-in, WS auth (no mic needed)."""
from __future__ import annotations

import math
import struct

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from api.main import app
from voice.call import CallSession
from voice.vad import EnergyVad, make_vad

client = TestClient(app)
AUTH = {"subprotocols": ["dbi-voice", "demo"]}


def _tone(seconds: float = 1.0, freq: float = 440.0, amp: int = 8000) -> bytes:
    n = int(seconds * 16000)
    return struct.pack(
        f"<{n}h",
        *[
            int(amp * math.sin(2 * math.pi * freq * i / 16000))
            for i in range(n)
        ],
    )


def _silence(seconds: float = 1.0) -> bytes:
    n = int(seconds * 16000)
    return struct.pack(f"<{n}h", *([0] * n))


def test_energy_vad_speech_cycle():
    vad = EnergyVad()
    started = ended = False
    for i in range(0, len(_tone()), 3200):
        s, e = vad.feed(_tone()[i : i + 3200])
        started, ended = started or s, ended or e
    assert started and not ended
    for i in range(0, len(_silence()), 3200):
        s, e = vad.feed(_silence()[i : i + 3200])
        ended = ended or e
    assert ended


def test_make_vad_never_raises():
    vad = make_vad()
    s, e = vad.feed(_silence(0.2))
    assert (s, e) == (False, False)


class FakeSTT:
    def transcribe_pcm16(self, pcm: bytes, sample_rate: int = 16000) -> str:
        assert len(pcm) > 3200
        return "да"


class FakeTTS:
    def __init__(self) -> None:
        self.cancelled: list[str] = []

    def synthesize(self, text: str):
        return 16000, b"\x00\x01" * 8000, "g-test"

    def cancel(self, generation_id: str) -> None:
        self.cancelled.append(generation_id)


def _call():
    tts = FakeTTS()
    call = CallSession(FakeSTT(), tts, lambda text: (f"echo:{text}", {}), vad=EnergyVad())
    return call, tts


def test_call_session_utterance_and_reply():
    call, _ = _call()
    utt = None
    pcm = _tone(1.2)
    for i in range(0, len(pcm), 3200):
        _, u = call.feed_pcm(pcm[i : i + 3200])
        utt = utt or u
    for i in range(0, len(_silence()), 3200):
        _, u = call.feed_pcm(_silence()[i : i + 3200])
        utt = utt or u
    assert utt and len(utt) > 3200
    reply = call.process_utterance(utt, call.seq, "call-x")
    assert reply["type"] == "audio" and reply["text"] == "echo:да"
    assert reply["_pcm"]


def test_barge_in_cancels_tts():
    call, tts = _call()
    call.mark_playing("g-1", duration_s=30.0)
    assert call.barged_in()
    pcm = _tone(0.4)
    started = False
    for i in range(0, len(pcm), 3200):
        s, _ = call.feed_pcm(pcm[i : i + 3200])
        started = started or s
    assert started
    assert call.stop_playback() == "g-1"
    assert tts.cancelled == ["g-1"]
    assert not call.barged_in()


def test_superseded_turn_is_dropped():
    call, _ = _call()
    call.seq = 5
    assert call.process_utterance(_tone(0.5), 4, "call-x") is None


def test_ws_call_rejects_anonymous():
    with pytest.raises(WebSocketDisconnect), client.websocket_connect("/voice/ws-call"):
        pass


def test_ws_call_ping():
    with client.websocket_connect("/voice/ws-call", **AUTH) as ws:
        assert ws.receive_json()["type"] == "ready"
        ws.send_json({"type": "ping"})
        assert ws.receive_json()["type"] == "pong"
