"""Payment plans: the client's page at /p/{token} and the owner's decisions on plan requests."""

from __future__ import annotations

import time
from datetime import date
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.deals import negotiator, plans
from app.deals.agent import DealAgentUnavailable, get_plan_model
from app.models import DealMessage, Invoice, Workspace
from app.money import display
from app.paypal import PayPalClient, PayPalError, get_paypal
from app.routers.invoices import current_workspace
from app.routers.ui import ASSET_VERSION, templates
from app.schemas.quote import ClientMessageIn, DecisionIn
from app.services import clock, payments
from app.services.money_view import OPEN, balance, days_overdue, state
from app.services.plan_links import current_due

public = APIRouter(prefix="/p", tags=["client"], include_in_schema=False)
owner = APIRouter(prefix="/api/invoices", tags=["plans"])

_last_refresh: dict[str, float] = {}


async def _today_of(session: AsyncSession, inv: Invoice) -> date:
    ws = await session.get(Workspace, inv.workspace_id)
    return clock.today_for(ws)


async def _by_token(session: AsyncSession, token: str) -> Invoice | None:
    if not token or len(token) > 40:
        return None
    return await session.scalar(select(Invoice).where(Invoice.public_token == token))


async def _invoice_or_404(session: AsyncSession, token: str) -> Invoice:
    inv = await _by_token(session, token)
    if inv is None:
        raise HTTPException(404, "This payment link isn't valid.")
    return inv


def _schedule(priced: dict[str, object] | None, cur: str) -> list[dict[str, str]]:
    if not priced:
        return []
    return [
        {
            "due": f"{date.fromisoformat(i['due']):%a %-d %b}",
            "amount": display(Decimal(i["amount"]), cur),
        }
        for i in priced["instalments"]  # type: ignore[union-attr]
    ]


def client_view(m: DealMessage, cur: str) -> dict[str, object]:
    shown = m.offer_status in ("open", "accepted", "superseded") and m.priced is not None
    return {
        "id": m.id,
        "role": m.role,
        "text": m.text,
        "offer_status": m.offer_status if shown else "none",
        "plan": _schedule(m.priced, cur) if shown else [],
    }


async def _state(session: AsyncSession, inv: Invoice) -> dict[str, object]:
    msgs = await plans.history(session, inv)
    today = await _today_of(session, inv)
    st = state(inv, today)
    agreed = []
    if inv.plan:
        covered = Decimal(0)
        for row in inv.plan["instalments"]:
            covered += Decimal(row["amount"])
            due = date.fromisoformat(row["due"])
            late = "late" if due < today else "upcoming"
            agreed.append(
                {
                    "due": f"{due:%a %-d %b}",
                    "amount": display(Decimal(row["amount"]), inv.currency),
                    "status": "paid" if inv.amount_paid >= covered else late,
                }
            )
    return {
        "status": "awaiting_owner" if plans.awaiting_owner(msgs) else st,
        "balance": display(balance(inv), inv.currency),
        "paid": display(inv.amount_paid, inv.currency),
        "has_paid": inv.amount_paid > 0,
        "days_overdue": days_overdue(inv, today),
        "pay_url": inv.payer_view_url,
        "agreed": agreed,
        "messages": [client_view(m, inv.currency) for m in msgs],
    }


@public.get("/{token}", response_class=HTMLResponse)
async def plan_page(
    token: str, request: Request, session: AsyncSession = Depends(get_session)
) -> HTMLResponse:
    inv = await _by_token(session, token)
    if inv is None:
        return templates.TemplateResponse(
            request, "quote_missing.html", {"v": ASSET_VERSION}, status_code=404
        )
    ws = await session.get(Workspace, inv.workspace_id)
    due = current_due(inv)
    return templates.TemplateResponse(
        request,
        "plan.html",
        {
            "v": ASSET_VERSION,
            "inv": inv,
            "business_name": ws.business_name if ws else "",
            "owner": negotiator.first_name(ws) if ws else "the owner",
            "total": display(inv.total, inv.currency),
            "due": f"{due:%-d %b %Y}" if due else "on receipt",
            "items": [
                {"name": i.name, "amount": display(i.quantity * i.unit_amount, inv.currency)}
                for i in inv.items
            ],  # fmt: skip
            "initial": await _state(session, inv),
        },
        headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"},
    )


@public.get("/{token}/status")
async def plan_status(
    token: str,
    session: AsyncSession = Depends(get_session),
    paypal: PayPalClient = Depends(get_paypal),
) -> dict[str, object]:
    inv = await _invoice_or_404(session, token)
    now = time.monotonic()
    if inv.status in OPEN and now - _last_refresh.get(inv.id, 0) > 5:
        _last_refresh[inv.id] = now
        try:
            await payments.refresh(session, paypal, inv)
        except PayPalError:
            pass
    return await _state(session, inv)


@public.post("/{token}/messages")
async def post_message(
    token: str,
    data: ClientMessageIn,
    session: AsyncSession = Depends(get_session),
    model: object = Depends(get_plan_model),
) -> dict[str, object]:
    inv = await _invoice_or_404(session, token)
    try:
        await plans.client_message(
            session, model, inv, data.text, today=await _today_of(session, inv)
        )  # type: ignore[arg-type]
    except negotiator.NegotiationClosed as exc:
        raise HTTPException(409, str(exc).capitalize() + ".") from exc
    except negotiator.TooManyMessages as exc:
        raise HTTPException(429, str(exc).capitalize() + ".") from exc
    except DealAgentUnavailable as exc:
        raise HTTPException(503, "Couldn't get an answer just now. Please try again.") from exc
    return await _state(session, inv)


@public.post("/{token}/plans/{message_id}/accept")
async def accept_plan(
    token: str,
    message_id: str,
    session: AsyncSession = Depends(get_session),
    paypal: PayPalClient = Depends(get_paypal),
) -> dict[str, object]:
    inv = await _invoice_or_404(session, token)
    try:
        await plans.accept(session, paypal, inv, message_id, today=await _today_of(session, inv))
    except (negotiator.OfferUnavailable, negotiator.NegotiationClosed) as exc:
        raise HTTPException(409, str(exc).capitalize() + ".") from exc
    except PayPalError as exc:
        raise HTTPException(502, exc.human()) from exc
    return await _state(session, inv)


@owner.post("/{invoice_id}/plan-messages/{message_id}/decision")
async def plan_decision(
    invoice_id: str,
    message_id: str,
    data: DecisionIn,
    session: AsyncSession = Depends(get_session),
    ws: Workspace = Depends(current_workspace),
) -> dict[str, object]:
    inv = await session.get(Invoice, invoice_id)
    if inv is None or inv.workspace_id != ws.id:
        raise HTTPException(404, "invoice not found")
    try:
        reply = await plans.owner_decision(
            session, inv, message_id, data.action, today=clock.today_for(ws), note=data.note
        )
    except negotiator.OfferUnavailable as exc:
        raise HTTPException(409, str(exc).capitalize() + ".") from exc
    return {"status": "ok", "message": client_view(reply, inv.currency)}
