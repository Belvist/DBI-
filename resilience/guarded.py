"""Transient-error retry + circuit breaker for clinic adapter calls.

- Retried: infrastructure flakes (connection loss, timeouts, SQLite lock).
  WRITEs are safe to retry: every one carries an idempotency_key.
- NEVER retried / NEVER counted as infra failure: SlotUnavailable and other
  business errors. A taken slot is a fact, not an outage; the engine
  re-searches instead.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field

from domain.adapter_errors import AdapterUnavailable
from domain.errors import SlotUnavailable


def is_transient(exc: BaseException) -> bool:
    if isinstance(exc, (ConnectionError, TimeoutError)):
        return True
    name = type(exc).__name__
    mod = type(exc).__module__
    if "psycopg" in mod and name in ("OperationalError", "ConnectionFailure", "could not connect"):
        return True
    if "sqlite3" in mod and name == "OperationalError":
        return "locked" in str(exc).lower()
    return False


@dataclass
class CircuitBreaker:
    fail_threshold: int = 5
    window_s: float = 60.0
    open_s: float = 30.0
    _failures: list[float] = field(default_factory=list)
    _opened_at: float | None = field(default=None)

    def _prune(self, now: float) -> None:
        self._failures = [t for t in self._failures if now - t <= self.window_s]
        if self._opened_at is not None and now - self._opened_at >= self.open_s:
            self._opened_at = None  # half-open: next call probes

    @property
    def is_open(self) -> bool:
        self._prune(time.monotonic())
        return self._opened_at is not None

    def record_success(self) -> None:
        self._failures.clear()
        self._opened_at = None

    def record_failure(self) -> bool:
        """Returns True if the breaker just opened."""
        now = time.monotonic()
        self._failures.append(now)
        self._prune(now)
        if len(self._failures) >= self.fail_threshold and self._opened_at is None:
            self._opened_at = now
            return True
        return False

    def guard(self) -> None:
        if self.is_open:
            raise AdapterUnavailable("clinic backend circuit is open")


def call_guarded(breaker: CircuitBreaker, fn, *args, attempts: int = 3, **kwargs):
    """Run fn with breaker + transient retries. Business errors pass through."""
    from observability import metrics

    breaker.guard()
    last: BaseException | None = None
    for attempt in range(attempts):
        try:
            out = fn(*args, **kwargs)
            breaker.record_success()
            return out
        except SlotUnavailable:
            raise
        except Exception as e:
            if not is_transient(e):
                raise
            last = e
            if attempt == attempts - 1:
                if breaker.record_failure():
                    metrics.inc("breaker_opens_total")
                metrics.inc("adapter_errors_total")
                raise AdapterUnavailable("clinic backend unavailable") from e
            time.sleep(0.05 * (2**attempt))
    raise AdapterUnavailable("clinic backend unavailable") from last
