"""KokoroTTS: the read-back voice on the server, when `SI_TTS_ENGINE=kokoro`.

Optional on purpose. Kokoro needs PyTorch and an ~330 MB model, so it isn't in
the default install or the Docker image (`uv sync --extra tts` adds it). The
default read-back uses the phone's own voice, which is instant, free and
multilingual. Here the model loads lazily on the first request, synthesis runs
in a worker thread, and recent sentences are cached.
"""

from __future__ import annotations

import io
import threading
import wave
from collections import OrderedDict
from typing import Any

import anyio

from app.logging_config import get_logger

log = get_logger("tts")

SAMPLE_RATE = 24_000
# BCP-47 prefix -> (Kokoro language code, voice). British English first: a UK tradesperson.
VOICES = {
    "en-gb": ("b", "bf_emma"),
    "en": ("a", "af_heart"),
    "es": ("e", "ef_dora"),
    "fr": ("f", "ff_siwis"),
    "it": ("i", "if_sara"),
    "pt": ("p", "pf_dora"),
    "hi": ("h", "hf_alpha"),
    "ja": ("j", "jf_alpha"),
    "zh": ("z", "zf_xiaobei"),
}


class VoiceUnavailable(RuntimeError):
    """Kokoro isn't installed, or can't speak this language: the phone's voice takes over."""


def voice_for(lang: str) -> tuple[str, str]:
    lang = (lang or "en").lower()
    for key in (lang, lang[:5], lang[:2]):
        if key in VOICES:
            return VOICES[key]
    raise VoiceUnavailable(f"no Kokoro voice for {lang}")


def to_wav(samples: Any) -> bytes:
    """float32 samples in [-1, 1] -> 16-bit mono WAV."""
    import numpy as np

    pcm = (np.clip(samples, -1.0, 1.0) * 32767).astype("<i2").tobytes()
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SAMPLE_RATE)
        w.writeframes(pcm)
    return buf.getvalue()


class KokoroVoice:
    def __init__(self, cache_size: int = 64) -> None:
        self._pipelines: dict[str, Any] = {}
        self._lock = threading.Lock()  # one synthesis at a time: the model isn't thread-safe
        self._cache: OrderedDict[tuple[str, str], bytes] = OrderedDict()
        self._cache_size = cache_size

    def _pipeline(self, code: str) -> Any:
        if code not in self._pipelines:
            try:
                from kokoro import KPipeline
            except ImportError as exc:  # pragma: no cover - depends on the optional extra
                raise VoiceUnavailable("install the tts extra: uv sync --extra tts") from exc
            log.info("kokoro_loading", lang_code=code)
            self._pipelines[code] = KPipeline(lang_code=code, repo_id="hexgrad/Kokoro-82M")
        return self._pipelines[code]

    def _synth(self, text: str, lang: str) -> bytes:
        import numpy as np

        code, voice = voice_for(lang)
        with self._lock:
            pipeline = self._pipeline(code)
            chunks = [
                audio for _, _, audio in pipeline(text, voice=voice, speed=1.0) if audio is not None
            ]
        if not chunks:
            raise VoiceUnavailable("Kokoro produced no audio")
        return to_wav(np.concatenate([np.asarray(c, dtype="float32") for c in chunks]))

    async def speak(self, text: str, lang: str) -> bytes:
        key = (lang.lower(), text)
        if key in self._cache:
            self._cache.move_to_end(key)
            return self._cache[key]
        audio = await anyio.to_thread.run_sync(self._synth, text, lang)
        self._cache[key] = audio
        if len(self._cache) > self._cache_size:
            self._cache.popitem(last=False)
        return audio


_voice: KokoroVoice | None = None


def get_voice() -> KokoroVoice:
    global _voice
    if _voice is None:
        _voice = KokoroVoice()
    return _voice
