"""Owner sign-in: one passcode, then a signed cookie.

SayInvoice has one owner per deployment, so there are no accounts: the owner types
`SI_OWNER_PASSCODE` once and gets a 30-day cookie, an HMAC of its issue time. The
key is derived from the passcode (plus `SI_SESSION_SECRET`, or the PayPal secret),
so changing the passcode signs every device out.

What stays public: the client's quote and plan pages (`/q/`, `/p/`, each behind a
128-bit token), PayPal's webhook (verified by signature), health, static files and
the sign-in page itself. Everything else, the app and every other `/api` route,
needs the cookie. Locally, with no passcode set, the app is open; in production a
missing passcode stops the server from starting.
"""

from __future__ import annotations

import hashlib
import hmac
import time
from http.cookies import SimpleCookie
from urllib.parse import urlsplit

from starlette.types import ASGIApp, Receive, Scope, Send

from app.settings import Settings, get_settings

COOKIE = "si_owner"
MAX_AGE = 30 * 24 * 3600
PUBLIC_PREFIXES = ("/q/", "/p/", "/static/", "/api/webhooks/")
PUBLIC_PATHS = {"/healthz", "/login", "/logout", "/manifest.webmanifest", "/sw.js", "/favicon.ico"}
UNSAFE = {"POST", "PUT", "PATCH", "DELETE"}


def required(settings: Settings) -> bool:
    return bool(settings.owner_passcode)


def _key(settings: Settings) -> bytes:
    secret = settings.session_secret or settings.paypal_client_secret
    material = f"sayinvoice-owner-v1\0{settings.owner_passcode}\0{secret}".encode()
    return hashlib.sha256(material).digest()


def _sign(settings: Settings, issued: str) -> str:
    return hmac.new(_key(settings), issued.encode(), hashlib.sha256).hexdigest()


def passcode_ok(settings: Settings, attempt: str) -> bool:
    # Compare digests, so neither the length nor the content leaks through timing.
    want = hashlib.sha256(settings.owner_passcode.encode()).digest()
    got = hashlib.sha256(attempt.strip().encode()).digest()
    return bool(settings.owner_passcode) and hmac.compare_digest(want, got)


def issue(settings: Settings, now: float | None = None) -> str:
    issued = str(int(now if now is not None else time.time()))
    return f"{issued}.{_sign(settings, issued)}"


def valid(settings: Settings, value: str | None, now: float | None = None) -> bool:
    if not value or "." not in value:
        return False
    issued, sig = value.split(".", 1)
    if not issued.isdigit() or not hmac.compare_digest(sig, _sign(settings, issued)):
        return False
    age = (now if now is not None else time.time()) - int(issued)
    return 0 <= age <= MAX_AGE


def is_public(path: str) -> bool:
    return path in PUBLIC_PATHS or path.startswith(PUBLIC_PREFIXES)


def _header(scope: Scope, name: bytes) -> str:
    for key, value in scope.get("headers", []):
        if key == name:
            return value.decode("latin-1")
    return ""


def cookie_from(scope: Scope) -> str | None:
    raw = _header(scope, b"cookie")
    if not raw:
        return None
    jar: SimpleCookie = SimpleCookie()
    try:
        jar.load(raw)
    except Exception:  # a malformed Cookie header is simply "not signed in"
        return None
    morsel = jar.get(COOKIE)
    return morsel.value if morsel else None


def same_origin(scope: Scope) -> bool:
    """For unsafe methods: a browser's Origin (when sent) must be this site."""
    origin = _header(scope, b"origin")
    if not origin or origin == "null":
        return not origin  # no Origin (curl, same-origin GET-like) is fine; "null" is not
    host = _header(scope, b"x-forwarded-host") or _header(scope, b"host")
    return urlsplit(origin).netloc == host


class OwnerAuth:
    """Pure ASGI, so streaming responses (the live-updates SSE) pass through untouched."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        settings = get_settings()
        path: str = scope["path"]
        method: str = scope["method"]
        owner_route = not is_public(path)
        if owner_route and method in UNSAFE and not same_origin(scope):
            await _respond(send, 403, b'{"detail":"Cross-site request refused."}')
            return
        if not owner_route or not required(settings) or valid(settings, cookie_from(scope)):
            await self.app(scope, receive, send)
            return
        if path.startswith("/api/") or path.startswith("/openapi") or method != "GET":
            await _respond(send, 401, b'{"detail":"Sign in first."}')
            return
        headers = [(b"location", b"/login"), (b"cache-control", b"no-store")]
        await send({"type": "http.response.start", "status": 303, "headers": headers})
        await send({"type": "http.response.body", "body": b""})


async def _respond(send: Send, status: int, body: bytes) -> None:
    await send({"type": "http.response.start", "status": status,
                "headers": [(b"content-type", b"application/json"),
                            (b"content-length", str(len(body)).encode())]})  # fmt: skip
    await send({"type": "http.response.body", "body": body})
