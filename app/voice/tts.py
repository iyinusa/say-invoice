"""KokoroTTS: the read-back voice on the server, when `SI_TTS_ENGINE=kokoro`.

Optional on purpose. Kokoro needs PyTorch and an ~330 MB model, so it isn't in
the default install or the Docker image (`uv sync --extra tts` adds it). The
default read-back uses the phone's own voice, which is instant, free and
multilingual.

Kokoro is preloaded when the server starts, never on someone's first tap: one
`KModel` (82M parameters) is loaded once and shared by every language's
`KPipeline` (a pipeline built without `model=` would load its own copy), then
each preloaded language gets its G2P, its voice pack and one warm-up inference.
After that a new sentence is only inference. All of it runs on one dedicated
worker thread (the model takes one call at a time anyway), so a slow synthesis
never ties up the event loop's shared thread pool. While it warms up,
`/api/tts` says so and the phone's voice reads instead. The phone asks for one
sentence at a time and plays each while the next is made. Recent sentences are
cached.
"""

from __future__ import annotations

import asyncio
import io
import re
import threading
import time
import wave
from collections import OrderedDict
from collections.abc import Iterable
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Any, Literal

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


REPO_ID = "hexgrad/Kokoro-82M"
# A short phrase per language for the warm-up inference (the first one is the slow one).
WARMUP = {"a": "Ready.", "b": "Ready.", "e": "Listo.", "f": "Prêt.", "i": "Pronto.",
          "p": "Pronto.", "h": "तैयार।", "j": "準備完了。", "z": "准备好了。"}  # fmt: skip
# The fixed closing sentences of the read-back (readBackText() in app.js). The phone asks
# for one sentence at a time, so these are made during warm-up and come from the cache.
_EN_PHRASES = [
    "Say send to send it, or change to edit it.",
    "Say send to create the link, or change to edit it.",
]
PHRASES = {"a": _EN_PHRASES, "b": _EN_PHRASES}

State = Literal["idle", "warming", "ready", "failed"]


class VoiceUnavailable(RuntimeError):
    """Kokoro isn't installed, or can't speak this language: the phone's voice takes over."""


class VoiceWarming(VoiceUnavailable):
    """Still preloading at startup: the phone's voice reads this one."""


def kokoro_installed() -> bool:
    """True only when the optional `tts` extra is really there (it isn't in the default image)."""
    import importlib.util

    try:
        return all(importlib.util.find_spec(m) is not None for m in ("kokoro", "numpy", "torch"))
    except (ImportError, ValueError):  # a broken or half-installed package counts as "not there"
        return False


def effective_engine(configured: str) -> str:
    """What the page should use: Kokoro only if it was asked for *and* can actually run."""
    if configured == "kokoro" and not kokoro_installed():
        return "browser"
    return configured if configured in ("browser", "kokoro") else "browser"


def voice_for(lang: str) -> tuple[str, str]:
    lang = (lang or "en").lower()
    for key in (lang, lang[:5], lang[:2]):
        if key in VOICES:
            return VOICES[key]
    raise VoiceUnavailable(f"no Kokoro voice for {lang}")


# Kokoro's English G2P spells some titles out ("Ms" -> "M S"); say them as words.
_EN_SAYS = [(re.compile(r"\bMs\b\.?"), "Miz"), (re.compile(r"\bMx\b\.?"), "Mix")]


def say_as(text: str, code: str) -> str:
    if code in ("a", "b"):
        for pattern, word in _EN_SAYS:
            text = pattern.sub(word, text)
    return text


def to_wav(samples: Any) -> bytes:
    """float32 samples in [-1, 1] -> 16-bit mono WAV."""
    import numpy as np  # only reached once kokoro_installed() is true

    pcm = (np.clip(samples, -1.0, 1.0) * 32767).astype("<i2").tobytes()
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SAMPLE_RATE)
        w.writeframes(pcm)
    return buf.getvalue()


