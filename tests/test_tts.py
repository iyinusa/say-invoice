"""Voice read-back on the server: off by default, Kokoro when it's turned on."""

from __future__ import annotations

from collections.abc import Iterator

import anyio
import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.settings import get_settings
from app.voice.tts import VoiceUnavailable, get_voice, to_wav, voice_for


class FakeVoice:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    async def speak(self, text: str, lang: str) -> bytes:
        self.calls.append((text, lang))
        if lang.startswith("xx"):
            raise VoiceUnavailable("no voice")
        return b"RIFF....WAVE"


@pytest.fixture
def voice(monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeVoice]:
    fake = FakeVoice()
    app.dependency_overrides[get_voice] = lambda: fake
    monkeypatch.setattr(get_settings(), "tts_engine", "kokoro")
    monkeypatch.setattr("app.voice.tts.kokoro_installed", lambda: True)
    yield fake
    app.dependency_overrides.pop(get_voice, None)


def test_off_by_default_so_the_phone_speaks(client: TestClient) -> None:
    assert client.get("/api/tts", params={"text": "Hello"}).status_code == 404


def test_kokoro_returns_audio(client: TestClient, voice: FakeVoice) -> None:
    resp = client.get("/api/tts", params={"text": "Invoice for £145.00", "lang": "en-GB"})
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "audio/wav"
    assert voice.calls == [("Invoice for £145.00", "en-GB")]


def test_unknown_language_falls_back_to_the_phone(client: TestClient, voice: FakeVoice) -> None:
    assert client.get("/api/tts", params={"text": "Hi", "lang": "xx"}).status_code == 404


def test_limits(client: TestClient, voice: FakeVoice) -> None:
    assert client.get("/api/tts", params={"text": "x" * 401}).status_code == 422
    assert client.get("/api/tts", params={"text": ""}).status_code == 422


def test_voice_choice() -> None:
    assert voice_for("en-GB") == ("b", "bf_emma")
    assert voice_for("en-US") == ("a", "af_heart")
    assert voice_for("es") == ("e", "ef_dora")
    with pytest.raises(VoiceUnavailable):
        voice_for("pl")


def test_wav_encoding() -> None:
    np = pytest.importorskip("numpy")
    wav = to_wav(np.zeros(2400, dtype="float32"))
    assert wav[:4] == b"RIFF" and len(wav) == 44 + 4800  # 0.1 s of 16-bit mono at 24 kHz


