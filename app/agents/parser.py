"""Parser agent: a voice note (or typed text) in, a checked preview card out.

One multimodal Gemini call reads the audio and returns a `ParsedJob` against a
JSON schema; there is no separate speech-to-text step, so it works the same in
iOS Safari and in every language. `parser_rules.finalize` then does everything
that must be right: pricing, dates, what's missing.

This is a single structured call rather than an ADK agent on purpose: it has
no tools and no conversation, so ADK would only add latency. The Deal and
Chase agents, which do use tools, run on ADK.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import date
from typing import Any

from google import genai
from google.genai import errors, types
from pydantic import ValidationError

from app.agents.parser_rules import RequestedMode, finalize
from app.logging_config import get_logger
from app.schemas.job import ParsedJob, ParseResult
from app.settings import Settings, get_settings

log = get_logger("parser")

# (parts, system prompt) -> the model's draft. Gemini in production, a fake in tests.
Extract = Callable[[list[types.Part], str], Awaitable[ParsedJob]]


class ParserNotConfigured(RuntimeError):
    """No Gemini credentials. Answered as 503 so the UI offers 'type instead'."""


class ParserUnavailable(RuntimeError):
    """Gemini timed out or failed twice. The UI keeps the recording and offers a retry."""


SYSTEM_PROMPT = """\
You read a tradesperson's description of a job (spoken or typed) and fill in a
draft {mode_hint} for them to check. Plumbers, cleaners, tutors, dog walkers,
market traders: expect accents, filler words, background noise and slang
("quid", "a ton" = 100, "fifty bucks").

Today is {today_long} ({today_iso}). The workspace currency is {currency}.

Rules:
- `transcript`: exactly what was said, in the spoken language. `language`: its BCP-47 code.
- `mode`: "quote" if they say quote, estimate or "how much would it be"; otherwise "invoice".
- One item per thing charged for. Labour and materials are separate items;
  parts and materials have is_material=true.
- "2 hours at £60" -> quantity 2, unit_price 60, unit "hours".
  A flat price -> quantity 1, unit "fixed". "5 walks at £12" -> quantity 5, unit "items".
- unit_price is always the price per unit exactly as said. Never add, multiply,
  apply discounts or compute totals: code does all the maths.
- No price said for an item -> unit_price 0 and add "items.N.unit_price" to missing_fields.
- "about", "roughly", "-ish", "around" -> approximate=true.
- When they correct themselves ("two hours, no, two and a half"), keep the last value.
- Currency from symbols or words (£/pounds/quid -> GBP, $/dollars/bucks -> USD,
  €/euros -> EUR, naira -> NGN). Leave it empty if nothing implies one.
- Copy email addresses exactly as said: never translate or "correct" any part
  (example.es stays example.es, never ejemplo.es).
- Spelled-out emails: "kennedy at example dot com" -> kennedy@example.com, and
  add "client_email" to uncertain_fields. Never invent an email.
- Due dates: a weekday ("due Friday", "end of the week" = friday) -> due_weekday;
  "next week" with a day ("end of next week", "Tuesday next week") also sets
  due_weeks_ahead 1. A span ("in two weeks",
  "net 30") -> due_in_days. A calendar date ("the 30th of November") -> due_date
  as ISO, choosing the next such date from today. Nothing said -> leave all empty.
- Deposits (quotes only): "30% deposit" -> deposit_pct 30; "£50 deposit" -> deposit_amount 50.
- Field paths: client_name, client_email, currency, items, items.N.unit_price,
  items.N.quantity, due_date, deposit_pct (N counts from 0).
- missing_fields: required things not said. Invoices need items with prices and
  client_email. Quotes need items with prices and a client name or email.
- follow_up_question: one short, friendly question for the most important
  missing field, in the spoken language. Empty if nothing is missing.
- Write item names short and client-friendly, in the spoken language.

