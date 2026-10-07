"""Deterministic Russian NLU. No network, no LLM, test-covered.

Resolves relative dates against a reference 'today' (default: real now,
tests/demo pin to 2026-10-13 so 'следующая неделя' is stable).
LLM proposer may only fill gaps — deterministic output wins on conflict.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta

from nlu.schema import Intent, NLUResult

_SPEC_PATTERNS: list[tuple[str, list[str]]] = [
    ("cardiology", ["кардиолог"]),
    ("neurology", ["невролог"]),
    ("therapy", ["терапевт"]),
    ("dermatology", ["дерматолог"]),
    ("ophthalmology", ["офтальмолог", "окулист"]),
]

_WEEKDAYS_RU: dict[str, int] = {
    "понедельник": 0, "вторник": 1, "среду": 2, "среда": 2, "среде": 2,
    "четверг": 3, "пятницу": 4, "пятница": 4, "субботу": 5, "воскресенье": 6,
}

_MONTHS_RU: dict[str, int] = {
    "января": 1, "февраля": 2, "марта": 3, "апреля": 4, "мая": 5, "июня": 6,
    "июля": 7, "августа": 8, "сентября": 9, "октября": 10, "ноября": 11, "декабря": 12,
}

_SURNAMES = ["петров", "иванов", "сидоров", "козлов", "смирнов", "федоров"]


def _norm(t: str) -> str:
    return re.sub(r"\s+", " ", t.lower().strip())


def _find_specialty(t: str) -> str | None:
    for spec, stems in _SPEC_PATTERNS:
        for s in stems:
            if s in t:
                return spec
    return None


def _find_doctor(t: str) -> str | None:
    for s in _SURNAMES:
        if s in t:
            return s
    return None


def _monday(d: datetime) -> datetime:
    return (d - timedelta(days=d.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)


def _resolve_dates(t: str, now: datetime) -> tuple[datetime | None, datetime | None, str | None]:
    """Returns (date_from, date_to, time_pref)."""
    time_pref: str | None = None
    if re.search(r"утр|до обед|перв.*половин", t):
        time_pref = "morning"
    if re.search(r"вечер|после шести|после 18|вторая половина", t):
        time_pref = "evening"

    # explicit "20 октября"
    m = re.search(r"(\d{1,2})\s+(января|февраля|марта|апреля|мая|июня|июля|августа|сентября|октября|ноября|декабря)", t)
    if m:
        day = int(m.group(1))
        month = _MONTHS_RU[m.group(2)]
        year = now.year
        try:
            d = datetime(year, month, day)
        except ValueError:
            return None, None, time_pref
        return d.replace(hour=0, minute=0), d.replace(hour=23, minute=59), time_pref

    # weekday mention -> nearest upcoming that weekday.
    # Last mention wins: "не среда, четверг" means Thursday.
    best: tuple[int, int] | None = None  # (pos, weekday)
    for name, wd in _WEEKDAYS_RU.items():
        pos = t.find(name)
        if pos >= 0 and (best is None or pos >= best[0]):
            best = (pos, wd)
    if best is not None:
        wd = best[1]
        delta = (wd - now.weekday()) % 7
        if delta == 0:
            delta = 7 if "следующ" in t else 0
        d = (now + timedelta(days=delta)).replace(hour=0, minute=0, second=0, microsecond=0)
        return d, d.replace(hour=23, minute=59), time_pref

    if "следующ" in t and "недел" in t:
        mon = _monday(now) + timedelta(days=7)
        return mon, mon + timedelta(days=7), time_pref
    if "эта недел" in t or "на неделе" in t:
        mon = _monday(now)
        return mon, mon + timedelta(days=7), time_pref
    if "сегодня" in t:
        d = now.replace(hour=0, minute=0, second=0, microsecond=0)
        return d, d + timedelta(days=1), time_pref
    if "завтра" in t:
        d = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        return d, d + timedelta(days=1), time_pref
    return None, None, time_pref


_CONFIRM = re.compile(r"^(да|давайте|хорошо|соглас|подтверждаю|записывай|половина|в \d|перв|втор|подходит|беру)")

# NB: all "не ..." denials REQUIRE a word boundary before "не".
# Without it "мне надо" / "мне подходит" false-positive as a refusal.
_DENY_STRONG = re.compile(r"вообще не")
_DENY_NEG = re.compile(r"\bне\s+(могу|подходит|надо|хочу|устраивает)\b")

# hour words, longest first ("восемь" contains "семь" — order matters)
_HOUR_WORDS: list[tuple[str, int]] = [
    ("двадцат", 20), ("девятнадцат", 19), ("восемнадцат", 18),
    ("семнадцат", 17), ("шестнадцат", 16), ("пятнадцат", 15),
    ("четырнадцат", 14), ("тринадцат", 13), ("двенадцат", 12),
    ("одиннадцат", 11), ("десят", 10), ("девят", 9), ("восьм", 8),
    ("семь", 7), ("шесть", 6), ("пять", 5),
]


def _extract_exact_time(t: str) -> str | None:
    """"в 20:00" / "где-то в 20" / "к восьми вечера" -> "HH:MM"."""
    m = re.search(r"(\d{1,2})[:.](\d{2})", t)
    if m:
        hh, mm = int(m.group(1)), int(m.group(2))
        if 0 <= hh <= 23 and 0 <= mm <= 59:
            return f"{hh:02d}:{mm:02d}"
    m = re.search(r"\bв\s+(\d{1,2})\b", t)
    hour: int | None = int(m.group(1)) if m else None
    if hour is None:
        for stem, h in _HOUR_WORDS:
            if re.search(rf"\b{stem}\w*", t):
                hour = h
                break
    if hour is None or not 0 <= hour <= 23:
        return None
    if hour < 12 and re.search(r"вечер", t):
        hour += 12
    return f"{hour:02d}:00"


def parse(text: str, now: datetime | None = None) -> NLUResult:
    now = now or datetime.now()
    t = _norm(text)
    spec = _find_specialty(t)
    doc = _find_doctor(t)
    df, dt, tpref = _resolve_dates(t, now)
    exact = _extract_exact_time(t)

    # status of existing booking
    if re.search(r"(когда|какая|на какое время|моя запись|я записан|статус)", t) and re.search(
        r"(запиш|запис|прием|приём|врач)", t
    ):
        return NLUResult(intent=Intent.GET_STATUS, specialty=spec, doctor_text=doc,
                          date_from=df, date_to=dt, time_preference=tpref,
                          exact_time=exact, confidence=0.9)
    # reschedule
    if re.search(r"(перенес|перенести|перезапис|не смогу|давай в|другое время|поменять)", t):
        return NLUResult(intent=Intent.RESCHEDULE, specialty=spec, doctor_text=doc,
                          date_from=df, date_to=dt, time_preference=tpref,
                          exact_time=exact, raw_time_text=text, confidence=0.9)
    # schedule inquiry
    if re.search(r"(расписание|кто (принимает|работает)|когда принимает|часы приема|свободно)", t):
        return NLUResult(intent=Intent.GET_SCHEDULE, specialty=spec, doctor_text=doc,
                          date_from=df, date_to=dt, time_preference=tpref,
                          exact_time=exact, confidence=0.85)
    # booking request first: "давайте запишите..." is BOOK, not CONFIRM
    if re.search(r"(запиш|запис|прием|приём|хочу к|нужен|нужна|ко врачу|доктор)", t):
        return NLUResult(intent=Intent.BOOK, specialty=spec, doctor_text=doc,
                          date_from=df, date_to=dt, time_preference=tpref,
                          exact_time=exact, raw_time_text=text,
                          confidence=0.9 if (spec or doc) else 0.6)
    # exact-time wish without a booking verb: "мне надо в 20:00",
    # "какие свободы на 20:00", "есть что-то на восемь вечера"
    if exact is not None and re.search(
        r"(надо|хочу|нужен|нужна|можно|есть|свобод|какие|время|слот|мест)", t
    ):
        return NLUResult(intent=Intent.BOOK, specialty=spec, doctor_text=doc,
                          date_from=df, date_to=dt, time_preference=tpref,
                          exact_time=exact, raw_time_text=text,
                          confidence=0.8 if (spec or doc or df) else 0.55)
    # strong denial beats correction: "вторник вообще не могу", "не подходит".
    # \b before "не" is load-bearing: "мне надо" must NOT match.
    if _DENY_STRONG.search(t) or _DENY_NEG.search(t):
        return NLUResult(intent=Intent.DENY, confidence=0.88)
    # strict correction "не среда, четверг": weekday after "не" + another weekday
    _wds = "понедельник|вторник|среду|среда|четверг|пятницу|пятница|субботу|воскресенье"
    if re.search(rf"\bне\s+({_wds})\W+({_wds})", t):
        return NLUResult(intent=Intent.CORRECT, specialty=spec, doctor_text=doc,
                          date_from=df, date_to=dt, time_preference=tpref,
                          exact_time=exact, raw_time_text=text, confidence=0.8)
    # bare denial
    if t.strip() in ("нет", "не могу", "не подходит", "нет, не могу"):
        return NLUResult(intent=Intent.DENY, confidence=0.85)
    # confirmation of a proposed slot
    if _CONFIRM.search(t) or t.strip() in ("да", "давайте", "хорошо", "подходит"):
        slot_idx = None
        if "перв" in t:
            slot_idx = 0
        elif "втор" in t and "вариант" in t:
            slot_idx = 1
        return NLUResult(intent=Intent.CONFIRM, slot_index=slot_idx,
                          exact_time=exact, raw_time_text=text, confidence=0.8)
    # bare specialty mention in booking context ("к неврологу")
    if spec and len(t.split()) <= 4:
        return NLUResult(intent=Intent.BOOK, specialty=spec, confidence=0.75)
    return NLUResult(intent=Intent.UNKNOWN, specialty=spec, doctor_text=doc,
                      date_from=df, date_to=dt, time_preference=tpref,
                      exact_time=exact, confidence=0.3)
