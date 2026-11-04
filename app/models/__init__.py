"""ORM models. Importing this package registers every table on `Base.metadata`."""

from __future__ import annotations

from app.db import Base
from app.models.core import (
    Client,
    DealMessage,
    DealPolicy,
    Event,
    Invoice,
    LineItem,
    Quote,
    QuoteLine,
    Reminder,
    Workspace,
)

__all__ = [
    "Base",
    "Client",
    "DealMessage",
    "DealPolicy",
    "Event",
    "Invoice",
    "LineItem",
    "Quote",
    "QuoteLine",
    "Reminder",
    "Workspace",
]
