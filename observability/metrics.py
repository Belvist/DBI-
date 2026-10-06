"""In-memory counters + Prometheus text rendering. No external deps."""
from __future__ import annotations

import threading

_lock = threading.Lock()
_counters: dict[str, int] = {}


def inc(name: str, amount: int = 1) -> None:
    with _lock:
        _counters[name] = _counters.get(name, 0) + amount


def snapshot() -> dict[str, int]:
    with _lock:
        return dict(_counters)


def reset() -> None:
    with _lock:
        _counters.clear()


def render_prometheus() -> str:
    lines = []
    snap = snapshot()
    for name in sorted(snap):
        lines.append(f"# TYPE dbi_{name} counter")
        lines.append(f"dbi_{name} {snap[name]}")
    return "\n".join(lines) + ("\n" if lines else "")
