"""Per-patient turn locks — two replicas must never interleave one dialogue.

- With Redis: distributed lock (SET NX PX) with a heartbeat thread that
  extends the lease until the turn ends. A turn with DB retries can run
  longer than any fixed TTL; without renewal Redis would release mid-turn
  and a second replica would enter the same dialogue.
- Without Redis: process-local threading locks (no expiry issue).
  Correct for a single replica; documented, never claimed as distributed.
"""
from __future__ import annotations

import threading
from collections.abc import Iterator
from contextlib import contextmanager

LOCK_PREFIX = "dbi:lock:patient:"
LOCK_TTL_S = 15

_local_locks: dict[str, threading.Lock] = {}
_local_guard = threading.Lock()


def _local_lock(patient_id: str) -> threading.Lock:
    with _local_guard:
        return _local_locks.setdefault(patient_id, threading.Lock())


class Lease:
    """Handle to a held patient lock. `lost` is set by the heartbeat when
    the lease demonstrably moved to another holder (extend returned 0)."""

    def __init__(self) -> None:
        self.lost = False


@contextmanager
def patient_turn_lock(
    redis_url: str, patient_id: str, timeout_s: float = 10, lease_s: int = LOCK_TTL_S
) -> Iterator[Lease]:
    """Serialize one dialogue turn for a patient. Raises on lock timeout."""
    lease = Lease()
    if not redis_url:
        locked = _local_lock(patient_id).acquire(timeout=timeout_s)
        if not locked:
            raise TimeoutError(f"patient lock timeout: {patient_id}")
        try:
            yield lease
        finally:
            _local_lock(patient_id).release()
        return

    import redis

    from domain.adapter_errors import DependencyUnavailable

    try:
        client = redis.Redis.from_url(redis_url, socket_connect_timeout=5, socket_timeout=5)
        lock = client.lock(
            LOCK_PREFIX + patient_id,
            timeout=lease_s,
            blocking_timeout=timeout_s,
        )
        acquired = lock.acquire()
    except redis.RedisError as e:
        raise DependencyUnavailable(f"patient lock failed: {e}") from e
    if not acquired:
        raise TimeoutError(f"patient lock timeout: {patient_id}")
    # NOTE: redis-py keeps the ownership token thread-local, so Lock.reacquire()
    # cannot run in the heartbeat thread. Renewal is an atomic Lua
    # compare-and-expire with the token captured here instead.
    token = lock.local.token
    extend = client.register_script(
        "if redis.call('get', KEYS[1]) == ARGV[1] then "
        "return redis.call('pexpire', KEYS[1], ARGV[2]) else return 0 end"
    )
    stop = threading.Event()

    def _heartbeat() -> None:
        while not stop.wait(max(0.2, lease_s / 3)):
            try:
                if extend(keys=[LOCK_PREFIX + patient_id], args=[token, lease_s * 1000]) == 0:
                    lease.lost = True
                    break
            except Exception:
                break  # redis down; holder still finishes its turn

    beat = threading.Thread(target=_heartbeat, daemon=True)
    beat.start()
    try:
        yield lease
    finally:
        stop.set()
        beat.join(timeout=lease_s)
        try:
            lock.release()
        except Exception:
            pass  # expired/lost locks auto-release; nothing to do
