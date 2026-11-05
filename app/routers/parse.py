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
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.parser import JobParser, ParserUnavailable, get_job_parser
from app.agents.parser_rules import finalize
from app.db import get_session
from app.logging_config import get_logger
from app.models import Workspace
from app.routers.invoices import current_workspace
from app.schemas.job import ParsedJob, ParseResult
from app.security.ratelimit import PARSE
from app.services import clients, clock
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


async def _remember_client(
    session: AsyncSession, ws: Workspace, result: ParseResult, *, mode: str, today: date,
    currency: str,
) -> ParseResult:  # fmt: skip
    """Fill in a saved client's email and language, then let the rules re-check the card."""
    from app.i18n import base

    result.client_language = base(result.language)
    match, options = await clients.find(session, ws, result.client_name, result.client_email)
    if match is None:
        result.client_suggestions = [clients.view(c) for c in options]
        return result
    if not result.client_email:
        draft = result.draft.model_copy(update={
            "client_email": match.email,
            "client_name": match.name or result.client_name,
            "missing_fields": [f for f in result.draft.missing_fields if f != "client_email"],
            "follow_up_question": "" if result.missing_fields[:1] == ["client_email"]
            else result.draft.follow_up_question,
        })  # fmt: skip
        result = finalize(draft, requested_mode=mode, default_currency=currency, today=today)  # type: ignore[arg-type]
    result.client_source = "saved"
    result.client_language = base(match.language)
    return result


def _audio_mime(content_type: str | None) -> str:
    # MediaRecorder sends e.g. "audio/webm;codecs=opus"; Gemini wants the bare type.
    mime = (content_type or "").split(";")[0].strip().lower()
    if mime == "video/webm":  # Chrome on Android labels audio-only webm this way
        mime = "audio/webm"
    if not mime.startswith("audio/"):
        raise HTTPException(415, "send an audio recording")
    return mime


@router.post("/parse", response_model=ParseResult, dependencies=[Depends(PARSE)])
async def parse_job(
    audio: UploadFile | None = File(None),
    text: str = Form(""),
    mode: Literal["auto", "invoice", "quote"] = Form("auto"),
    previous: str = Form("", description="The `draft` from the last card, as JSON."),
    tz: str = Form("", description="IANA time zone from the browser, e.g. Europe/London."),
    settings: Settings = Depends(get_settings),
    parser: JobParser = Depends(get_job_parser),
    ws: Workspace = Depends(current_workspace),
    session: AsyncSession = Depends(get_session),
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
        today = _today(tz) + clock.offset(ws)  # "due Friday" counts from the demo clock
        result = await parser.parse(
            today=today,
            default_currency=ws.currency,
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

    result = await _remember_client(session, ws, result, mode=mode, today=today,
                                    currency=ws.currency)  # fmt: skip
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
