"""Voice channel tests: WS protocol + barge-in semantics (no mic needed)."""
from __future__ import annotations

from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from api.booking_service import DialogueSession
from api.main import app
from clinic_adapter.mock_sqlite import MockSqliteClinic
from domain.models import PatientRef
from voice.runtime import VoiceRuntime
from voice.stt import BrowserSTT, LocalSTTStub
from voice.tts import BrowserTTS, LocalTTSStub

client = TestClient(app)


def test_ws_full_book_flow():
    with client.websocket_connect("/voice/ws/p-ws-1") as ws:
        ws.send_json({"type": "ping"})
        assert ws.receive_json()["type"] == "pong"
        ws.send_json({"type": "user_text", "text": "Запиши меня к неврологу на следующей неделе вечером"})
        m1 = ws.receive_json()
        assert m1["type"] == "speech" and m1["flow"] == "book"
        ws.send_json({"type": "user_text", "text": "Первый вариант"})
        m2 = ws.receive_json()
        assert m2["type"] == "speech"
        ws.send_json({"type": "user_text", "text": "Да"})
        m3 = ws.receive_json()
        assert m3["type"] == "speech" and m3["booking_id"]
        assert "записаны" in m3["text"].lower()


def test_ws_barge_in_cancels_generation():
    with client.websocket_connect("/voice/ws/p-ws-2") as ws:
        ws.send_json({"type": "user_text", "text": "Запиши меня к неврологу"})
        ws.receive_json()  # elicitation speech
        ws.send_json({"type": "barge_in"})
        ack = ws.receive_json()
        assert ack["type"] == "stopped"
        kinds = [e.kind for e in ws.app.state.session_factory("p-ws-2").trace.events]
        assert "barge_in" in kinds


def test_ws_unknown_message():
    with client.websocket_connect("/voice/ws/p-ws-3") as ws:
        ws.send_json({"type": "zzz"})
        assert ws.receive_json()["type"] == "error"


def test_runtime_supersede_on_barge_in():
    sess = DialogueSession(
        PatientRef(patient_id="p-rt-1"), MockSqliteClinic(), now=datetime(2026, 10, 13, 12, 0)
    )
    rt = VoiceRuntime(sess)
    speech, gen = rt.on_user_text("Запиши меня к неврологу на следующей неделе вечером")
    assert speech and not gen.cancelled
    rt.on_user_start()  # user interrupts playback
    assert gen.cancelled


def test_provider_contracts():
    assert "browser" in BrowserSTT().describe()
    assert "browser" in BrowserTTS().describe()
    with pytest.raises(NotImplementedError):
        LocalSTTStub().transcribe(b"\x00" * 640)
    with pytest.raises(NotImplementedError):
        LocalTTSStub().synthesize("привет")


def test_voice_page_served():
    r = client.get("/voice")
    assert r.status_code == 200 and "Говорить" in r.text
    assert "ru-RU" in r.text
