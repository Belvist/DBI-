"""WRITE commands. READs are plain method calls on the adapter.

Every WRITE carries an idempotency_key: safe retry after TTS timeout,
double-tap, or duplicated webhook never creates two bookings.
"""
from __future__ import annotations

from pydantic import BaseModel, Field

from domain.models import PatientRef


class CreateAppointmentCommand(BaseModel):
    patient: PatientRef
    slot_id: str = Field(min_length=1)
    idempotency_key: str = Field(min_length=8, max_length=128)


class RescheduleCommand(BaseModel):
    patient: PatientRef
    booking_id: str = Field(min_length=1)
    new_slot_id: str = Field(min_length=1)
    idempotency_key: str = Field(min_length=8, max_length=128)
