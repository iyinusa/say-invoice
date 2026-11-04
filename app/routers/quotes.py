"""Quotes: the owner's API, and the client's public page at /q/{token}.

The public routes have no login: the 128-bit token in the link is the
credential, and they only ever show or change that one quote.
"""

from __future__ import annotations

import time
from datetime import date
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import HTMLResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.deals import negotiator
from app.deals.agent import DealAgentUnavailable, DealModel, get_deal_model
from app.models import DealMessage, Quote, Workspace
from app.money import display, line_total, plain
from app.paypal import PayPalClient, PayPalError, get_paypal
from app.routers.invoices import current_workspace
from app.routers.ui import ASSET_VERSION, templates
from app.schemas.invoice import InvoiceOut
from app.schemas.quote import (
    AcceptIn,
    ClientMessageIn,
    DecisionIn,
    FinalIn,
    QuoteIn,
    QuoteOut,
)
from app.services import clock, final, invoicing, payments, quotes
from app.services import policy as policies
from app.settings import Settings, get_settings

router = APIRouter(prefix="/api/quotes", tags=["quotes"])
public = APIRouter(prefix="/q", tags=["client"], include_in_schema=False)

# Don't ask PayPal more than once every few seconds per quote, however often the page polls.
_last_refresh: dict[str, float] = {}
REFRESH_EVERY_S = 5.0


async def _today_of(session: AsyncSession, workspace_id: str) -> date:
    """The quote's workspace clock (the demo clock, if it has been moved)."""
    ws = await session.get(Workspace, workspace_id)
    return clock.today_for(ws)


# ── owner ────────────────────────────────────────────────────────────────────


@router.post("", response_model=QuoteOut, status_code=201)
async def create_quote(
    data: QuoteIn,
    session: AsyncSession = Depends(get_session),
    ws: Workspace = Depends(current_workspace),
    settings: Settings = Depends(get_settings),
) -> QuoteOut:
    today = clock.today_for(ws)
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


@router.get("/{quote_id}/messages")
async def owner_messages(
    quote_id: str,
    session: AsyncSession = Depends(get_session),
    ws: Workspace = Depends(current_workspace),
) -> list[dict[str, object]]:
    """The whole negotiation, including what the guard refused and why."""
    q = await quotes.get_quote(session, ws, quote_id)
    if q is None:
        raise HTTPException(404, "quote not found")
    return [
        {
            "id": m.id, "role": m.role, "text": m.text, "offer_status": m.offer_status,
            "offer": m.offer, "priced": m.priced, "violations": m.violations,
            "owner_summary": m.owner_summary, "created_at": m.created_at.isoformat(),
        }
        for m in await negotiator.history(session, q)
    ]  # fmt: skip


@router.post("/{quote_id}/messages/{message_id}/decision")
async def owner_decision(
    quote_id: str,
    message_id: str,
    data: DecisionIn,
    session: AsyncSession = Depends(get_session),
    ws: Workspace = Depends(current_workspace),
) -> dict[str, object]:
    """Approve / Decline / Counter on an escalated request."""
    q = await quotes.get_quote(session, ws, quote_id)
    if q is None:
        raise HTTPException(404, "quote not found")
    try:
        reply = await negotiator.owner_decision(
            session, q, message_id, data.action,  # type: ignore[arg-type]
            today=clock.today_for(ws), labour_discount_pct=data.labour_discount_pct,
            deposit_pct=data.deposit_pct, note=data.note,
        )  # fmt: skip
    except negotiator.OfferUnavailable as exc:
        raise HTTPException(409, str(exc).capitalize() + ".") from exc
    return {"status": q.status, "message": client_view(reply)}


