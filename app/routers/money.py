"""The money view: "Who owes me", job pages, and the owner's invoice actions."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.models import Workspace
from app.paypal import PayPalClient, PayPalError, get_paypal
from app.routers.invoices import current_workspace
from app.schemas.invoice import InvoiceOut
from app.services import invoice_actions as actions
from app.services import invoicing, money_view, quotes
from app.settings import Settings, get_settings

router = APIRouter(prefix="/api", tags=["money"])


def today() -> date:
    return datetime.now(UTC).date()


@router.get("/home")
async def who_owes_me(
    session: AsyncSession = Depends(get_session), ws: Workspace = Depends(current_workspace)
) -> dict[str, object]:
    return await money_view.home(session, ws, today())


@router.get("/jobs/invoice/{invoice_id}")
async def invoice_job(
    invoice_id: str,
    session: AsyncSession = Depends(get_session),
    ws: Workspace = Depends(current_workspace),
) -> dict[str, object]:
    inv = await invoicing.get_invoice(session, ws, invoice_id)
    if inv is None:
        raise HTTPException(404, "invoice not found")
    return await money_view.invoice_detail(session, ws, inv, today())


@router.get("/jobs/quote/{quote_id}")
async def quote_job(
    quote_id: str,
    session: AsyncSession = Depends(get_session),
    ws: Workspace = Depends(current_workspace),
    settings: Settings = Depends(get_settings),
) -> dict[str, object]:
    q = await quotes.get_quote(session, ws, quote_id)
    if q is None:
        raise HTTPException(404, "quote not found")
    return await money_view.quote_detail(session, ws, q, today(), settings.public_base_url)


class RemindIn(BaseModel):
    note: str = Field(default="", max_length=1000)


class PaymentIn(BaseModel):
    amount: Decimal | None = Field(default=None, gt=0, decimal_places=2)
    method: str = "CASH"
    note: str = Field(default="", max_length=500)


class CancelIn(BaseModel):
    note: str = Field(default="", max_length=500)


async def _invoice(session: AsyncSession, ws: Workspace, invoice_id: str):  # type: ignore[no-untyped-def]
    inv = await invoicing.get_invoice(session, ws, invoice_id)
    if inv is None:
        raise HTTPException(404, "invoice not found")
    return inv


def _refused(exc: Exception) -> HTTPException:
    return HTTPException(409, str(exc)[:1].upper() + str(exc)[1:] + ".")


@router.post("/invoices/{invoice_id}/remind")
async def remind_now(
    invoice_id: str,
    data: RemindIn,
    session: AsyncSession = Depends(get_session),
    ws: Workspace = Depends(current_workspace),
    paypal: PayPalClient = Depends(get_paypal),
) -> dict[str, str]:
    inv = await _invoice(session, ws, invoice_id)
    try:
        await actions.remind(session, paypal, ws, inv, note=data.note)
    except actions.ActionRefused as exc:
        raise _refused(exc) from exc
    except PayPalError as exc:
        raise HTTPException(502, exc.human()) from exc
    return {"status": "sent"}


@router.post("/invoices/{invoice_id}/record-payment", response_model=InvoiceOut)
async def mark_paid(
    invoice_id: str,
    data: PaymentIn,
    session: AsyncSession = Depends(get_session),
    ws: Workspace = Depends(current_workspace),
    paypal: PayPalClient = Depends(get_paypal),
) -> InvoiceOut:
    inv = await _invoice(session, ws, invoice_id)
    try:
        await actions.record_payment(session, paypal, ws, inv, amount=data.amount,
                                     method=data.method, note=data.note, today=today())  # fmt: skip
    except actions.ActionRefused as exc:
        raise _refused(exc) from exc
    except PayPalError as exc:
        raise HTTPException(502, exc.human()) from exc
    return invoicing.to_out(inv)


@router.post("/invoices/{invoice_id}/cancel", response_model=InvoiceOut)
async def cancel_invoice(
    invoice_id: str,
    data: CancelIn,
    session: AsyncSession = Depends(get_session),
    ws: Workspace = Depends(current_workspace),
    paypal: PayPalClient = Depends(get_paypal),
) -> InvoiceOut:
    inv = await _invoice(session, ws, invoice_id)
    try:
        await actions.cancel(session, paypal, ws, inv, note=data.note)
    except actions.ActionRefused as exc:
        raise _refused(exc) from exc
    except PayPalError as exc:
        raise HTTPException(502, exc.human()) from exc
    return invoicing.to_out(inv)


@router.post("/invoices/{invoice_id}/retry", response_model=InvoiceOut)
async def retry_send(
    invoice_id: str,
    session: AsyncSession = Depends(get_session),
    ws: Workspace = Depends(current_workspace),
    paypal: PayPalClient = Depends(get_paypal),
) -> InvoiceOut:
    inv = await _invoice(session, ws, invoice_id)
    try:
        await actions.retry(session, paypal, ws, inv, today=today())
    except actions.ActionRefused as exc:
        raise _refused(exc) from exc
    except PayPalError as exc:
        raise HTTPException(502, exc.human()) from exc
    return invoicing.to_out(inv)
