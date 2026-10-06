"""ws-call E2E against fake STT/TTS HTTP workers (runs in CI).

Proves the NETWORK contract + concurrency, not model quality:
- PCM -> VAD endpoint -> STT HTTP -> dialogue -> TTS NDJSON stream
- first audio chunk arrives while synthesis still streams (no full buffer)
- one call_id threads STT/TTS/cancel
- barge-in during playback: immediate "stop", /v1/cancel with the live
  generation_id, stale chunks never reach the socket, next turn proceeds
"""
from __future__ import annotations

import base64
import json
import struct
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from fastapi.testclient import TestClient

from api.main import app

client = TestClient(app)


from pathlib import Path

_FIX = Path(__file__).resolve().parent / "fixtures"


def _fixture(name: str) -> bytes:
    # Real recorded Russian speech: synthetic tones do not pass Silero VAD.
    return (_FIX / f"{name}.pcm").read_bytes()


def _silence(seconds: float = 1.0) -> bytes:
    n = int(seconds * 16000)
    return struct.pack(f"<{n}h", *([0] * n))


class _Store:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.stt_calls: list[dict] = []
        self.tts_calls: list[dict] = []
        self.cancels: list[dict] = []


def _make_servers(store: _Store, chunk_delay: float = 0.4):
    class STT(BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length) or b"{}")
            with store.lock:
                store.stt_calls.append(body)
            time.sleep(0.1)
            out = json.dumps({"text": "да"}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(out)))
            self.end_headers()
            self.wfile.write(out)

        def log_message(self, *a):
            pass

    class TTS(BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length) or b"{}")
            if self.path == "/v1/cancel":
                with store.lock:
                    store.cancels.append(body)
                out = b'{"ok": true}'
                self.send_response(200)
                self.send_header("Content-Length", str(len(out)))
                self.end_headers()
                self.wfile.write(out)
                return
            with store.lock:
                store.tts_calls.append(body)
            self.send_response(200)
            self.send_header("Content-Type", "application/x-ndjson")
            self.end_headers()
            for i in range(3):
                time.sleep(chunk_delay)
                line = json.dumps({
                    "sequence": i, "sample_rate": 16000,
                    "pcm_b64": base64.b64encode(b"\x01\x02" * 1600).decode(),
                }) + "\n"
                try:
                    self.wfile.write(line.encode())
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    return
            try:
                self.wfile.write(b'{"done": true}\n')
            except (BrokenPipeError, ConnectionResetError):
                pass

        def log_message(self, *a):
            pass

    stt = ThreadingHTTPServer(("127.0.0.1", 0), STT)
    tts = ThreadingHTTPServer(("127.0.0.1", 0), TTS)
    for s in (stt, tts):
        threading.Thread(target=s.serve_forever, daemon=True).start()
    return stt, tts


@pytest.fixture()
def fake_workers(monkeypatch):
    import api.main as api_main

    store = _Store()
    stt, tts = _make_servers(store)
    monkeypatch.setattr(
        api_main.app.state, "voice_stt_url",
        f"http://127.0.0.1:{stt.server_port}",
    )
    monkeypatch.setattr(
        api_main.app.state, "voice_tts_url",
        f"http://127.0.0.1:{tts.server_port}",
    )
    yield store
    stt.shutdown()
    tts.shutdown()


def _send_utterance(ws, pcm: bytes, frame: int = 3200) -> None:
    for i in range(0, len(pcm), frame):
        ws.send_bytes(pcm[i : i + frame])
    ws.send_bytes(_silence(1.2))


def test_ws_call_full_flow_chunked_audio(fake_workers):
    call_id = None
    with client.websocket_connect("/voice/ws-call", subprotocols=["dbi-voice", "demo"]) as ws:
        ready = ws.receive_json()
        assert ready["type"] == "ready"
        call_id = ready["call_id"]
        _send_utterance(ws, _fixture("ru_da"))
        first_audio_at = None
        seen_turn = False
        deadline = time.monotonic() + 30
        audio_frames = 0
        while time.monotonic() < deadline:
            msg = ws.receive_json()
            if msg["type"] == "turn":
                seen_turn = True
                assert msg["heard"] == "да"
            elif msg["type"] == "audio":
                if first_audio_at is None:
                    first_audio_at = time.monotonic()
                ws.receive_bytes()
                audio_frames += 1
                if audio_frames >= 3:
                    break
        assert seen_turn and audio_frames >= 3 and first_audio_at is not None
    # one call_id across STT + TTS
    assert fake_workers.stt_calls and fake_workers.tts_calls
    assert {c["call_id"] for c in fake_workers.stt_calls} == {call_id}
    assert {c["call_id"] for c in fake_workers.tts_calls} == {call_id}


def test_ws_call_barge_cancels_live_tts(fake_workers):
    with client.websocket_connect("/voice/ws-call", subprotocols=["dbi-voice", "demo"]) as ws:
        ws.receive_json()  # ready
        _send_utterance(ws, _fixture("ru_da"))
        gen = None
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            msg = ws.receive_json()
            if msg["type"] == "audio":
                gen = msg["gen"]
                ws.receive_bytes()
                break
        assert gen
        # user interrupts while TTS still streams remaining chunks
        _send_utterance(ws, _fixture("ru_da"))
        stopped = False
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            msg = ws.receive_json()
            if msg["type"] == "stop":
                stopped = True
                break
            if msg["type"] == "audio":
                ws.receive_bytes()  # drain (belongs to killed gen or next turn)
        assert stopped
        time.sleep(0.5)  # let the background cancel land
        cancels = [c.get("generation_id") for c in fake_workers.cancels]
        assert gen in cancels
