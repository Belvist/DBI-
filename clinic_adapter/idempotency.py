"""Idempotency fingerprints — same (patient, kind, key) + same payload = retry.

A different payload under an already-used key is a caller bug, surfaced as
IdempotencyConflict (HTTP 409), never silently aliased to the old result.
"""
from __future__ import annotations

import hashlib

from domain.commands import CreateAppointmentCommand, RescheduleCommand


def _digest(*parts: str) -> str:
    return hashlib.sha256("|".join(parts).encode()).hexdigest()[:32]


def fingerprint_create(cmd: CreateAppointmentCommand) -> str:
    return _digest("create", cmd.slot_id)


def fingerprint_reschedule(cmd: RescheduleCommand) -> str:
    return _digest("reschedule", cmd.booking_id, cmd.new_slot_id)