class KokoroVoice:
    def __init__(self, cache_size: int = 64, device: str | None = None) -> None:
        self._device = device
        self._model: Any = None
        self._pipelines: dict[str, Any] = {}
        # Preload and every synthesis run here, off the event loop's shared pool.
        self._worker = ThreadPoolExecutor(max_workers=1, thread_name_prefix="kokoro")
        # One synthesis at a time (the model isn't thread-safe); re-entrant for the warm-up.
        self._lock = threading.RLock()
        # Keyed by voice, not the raw tag: "en-GB" and "en-gb" are one voice, "en" and "en-US" too.
        self._cache: OrderedDict[tuple[str, str], bytes] = OrderedDict()
        self._cache_size = cache_size
        self.state: State = "idle"
        self.error = ""
        self.languages: list[str] = []
        self.warm_seconds: float | None = None

    # ── loading ───────────────────────────────────────────────────────────
    def _load_model(self) -> Any:
        if self._model is None:
            try:
                import torch
                from kokoro import KModel
            except ImportError as exc:  # pragma: no cover - depends on the optional extra
                raise VoiceUnavailable("install the tts extra: uv sync --extra tts") from exc
            device = self._device or ("cuda" if torch.cuda.is_available() else "cpu")
            started = time.perf_counter()
            self._model = KModel(repo_id=REPO_ID).to(device).eval()
            log.info("kokoro_model_loaded", device=device,
                     seconds=round(time.perf_counter() - started, 2))  # fmt: skip
        return self._model

    def _pipeline(self, code: str) -> Any:
        if code not in self._pipelines:
            from kokoro import KPipeline

            # model= is the point: every language shares the one loaded model.
            self._pipelines[code] = KPipeline(
                lang_code=code, repo_id=REPO_ID, model=self._load_model()
            )
        return self._pipelines[code]

    def preload(self, languages: Iterable[str]) -> None:
        """Blocking (run it off the event loop): model, then per language G2P + voice + warm-up."""
        self.state = "warming"
        started = time.perf_counter()
        try:
            with self._lock:
                self._load_model()
                for lang in languages:
                    try:
                        code, voice = voice_for(lang)
                    except VoiceUnavailable:
                        log.warning("kokoro_no_voice", lang=lang)
                        continue
                    t = time.perf_counter()
                    pipeline = self._pipeline(code)
                    pipeline.load_voice(voice)
                    for _ in pipeline(WARMUP.get(code, "Ready."), voice=voice, speed=1.0):
                        pass
                    for phrase in PHRASES.get(code, []):
                        self._remember((voice, phrase), self._synth(phrase, lang))
                    self.languages.append(lang)
                    log.info("kokoro_language_ready", lang=lang, voice=voice,
                             seconds=round(time.perf_counter() - t, 2))  # fmt: skip
        except Exception as exc:  # a download or load failure: the phone's voice, no crash
            self.state, self.error = "failed", repr(exc)[:300]
            log.warning("kokoro_preload_failed", error=self.error)
            return
        self.warm_seconds = round(time.perf_counter() - started, 2)
        self.state = "ready"
        log.info("kokoro_ready", languages=self.languages, seconds=self.warm_seconds)

    def start_preload(self, languages: Iterable[str]) -> Future[None]:
        """Preload in the background so the server answers (and health checks pass) meanwhile."""
        self.state = "warming"
        return self._worker.submit(self.preload, list(languages))

    def status(self) -> dict[str, object]:
        return {"state": self.state, "languages": self.languages,
                "warm_seconds": self.warm_seconds, "cached": len(self._cache)}  # fmt: skip

    # ── speaking ──────────────────────────────────────────────────────────
    def _synth(self, text: str, lang: str) -> bytes:
        try:
            import numpy as np
        except ImportError as exc:
            raise VoiceUnavailable("install the tts extra: uv sync --extra tts") from exc

        code, voice = voice_for(lang)
        started = time.perf_counter()
        with self._lock:
            pipeline = self._pipeline(code)  # a language that wasn't preloaded: G2P only, no model
            spoken = say_as(text, code)
            chunks = [
                audio
                for _, _, audio in pipeline(spoken, voice=voice, speed=1.0)
                if audio is not None
            ]
        if not chunks:
            raise VoiceUnavailable("Kokoro produced no audio")
        wav = to_wav(np.concatenate([np.asarray(c, dtype="float32") for c in chunks]))
        log.info("kokoro_spoke", lang=lang, chars=len(text),
                 seconds=round(time.perf_counter() - started, 2))  # fmt: skip
        return wav

    def _remember(self, key: tuple[str, str], audio: bytes) -> None:
        self._cache[key] = audio
        self._cache.move_to_end(key)
        while len(self._cache) > self._cache_size:
            self._cache.popitem(last=False)

    async def speak(self, text: str, lang: str) -> bytes:
        key = (voice_for(lang)[1], text)  # an unsupported language: VoiceUnavailable, quickly
        if key in self._cache:
            self._cache.move_to_end(key)
            return self._cache[key]
        if self.state == "warming":
            raise VoiceWarming("the server voice is still warming up; the phone's voice reads this")
        if self.state == "failed":
            raise VoiceUnavailable("the server voice didn't load; the phone's voice takes over")
        try:
            audio = await asyncio.wrap_future(self._worker.submit(self._synth, text, lang))
        except VoiceUnavailable:
            raise
        except Exception as exc:  # a model download or synthesis failure: never a 500
            log.warning("kokoro_failed", error=repr(exc)[:300])
            raise VoiceUnavailable("the server voice failed; the phone's voice takes over") from exc
        self._remember(key, audio)
        return audio


def prefetch_all() -> None:
    """Docker build step: download the model and every voice pack, then warm English once."""
    from huggingface_hub import hf_hub_download

    for _, voice in VOICES.values():
        hf_hub_download(repo_id=REPO_ID, filename=f"voices/{voice}.pt")
    voice = KokoroVoice()
    voice.preload(["en-GB"])
    if voice.state != "ready":
        raise SystemExit(f"kokoro prefetch failed: {voice.error}")
    print(f"kokoro ready in {voice.warm_seconds}s")


_voice: KokoroVoice | None = None


def get_voice() -> KokoroVoice:
    global _voice
    if _voice is None:
        _voice = KokoroVoice()
    return _voice


if __name__ == "__main__":  # python -m app.voice.tts  (used by the Dockerfile)
    prefetch_all()
