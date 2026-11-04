"""The tables for the core invoice loop (TASKS.md §4).

Quotes, the Deal Policy and deal messages join these in Phase 2.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base
from app.models.base import IdMixin, Money


class Workspace(IdMixin, Base):
    """One tradesperson's business. Demo workspaces (one per visitor) come later."""

    __tablename__ = "workspace"

    business_name: Mapped[str] = mapped_column(String(120))
    owner_name: Mapped[str] = mapped_column(String(120), default="")
    # The PayPal business account email: PayPal rejects any other invoicer.
    owner_email: Mapped[str] = mapped_column(String(254), default="")
    currency: Mapped[str] = mapped_column(String(3), default="GBP")
    default_due_days: Mapped[int] = mapped_column(Integer, default=7)
    language: Mapped[str] = mapped_column(String(16), default="en")


class Client(IdMixin, Base):
    __tablename__ = "client"
    __table_args__ = (UniqueConstraint("workspace_id", "email"),)

    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspace.id"), index=True)
    name: Mapped[str] = mapped_column(String(120), default="")
    email: Mapped[str] = mapped_column(String(254))
    phone: Mapped[str] = mapped_column(String(40), default="")
    language: Mapped[str] = mapped_column(String(16), default="en")


class Invoice(IdMixin, Base):
    __tablename__ = "invoice"
    __table_args__ = (UniqueConstraint("workspace_id", "idempotency_key"),)

    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspace.id"), index=True)
    client_id: Mapped[str] = mapped_column(ForeignKey("client.id"), index=True)
    # The UI's key for one tap of "Send": a retry with the same key never sends twice.
    idempotency_key: Mapped[str] = mapped_column(String(64))
    payload_hash: Mapped[str] = mapped_column(String(64))
    kind: Mapped[str] = mapped_column(String(16), default="standard")  # standard|deposit|final
    # draft | sent | partially_paid | paid | overdue | cancelled
    status: Mapped[str] = mapped_column(String(20), default="draft", index=True)
    paypal_invoice_id: Mapped[str | None] = mapped_column(String(64), unique=True)
    number: Mapped[str] = mapped_column(String(40), default="")
    currency: Mapped[str] = mapped_column(String(3))
    total: Mapped[Decimal] = mapped_column(Money)
    amount_paid: Mapped[Decimal] = mapped_column(Money, default=Decimal(0))
    due_date: Mapped[date | None] = mapped_column(Date)
    note: Mapped[str] = mapped_column(Text, default="")
    payer_view_url: Mapped[str] = mapped_column(String(500), default="")
    qr_png: Mapped[bytes | None] = mapped_column(LargeBinary, deferred=True)
    transcript: Mapped[str] = mapped_column(Text, default="")
    raw_ai_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    # The last PayPal problem, in plain English, while the invoice is stuck as a draft.
    last_error: Mapped[str] = mapped_column(String(500), default="")
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    client: Mapped[Client] = relationship(lazy="joined")
    items: Mapped[list[LineItem]] = relationship(
        order_by="LineItem.position", cascade="all, delete-orphan", lazy="selectin"
    )


class LineItem(IdMixin, Base):
    __tablename__ = "line_item"

    invoice_id: Mapped[str] = mapped_column(ForeignKey("invoice.id"), index=True)
    position: Mapped[int] = mapped_column(Integer, default=0)
    name: Mapped[str] = mapped_column(String(200))
    description: Mapped[str] = mapped_column(String(1000), default="")
    quantity: Mapped[Decimal] = mapped_column(Money)
    unit_amount: Mapped[Decimal] = mapped_column(Money)
    unit: Mapped[str] = mapped_column(String(10), default="items")  # hours|items|fixed
    is_material: Mapped[bool] = mapped_column(Boolean, default=False)


class Event(IdMixin, Base):
    """The job's audit timeline: created, sent, paid, reminded, and PayPal errors."""

    __tablename__ = "event"

    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspace.id"), index=True)
    invoice_id: Mapped[str | None] = mapped_column(ForeignKey("invoice.id"), index=True)
    kind: Mapped[str] = mapped_column(String(40))
    data: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
