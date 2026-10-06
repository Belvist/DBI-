"""Resilient clinic adapter: same READ/WRITE contract, guarded calls.

Every method runs through breaker + transient retry. Business outcomes
(SlotUnavailable, AppointmentNotFound, ...) pass through untouched so the
dialogue keeps its precise recovery behavior (re-search, not retry).
"""
from __future__ import annotations

from datetime import datetime

from clinic_adapter.base import ClinicAdapter
from domain.adapter_errors import AdapterUnavailable  # noqa: F401 (re-exported)
from domain.commands import CreateAppointmentCommand, RescheduleCommand
from domain.errors import SlotUnavailable
from domain.models import Appointment, Doctor, PatientRef, SlotWithDoctor, Specialty
from observability import metrics
from resilience.guarded import CircuitBreaker, call_guarded


class ResilientAdapter:
    def __init__(self, base: ClinicAdapter, breaker: CircuitBreaker | None = None) -> None:
        self._base = base
        self._breaker = breaker or CircuitBreaker()

    @property
    def breaker(self) -> CircuitBreaker:
        return self._breaker

    # ---- READ ----
    def find_doctors(
        self,
        specialty: Specialty | None = None,
        name_query: str | None = None,
    ) -> list[Doctor]:
        metrics.inc("adapter_calls_total")
        return call_guarded(self._breaker, self._base.find_doctors, specialty, name_query)

    def find_slots(
        self,
        doctor_id: str | None = None,
        specialty: Specialty | None = None,
        date_from: datetime | None = None,
        date_to: datetime | None = None,
        time_pref: str | None = None,
        limit: int = 5,
    ) -> list[SlotWithDoctor]:
        metrics.inc("adapter_calls_total")
        return call_guarded(
            self._breaker, self._base.find_slots,
            doctor_id, specialty, date_from, date_to, time_pref, limit,
        )

    def get_appointments(self, patient: PatientRef) -> list[Appointment]:
        metrics.inc("adapter_calls_total")
        return call_guarded(self._breaker, self._base.get_appointments, patient)

    def get_schedule(
        self,
        doctor_id: str | None = None,
        date_from: datetime | None = None,
        date_to: datetime | None = None,
    ) -> list[SlotWithDoctor]:
        metrics.inc("adapter_calls_total")
        return call_guarded(
            self._breaker, self._base.get_schedule, doctor_id, date_from, date_to
        )

    # ---- WRITE ----
    def create_appointment(self, cmd: CreateAppointmentCommand) -> Appointment:
        metrics.inc("adapter_calls_total")
        try:
            appt = call_guarded(self._breaker, self._base.create_appointment, cmd)
        except SlotUnavailable:
            metrics.inc("races_total")
            raise
        metrics.inc("bookings_created_total")
        return appt

    def reschedule_appointment(self, cmd: RescheduleCommand) -> Appointment:
        metrics.inc("adapter_calls_total")
        try:
            appt = call_guarded(self._breaker, self._base.reschedule_appointment, cmd)
        except SlotUnavailable:
            metrics.inc("races_total")
            raise
        metrics.inc("bookings_moved_total")
        return appt
