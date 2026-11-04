"""The final invoice for a booked quote: the work as done, minus what's already agreed or paid.

    agreed lines (± changes spoken at the end of the job)
  − the agreed labour discount (the quote's %, applied to the labour as done)
  − the deposit actually received
  = the balance PayPal invoices

PayPal has no negative line items, so both deductions go in as one
invoice-level discount, spelled out in the invoice note. Our total and PayPal's
are then the same to the penny. A deposit invoice nobody paid is cancelled
first, so the client is never asked for the same money twice.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.logging_config import get_logger
from app.models import Event, Invoice, Quote, Workspace
from app.money import display, percent_of, plain, subtotal, to_money
from app.paypal.client import PayPalError
from app.schemas.invoice import InvoiceIn, InvoiceItemIn
from app.services import invoicing, payments, quotes
from app.services.invoicing import PayPalInvoicing

log = get_logger("final")


class AlreadyInvoiced(Exception):
    def __init__(self, invoice: Invoice) -> None:
        super().__init__("this job has already been invoiced")
        self.invoice = invoice


class NothingToInvoice(Exception):
    """The deposit already covers the work as done."""


class PayPalAndCancel(PayPalInvoicing):  # pragma: no cover - typing only
    async def cancel(self, invoice_id: str, *, subject: str = "", note: str = "") -> None: ...
    async def get_invoice(self, invoice_id: str) -> dict[str, Any]: ...


async def final_invoice(session: AsyncSession, q: Quote) -> Invoice | None:
    return await session.scalar(
        select(Invoice)
        .where(Invoice.quote_id == q.id, Invoice.kind == "final")
        .order_by(Invoice.created_at.desc())
        .limit(1)
    )


async def jobs_to_finish(session: AsyncSession, ws: Workspace) -> list[dict[str, Any]]:
    """Accepted quotes not yet invoiced: the "Jobs to finish" list on the home screen."""
    accepted = await session.scalars(
        select(Quote)
        .where(Quote.workspace_id == ws.id, Quote.status == "accepted")
        .order_by(Quote.job_date.is_(None), Quote.job_date, Quote.accepted_at)
    )
    out = []
    for q in accepted:
        final = await final_invoice(session, q)
        if final is not None and final.status != "draft":
            continue
        dep = await quotes.deposit_invoice(session, q)
        out.append(
            {
                "quote_id": q.id,
                "number": q.number,
                "client_name": q.client_name,
                "client_email": q.client_email,
                "currency": q.currency,
                "total": str(q.total),
                "labour_discount_pct": str(plain(q.labour_discount_pct)),
                "job_date": q.job_date.isoformat() if q.job_date else None,
                "deposit": None
                if dep is None
                else {"status": dep.status, "amount": str(dep.total), "paid": str(dep.amount_paid)},
                "lines": [
                    {
                        "name": ln.name,
                        "description": ln.description,
                        "quantity": str(plain(ln.quantity)),
                        "unit_price": str(ln.unit_amount),
                        "unit": ln.unit,
                        "is_material": ln.is_material,
                    }
                    for ln in q.lines
                ],
            }
        )
    return out


def breakdown(q: Quote, items: list[InvoiceItemIn], deposit_paid: Decimal) -> dict[str, Decimal]:
    cur = q.currency
    lines = [(i.quantity, to_money(i.unit_price, cur)) for i in items]
    labour = [(i.quantity, to_money(i.unit_price, cur)) for i in items if not i.is_material]
    work = subtotal(lines, cur)
    labour_total = subtotal(labour, cur)
    labour_discount = percent_of(labour_total, q.labour_discount_pct, cur)
    discount = labour_discount + deposit_paid
    return {
        "work": work,
        "labour_discount": labour_discount,
        "deposit_paid": deposit_paid,
        "discount": discount,
        "balance": work - discount,
    }


async def send_final(
    session: AsyncSession,
    paypal: PayPalAndCancel,
    ws: Workspace,
    q: Quote,
    *,
    client_name: str,
    client_email: str,
    items: list[InvoiceItemIn],
    due_date: date | None,
    notes: str,
    transcript: str,
    draft: dict[str, Any] | None,
    today: date,
) -> Invoice:
    if q.status != "accepted":
        raise quotes.QuoteClosed("only an accepted quote can be invoiced")
    existing = await final_invoice(session, q)
    if existing is not None and existing.status != "draft":
        raise AlreadyInvoiced(existing)

    # The deposit as PayPal sees it right now, not as we last heard.
    dep = await quotes.deposit_invoice(session, q)
    if dep is not None and dep.status in payments.OPEN:
        try:
            await payments.refresh(session, paypal, dep)
        except PayPalError as exc:
            log.warning("deposit_refresh_failed", quote_id=q.id, error=str(exc))
    deposit_paid = dep.amount_paid if dep is not None else Decimal(0)

    numbers = breakdown(q, items, deposit_paid)
    if numbers["balance"] <= 0:
        raise NothingToInvoice(
            f"the deposit already covers this job ({display(deposit_paid, q.currency)} paid)"
        )

    # An unpaid deposit invoice would ask for the same money twice: cancel it.
    if dep is not None and dep.status in payments.OPEN and dep.amount_paid == 0:
        if dep.paypal_invoice_id:
            await paypal.cancel(
                dep.paypal_invoice_id, note=f"Replaced by the final invoice for quote {q.number}."
            )
        dep.status = "cancelled"
        session.add(Event(workspace_id=ws.id, invoice_id=dep.id, kind="invoice_cancelled",
                          data={"reason": "replaced_by_final"}))  # fmt: skip
        await session.commit()

    cur = q.currency
    parts = [f"Final invoice for quote {q.number}."]
    if numbers["labour_discount"] > 0:
        parts.append(
            f"Includes the agreed {plain(q.labour_discount_pct)}% off labour "
            f"(−{display(numbers['labour_discount'], cur)})."
        )
    if deposit_paid > 0:
        parts.append(f"Deposit received (−{display(deposit_paid, cur)}) deducted.")
    if notes.strip():
        parts.append(notes.strip())

    data = InvoiceIn(
        idempotency_key=f"final-{q.id}",
        language=q.language,  # one final invoice per quote, however often it's tapped
        client_name=client_name,
        client_email=client_email,
        currency=cur,
        items=items,
        due_date=due_date,
        notes=" ".join(parts)[:4000],
        transcript=transcript,
        draft=draft,
    )
    inv = await invoicing.create_and_send(
        session, paypal, ws, data, today=today, kind="final", quote_id=q.id,
        discount=numbers["discount"],
    )  # fmt: skip
    record = {k: str(v) for k, v in numbers.items()} | {"quote_id": q.id}
    session.add(Event(workspace_id=ws.id, invoice_id=inv.id, kind="final_invoiced", data=record))
    await session.commit()
    log.info("final_invoiced", quote_id=q.id, balance=str(numbers["balance"]))
    return inv
