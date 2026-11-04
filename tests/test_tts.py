"""Voice read-back on the server: off by default, Kokoro when it's turned on."""

from __future__ import annotations

from collections.abc import Iterator

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
