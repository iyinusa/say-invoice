"""Measure the parser: accuracy per field and per card, plus latency.

    uv run python -m scripts.eval_parser                          # text, model from .env
    uv run python -m scripts.eval_parser --model gemini-2.5-flash --model gemini-3.5-flash
    uv run python -m scripts.eval_parser --audio                  # evals/audio/*.wav voice notes
    uv run python -m scripts.eval_parser --only bella-walks -v

Runs every case in evals/parser_cases.json through the same `JobParser` as
/api/parse (prompt, Gemini call, then the code rules) and scores the final
card, so what it measures is what the tradesperson would see. Results go to
evals/results/<model>-<text|audio>.json; the summary is printed as Markdown
for the README.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import statistics
import time
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

from app.agents.parser import JobParser, ParserUnavailable, gemini_extractor
from app.schemas.job import ParseResult
from app.settings import get_settings

ROOT = Path(__file__).resolve().parent.parent
CASES = ROOT / "evals" / "parser_cases.json"
AUDIO_DIR = ROOT / "evals" / "audio"
RESULTS_DIR = ROOT / "evals" / "results"

FIELDS = (
    "mode", "client_name", "client_email", "currency", "language", "items",
    "subtotal", "due_date", "deposit_pct", "deposit_amount", "missing", "ready",
)  # fmt: skip


def _dec(v: Any) -> Decimal | None:
    return None if v is None else Decimal(str(v))


def _name_key(s: str) -> str:
    s = re.sub(r"[^\w\s]", "", s.lower())
    return " ".join(w for w in s.split() if w not in {"mr", "mrs", "ms", "miss", "the"})


def _items_key(items: list[Any]) -> list[tuple[Decimal, Decimal]]:
    return sorted((Decimal(str(q)), Decimal(str(p))) for q, p, *_ in items)


def score_field(name: str, expected: Any, card: ParseResult) -> bool:
    """One field of one card. Lenient only where a human reader would be."""
    match name:
        case "client_name":
            got, want = _name_key(card.client_name), _name_key(expected)
            return bool(got and want) and (want in got or got in want)
        case "language":
            return card.language.split("-")[0].lower() == expected.lower()
        case "items":
            got = [(i.quantity, i.unit_price, i.is_material) for i in card.items]
            if _items_key(got) != _items_key(expected):
                return False
            # Materials flag, where the case cares: match each expected row to its line.
            by_amount = {(q, p): m for q, p, m in got}
            return all(
                m is None or by_amount.get((Decimal(str(q)), Decimal(str(p)))) == m
                for q, p, m in expected
            )
        case "subtotal" | "deposit_pct" | "deposit_amount":
            return _dec(getattr(card, name)) == _dec(expected)
        case "due_date":
            got = card.due_date.isoformat() if card.due_date else None
            return got == expected
        case "missing":
            return card.missing_fields == expected
        case _:
            return getattr(card, name) == expected


@dataclass
class CaseRun:
    id: str
    source: str
    latency_s: float
    error: str = ""
    fields: dict[str, bool] = field(default_factory=dict)
    card: dict[str, Any] | None = None

    @property
    def correct(self) -> bool:
        return not self.error and all(self.fields.values())


async def run_case(
    parser: JobParser, case: dict[str, Any], *, today: date, currency: str, audio: bool
) -> CaseRun:
    wav = AUDIO_DIR / f"{case['id']}.wav"
    use_audio = audio and not case.get("typed") and wav.exists()
    started = time.perf_counter()
    try:
        card = await parser.parse(
            today=today,
            default_currency=currency,
            text="" if use_audio else case["say"],
            audio=wav.read_bytes() if use_audio else None,
            audio_mime="audio/wav" if use_audio else "",
            mode=case.get("mode", "auto"),
        )
    except ParserUnavailable as exc:
        cause = exc.__cause__
        return CaseRun(
            case["id"], "audio" if use_audio else "text", time.perf_counter() - started,
            error=repr(cause or exc)[:300], fields=dict.fromkeys(case["expect"], False),
        )  # fmt: skip
    run = CaseRun(case["id"], "audio" if use_audio else "text", time.perf_counter() - started)
    run.fields = {k: score_field(k, v, card) for k, v in case["expect"].items()}
    run.card = card.model_dump(mode="json", exclude={"draft"})
    return run


def _pct(n: int, d: int) -> str:
    return f"{100 * n / d:.0f}%" if d else "n/a"


def summarise(model: str, runs: list[CaseRun]) -> dict[str, Any]:
    per_field: dict[str, list[bool]] = {f: [] for f in FIELDS}
    for r in runs:
        for f, ok in r.fields.items():
            per_field[f].append(ok)
    lat = sorted(r.latency_s for r in runs if not r.error) or [0.0]
    return {
        "model": model,
        "cases": len(runs),
        "cards_correct": sum(r.correct for r in runs),
        "errors": sum(bool(r.error) for r in runs),
        "fields": {f: [sum(v), len(v)] for f, v in per_field.items() if v},
        "latency_p50_s": round(statistics.median(lat), 2),
        "latency_p95_s": round(lat[min(len(lat) - 1, int(0.95 * len(lat)))], 2),
    }


def print_report(summaries: list[dict[str, Any]], runs_by_model: dict[str, list[CaseRun]],
                 verbose: bool) -> None:  # fmt: skip
    for s in summaries:
        print(f"\n## {s['model']}\n")
        seen_errors: set[tuple[str, str]] = set()
        for r in runs_by_model[s["model"]]:
            if r.correct and not verbose:
                continue
            if r.error:
                if (r.id, r.error) not in seen_errors:
                    seen_errors.add((r.id, r.error))
                    print(f"- **{r.id}** ({r.source}): error {r.error}")
                continue
            bad = [k for k, ok in r.fields.items() if not ok]
            print(f"- **{r.id}** ({r.source}, {r.latency_s:.1f}s): wrong {bad or 'nothing'}")
            if bad or verbose:
                card = r.card or {}
                got = {k: card.get(k) for k in ("client_name", "client_email", "currency",
                       "due_date", "subtotal", "missing_fields", "ready")}  # fmt: skip
                got["items"] = [(i["quantity"], i["unit_price"], i["is_material"])
                                for i in card.get("items", [])]  # fmt: skip
                print(f"    got: {json.dumps(got, ensure_ascii=False)}")

    print("\n| Model | Cards fully right | " + " | ".join(FIELDS) + " | p50 | p95 |")
    print("|---|---|" + "---|" * len(FIELDS) + "---|---|")
    for s in summaries:
        cells = [_pct(*s["fields"][f]) if f in s["fields"] else "" for f in FIELDS]
        print(
            f"| {s['model']} | {s['cards_correct']}/{s['cases']} "
            f"({_pct(s['cards_correct'], s['cases'])}) | "
            + " | ".join(cells)
            + f" | {s['latency_p50_s']}s | {s['latency_p95_s']}s |"
        )


def _cases() -> dict[str, Any]:
    return json.loads(CASES.read_text())


async def main(args: argparse.Namespace) -> None:
    settings = get_settings()
    if not settings.gemini_configured:
        raise SystemExit("set SI_GOOGLE_API_KEY (or SI_VERTEX_PROJECT) in .env first")
    data = _cases()
    cases = [c for c in data["cases"] if not args.only or c["id"] in args.only]
    today = date.fromisoformat(data["today"])
    models = args.model or [settings.gemini_model]
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)

    summaries, runs_by_model = [], {}
    for model in models:
        update = {"gemini_model": model, "gemini_thinking_level": args.thinking}
        parser = JobParser(gemini_extractor(settings.model_copy(update=update)))
        gate = asyncio.Semaphore(args.concurrency)

        async def one(
            case: dict[str, Any], parser: JobParser = parser, gate: asyncio.Semaphore = gate
        ) -> CaseRun:
            async with gate:
                return await run_case(
                    parser, case, today=today, currency=data["default_currency"], audio=args.audio
                )

        runs = await asyncio.gather(*(one(c) for c in cases * args.repeat))
        tag = f"{model}{'-' + args.thinking if args.thinking else ''}"
        summary = summarise(tag, runs)
        summaries.append(summary)
        runs_by_model[tag] = runs
        out = RESULTS_DIR / f"{tag}-{'audio' if args.audio else 'text'}.json"
        out.write_text(
            json.dumps(
                {"summary": summary, "runs": [r.__dict__ for r in runs]},
                indent=2,
                ensure_ascii=False,
                default=str,
            )  # fmt: skip
        )
    print_report(summaries, runs_by_model, args.verbose)


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--model", action="append", help="repeat to compare models")
    p.add_argument("--audio", action="store_true", help="use evals/audio/<id>.wav when present")
    p.add_argument("--only", nargs="+", help="case ids")
    p.add_argument("--thinking", default="", help="minimal|low|medium|high")
    p.add_argument("--repeat", type=int, default=1, help="run each case N times")
    p.add_argument("--concurrency", type=int, default=4)
    p.add_argument("-v", "--verbose", action="store_true", help="show passing cases too")
    asyncio.run(main(p.parse_args()))
