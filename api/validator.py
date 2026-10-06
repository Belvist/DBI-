"""Response validator — anti-hallucination gate.

Any concrete slot datetime or booking id spoken by the assistant must be
grounded in adapter-returned facts. Validation is performed on the exact
(day, month, time) tuple, not on independent day/time sets.
"""
from __future__ import annotations

import re
from datetime import datetime

_MONTHS = [
    "", "января", "февраля", "марта", "апреля", "мая", "июня",
    "июля", "августа", "сентября", "октября", "ноября", "декабря",
]
_MONTH_TO_NUM = {name: i for i, name in enumerate(_MONTHS) if name}
_FULL_DT_RE = re.compile(
    r"(\d{1,2})\s+"
    r"(января|февраля|марта|апреля|мая|июня|июля|августа|сентября|октября|ноября|декабря)"
    r"\s+в\s+(\d{1,2}:\d{2})"
)
_TIME_RE = re.compile(r"(?<!\d)(\d{1,2}:\d{2})(?!\d)")
_BOOKING_RE = re.compile(r"\bA[A-Z0-9]{5,8}\b")

_SAFE_FALLBACK = "Уточню данные и вернусь с точным вариантом."


def validate(
    text: str,
    allowed_datetimes: list[datetime],
    allowed_booking_ids: list[str],
) -> tuple[bool, str]:
    """Return (ok, text_or_fallback)."""
    allowed_pairs = {
        (d.day, d.month, d.strftime("%H:%M"))
        for d in allowed_datetimes
    }
    allowed_times = {d.strftime("%H:%M") for d in allowed_datetimes}
    allowed_ids = set(allowed_booking_ids)

    full_matches = list(_FULL_DT_RE.finditer(text))
    for m in full_matches:
        day = int(m.group(1))
        month = _MONTH_TO_NUM[m.group(2)]
        hm = m.group(3)
        if (day, month, hm) not in allowed_pairs:
            return False, _SAFE_FALLBACK

    # Also validate standalone times. Times already covered by a full datetime
    # are fine; all others still must exist in the adapter allow-list.
    covered_spans = [m.span(3) for m in full_matches]
    for m in _TIME_RE.finditer(text):
        if any(m.start() >= a and m.end() <= b for a, b in covered_spans):
            continue
        if m.group(1) not in allowed_times:
            return False, _SAFE_FALLBACK

    for m in _BOOKING_RE.finditer(text):
        if m.group(0) not in allowed_ids:
            return False, _SAFE_FALLBACK

    # A positive booking claim must be grounded by an actual backend booking.
    low = text.lower()
    if ("вы записаны" in low or "запись перенесена" in low) and not allowed_ids:
        return False, _SAFE_FALLBACK

    return True, text
