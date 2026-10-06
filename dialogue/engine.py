"""Dialogue engine — the ONLY owner of workflow transitions.

LLM/NLU returns a typed candidate; THIS module decides ELICIT -> SEARCH ->
PROPOSE -> CONFIRM -> COMMIT. COMMIT always re-checks availability and runs
inside the adapter's atomic transaction (race-safe).

Design: pure-ish step(state, nlu, adapter, ctx) -> (state, speech).
No global state, no hidden fallbacks.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime

from clinic_adapter.base import ClinicAdapter
from dialogue import renderer
from dialogue.state import DialogueState, Flow, PendingOperation, Phase
from domain.adapter_errors import AdapterUnavailable
from domain.commands import CreateAppointmentCommand, RescheduleCommand
from domain.errors import SlotUnavailable
from domain.models import Specialty
from nlu.schema import Intent, NLUResult
from observability.trace import Trace


@dataclass
class TurnCtx:
    now: datetime
    trace: Trace
    idempotency_key: str = field(default_factory=lambda: "ik-" + uuid.uuid4().hex[:12])
    # True when the caller supplied idempotency_key explicitly (API contract).
    # An explicit key always wins over a pending one; otherwise the pending
    # operation's key is reused so unknown-WRITE retries dedup server-side.
    idempotency_key_explicit: bool = False


def _spec(s: str | None) -> Specialty | None:
    if not s:
        return None
    try:
        return Specialty(s)
    except ValueError:
        return None


def _doctor_matches(state: DialogueState, adapter: ClinicAdapter):
    if not state.doctor_text:
        return []
    return adapter.find_doctors(name_query=state.doctor_text)


def _search_booking(state: DialogueState, adapter: ClinicAdapter) -> list:
    doctor_id: str | None = None
    if state.doctor_text:
        docs = _doctor_matches(state, adapter)
        if len(docs) == 1:
            doctor_id = docs[0].doctor_id
    return adapter.find_slots(
        doctor_id=doctor_id,
        specialty=None if doctor_id else _spec(state.specialty),
        date_from=state.date_from,
        date_to=state.date_to,
        time_pref=state.time_preference,
        limit=3,
    )


def step(
    state: DialogueState, nlu: NLUResult, adapter: ClinicAdapter, ctx: TurnCtx
) -> tuple[DialogueState, str]:
    state.turn += 1
    ctx.trace.log(
        "user_nlu",
        intent=nlu.intent.value,
        conf=nlu.confidence,
        spec=nlu.specialty,
        doc=nlu.doctor_text,
    )
    handler = {
        Intent.BOOK: _on_book,
        Intent.GET_STATUS: _on_status,
        Intent.RESCHEDULE: _on_reschedule,
        Intent.GET_SCHEDULE: _on_schedule,
        Intent.CONFIRM: _on_confirm,
        Intent.DENY: _on_deny,
        Intent.CORRECT: _on_correct,
        Intent.UNKNOWN: _on_unknown,
    }[nlu.intent]
    try:
        return handler(state, nlu, adapter, ctx)
    except AdapterUnavailable:
        # Fail-safe: backend pain never becomes an invented slot or booking.
        # No state committed by the failed call; the turn stays retryable.
        from observability import metrics

        ctx.trace.log("adapter_unavailable")
        metrics.inc("fallbacks_total")
        state.phase = Phase.ELICIT
        return state, renderer.temporarily_unavailable()


def _on_book(state: DialogueState, nlu: NLUResult, adapter: ClinicAdapter, ctx: TurnCtx):
    if nlu.specialty:
        state.specialty = nlu.specialty
    if nlu.doctor_text:
        state.doctor_text = nlu.doctor_text
    if nlu.date_from:
        state.date_from, state.date_to = nlu.date_from, nlu.date_to
    if nlu.time_preference:
        state.time_preference = nlu.time_preference

    state.flow = Flow.BOOK

    if state.doctor_text:
        docs = _doctor_matches(state, adapter)
        if not docs:
            state.phase = Phase.ELICIT
            state.last_error = "unknown_doctor"
            return state, renderer.unknown_doctor(state.doctor_text)
        if len(docs) > 1:
            state.phase = Phase.ELICIT
            return state, renderer.ambiguous_doctor(docs)
        # Doctor is a backend fact. Persist the backend specialty so later
        # searches cannot accidentally drift to another specialist.
        state.specialty = docs[0].specialty.value

    if not state.specialty and not state.doctor_text:
        state.phase = Phase.ELICIT
        return state, renderer.ask_specialty()
    if not state.date_from:
        state.phase = Phase.ELICIT
        return state, renderer.ask_day()

    slots = _search_booking(state, adapter)
    ctx.trace.log("search", found=len(slots), doctor=state.doctor_text)
    if not slots:
        state.phase = Phase.ELICIT
        state.last_error = "no_slots"
        return state, renderer.no_slots()

    state.candidates = slots
    state.phase = Phase.PROPOSE
    return state, renderer.propose_slots(slots)


def _on_status(state: DialogueState, nlu: NLUResult, adapter: ClinicAdapter, ctx: TurnCtx):
    appts = adapter.get_appointments(state.patient)
    doctors = {d.doctor_id: d for d in adapter.find_doctors()}
    items = [(a, doctors.get(a.doctor_id)) for a in appts]
    items = [(a, d) for a, d in items if d is not None]

    if nlu.doctor_text:
        doctor_ids = {
            d.doctor_id for d in adapter.find_doctors(name_query=nlu.doctor_text)
        }
        items = [(a, d) for a, d in items if a.doctor_id in doctor_ids]
    if nlu.specialty:
        wanted = _spec(nlu.specialty)
        if wanted:
            items = [(a, d) for a, d in items if d.specialty == wanted]

    state.flow, state.phase = Flow.STATUS, Phase.IDLE
    if len(items) == 1:
        state.active_booking_id = items[0][0].booking_id
    ctx.trace.log("status", count=len(items))
    return state, renderer.status_list(items)


def _on_schedule(state: DialogueState, nlu: NLUResult, adapter: ClinicAdapter, ctx: TurnCtx):
    doctor_id: str | None = None
    if nlu.doctor_text:
        docs = adapter.find_doctors(name_query=nlu.doctor_text)
        if not docs:
            return state, renderer.unknown_doctor(nlu.doctor_text)
        if len(docs) > 1:
            return state, renderer.ambiguous_doctor(docs)
        doctor_id = docs[0].doctor_id

    spec = _spec(nlu.specialty or state.specialty)
    slots = adapter.get_schedule(
        doctor_id=doctor_id,
        date_from=nlu.date_from,
        date_to=nlu.date_to,
    )
    if nlu.time_preference == "morning":
        slots = [x for x in slots if x.slot.start.hour < 13]
    elif nlu.time_preference == "evening":
        slots = [x for x in slots if x.slot.start.hour >= 17]
    if spec and not doctor_id:
        slots = [x for x in slots if x.doctor.specialty == spec]

    state.flow = Flow.SCHEDULE
    if not slots:
        return state, renderer.no_slots()
    state.candidates = slots[:5]
    return state, renderer.propose_slots(state.candidates)


def _on_reschedule(state: DialogueState, nlu: NLUResult, adapter: ClinicAdapter, ctx: TurnCtx):
    appts = adapter.get_appointments(state.patient)
    state.flow = Flow.RESCHEDULE
    if not appts:
        return state, "У вас нет активных записей для переноса. Оформить новую?"

    doctors = {d.doctor_id: d for d in adapter.find_doctors()}
    targets = list(appts)

    # If the user names the current doctor, use it to identify which booking
    # is being moved. Never silently pick the first of several bookings.
    if nlu.doctor_text:
        ids = {d.doctor_id for d in adapter.find_doctors(name_query=nlu.doctor_text)}
        targets = [a for a in targets if a.doctor_id in ids]
    elif nlu.specialty:
        wanted = _spec(nlu.specialty)
        if wanted:
            targets = [
                a for a in targets
                if a.doctor_id in doctors and doctors[a.doctor_id].specialty == wanted
            ]

    if not targets:
        return state, "Не нашла подходящую активную запись. Уточните врача или дату записи."
    if len(targets) > 1:
        state.phase = Phase.ELICIT
        return state, renderer.choose_booking(
            [(a, doctors[a.doctor_id]) for a in targets if a.doctor_id in doctors]
        )

    target = targets[0]
    state.active_booking_id = target.booking_id

    if nlu.date_from:
        state.date_from, state.date_to = nlu.date_from, nlu.date_to
    if nlu.time_preference:
        state.time_preference = nlu.time_preference

    if not state.date_from:
        state.phase = Phase.ELICIT
        return state, "На какой день перенести? Назовите дату или день недели."

    # A reschedule keeps the same doctor unless the user explicitly named
    # another known doctor in this turn.
    desired_doctor_id = target.doctor_id
    if nlu.doctor_text:
        matches = adapter.find_doctors(name_query=nlu.doctor_text)
        if not matches:
            return state, renderer.unknown_doctor(nlu.doctor_text)
        if len(matches) > 1:
            return state, renderer.ambiguous_doctor(matches)
        desired_doctor_id = matches[0].doctor_id

    slots = adapter.find_slots(
        doctor_id=desired_doctor_id,
        date_from=state.date_from,
        date_to=state.date_to,
        time_pref=state.time_preference,
        limit=3,
    )
    if not slots:
        state.phase = Phase.ELICIT
        return state, "На эти даты ничего свободного нет. Попробуем другие дни?"

    state.candidates = slots
    state.phase = Phase.PROPOSE
    ctx.trace.log(
        "reschedule_search",
        booking=target.booking_id,
        doctor=desired_doctor_id,
        found=len(slots),
    )
    return state, renderer.propose_slots(slots)


def _pick_candidate(state: DialogueState, nlu: NLUResult) -> int:
    if nlu.slot_index is not None and 0 <= nlu.slot_index < len(state.candidates):
        return nlu.slot_index

    if nlu.raw_time_text:
        import re

        t = nlu.raw_time_text.lower()
        m = re.search(r"(\d{1,2})[:.](\d{2})", t)
        hh_mm = None
        if m:
            hh_mm = (int(m.group(1)), int(m.group(2)))
        elif "половина седьмого" in t or ("половин" in t and "седьм" in t):
            hh_mm = (18, 30)
        if hh_mm:
            for i, c in enumerate(state.candidates):
                if c.slot.start.hour == hh_mm[0] and c.slot.start.minute == hh_mm[1]:
                    return i
    return 0


def _uncertain_pending(state: DialogueState):
    pending = state.pending_operation
    if pending is not None and pending.status == "uncertain":
        return pending
    return None


def _on_confirm(state: DialogueState, nlu: NLUResult, adapter: ClinicAdapter, ctx: TurnCtx):
    if state.phase == Phase.PROPOSE and state.candidates:
        idx = _pick_candidate(state, nlu)
        sel = state.candidates[idx]
        uncertain = _uncertain_pending(state)
        if uncertain is not None and sel.slot.slot_id != uncertain.slot_id:
            # An uncertain WRITE is unresolved: starting a new conflicting
            # operation now could double-book. Reconcile first.
            ctx.trace.log("pending_blocked", slot=sel.slot.slot_id)
            return state, renderer.pending_blocked()
        state.selected_slot_id = sel.slot.slot_id
        state.phase = Phase.CONFIRM
        # Create pending operation with stable idempotency_key for this COMMIT
        if state.pending_operation is None or state.pending_operation.slot_id != state.selected_slot_id:
            state.pending_operation = PendingOperation(
                idempotency_key=ctx.idempotency_key,
                kind="reschedule" if state.flow == Flow.RESCHEDULE else "create",
                slot_id=state.selected_slot_id,
                booking_id=state.active_booking_id if state.flow == Flow.RESCHEDULE else None,
            )
        return state, renderer.confirm_slot(sel)

    if state.phase == Phase.CONFIRM and state.selected_slot_id:
        uncertain = _uncertain_pending(state)
        if uncertain is not None and state.selected_slot_id != uncertain.slot_id:
            return state, renderer.pending_blocked()
        return _commit(state, adapter, ctx)

    return _on_unknown(state, nlu, adapter, ctx)


def _commit(state: DialogueState, adapter: ClinicAdapter, ctx: TurnCtx):
    assert state.selected_slot_id
    state.phase = Phase.COMMIT
    # Key precedence: an explicitly supplied caller key always wins (and
    # rebinds the pending operation to it); otherwise a pending operation's
    # key is reused so unknown-WRITE retries dedup server-side; only with
    # neither do we fall back to this turn's generated key.
    pending = state.pending_operation
    if ctx.idempotency_key_explicit and (
        pending is None or pending.status == "prepared"
    ):
        # Key rebind allowed only before the first WRITE attempt.
        key = ctx.idempotency_key
        state.pending_operation = PendingOperation(
            idempotency_key=key,
            kind="reschedule" if state.flow == Flow.RESCHEDULE else "create",
            booking_id=state.active_booking_id,
            slot_id=state.selected_slot_id,
        )
    elif pending is not None:
        # Uncertain (or prepared) operation: the key is immutable from here.
        key = pending.idempotency_key
    else:
        key = ctx.idempotency_key
    try:
        if state.flow == Flow.RESCHEDULE and state.active_booking_id:
            appt = adapter.reschedule_appointment(
                RescheduleCommand(
                    patient=state.patient,
                    booking_id=state.active_booking_id,
                    new_slot_id=state.selected_slot_id,
                    idempotency_key=key,
                )
            )
            doctors = {d.doctor_id: d for d in adapter.find_doctors()}
            d = doctors[appt.doctor_id]
            ctx.trace.log("booking_moved", booking_id=appt.booking_id)
            state.phase, state.flow = Phase.IDLE, Flow.NONE
            state.candidates, state.selected_slot_id = [], None
            state.active_booking_id = appt.booking_id
            state.pending_operation = None
            return state, renderer.moved(appt, d)

        appt = adapter.create_appointment(
            CreateAppointmentCommand(
                patient=state.patient,
                slot_id=state.selected_slot_id,
                idempotency_key=key,
            )
        )
        doctors = {d.doctor_id: d for d in adapter.find_doctors()}
        d = doctors[appt.doctor_id]
        ctx.trace.log(
            "booking_created",
            booking_id=appt.booking_id,
            slot=state.selected_slot_id,
        )
        state.phase, state.flow = Phase.IDLE, Flow.NONE
        state.candidates, state.selected_slot_id = [], None
        state.active_booking_id = appt.booking_id
        state.pending_operation = None
        return state, renderer.booked(appt, d)

    except AdapterUnavailable:
        # WRITE outcome unknown: the booking may have committed server-side
        # while the response was lost. Persist the operation identity so the
        # next retry reuses the SAME idempotency_key and the backend dedups.
        # Keep the selected slot as well: if the retry hits a taken slot,
        # SlotUnavailable recovers via re-search instead of double-booking.
        from observability import metrics

        ctx.trace.log("commit_outcome_unknown", slot=state.selected_slot_id)
        metrics.inc("fallbacks_total")
        state.pending_operation = PendingOperation(
            idempotency_key=key,
            kind="reschedule" if state.flow == Flow.RESCHEDULE else "create",
            booking_id=state.active_booking_id,
            slot_id=state.selected_slot_id,
            status="uncertain",
        )
        state.phase = Phase.CONFIRM
        return state, renderer.outcome_unknown()

    except SlotUnavailable:
        ctx.trace.log("race_taken", slot=state.selected_slot_id)
        slots = _search_booking(state, adapter)
        state.candidates = [
            s for s in slots if s.slot.slot_id != state.selected_slot_id
        ][:3]
        state.selected_slot_id = None
        state.phase = Phase.PROPOSE if state.candidates else Phase.ELICIT
        if not state.candidates:
            return state, renderer.no_slots()
        return state, renderer.race_taken() + " " + renderer.propose_slots(state.candidates)


def _on_deny(state: DialogueState, nlu: NLUResult, adapter: ClinicAdapter, ctx: TurnCtx):
    if _uncertain_pending(state) is not None:
        # "Нет" does not cancel an uncertain WRITE: the booking may exist.
        # Keep everything; guide the user to reconcile first.
        ctx.trace.log("deny_during_uncertain")
        return state, renderer.pending_unresolved()
    if state.phase in (Phase.PROPOSE, Phase.CONFIRM):
        state.candidates = []
        state.selected_slot_id = None
        state.phase = Phase.ELICIT
        return state, "Хорошо, эти варианты убираю. Какой день тогда удобен?"
    return state, renderer.unknown()


def _on_correct(state: DialogueState, nlu: NLUResult, adapter: ClinicAdapter, ctx: TurnCtx):
    if nlu.date_from:
        state.date_from, state.date_to = nlu.date_from, nlu.date_to
    if nlu.time_preference:
        state.time_preference = nlu.time_preference

    if state.flow in (Flow.BOOK, Flow.RESCHEDULE, Flow.NONE):
        state.flow = Flow.BOOK if state.flow == Flow.NONE else state.flow
        slots = _search_booking(state, adapter) if state.specialty or state.date_from else []
        if slots:
            state.candidates = slots
            state.phase = Phase.PROPOSE
            return state, renderer.propose_slots(slots)
        state.phase = Phase.ELICIT
        return state, renderer.ask_day()

    return _on_unknown(state, nlu, adapter, ctx)


def _on_unknown(state: DialogueState, nlu: NLUResult, adapter: ClinicAdapter, ctx: TurnCtx):
    if state.phase == Phase.PROPOSE and state.candidates:
        return _on_confirm(state, nlu, adapter, ctx)

    if state.phase == Phase.CONFIRM and state.selected_slot_id:
        return _commit(state, adapter, ctx)

    if (
        state.flow == Flow.BOOK
        and state.phase == Phase.ELICIT
        and (nlu.date_from or nlu.time_preference or nlu.specialty or nlu.doctor_text)
    ):
        return _on_book(state, nlu, adapter, ctx)

    return state, renderer.unknown()
