"""Typed domain errors — every failure is explicit, never a silent None."""
from __future__ import annotations


class ClinicError(Exception):
    code: str = "clinic_error"


class SlotNotFound(ClinicError):
    code = "slot_not_found"


class SlotUnavailable(ClinicError):
    """Slot was free at SEARCH time but taken at COMMIT time (race)."""

    code = "slot_unavailable"


class DoctorNotFound(ClinicError):
    code = "doctor_not_found"


class AppointmentNotFound(ClinicError):
    code = "appointment_not_found"


class NoAlternatives(ClinicError):
    code = "no_alternatives"


class AmbiguousRequest(ClinicError):
    code = "ambiguous_request"


class NotConfigured(ClinicError):
    code = "not_configured"


class HallucinatedResponse(ClinicError):
    """Renderer tried to speak a slot/booking the backend never returned."""

    code = "hallucinated_response"


class IdempotencyConflict(ClinicError):
    """Same (patient, kind, key) but a DIFFERENT operation payload.

    The caller is reusing a key for a new operation. Never silently return
    the old result and never execute: surface 409 and require a fresh key.
    """

    code = "idempotency_conflict"


class UnknownIdentity(ClinicError):
    """Credential identifies nobody. Fail closed: no patient, no data."""

    code = "unknown_identity"


class IdentityMismatch(ClinicError):
    """Request scoped to patient X, credential belongs to patient Y."""

    code = "identity_mismatch"
