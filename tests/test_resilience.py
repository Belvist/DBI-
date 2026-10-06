"""Resilience: breaker, retry, fail-safe speech, metrics, readiness."""
from __future__ import annotations

import sqlite3
from datetime import datetime

from fastapi.testclient import TestClient

from api.booking_service import DialogueSession
from api.main import app
from clinic_adapter.mock_sqlite import MockSqliteClinic
from clinic_adapter.resilient import ResilientAdapter
from domain.adapter_errors import AdapterUnavailable
from domain.errors import SlotUnavailable
from domain.models import PatientRef
from observability import metrics
from resilience.guarded import CircuitBreaker, call_guarded

client = TestClient(app)

NOW = datetime(2026, 10, 13, 12, 0)


class Flaky:
    def __init__(self, fails: int, exc: Exception):
        self.left = fails
        self.exc = exc
        self.calls = 0

    def __call__(self, *args):
        self.calls += 1
        if self.calls <= self.left:
            raise self.exc
        return "ok"


def test_retry_recovers_transient_then_succeeds():
    metrics.reset()
    out = call_guarded(
        CircuitBreaker(), Flaky(2, ConnectionError("down")), attempts=3
    )
    assert out == "ok"


def test_retry_gives_up_and_counts():
    metrics.reset()
    breaker = CircuitBreaker(fail_threshold=10)
    try:
        call_guarded(breaker, Flaky(5, ConnectionError("down")), attempts=2)
        raise AssertionError("must raise")
    except AdapterUnavailable:
        pass
    assert metrics.snapshot()["adapter_errors_total"] == 1


def test_breaker_opens_and_recovers():
    breaker = CircuitBreaker(fail_threshold=2, window_s=60, open_s=0.05)
    for _ in range(2):
        try:
            call_guarded(breaker, Flaky(1, ConnectionError("x")), attempts=1)
        except (ConnectionError, AdapterUnavailable):
            pass
    assert breaker.is_open
    try:
        call_guarded(breaker, Flaky(0, ConnectionError("x")), attempts=1)
        raise AssertionError("must raise unavailable")
    except AdapterUnavailable:
        pass
    import time

    time.sleep(0.06)
    assert call_guarded(breaker, Flaky(0, ConnectionError("x")), attempts=1) == "ok"


def test_business_error_passes_through_untouched():
    metrics.reset()
    calls = []

    def taken(*a):
        calls.append(1)
        raise SlotUnavailable("taken")

    breaker = CircuitBreaker()
    try:
        call_guarded(breaker, taken, attempts=3)
        raise AssertionError("must raise")
    except SlotUnavailable:
        pass
    assert len(calls) == 1  # no retry on business facts
    assert not breaker.is_open
    assert metrics.snapshot().get("adapter_errors_total", 0) == 0


class DeadAdapter:
    """Every call fails with an infra error."""

    def find_doctors(self, *a, **k):
        raise sqlite3.OperationalError("database is locked")

    def find_slots(self, *a, **k):
        raise sqlite3.OperationalError("database is locked")

    def get_appointments(self, *a, **k):
        raise sqlite3.OperationalError("database is locked")

    def get_schedule(self, *a, **k):
        raise sqlite3.OperationalError("database is locked")


def test_dead_backend_speaks_safe_fallback():
    sess = DialogueSession(
        PatientRef(patient_id="p-dead"), ResilientAdapter(DeadAdapter()), now=NOW
    )
    speech = sess.turn("Запиши меня к неврологу на следующей неделе")
    assert "технические трудности" in speech.lower()
    assert "записаны" not in speech.lower()
    assert sess.state.active_booking_id is None


class CommitThenDrop:
    """Models commit-success + lost-response: the booking lands in the DB,
    but the call raises a transient error afterwards. Flip `drop` to False
    to simulate backend recovery."""

    def __init__(self, base):
        self._base = base
        self.drop = True

    def __getattr__(self, name):
        return getattr(self._base, name)

    def create_appointment(self, cmd):
        if not self.drop:
            return self._base.create_appointment(cmd)
        try:
            self._base.create_appointment(cmd)
        finally:
            raise ConnectionError("response lost")


def _drive_to_confirm(sess):
    sess.turn("Запиши меня к неврологу на следующей неделе вечером")
    assert sess.state.candidates
    sess.turn("Первый вариант")
    assert sess.state.phase.value == "confirm"


def test_commit_unknown_never_claims_nothing_recorded():
    sqlite = MockSqliteClinic()
    flaky = CommitThenDrop(sqlite)
    sess = DialogueSession(
        PatientRef(patient_id="p-unknown"), ResilientAdapter(flaky), now=NOW
    )
    _drive_to_confirm(sess)
    speech = sess.turn("Да")
    assert "подтвердить" in speech.lower()
    assert "ничего не записано" not in speech.lower()
    assert "записаны" not in speech.lower()
    # the lie we avoid: the booking IS in the DB
    assert len(sqlite.get_appointments(sess.patient)) == 1
    assert sess.state.active_booking_id is None
    # backend recovers: retry hits the same taken slot -> race recovery,
    # never a duplicate booking
    flaky.drop = False
    retry = sess.turn("Да")
    assert "недоступно" in retry.lower() or "другой вариант" in retry.lower()
    assert len(sqlite.get_appointments(sess.patient)) == 1


def test_ready_503_when_snapshots_die_after_startup(monkeypatch):
    import api.main as api_main

    class DeadSnapshots:
        def ping(self):
            raise ConnectionError("redis gone")

    monkeypatch.setattr(api_main, "_SNAPSHOTS", DeadSnapshots())
    r = client.get("/ready")
    assert r.status_code == 503


def test_breaker_thread_safety_smoke():
    import threading

    breaker = CircuitBreaker(fail_threshold=1000)
    def hammer():
        for _ in range(200):
            breaker.record_failure()
            assert isinstance(breaker.is_open, bool)
            breaker.record_success()

    threads = [threading.Thread(target=hammer) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert isinstance(breaker.is_open, bool)


def test_metrics_endpoint_lists_counters():
    metrics.reset()
    client.post(
        "/dialogue/turn",
        json={"text": "привет"},
        headers={"X-Patient-Token": "demo"},
    )
    r = client.get("/metrics")
    assert r.status_code == 200
    assert "dbi_turns_total" in r.text


def test_ready_ok_with_live_backend():
    r = client.get("/ready")
    assert r.status_code == 200 and r.json()["status"] == "ready"


def test_ready_503_when_backend_down(monkeypatch):
    import api.main as api_main

    # /ready probes the BASE adapter directly (never through the breaker),
    # so a dead backend is observed even with a closed circuit.
    monkeypatch.setattr(api_main, "_BASE", DeadAdapter())
    r = client.get("/ready")
    assert r.status_code == 503
