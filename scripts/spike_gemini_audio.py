"""Spike 3: one multimodal Gemini call turns a voice note into a structured job.

    uv run python -m scripts.spike_gemini_audio path/to/voice-note.m4a
    uv run python -m scripts.spike_gemini_audio --text "Fixed Mr Kennedy's boiler, 2h at £60..."

Record the voice note on your phone (Voice Memos / Recorder) to test real
accents and background noise. Prints the transcript, the parsed JSON and the
total computed in code, plus the latency.
"""

from __future__ import annotations

import argparse
import mimetypes
import time
from pathlib import Path

from google import genai
from google.genai import types

from app.money import subtotal
from app.schemas.job import ParsedJob
from app.settings import get_settings

PROMPT = """\
You turn a tradesperson's spoken description of a job into structured data for
an invoice (or a quote, if they say "quote" or "estimate").

Rules:
- Transcribe exactly what was said into `transcript`, in the spoken language.
- Extract line items. "2 hours at £60" is quantity 2, unit_price 60, unit hours.
  Parts and materials are separate items with is_material=true.
- Never add up, never compute totals, never invent prices.
- Currency from symbols or words (£ -> GBP, $ -> USD, € -> EUR, naira -> NGN).
- If the client's email is not said, add "client_email" to missing_fields and
  ask for it in follow_up_question. Spelled-out emails ("kennedy at example dot
  com") become kennedy@example.com; list them in uncertain_fields.
- Relative dates ("due Friday", "in two weeks") -> due_in_days or due_date
  relative to today: {today}.
"""


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("audio", nargs="?", type=Path)
    p.add_argument("--text")
    args = p.parse_args()
    if not args.audio and not args.text:
        p.error("give an audio file or --text")

    settings = get_settings()
    settings.apply_google_env()
    client = genai.Client()

    parts: list[types.Part] = []
    if args.audio:
        mime = mimetypes.guess_type(args.audio.name)[0] or "audio/webm"
        parts.append(types.Part.from_bytes(data=args.audio.read_bytes(), mime_type=mime))
    else:
        parts.append(types.Part.from_text(text=args.text))

    started = time.perf_counter()
    resp = client.models.generate_content(
        model=settings.gemini_model,
        contents=[types.Content(role="user", parts=parts)],
        config=types.GenerateContentConfig(
            system_instruction=PROMPT.format(today=time.strftime("%A %Y-%m-%d")),
            response_mime_type="application/json",
            response_schema=ParsedJob,
            temperature=0,
        ),
    )
    elapsed = time.perf_counter() - started

    job = ParsedJob.model_validate_json(resp.text or "{}")
    print(job.model_dump_json(indent=2))
    currency = job.currency or "GBP"
    total = subtotal(((i.quantity, i.unit_price) for i in job.items), currency)
    print(f"\ntotal (computed in code): {currency} {total}   model={settings.gemini_model}")
    print(f"latency: {elapsed:.2f}s")


if __name__ == "__main__":
    main()