Examples (said -> key fields):
1. "Fixed Mr Kennedy's boiler, 2 hours at £60, plus £25 for a valve, email kennedy@example.com"
   -> invoice, GBP, client "Mr Kennedy"; items: "Boiler repair (labour)" 2 hours @60;
   "Valve" 1 items @25 material.
2. "Deep clean for Sarah Jones, flat rate 120 pounds, due Friday"
   -> invoice, GBP; "Deep clean" 1 fixed @120; due_weekday friday;
   missing client_email; question "What's Sarah Jones's email address?"
3. "Quote for Mrs Patel, replace the kitchen tap, £90 labour plus about £40 parts, 30% deposit"
   -> quote; "Replace kitchen tap (labour)" 1 fixed @90; "Parts" 1 items @40 material
   approximate; deposit_pct 30.
4. "Walked Bella five times this week, twelve quid a walk, owner is Kate, kate.b@gmail.com"
   -> invoice, GBP, client "Kate"; "Dog walks (Bella)" 5 items @12.
5. "Invoice Tom two days labour at 180 a day and sixty quid of materials,
   tom at toms builds dot co dot uk, net 30"
   -> "Labour (days)" 2 items @180; "Materials" 1 items @60 material;
   client_email tom@tomsbuilds.co.uk (uncertain); due_in_days 30.
6. "Gutter clearing at the Hendersons', sixty-five dollars, due in two weeks"
   -> USD, client "the Hendersons"; "Gutter clearing" 1 fixed @65; due_in_days 14;
   missing client_email.
7. "Garden tidy for Mr Okafor, 3 hours at 25 euros, plus green waste disposal"
   -> EUR; "Garden tidy" 3 hours @25; "Green waste disposal" 1 items @0;
   missing items.1.unit_price and client_email.
8. "Estimate for Lisa, repaint the hallway, roughly 400 all in, fifty quid deposit"
   -> quote, GBP; "Repaint hallway" 1 fixed @400 approximate; deposit_amount 50.
9. "Leak fixed, forty-five for the call-out and a 12.50 washer"
   -> "Call-out" 1 fixed @45; "Washer" 1 items @12.5 material; missing client_email.
10. "Two hours at sixty, sorry, two and a half hours, for Jim, jim@example.com"
    -> "Labour" 2.5 hours @60.
11. "Factura para Carmen López, limpieza de oficina, cuatro horas a 15 euros la hora,
    carmen.lopez@example.es"
    -> language "es", EUR; "Limpieza de oficina" 4 hours @15.
12. "Twenty jars of honey at six pounds each for Ben, ben@example.com, due 30th November"
    -> "Honey (jar)" 20 items @6; due_date the next 30 November.
13. "Mr Ade, tutoring, 1.5 hrs @ $40/hr, ade@example.com" (typed)
    -> USD; "Tutoring" 1.5 hours @40.
14. "Patio jet wash for Mrs Cole, eighty quid, cole@example.com, by the end of next week"
    -> "Patio jet wash" 1 fixed @80; due_weekday friday, due_weeks_ahead 1.
"""

FOLLOW_UP_PROMPT = """\
The tradesperson is answering a question about the draft below, or saying what
changed ("used a £45 cartridge instead of the parts", "took an extra hour",
"didn't need the valve"). Return the complete updated draft: apply what they say
now (an answer, a correction, a swapped, added or removed item) and keep every
other field and item exactly as it is. `transcript` holds only what they said now.

