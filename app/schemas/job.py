"""What the AI extracts from a spoken job or quote.

The model fills these fields and nothing else: no totals, no taxes, no maths.
`app.money` turns the numbers into amounts once a human has seen them.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class ParsedItem(BaseModel):
    name: str = Field(description="Short line-item title, e.g. 'Boiler repair (labour)'.")
    description: str = Field(default="", description="Optional extra detail for the client.")
    quantity: float = Field(description="Hours, units or 1 for a fixed price.")
    unit_price: float = Field(description="Price per hour/unit, as spoken. Never a computed total.")
    unit: Literal["hours", "items", "fixed"] = "items"
    is_material: bool = Field(
        default=False, description="True for parts/materials, false for labour or service."
    )


class ParsedJob(BaseModel):
    mode: Literal["invoice", "quote"] = "invoice"
    transcript: str = Field(description="Verbatim transcript of what was said.")
    language: str = Field(default="en", description="BCP-47 code of the spoken language.")
    client_name: str = ""
    client_email: str = ""
    currency: str = Field(default="", description="ISO 4217 code if stated or implied by £/$/€.")
    items: list[ParsedItem] = Field(default_factory=list)
    due_in_days: int | None = Field(default=None, description="Days until due, if stated.")
    due_date: str | None = Field(default=None, description="ISO date if a date was stated.")
    deposit_pct: float | None = Field(default=None, description="Quotes only: deposit percent.")
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
