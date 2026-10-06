"""Structured trace — every turn is auditable for the jury."""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field


@dataclass
class TraceEvent:
    event_id: str
    seq: int
    t_ms: int
    kind: str
    data: dict


@dataclass
class Trace:
    call_id: str = field(default_factory=lambda: "call-" + uuid.uuid4().hex[:8])
    _seq: int = field(default=0, repr=False)
    _t0: float = field(default_factory=time.monotonic, repr=False)
    events: list[TraceEvent] = field(default_factory=list)

    def log(self, kind: str, **data) -> TraceEvent:
        self._seq += 1
        ev = TraceEvent(
            event_id=uuid.uuid4().hex[:8],
            seq=self._seq,
            t_ms=int((time.monotonic() - self._t0) * 1000),
            kind=kind,
            data=data,
        )
        self.events.append(ev)
        return ev
