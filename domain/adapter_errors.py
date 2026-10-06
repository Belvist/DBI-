"""Fail-safe backend errors: infrastructure pain never becomes a lie."""
from __future__ import annotations


class AdapterUnavailable(Exception):
    """Backend unusable right now (breaker open / retries exhausted).

    The dialogue answers with a safe fallback instead of inventing facts.
    Never carries slot or booking details.
    """
