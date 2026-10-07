"""Voice activity detection for the phone-call mode.

Silero ONNX when the model file is available (idea + windowing pattern
borrowed from IMVERA's voice_runtime, own implementation), otherwise a
plain energy detector. Both expose the same feed() -> events interface,
so the call state machine never cares which one runs.
"""
from __future__ import annotations

import array
import logging
import math
import os
from pathlib import Path

log = logging.getLogger("dbi.voice")

DEFAULT_MODEL = Path(
    os.getenv(
        "DBI_VAD_MODEL",
        "/Users/earflow/Downloads/voiseup/config/models/silero_vad.onnx",
    )
)
WINDOW_SAMPLES = 512  # 32ms @ 16kHz


class EnergyVad:
    """Zero-dependency fallback: RMS threshold with hangover."""

    def __init__(self, rms_threshold: float = 400.0) -> None:
        self._thr = rms_threshold
        self._buf = b""
        self._in_speech = False
        self._speech_ms = 0
        self._silence_ms = 0

    def _window_ms(self) -> int:
        return WINDOW_SAMPLES * 1000 // 16000

    def feed(self, pcm: bytes) -> tuple[bool, bool]:
        started, ended = False, False
        self._buf += pcm
        while len(self._buf) >= WINDOW_SAMPLES * 2:
            frame = self._buf[: WINDOW_SAMPLES * 2]
            self._buf = self._buf[WINDOW_SAMPLES * 2 :]
            samples = array.array("h")
            samples.frombytes(frame)
            rms = math.sqrt(sum(int(s) * int(s) for s in samples) / max(len(samples), 1))
            if rms >= self._thr:
                self._silence_ms = 0
                self._speech_ms += self._window_ms()
                if not self._in_speech and self._speech_ms >= 100:
                    self._in_speech = True
                    started = True
            elif self._in_speech:
                self._silence_ms += self._window_ms()
                if self._silence_ms >= 700:
                    self._in_speech = False
                    self._speech_ms = 0
                    ended = True
            else:
                self._speech_ms = 0
        return started, ended

    @property
    def in_speech(self) -> bool:
        return self._in_speech

    def reset(self) -> None:
        self._buf = b""
        self._in_speech = False
        self._speech_ms = 0
        self._silence_ms = 0


class SileroVad(EnergyVad):
    """Streaming Silero VAD; falls back to energy per-window on low prob."""

    def __init__(self, model_path: Path | None = None, prob_threshold: float = 0.4) -> None:
        super().__init__()
        import numpy as np
        import onnxruntime as ort

        path = model_path or DEFAULT_MODEL
        if not path.exists():
            raise FileNotFoundError(path)
        opts = ort.SessionOptions()
        opts.inter_op_num_threads = 1
        opts.intra_op_num_threads = 1
        self._np = np
        self._session = ort.InferenceSession(str(path), providers=["CPUExecutionProvider"], sess_options=opts)
        inputs = {i.name for i in self._session.get_inputs()}
        self._uses_state = "state" in inputs
        self._prob_threshold = prob_threshold
        self._reset_state()

    def _reset_state(self) -> None:
        np = self._np
        if self._uses_state:
            self._state = np.zeros((2, 1, 128), dtype=np.float32)
        else:
            self._h = np.zeros((2, 1, 64), dtype=np.float32)
            self._c = np.zeros((2, 1, 64), dtype=np.float32)

    def _probability(self, audio) -> float:
        np = self._np
        if self._uses_state:
            out, self._state = self._session.run(
                None,
                {"input": audio.reshape(1, -1), "state": self._state,
                 "sr": np.array(16000, dtype=np.int64)},
            )
            return float(out[0][0])
        out, self._h, self._c = self._session.run(
            None,
            {"input": audio.reshape(1, -1), "h": self._h, "c": self._c,
             "sr": np.array(16000, dtype=np.int64)},
        )
        return float(out[0][0])

    def feed(self, pcm: bytes) -> tuple[bool, bool]:
        np = self._np
        started, ended = False, False
        self._buf += pcm
        while len(self._buf) >= WINDOW_SAMPLES * 2:
            frame = self._buf[: WINDOW_SAMPLES * 2]
            self._buf = self._buf[WINDOW_SAMPLES * 2 :]
            samples = array.array("h")
            samples.frombytes(frame)
            audio = np.array(samples, dtype=np.float32) / 32768.0
            try:
                prob = self._probability(audio)
            except Exception as e:
                log.warning("silero inference failed, energy fallback: %s", e)
                prob = 0.0
            rms = math.sqrt(sum(int(s) * int(s) for s in samples) / max(len(samples), 1))
            loud = prob >= self._prob_threshold or (rms >= 500 and prob >= 0.05)
            if loud:
                self._silence_ms = 0
                self._speech_ms += self._window_ms()
                if not self._in_speech and self._speech_ms >= 100:
                    self._in_speech = True
                    started = True
            elif self._in_speech:
                self._silence_ms += self._window_ms()
                if self._silence_ms >= 700:
                    self._in_speech = False
                    self._speech_ms = 0
                    ended = True
            else:
                self._speech_ms = 0
        return started, ended

    def reset(self) -> None:
        super().reset()
        self._reset_state()


def make_vad():
    """Silero when available, energy otherwise. Never raises."""
    try:
        vad = SileroVad()
        log.info("vad=silero")
        return vad
    except Exception as e:
        log.warning("vad=silero unavailable (%s), energy fallback", e)
        return EnergyVad()
