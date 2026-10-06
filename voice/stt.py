"""STT provider contract. Browser engine is the default (zero creds, jury-ready).

A future local engine (GigaAM streaming) plugs in as a new implementation of
`STTProvider` without touching dialogue or transport code.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol


@dataclass(frozen=True)
class STTConfig:
    engine: Literal["browser", "local_stub"] = "browser"
    language: str = "ru-RU"
    interim_results: bool = True


@dataclass(frozen=True)
class STTResult:
    text: str
    is_final: bool
    confidence: float = 1.0


class STTProvider(Protocol):
    @property
    def config(self) -> STTConfig: ...
    def describe(self) -> str: ...


@dataclass(frozen=True)
class BrowserSTT:
    """Web Speech API recognition runs in `voice/web/index.html`.

    Server carries no STT code by design: nothing to install, no keys,
    reproducible on any jury laptop with Chrome.
    """

    _config: STTConfig = STTConfig()

    @property
    def config(self) -> STTConfig:
        return self._config

    def describe(self) -> str:
        return f"browser web-speech lang={self._config.language}"


@dataclass(frozen=True)
class LocalSTTStub:
    """Placeholder for a future streaming GigaAM engine.

    Instantiating is fine; transcribing raises until a real engine lands,
    so nobody can mistake the stub for a working path.
    """

    _config: STTConfig = STTConfig(engine="local_stub")

    @property
    def config(self) -> STTConfig:
        return self._config

    def describe(self) -> str:
        return "local-stub (not implemented — see docs/VOICE_MANUAL_GATE.md)"

    def transcribe(self, _pcm: bytes) -> STTResult:
        raise NotImplementedError("local STT engine not wired in PR2")
