"""Clinic adapter contract: READ vs WRITE split.

READ  — pure queries, safe to call on every turn.
WRITE — state-changing, require idempotency_key, atomic, race-checked.

The 13.10 DBI data/API plugs in as a new implementation of this Protocol.
Dialogue, NLU and API never touch SQLite/HTTP directly.
"""
from __future__ import annotations

from datetime import datetime
from typing import Protocol

from domain.commands import CreateAppointmentCommand, RescheduleCommand
from domain.models import Appointment, Doctor, PatientRef, SlotWithDoctor, Specialty


class ClinicAdapter(Protocol):
    # ---- READ ----
    def find_doctors(
        self,
        specialty: Specialty | None = None,
        name_query: str | None = None,
    ) -> list[Doctor]: ...

    def find_slots(
        self,
        doctor_id: str | None = None,
        specialty: Specialty | None = None,
        date_from: datetime | None = None,
        date_to: datetime | None = None,
        time_pref: str | None = None,  # "morning" | "evening" | None
        limit: int = 5,
    ) -> list[SlotWithDoctor]: ...

    def get_appointments(self, patient: PatientRef) -> list[Appointment]: ...

    def get_schedule(
        self,
        doctor_id: str | None = None,
        date_from: datetime | None = None,
        date_to: datetime | None = None,
    ) -> list[SlotWithDoctor]: ...

    # ---- WRITE (atomic + idempotent) ----
    def create_appointment(self, cmd: CreateAppointmentCommand) -> Appointment: ...
    def reschedule_appointment(self, cmd: RescheduleCommand) -> Appointment: ...

    # ---- HEALTH (no breaker, no metrics, no state change) ----
    def ping(self) -> None: ...
