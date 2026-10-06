"""Typed domain models. No I/O, no LLM, pure validation.

Every datetime is timezone-naive Europe/Moscow wall time for the hackathon
scope. The DBI adapter (13.10) may introduce tz-aware ISO; conversion lives
in the adapter, never here.
"""
from __future__ import annotations

from datetime import datetime
from enum import Enum

from pydantic import BaseModel, Field, field_validator


class Specialty(str, Enum):
    CARDIOLOGY = "cardiology"
    NEUROLOGY = "neurology"
    THERAPY = "therapy"
    DERMATOLOGY = "dermatology"
    OPHTHALMOLOGY = "ophthalmology"


SPECIALTY_RU: dict[Specialty, str] = {
    Specialty.CARDIOLOGY: "кардиолог",
    Specialty.NEUROLOGY: "невролог",
    Specialty.THERAPY: "терапевт",
    Specialty.DERMATOLOGY: "дерматолог",
    Specialty.OPHTHALMOLOGY: "офтальмолог",
}

SPECIALTY_RU_REVERSE: dict[str, Specialty] = {}
for _spec, _ru in SPECIALTY_RU.items():
    SPECIALTY_RU_REVERSE[_ru] = _spec
    # common inflections seen in speech
    if _ru.endswith("ог"):
        SPECIALTY_RU_REVERSE[_ru + "у"] = _spec   # кардиологу
        SPECIALTY_RU_REVERSE[_ru + "а"] = _spec   # кардиолога


class PatientRef(BaseModel):
    """Who is speaking. Identification strategy plugs in after DBI TЗ (13.10).

    For now: fixed demo patient, but every adapter call already takes
    patient_id — so GET_STATUS / RESCHEDULE are architecturally real.
    """

    patient_id: str = Field(min_length=1, max_length=64)
    phone: str | None = Field(default=None, max_length=32)
    name: str | None = Field(default=None, max_length=128)


class Doctor(BaseModel):
    doctor_id: str = Field(min_length=1)
    full_name: str = Field(min_length=1)  # "Петрова Анна Сергеевна"
    short_name: str = Field(min_length=1)  # "Петрова А.С."
    specialty: Specialty


class Slot(BaseModel):
    slot_id: str = Field(min_length=1)
    doctor_id: str = Field(min_length=1)
    start: datetime
    end: datetime

    @field_validator("end")
    @classmethod
    def _end_after_start(cls, v: datetime, info) -> datetime:
        start = info.data.get("start")
        if start is not None and v <= start:
            raise ValueError("slot end must be after start")
        return v


class AppointmentStatus(str, Enum):
    ACTIVE = "active"
    CANCELLED = "cancelled"
    MOVED = "moved"  # historic record left after reschedule


class Appointment(BaseModel):
    booking_id: str = Field(min_length=1)
    patient_id: str = Field(min_length=1)
    doctor_id: str = Field(min_length=1)
    slot_id: str = Field(min_length=1)
    start: datetime
    end: datetime
    status: AppointmentStatus = AppointmentStatus.ACTIVE
    idempotency_key: str = Field(min_length=8, max_length=128)
    created_at: datetime


class SlotWithDoctor(BaseModel):
    slot: Slot
    doctor: Doctor
