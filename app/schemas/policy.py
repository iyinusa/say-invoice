"""The Deal Policy settings form. Bounds keep a slip of the thumb from giving work away."""

from __future__ import annotations

from decimal import Decimal

from pydantic import BaseModel, Field


class PolicyIn(BaseModel):
    max_discount_pct: Decimal = Field(ge=0, le=50, decimal_places=2)
    floor_pct: Decimal = Field(ge=50, le=100, decimal_places=2)
    min_deposit_pct: Decimal = Field(ge=0, le=100, decimal_places=2)
    max_instalments: int = Field(ge=1, le=12)
    max_plan_days: int = Field(ge=7, le=365)
    price_negotiable: bool
    scope_negotiable: bool
    date_negotiable: bool
    auto_agree_within_policy: bool


class PolicyOut(PolicyIn):
    pass
