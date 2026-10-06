"""Grounded renderer — speaks ONLY backend-returned facts."""
from __future__ import annotations

from datetime import datetime

from domain.models import Appointment, Doctor, SlotWithDoctor

_RU_MONTHS = [
    "", "января", "февраля", "марта", "апреля", "мая", "июня",
    "июля", "августа", "сентября", "октября", "ноября", "декабря",
]
_RU_WEEKDAY = [
    "понедельник", "вторник", "среда", "четверг",
    "пятница", "суббота", "воскресенье",
]


def fmt_dt(dt: datetime) -> str:
    return f"{dt.day} {_RU_MONTHS[dt.month]} в {dt.strftime('%H:%M')}"


def fmt_slot(s: SlotWithDoctor) -> str:
    wd = _RU_WEEKDAY[s.slot.start.weekday()]
    return f"{wd}, {fmt_dt(s.slot.start)} — {s.doctor.short_name}"


def ask_specialty() -> str:
    return (
        "Здравствуйте! К какому специалисту хотите записаться? "
        "Например: кардиолог, невролог, терапевт."
    )


def ask_day() -> str:
    return (
        "Подскажите, какой день вам удобен? Можно сказать "
        "«на следующей неделе вечером» или назвать дату."
    )


def propose_slots(slots: list[SlotWithDoctor]) -> str:
    lines = [f"{i + 1}. {fmt_slot(s)}" for i, s in enumerate(slots[:3])]
    return "Нашла свободное время:\n" + "\n".join(lines) + "\nКакой вариант удобнее?"


def confirm_slot(s: SlotWithDoctor) -> str:
    return (
        f"Вы выбрали {s.doctor.full_name}, {fmt_dt(s.slot.start)}. "
        "Подтвердить запись? Скажите «да»."
    )


def booked(appt: Appointment, doctor: Doctor) -> str:
    return (
        f"Готово! Вы записаны к {doctor.full_name} на {fmt_dt(appt.start)}. "
        f"Номер записи {appt.booking_id}."
    )


def status_list(items: list[tuple[Appointment, Doctor]]) -> str:
    if not items:
        return "У вас нет подходящих активных записей. Хотите записаться?"
    parts = [
        f"{fmt_dt(a.start)} — {d.full_name} (№{a.booking_id})"
        for a, d in items
    ]
    return "Ваши записи:\n" + "\n".join(parts)


def moved(appt: Appointment, doctor: Doctor) -> str:
    return (
        f"Готово. Запись перенесена на {fmt_dt(appt.start)} к {doctor.full_name}. "
        f"Новый номер {appt.booking_id}."
    )


def unknown_doctor(query: str) -> str:
    return (
        f"Не нашла врача по запросу «{query}». "
        "Назовите фамилию ещё раз или укажите специальность."
    )


def ambiguous_doctor(doctors: list[Doctor]) -> str:
    names = ", ".join(d.full_name for d in doctors[:3])
    return f"Нашла несколько врачей: {names}. Уточните, кого вы имеете в виду."


def choose_booking(items: list[tuple[Appointment, Doctor]]) -> str:
    if not items:
        return "Не нашла активную запись для переноса."
    lines = [
        f"{i + 1}. {d.full_name}, {fmt_dt(a.start)}, №{a.booking_id}"
        for i, (a, d) in enumerate(items[:3])
    ]
    return (
        "У вас несколько активных записей. Уточните, какую переносим:\n"
        + "\n".join(lines)
    )


def race_taken() -> str:
    return "Это время только что стало недоступно. Подберу другой вариант."


def no_slots() -> str:
    return (
        "К сожалению, на эти даты свободного времени нет. "
        "Подсказать ближайшие другие дни?"
    )


def unknown() -> str:
    return "Не совсем поняла. Вы хотите записаться, узнать свою запись или перенести её?"
