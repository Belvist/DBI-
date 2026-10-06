"""Distributed sessions: Redis parity, turn locks, cache bounds."""
from __future__ import annotations

import os
import threading
import time
from datetime import datetime

import pytest

from dialogue.state import DialogueState
from domain.models import PatientRef
from sessions.locks import patient_turn_lock
from sessions.redis_store import RedisSessionStore
from sessions.store import SessionStore

REDIS_URL = os.getenv("DBI_REDIS_URL", "redis://127.0.0.1:6379/0")


def _redis_up() -> bool:
    try:
        import redis

        redis.Redis.from_url(
            os.getenv("DBI_REDIS_URL", "redis://127.0.0.1:6379/0"),
            socket_connect_timeout=2,
        ).ping()
        return True
    except Exception:
        return False


needs_redis = pytest.mark.skipif(not _redis_up(), reason="redis not reachable")


def _state(pid: str) -> DialogueState:
    return DialogueState(patient=PatientRef(patient_id=pid))


def test_sqlite_and_redis_agree_on_contract(tmp_path):
    stores = [SessionStore(tmp_path / "s.sqlite")]
    if _redis_up():
        stores.append(RedisSessionStore(REDIS_URL))
    assert stores
    for i, store in enumerate(stores):
        pid = f"p-conform-{i}"
        assert store.load(pid) is None
        st = _state(pid)
        st.turn = 7
        store.save(st)
        back = store.load(pid)
        assert back is not None and back.turn == 7
        store.drop(pid)
        assert store.load(pid) is None


@needs_redis
def test_redis_ttl_expires():
    store = RedisSessionStore(REDIS_URL, ttl_s=1)
    store.save(_state("p-ttl"))
    assert store.load("p-ttl") is not None
    time.sleep(1.2)
    assert store.load("p-ttl") is None


def test_local_lock_serializes_turns():
    order: list[str] = []

    def turn(name: str):
        with patient_turn_lock("", "p-lock"):
            order.append(f"{name}-in")
            time.sleep(0.05)
            order.append(f"{name}-out")

    threads = [threading.Thread(target=turn, args=(f"t{i}",)) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    # no interleaving: every in is immediately followed by its out
    for i in range(0, len(order), 2):
        assert order[i].split("-")[0] == order[i + 1].split("-")[0]


@needs_redis
def test_redis_lock_survives_past_lease_via_heartbeat():
    # Lease 2s, turn holds 5s: without renewal the contender would get in.
    entered: list[bool] = []

    def holder():
        with patient_turn_lock(REDIS_URL, "p-heartbeat", timeout_s=10, lease_s=2):
            import time as _t

            _t.sleep(5)

    t = threading.Thread(target=holder)
    t.start()
    import time as _t

    _t.sleep(0.5)
    try:
        with pytest.raises(TimeoutError), patient_turn_lock(
            REDIS_URL, "p-heartbeat", timeout_s=3, lease_s=2
        ):
            entered.append(True)
    finally:
        t.join()
    assert not entered


@needs_redis
def test_redis_lock_timeout_when_held():
    import redis

    client = redis.Redis.from_url(REDIS_URL)
    lock = client.lock("dbi:lock:patient:p-held", timeout=30)
    assert lock.acquire()
    try:
        with pytest.raises(TimeoutError), patient_turn_lock(REDIS_URL, "p-held", timeout_s=1):
            pass
    finally:
        lock.release()


def test_cas_conflict_raises_stale_state(tmp_path):
    from sessions.errors import StaleState

    store = SessionStore(tmp_path / "s.sqlite")
    first = _state("p-cas")
    store.save(first)  # revision 0 -> 1
    stale = _state("p-cas")  # revision 0, same patient, older base
    with pytest.raises(StaleState):
        store.save(stale)
    # winner's state intact
    assert store.load("p-cas").revision == 1


@needs_redis
def test_two_replicas_alternate_without_rollback(tmp_path):
    """API-A and API-B share one Redis + one clinic; turns alternate.

    No replica may ever continue from stale local state: B must see the
    BOOK flow A started, and A must see the CONFIRM B produced.
    """
    import uuid

    from clinic_adapter.mock_sqlite import MockSqliteClinic
    from sessions.coordinator import run_turn
    from sessions.redis_store import RedisSessionStore

    clinic = MockSqliteClinic(path=tmp_path / "clinic.sqlite")
    pid = f"p-two-{uuid.uuid4().hex[:6]}"
    patient = PatientRef(patient_id=pid)
    now = datetime(2026, 10, 13, 12, 0)

    def turn_on_replica(text: str):
        store = RedisSessionStore(REDIS_URL)  # fresh objects per replica
        return run_turn(
            patient=patient, text=text, idempotency_key=None,
            adapter=clinic, snapshots=store, redis_url=REDIS_URL, now=now,
        )

    _, sess1 = turn_on_replica("Запиши меня к неврологу")
    assert sess1.state.phase.value == "elicit" and sess1.state.flow.value == "book"

    s2, sess2 = turn_on_replica("На следующей неделе вечером")
    assert sess2.state.phase.value == "propose" and len(sess2.state.candidates) == 3, s2[:120]

    s3, sess3 = turn_on_replica("Первый вариант")
    assert sess3.state.phase.value == "confirm", s3[:120]

    s4, sess4 = turn_on_replica("Да")
    assert "записаны" in s4.lower() and sess4.state.active_booking_id
    assert len(clinic.get_appointments(patient)) == 1


@needs_redis
def test_redis_cas_conflict_raises_stale_state():
    from sessions.errors import StaleState

    store = RedisSessionStore(REDIS_URL)
    store.drop("p-cas-r")
    first = _state("p-cas-r")
    store.save(first)
    with pytest.raises(StaleState):
        store.save(_state("p-cas-r"))
    assert store.load("p-cas-r").revision == 1
    store.drop("p-cas-r")
