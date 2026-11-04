"""Quotes: created from the preview card, opened by the client at /q/{token}.

Accepting a quote locks exactly what was agreed (a snapshot, not a pointer to
lines that might change), then creates and sends a PayPal deposit invoice
through the same idempotent path as every other invoice. Retrying an accept
(a double tap, a dropped signal) never takes the deposit twice.
"""

from __future__ import annotations

import secrets
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.logging_config import get_logger
from app.models import (
    Client,
    DealMessage,
    DealPolicy,
    Event,
    Invoice,
    Quote,
    QuoteLine,
    Workspace,
)
from app.models.base import utcnow
from app.money import line_total, percent_of, plain, subtotal, to_money
from app.schemas.invoice import InvoiceIn, InvoiceItemIn, InvoiceItemOut
from app.schemas.quote import AcceptIn, DepositOut, QuoteIn, QuoteOut
from app.services import invoicing
from app.services.invoicing import PayPalInvoicing
from app.services.live import broker

log = get_logger("quotes")

OPEN = ("sent", "negotiating", "awaiting_owner")


class QuoteClosed(Exception):
    """Declined or expired: it can't be accepted any more."""


class EmailNeeded(Exception):
    """PayPal needs somewhere to send the deposit request."""


def deposit_amount(q: Quote) -> Decimal:
    return percent_of(q.total, q.deposit_pct, q.currency)


def list_total(q: Quote) -> Decimal:
    """The lines at full price; `q.total` is after any negotiated labour discount."""
    return subtotal(((ln.quantity, ln.unit_amount) for ln in q.lines), q.currency)


def public_url(q: Quote, base_url: str) -> str:
    return f"{base_url.rstrip('/')}/q/{q.public_token}"


def is_expired(q: Quote, today: date) -> bool:
    return q.status in OPEN and q.valid_until < today


async def _client(
    session: AsyncSession, ws: Workspace, name: str, email: str, language: str = ""
) -> Client:
    email = email.lower()
    client = await session.scalar(
        select(Client).where(Client.workspace_id == ws.id, Client.email == email)
    )
    if client is None:
        client = Client(workspace_id=ws.id, name=name, email=email, language=language or "en")
        session.add(client)
    else:
        if name:
            client.name = name
        if language:
            client.language = language
    await session.flush()
    return client


async def create_quote(
    session: AsyncSession, ws: Workspace, policy: DealPolicy, data: QuoteIn, *, today: date
) -> Quote:
    existing = await session.scalar(
        select(Quote).where(
            Quote.workspace_id == ws.id, Quote.idempotency_key == data.idempotency_key
        )
    )
    if existing is not None:
        return existing  # a double tap on "Create quote link"

    count = await session.scalar(select(func.count()).where(Quote.workspace_id == ws.id)) or 0
    email = str(data.client_email).lower() if data.client_email else ""
    client = await _client(session, ws, data.client_name, email, data.language) if email else None
    lines = [
        QuoteLine(
            position=n,
            name=item.name,
            description=item.description,
            quantity=item.quantity,
            unit_amount=to_money(item.unit_price, data.currency),
            unit=item.unit,
            is_material=item.is_material,
        )
        for n, item in enumerate(data.items)
    ]
    q = Quote(
        workspace_id=ws.id,
        client_id=client.id if client else None,
        idempotency_key=data.idempotency_key,
        public_token=secrets.token_urlsafe(16),
        number=f"Q-{count + 1:04d}",
        client_name=data.client_name,
        client_email=email,
        language=data.language,
        currency=data.currency,
        total=subtotal(((ln.quantity, ln.unit_amount) for ln in lines), data.currency),
        # Not said: the owner's own minimum from the Deal Policy.
        deposit_pct=Decimal(
            data.deposit_pct if data.deposit_pct is not None else policy.min_deposit_pct
        ).quantize(Decimal("0.01")),
        job_date=data.job_date,
        valid_until=today + timedelta(days=data.valid_days),
        note=data.notes,
        transcript=data.transcript,
        raw_ai_json=data.draft,
        lines=lines,
    )
    session.add(q)
    await session.flush()
    session.add(Event(workspace_id=ws.id, kind="quote_created", data={"quote_id": q.id}))
    await session.commit()
    log.info("quote_created", quote_id=q.id, number=q.number, total=str(q.total))
    return q


async def get_quote(session: AsyncSession, ws: Workspace, quote_id: str) -> Quote | None:
    return await session.scalar(
        select(Quote).where(Quote.id == quote_id, Quote.workspace_id == ws.id)
    )


async def by_token(session: AsyncSession, token: str) -> Quote | None:
    if not token or len(token) > 40:
        return None
    return await session.scalar(select(Quote).where(Quote.public_token == token))


async def deposit_invoice(session: AsyncSession, q: Quote) -> Invoice | None:
    return await session.scalar(
        select(Invoice)
        .where(Invoice.quote_id == q.id, Invoice.kind == "deposit")
        .order_by(Invoice.created_at.desc())
        .limit(1)
    )


