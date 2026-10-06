"""FastAPI identity enforcement — HTTP layer, no dialogue logic here."""
from __future__ import annotations

from fastapi import HTTPException

from domain.models import PatientRef
from identity.providers import IdentityContext, IdentityProvider


def identify_patient(
    provider: IdentityProvider,
    token: str | None,
) -> PatientRef:
    from domain.errors import UnknownIdentity

    if not token:
        raise HTTPException(status_code=401, detail="missing X-Patient-Token")
    try:
        return provider.identify(IdentityContext(token=token))
    except UnknownIdentity as e:
        raise HTTPException(status_code=401, detail=str(e)) from None


def require_scope(patient: PatientRef, requested_id: str | None) -> PatientRef:
    """Client may omit patient_id (identified one is used) but never override it."""
    from domain.errors import IdentityMismatch

    if requested_id and requested_id != patient.patient_id:
        raise HTTPException(
            status_code=403, detail=str(IdentityMismatch("patient scope mismatch"))
        )
    return patient