Current draft:
{draft}
"""


def build_system_prompt(mode: RequestedMode, today: date, currency: str) -> str:
    hint = {"auto": "invoice or quote", "invoice": "invoice", "quote": "quote"}[mode]
    return SYSTEM_PROMPT.format(
        mode_hint=hint,
        today_long=f"{today:%A} {today.day} {today:%B %Y}",
        today_iso=today.isoformat(),
        currency=currency,
    )


def build_parts(
    *, text: str, audio: bytes | None, audio_mime: str, previous: ParsedJob | None
) -> list[types.Part]:
    parts: list[types.Part] = []
    if previous is not None:
        draft = previous.model_dump_json(exclude={"follow_up_question"})
        parts.append(types.Part.from_text(text=FOLLOW_UP_PROMPT.format(draft=draft)))
    if audio:
        parts.append(types.Part.from_bytes(data=audio, mime_type=audio_mime))
    if text.strip():
        parts.append(types.Part.from_text(text=text.strip()))
    return parts


def _retryable(exc: Exception) -> bool:
    if isinstance(exc, TimeoutError | ValidationError | errors.ServerError):
        return True
    # 429: a quota blip, worth one more go. Other 4xx (bad key, bad audio) won't fix themselves.
    return isinstance(exc, errors.ClientError) and exc.code == 429


class JobParser:
    def __init__(self, extract: Extract, *, attempts: int = 2) -> None:
        self._extract = extract
        self._attempts = attempts

    async def parse(
        self,
        *,
        today: date,
        default_currency: str,
        text: str = "",
        audio: bytes | None = None,
        audio_mime: str = "",
        mode: RequestedMode = "auto",
        previous: ParsedJob | None = None,
    ) -> ParseResult:
        parts = build_parts(text=text, audio=audio, audio_mime=audio_mime, previous=previous)
        if not parts or (previous is not None and len(parts) == 1):
            raise ValueError("say or type something first")
        system = build_system_prompt(mode, today, default_currency)

        job = await self._call(parts, system)
        if previous is not None and previous.transcript:
            # Keep the whole conversation as the invoice's transcript.
            job.transcript = f"{previous.transcript}\n{job.transcript}".strip()
        return finalize(
            job,
            requested_mode=mode,
            default_currency=default_currency,
            today=today,
            heard=audio is not None,
        )

    async def _call(self, parts: list[types.Part], system: str) -> ParsedJob:
        last: Exception | None = None
        for attempt in range(1, self._attempts + 1):
            try:
                return await self._extract(parts, system)
            except Exception as exc:
                last = exc
                log.warning("parse_attempt_failed", attempt=attempt, error=repr(exc)[:300])
                if not _retryable(exc):
                    break
        raise ParserUnavailable("couldn't read that just now") from last


def _all_required(schema: Any) -> Any:
    """Mark every property required, recursively.

    With optional fields Gemini sometimes closes the JSON early: a valid draft
    that stops after `client_email`, with no items. Required fields (defaults
    still describe the empty value) make it fill in the whole draft.
    """
    if isinstance(schema, dict):
        if "properties" in schema:
            schema["required"] = list(schema["properties"])
        for value in schema.values():
            _all_required(value)
    elif isinstance(schema, list):
        for value in schema:
            _all_required(value)
    return schema


MODEL_SCHEMA = _all_required(ParsedJob.model_json_schema())


def gemini_extractor(settings: Settings) -> Extract:
    settings.apply_google_env()
    client = genai.Client()
    level = settings.gemini_thinking_level.strip().upper()
    thinking = types.ThinkingConfig(thinking_level=types.ThinkingLevel(level)) if level else None

    async def extract(parts: list[types.Part], system: str) -> ParsedJob:
        resp = await asyncio.wait_for(
            client.aio.models.generate_content(
                model=settings.gemini_model,
                contents=[types.Content(role="user", parts=parts)],
                config=types.GenerateContentConfig(
                    system_instruction=system,
                    response_mime_type="application/json",
                    response_json_schema=MODEL_SCHEMA,
                    temperature=0,
                    thinking_config=thinking,
                ),
            ),
            timeout=settings.parse_timeout_s,
        )
        return ParsedJob.model_validate_json(resp.text or "")

    return extract


_parser: JobParser | None = None


def get_job_parser() -> JobParser:
    """Process-wide parser. FastAPI dependency; overridden in tests."""
    global _parser
    if _parser is None:
        settings = get_settings()
        if not settings.gemini_configured:
            raise ParserNotConfigured("set SI_GOOGLE_API_KEY or SI_VERTEX_PROJECT")
        _parser = JobParser(gemini_extractor(settings))
    return _parser
