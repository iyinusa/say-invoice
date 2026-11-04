"""The Chase Agent's run: who is due a reminder, at which rung, and what happens next.

All of the deciding is here, in code:
- only invoices PayPal still says are unpaid (re-read at the start of every run),
- at least a day past due,
- at most one reminder per invoice every 72 hours, counting the owner's own,
- one rung at a time: friendly, then firm (day 7+), then final (day 14+). The
  first reminder is always friendly, however late the invoice,
- approval mode by default: a draft waits for the owner's Send / Edit / Skip.

The agent (app.agents.chase) only checks the invoice with PayPal's tools and
writes the words.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from datetime import UTC, date, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.chase import (
    TONES,
    AdkChaseWriter,
    ReminderDraft,
    ReminderFacts,
    ReminderWriter,
    check_note,
    template_reminder,
)
from app.logging_config import get_logger
from app.models import Event, Invoice, Reminder, Workspace
from app.models.base import utcnow
from app.paypal.client import PayPalError
from app.services import invoice_actions, payments
from app.services.invoice_actions import ActionRefused, aware, last_reminder
from app.services.live import broker
from app.services.money_view import OPEN, balance, days_overdue
from app.services.plan_links import client_language, next_instalment
from app.settings import Settings

log = get_logger("chase")

GAP = timedelta(hours=72)
RUNG_DAYS = {1: 1, 2: 7, 3: 14}  # the days overdue at which each rung becomes due

WriterFactory = Callable[[], AbstractAsyncContextManager[ReminderWriter]]


def next_rung(days_late: int, last_rung_sent: int) -> int | None:
    """The rung to send now, or None. Climbs one rung at a time, never skips one."""
    reached = max((rung for rung, days in RUNG_DAYS.items() if days_late >= days), default=0)
    rung = min(reached, last_rung_sent + 1)
    return rung if rung > last_rung_sent else None


async def _last_rung(session: AsyncSession, inv: Invoice) -> int:
    """The highest rung sent since the client last paid something: a payment resets the ladder,
    so someone keeping to a plan gets a friendly nudge for a later instalment, not a final one."""
    paid_at = await session.scalar(
        select(Event.created_at)
        .where(Event.invoice_id == inv.id, Event.kind == "payment_received")
        .order_by(Event.created_at.desc())
        .limit(1)
    )
    query = select(Reminder.step).where(Reminder.invoice_id == inv.id, Reminder.status == "sent")
    if paid_at is not None:
        query = query.where(Reminder.decided_at > paid_at)
    return max(await session.scalars(query), default=0)


async def _pending(session: AsyncSession, inv: Invoice) -> Reminder | None:
    return await session.scalar(
        select(Reminder).where(Reminder.invoice_id == inv.id, Reminder.status == "pending")
    )


def facts_for(inv: Invoice, ws: Workspace, today: date, rung: int) -> ReminderFacts:
    return ReminderFacts(
        business=ws.business_name,
        owner=ws.owner_name or ws.business_name,
        client_name=inv.client.name,
        number=inv.number,
        paypal_invoice_id=inv.paypal_invoice_id or "",
        currency=inv.currency,
        total=inv.total,
        amount_paid=inv.amount_paid,
        balance=balance(inv),
        due_date=inv.due_date,
        days_overdue=days_overdue(inv, today),
        step=rung,
        instalment=nxt[1] if (nxt := next_instalment(inv)) else None,
        instalment_due=nxt[0] if nxt else None,
        language=client_language(inv),
    )


def reminder_view(r: Reminder, inv: Invoice, today: date) -> dict[str, Any]:
    from app.money import display

    return {
        "id": r.id,
        "invoice_id": inv.id,
        "number": inv.number,
        "client": inv.client.name or inv.client.email,
        "step": r.step,
        "tone": TONES[r.step][0],
        "subject": r.subject,
        "note": r.note,
        "source": r.source,
        "tool_calls": [c.get("tool") for c in (r.trace or {}).get("tool_calls", [])],
        "balance": display(balance(inv), inv.currency),
        "days_overdue": days_overdue(inv, today),
        "status": r.status,
    }


async def due_invoices(
    session: AsyncSession, paypal: Any, ws: Workspace, *, today: date, now: datetime
) -> list[tuple[Invoice, int]]:
    """Invoices owed a reminder right now, with the rung each is owed."""
    open_invoices = list(
        await session.scalars(
            select(Invoice).where(
                Invoice.workspace_id == ws.id,
                Invoice.status.in_(OPEN),
                Invoice.paypal_invoice_id.is_not(None),
            )
        )
    )
    due: list[tuple[Invoice, int]] = []
    for inv in open_invoices:
        try:
            await payments.refresh(session, paypal, inv)  # never chase money already paid
        except PayPalError as exc:
            log.warning("chase_refresh_failed", invoice_id=inv.id, error=str(exc))
            continue
        if inv.status not in OPEN or await _pending(session, inv) is not None:
            continue
        last = await last_reminder(session, inv)
        if last is not None and now - aware(last.created_at) < GAP:
            continue
        # The owner said "Skip": that means not now, so the 72 hours apply to it too.
        skipped = await session.scalar(
            select(Reminder.decided_at)
            .where(Reminder.invoice_id == inv.id, Reminder.status == "skipped")
            .order_by(Reminder.decided_at.desc())
            .limit(1)
        )
        if skipped is not None and now - aware(skipped) < GAP:
            continue
        rung = next_rung(days_overdue(inv, today), await _last_rung(session, inv))
        if rung is not None:
            due.append((inv, rung))
    return due


async def run(
    session: AsyncSession,
    paypal: Any,
    ws: Workspace,
    writer_factory: WriterFactory,
    *,
    today: date,
    now: datetime | None = None,
) -> dict[str, Any]:
    now = now or utcnow()
    due = await due_invoices(session, paypal, ws, today=today, now=now)
    results: list[dict[str, Any]] = []
    if due:
        async with writer_factory() as writer:
            gate = asyncio.Semaphore(3)

            async def write(facts: ReminderFacts) -> ReminderDraft:
                async with gate:
                    try:
                        return await writer(facts)
                    except Exception as exc:  # the agent failing must never stop the chase
                        log.warning(
                            "chase_writer_failed", invoice=facts.number, error=repr(exc)[:300]
                        )
                        return template_reminder(facts, f"the agent failed: {type(exc).__name__}")

            # The agent works on up to three invoices at once; the database is written one by one.
            all_facts = [facts_for(inv, ws, today, rung) for inv, rung in due]
            drafts = await asyncio.gather(*(write(f) for f in all_facts))
            for (inv, rung), facts, draft in zip(due, all_facts, drafts, strict=True):
                if draft.skip_reason:
                    results.append(
                        {"number": inv.number, "status": "skipped", "why": draft.skip_reason}
                    )
                    continue
                if check_note(draft.note, facts):  # belt and braces: the writer checks too
                    draft = template_reminder(facts, "note failed the final check")
                r = await _save(session, ws, inv, rung, draft)
                if ws.auto_chase:
                    try:
                        await send(session, paypal, ws, r, now=now)
                    except (ActionRefused, PayPalError) as exc:
                        log.warning("chase_send_failed", invoice_id=inv.id, error=str(exc))
                else:
                    broker.publish(ws.id, {"type": "reminder", **reminder_view(r, inv, today)})
                results.append(
                    {
                        "number": inv.number,
                        "client": inv.client.name,
                        "step": rung,
                        "tone": TONES[rung][0],
                        "status": r.status,
                        "source": r.source,
                    }
                )
    summary = {
        "checked_at": now.isoformat(),
        "due": len(due),
        "drafted": sum(1 for r in results if r["status"] == "pending"),
        "sent": sum(1 for r in results if r["status"] == "sent"),
        "skipped": sum(1 for r in results if r["status"] == "skipped"),
        "agent": any(r.get("source") == "agent" for r in results),
        "auto": ws.auto_chase,
        "results": results,
    }
    log.info(
        "chase_run", workspace_id=ws.id, **{k: v for k, v in summary.items() if k != "results"}
    )
    return summary


async def _save(
    session: AsyncSession, ws: Workspace, inv: Invoice, rung: int, draft: ReminderDraft
) -> Reminder:
    r = Reminder(
        workspace_id=ws.id, invoice_id=inv.id, step=rung, subject=draft.subject,
        note=draft.note, source=draft.source, trace=draft.trace,
    )  # fmt: skip
    session.add(r)
    await session.commit()
    return r


async def send(
    session: AsyncSession,
    paypal: Any,
    ws: Workspace,
    r: Reminder,
    *,
    note: str | None = None,
    now: datetime | None = None,
) -> Reminder:
    """Send a drafted reminder (the owner's Send, or automatic mode), re-checking everything."""
    if r.status != "pending":
        raise ActionRefused("this reminder has already been dealt with")
    inv = await session.get(Invoice, r.invoice_id)
    assert inv is not None
    await payments.refresh(session, paypal, inv)
    if inv.status not in OPEN:
        r.status, r.decided_at = "skipped", utcnow()
        await session.commit()
        raise ActionRefused("the invoice has been paid or cancelled since, so it wasn't sent")
    if note is not None:
        r.note = note.strip()[:1000]
    await invoice_actions.remind(
        session, paypal, ws, inv, note=r.note, by="chase", gap=GAP, subject=r.subject,
        extra={"step": r.step, "tone": TONES[r.step][0], "source": r.source}, now=now,
    )  # fmt: skip
    r.status, r.decided_at = "sent", utcnow()
    await session.commit()
    return r


async def skip(session: AsyncSession, r: Reminder) -> Reminder:
    if r.status != "pending":
        raise ActionRefused("this reminder has already been dealt with")
    r.status, r.decided_at = "skipped", utcnow()
    await session.commit()
    return r


async def pending(session: AsyncSession, ws: Workspace, today: date) -> list[dict[str, Any]]:
    rows = await session.execute(
        select(Reminder, Invoice)
        .join(Invoice, Invoice.id == Reminder.invoice_id)
        .where(Reminder.workspace_id == ws.id, Reminder.status == "pending")
        .order_by(Reminder.created_at)
    )
    return [reminder_view(r, inv, today) for r, inv in rows.all()]


def chase_writer(settings: Settings, paypal: Any) -> WriterFactory:
    """The ADK agent with PayPal's MCP tools, or the template if that can't start."""

    @asynccontextmanager
    async def factory() -> AsyncIterator[ReminderWriter]:
        try:
            token = await paypal.access_token()
            writer = AdkChaseWriter(settings, token)
            await writer.__aenter__()
        except Exception as exc:
            log.warning("chase_agent_unavailable", error=repr(exc)[:300])

            async def fallback(facts: ReminderFacts) -> ReminderDraft:
                return template_reminder(facts, "the agent couldn't start")

            yield fallback
            return
        try:
            yield writer
        finally:
            await writer.__aexit__(None, None, None)

    return factory


def today_utc() -> date:
    return datetime.now(UTC).date()


def today_for(ws: Workspace) -> date:
    from app.services.clock import today_for as _today_for

    return _today_for(ws)
