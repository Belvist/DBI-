"""Deterministic NLU coverage for jury-critical utterances."""
from __future__ import annotations

from datetime import datetime

from nlu import deterministic as det
from nlu.schema import Intent

NOW = datetime(2026, 10, 13, 12, 0)  # Monday


def test_specialty_inflections():
    assert det.parse("Хочу записаться к кардиологу", NOW).specialty == "cardiology"
    assert det.parse("к неврологу", NOW).specialty == "neurology"


def test_next_week_evening():
    r = det.parse("на следующей неделе вечером", NOW)
    assert r.time_preference == "evening"
    assert r.date_from is not None and r.date_from.weekday() == 0


def test_status_intent():
    r = det.parse("На какое время я записан к Иванову?", NOW)
    assert r.intent == Intent.GET_STATUS


def test_reschedule_intent():
    r = det.parse("Не смогу. Давай в четверг утром", NOW)
    assert r.intent == Intent.RESCHEDULE
    assert r.date_from is not None and r.date_from.weekday() == 3


def test_half_past_six_confirm():
    r = det.parse("Давайте половину седьмого", NOW)
    assert r.intent == Intent.CONFIRM
