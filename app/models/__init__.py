"""ORM models. Importing this package registers every table on `Base.metadata`."""

from __future__ import annotations

from app.db import Base
from app.models.core import (
    Client,
    DealPolicy,
    Event,
    Invoice,
    LineItem,
    Quote,
    QuoteLine,
    Workspace,
)

__all__ = [
    "Base",
    "Client",
    "DealPolicy",
    "Event",
    "Invoice",
    "LineItem",
    "Quote",
    "QuoteLine",
    "Workspace",
]
