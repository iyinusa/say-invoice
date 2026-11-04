"""GET /api/tts: the read-back sentence as audio, when the server voice (Kokoro) is on."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Response

from app.settings import Settings, get_settings
from app.voice.tts import KokoroVoice, VoiceUnavailable, get_voice

router = APIRouter(prefix="/api", tags=["voice"])


@router.get("/tts", response_class=Response)
async def speak(
    text: str = Query(min_length=1, max_length=400),
    lang: str = Query(default="en-GB", max_length=16),
    settings: Settings = Depends(get_settings),
    voice: KokoroVoice = Depends(get_voice),
) -> Response:
    # 404 means "use the phone's voice": the app falls back without a fuss.
    if settings.tts_engine != "kokoro":
        raise HTTPException(404, "The server voice is off; the phone's voice is used.")
    try:
        audio = await voice.speak(text.strip(), lang)
    except VoiceUnavailable as exc:
        raise HTTPException(404, str(exc)) from exc
    return Response(
        audio, media_type="audio/wav", headers={"Cache-Control": "private, max-age=86400"}
    )
