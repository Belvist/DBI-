"""Bounded in-memory session cache — the dict must not grow forever.

LRU by recency + idle TTL. The snapshot store stays the source of truth;
eviction only drops the hot object, next access restores from snapshot.
"""
from __future__ import annotations

import time
from collections import OrderedDict
from collections.abc import Callable


class SessionCache[V]:
    def __init__(self, maxsize: int = 1000, idle_ttl_s: float = 3600) -> None:
        self._maxsize = maxsize
        self._ttl = idle_ttl_s
        self._items: OrderedDict[str, tuple[float, V]] = OrderedDict()

    def __len__(self) -> int:
        return len(self._items)

    def get_or_create(self, key: str, factory: Callable[[], V]) -> V:
        now = time.monotonic()
        if key in self._items:
            _, value = self._items.pop(key)
            self._items[key] = (now, value)
            return value
        value = factory()
        self._items[key] = (now, value)
        self._evict(now)
        return value

    def clear(self) -> None:
        self._items.clear()

    def _evict(self, now: float) -> None:
        expired = [k for k, (t, _) in self._items.items() if now - t > self._ttl]
        for k in expired:
            del self._items[k]
        while len(self._items) > self._maxsize:
            self._items.popitem(last=False)
