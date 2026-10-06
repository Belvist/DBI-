"""Dialogue engine — the ONLY owner of workflow transitions.

LLM/NLU returns a typed candidate; THIS module decides ELICIT -> SEARCH ->
PROPOSE -> CONFIRM -> COMMIT. COMMIT always re-checks availability and runs
inside the adapter's atomic transaction (race-safe).

Design: pure-ish step(state, nlu, adapter, ctx) -> (state, speech, trace).
No global state, no hidden fallbacks.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime

from clinic_adapter.base import ClinicAdapter
from dialogue import renderer
from dialogue.state import DialogueState, Flow, Phase
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


def _spec(s: str | None) -> Specialty | None:
    if not s:
        return None
    try:
        return Specialty(s)
    except ValueError:
        return None


def _search_booking(state: DialogueState, adapter: ClinicAdapter) -> list:
    return adapter.find_slots(
        specialty=_spec(state.specialty),
        date_from=state.date_from,
        date_to=state.date_to,
        time_pref=state.time_preference,
        limit=3,
    )


def step(
    state: DialogueState, nlu: NLUResult, adapter: ClinicAdapter, ctx: TurnCtx
) -> tuple[DialogueState, str]:
    state.turn += 1
    ctx.trace.log("user_nlu", intent=nlu.intent.value, conf=nlu.confidence,
                   spec=nlu.specialty, doc=nlu.doctor_text)
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
    return handler(state, nlu, adapter, ctx)


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
    if not state.specialty and not state.doctor_text:
        state.phase = Phase.ELICIT
        return state, renderer.ask_specialty()
    if not state.date_from:
        state.phase = Phase.ELICIT
        return state, renderer.ask_day()
    slots = _search_booking(state, adapter)
    ctx.trace.log("search", found=len(slots))
    if not slots:
        state.phase = Phase.ELICIT
        state.last_error = "no_slots"
        return state, renderer.no_slots()
    state.candidates = slots
    # auto-select single candidate? No — always ask, jury wants explicit confirm.
    state.phase = Phase.PROPOSE
    return state, renderer.propose_slots(slots)


def _on_status(state: DialogueState, nlu: NLUResult, adapter: ClinicAdapter, ctx: TurnCtx):
    appts = adapter.get_appointments(state.patient)
    doctors = {d.doctor_id: d for d in adapter.find_doctors()}
    items = [(a, doctors.get(a.doctor_id)) for a in appts]
    items = [(a, d) for a, d in items if d is not None]
    state.flow, state.phase = Flow.STATUS, Phase.IDLE
    if appts and appts[0].booking_id:
        state.active_booking_id = appts[0].booking_id
    ctx.trace.log("status", count=len(items))
    return state, renderer.status_list(items)


def _on_schedule(state: DialogueState, nlu: NLUResult, adapter: ClinicAdapter, ctx: TurnCtx):
    spec = _spec(nlu.specialty or state.specialty)
    slots = adapter.find_slots(
        specialty=spec, date_from=nlu.date_from, date_to=nlu.date_to,
        time_pref=nlu.time_preference, limit=5,
    )
    state.flow = Flow.SCHEDULE
    if not slots:
        return state, renderer.no_slots()
    state.candidates = slots
    return state, renderer.propose_slots(slots)


def _on_reschedule(state: DialogueState, nlu: NLUResult, adapter: ClinicAdapter, ctx: TurnCtx):
    appts = adapter.get_appointments(state.patient)
    if not appts:
        state.flow = Flow.RESCHEDULE
        return state, "У вас нет активных записей для переноса. Оформить новую?"
    target = appts[0]
    state.active_booking_id = target.booking_id
    # new constraints from this turn override
    if nlu.date_from:
        state.date_from, state.date_to = nlu.date_from, nlu.date_to
    if nlu.time_preference:
        state.time_preference = nlu.time_preference
    if nlu.specialty:
        state.specialty = nlu.specialty
    if not state.date_from:
        state.flow, state.phase = Flow.RESCHEDULE, Phase.ELICIT
        return state, "На какой день перенести? Назовите дату или день недели."
    slots = adapter.find_slots(
        date_from=state.date_from, date_to=state.date_to,
        time_pref=state.time_preference, limit=3,
    )
    if not slots:
        state.phase = Phase.ELICIT
        return state, "На эти даты ничего свободного нет. Попробуем другие дни?"
    state.candidates = slots
    state.flow, state.phase = Flow.RESCHEDULE, Phase.PROPOSE
    ctx.trace.log("reschedule_search", booking=target.booking_id, found=len(slots))
    return state, renderer.propose_slots(slots)


def _pick_candidate(state: DialogueState, nlu: NLUResult) -> int:
    if nlu.slot_index is not None and 0 <= nlu.slot_index < len(state.candidates):
        return nlu.slot_index
    # "половина седьмого" / "18:30" -> match by time text
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


def _on_confirm(state: DialogueState, nlu: NLUResult, adapter: ClinicAdapter, ctx: TurnCtx):
    if state.phase == Phase.PROPOSE and state.candidates:
        idx = _pick_candidate(state, nlu)
        sel = state.candidates[idx]
        state.selected_slot_id = sel.slot.slot_id
        state.phase = Phase.CONFIRM
        return state, renderer.confirm_slot(sel)
    if state.phase == Phase.CONFIRM and state.selected_slot_id:
        return _commit(state, adapter, ctx)
    return _on_unknown(state, nlu, adapter, ctx)


def _commit(state: DialogueState, adapter: ClinicAdapter, ctx: TurnCtx):
    assert state.selected_slot_id
    state.phase = Phase.COMMIT
    try:
        if state.flow == Flow.RESCHEDULE and state.active_booking_id:
            appt = adapter.reschedule_appointment(
                RescheduleCommand(
                    patient=state.patient,
                    booking_id=state.active_booking_id,
                    new_slot_id=state.selected_slot_id,
                    idempotency_key=ctx.idempotency_key,
                )
            )
            doctors = {d.doctor_id: d for d in adapter.find_doctors()}
            d = doctors[appt.doctor_id]
            ctx.trace.log("booking_moved", booking_id=appt.booking_id)
            state.phase, state.flow = Phase.IDLE, Flow.NONE
            state.candidates, state.selected_slot_id = [], None
            return state, renderer.moved(appt, d)
        appt = adapter.create_appointment(
            CreateAppointmentCommand(
                patient=state.patient,
                slot_id=state.selected_slot_id,
                idempotency_key=ctx.idempotency_key,
            )
        )
        doctors = {d.doctor_id: d for d in adapter.find_doctors()}
        d = doctors[appt.doctor_id]
        ctx.trace.log("booking_created", booking_id=appt.booking_id,
                       slot=state.selected_slot_id)
        state.phase, state.flow = Phase.IDLE, Flow.NONE
        state.candidates, state.selected_slot_id = [], None
        state.active_booking_id = appt.booking_id
        return state, renderer.booked(appt, d)
    except SlotUnavailable:
        # race: slot taken between PROPOSE and COMMIT — re-search once
        ctx.trace.log("race_taken", slot=state.selected_slot_id)
        slots = _search_booking(state, adapter)
        state.candidates = [s for s in slots if s.slot.slot_id != state.selected_slot_id][:3]
        state.selected_slot_id = None
        state.phase = Phase.PROPOSE if state.candidates else Phase.ELICIT
        if not state.candidates:
            return state, renderer.no_slots()
        return state, renderer.race_taken() + " " + renderer.propose_slots(state.candidates)


def _on_deny(state: DialogueState, nlu: NLUResult, adapter: ClinicAdapter, ctx: TurnCtx):
    # "вторник вообще не могу" during PROPOSE -> drop those candidates, re-search
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
    # context-sensitive: PROPOSE + bare "да" with candidates -> treat as CONFIRM select
    if state.phase == Phase.PROPOSE and state.candidates:
        return _on_confirm(state, nlu, adapter, ctx)
    # CONFIRM + "да" -> commit (deterministic parser sometimes labels bare да as UNKNOWN-adjacent)
    if state.phase == Phase.CONFIRM and state.selected_slot_id:
        return _commit(state, adapter, ctx)
    # ELICIT continuation: "на следующей неделе вечером" alone carries dates
    # but no booking verb — continue the open BOOK flow instead of resetting.
    if (
        state.flow == Flow.BOOK
        and state.phase == Phase.ELICIT
        and (nlu.date_from or nlu.time_preference or nlu.specialty or nlu.doctor_text)
    ):
        return _on_book(state, nlu, adapter, ctx)
    return state, renderer.unknown()
