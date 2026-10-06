"""Voice engines backed by the already-running IMVERA workers (read-only use).

- STT: GigaAM worker, POST {base}/v1/transcribe {call_id, sample_rate, pcm_b64}
  with 16-bit mono PCM. Utterance-final (no streaming partials on CPU).
- TTS: Qwen worker, POST {base}/v1/synthesize -> NDJSON {sequence,
  sample_rate, pcm_b64} chunks, concatenated here.

Defaults point at localhost where the user already runs these workers.
Override with DBI_VOICE_STT_URL / DBI_VOICE_TTS_URL. Nothing here imports
or modifies voiseup code — plain HTTP to its documented endpoints.
"""
from __future__ import annotations

import base64
import json
import logging
import urllib.request
import uuid

log = logging.getLogger("dbi.voice")

DEFAULT_STT_URL = "http://127.0.0.1:9101"
DEFAULT_TTS_URL = "http://127.0.0.1:9102"
MAX_PCM_BYTES = 60 * 16000 * 2  # 60s @ 16kHz mono int16


class VoiceEngineError(Exception):
    """voiseup worker unreachable or unhappy. Caller falls back / 503s."""


def _post_json(url: str, payload: dict, timeout: float) -> tuple[int, bytes]:
    try:
        req = urllib.request.Request(
            url, data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read()
    except Exception as e:
        raise VoiceEngineError(f"POST {url} failed: {e}") from e


class VoiseupSTT:
    def __init__(self, base_url: str = DEFAULT_STT_URL, timeout: float = 60.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def describe(self) -> str:
        return f"voiseup-gigaam at {self.base_url}"

    def transcribe_pcm16(self, pcm: bytes, sample_rate: int = 16000) -> str:
        """Transcribe one utterance. Raises VoiceEngineError on failure."""
        if not pcm or len(pcm) > MAX_PCM_BYTES:
            raise VoiceEngineError(f"bad utterance size: {len(pcm)}")
        if len(pcm) < 3200:  # <0.1s @16k: nothing to recognize
            return ""
        status, raw = _post_json(
            f"{self.base_url}/v1/transcribe",
            {"call_id": f"dbi-{uuid.uuid4().hex[:8]}", "sample_rate": sample_rate,
             "pcm_b64": base64.b64encode(pcm).decode()},
            self.timeout,
        )
        if status != 200:
            raise VoiceEngineError(f"stt status {status}: {raw[:200]!r}")
        try:
            text = json.loads(raw.decode()).get("text", "")
        except Exception as e:
            raise VoiceEngineError(f"stt bad response: {e}") from e
        return text.strip()


class VoiseupTTS:
    def __init__(self, base_url: str = DEFAULT_TTS_URL, timeout: float = 60.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def describe(self) -> str:
        return f"voiseup-qwen at {self.base_url}"

    def synthesize(self, text: str) -> tuple[int, bytes]:
        """Returns (sample_rate, concatenated PCM16 bytes)."""
        if not text or not text.strip():
            raise VoiceEngineError("empty text")
        status, raw = _post_json(
            f"{self.base_url}/v1/synthesize",
            {"call_id": f"dbi-{uuid.uuid4().hex[:8]}",
             "generation_id": f"g-{uuid.uuid4().hex[:8]}",
             "phrase_id": f"p-{uuid.uuid4().hex[:8]}",
             "text": text[:2000], "language": "ru"},
            self.timeout,
        )
        if status != 200:
            raise VoiceEngineError(f"tts status {status}: {raw[:200]!r}")
        rate = 24000
        chunks: list[bytes] = []
        for line in raw.decode(errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                item = json.loads(line)
            except Exception as e:
                log.debug("skipping malformed tts chunk: %s", e)
                continue
            if item.get("done"):
                if item.get("error"):
                    raise VoiceEngineError(f"tts engine: {item['error']}")
                break
            if "pcm_b64" in item:
                rate = int(item.get("sample_rate", rate))
                chunks.append(base64.b64decode(item["pcm_b64"]))
        if not chunks:
            raise VoiceEngineError("tts produced no audio")
        return rate, b"".join(chunks)
