"""What the AI extracts from a spoken job or quote, and what code makes of it.

`ParsedJob` is the model's output: the fields it fills and nothing else. No
totals, no taxes, no date arithmetic. `ParseResult` is what the API returns
after `app.agents.parser_rules` has checked the draft, resolved the due date
and priced every line in code.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, Field

Mode = Literal["invoice", "quote"]
Weekday = Literal["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]


class ParsedItem(BaseModel):
    name: str = Field(description="Short line-item title, e.g. 'Boiler repair (labour)'.")
    description: str = Field(default="", description="Optional extra detail for the client.")
    quantity: float = Field(description="Hours, units or 1 for a fixed price.")
    unit_price: float = Field(
        description="Price per hour/unit, as spoken. 0 if no price was said. Never a total."
    )
    unit: Literal["hours", "items", "fixed"] = "items"
    is_material: bool = Field(
        default=False, description="True for parts/materials, false for labour or service."
    )
    approximate: bool = Field(
        default=False, description="True when the price was hedged: 'about', 'roughly', '-ish'."
    )


class ParsedJob(BaseModel):
    mode: Mode = "invoice"
    transcript: str = Field(description="Verbatim transcript of what was said.")
    language: str = Field(default="en", description="BCP-47 code of the spoken language.")
    client_name: str = ""
    client_email: str = ""
    currency: str = Field(default="", description="ISO 4217 code if stated or implied by £/$/€.")
    items: list[ParsedItem] = Field(default_factory=list)
    due_in_days: int | None = Field(
        default=None, description="'in two weeks' -> 14, 'net 30' -> 30, 'today' -> 0."
    )
    due_weekday: Weekday | None = Field(
        default=None, description="'due Friday' -> friday. Code turns it into a date."
    )
    due_weeks_ahead: int | None = Field(
        default=None, description="With due_weekday: 1 for 'next week', e.g. 'end of next week'."
    )
    due_date: str | None = Field(
        default=None, description="ISO date, only when a calendar date was said."
    )
    deposit_pct: float | None = Field(default=None, description="Quotes only: '30% deposit'.")
    deposit_amount: float | None = Field(
        default=None, description="Quotes only: a deposit said as money, '£50 deposit'."
    )
    notes: str = Field(default="", description="Anything said for the client that isn't an item.")
    missing_fields: list[str] = Field(
        default_factory=list, description="Required fields that were not said, e.g. client_email."
    )
    uncertain_fields: list[str] = Field(
        default_factory=list, description="Fields that were inferred or misheard-likely."
    )
    follow_up_question: str = Field(
        default="", description="One short question to fill the most important missing field."
    )


class PricedItem(BaseModel):
    name: str
    description: str = ""
    quantity: Decimal
    unit_price: Decimal
    unit: Literal["hours", "items", "fixed"]
    is_material: bool
    approximate: bool
    line_total: Decimal


class ParseResult(BaseModel):
    """The preview card. Every amount and date here was computed in code."""

    mode: Mode
    ready: bool = Field(description="True when nothing required is missing: safe to send.")
    transcript: str
    language: str
    client_name: str
    client_email: str
    currency: str
    items: list[PricedItem]
    subtotal: Decimal
    due_date: date | None
    deposit_pct: Decimal | None
    deposit_amount: Decimal | None
    notes: str
    missing_fields: list[str]
    uncertain_fields: list[str]
    follow_up_question: str
    warnings: list[str]
    # The model's raw draft. The UI sends it back as `previous` with the answer
    # to the follow-up question, and it is stored as the invoice's raw_ai_json.
    draft: ParsedJob
