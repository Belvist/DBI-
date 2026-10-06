"""Clock: production runs on system time, never on the demo fixture."""
from __future__ import annotations

from datetime import datetime

from nlu import deterministic as det
from observability.clock import DEMO_NOW, SystemClock, resolve_clock


def test_production_clock_is_system():
    clock = resolve_clock("production", "")
    assert isinstance(clock, SystemClock)
    assert abs((clock.now() - datetime.now()).total_seconds()) < 5


def test_demo_and_test_clocks_are_pinned():
    assert resolve_clock("demo", "").now() == DEMO_NOW
    assert resolve_clock("test", "").now() == DEMO_NOW


def test_clock_override_parsing():
    assert isinstance(resolve_clock("demo", "system"), SystemClock)
    assert resolve_clock("demo", "fixed:2026-11-01T09:00").now() == datetime(2026, 11, 1, 9, 0)
    # override can never pin production to a fixture
    assert isinstance(resolve_clock("production", "fixed:2026-11-01T09:00"), SystemClock)


def test_tomorrow_is_relative_to_clock_not_fixture():
    now = datetime(2026, 11, 4, 10, 0)  # a Wednesday, far from DEMO_NOW
    r = det.parse("запиши меня завтра", now)
    assert r.date_from is not None
    assert (r.date_from.date() - now.date()).days == 1