def test_kokoro_asked_for_but_not_installed_falls_back(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The Docker image without the tts extra, with SI_TTS_ENGINE=kokoro in .env.
    monkeypatch.setattr(get_settings(), "tts_engine", "kokoro")
    monkeypatch.setattr("app.voice.tts.kokoro_installed", lambda: False)
    assert client.get("/api/tts", params={"text": "Hello"}).status_code == 404  # not a 500
    assert 'ttsEngine: "browser"' in client.get("/").text  # the page won't even try


def test_a_synthesis_failure_is_a_fallback_not_a_500(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.voice.tts import KokoroVoice

    monkeypatch.setattr(get_settings(), "tts_engine", "kokoro")
    monkeypatch.setattr("app.voice.tts.kokoro_installed", lambda: True)
    broken = KokoroVoice()

    def boom(text: str, lang: str) -> bytes:
        raise RuntimeError("model download failed")

    monkeypatch.setattr(broken, "_synth", boom)
    app.dependency_overrides[get_voice] = lambda: broken
    try:
        assert client.get("/api/tts", params={"text": "Hello"}).status_code == 404
    finally:
        app.dependency_overrides.pop(get_voice, None)


# ── preloading at startup ────────────────────────────────────────────────────
class _FakeKModel:
    made = 0

    def __init__(self, repo_id: str) -> None:
        type(self).made += 1

    def to(self, device: str) -> _FakeKModel:
        self.device = device
        return self

    def eval(self) -> _FakeKModel:
        return self


class _FakePipeline:
    made: list[tuple[str, object]] = []

    def __init__(self, lang_code: str, repo_id: str, model: object) -> None:
        type(self).made.append((lang_code, model))
        self.voices: list[str] = []
        self.said: list[str] = []

    def load_voice(self, voice: str) -> None:
        self.voices.append(voice)

    def __call__(self, text: str, voice: str, speed: float):  # noqa: ANN204
        import numpy as np

        self.said.append(text)
        yield text, "", np.zeros(240, dtype="float32")


@pytest.fixture
def fake_kokoro(monkeypatch: pytest.MonkeyPatch) -> None:
    import sys
    import types

    pytest.importorskip("numpy")
    _FakeKModel.made = 0
    _FakePipeline.made = []
    kokoro = types.ModuleType("kokoro")
    kokoro.KModel = _FakeKModel  # type: ignore[attr-defined]
    kokoro.KPipeline = _FakePipeline  # type: ignore[attr-defined]
    torch = types.ModuleType("torch")
    torch.cuda = types.SimpleNamespace(is_available=lambda: False)  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "kokoro", kokoro)
    monkeypatch.setitem(sys.modules, "torch", torch)


def test_preload_loads_one_model_shared_by_every_language(fake_kokoro: None) -> None:
    from app.voice.tts import KokoroVoice

    v = KokoroVoice()
    v.preload(["en-GB", "es", "pl"])  # pl has no Kokoro voice: skipped, not fatal
    assert v.state == "ready"
    assert v.languages == ["en-GB", "es"]
    assert _FakeKModel.made == 1  # not one per language
    (b_code, b_model), (e_code, e_model) = _FakePipeline.made
    assert (b_code, e_code) == ("b", "e") and b_model is e_model is v._model
    english = v._pipelines["b"]
    assert english.voices == ["bf_emma"]
    # warm-up, then the read-back's fixed sentences, which are now cached
    assert english.said[0] == "Ready." and len(english.said) == 3
    assert ("bf_emma", "Say send to send it, or change to edit it.") in v._cache


def test_after_preload_a_new_sentence_is_only_inference(fake_kokoro: None) -> None:
    from app.voice.tts import KokoroVoice

    v = KokoroVoice()
    v.preload(["en-GB"])
    wav = anyio.run(v.speak, "Invoice for £145.00 to Mr Kennedy", "en-GB")
    assert wav[:4] == b"RIFF"
    assert _FakeKModel.made == 1 and len(_FakePipeline.made) == 1  # nothing reloaded
    # A language that wasn't preloaded gets a pipeline on the same model, not a new model.
    anyio.run(v.speak, "Hola", "es")
    assert _FakeKModel.made == 1 and _FakePipeline.made[1][1] is v._model


def test_while_warming_the_phone_reads(client: TestClient, voice: FakeVoice) -> None:
    from app.voice.tts import KokoroVoice

    warming = KokoroVoice()
    warming.state = "warming"
    app.dependency_overrides[get_voice] = lambda: warming
    resp = client.get("/api/tts", params={"text": "Hello"})
    assert resp.status_code == 503 and resp.headers["retry-after"] == "5"
    assert client.get("/api/tts/status").json()["state"] == "warming"


def test_a_failed_preload_is_a_fallback(
    fake_kokoro: None, client: TestClient, voice: FakeVoice, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.voice.tts import KokoroVoice

    def broken(self: object, repo_id: str) -> None:
        raise OSError("no network to download the model")

    monkeypatch.setattr(_FakeKModel, "__init__", broken)
    v = KokoroVoice()
    v.preload(["en-GB"])
    assert v.state == "failed" and "no network" in v.error
    app.dependency_overrides[get_voice] = lambda: v
    assert client.get("/api/tts", params={"text": "Hello"}).status_code == 404


def test_status_when_off(client: TestClient) -> None:
    assert client.get("/api/tts/status").json() == {"engine": "browser", "state": "off"}


def test_startup_preloads_the_configured_languages(monkeypatch: pytest.MonkeyPatch) -> None:
    import app.voice.tts as tts

    started: list[list[str]] = []
    monkeypatch.setattr(get_settings(), "tts_engine", "kokoro")
    monkeypatch.setattr(get_settings(), "tts_preload", "en-GB, es")
    monkeypatch.setattr(tts, "kokoro_installed", lambda: True)
    monkeypatch.setattr(tts.KokoroVoice, "start_preload", lambda self, langs: started.append(langs))
    monkeypatch.setattr(tts, "_voice", None)
    with TestClient(app):
        pass
    assert started == [["en-GB", "es"]]


def test_cache_is_per_voice_not_per_spelling(fake_kokoro: None) -> None:
    from app.voice.tts import KokoroVoice

    v = KokoroVoice()
    v.preload(["en-GB"])
    anyio.run(v.speak, "Invoice for £90.00 to Mrs Patel, due in 7 days.", "en-GB")
    anyio.run(v.speak, "Invoice for £90.00 to Mrs Patel, due in 7 days.", "en-gb")
    said = v._pipelines["b"].said
    assert said.count("Invoice for £90.00 to Mrs Patel, due in 7 days.") == 1


def test_titles_are_said_as_words_not_letters() -> None:
    from app.voice.tts import say_as

    assert say_as("Invoice for £90.00 to Ms Okafor,", "b") == "Invoice for £90.00 to Miz Okafor,"
    assert say_as("Ms. Okafor and Mx Lee", "a") == "Miz Okafor and Mix Lee"
    assert say_as("Msgr Ms", "e") == "Msgr Ms"  # only English; and only the whole word
    assert say_as("Msgr", "b") == "Msgr"