@router.post("/{quote_id}/final-invoice", response_model=InvoiceOut, status_code=201)
async def send_final_invoice(
    quote_id: str,
    data: FinalIn,
    session: AsyncSession = Depends(get_session),
    ws: Workspace = Depends(current_workspace),
    paypal: PayPalClient = Depends(get_paypal),
) -> InvoiceOut:
    """Finish the job: the work as done, less the agreed discount and the deposit received."""
    q = await quotes.get_quote(session, ws, quote_id)
    if q is None:
        raise HTTPException(404, "quote not found")
    today = clock.today_for(ws)
    if data.due_date is not None and data.due_date < today:
        raise HTTPException(422, "The due date is in the past.")
    try:
        inv = await final.send_final(
            session, paypal, ws, q, client_name=data.client_name or q.client_name,
            client_email=str(data.client_email), items=data.items, due_date=data.due_date,
            notes=data.notes, transcript=data.transcript, draft=data.draft, today=today,
        )  # fmt: skip
    except final.AlreadyInvoiced as exc:
        # A retried tap after a dropped signal: hand back the invoice it already made.
        return invoicing.to_out(exc.invoice)
    except final.NothingToInvoice as exc:
        raise HTTPException(422, str(exc).capitalize() + ".") from exc
    except quotes.QuoteClosed as exc:
        raise HTTPException(409, str(exc).capitalize() + ".") from exc
    except PayPalError as exc:
        raise HTTPException(502, exc.human()) from exc
    return invoicing.to_out(inv)


jobs = APIRouter(prefix="/api/jobs", tags=["quotes"])


@jobs.get("/to-finish")
async def jobs_to_finish(
    session: AsyncSession = Depends(get_session), ws: Workspace = Depends(current_workspace)
) -> list[dict[str, object]]:
    return await final.jobs_to_finish(session, ws)


approvals = APIRouter(prefix="/api/approvals", tags=["quotes"])


@approvals.get("")
async def pending_approvals(
    session: AsyncSession = Depends(get_session), ws: Workspace = Depends(current_workspace)
) -> list[dict[str, object]]:
    """Requests waiting for the owner, for when the app opens (live ones arrive over SSE)."""
    from app.deals import plans
    from app.models import Invoice

    out = []
    # Payment-plan requests: invoices rather than quotes.
    for msg in await session.scalars(
        select(DealMessage)
        .where(DealMessage.workspace_id == ws.id, DealMessage.offer_status == "escalated",
               DealMessage.kind == "plan")
        .order_by(DealMessage.created_at)
    ):  # fmt: skip
        inv = await session.get(Invoice, msg.invoice_id)
        if inv is None:
            continue
        client_text = await session.scalar(
            select(DealMessage.text)
            .where(DealMessage.invoice_id == inv.id, DealMessage.role == "client",
                   DealMessage.created_at <= msg.created_at)
            .order_by(DealMessage.created_at.desc())
            .limit(1)
        )  # fmt: skip
        out.append(plans.approval_event(inv, msg, client_text or ""))
    rows = await session.execute(
        select(DealMessage, Quote)
        .join(Quote, Quote.id == DealMessage.quote_id)
        .where(DealMessage.workspace_id == ws.id, DealMessage.offer_status == "escalated")
        .order_by(DealMessage.created_at)
    )
    for msg, q in rows.all():
        client_text = await session.scalar(
            select(DealMessage.text)
            .where(DealMessage.quote_id == q.id, DealMessage.role == "client",
                   DealMessage.created_at <= msg.created_at)
            .order_by(DealMessage.created_at.desc())
            .limit(1)
        )  # fmt: skip
        out.append(negotiator.approval_event(q, msg, client_text or ""))
    return out


# ── client (public) ──────────────────────────────────────────────────────────


