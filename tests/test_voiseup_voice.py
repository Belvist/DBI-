"""voiseup voice engines — live only, skipped when workers are down."""
from __future__ import annotations

import urllib.request

import pytest

from voice.voiseup import VoiseupSTT, VoiseupTTS

STT_URL = "http://127.0.0.1:9101"
TTS_URL = "http://127.0.0.1:9102"


def _up(url: str) -> bool:
    try:
        urllib.request.urlopen(url + "/v1/capabilities", timeout=3)
        return True
    except Exception:
        return False


needs_stt = pytest.mark.skipif(not _up(STT_URL), reason="voiseup STT down")
needs_tts = pytest.mark.skipif(_up(TTS_URL) is False, reason="voiseup TTS down")


def _silent_pcm(seconds: float = 1.0, rate: int = 16000) -> bytes:
    import struct

    n = int(seconds * rate)
    return struct.pack(f"<{n}h", *([0] * n))


@needs_stt
def test_stt_rejects_garbage_size():
    from voice.voiseup import VoiceEngineError

    stt = VoiseupSTT(STT_URL)
    with pytest.raises(VoiceEngineError):
        stt.transcribe_pcm16(b"")
    with pytest.raises(VoiceEngineError):
        stt.transcribe_pcm16(b"x" * (61 * 16000 * 2))


@needs_stt
def test_stt_transcribes_real_utterance():
    import subprocess

    subprocess.run(
        ["say", "-v", "Milena", "-o", "/tmp/dbi_voice_test.aiff", "Запишите меня к врачу"],
        check=True,
    )
    subprocess.run(
        ["ffmpeg", "-y", "-i", "/tmp/dbi_voice_test.aiff",
         "-ar", "16000", "-ac", "1", "-f", "s16le", "/tmp/dbi_voice_test.pcm"],
        check=True, capture_output=True,
    )
    from pathlib import Path

    pcm = Path("/tmp/dbi_voice_test.pcm").read_bytes()
    text = VoiseupSTT(STT_URL, timeout=60).transcribe_pcm16(pcm)
    assert "запиш" in text.lower()  # "запишите", не "записа..." — ш, не с


@needs_tts
def test_tts_synthesizes_russian():
    rate, pcm, gen_id = VoiseupTTS(TTS_URL, timeout=90).synthesize("Здравствуйте!")
    assert rate in (16000, 22050, 24000, 44100, 48000)
    assert len(pcm) > 1000 and len(pcm) % 2 == 0
    assert gen_id.startswith("g-")


@needs_stt
def test_transcribe_endpoint_roundtrip():
    from fastapi.testclient import TestClient

    from api.main import app

    client = TestClient(app)
    r = client.post(
        "/voice/transcribe?sample_rate=16000",
        content=_silent_pcm(0.5),
        headers={"Content-Type": "application/octet-stream", "X-Patient-Token": "demo"},
    )
    assert r.status_code == 200 and "text" in r.json()


@needs_tts
def test_speak_endpoint_returns_wav():
    from fastapi.testclient import TestClient

    from api.main import app

    client = TestClient(app)
    r = client.post(
        "/voice/speak",
        json={"text": "Здравствуйте!"},
        headers={"X-Patient-Token": "demo"},
    )
    assert r.status_code == 200
    assert r.headers["content-type"] == "audio/wav"
    assert r.content[:4] == b"RIFF"


def test_transcribe_requires_auth():
    from fastapi.testclient import TestClient

    from api.main import app

    client = TestClient(app)
    r = client.post("/voice/transcribe", content=b"\x00" * 3200)
    assert r.status_code == 401
