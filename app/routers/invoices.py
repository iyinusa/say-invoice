"""Invoices: send one from a confirmed card, read it back, show its pay-by-QR code."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Response
from fastapi.responses import JSONResponse
from fastapi.sse import EventSourceResponse, ServerSentEvent
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session, get_sessionmaker
from app.models import Workspace
from app.paypal import PayPalClient, PayPalError, get_paypal
from app.schemas.invoice import InvoiceIn, InvoiceOut
from app.services import invoicing, payments
from app.services.live import broker
from app.settings import Settings, get_settings

router = APIRouter(prefix="/api/invoices", tags=["invoices"])
events_router = APIRouter(prefix="/api", tags=["live"])


async def current_workspace(
    session: AsyncSession = Depends(get_session), settings: Settings = Depends(get_settings)
) -> Workspace:
    return await invoicing.default_workspace(session, settings)


@router.post("", response_model=InvoiceOut, status_code=201)
async def send_invoice(
    data: InvoiceIn,
    session: AsyncSession = Depends(get_session),
    ws: Workspace = Depends(current_workspace),
    paypal: PayPalClient = Depends(get_paypal),
) -> InvoiceOut | JSONResponse:
    # Server-side "today" in UTC; a day either way only matters for a past-due check.
    today = datetime.now(UTC).date()
    if data.due_date is not None and data.due_date < today - timedelta(days=1):
        raise HTTPException(422, "The due date is in the past.")
    try:
        inv = await invoicing.create_and_send(session, paypal, ws, data, today=today)
    except invoicing.InvoiceConflict as exc:
        raise HTTPException(409, str(exc)) from exc
    except PayPalError as exc:
        # The draft is saved; the same idempotency key retries from where it stopped.
        return JSONResponse({"detail": exc.human(), "retryable": exc.status >= 500}, 502)
    return invoicing.to_out(inv)


@router.get("/{invoice_id}", response_model=InvoiceOut)
async def read_invoice(
    invoice_id: str,
    session: AsyncSession = Depends(get_session),
    ws: Workspace = Depends(current_workspace),
) -> InvoiceOut:
    inv = await invoicing.get_invoice(session, ws, invoice_id)
    if inv is None:
        raise HTTPException(404, "invoice not found")
    return invoicing.to_out(inv)


@router.post("/{invoice_id}/refresh", response_model=InvoiceOut)
async def refresh_invoice(
    invoice_id: str,
    session: AsyncSession = Depends(get_session),
    ws: Workspace = Depends(current_workspace),
    paypal: PayPalClient = Depends(get_paypal),
) -> InvoiceOut:
    """Ask PayPal now. The Sent screen calls this while it waits, in case webhooks are slow."""
    inv = await invoicing.get_invoice(session, ws, invoice_id)
    if inv is None:
        raise HTTPException(404, "invoice not found")
    try:
        await payments.refresh(session, paypal, inv)
    except PayPalError as exc:
        raise HTTPException(502, exc.human()) from exc
    return invoicing.to_out(inv)


@router.get("/{invoice_id}/qr.png", response_class=Response)
async def invoice_qr(
    invoice_id: str,
    session: AsyncSession = Depends(get_session),
    ws: Workspace = Depends(current_workspace),
    paypal: PayPalClient = Depends(get_paypal),
) -> Response:
    inv = await invoicing.get_invoice(session, ws, invoice_id)
    if inv is None:
        raise HTTPException(404, "invoice not found")
    try:
        png = await invoicing.qr_png(session, paypal, inv)
    except invoicing.InvoiceNotPayable as exc:
        raise HTTPException(409, "Send the invoice first.") from exc
    except PayPalError as exc:
        raise HTTPException(502, exc.human()) from exc
    return Response(png, media_type="image/png", headers={"Cache-Control": "private, max-age=3600"})


async def _workspace_id(settings: Settings = Depends(get_settings)) -> str:
    # Its own short session: a request-scoped one would hold a pooled database
    # connection open for as long as the phone keeps the stream open.
    async with get_sessionmaker()() as session:
        return (await invoicing.default_workspace(session, settings)).id


@events_router.get("/events", response_class=EventSourceResponse)
async def live_events(workspace_id: str = Depends(_workspace_id)) -> AsyncIterator[ServerSentEvent]:
    """Server-Sent Events: "invoice" whenever one changes (paid, part-paid, cancelled).

    FastAPI adds a keepalive comment every 15 s, so proxies keep the stream open.
    """
    with broker.subscribe(workspace_id) as queue:
        yield ServerSentEvent(event="hello", data={"workspace": workspace_id}, retry=3000)
        while True:
            event = await queue.get()
            yield ServerSentEvent(event=event["type"], data=event)
