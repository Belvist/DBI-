"""Session coordination errors."""
from __future__ import annotations


class StaleState(Exception):
    """Snapshot revision moved under us: another replica committed first.

    The turn must NOT overwrite the newer state. Caller answers with a
    safe retryable fallback (HTTP 409 / WS error), never with stale facts.
    """


class OperationInProgress(Exception):
    """A new/conflicting key arrived while an uncertain WRITE is unresolved.

    After the first WRITE attempt the operation key is immutable; callers
    must reconcile (same key) instead of rebinding. Maps to HTTP 409.
    """
