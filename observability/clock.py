"""Runtime clock: production runs on system time, never on a demo fixture.

- SystemClock: `datetime.now()` per call. The ONLY clock in production.
- FixedClock: pinned instant for tests / demo / eval reproducibility.

`DialogueSession(now=None)` still means "system now" (booking_service,
NLU parser), so library code stays correct even without an explicit clock;
the application layer (main/coordinator path) always passes one explicitly.
"""
from __future__ import annotations

from datetime import datetime


class SystemClock:
    def now(self) -> datetime:
        return datetime.now()


class FixedClock:
    def __init__(self, at: datetime) -> None:
        self._at = at

    def now(self) -> datetime:
        return self._at


DEMO_NOW = datetime(2026, 10, 13, 12, 0)


def resolve_clock(env: str, override: str = "") -> SystemClock | FixedClock:
    """Pick the clock. Production is ALWAYS system time.

    `override` (DBI_CLOCK) is an escape hatch for non-production demos:
    "system" or "fixed:<iso datetime>". It is IGNORED in production, so no
    env var can ever pin production dates to a fixture again.
    """
    if env == "production":
        return SystemClock()
    if override == "system":
        return SystemClock()
    if override.startswith("fixed:"):
        return FixedClock(datetime.fromisoformat(override[len("fixed:"):]))
    return FixedClock(DEMO_NOW)
