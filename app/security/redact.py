"""Keep personal data out of the logs.

App log lines carry ids and amounts by design. This is the safety net for what
we don't write ourselves: error strings from PayPal or Gemini that echo an email,
and uvicorn's access log, which would otherwise record the read-back sentence in
`/api/tts?text=...` and the secret client links `/q/<token>`, `/p/<token>`.
"""

from __future__ import annotations

import logging
import re
from typing import Any

EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
# 10+ digits (phones, card-like runs), but not inside an id: "...-446655440000" is a UUID tail.
PHONE = re.compile(r"(?<![\w.-])\+?\d[\d ()-]{8,}\d(?![\w.-])")
TOKEN_PATH = re.compile(r"/(q|p)/[A-Za-z0-9_-]{8,}")
QUERY = re.compile(r"\?[^\s\"]*")


def _number(m: re.Match[str]) -> str:
    return "[number]" if sum(c.isdigit() for c in m.group()) >= 10 else m.group()  # not a date


def scrub(text: str) -> str:
    text = EMAIL.sub("[email]", text)
    text = TOKEN_PATH.sub(r"/\1/[token]", text)
    return PHONE.sub(_number, text)


def scrub_access(text: str) -> str:
    return scrub(QUERY.sub("?[…]", text))


def structlog_processor(_logger: Any, _name: str, event: dict[str, Any]) -> dict[str, Any]:
    for key, value in event.items():
        if isinstance(value, str) and key != "timestamp":
            event[key] = scrub(value)
    return event


class AccessLogFilter(logging.Filter):
    """uvicorn.access: drop query strings and mask client-link tokens."""

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.args, tuple) and len(record.args) >= 3:
            args = list(record.args)
            args[2] = scrub_access(str(args[2]))  # (client, method, path, http_version, status)
            record.args = tuple(args)
        else:
            record.msg = scrub_access(str(record.msg))
        return True
