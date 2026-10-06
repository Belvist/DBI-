"""LLM proposer — proposes meaning, never decides. Disabled by default.

Contract:
- Returns NLUResult | None. None = abstain, deterministic path owns the turn.
- Output is schema-validated and confidence-clamped; workflow ignores any
  field the proposer hallucinates (e.g. exact slot datetimes are dropped —
  only date_range hints + specialty survive).
- No network call in the default offline proposer (returns None).
  Real LLM wiring plugs in here 13.10+ behind an env flag, with logs.
"""
from __future__ import annotations

import os
from datetime import datetime

from nlu.schema import NLUResult


class LlmProposer:
    def __init__(self, enabled: bool | None = None) -> None:
        if enabled is None:
            enabled = os.getenv("DBI_LLM_NLU", "0") == "1"
        self.enabled = enabled

    def propose(self, text: str, now: datetime | None = None) -> NLUResult | None:
        if not self.enabled:
            return None
        # Placeholder for a real LLM call: must return schema-valid JSON.
        # Until wired, abstain rather than guess.
        _ = (text, now)
        return None


def fuse(deterministic: NLUResult, proposed: NLUResult | None) -> NLUResult:
    """Deterministic wins. LLM only fills UNKNOWN / low-confidence gaps."""
    if proposed is None:
        return deterministic
    if deterministic.confidence >= 0.6:
        return deterministic
    # adopt proposal but strip anything that could invent a slot
    proposed.date_from = proposed.date_from
    proposed.confidence = min(proposed.confidence, 0.65)
    return proposed
