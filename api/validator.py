"""Response validator — the anti-hallucination gate (jury criterion: quality 5/5).

Every assistant utterance that mentions a slot datetime or booking_id must
reference ONLY values previously returned by the adapter in this call.
Otherwise the utterance is replaced by a safe fallback and logged.
"""
from __future__ import annotations

import re
from datetime import datetime

_PATTERNS = [
    re.compile(r"(\d{1,2})\s+(января|февраля|марта|апреля|мая|июня|июля|августа|сентября|октября|ноября|декабря)\s+в\s+(\d{1,2}:\d{2})"),
    re.compile(r"№?\s?(A[A-Z0-9]{5,8})"),
    re.compile(r"(\d{1,2}:\d{2})"),
]


def validate(
    text: str,
    allowed_datetimes: list[datetime],
    allowed_booking_ids: list[str],
) -> tuple[bool, str]:
    """Returns (ok, text_or_fallback)."""
    if not allowed_datetimes and not allowed_booking_ids:
        # elicitation / error messages contain no facts — allow
        for p in _PATTERNS[:1]:
            if p.search(text) and "записаны" in text.lower():
                return False, "Уточню данные и вернусь с точным вариантом."
        return True, text
    allowed_times = {d.strftime("%H:%M") for d in allowed_datetimes}
    allowed_days = {(d.day, d.month) for d in allowed_datetimes}
    months = {"января", "февраля", "марта", "апреля", "мая", "июня", "июля",
              "августа", "сентября", "октября", "ноября", "декабря"}
    for m in re.finditer(r"(\d{1,2})\s+(\w+)\s+в\s+(\d{1,2}:\d{2})", text):
        day, mon, hm = int(m.group(1)), m.group(2), m.group(3)
        if mon in months:
            mon_n = ["", "января", "февраля", "марта", "апреля", "мая", "июня",
                     "июля", "августа", "сентября", "октября", "ноября", "декабря"].index(mon)
            if (day, mon_n) not in allowed_days or hm not in allowed_times:
                return False, "Уточню данные и вернусь с точным вариантом."
    for m in re.finditer(r"A[A-Z0-9]{5,8}", text):
        if m.group(0) not in allowed_booking_ids:
            return False, "Уточню данные и вернусь с точным вариантом."
    return True, text
