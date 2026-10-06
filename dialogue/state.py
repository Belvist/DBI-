"""Dialogue state — what is true. Owned by code, not by LLM."""
from __future__ import annotations

import uuid
from datetime import datetime
from enum import Enum
from typing import Literal

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


class PendingOperation(BaseModel):
    """Durable identity of an in-flight clinic WRITE.

    Created BEFORE the adapter call; if the outcome is unknown, the next
    retry MUST reuse the same idempotency_key so the backend dedups instead
    of double-booking.
    """

    operation_id: str = Field(
        default_factory=lambda: "op-" + uuid.uuid4().hex[:12]
    )
    idempotency_key: str = Field(min_length=8, max_length=128)
    kind: Literal["create", "reschedule"]
    booking_id: str | None = None  # set for reschedule (the booking being moved)
    slot_id: str = Field(min_length=1)  # target slot
    status: Literal["unknown"] = "unknown"


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
    # Snapshot fencing: incremented on every authoritative save. The snapshot
    # store is the source of truth across replicas; this counter is its CAS.
    revision: int = 0
    pending_operation: PendingOperation | None = None
