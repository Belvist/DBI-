"""Per-patient turn locks — two replicas must never interleave one dialogue.

- With Redis: distributed lock (SET NX PX), so API-1 and API-2 serialize
  turns for the same patient. Lock release is ownership-checked by redis-py.
- Without Redis: process-local threading locks. Correct for a single
  replica; documented, never silently claimed as distributed.
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


@contextmanager
def patient_turn_lock(
    redis_url: str, patient_id: str, timeout_s: float = 10
) -> Iterator[None]:
    """Serialize one dialogue turn for a patient. Raises on lock timeout."""
    if redis_url:
        import redis

        client = redis.Redis.from_url(
            redis_url, socket_connect_timeout=5, socket_timeout=5
        )
        lock = client.lock(
            LOCK_PREFIX + patient_id,
            timeout=LOCK_TTL_S,
            blocking_timeout=timeout_s,
        )
        acquired = lock.acquire()
        if not acquired:
            raise TimeoutError(f"patient lock timeout: {patient_id}")
        try:
            yield
        finally:
            try:
                lock.release()
            except Exception:
                pass  # expired locks auto-release; nothing to do
    else:
        locked = _local_lock(patient_id).acquire(timeout=timeout_s)
        if not locked:
            raise TimeoutError(f"patient lock timeout: {patient_id}")
        try:
            yield
        finally:
            _local_lock(patient_id).release()
