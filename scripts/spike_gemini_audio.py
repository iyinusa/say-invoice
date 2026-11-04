"""Spike 3: one multimodal Gemini call turns a voice note into a checked preview card.

    uv run python -m scripts.spike_gemini_audio path/to/voice-note.m4a
    uv run python -m scripts.spike_gemini_audio --text "Fixed Mr Kennedy's boiler, 2h at £60..."
    uv run python -m scripts.spike_gemini_audio note.m4a --answer "kennedy@example.com"

Record the voice note on your phone (Voice Memos / Recorder) to test real
accents and background noise. Runs the same parser as `/api/parse`, prints
the card (amounts computed in code) and the latency. `--answer` replies to
the card's follow-up question, like the UI does.
"""

from __future__ import annotations

import argparse
import asyncio
import mimetypes
import time
from datetime import date
from pathlib import Path

from app.agents.parser import JobParser, gemini_extractor
from app.settings import get_settings


async def main(args: argparse.Namespace) -> None:
    settings = get_settings()
    if not settings.gemini_configured:
        raise SystemExit("set SI_GOOGLE_API_KEY (or SI_VERTEX_PROJECT) in .env first")
    parser = JobParser(gemini_extractor(settings))

    audio = args.audio.read_bytes() if args.audio else None
    mime = (mimetypes.guess_type(args.audio.name)[0] or "audio/webm") if args.audio else ""
    if mime == "audio/x-m4a":
        mime = "audio/mp4"

    started = time.perf_counter()
    card = await parser.parse(
        today=date.today(),
        default_currency=settings.default_currency,
        text=args.text or "",
        audio=audio,
        audio_mime=mime,
        mode=args.mode,
    )
    print(card.model_dump_json(indent=2, exclude={"draft"}))
    print(f"\nlatency: {time.perf_counter() - started:.2f}s   model={settings.gemini_model}")

    if args.answer:
        started = time.perf_counter()
        card = await parser.parse(
            today=date.today(),
            default_currency=settings.default_currency,
            text=args.answer,
            mode=args.mode,
            previous=card.draft,
        )
        print("\n── after the answer ──")
        print(card.model_dump_json(indent=2, exclude={"draft"}))
        print(f"\nlatency: {time.perf_counter() - started:.2f}s")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("audio", nargs="?", type=Path)
    p.add_argument("--text")
    p.add_argument("--mode", choices=["auto", "invoice", "quote"], default="auto")
    p.add_argument("--answer", help="reply to the follow-up question")
    a = p.parse_args()
    if not a.audio and not a.text:
        p.error("give an audio file or --text")
    asyncio.run(main(a))
