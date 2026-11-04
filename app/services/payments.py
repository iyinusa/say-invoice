"""Keep our invoices in step with PayPal: webhooks first, a reconcile loop as backup.

Every path (webhook, the 60-second reconcile loop, the phone's "check now")
re-reads the invoice from PayPal and applies what it says. A webhook body is
never trusted on its own, so a replayed or out-of-order delivery can't move an
invoice backwards or mark it paid.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from decimal import Decimal, InvalidOperation
from typing import Any, Protocol

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.logging_config import get_logger
from app.models import Event, Invoice
from app.models.base import utcnow
from app.paypal.client import PayPalError
from app.services.live import broker

log = get_logger("payments")

# PayPal invoice status -> ours. Statuses not listed leave ours unchanged.
STATUS_MAP = {
    "DRAFT": "draft",
    "SENT": "sent",
    "SCHEDULED": "sent",
    "UNPAID": "sent",
    "PAYMENT_PENDING": "sent",
    "PARTIALLY_PAID": "partially_paid",
    "MARKED_AS_PARTIALLY_PAID": "partially_paid",
    "PAID": "paid",
    "MARKED_AS_PAID": "paid",
    "CANCELLED": "cancelled",
    "REFUNDED": "refunded",
    "PARTIALLY_REFUNDED": "refunded",
    "MARKED_AS_REFUNDED": "refunded",
}
OPEN = ("sent", "partially_paid", "overdue")
# Ours only: PayPal still says SENT for an invoice we've marked overdue.
_STICKY = {("overdue", "sent")}


class PayPalReader(Protocol):
    async def get_invoice(self, invoice_id: str) -> dict[str, Any]: ...


def invoice_event(inv: Invoice) -> dict[str, Any]:
    """What the phone needs to redraw a card. Amounts as strings, like the API."""
    return {
        "type": "invoice",
        "id": inv.id,
        "number": inv.number,
        "status": inv.status,
        "currency": inv.currency,
        "total": str(inv.total),
        "amount_paid": str(inv.amount_paid),
        "paid_at": inv.paid_at.isoformat() if inv.paid_at else None,
    }


def _paid_amount(remote: dict[str, Any]) -> Decimal | None:
    raw = ((remote.get("payments") or {}).get("paid_amount") or {}).get("value")
    try:
        return Decimal(str(raw)) if raw is not None else None
    except InvalidOperation:
        return None


async def apply_remote(session: AsyncSession, inv: Invoice, remote: dict[str, Any]) -> bool:
    """Apply a PayPal invoice representation. Returns True if anything changed."""
    changed = False
    events: list[Event] = []

    paid = _paid_amount(remote)
    if paid is not None and paid != inv.amount_paid:
        delta = paid - inv.amount_paid
        inv.amount_paid = paid
        changed = True
        if delta > 0:
            events.append(
                Event(
                    workspace_id=inv.workspace_id,
                    invoice_id=inv.id,
                    kind="payment_received",
                    data={"amount": str(delta), "paid_total": str(paid)},
                )
            )

    remote_status = str(remote.get("status", ""))
    status = STATUS_MAP.get(remote_status)
    if status is None:
        if remote_status:
            log.warning("unknown_paypal_status", status=remote_status, invoice_id=inv.id)
    elif status != inv.status and status != "draft" and (inv.status, status) not in _STICKY:
        old, inv.status = inv.status, status
        changed = True
        if status == "paid":
            inv.paid_at = utcnow()
        events.append(
            Event(
                workspace_id=inv.workspace_id,
                invoice_id=inv.id,
                kind=f"invoice_{status}",
                data={"from": old, "paypal_status": remote_status},
            )
        )

    if changed:
        session.add_all(events)
        await session.commit()
        broker.publish(inv.workspace_id, invoice_event(inv))
        log.info("invoice_synced", invoice_id=inv.id, status=inv.status, paid=str(inv.amount_paid))
    return changed


async def refresh(session: AsyncSession, paypal: PayPalReader, inv: Invoice) -> bool:
    if not inv.paypal_invoice_id:
        return False
    return await apply_remote(session, inv, await paypal.get_invoice(inv.paypal_invoice_id))


def webhook_invoice_id(event: dict[str, Any]) -> str:
    """INVOICING.* events carry the invoice under `resource.invoice` or as `resource`."""
    resource = event.get("resource") or {}
    invoice = resource.get("invoice") if isinstance(resource.get("invoice"), dict) else resource
    return str(invoice.get("id") or "")


async def handle_webhook(session: AsyncSession, paypal: PayPalReader, event: dict[str, Any]) -> str:
    """Returns what happened, for the log line and the response body."""
    if not str(event.get("event_type", "")).startswith("INVOICING.INVOICE."):
        return "ignored"
    paypal_id = webhook_invoice_id(event)
    inv = await session.scalar(select(Invoice).where(Invoice.paypal_invoice_id == paypal_id))
    if inv is None:
        return "unknown_invoice"  # e.g. made in PayPal directly, not through SayInvoice
    return "updated" if await refresh(session, paypal, inv) else "unchanged"


async def reconcile_once(
    sessionmaker: async_sessionmaker[AsyncSession], paypal: PayPalReader, *, limit: int = 50
) -> int:
    """Re-check open invoices against PayPal. Returns how many changed."""
    cutoff = utcnow() - timedelta(days=120)
    async with sessionmaker() as session:
        invoices = (
            await session.scalars(
                select(Invoice)
                .where(
                    Invoice.status.in_(OPEN),
                    Invoice.paypal_invoice_id.is_not(None),
                    Invoice.sent_at >= cutoff,
                )
                .order_by(Invoice.sent_at.desc())
                .limit(limit)
            )
        ).all()
        changed = 0
        for inv in invoices:
            try:
                changed += await refresh(session, paypal, inv)
            except PayPalError as exc:
                log.warning("reconcile_failed", invoice_id=inv.id, error=str(exc))
    return changed


async def reconcile_forever(
    sessionmaker: async_sessionmaker[AsyncSession], paypal: PayPalReader, interval_s: float
) -> None:
    """Backup for slow or missing sandbox webhooks, so "Paid" still shows up in a demo."""
    while True:
        await asyncio.sleep(interval_s)
        try:
            changed = await reconcile_once(sessionmaker, paypal)
            if changed:
                log.info("reconciled", changed=changed)
        except Exception as exc:  # keep the loop alive whatever happens
            log.exception("reconcile_loop_error", error=str(exc))
