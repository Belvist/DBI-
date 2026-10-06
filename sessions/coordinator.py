"""Turn coordinator — the ONLY writer path for dialogue state.

Per turn, inside the patient lock:
  load LATEST snapshot -> build session -> turn -> CAS save -> release.

The local process never trusts a cached DialogueSession: the snapshot store
(Redis in multi-replica, SQLite file otherwise) is the source of truth, and
`revision` is the fencing token. A replica that lost its lease gets
StaleState on save instead of overwriting newer state.
"""
from __future__ import annotations

from datetime import datetime

from api.booking_service import DialogueSession
from domain.models import PatientRef
from sessions.locks import patient_turn_lock


def run_turn(
    *,
    patient: PatientRef,
    text: str,
    idempotency_key: str | None,
    adapter,
    snapshots,
    redis_url: str,
    now: datetime | None = None,
) -> tuple[str, DialogueSession]:
    """Process one turn against authoritative state. Returns (speech, session).

    Raises StaleState (do not overwrite) when another replica committed first;
    callers answer with a retryable fallback.
    """
    from sessions.errors import OperationInProgress

    with patient_turn_lock(redis_url, patient.patient_id) as lease:
        loaded = snapshots.load(patient.patient_id)
        sess = DialogueSession(patient, adapter, now=now)
        if loaded is not None and loaded.patient.patient_id == patient.patient_id:
            sess.state = loaded
        pending = sess.state.pending_operation
        if (
            pending is not None
            and pending.status == "uncertain"
            and idempotency_key is not None
            and idempotency_key != pending.idempotency_key
        ):
            # Key immutable after the first WRITE attempt: rebinding would
            # destroy reconciliation. Caller must retry with the same key.
            raise OperationInProgress(
                f"uncertain {pending.kind} operation {pending.operation_id}: "
                "new idempotency key rejected"
            )
        speech = sess.turn(text, idempotency_key=idempotency_key)
        snapshots.save(sess.state)  # CAS: raises StaleState on concurrent write
        if lease.lost:
            sess.trace.log("lease_lost_but_saved")
        return speech, sess
