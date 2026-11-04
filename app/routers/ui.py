"""The tradesperson's app: one page, Record -> Preview -> Send -> Sent.

Server-rendered shell plus Alpine.js for the screen-to-screen state, so there
is no SPA build. The client-facing pages (/q, /p) are separate templates.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.services.invoicing import default_workspace
from app.settings import Settings, get_settings

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
    return templates.TemplateResponse(
        request,
        "app.html",
        {
            "business_name": ws.business_name,
            "currency": ws.currency,
            "default_due_days": ws.default_due_days,
            "v": ASSET_VERSION,
        },
    )
