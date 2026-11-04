"""What the preview card sends when the tradesperson taps Send, and what comes back.

The card can be edited by hand, so nothing here trusts the parser: every field
is re-validated and the total is recomputed by `app.money`.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator

from app.agents.parser_rules import PAYPAL_CURRENCIES


class InvoiceItemIn(BaseModel):
    # Strip before the length checks, so a name of only spaces is refused.
    model_config = ConfigDict(str_strip_whitespace=True)

    name: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=1000)
    quantity: Decimal = Field(gt=0, le=10_000, decimal_places=2)
    unit_price: Decimal = Field(gt=0, le=1_000_000, decimal_places=2)
    unit: Literal["hours", "items", "fixed"] = "items"
    is_material: bool = False


class InvoiceIn(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    idempotency_key: str = Field(min_length=8, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")
    client_name: str = Field(default="", max_length=120)
    client_email: EmailStr
    currency: str
    items: list[InvoiceItemIn] = Field(min_length=1, max_length=50)
    due_date: date | None = None
    notes: str = Field(default="", max_length=4000)
    transcript: str = Field(default="", max_length=10_000)
    draft: dict[str, Any] | None = Field(default=None, description="The parser's raw draft.")
    # The client's language (their pages, messages and reminders); kept on the client record.
    language: str = Field(default="en", max_length=16)

    @field_validator("language")
    @classmethod
    def _language(cls, v: str) -> str:
        from app.i18n import base

        return base(v)

    @field_validator("currency")
    @classmethod
    def _currency(cls, v: str) -> str:
        code = v.strip().upper()
        if code not in PAYPAL_CURRENCIES:
            raise ValueError(f"PayPal can't invoice in {code or 'that currency'}")
        return code


class InvoiceItemOut(BaseModel):
    name: str
    description: str
    quantity: Decimal
    unit_price: Decimal
    unit: str
    is_material: bool
    line_total: Decimal


class InvoiceOut(BaseModel):
    id: str
    number: str
    status: str
    client_name: str
    client_email: str
    currency: str
    total: Decimal
    discount: Decimal
    amount_paid: Decimal
    due_date: date | None
    note: str
    payer_view_url: str
    items: list[InvoiceItemOut]
    created_at: datetime
    sent_at: datetime | None
    paid_at: datetime | None
    last_error: str
