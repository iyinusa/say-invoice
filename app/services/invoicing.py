"""Create and send a PayPal invoice from a confirmed preview card.

The order matters. The invoice is saved as a draft first, then created at
PayPal, then sent, committing after each step. A retry (the same idempotency
key, e.g. a double tap or a dropped connection) resumes from where it stopped,
and PayPal-Request-Id makes each PayPal call safe to repeat. A job is never
invoiced twice.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from datetime import date, timedelta
from decimal import Decimal
from typing import Any, Protocol

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.logging_config import get_logger
from app.models import Client, Event, Invoice, LineItem, Workspace
from app.models.base import utcnow
from app.money import line_total, subtotal, to_money
from app.paypal.client import PayPalError
from app.paypal.invoices import InvoiceDraft, Item, Party, UnitOfMeasure, build_invoice_body
from app.schemas.invoice import InvoiceIn, InvoiceItemOut, InvoiceOut
from app.settings import Settings

log = get_logger("invoicing")

# Statuses PayPal will make a pay-by-QR code for.
PAYABLE = {"sent", "partially_paid", "overdue"}


class PayPalInvoicing(Protocol):
    """The slice of `PayPalClient` this module uses (a fake in tests)."""

    async def next_invoice_number(self) -> str: ...
    async def create_draft_invoice(
        self, body: Mapping[str, Any], *, request_id: str | None = None
    ) -> dict[str, Any]: ...
    async def send_invoice(self, invoice_id: str, *, request_id: str | None = None) -> str: ...
    async def generate_qr_code(self, invoice_id: str, *, size: int = 500) -> bytes: ...


class InvoiceConflict(Exception):
    """Same key, different content, and the first version already exists at PayPal."""


class InvoiceNotPayable(Exception):
    """No QR code yet: the invoice hasn't been sent (or is paid or cancelled)."""


# ── workspace + client ───────────────────────────────────────────────────────


async def default_workspace(session: AsyncSession, settings: Settings) -> Workspace:
    """The single workspace until onboarding and demo workspaces exist."""
    ws = await session.scalar(select(Workspace).order_by(Workspace.created_at).limit(1))
    if ws is None:
        ws = Workspace(
            business_name=settings.business_name,
            owner_name=settings.owner_name,
            owner_email=settings.paypal_merchant_email,
            currency=settings.default_currency,
            default_due_days=settings.default_due_days,
        )
        session.add(ws)
        await session.commit()
    elif settings.paypal_merchant_email and ws.owner_email != settings.paypal_merchant_email:
        # Credentials added after the first run: PayPal only accepts the merchant's email.
        ws.owner_email = settings.paypal_merchant_email
        await session.commit()
    return ws


async def _client_for(session: AsyncSession, ws: Workspace, name: str, email: str) -> Client:
    email = email.lower()
    client = await session.scalar(
        select(Client).where(Client.workspace_id == ws.id, Client.email == email)
    )
    if client is None:
        client = Client(workspace_id=ws.id, name=name, email=email)
        session.add(client)
    elif name:
        client.name = name
    return client


# ── create + send ────────────────────────────────────────────────────────────


def payload_hash(data: InvoiceIn) -> str:
    """What makes two Send taps "the same invoice": everything the client will see."""
    content = data.model_dump(mode="json", exclude={"idempotency_key", "draft", "transcript"})
    return hashlib.sha256(json.dumps(content, sort_keys=True).encode()).hexdigest()


def resolve_due_date(data: InvoiceIn, ws: Workspace, today: date) -> date | None:
    if data.due_date is not None:
        return data.due_date
    return today + timedelta(days=ws.default_due_days) if ws.default_due_days > 0 else None


