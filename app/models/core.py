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
    # What they do ("Plumber"): shapes the examples and the invoice preview in setup.
    trade: Mapped[str] = mapped_column(String(40), default="")
    # The default "Note to recipient" on every PayPal invoice, unless the job says its own.
    invoice_note: Mapped[str] = mapped_column(String(1000), default="")
    language: Mapped[str] = mapped_column(String(16), default="en")
    # False (the default): the Chase Agent drafts reminders and the owner approves each one.
    auto_chase: Mapped[bool] = mapped_column(Boolean, default=False)
    # Demo clock: the app behaves as if this many days have passed. Never touches PayPal's dates.
    clock_offset_days: Mapped[int] = mapped_column(Integer, default=0)
    # False until the owner has been through the 30-second setup.
    onboarded: Mapped[bool] = mapped_column(Boolean, default=False)


class DealPolicy(IdMixin, Base):
    """The tradesperson's rules for negotiating. PolicyGuard enforces them in code.

    Percentages are of the quote's list price. Materials are never discounted;
    that is a fixed rule in PolicyGuard, not a setting.
    """

    __tablename__ = "deal_policy"

    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspace.id"), unique=True)
    # Most the agent may take off labour, e.g. 10 = 10% off the labour lines.
    max_discount_pct: Mapped[Decimal] = mapped_column(Money, default=Decimal(10))
    # Lowest acceptable total, as % of the list price for the same work. Never shown
    # to the client or to the AI: it only learns that an offer was refused.
    floor_pct: Mapped[Decimal] = mapped_column(Money, default=Decimal(90))
    min_deposit_pct: Mapped[Decimal] = mapped_column(Money, default=Decimal(25))
    max_instalments: Mapped[int] = mapped_column(Integer, default=3)
    max_plan_days: Mapped[int] = mapped_column(Integer, default=60)
    price_negotiable: Mapped[bool] = mapped_column(Boolean, default=True)
    scope_negotiable: Mapped[bool] = mapped_column(Boolean, default=True)
    date_negotiable: Mapped[bool] = mapped_column(Boolean, default=True)
    # False = "ask me before agreeing any deal", even one inside the rules.
    auto_agree_within_policy: Mapped[bool] = mapped_column(Boolean, default=True)


class Client(IdMixin, Base):
    __tablename__ = "client"
    __table_args__ = (UniqueConstraint("workspace_id", "email"),)

    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspace.id"), index=True)
    name: Mapped[str] = mapped_column(String(120), default="")
    email: Mapped[str] = mapped_column(String(254))
    phone: Mapped[str] = mapped_column(String(40), default="")
    language: Mapped[str] = mapped_column(String(16), default="en")


