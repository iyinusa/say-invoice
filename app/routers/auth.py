"""GET/POST /login and POST /logout for the owner's passcode (see app.security.auth)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from app.logging_config import get_logger
from app.routers.ui import ASSET_VERSION, templates
from app.security import auth
from app.security.ratelimit import LOGIN
from app.settings import Settings, get_settings

router = APIRouter(include_in_schema=False)
log = get_logger("auth")


def _page(request: Request, error: str = "", status: int = 200) -> HTMLResponse:
    return templates.TemplateResponse(
        request, "login.html", {"v": ASSET_VERSION, "error": error}, status_code=status,
        headers={"Cache-Control": "no-store"},
    )  # fmt: skip


@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, settings: Settings = Depends(get_settings)) -> Response:
    if not auth.required(settings) or auth.valid(settings, request.cookies.get(auth.COOKIE)):
        return RedirectResponse("/", status_code=303)
    return _page(request)


@router.post("/login", dependencies=[Depends(LOGIN)])
async def login(
    request: Request, passcode: str = Form("", max_length=200),
    settings: Settings = Depends(get_settings),
) -> Response:  # fmt: skip
    if not auth.required(settings):
        return RedirectResponse("/", status_code=303)
    if not auth.passcode_ok(settings, passcode):
        log.warning("login_failed")
        return _page(request, "That passcode isn't right.", status=401)
    resp = RedirectResponse("/", status_code=303)
    resp.set_cookie(
        auth.COOKIE, auth.issue(settings), max_age=auth.MAX_AGE, httponly=True, samesite="lax",
        secure=settings.is_production or request.url.scheme == "https", path="/",
    )  # fmt: skip
    log.info("login_ok")
    return resp


@router.post("/logout")
async def logout() -> Response:
    resp = RedirectResponse("/login", status_code=303)
    resp.delete_cookie(auth.COOKIE, path="/")
    return resp
