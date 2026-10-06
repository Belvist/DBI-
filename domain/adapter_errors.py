"""Fail-safe backend errors: infrastructure pain never becomes a lie."""
from __future__ import annotations


class AdapterUnavailable(Exception):
    """Backend unusable right now (breaker open / retries exhausted).

    The dialogue answers with a safe fallback instead of inventing facts.
    Never carries slot or booking details.
    """


class DependencyUnavailable(Exception):
    """A platform dependency (Redis, pool, snapshot store) failed.

    Maps to HTTP 503 / a controlled WS error — never to a 500 with a
    traceback, and never to invented dialogue facts.
    """
