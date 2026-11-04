"""Translate app/i18n/en.json into the client languages, once, with Gemini.

    uv run python -m scripts.translate_strings            # every language
    uv run python -m scripts.translate_strings --only es

Placeholders like {owner} must survive exactly: any translation that drops or
invents one is refused and the English kept. Review the output, then commit it;
the app never translates at run time.
"""

from __future__ import annotations

import argparse
import json
import re

from google import genai
from google.genai import types

from app.i18n import HERE, LANGUAGES
from app.settings import get_settings

PLACEHOLDER = re.compile(r"\{[a-z_]+\}")

PROMPT = """\
Translate these UI and message strings for a small trades business's invoicing
app into {language}. The reader is the business's client: write warmly and
plainly, as a local tradesperson would. Use the polite form where the language
has one. Keep every {{placeholder}} exactly as it is, and keep “quotes”, & and
emoji as they are. Return a JSON object with the same keys.

{strings}
"""


def _slots(text: str) -> list[str]:
    return sorted(PLACEHOLDER.findall(text))


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--only", nargs="+")
    args = p.parse_args()
    settings = get_settings()
    settings.apply_google_env()
    client = genai.Client()
    english = json.loads((HERE / "en.json").read_text(encoding="utf-8"))
    for code, name in LANGUAGES.items():
        if code == "en" or (args.only and code not in args.only):
            continue
        resp = client.models.generate_content(
            model=settings.gemini_model,
            contents=PROMPT.format(
                language=name, strings=json.dumps(english, ensure_ascii=False, indent=1)
            ),
            config=types.GenerateContentConfig(
                response_mime_type="application/json", temperature=0.2
            ),
        )
        got = json.loads(resp.text or "{}")
        out, refused = {}, []
        for key, text in english.items():
            local = got.get(key)
            if isinstance(local, str) and sorted(PLACEHOLDER.findall(local)) == sorted(
                PLACEHOLDER.findall(text)
            ):
                out[key] = local
            else:
                refused.append(key)  # English stays for this one
        (HERE / f"{code}.json").write_text(
            json.dumps(out, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        print(f"{code}: {len(out)} strings{f', kept English for {refused}' if refused else ''}")


if __name__ == "__main__":
    main()
