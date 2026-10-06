"""Bounded in-memory session cache — the dict must not grow forever.

LRU by recency + idle TTL. The snapshot store stays the source of truth;
eviction only drops the hot object, next access restores from snapshot.
"""
from __future__ import annotations

import time
from collections import OrderedDict
from collections.abc import Callable
from threading import Lock


class SessionCache[V]:
    """Thread-safe: HTTP workers and WS threads share one instance."""

    def __init__(self, maxsize: int = 1000, idle_ttl_s: float = 3600) -> None:
        self._maxsize = maxsize
        self._ttl = idle_ttl_s
        self._guard = Lock()
        self._items: OrderedDict[str, tuple[float, V]] = OrderedDict()

    def __len__(self) -> int:
        with self._guard:
            return len(self._items)

    def get_or_create(self, key: str, factory: Callable[[], V]) -> V:
        # Factory runs outside the lock: session restore does I/O and must
        # never block other patients. Double-build is benign (last wins).
        with self._guard:
            if key in self._items:
                _, value = self._items.pop(key)
                self._items[key] = (time.monotonic(), value)
                return value
        value = factory()
        with self._guard:
            self._items[key] = (time.monotonic(), value)
            self._evict_locked(time.monotonic())
        return value

    def clear(self) -> None:
        with self._guard:
            self._items.clear()

    def _evict_locked(self, now: float) -> None:
        # Caller must hold _guard.
        expired = [k for k, (t, _) in self._items.items() if now - t > self._ttl]
        for k in expired:
            del self._items[k]
        while len(self._items) > self._maxsize:
            self._items.popitem(last=False)
