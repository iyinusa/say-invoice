"""The "Who owes me" home and the job detail timeline.

Overdue is derived, not stored: an invoice still open after its due date. That
keeps PayPal's status as the one source of truth and lets the demo clock move
"today" without rewriting rows.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import DealMessage, Event, Invoice, Quote, Workspace
from app.money import compact, display
from app.services import quotes as quote_svc

OPEN = ("sent", "partially_paid", "overdue")
OPEN_QUOTES = ("sent", "negotiating", "awaiting_owner")
KIND_LABEL = {"standard": "Invoice", "deposit": "Deposit", "final": "Final invoice"}


def balance(inv: Invoice) -> Decimal:
    return inv.total - inv.amount_paid


def days_overdue(inv: Invoice, today: date) -> int:
    if inv.status not in OPEN or inv.due_date is None:
        return 0
    return max(0, (today - inv.due_date).days)


def state(inv: Invoice, today: date) -> str:
    if inv.status == "draft":
        return "draft"
    if inv.status in OPEN:
        return "overdue" if days_overdue(inv, today) > 0 else "owed"
    return inv.status  # paid | cancelled | refunded


def _iso(dt: datetime) -> str:
    """Always with a zone: SQLite drops it, and a browser would read the time as local."""
    return (dt if dt.tzinfo else dt.replace(tzinfo=UTC)).isoformat()


def _when(d: date | None) -> str:
    return f"{d:%a} {d.day} {d:%b}" if d else ""


def invoice_row(inv: Invoice, today: date, quote_number: str | None = None) -> dict[str, Any]:
    st = state(inv, today)
    what = KIND_LABEL.get(inv.kind, "Invoice")
    ref = f"{what} {inv.number}".strip() if inv.number else what
    if quote_number and inv.kind == "deposit":
        ref = f"Deposit for {quote_number}"
    late = days_overdue(inv, today)
    detail = {
        "draft": "Not sent: tap to retry",
        "owed": f"due {_when(inv.due_date)}" if inv.due_date else "due on receipt",
        "overdue": f"{late} day{'s' if late != 1 else ''} overdue",
        "paid": f"paid {_when(inv.paid_at.date())}" if inv.paid_at else "paid",
        "cancelled": "cancelled",
        "refunded": "refunded",
    }.get(st, st)
    if st in ("owed", "overdue") and inv.amount_paid > 0:
        detail += f" · {display(inv.amount_paid, inv.currency)} paid"
    amount = inv.total if st in ("paid", "cancelled", "refunded") else balance(inv)
    return {
        "kind": "invoice",
        "id": inv.id,
        "title": inv.client.name or inv.client.email,
        "subtitle": f"{ref} · {detail}",
        "amount": display(amount, inv.currency),
        "state": st,
        "days_overdue": late,
    }


def quote_row(q: Quote, booked: bool) -> dict[str, Any]:
    label = {
        "sent": "waiting for the client",
        "negotiating": "client is negotiating",
        "awaiting_owner": "needs your answer",
    }.get(q.status, q.status)
    if booked:
        label = "booked: finish the job"
    return {
        "kind": "quote",
        "id": q.id,
        "title": q.client_name or q.client_email,
        "subtitle": f"Quote {q.number} · {label}",
        "amount": display(q.total, q.currency),
        "state": "booked" if booked else ("attention" if q.status == "awaiting_owner" else "quote"),
        "days_overdue": 0,
    }


async def home(session: AsyncSession, ws: Workspace, today: date) -> dict[str, Any]:
    invoices = list(
        await session.scalars(
            select(Invoice).where(Invoice.workspace_id == ws.id).order_by(Invoice.created_at.desc())
        )
    )
    quotes = list(
        await session.scalars(
            select(Quote).where(Quote.workspace_id == ws.id).order_by(Quote.created_at.desc())
        )
    )
    numbers = {q.id: q.number for q in quotes}
    finals = {i.quote_id for i in invoices if i.kind == "final" and i.status != "draft"}
    cur = ws.currency

    owed_rows, overdue_rows, paid_rows = [], [], []
    owed = overdue = Decimal(0)
    for inv in invoices:
        st = state(inv, today)
        row = invoice_row(inv, today, numbers.get(inv.quote_id or ""))
        if st in ("owed", "draft"):
            owed_rows.append(row)
        elif st == "overdue":
            overdue_rows.append(row)
        elif st == "paid":
            paid_rows.append(row)
        if st in ("owed", "overdue") and inv.currency == cur:
            owed += balance(inv)
            if st == "overdue":
                overdue += balance(inv)
    overdue_rows.sort(key=lambda r: -r["days_overdue"])

    # Money in this month, from the payment events (part payments count too).
    month_start = datetime(today.year, today.month, 1, tzinfo=UTC)
    paid_month = Decimal(0)
    for ev in await session.scalars(
        select(Event).where(
            Event.workspace_id == ws.id,
            Event.kind == "payment_received",
            Event.created_at >= month_start,
        )
    ):
        paid_month += Decimal(str(ev.data.get("amount", "0")))

    quote_rows = []
    quotes_out = Decimal(0)
    for q in quotes:
        if q.status in OPEN_QUOTES and q.valid_until >= today:
            quote_rows.append(quote_row(q, booked=False))
            quotes_out += q.total if q.currency == cur else 0
        elif q.status == "accepted" and q.id not in finals:
            quote_rows.append(quote_row(q, booked=True))
    quote_rows.sort(key=lambda r: {"attention": 0, "booked": 1}.get(r["state"], 2))

    return {
        "currency": cur,
        "totals": {
            "owed": display(owed, cur),
            "owed_count": sum(1 for r in owed_rows if r["state"] == "owed") + len(overdue_rows),
            "overdue": compact(overdue, cur),
            "overdue_count": len(overdue_rows),
            "paid_month": compact(paid_month, cur),
            "month": f"{today:%b}",
            "quotes_out": compact(quotes_out, cur),
            "quotes_count": sum(1 for r in quote_rows if r["state"] != "booked"),
        },
        "tabs": {
            "quotes": quote_rows,
            "owed": owed_rows,
            "overdue": overdue_rows,
            "paid": paid_rows[:30],
        },
    }


# ── job detail ────────────────────────────────────────────────────────────────


def _event_entry(ev: Event, cur: str) -> dict[str, Any] | None:
    d = ev.data or {}

    def amt(key: str) -> str:
        return display(Decimal(str(d[key])), cur) if d.get(key) not in (None, "") else ""

    method = str(d.get("method", "")).replace("_", " ").lower()
    label = {
        "quote_created": "Quote created",
        "quote_accepted": f"Client accepted the quote ({amt('total')})",
        "invoice_created": f"{KIND_LABEL.get(d.get('kind', 'standard'), 'Invoice')} created",
        "invoice_sent": f"Sent by PayPal ({amt('total')})",
        "payment_received": f"Payment received: {amt('amount')}",
        "invoice_partially_paid": "Part-paid",
        "invoice_paid": "Paid in full",
        "invoice_cancelled": "Cancelled",
        "invoice_refunded": "Refunded",
        "paypal_error": "PayPal couldn't complete a step",
        "final_invoiced": f"Final invoice: balance {amt('balance')}",
        "reminder_sent": "Reminder sent"
        + (" by you" if d.get("by") == "owner" else " by the Chase Agent"),
        "payment_recorded": f"Marked as paid ({method}, {amt('amount')})",
    }.get(ev.kind)
    if label is None:
        return None
    return {"at": _iso(ev.created_at), "kind": ev.kind, "label": label}


def _message_entry(m: DealMessage) -> dict[str, Any]:
    p = m.priced or {}
    total = display(Decimal(p["total"]), p["currency"]) if p.get("total") else ""
    if m.role == "client":
        label = f"Client asked: “{m.text[:140]}”"
    elif m.role == "owner":
        label = f"You offered {total}" if m.offer else f"You replied: “{m.text[:140]}”"
    elif m.offer_status == "escalated":
        label = "Sent to you for approval"
    elif m.offer:
        label = f"Assistant offered {total}"
    else:
        label = f"Assistant replied: “{m.text[:140]}”"
    return {"at": _iso(m.created_at), "kind": f"deal_{m.role}", "label": label}


async def _events_for(
    session: AsyncSession, ws: Workspace, invoice_ids: list[str], quote_id: str | None
) -> list[Event]:
    rows = list(
        await session.scalars(
            select(Event).where(Event.workspace_id == ws.id).order_by(Event.created_at)
        )
    )
    return [
        ev for ev in rows
        if ev.invoice_id in invoice_ids
        or (quote_id and ev.invoice_id is None and (ev.data or {}).get("quote_id") == quote_id)
    ]  # fmt: skip


def invoice_actions(inv: Invoice, st: str) -> dict[str, bool]:
    return {
        "remind": st in ("owed", "overdue") and bool(inv.paypal_invoice_id),
        "record_payment": st in ("owed", "overdue") and bool(inv.paypal_invoice_id),
        "cancel": st in ("owed", "overdue", "draft") and inv.amount_paid == 0,
        "retry": st == "draft",
        "qr": st in ("owed", "overdue") and bool(inv.paypal_invoice_id),
    }


async def invoice_detail(
    session: AsyncSession, ws: Workspace, inv: Invoice, today: date
) -> dict[str, Any]:
    q = await session.get(Quote, inv.quote_id) if inv.quote_id else None
    st = state(inv, today)
    entries = [e for ev in await _events_for(session, ws, [inv.id], None)
               if (e := _event_entry(ev, inv.currency))]  # fmt: skip
    return {
        "kind": "invoice",
        "id": inv.id,
        "title": inv.client.name or inv.client.email,
        "email": inv.client.email,
        "reference": f"{KIND_LABEL.get(inv.kind, 'Invoice')} {inv.number}".strip(),
        "quote": {"id": q.id, "number": q.number} if q else None,
        "state": st,
        "currency": inv.currency,
        "total": str(inv.total),
        "total_display": display(inv.total, inv.currency),
        "paid_display": display(inv.amount_paid, inv.currency),
        "balance": str(balance(inv)),
        "balance_display": display(balance(inv), inv.currency),
        "due": _when(inv.due_date) if inv.due_date else "on receipt",
        "days_overdue": days_overdue(inv, today),
        "pay_url": inv.payer_view_url,
        "last_error": inv.last_error,
        "items": [
            {"name": i.name, "amount": display(i.quantity * i.unit_amount, inv.currency)}
            for i in inv.items
        ],
        "actions": invoice_actions(inv, st),
        "timeline": entries,
    }


async def quote_detail(
    session: AsyncSession, ws: Workspace, q: Quote, today: date, base_url: str
) -> dict[str, Any]:
    invs = list(await session.scalars(select(Invoice).where(Invoice.quote_id == q.id)))
    final = next((i for i in invs if i.kind == "final" and i.status != "draft"), None)
    events = await _events_for(session, ws, [i.id for i in invs], q.id)
    entries = [e for ev in events if (e := _event_entry(ev, q.currency))]
    msgs = await session.scalars(select(DealMessage).where(DealMessage.quote_id == q.id))
    entries += [_message_entry(m) for m in msgs]
    entries.sort(key=lambda e: e["at"])
    expired = q.status in OPEN_QUOTES and q.valid_until < today
    return {
        "kind": "quote",
        "id": q.id,
        "title": q.client_name or q.client_email,
        "email": q.client_email,
        "reference": f"Quote {q.number}" + (f" · version {q.version}" if q.version > 1 else ""),
        "state": "expired" if expired else q.status,
        "currency": q.currency,
        "total_display": display(q.total, q.currency),
        "deposit_display": display(quote_svc.deposit_amount(q), q.currency),
        "public_url": quote_svc.public_url(q, base_url),
        "invoices": [invoice_row(i, today, q.number) for i in invs],
        "items": [
            {"name": ln.name, "amount": display(ln.quantity * ln.unit_amount, q.currency)}
            for ln in q.lines
        ],
        "actions": {"finish": q.status == "accepted" and final is None},
        "timeline": entries,
    }
