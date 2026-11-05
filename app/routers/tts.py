"""GET /api/tts: the read-back sentence as audio, when the server voice (Kokoro) is on."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Response

from app.security.ratelimit import TTS
from app.settings import Settings, get_settings
from app.voice.tts import KokoroVoice, VoiceUnavailable, VoiceWarming, effective_engine, get_voice

router = APIRouter(prefix="/api", tags=["voice"])


@router.get("/tts", response_class=Response, dependencies=[Depends(TTS)])
async def speak(
    text: str = Query(min_length=1, max_length=400),
    lang: str = Query(default="en-GB", max_length=16),
    settings: Settings = Depends(get_settings),
    voice: KokoroVoice = Depends(get_voice),
) -> Response:
    # 404 means "use the phone's voice": the app falls back without a fuss.
    if effective_engine(settings.tts_engine) != "kokoro":
        raise HTTPException(404, "The server voice is off; the phone's voice is used.")
    try:
        audio = await voice.speak(text.strip(), lang)
    except VoiceWarming as exc:  # just started: the phone reads this one, retry soon
        raise HTTPException(503, str(exc), headers={"Retry-After": "5"}) from exc
    except VoiceUnavailable as exc:
        raise HTTPException(404, str(exc)) from exc
    return Response(
        audio, media_type="audio/wav", headers={"Cache-Control": "private, max-age=86400"}
    )


@router.get("/tts/status")
async def status(
    settings: Settings = Depends(get_settings), voice: KokoroVoice = Depends(get_voice)
) -> dict[str, object]:
    engine = effective_engine(settings.tts_engine)
    if engine != "kokoro":
        return {"engine": engine, "state": "off"}
    return {"engine": engine, **voice.status()}
