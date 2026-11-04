"""The client's language: fixed texts on the client's pages, in messages and in reminders.

English is the source (`en.json`). The other languages were translated once
with Gemini by `scripts/translate_strings.py` (which refuses any translation
that changes a {placeholder}) and committed, so nothing is translated at run time.
Missing keys fall back to English.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
# What the card's "Client's language" menu offers, in each language's own name.
LANGUAGES = {
    "en": "English",
    "es": "Español",
    "fr": "Français",
    "de": "Deutsch",
    "it": "Italiano",
    "pt": "Português",
    "pl": "Polski",
}


def base(lang: str | None) -> str:
    """'es-MX' -> 'es'; anything we don't have -> 'en'."""
    code = (lang or "en").split("-")[0].lower()
    return code if code in LANGUAGES else "en"


@lru_cache
def strings(lang: str) -> dict[str, str]:
    english = json.loads((HERE / "en.json").read_text(encoding="utf-8"))
    code = base(lang)
    if code == "en":
        return english
    path = HERE / f"{code}.json"
    local = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    return {**english, **local}


def t(lang: str | None, key: str, **values: Any) -> str:
    text = strings(base(lang)).get(key, key)
    return text.format(**values) if values else text


def fmt_date(d: Any, lang: str | None, *, weekday: bool = False, year: bool = False) -> str:
    """'Wed 4 Nov 2026' in English; '04.11.2026' style elsewhere (no English month names)."""
    if d is None:
        return ""
    if base(lang) == "en":
        out = f"{d:%a} {d.day} {d:%b}" if weekday else f"{d.day} {d:%b}"
        return f"{out} {d.year}" if year else out
    return f"{d:%d.%m.%Y}" if year else f"{d:%d.%m}"
