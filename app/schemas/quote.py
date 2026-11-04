"""Quotes: what the preview card sends in quote mode, and what the owner and client see."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator, model_validator

from app.agents.parser_rules import PAYPAL_CURRENCIES
from app.schemas.invoice import InvoiceItemIn, InvoiceItemOut


class QuoteIn(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    idempotency_key: str = Field(min_length=8, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")
    client_name: str = Field(default="", max_length=120)
    # Optional: a quote link can be sent by text message.
    client_email: EmailStr | None = None
    currency: str
    items: list[InvoiceItemIn] = Field(min_length=1, max_length=50)
    deposit_pct: Decimal | None = Field(default=None, ge=0, le=100, decimal_places=2)
    job_date: date | None = None
    valid_days: int = Field(default=30, ge=1, le=90)
    notes: str = Field(default="", max_length=4000)
    transcript: str = Field(default="", max_length=10_000)
    draft: dict[str, Any] | None = None

    @field_validator("client_email", mode="before")
    @classmethod
    def _blank_is_none(cls, v: object) -> object:
        return None if isinstance(v, str) and not v.strip() else v

    @field_validator("currency")
    @classmethod
    def _currency(cls, v: str) -> str:
        code = v.strip().upper()
        if code not in PAYPAL_CURRENCIES:
            raise ValueError(f"PayPal can't take a deposit in {code or 'that currency'}")
        return code

    @model_validator(mode="after")
    def _someone(self) -> QuoteIn:
        if not self.client_name and not self.client_email:
            raise ValueError("Who is the quote for? Add a name or an email.")
        return self


class DepositOut(BaseModel):
    invoice_id: str
    status: str
    amount: Decimal
    amount_paid: Decimal
    pay_url: str


class QuoteOut(BaseModel):
    id: str
    number: str
    status: str
    version: int
    client_name: str
    client_email: str
    currency: str
    items: list[InvoiceItemOut]
    total: Decimal
    list_total: Decimal
    labour_discount_pct: Decimal
    discount_amount: Decimal
    deposit_pct: Decimal
    deposit_amount: Decimal
    job_date: date | None
    valid_until: date
    note: str
    public_url: str
    created_at: datetime
    accepted_at: datetime | None
    deposit: DepositOut | None


class ClientMessageIn(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    text: str = Field(min_length=1, max_length=500)


class DecisionIn(BaseModel):
    action: str = Field(pattern="^(approve|decline|counter|reply)$")
    labour_discount_pct: Decimal | None = Field(default=None, ge=0, le=100, decimal_places=2)
    deposit_pct: Decimal | None = Field(default=None, ge=0, le=100, decimal_places=2)
    note: str = Field(default="", max_length=500)


class FinalIn(BaseModel):
    """The finish-job card: the work as done. Currency and discounts come from the quote."""

    model_config = ConfigDict(str_strip_whitespace=True)

    client_name: str = Field(default="", max_length=120)
    client_email: EmailStr
    items: list[InvoiceItemIn] = Field(min_length=1, max_length=50)
    due_date: date | None = None
    notes: str = Field(default="", max_length=2000)
    transcript: str = Field(default="", max_length=10_000)
    draft: dict[str, Any] | None = None


class AcceptIn(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    # Where PayPal sends the deposit request. Optional when retrying an accepted quote.
    email: EmailStr | None = None
    name: str = Field(default="", max_length=120)

    @field_validator("email", mode="before")
    @classmethod
    def _blank_is_none(cls, v: object) -> object:
        return None if isinstance(v, str) and not v.strip() else v
