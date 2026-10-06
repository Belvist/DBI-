"""Patient identity — WHO is speaking. Swappable before/after 13.10.

Fail-closed rules (see docs/ARCHITECTURE.md):
- No credential -> no patient. There is no anonymous fallback.
- A request scoped to patient X with a credential for patient Y -> 403.
- GET_STATUS / RESCHEDULE always run under the IDENTIFIED patient, never
  under a client-supplied id, so one patient can never see or move
  another patient's bookings.

Providers:
- DemoIdentityProvider  (DBI_IDENTITY=demo): token "demo" -> fixed patient.
  Jury/demo convenience only; logs a loud warning, never the default in prod.
- TokenFileIdentityProvider (DBI_IDENTITY=tokenfile): bearer tokens mapped
  to patients in a JSON file. DBI's real mechanism (13.10) plugs in here
  as a third implementation of IdentityProvider.
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from domain.models import PatientRef

log = logging.getLogger("dbi.identity")

DEMO_TOKEN = "demo"
DEMO_PATIENT = PatientRef(
    patient_id="demo-patient", phone="+7***42", name="Демо Пациент"
)


@dataclass(frozen=True)
class IdentityContext:
    token: str


class IdentityProvider(Protocol):
    def identify(self, ctx: IdentityContext) -> PatientRef:
        """Return the patient for this credential or raise UnknownIdentity."""
        ...


class DemoIdentityProvider:
    """Single fixed patient, token-gated. Demo only."""

    def __init__(self) -> None:
        log.warning("identity=demo: single fixed patient, NOT for production")

    def identify(self, ctx: IdentityContext) -> PatientRef:
        from domain.errors import UnknownIdentity

        if ctx.token != DEMO_TOKEN:
            raise UnknownIdentity("unknown token for demo provider")
        return DEMO_PATIENT


class TokenFileIdentityProvider:
    """Bearer-token -> patient mapping loaded from a JSON file.

    File format: {"<token>": {"patient_id": "...", "phone": "...", "name": "..."}}
    """

    def __init__(self, path: str | Path) -> None:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
        self._table: dict[str, PatientRef] = {
            token: PatientRef(**info) for token, info in raw.items()
        }
        log.info("identity=tokenfile: %d tokens loaded", len(self._table))

    def identify(self, ctx: IdentityContext) -> PatientRef:
        from domain.errors import UnknownIdentity

        try:
            return self._table[ctx.token]
        except KeyError:
            raise UnknownIdentity("unknown bearer token") from None


def resolve_provider(
    mode: str | None = None, tokens_file: str | None = None
) -> IdentityProvider:
    mode = mode if mode is not None else os.getenv("DBI_IDENTITY", "demo")
    if mode == "demo":
        return DemoIdentityProvider()
    if mode == "tokenfile":
        path = tokens_file if tokens_file is not None else os.getenv("DBI_TOKENS_FILE", "")
        if not path:
            raise RuntimeError("DBI_IDENTITY=tokenfile requires DBI_TOKENS_FILE")
        return TokenFileIdentityProvider(path)
    raise RuntimeError(f"unknown DBI_IDENTITY mode: {mode!r}")
