"""Quotes: the owner's API, and the client's public page at /q/{token}.

The public routes have no login: the 128-bit token in the link is the
credential, and they only ever show or change that one quote.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import HTMLResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.models import Quote, Workspace
from app.money import display, line_total, plain
from app.paypal import PayPalClient, PayPalError, get_paypal
from app.routers.invoices import current_workspace
from app.routers.ui import ASSET_VERSION, templates
from app.schemas.quote import AcceptIn, QuoteIn, QuoteOut
from app.services import invoicing, payments, quotes
from app.services import policy as policies
from app.settings import Settings, get_settings

router = APIRouter(prefix="/api/quotes", tags=["quotes"])
public = APIRouter(prefix="/q", tags=["client"], include_in_schema=False)

# Don't ask PayPal more than once every few seconds per quote, however often the page polls.
_last_refresh: dict[str, float] = {}
REFRESH_EVERY_S = 5.0


def _today() -> datetime:
    return datetime.now(UTC)


# ── owner ────────────────────────────────────────────────────────────────────


@router.post("", response_model=QuoteOut, status_code=201)
async def create_quote(
    data: QuoteIn,
    session: AsyncSession = Depends(get_session),
    ws: Workspace = Depends(current_workspace),
    settings: Settings = Depends(get_settings),
) -> QuoteOut:
    today = _today().date()
    if data.job_date is not None and data.job_date < today:
        raise HTTPException(422, "The job date is in the past.")
    policy = await policies.get_policy(session, ws)
    q = await quotes.create_quote(session, ws, policy, data, today=today)
    return quotes.to_out(q, await quotes.deposit_invoice(session, q), settings.public_base_url)


@router.get("/{quote_id}", response_model=QuoteOut)
async def read_quote(
    quote_id: str,
    session: AsyncSession = Depends(get_session),
    ws: Workspace = Depends(current_workspace),
    settings: Settings = Depends(get_settings),
) -> QuoteOut:
    q = await quotes.get_quote(session, ws, quote_id)
    if q is None:
        raise HTTPException(404, "quote not found")
    return quotes.to_out(q, await quotes.deposit_invoice(session, q), settings.public_base_url)


# ── client (public) ──────────────────────────────────────────────────────────


async def _quote_or_404(token: str, session: AsyncSession) -> Quote:
    q = await quotes.by_token(session, token)
    if q is None:
        raise HTTPException(404, "This quote link isn't valid.")
    return q


async def _status(session: AsyncSession, q: Quote, settings: Settings) -> dict[str, object]:
    out = quotes.to_out(q, await quotes.deposit_invoice(session, q), settings.public_base_url)
    deposit = out.deposit
    return {
        "status": "expired" if quotes.is_expired(q, _today().date()) else q.status,
        "email": q.client_email,
        "deposit_required": out.deposit_amount > 0,
        "deposit_amount": display(out.deposit_amount, q.currency),
        "deposit": None
        if deposit is None
        else {
            "status": deposit.status,
            "pay_url": deposit.pay_url,
            "paid": display(deposit.amount_paid, q.currency),
        },
    }


@public.get("/{token}", response_class=HTMLResponse)
async def quote_page(
    token: str,
    request: Request,
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> HTMLResponse:
    q = await quotes.by_token(session, token)
    if q is None:
        return templates.TemplateResponse(
            request, "quote_missing.html", {"v": ASSET_VERSION}, status_code=404
        )
    ws = await session.get(Workspace, q.workspace_id)
    deposit = quotes.deposit_amount(q)
    lines = [
        {
            "name": ln.name,
            "description": ln.description,
            "qty": plain(ln.quantity),
            "unit": ln.unit,
            "price": display(ln.unit_amount, q.currency),
            "total": display(line_total(ln.quantity, ln.unit_amount, q.currency), q.currency),
            "is_material": ln.is_material,
        }
        for ln in q.lines
    ]
    return templates.TemplateResponse(
        request,
        "quote.html",
        {
            "v": ASSET_VERSION,
            "q": q,
            "business_name": ws.business_name if ws else "",
            "lines": lines,
            "total": display(q.total, q.currency),
            "deposit": display(deposit, q.currency),
            "balance": display(q.total - deposit, q.currency),
            "deposit_pct": plain(q.deposit_pct),
            "has_deposit": deposit > 0,
            "initial": await _status(session, q, settings),
        },
        headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"},
    )


@public.get("/{token}/status")
async def quote_status(
    token: str,
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
    paypal: PayPalClient = Depends(get_paypal),
) -> dict[str, object]:
    """Polled by the quote page while the deposit is outstanding."""
    q = await _quote_or_404(token, session)
    inv = await quotes.deposit_invoice(session, q)
    now = time.monotonic()
    if (
        inv is not None
        and inv.status in payments.OPEN
        and now - _last_refresh.get(q.id, 0) > REFRESH_EVERY_S
    ):
        _last_refresh[q.id] = now
        try:
            await payments.refresh(session, paypal, inv)
        except PayPalError:
            pass  # show the last known state; the reconcile loop will catch up
    return await _status(session, q, settings)


@public.post("/{token}/accept")
async def accept_quote(
    token: str,
    data: AcceptIn,
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
    paypal: PayPalClient = Depends(get_paypal),
) -> dict[str, object]:
    q = await _quote_or_404(token, session)
    try:
        await quotes.accept(session, paypal, q, data, today=_today().date())
    except quotes.QuoteClosed as exc:
        raise HTTPException(409, str(exc).capitalize() + ".") from exc
    except quotes.EmailNeeded as exc:
        raise HTTPException(422, str(exc).capitalize() + ".") from exc
    except PayPalError as exc:
        # Accepted and saved; tapping the button again retries the deposit invoice.
        raise HTTPException(502, exc.human()) from exc
    return await _status(session, q, settings)


@public.get("/{token}/deposit-qr.png", response_class=Response)
async def deposit_qr(
    token: str,
    session: AsyncSession = Depends(get_session),
    paypal: PayPalClient = Depends(get_paypal),
) -> Response:
    q = await _quote_or_404(token, session)
    inv = await quotes.deposit_invoice(session, q)
    if inv is None:
        raise HTTPException(409, "Accept the quote first.")
    try:
        png = await invoicing.qr_png(session, paypal, inv)
    except invoicing.InvoiceNotPayable as exc:
        raise HTTPException(409, "Nothing to pay right now.") from exc
    except PayPalError as exc:
        raise HTTPException(502, exc.human()) from exc
    return Response(png, media_type="image/png", headers={"Cache-Control": "private, max-age=3600"})
