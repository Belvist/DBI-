"""DBI production adapter — plugs in 13.10. Same contract, zero dialogue changes."""
from __future__ import annotations

from datetime import datetime

from clinic_adapter.base import ClinicAdapter
from domain.commands import CreateAppointmentCommand, RescheduleCommand
from domain.errors import NotConfigured
from domain.models import Appointment, Doctor, PatientRef, SlotWithDoctor, Specialty


class DbiApiClinic(ClinicAdapter):
    """Replace internals on 13.10 with real DBI JSON/API/CSV mapping."""

    def __init__(self, base_url: str = "", token: str = "") -> None:
        self.base_url = base_url
        self.token = token

    def _missing(self) -> NotConfigured:
        return NotConfigured("DBI credentials/data not provided yet (expected 13.10)")

    def find_doctors(
        self, specialty: Specialty | None = None, name_query: str | None = None
    ) -> list[Doctor]:
        raise self._missing()

    def find_slots(
        self,
        doctor_id: str | None = None,
        specialty: Specialty | None = None,
        date_from: datetime | None = None,
        date_to: datetime | None = None,
        time_pref: str | None = None,
        limit: int = 5,
    ) -> list[SlotWithDoctor]:
        raise self._missing()

    def get_appointments(self, patient: PatientRef) -> list[Appointment]:
        raise self._missing()

    def get_schedule(
        self,
        doctor_id: str | None = None,
        date_from: datetime | None = None,
        date_to: datetime | None = None,
    ) -> list[SlotWithDoctor]:
        raise self._missing()

    def create_appointment(self, cmd: CreateAppointmentCommand) -> Appointment:
        raise self._missing()

    def reschedule_appointment(self, cmd: RescheduleCommand) -> Appointment:
        raise self._missing()