class Quote(IdMixin, Base):
    """A priced offer the client opens at /q/{public_token}. It lives in our app:
    PayPal Invoicing has no quote endpoint (Spike 4c), so PayPal takes over at the deposit."""

    __tablename__ = "quote"
    __table_args__ = (UniqueConstraint("workspace_id", "idempotency_key"),)

    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspace.id"), index=True)
    client_id: Mapped[str | None] = mapped_column(ForeignKey("client.id"), index=True)
    idempotency_key: Mapped[str] = mapped_column(String(64))
    # The client's only credential: 128 random bits, URL-safe.
    public_token: Mapped[str] = mapped_column(String(40), unique=True)
    number: Mapped[str] = mapped_column(String(20))
    # sent | negotiating | awaiting_owner | accepted | declined | expired
    status: Mapped[str] = mapped_column(String(20), default="sent", index=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    client_name: Mapped[str] = mapped_column(String(120), default="")
    client_email: Mapped[str] = mapped_column(String(254), default="")
    # The client's language: their quote page, the Deal Agent and the messages use it.
    language: Mapped[str] = mapped_column(String(8), default="en")
    currency: Mapped[str] = mapped_column(String(3))
    total: Mapped[Decimal] = mapped_column(Money)
    deposit_pct: Mapped[Decimal] = mapped_column(Money)
    # Set when the client accepts a negotiated offer: % off the labour lines only.
    labour_discount_pct: Mapped[Decimal] = mapped_column(Money, default=Decimal(0))
    job_date: Mapped[date | None] = mapped_column(Date)
    valid_until: Mapped[date] = mapped_column(Date)
    note: Mapped[str] = mapped_column(Text, default="")
    transcript: Mapped[str] = mapped_column(Text, default="")
    raw_ai_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    # Snapshot of exactly what was accepted, so later edits can't rewrite history.
    agreed_terms: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    accepted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    lines: Mapped[list[QuoteLine]] = relationship(
        order_by="QuoteLine.position", cascade="all, delete-orphan", lazy="selectin"
    )


class DealMessage(IdMixin, Base):
    """One message in a quote's negotiation, with the offer it carries (if any).

    `offer` is what the Deal Agent proposed (no prices); `priced` is PolicyGuard's
    repricing of it; `violations` is why it was refused. Every message is kept,
    so the job's timeline shows exactly how a price was reached.
    """

    __tablename__ = "deal_message"

    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspace.id"), index=True)
    # A quote negotiation, or (kind "plan") a payment-plan request on an invoice.
    kind: Mapped[str] = mapped_column(String(10), default="quote")
    quote_id: Mapped[str | None] = mapped_column(ForeignKey("quote.id"), index=True)
    invoice_id: Mapped[str | None] = mapped_column(ForeignKey("invoice.id"), index=True)
    role: Mapped[str] = mapped_column(String(10))  # client | agent | owner
    text: Mapped[str] = mapped_column(Text)
    # none | open (client may accept) | escalated (waiting for the owner) |
    # approved | declined | countered | accepted | superseded
    offer_status: Mapped[str] = mapped_column(String(12), default="none")
    offer: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    priced: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    violations: Mapped[list[Any] | None] = mapped_column(JSON)
    # One line for the owner's approval card.
    owner_summary: Mapped[str] = mapped_column(String(500), default="")


class QuoteLine(IdMixin, Base):
    __tablename__ = "quote_line"

    quote_id: Mapped[str] = mapped_column(ForeignKey("quote.id"), index=True)
    position: Mapped[int] = mapped_column(Integer, default=0)
    name: Mapped[str] = mapped_column(String(200))
    description: Mapped[str] = mapped_column(String(1000), default="")
    quantity: Mapped[Decimal] = mapped_column(Money)
    unit_amount: Mapped[Decimal] = mapped_column(Money)
    unit: Mapped[str] = mapped_column(String(10), default="items")
    is_material: Mapped[bool] = mapped_column(Boolean, default=False)


class Invoice(IdMixin, Base):
    __tablename__ = "invoice"
    __table_args__ = (UniqueConstraint("workspace_id", "idempotency_key"),)

    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspace.id"), index=True)
    client_id: Mapped[str] = mapped_column(ForeignKey("client.id"), index=True)
    # Set for a quote's deposit invoice (and, later, its final invoice).
    quote_id: Mapped[str | None] = mapped_column(ForeignKey("quote.id"), index=True)
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
    # Invoice-level deduction (a final invoice: agreed labour discount + deposit received).
    discount: Mapped[Decimal] = mapped_column(Money, default=Decimal(0))
    amount_paid: Mapped[Decimal] = mapped_column(Money, default=Decimal(0))
    due_date: Mapped[date | None] = mapped_column(Date)
    note: Mapped[str] = mapped_column(Text, default="")
    payer_view_url: Mapped[str] = mapped_column(String(500), default="")
    # The client's link to /p/{token}: "Can't pay in full? Propose a plan."
    public_token: Mapped[str | None] = mapped_column(String(40), unique=True)
    # An agreed payment plan: {"instalments": [{"due": iso, "amount": str}], "agreed_at": iso}
    plan: Mapped[dict[str, Any] | None] = mapped_column(JSON)
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


class Reminder(IdMixin, Base):
    """One rung of the Chase Agent's ladder for one invoice: drafted, then sent or skipped."""

    __tablename__ = "reminder"

    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspace.id"), index=True)
    invoice_id: Mapped[str] = mapped_column(ForeignKey("invoice.id"), index=True)
    step: Mapped[int] = mapped_column(Integer)  # 1 friendly · 2 firm · 3 final
    status: Mapped[str] = mapped_column(String(10), default="pending")  # pending|sent|skipped
    subject: Mapped[str] = mapped_column(String(200), default="")
    note: Mapped[str] = mapped_column(Text, default="")
    # agent (ADK + PayPal MCP tools) or template (the fallback when the agent can't run)
    source: Mapped[str] = mapped_column(String(10), default="agent")
    # What the agent looked up: tool calls and PayPal's status, for the timeline and judges.
    trace: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Event(IdMixin, Base):
    """The job's audit timeline: created, sent, paid, reminded, and PayPal errors."""

    __tablename__ = "event"

    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspace.id"), index=True)
    invoice_id: Mapped[str | None] = mapped_column(ForeignKey("invoice.id"), index=True)
    kind: Mapped[str] = mapped_column(String(40))
    data: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
