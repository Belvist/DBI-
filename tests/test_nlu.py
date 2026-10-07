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


def test_mne_nado_is_not_deny():
    # "мне надо" contains "не надо" as substring — must NOT match.
    for t in ["Блин мне надо где-то в 20:00", "Мне подходит вторник", "Мне надо к врачу"]:
        assert det.parse(t, NOW).intent != Intent.DENY, t


def test_real_deny_still_works():
    for t in ["Нет, вторник вообще не могу", "Не подходит", "Не надо"]:
        assert det.parse(t, NOW).intent == Intent.DENY, t


def test_exact_time_requests():
    r = det.parse("мне надо где-то в 20:00", NOW)
    assert r.intent == Intent.BOOK and r.exact_time == "20:00"
    r = det.parse("А какие свободы на 20:00", NOW)
    assert r.intent == Intent.BOOK and r.exact_time == "20:00"
    r = det.parse("давай к восьми вечера", NOW)
    assert r.exact_time == "20:00"
