"""Per-IP sliding-window rate limits, in memory.

One Render instance serves SayInvoice, so a process-local limiter is the honest
size. It guards what costs money or invites guessing: Gemini parses, the client
chat (Deal and Plan agents), the server voice, and sign-in attempts.
"""

from __future__ import annotations

import math
import time
from collections import deque
from collections.abc import Callable

from fastapi import HTTPException, Request

MAX_KEYS = 20_000  # beyond this, forget everyone: memory stays bounded under a flood


class RateLimiter:
    def __init__(self) -> None:
        self._hits: dict[str, deque[float]] = {}

    def hit(self, key: str, limit: int, window: float, now: float | None = None) -> float:
        """Count one hit. Returns 0 if allowed, else the seconds until the next one is."""
        now = time.monotonic() if now is None else now
        if len(self._hits) > MAX_KEYS:
            self._hits.clear()
        q = self._hits.setdefault(key, deque())
        while q and q[0] <= now - window:
            q.popleft()
        if len(q) >= limit:
            return max(q[0] + window - now, 0.001)
        q.append(now)
        return 0.0

    def reset(self) -> None:
        self._hits.clear()


limiter = RateLimiter()


def client_ip(request: Request) -> str:
    # uvicorn runs with --proxy-headers, so behind Render this is the caller, not the proxy.
    return request.client.host if request.client else "unknown"


def rate_limit(name: str, *rules: tuple[int, float]) -> Callable[[Request], None]:
    """A FastAPI dependency: e.g. rate_limit("parse", (30, 60), (300, 3600))."""

    def check(request: Request) -> None:
        ip = client_ip(request)
        wait = max(limiter.hit(f"{name}:{ip}:{window}", limit, window) for limit, window in rules)
        if wait:
            raise HTTPException(
                429,
                "Too many tries. Please wait a moment and try again.",
                headers={"Retry-After": str(math.ceil(wait))},
            )

    return check


# The limits, in one place.
PARSE = rate_limit("parse", (30, 60), (300, 3600))
CLIENT_CHAT = rate_limit("client_chat", (10, 60), (60, 3600))
CLIENT_ACTION = rate_limit("client_action", (20, 60))
TTS = rate_limit("tts", (60, 60))
LOGIN = rate_limit("login", (5, 60), (30, 3600))