def client_view(m: DealMessage) -> dict[str, object]:
    """What the client may see of a message: never the guard's reasons or owner notes."""
    shown = m.offer_status in ("open", "accepted", "superseded") and m.priced is not None
    offer = None
    if shown and m.priced:
        p, cur = m.priced, m.priced["currency"]
        offer = {
            "lines": [{"name": ln["name"], "quantity": str(plain(Decimal(ln["quantity"]))),
                       "total": display(Decimal(ln["line_total"]), cur)} for ln in p["lines"]],
            "removed": p["removed"],
            "discount_pct": str(plain(Decimal(p["discount_pct"]))),
            "discount": display(Decimal(p["discount_amount"]), cur),
            "has_discount": Decimal(p["discount_amount"]) > 0,
            "total": display(Decimal(p["total"]), cur),
            "deposit_pct": str(plain(Decimal(p["deposit_pct"]))),
            "deposit": display(Decimal(p["deposit_amount"]), cur),
            "has_deposit": Decimal(p["deposit_amount"]) > 0,
            "job_date": p["job_date"],
        }  # fmt: skip
    return {
        "id": m.id,
        "role": m.role,
        "text": m.text,
        "offer_status": m.offer_status if shown else "none",
        "offer": offer,
    }


async def _conversation(session: AsyncSession, q: Quote) -> dict[str, object]:
    return {
        "status": q.status,
        "messages": [client_view(m) for m in await negotiator.history(session, q)],
    }


@public.get("/{token}/messages")
async def client_messages(
    token: str, session: AsyncSession = Depends(get_session)
) -> dict[str, object]:
    return await _conversation(session, await _quote_or_404(token, session))


@public.post("/{token}/messages")
async def post_client_message(
    token: str,
    data: ClientMessageIn,
    session: AsyncSession = Depends(get_session),
    model: DealModel = Depends(get_deal_model),
) -> dict[str, object]:
    q = await _quote_or_404(token, session)
    try:
        today = await _today_of(session, q.workspace_id)
        await negotiator.client_message(session, model, q, data.text, today=today)
    except negotiator.NegotiationClosed as exc:
        raise HTTPException(409, str(exc).capitalize() + ".") from exc
    except negotiator.TooManyMessages as exc:
        raise HTTPException(429, str(exc).capitalize() + ".") from exc
    except DealAgentUnavailable as exc:
        raise HTTPException(503, "Couldn't get an answer just now. Please try again.") from exc
    return await _conversation(session, q)


@public.post("/{token}/offers/{message_id}/accept")
async def accept_offer(
    token: str,
    message_id: str,
    data: AcceptIn,
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
    paypal: PayPalClient = Depends(get_paypal),
) -> dict[str, object]:
    """Accept a negotiated offer: the quote becomes the next version, then the deposit."""
    q = await _quote_or_404(token, session)
    today = await _today_of(session, q.workspace_id)
    if not data.email and not q.client_email:
        raise HTTPException(422, "Add your email so PayPal can send the deposit request.")
    try:
        await negotiator.apply_offer(session, q, message_id, today=today)
        await quotes.accept(session, paypal, q, data, today=today)
    except (negotiator.OfferUnavailable, negotiator.NegotiationClosed, quotes.QuoteClosed) as exc:
        raise HTTPException(409, str(exc).capitalize() + ".") from exc
    except PayPalError as exc:
        raise HTTPException(502, exc.human()) from exc
    return await _status(session, q, settings)


async def _quote_or_404(token: str, session: AsyncSession) -> Quote:
    q = await quotes.by_token(session, token)
    if q is None:
        raise HTTPException(404, "This quote link isn't valid.")
    return q


async def _status(session: AsyncSession, q: Quote, settings: Settings) -> dict[str, object]:
    out = quotes.to_out(q, await quotes.deposit_invoice(session, q), settings.public_base_url)
    deposit = out.deposit
    return {
        "status": "expired"
        if quotes.is_expired(q, await _today_of(session, q.workspace_id))
        else q.status,
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
            "discount_pct": plain(q.labour_discount_pct),
            "discount": display(quotes.list_total(q) - q.total, q.currency),
            "has_discount": quotes.list_total(q) > q.total,
            "owner": negotiator.first_name(ws) if ws else "the owner",
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
        await quotes.accept(
            session, paypal, q, data, today=await _today_of(session, q.workspace_id)
        )
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
