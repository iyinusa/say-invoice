"""POST /api/parse: a voice note or typed text in, a preview card out.

Send `multipart/form-data` with `audio` (the browser's MediaRecorder blob)
and/or `text`. To answer the card's follow-up question, send the answer plus
the card's `draft` back as `previous`.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from pydantic import ValidationError

from app.agents.parser import JobParser, ParserUnavailable, get_job_parser
from app.logging_config import get_logger
from app.models import Workspace
from app.routers.invoices import current_workspace
from app.schemas.job import ParsedJob, ParseResult
from app.services import clock
from app.settings import Settings, get_settings

router = APIRouter(prefix="/api", tags=["parse"])
log = get_logger("parse")

MAX_TEXT_CHARS = 2000


def _today(tz: str) -> date:
    """The tradesperson's date, not the server's: 'due Friday' said at 11pm matters."""
    try:
        zone = ZoneInfo(tz) if tz else UTC
    except (ZoneInfoNotFoundError, ValueError):
        zone = UTC
    return datetime.now(zone).date()


def _audio_mime(content_type: str | None) -> str:
    # MediaRecorder sends e.g. "audio/webm;codecs=opus"; Gemini wants the bare type.
    mime = (content_type or "").split(";")[0].strip().lower()
    if mime == "video/webm":  # Chrome on Android labels audio-only webm this way
        mime = "audio/webm"
    if not mime.startswith("audio/"):
        raise HTTPException(415, "send an audio recording")
    return mime


@router.post("/parse", response_model=ParseResult)
async def parse_job(
    audio: UploadFile | None = File(None),
    text: str = Form(""),
    mode: Literal["auto", "invoice", "quote"] = Form("auto"),
    previous: str = Form("", description="The `draft` from the last card, as JSON."),
    tz: str = Form("", description="IANA time zone from the browser, e.g. Europe/London."),
    settings: Settings = Depends(get_settings),
    parser: JobParser = Depends(get_job_parser),
    ws: Workspace = Depends(current_workspace),
) -> ParseResult:
    if len(text) > MAX_TEXT_CHARS:
        raise HTTPException(413, f"keep it under {MAX_TEXT_CHARS} characters")

    audio_bytes: bytes | None = None
    mime = ""
    if audio is not None:
        mime = _audio_mime(audio.content_type)
        # Read one byte past the cap so an oversized upload is refused, not truncated.
        audio_bytes = await audio.read(settings.max_audio_bytes + 1)
        if len(audio_bytes) > settings.max_audio_bytes:
            raise HTTPException(413, "that recording is too long; try a shorter one")
        audio_bytes = audio_bytes or None

    prior: ParsedJob | None = None
    if previous.strip():
        try:
            prior = ParsedJob.model_validate_json(previous)
        except ValidationError as exc:
            raise HTTPException(422, "previous is not a valid draft") from exc

    if not audio_bytes and not text.strip():
        raise HTTPException(422, "say or type the job first")

    try:
        result = await parser.parse(
            today=_today(tz) + clock.offset(ws),  # "due Friday" counts from the demo clock
            default_currency=settings.default_currency,
            text=text,
            audio=audio_bytes,
            audio_mime=mime,
            mode=mode,
            previous=prior,
        )
    except ParserUnavailable as exc:
        raise HTTPException(
            503, "Couldn't read that just now. Try again, or type it instead."
        ) from exc

    log.info(
        "parsed",
        mode=result.mode,
        ready=result.ready,
        items=len(result.items),
        missing=result.missing_fields,
        source="audio" if audio_bytes else "text",
        follow_up=prior is not None,
    )
    return result