def _fill(inv: Invoice, data: InvoiceIn, client: Client, ws: Workspace, today: date) -> None:
    inv.client = client
    inv.currency = data.currency
    inv.due_date = resolve_due_date(data, ws, today)
    inv.note = data.notes
    inv.transcript = data.transcript
    inv.raw_ai_json = data.draft
    inv.payload_hash = payload_hash(data)
    inv.items = [
        LineItem(
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
    inv.total = subtotal(((i.quantity, i.unit_amount) for i in inv.items), data.currency)


async def create_and_send(
    session: AsyncSession,
    paypal: PayPalInvoicing,
    ws: Workspace,
    data: InvoiceIn,
    *,
    today: date,
) -> Invoice:
    inv = await session.scalar(
        select(Invoice).where(
            Invoice.workspace_id == ws.id, Invoice.idempotency_key == data.idempotency_key
        )
    )
    if inv is not None and inv.status != "draft":
        return inv  # already sent: this is a retry of a tap that worked
    if inv is None:
        client = await _client_for(session, ws, data.client_name, str(data.client_email))
        inv = Invoice(workspace_id=ws.id, idempotency_key=data.idempotency_key)
        _fill(inv, data, client, ws, today)
        session.add(inv)
        await session.flush()
        session.add(Event(workspace_id=ws.id, invoice_id=inv.id, kind="invoice_created"))
        await session.commit()
    elif inv.payload_hash != payload_hash(data):
        if inv.paypal_invoice_id:
            raise InvoiceConflict("that invoice already exists at PayPal; start a new one")
        # Edited after a failed send, before PayPal ever saw it: replace the draft
        # (the old lines go with delete-orphan).
        client = await _client_for(session, ws, data.client_name, str(data.client_email))
        _fill(inv, data, client, ws, today)
        await session.commit()
    return await push_to_paypal(session, paypal, ws, inv, today=today)


def to_paypal_draft(inv: Invoice, ws: Workspace, today: date) -> InvoiceDraft:
    def uom(unit: str, qty: Decimal) -> UnitOfMeasure:
        if unit == "hours":
            return "HOURS"
        return "AMOUNT" if unit == "fixed" and qty == 1 else "QUANTITY"

    return InvoiceDraft(
        number=inv.number,
        currency=inv.currency,
        items=[
            Item(
                name=i.name,
                quantity=i.quantity,
                unit_amount=i.unit_amount,
                description=i.description,
                unit_of_measure=uom(i.unit, i.quantity),
            )
            for i in inv.items
        ],
        recipient=Party(inv.client.name, inv.client.email),
        invoicer=Party(ws.owner_name, ws.owner_email, ws.business_name),
        invoice_date=today,
        due_date=inv.due_date,
        note=inv.note,
    )


async def push_to_paypal(
    session: AsyncSession, paypal: PayPalInvoicing, ws: Workspace, inv: Invoice, *, today: date
) -> Invoice:
    # The content hash is part of the request id: an edited retry is a new request.
    request_prefix = f"{inv.idempotency_key}-{inv.payload_hash[:12]}"
    try:
        if not inv.paypal_invoice_id:
            if not inv.number:
                inv.number = await paypal.next_invoice_number()
                await session.commit()
            body = build_invoice_body(to_paypal_draft(inv, ws, today))
            created = await paypal.create_draft_invoice(body, request_id=f"{request_prefix}-create")
            inv.paypal_invoice_id = str(created["id"])
            await session.commit()
        href = await paypal.send_invoice(inv.paypal_invoice_id, request_id=f"{request_prefix}-send")
    except PayPalError as exc:
        log.warning("paypal_send_failed", invoice_id=inv.id, error=str(exc), debug_id=exc.debug_id)
        inv.last_error = exc.human()[:500]
        session.add(
            Event(
                workspace_id=ws.id,
                invoice_id=inv.id,
                kind="paypal_error",
                data={"status": exc.status, "name": exc.name, "debug_id": exc.debug_id},
            )
        )
        await session.commit()
        raise

    inv.status = "sent"
    inv.payer_view_url = href
    inv.sent_at = utcnow()
    inv.last_error = ""
    session.add(
        Event(
            workspace_id=ws.id,
            invoice_id=inv.id,
            kind="invoice_sent",
            data={"paypal_invoice_id": inv.paypal_invoice_id, "total": str(inv.total)},
        )
    )
    await session.commit()
    log.info("invoice_sent", invoice_id=inv.id, number=inv.number, total=str(inv.total))
    return inv


# ── read ─────────────────────────────────────────────────────────────────────


async def get_invoice(session: AsyncSession, ws: Workspace, invoice_id: str) -> Invoice | None:
    return await session.scalar(
        select(Invoice).where(Invoice.id == invoice_id, Invoice.workspace_id == ws.id)
    )


async def qr_png(session: AsyncSession, paypal: PayPalInvoicing, inv: Invoice) -> bytes:
    """The doorstep "scan to pay" code, fetched from PayPal once and then cached."""
    await session.refresh(inv, ["qr_png"])
    if inv.qr_png:
        return inv.qr_png
    if inv.status not in PAYABLE or not inv.paypal_invoice_id:
        raise InvoiceNotPayable(f"invoice is {inv.status}")
    inv.qr_png = await paypal.generate_qr_code(inv.paypal_invoice_id)
    await session.commit()
    return inv.qr_png


def to_out(inv: Invoice) -> InvoiceOut:
    return InvoiceOut(
        id=inv.id,
        number=inv.number,
        status=inv.status,
        client_name=inv.client.name,
        client_email=inv.client.email,
        currency=inv.currency,
        total=inv.total,
        amount_paid=inv.amount_paid,
        due_date=inv.due_date,
        note=inv.note,
        payer_view_url=inv.payer_view_url,
        items=[
            InvoiceItemOut(
                name=i.name,
                description=i.description,
                quantity=i.quantity,
                unit_price=i.unit_amount,
                unit=i.unit,
                is_material=i.is_material,
                line_total=line_total(i.quantity, i.unit_amount, inv.currency),
            )
            for i in inv.items
        ],
        created_at=inv.created_at,
        sent_at=inv.sent_at,
        last_error=inv.last_error,
    )