def snapshot(q: Quote) -> dict[str, Any]:
    return {
        "version": q.version,
        "currency": q.currency,
        "lines": [
            {
                "name": ln.name,
                "quantity": str(ln.quantity),
                "unit_price": str(ln.unit_amount),
                "is_material": ln.is_material,
            }
            for ln in q.lines
        ],
        "list_total": str(list_total(q)),
        "labour_discount_pct": str(q.labour_discount_pct),
        "discount_amount": str(list_total(q) - q.total),
        "total": str(q.total),
        "deposit_pct": str(q.deposit_pct),
        "deposit_amount": str(deposit_amount(q)),
        "job_date": q.job_date.isoformat() if q.job_date else None,
    }


def quote_event(q: Quote, deposit: Invoice | None) -> dict[str, Any]:
    return {
        "type": "quote",
        "id": q.id,
        "number": q.number,
        "status": q.status,
        "deposit_status": deposit.status if deposit else None,
    }


async def accept(
    session: AsyncSession,
    paypal: PayPalInvoicing,
    q: Quote,
    data: AcceptIn,
    *,
    today: date,
) -> Quote:
    if q.status == "declined":
        raise QuoteClosed("this quote was declined")
    if is_expired(q, today):
        q.status = "expired"
        await session.commit()
        raise QuoteClosed("this quote has expired")

    ws = await session.get(Workspace, q.workspace_id)
    assert ws is not None
    if q.status != "accepted":
        email = str(data.email) if data.email else q.client_email
        if not email:
            raise EmailNeeded("add your email so PayPal can send the deposit request")
        client = await _client(session, ws, data.name or q.client_name, email, q.language)
        q.client_id = client.id
        q.client_email = client.email
        q.client_name = data.name or q.client_name
        q.status = "accepted"
        q.accepted_at = utcnow()
        # Accepting closes the negotiation: no offer or owner request stays live.
        for m in await session.scalars(
            select(DealMessage).where(
                DealMessage.quote_id == q.id, DealMessage.offer_status.in_(("open", "escalated"))
            )
        ):
            m.offer_status = "superseded"
        q.agreed_terms = snapshot(q)
        session.add(
            Event(
                workspace_id=ws.id, kind="quote_accepted", data={"quote_id": q.id, **q.agreed_terms}
            )
        )
        await session.commit()
        broker.publish(ws.id, quote_event(q, None))
        log.info("quote_accepted", quote_id=q.id, number=q.number)

    amount = deposit_amount(q)
    if amount <= 0:
        return q  # no deposit asked for: booked on acceptance
    existing = await deposit_invoice(session, q)
    if existing is not None and existing.status != "draft":
        return q
    # Same key every time: a retried accept resumes the same deposit invoice.
    work = ", ".join(ln.name for ln in q.lines)
    deposit = InvoiceIn(
        idempotency_key=f"deposit-{q.id}",
        language=q.language,
        client_name=q.client_name,
        client_email=q.client_email,
        currency=q.currency,
        items=[
            InvoiceItemIn(
                name=f"Deposit ({plain(q.deposit_pct)}%) for quote {q.number}",
                description=work[:1000],
                quantity=Decimal(1),
                unit_price=amount,
                unit="fixed",
            )
        ],
        due_date=today,
        notes=(
            f"Deposit to book the work in quote {q.number}. "
            "The balance is invoiced when the job is done."
        ),
    )
    inv = await invoicing.create_and_send(
        session, paypal, ws, deposit, today=today, kind="deposit", quote_id=q.id
    )
    broker.publish(ws.id, quote_event(q, inv))
    return q


def to_out(q: Quote, deposit: Invoice | None, base_url: str) -> QuoteOut:
    return QuoteOut(
        id=q.id,
        number=q.number,
        status=q.status,
        version=q.version,
        client_name=q.client_name,
        client_email=q.client_email,
        currency=q.currency,
        items=[
            InvoiceItemOut(
                name=ln.name,
                description=ln.description,
                quantity=ln.quantity,
                unit_price=ln.unit_amount,
                unit=ln.unit,
                is_material=ln.is_material,
                line_total=line_total(ln.quantity, ln.unit_amount, q.currency),
            )
            for ln in q.lines
        ],
        total=q.total,
        list_total=list_total(q),
        labour_discount_pct=q.labour_discount_pct,
        discount_amount=list_total(q) - q.total,
        deposit_pct=q.deposit_pct,
        deposit_amount=deposit_amount(q),
        job_date=q.job_date,
        valid_until=q.valid_until,
        note=q.note,
        public_url=public_url(q, base_url),
        created_at=q.created_at,
        accepted_at=q.accepted_at,
        deposit=DepositOut(
            invoice_id=deposit.id,
            status=deposit.status,
            amount=deposit.total,
            amount_paid=deposit.amount_paid,
            pay_url=deposit.payer_view_url,
        )
        if deposit
        else None,
    )
