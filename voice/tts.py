"""TTS provider contract. Browser engine is the default (zero creds, jury-ready).

A future local engine (Silero/Qwen streaming PCM) plugs in as a new
implementation of `TTSProvider` without touching dialogue or transport code.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol


@dataclass(frozen=True)
class TTSConfig:
    engine: Literal["browser", "local_stub"] = "browser"
    language: str = "ru-RU"
    rate: float = 1.0


class TTSProvider(Protocol):
    @property
    def config(self) -> TTSConfig: ...
    def describe(self) -> str:
        ...


@dataclass(frozen=True)
class BrowserTTS:
    """speechSynthesis runs in `voice/web/index.html`.

    Barge-in is client-side and instant: any interim STT result cancels the
    current utterance before the server even hears about it.
    """

    _config: TTSConfig = TTSConfig()

    @property
    def config(self) -> TTSConfig:
        return self._config

    def describe(self) -> str:
        return f"browser speech-synthesis lang={self._config.language}"


@dataclass(frozen=True)
class LocalTTSStub:
    """Placeholder for future streaming PCM synthesis (Silero/Qwen)."""

    _config: TTSConfig = TTSConfig(engine="local_stub")

    @property
    def config(self) -> TTSConfig:
        return self._config

    def describe(self) -> str:
        return "local-stub (not implemented — see docs/VOICE_MANUAL_GATE.md)"

    def synthesize(self, _text: str) -> bytes:
        raise NotImplementedError("local TTS engine not wired in PR2")
