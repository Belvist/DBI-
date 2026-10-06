"""Distributed sessions: Redis parity, turn locks, cache bounds."""
from __future__ import annotations

import os
import threading
import time

import pytest

from dialogue.state import DialogueState
from domain.models import PatientRef
from sessions.cache import SessionCache
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


def test_cache_evicts_idle_and_bounds_size():
    cache: SessionCache[int] = SessionCache(maxsize=3, idle_ttl_s=0.05)
    for i in range(5):
        cache.get_or_create(f"k{i}", lambda i=i: i)
    assert len(cache) == 3
    time.sleep(0.06)
    cache.get_or_create("fresh", lambda: 99)
    assert len(cache) == 1


def test_cache_factory_called_once():
    cache: SessionCache[object] = SessionCache()
    calls = []
    obj = cache.get_or_create("k", lambda: calls.append(1) or object())
    assert cache.get_or_create("k", lambda: object()) is obj
    assert len(calls) == 1


def test_cache_thread_safety_smoke():
    cache: SessionCache[int] = SessionCache(maxsize=50, idle_ttl_s=60)

    def hammer(n: int):
        for i in range(200):
            cache.get_or_create(f"k{(n + i) % 60}", lambda i=i: i)

    threads = [threading.Thread(target=hammer, args=(n,)) for n in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(cache) <= 50
