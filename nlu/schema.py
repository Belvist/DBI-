"""NLU schema — LLM returns ONLY this, never drives workflow."""
from __future__ import annotations

from datetime import datetime
from enum import Enum

from pydantic import BaseModel, Field


class Intent(str, Enum):
    BOOK = "BOOK"
    GET_STATUS = "GET_STATUS"
    RESCHEDULE = "RESCHEDULE"
    GET_SCHEDULE = "GET_SCHEDULE"
    CONFIRM = "CONFIRM"
    DENY = "DENY"
    CORRECT = "CORRECT"
    UNKNOWN = "UNKNOWN"


class NLUResult(BaseModel):
    intent: Intent
    specialty: str | None = None  # "cardiology" | "neurology" | ...
    doctor_text: str | None = None  # raw surname mention, e.g. "иванову"
    date_from: datetime | None = None
    date_to: datetime | None = None
    time_preference: str | None = None  # "morning" | "evening" | None
    exact_time: str | None = None  # "20:00" — конкретное время ("в восемь вечера")
    slot_index: int | None = None  # "первый вариант", "половина седьмого" resolved later
    raw_time_text: str | None = None
    confidence: float = Field(ge=0.0, le=1.0, default=0.0)
