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
    def __init__(self) -> None:
        self.seen_call_ids: list = []

    def transcribe_pcm16(self, pcm: bytes, sample_rate: int = 16000, call_id=None) -> str:
        assert len(pcm) > 3200
        self.seen_call_ids.append(call_id)
        return "да"


class FakeTTS:
    def __init__(self) -> None:
        self.cancelled: list[str] = []

    def synthesize(self, text: str, call_id=None):
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


def test_ws_barge_invalidates_inflight_pipeline():
    import queue

    from api.voice_call import _Shared, _barge

    class FakeTaskGroup:
        def __init__(self) -> None:
            self.calls = []

        def start_soon(self, fn, *args) -> None:
            self.calls.append((fn, args))

    shared = _Shared(seq=7, speaking=True, tts_gen="g-live")
    out = queue.Queue()
    tts = FakeTTS()
    tg = FakeTaskGroup()

    _barge(shared, out, tts, tg)

    assert shared.seq == 8
    assert not shared.speaking
    assert shared.tts_gen is None
    kind, payload = out.get_nowait()
    assert kind == "stop"
    assert payload == {"type": "stop", "gen": "g-live"}
    assert tg.calls and tg.calls[0][1][-1] == "g-live"


def test_superseded_turn_is_dropped():
    call, _ = _call()
    call.seq = 5
    assert call.process_utterance(_tone(0.5), 4, "call-x") is None


def test_streaming_barge_drops_first_stale_audio():
    import queue

    from voice.call import run_pipeline_streaming

    class StreamingTTS(FakeTTS):
        def synthesize_stream(self, text: str, call_id=None):
            yield 16000, b"\x01\x02" * 1600, "g-race", False

    tts = StreamingTTS()
    call = CallSession(
        FakeSTT(), tts, lambda text: (f"echo:{text}", {}),
        vad=EnergyVad(),
    )
    out = queue.Queue()
    live = {"value": True}

    def on_speak(gen_id: str) -> None:
        out.put(("speak", {"gen": gen_id}))
        # Simulate a barge racing exactly with generation activation.
        live["value"] = False

    run_pipeline_streaming(
        call, _tone(0.5), 1, "call-race", out,
        is_live=lambda: live["value"], on_speak=on_speak,
    )

    items = []
    while not out.empty():
        items.append(out.get_nowait())
    assert any(item[0] == "speak" for item in items)
    assert not any(item[0] == "audio" for item in items)
    assert tts.cancelled == ["g-race"]


def test_ws_call_rejects_anonymous():
    with pytest.raises(WebSocketDisconnect), client.websocket_connect("/voice/ws-call"):
        pass


def test_ws_call_ping():
    with client.websocket_connect("/voice/ws-call", **AUTH) as ws:
        assert ws.receive_json()["type"] == "ready"
        ws.send_json({"type": "ping"})
        assert ws.receive_json()["type"] == "pong"
