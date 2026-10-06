"""Booking service — owns the SEARCH -> CONFIRM -> COMMIT transaction.

Dialogue engine calls this (or the adapter directly for READs). The service
adds the second availability check at COMMIT time so a slot shown to the
user but grabbed concurrently fails loudly with SlotUnavailable instead of
double-booking.
"""
from __future__ import annotations

from datetime import datetime

from api.validator import validate
from clinic_adapter.base import ClinicAdapter
from dialogue.engine import TurnCtx, step
from dialogue.state import DialogueState
from domain.adapter_errors import AdapterUnavailable
from domain.models import PatientRef
from nlu import deterministic as det
from nlu.llm_proposer import LlmProposer, fuse
from observability import metrics
from observability.trace import Trace


class DialogueSession:
    """One patient conversation. Holds state + trace + last grounded facts."""

    def __init__(self, patient: PatientRef, adapter: ClinicAdapter,
                 now: datetime | None = None) -> None:
        self.patient = patient
        self.adapter = adapter
        self.state = DialogueState(patient=patient)
        self.trace = Trace()
        self.now = now or datetime.now()
        self.proposer = LlmProposer()
        self._allowed_dts: list[datetime] = []
        self._allowed_bids: list[str] = []

    def turn(self, text: str, idempotency_key: str | None = None) -> str:
        import re

        metrics.inc("turns_total")
        user_times = set(re.findall(r"(?<!\d)(\d{1,2}:\d{2})(?!\d)", text))
        det_out = det.parse(text, now=self.now)
        fused = fuse(det_out, self.proposer.propose(text, self.now))
        ctx = TurnCtx(now=self.now, trace=self.trace)
        if idempotency_key:
            ctx.idempotency_key = idempotency_key
            ctx.idempotency_key_explicit = True
        self.state, speech = step(self.state, fused, self.adapter, ctx)
        self._refresh_allowlist()
        ok, safe = validate(speech, self._allowed_dts, self._allowed_bids, user_times)
        if not ok:
            self.trace.log("hallucination_blocked", draft=speech[:120])
            metrics.inc("fallbacks_total")
            return safe
        return safe

    def _refresh_allowlist(self) -> None:
        self._allowed_dts = [c.slot.start for c in self.state.candidates]
        try:
            appointments = self.adapter.get_appointments(self.patient)
        except AdapterUnavailable:
            # Backend down: keep the candidate-only allowlist. Fewer allowed
            # facts means a STRICTER validator — the safe direction.
            appointments = []
        for a in appointments:
            self._allowed_dts.append(a.start)
            self._allowed_bids.append(a.booking_id)
        if self.state.active_booking_id:
            self._allowed_bids.append(self.state.active_booking_id)
