"""What the owner can do to an invoice from its job page: remind, mark paid, cancel, retry.

Every action goes to PayPal first, then re-reads the invoice from PayPal, so our
status only ever reflects what PayPal says actually happened.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any, Protocol

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.logging_config import get_logger
from app.models import Event, Invoice, Workspace
from app.models.base import utcnow
from app.money import display
from app.services import invoicing, payments
from app.services.money_view import OPEN, balance

log = get_logger("invoice_actions")

# A tap on "Remind now" can't nag a client more than once a day.
MANUAL_REMINDER_GAP = timedelta(hours=24)
METHODS = ("CASH", "BANK_TRANSFER", "CHECK", "OTHER")


class ActionRefused(Exception):
    """Not possible for this invoice right now; the message says why, in plain English."""


class PayPalActions(Protocol):
    async def remind(self, invoice_id: str, *, subject: str = "", note: str = "") -> None: ...
    async def cancel(self, invoice_id: str, *, subject: str = "", note: str = "") -> None: ...
    async def record_payment(
        self, invoice_id: str, *, amount: Decimal, currency: str, method: str = "CASH",
        paid_on: date | None = None, note: str = "",
    ) -> str: ...  # fmt: skip
    async def get_invoice(self, invoice_id: str) -> dict[str, Any]: ...


def aware(dt: datetime) -> datetime:
    """SQLite hands datetimes back without a zone, Postgres with one: treat both as UTC."""
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


async def last_reminder(session: AsyncSession, inv: Invoice) -> Event | None:
    return await session.scalar(
        select(Event)
        .where(Event.invoice_id == inv.id, Event.kind == "reminder_sent")
        .order_by(Event.created_at.desc())
        .limit(1)
    )


def default_reminder(inv: Invoice, ws: Workspace) -> str:
    return (
        f"Hi {inv.client.name or 'there'}, just a friendly reminder about invoice {inv.number} "
        f"for {display(balance(inv), inv.currency)}. You can pay by card or PayPal from this "
        f"email. Thank you! {ws.owner_name or ws.business_name}"
    )


async def remind(
    session: AsyncSession, paypal: PayPalActions, ws: Workspace, inv: Invoice,
    *, note: str = "", by: str = "owner", gap: timedelta = MANUAL_REMINDER_GAP,
) -> Event:  # fmt: skip
    if inv.status not in OPEN or not inv.paypal_invoice_id:
        raise ActionRefused("only an unpaid, sent invoice can be reminded")
    last = await last_reminder(session, inv)
    if last is not None and (since := utcnow() - aware(last.created_at)) < gap:
        hours = int(since.total_seconds() // 3600)
        when = f"{hours} hour{'s' if hours != 1 else ''}" if hours else "less than an hour"
        raise ActionRefused(f"a reminder went out {when} ago")
    text = (note or default_reminder(inv, ws)).strip()[:1000]
    await paypal.remind(
        inv.paypal_invoice_id,
        subject=f"Reminder: invoice {inv.number} from {ws.business_name}",
        note=text,
    )
    ev = Event(
        workspace_id=ws.id, invoice_id=inv.id, kind="reminder_sent", data={"by": by, "note": text}
    )
    session.add(ev)
    await session.commit()
    log.info("reminder_sent", invoice_id=inv.id, by=by)
    return ev


async def record_payment(
    session: AsyncSession, paypal: PayPalActions, ws: Workspace, inv: Invoice,
    *, amount: Decimal | None, method: str, note: str = "", today: date,
) -> Invoice:  # fmt: skip
    if inv.status not in OPEN or not inv.paypal_invoice_id:
        raise ActionRefused("only an unpaid, sent invoice can be marked as paid")
    if method not in METHODS:
        raise ActionRefused("choose cash, bank transfer, cheque or other")
    owed = balance(inv)
    amount = owed if amount is None else amount
    if amount <= 0 or amount > owed:
        raise ActionRefused(f"the amount must be between {display(Decimal('0.01'), inv.currency)} "
                            f"and {display(owed, inv.currency)}")  # fmt: skip
    await paypal.record_payment(
        inv.paypal_invoice_id, amount=amount, currency=inv.currency, method=method,
        paid_on=today, note=note,
    )  # fmt: skip
    session.add(Event(workspace_id=ws.id, invoice_id=inv.id, kind="payment_recorded",
                      data={"method": method, "amount": str(amount)}))  # fmt: skip
    await session.commit()
    await payments.refresh(session, paypal, inv)  # PayPal's word on the new status
    return inv


async def cancel(
    session: AsyncSession, paypal: PayPalActions, ws: Workspace, inv: Invoice, *, note: str = ""
) -> Invoice:
    if inv.amount_paid > 0:
        raise ActionRefused("money has been paid on this invoice, so it can't be cancelled")
    if inv.status == "draft":
        inv.status = "cancelled"  # PayPal never sent it: nothing to tell the client
        session.add(Event(workspace_id=ws.id, invoice_id=inv.id, kind="invoice_cancelled",
                          data={"from": "draft"}))  # fmt: skip
        await session.commit()
        return inv
    if inv.status not in OPEN or not inv.paypal_invoice_id:
        raise ActionRefused("this invoice can't be cancelled")
    await paypal.cancel(inv.paypal_invoice_id, note=note or "This invoice has been cancelled.")
    await payments.refresh(session, paypal, inv)
    return inv


async def retry(
    session: AsyncSession, paypal: Any, ws: Workspace, inv: Invoice, *, today: date
) -> Invoice:
    if inv.status != "draft":
        raise ActionRefused("only an invoice that failed to send can be retried")
    return await invoicing.push_to_paypal(session, paypal, ws, inv, today=today)
