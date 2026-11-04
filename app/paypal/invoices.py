"""Build PayPal Invoicing v2 request bodies from our own typed objects.

Pure functions, no I/O: everything that decides *what* we send to PayPal lives
here so it can be unit-tested without the network.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any, Literal

from app.money import fmt, plain, subtotal

UnitOfMeasure = Literal["QUANTITY", "HOURS", "AMOUNT"]


@dataclass(frozen=True)
class Item:
    name: str
    quantity: Decimal
    unit_amount: Decimal
    description: str = ""
    unit_of_measure: UnitOfMeasure = "QUANTITY"


@dataclass(frozen=True)
class Party:
    name: str
    email: str
    business_name: str = ""


@dataclass(frozen=True)
class InvoiceDraft:
    number: str
    currency: str
    items: list[Item]
    recipient: Party
    invoicer: Party
    invoice_date: date
    due_date: date | None = None
    note: str = ""
    memo: str = ""
    # Deducted from the total as an invoice-level discount, e.g. a deposit
    # already paid against a quote ("Deposit received").
    discount: Decimal | None = None
    # When set, the client may pay in parts with at least this much per payment.
    minimum_amount_due: Decimal | None = None
    extra_configuration: dict[str, Any] = field(default_factory=dict)

    @property
    def subtotal(self) -> Decimal:
        return subtotal(((i.quantity, i.unit_amount) for i in self.items), self.currency)

    @property
    def total(self) -> Decimal:
        return self.subtotal - (self.discount or Decimal(0))


def _name(full: str) -> dict[str, str]:
    """PayPal wants given/surname; we only ever have what the user said."""
    parts = full.strip().split(maxsplit=1)
    if not parts:
        return {}
    if len(parts) == 1:
        return {"given_name": parts[0]}
    return {"given_name": parts[0], "surname": parts[1]}


def _money(value: Decimal, currency: str) -> dict[str, str]:
    return {"currency_code": currency, "value": fmt(value, currency)}


def build_invoice_body(draft: InvoiceDraft) -> dict[str, Any]:
    cur = draft.currency.upper()
    if not draft.items:
        raise ValueError("an invoice needs at least one item")
    if draft.total <= 0:
        raise ValueError("invoice total must be positive")

    detail: dict[str, Any] = {
        "invoice_number": draft.number,
        "currency_code": cur,
        "invoice_date": draft.invoice_date.isoformat(),
    }
    if draft.due_date:
        detail["payment_term"] = {
            "term_type": "DUE_ON_DATE_SPECIFIED",
            "due_date": draft.due_date.isoformat(),
        }
    else:
        detail["payment_term"] = {"term_type": "DUE_ON_RECEIPT"}
    if draft.note:
        detail["note"] = draft.note
    if draft.memo:
        detail["memo"] = draft.memo

    invoicer: dict[str, Any] = {"name": _name(draft.invoicer.name)}
    if draft.invoicer.email:
        invoicer["email_address"] = draft.invoicer.email
    if draft.invoicer.business_name:
        invoicer["business_name"] = draft.invoicer.business_name

    recipient: dict[str, Any] = {"email_address": draft.recipient.email}
    if draft.recipient.name:
        recipient["name"] = _name(draft.recipient.name)

    items = []
    for item in draft.items:
        entry: dict[str, Any] = {
            "name": item.name[:200],
            "quantity": str(plain(item.quantity)),
            "unit_amount": _money(item.unit_amount, cur),
            "unit_of_measure": item.unit_of_measure,
        }
        if item.description:
            entry["description"] = item.description[:1000]
        items.append(entry)

    configuration: dict[str, Any] = {"allow_tip": False, **draft.extra_configuration}
    if draft.minimum_amount_due is not None:
        configuration["partial_payment"] = {
            "allow_partial_payment": True,
            "minimum_amount_due": _money(draft.minimum_amount_due, cur),
        }

    body: dict[str, Any] = {
        "detail": detail,
        "invoicer": invoicer,
        "primary_recipients": [{"billing_info": recipient}],
        "items": items,
        "configuration": configuration,
    }
    if draft.discount:
        body["amount"] = {
            "breakdown": {"discount": {"invoice_discount": {"amount": _money(draft.discount, cur)}}}
        }
    return body
