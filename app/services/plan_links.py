"""The client's "Propose a plan" link, and where an invoice on a plan stands.

Kept free of other app imports: invoicing, the money view and the Chase Agent
all use it.
"""

from __future__ import annotations

import secrets
from datetime import date
from decimal import Decimal
from typing import Any

from app.settings import get_settings


def ensure_token(inv: Any) -> str:
    if not inv.public_token:
        inv.public_token = secrets.token_urlsafe(16)
    return str(inv.public_token)


def plan_url(inv: Any, base_url: str | None = None) -> str:
    base = (base_url or get_settings().public_base_url).rstrip("/")
    return f"{base}/p/{ensure_token(inv)}"


def client_language(inv: Any) -> str:
    client = getattr(inv, "client", None)
    return getattr(client, "language", None) or "en"


def plan_line(inv: Any) -> str:
    from app.i18n import t

    return t(client_language(inv), "plan_line", url=plan_url(inv))


def next_instalment(inv: Any) -> tuple[date, Decimal] | None:
    """The first instalment not yet covered by payments: (its due date, what's owed by then)."""
    if not inv.plan:
        return None
    covered = Decimal(0)
    for row in inv.plan.get("instalments", []):
        covered += Decimal(row["amount"])
        if inv.amount_paid < covered:
            return date.fromisoformat(row["due"]), covered - inv.amount_paid
    return None


def current_due(inv: Any) -> date | None:
    """When the client is next expected to pay: the next instalment on a plan, else the due date."""
    nxt = next_instalment(inv)
    return nxt[0] if nxt else inv.due_date
