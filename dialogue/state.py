"""Dialogue state — what is true. Owned by code, not by LLM."""
from __future__ import annotations

from datetime import datetime
from enum import Enum

from pydantic import BaseModel, Field

from domain.models import PatientRef, SlotWithDoctor


class Phase(str, Enum):
    IDLE = "idle"
    ELICIT = "elicit"      # missing specialty/date — asking
    SEARCH = "search"      # transient, set before adapter call
    PROPOSE = "propose"    # slots offered, awaiting CONFIRM/DENY/CORRECT
    CONFIRM = "confirm"    # explicit "всё верно?" gate before COMMIT
    COMMIT = "commit"      # transient, WRITE in progress


class Flow(str, Enum):
    NONE = "none"
    BOOK = "book"
    STATUS = "status"
    RESCHEDULE = "reschedule"
    SCHEDULE = "schedule"


class DialogueState(BaseModel):
    patient: PatientRef
    phase: Phase = Phase.IDLE
    flow: Flow = Flow.NONE
    specialty: str | None = None
    doctor_text: str | None = None
    date_from: datetime | None = None
    date_to: datetime | None = None
    time_preference: str | None = None
    candidates: list[SlotWithDoctor] = Field(default_factory=list)
    selected_slot_id: str | None = None
    active_booking_id: str | None = None
    last_error: str | None = None
    turn: int = 0
