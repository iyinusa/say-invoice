"""Synthesise the parser eval's voice notes with Gemini TTS.

    uv run python -m scripts.make_eval_audio             # every spoken case, skips existing
    uv run python -m scripts.make_eval_audio --force --noise 0.04
    uv run python -m scripts.make_eval_audio --only bella-walks

Writes evals/audio/<id>.wav (24 kHz mono). Each case is read in the accent and
manner its `voice` field describes, with background hiss mixed in. These are
synthetic stand-ins: replace any file with a real phone recording of the same
sentence (converted to WAV) and the eval picks it up.
"""

from __future__ import annotations

import argparse
import array
import asyncio
import json
import random
import wave

from google import genai
from google.genai import types

from app.settings import get_settings
from scripts.eval_parser import AUDIO_DIR, CASES

# 2.5 reads the transcript faithfully; 3.8 ad-libs ("Lisa" became "Lucy"), which
# would make the eval measure the TTS instead of the parser.
TTS_MODEL = "gemini-2.5-flash-preview-tts"
VOICES = ["Kore", "Puck", "Charon", "Fenrir", "Aoede", "Leda", "Orus", "Zephyr"]
RATE = 24_000


def add_noise(pcm: bytes, level: float, seed: int) -> bytes:
    """Mix in background noise at `level` of full scale (0.03 is a busy street)."""
    samples = array.array("h", pcm)
    rng = random.Random(seed)  # noqa: S311 - test noise, not security
    peak = int(32767 * level)
    for i, s in enumerate(samples):
        samples[i] = max(-32768, min(32767, s + int(rng.gauss(0, peak))))
    return samples.tobytes()


def write_wav(path: object, pcm: bytes) -> None:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(pcm)


async def synth(client: genai.Client, text: str, voice: str, direction: str) -> bytes:
    resp = await client.aio.models.generate_content(
        model=TTS_MODEL,
        # A plain "Say this as ...:" prefix gets read aloud; this layout doesn't.
        contents=(
            f"# AUDIO PROFILE\n{direction}, leaving a quick voice note on a phone.\n\n"
            f"### DIRECTOR'S NOTES\nAccent and manner: {direction}. Natural pace. "
            f"Only the transcript below is spoken.\n\n#### TRANSCRIPT\n{text}"
        ),
        config=types.GenerateContentConfig(
            response_modalities=["AUDIO"],
            speech_config=types.SpeechConfig(
                voice_config=types.VoiceConfig(
                    prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=voice)
                )
            ),
        ),
    )
    part = resp.candidates[0].content.parts[0]  # type: ignore[index,union-attr]
    if not part.inline_data or not part.inline_data.data:
        raise RuntimeError("TTS returned no audio")
    return part.inline_data.data


async def main(args: argparse.Namespace) -> None:
    settings = get_settings()
    settings.apply_google_env()
    client = genai.Client()
    AUDIO_DIR.mkdir(parents=True, exist_ok=True)
    cases = [
        c
        for c in json.loads(CASES.read_text())["cases"]
        if not c.get("typed") and (not args.only or c["id"] in args.only)
    ]
    gate = asyncio.Semaphore(4)

    async def one(n: int, case: dict[str, str]) -> None:
        out = AUDIO_DIR / f"{case['id']}.wav"
        if out.exists() and not args.force:
            return
        async with gate:
            pcm = await synth(client, case["say"], VOICES[n % len(VOICES)], case["voice"])
        if args.noise:
            pcm = add_noise(pcm, args.noise, seed=n)
        write_wav(out, pcm)
        print(f"{case['id']}: {len(pcm) / 2 / RATE:.1f}s")

    await asyncio.gather(*(one(n, c) for n, c in enumerate(cases)))


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--only", nargs="+")
    p.add_argument("--force", action="store_true")
    p.add_argument("--noise", type=float, default=0.02, help="0 for clean audio")
    asyncio.run(main(p.parse_args()))
