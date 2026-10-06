"""Shared clinic seed + datetime codec.

Both adapters (SQLite mock, Postgres) store datetimes as `YYYY-MM-DDTHH:MM`
TEXT so filtering/sorting semantics are identical. Timezone conversion, if
DBI requires it on 13.10, lives in `dbi.py`, never here.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from domain.models import Specialty

_FMT = "%Y-%m-%dT%H:%M"


def dt(s: str) -> datetime:
    return datetime.strptime(s, _FMT)


def ts(d: datetime) -> str:
    return d.strftime(_FMT)


SEED_DOCTORS: list[tuple[str, str, str, Specialty]] = [
    ("d_petrova", "Петрова Анна Сергеевна", "Петрова А.С.", Specialty.NEUROLOGY),
    ("d_ivanov", "Иванов Игорь Петрович", "Иванов И.П.", Specialty.CARDIOLOGY),
    ("d_sidorova", "Сидорова Мария Ивановна", "Сидорова М.И.", Specialty.THERAPY),
    ("d_kozlov", "Козлов Дмитрий Андреевич", "Козлов Д.А.", Specialty.CARDIOLOGY),
    ("d_smirnova", "Смирнова Ольга Викторовна", "Смирнова О.В.", Specialty.THERAPY),
    ("d_fedorov", "Федоров Сергей Николаевич", "Федоров С.Н.", Specialty.NEUROLOGY),
]

SEED_BASE = datetime(2026, 10, 12, 9, 0)

SLOT_GRID: list[tuple[int, int]] = [
    (9, 0), (9, 45), (10, 30), (11, 15), (18, 0), (18, 30), (19, 15)
]


def seed_slots(base_monday: datetime = SEED_BASE) -> list[tuple[str, str, str, str]]:
    """Return (slot_id, doctor_id, start, end) rows for two work-weeks."""
    rows = []
    for day_off in range(14):
        day = base_monday + timedelta(days=day_off)
        if day.weekday() >= 5:
            continue
        for hour, minute in SLOT_GRID:
            start = day.replace(hour=hour, minute=minute, second=0, microsecond=0)
            end = start + timedelta(minutes=30)
            for doctor_id, _, _, _ in SEED_DOCTORS:
                sid = f"s_{doctor_id}_{start.strftime('%m%d_%H%M')}"
                rows.append((sid, doctor_id, ts(start), ts(end)))
    return rows
