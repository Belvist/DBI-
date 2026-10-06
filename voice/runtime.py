"""Thin voice runtime — ideas from IMVERA call_actor, zero copy.

Owns ONLY: turn ownership (who speaks now), generation cancellation
(barge-in: user interrupts -> assistant stops), trace events.
STT/TTS are interfaces; default is text (demo) so DoD never depends on GPUs.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field

from api.booking_service import DialogueSession
from observability.trace import Trace


@dataclass
class Generation:
    gen_id: str = field(default_factory=lambda: "g-" + uuid.uuid4().hex[:8])
    cancelled: bool = False

    def cancel(self) -> None:
        self.cancelled = True


class VoiceRuntime:
    """Wraps a DialogueSession with barge-in semantics."""

    def __init__(self, session: DialogueSession) -> None:
        self.session = session
        self.trace: Trace = session.trace
        self.current: Generation | None = None
        self.user_is_speaking = False

    def on_user_start(self) -> None:
        """VAD fired: user interrupted — stop current TTS immediately."""
        self.user_is_speaking = True
        if self.current and not self.current.cancelled:
            self.current.cancel()
            self.trace.log("barge_in", gen_id=self.current.gen_id)

    def on_user_text(self, text: str) -> tuple[str, Generation]:
        self.user_is_speaking = False
        gen = Generation()
        self.current = gen
        speech = self.session.turn(text)
        if gen.cancelled:
            self.trace.log("gen_superseded", gen_id=gen.gen_id)
            return "", gen
        self.trace.log("assistant_speech", gen_id=gen.gen_id, text=speech[:160])
        return speech, gen
