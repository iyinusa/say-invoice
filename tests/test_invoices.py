from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from app.paypal.invoices import InvoiceDraft, Item, Party, build_invoice_body


def _draft(**overrides: object) -> InvoiceDraft:
    base: dict[str, object] = {
        "number": "INV-0001",
        "currency": "GBP",
        "items": [
            Item("Boiler repair", Decimal(2), Decimal(60), unit_of_measure="HOURS"),
            Item("Valve", Decimal(1), Decimal("25"), description="15mm"),
        ],
        "recipient": Party("Tom Kennedy", "kennedy@example.com"),
        "invoicer": Party("Dave Mason", "dave@biz.example.com", "Dave's Plumbing"),
        "invoice_date": date(2026, 10, 25),
        "due_date": date(2026, 11, 1),
    }
    base.update(overrides)
    return InvoiceDraft(**base)  # type: ignore[arg-type]


def test_basic_body() -> None:
    body = build_invoice_body(_draft())
    assert body["detail"]["invoice_number"] == "INV-0001"
    assert body["detail"]["payment_term"] == {
        "term_type": "DUE_ON_DATE_SPECIFIED",
        "due_date": "2026-11-01",
    }
    assert body["invoicer"]["business_name"] == "Dave's Plumbing"
    assert body["primary_recipients"][0]["billing_info"] == {
        "email_address": "kennedy@example.com",
        "name": {"given_name": "Tom", "surname": "Kennedy"},
    }
    labour, valve = body["items"]
    assert labour["quantity"] == "2"
    assert labour["unit_amount"] == {"currency_code": "GBP", "value": "60.00"}
    assert labour["unit_of_measure"] == "HOURS"
    assert valve["description"] == "15mm"
    assert "partial_payment" not in body["configuration"]
    assert "amount" not in body


def test_deposit_becomes_invoice_discount() -> None:
    draft = _draft(discount=Decimal("39"))
    assert draft.total == Decimal("106.00")
    body = build_invoice_body(draft)
    assert body["amount"]["breakdown"]["discount"]["invoice_discount"]["amount"] == {
        "currency_code": "GBP",
        "value": "39.00",
    }


def test_partial_payment_terms() -> None:
    body = build_invoice_body(_draft(minimum_amount_due=Decimal("50")))
    assert body["configuration"]["partial_payment"] == {
        "allow_partial_payment": True,
        "minimum_amount_due": {"currency_code": "GBP", "value": "50.00"},
    }


def test_due_on_receipt_without_due_date() -> None:
    body = build_invoice_body(_draft(due_date=None))
    assert body["detail"]["payment_term"] == {"term_type": "DUE_ON_RECEIPT"}


def test_single_word_name() -> None:
    body = build_invoice_body(_draft(recipient=Party("Kennedy", "k@example.com")))
    assert body["primary_recipients"][0]["billing_info"]["name"] == {"given_name": "Kennedy"}


@pytest.mark.parametrize(
    "overrides",
    [{"items": []}, {"discount": Decimal("145")}],
)
def test_rejects_empty_or_non_positive(overrides: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        build_invoice_body(_draft(**overrides))
