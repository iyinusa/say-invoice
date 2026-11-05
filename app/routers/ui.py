"""The tradesperson's app: one page, Record -> Preview -> Send -> Sent.

Server-rendered shell plus Alpine.js for the screen-to-screen state, so there
is no SPA build. The client-facing pages (/q, /p) are separate templates.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.i18n import LANGUAGES
from app.services.invoicing import default_workspace
from app.services.policy import get_policy
from app.settings import Settings, get_settings
from app.voice.tts import effective_engine

APP_DIR = Path(__file__).resolve().parent.parent
STATIC_DIR = APP_DIR / "static"
templates = Jinja2Templates(directory=APP_DIR / "templates")

router = APIRouter(include_in_schema=False)


def _asset_version() -> str:
    """Changes whenever a static file does, so phones never run yesterday's JS."""
    digest = hashlib.sha256()
    for path in sorted(STATIC_DIR.rglob("*")):
        if path.is_file():
            digest.update(path.read_bytes())
    return digest.hexdigest()[:10]


ASSET_VERSION = _asset_version()


@router.get("/", response_class=HTMLResponse)
async def app_page(
    request: Request,
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> HTMLResponse:
    ws = await default_workspace(session, settings)
    policy = await get_policy(session, ws)
    return templates.TemplateResponse(
        request,
        "app.html",
        {
            "business_name": ws.business_name,
            "onboarded": ws.onboarded,
            "owner_name": ws.owner_name,
            "invoice_note": ws.invoice_note,
            "trade": ws.trade,
            "currency": ws.currency,
            "default_due_days": ws.default_due_days,
            "min_deposit": float(policy.min_deposit_pct),
            "tts_engine": effective_engine(settings.tts_engine),
            "languages": LANGUAGES,
            "v": ASSET_VERSION,
            "signed_in": bool(settings.owner_passcode),
        },
    )


@router.get("/manifest.webmanifest")
async def manifest(
    session: AsyncSession = Depends(get_session), settings: Settings = Depends(get_settings)
) -> JSONResponse:
    """Lets "Add to Home Screen" install SayInvoice as an app."""
    ws = await default_workspace(session, settings)
    icons = "/static/icons"
    return JSONResponse(
        {
            "name": f"SayInvoice · {ws.business_name}" if ws.onboarded else "SayInvoice",
            "short_name": "SayInvoice",
            "description": "Say a job, send a PayPal invoice.",
            "start_url": "/",
            "scope": "/",
            "display": "standalone",
            "orientation": "portrait",
            "background_color": "#f4f4f0",
            "theme_color": "#0b6b46",
            "icons": [
                {"src": f"{icons}/icon-192.png", "sizes": "192x192", "type": "image/png"},
                {"src": f"{icons}/icon-512.png", "sizes": "512x512", "type": "image/png"},
                {"src": f"{icons}/maskable-512.png", "sizes": "512x512", "type": "image/png",
                 "purpose": "maskable"},
                {"src": f"{icons}/icon.svg", "sizes": "any", "type": "image/svg+xml"},
            ],
        },
        media_type="application/manifest+json",
    )  # fmt: skip


@router.get("/sw.js")
async def service_worker() -> Response:
    """Served from the root so it can look after the whole app, not just /static."""
    body = (STATIC_DIR / "sw.js").read_text().replace("__VERSION__", ASSET_VERSION)
    return Response(
        body,
        media_type="text/javascript",
        headers={"Cache-Control": "no-cache", "Service-Worker-Allowed": "/"},
    )


def not_found_page(request: Request) -> HTMLResponse:
    """A friendly 404 for a client's quote or plan link that doesn't exist."""
    from app import i18n

    return templates.TemplateResponse(
        request, "quote_missing.html", {"v": ASSET_VERSION, "T": i18n.strings("en"), "lang": "en"},
        status_code=404,
    )  # fmt: skip
