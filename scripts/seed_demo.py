"""Seed a demo workspace: one realistic fortnight for a plumber, in your PayPal sandbox.

    uv run alembic upgrade head
    uv run python -m scripts.seed_demo

Everything goes through the app's own services, so it is real end to end: every
invoice and deposit exists in the PayPal sandbox (Remind, the QR code and paying
from PayPal's page all work), prices come from PolicyGuard, and payments are
recorded in PayPal and read back. Only the clients' chat turns are scripted, so
no Gemini key is needed.

What you get ("Who owes me" and the job pages):
  - Mr Kennedy      invoice, paid by bank transfer
  - Ms Murphy       invoice, sent, due in 10 days
  - Mrs Okafor      invoice, 12 days overdue (the Chase Agent has work to do)
  - Mr Wójcik       invoice, overdue, now on an agreed 3-payment plan, 1st paid
  - Mrs Patel       quote, negotiating: the agent offered 10% off labour
  - Mr Adeyemi      quote, waiting for you: asked for 25% off (over your limit)
  - Ms Chen         quote accepted, 30% deposit paid: booked, final invoice to send
  - Mr Brennan      quote, just sent, no reply yet

Safe to run again: every job has a fixed idempotency key, so a second run finds
the first one's jobs and adds nothing. Sandbox only: it refuses a live account.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.exc import OperationalError, ProgrammingError

from app.db import get_sessionmaker
from app.deals import negotiator, plans
from app.deals.agent import AgentLine, AgentOffer, AgentTurn
from app.deals.plans import AgentPlan, PlanTurn
from app.models import DealMessage, Invoice, Quote, Workspace
from app.money import display
from app.paypal import get_paypal
from app.schemas.invoice import InvoiceIn, InvoiceItemIn
from app.schemas.quote import AcceptIn, QuoteIn
from app.services import invoice_actions, quotes
from app.services.clock import real_today
from app.services.invoicing import create_and_send, default_workspace
from app.services.policy import get_policy
from app.settings import get_settings

KEY = "seed-v1"  # bump to seed a second, separate set


class Scripted:
    """Stands in for Gemini: replays the turns a real agent would plausibly take."""

    def __init__(self, *turns: Any) -> None:
        self.turns = list(turns)

    async def __call__(self, system: str, conversation: str) -> Any:
        return self.turns.pop(0) if len(self.turns) > 1 else self.turns[0]


def item(
    name: str, qty: str, price: str, *, unit: str = "items", material: bool = False
) -> InvoiceItemIn:
    return InvoiceItemIn(
        name=name, quantity=Decimal(qty), unit_price=Decimal(price), unit=unit, is_material=material
    )


def keep(*quantities: int) -> list[AgentLine]:
    """Keep every line of the quote (L1, L2, …) at these quantities."""
    return [AgentLine(line=f"L{n}", quantity=q) for n, q in enumerate(quantities, start=1)]


def say(text: str) -> None:
    print(f"  {text}", flush=True)


async def _chatted(session: Any, q: Quote) -> bool:
    n = await session.scalar(select(func.count()).where(DealMessage.quote_id == q.id))
    return bool(n)


async def seed(session: Any, paypal: Any, ws: Workspace) -> None:
    today = real_today()
    policy = await get_policy(session, ws)
    cur = ws.currency

    async def invoice(slug: str, name: str, email: str, items: list[InvoiceItemIn], due_in: int,
                      notes: str = "") -> Invoice:  # fmt: skip
        data = InvoiceIn(
            idempotency_key=f"{KEY}-inv-{slug}", client_name=name, client_email=email, currency=cur,
            items=items, due_date=today + timedelta(days=due_in), notes=notes,
            transcript=f"(demo) {name}",
        )  # fmt: skip
        inv = await create_and_send(session, paypal, ws, data, today=today)
        if due_in < 0 and inv.sent_at and inv.sent_at.date() == today:
            # It went out when it was issued, before it fell due (see invoice_date_for).
            issued = today + timedelta(days=due_in - ws.default_due_days)
            inv.sent_at = datetime.combine(issued, datetime.min.time(), UTC) + timedelta(hours=9)
            await session.commit()
        return inv

    async def quote(slug: str, name: str, email: str | None, items: list[InvoiceItemIn],
                    deposit: str = "30", job_in: int | None = None) -> Quote:  # fmt: skip
        data = QuoteIn(
            idempotency_key=f"{KEY}-quote-{slug}", client_name=name, client_email=email,
            currency=cur, items=items, deposit_pct=Decimal(deposit),
            job_date=today + timedelta(days=job_in) if job_in is not None else None,
            transcript=f"(demo) quote for {name}",
        )  # fmt: skip
        return await quotes.create_quote(session, ws, policy, data, today=today)

    # 1. Paid.
    inv = await invoice("kennedy", "Mr Kennedy", "kennedy@example.com", [
        item("Boiler repair", "2", "60", unit="hours"),
        item("Pressure valve", "1", "25", material=True),
    ], due_in=7, notes="Thanks for having me round.")  # fmt: skip
    if inv.status in ("sent", "partially_paid"):
        await invoice_actions.record_payment(session, paypal, ws, inv, amount=None,
                                             method="BANK_TRANSFER", today=today)  # fmt: skip
    say(f"Mr Kennedy     invoice {inv.number}: {inv.status}")

    # 2. Sent, not yet due.
    inv = await invoice("murphy", "Ms Murphy", "murphy@example.com", [
        item("Radiator valve replacement", "1", "85", unit="fixed"),
        item("Thermostatic valves", "2", "18.50", material=True),
    ], due_in=10)  # fmt: skip
    say(f"Ms Murphy      invoice {inv.number}: {inv.status}, due {inv.due_date}")

    # 3. Overdue.
    inv = await invoice("okafor", "Mrs Okafor", "okafor@example.com", [
        item("Bathroom tap replacement", "1.5", "60", unit="hours"),
        item("Mixer tap", "1", "122.40", material=True),
    ], due_in=-12)  # fmt: skip
    say(f"Mrs Okafor     invoice {inv.number}: {inv.status}, due {inv.due_date} (overdue)")

    # 4. Overdue, then a payment plan agreed on the client's plan page, first payment made.
    inv = await invoice("wojcik", "Mr Wójcik", "wojcik@example.com", [
        item("Central heating power flush", "1", "320", unit="fixed"),
        item("Inhibitor and cleaner", "1", "40", material=True),
    ], due_in=-5)  # fmt: skip
    if inv.plan is None and inv.status == "sent":
        offer = PlanTurn(
            reply="Of course. Three payments, two weeks apart, starting today.",
            plan=AgentPlan(instalments=3, first_due_in_days=0, every_days=14),
        )
        added = await plans.client_message(
            session, Scripted(offer), inv,
            "Money's tight this month. Could I pay this in three goes?", today=today,
        )  # fmt: skip
        open_offer = next(m for m in added if m.offer_status == "open")
        await plans.accept(session, paypal, inv, open_offer.id, today=today)
        first = Decimal(inv.plan["instalments"][0]["amount"])  # type: ignore[index]
        await invoice_actions.record_payment(session, paypal, ws, inv, amount=first,
                                             method="BANK_TRANSFER", today=today)  # fmt: skip
    say(f"Mr Wójcik      invoice {inv.number}: {inv.status}, plan of "
        f"{len((inv.plan or {}).get('instalments', []))}")  # fmt: skip

    # 5. Quote being negotiated: the agent counters inside the rules.
    q = await quote("patel", "Mrs Patel", "patel@example.com", [
        item("Replace kitchen tap", "1", "90", unit="fixed"),
        item("Tap and fittings", "1", "40", material=True),
    ], job_in=6)  # fmt: skip
    if not await _chatted(session, q):
        counter = AgentTurn(
            reply="I can take 10% off the labour; the parts stay as they are. "
            "Tap Accept if that works.",
            offer=AgentOffer(lines=keep(1, 1), labour_discount_pct=10, deposit_pct=30),
        )
        ask_for = display(Decimal(110), cur)
        await negotiator.client_message(
            session, Scripted(counter), q, f"Could you do it for {ask_for}?", today=today
        )
    say(f"Mrs Patel      quote {q.number}: negotiating")

    # 6. Quote waiting for the owner: the client wants more than the rules allow.
    q = await quote("adeyemi", "Mr Adeyemi", "adeyemi@example.com", [
        item("Bathroom refit: labour", "16", "25", unit="hours"),
        item("Shower valve and fittings", "1", "80", material=True),
    ], job_in=14)  # fmt: skip
    if not await _chatted(session, q):
        limit = f"{policy.max_discount_pct.normalize():f}%"
        ask = AgentTurn(
            reply="Let me check that with the owner.",
            needs_owner=True,
            owner_summary=f"Wants 25% off for paying up front; your limit is {limit} off labour.",
            offer=AgentOffer(lines=keep(16, 1), labour_discount_pct=25, deposit_pct=30),
        )
        await negotiator.client_message(
            session, Scripted(ask), q, "Can you do 25% off if I pay up front?", today=today
        )
    say(f"Mr Adeyemi     quote {q.number}: {q.status}")

    # 7. Booked: accepted, deposit paid; the final invoice is yours to send after the job.
    q = await quote("chen", "Ms Chen", "chen@example.com", [
        item("Annual boiler service", "1", "95", unit="fixed"),
        item("Replace expansion vessel", "2", "45", unit="hours"),
        item("Expansion vessel", "1", "55", material=True),
    ], job_in=3)  # fmt: skip
    if q.status == "sent":
        await quotes.accept(
            session, paypal, q, AcceptIn(email="chen@example.com", name="Ms Chen"), today=today
        )
    deposit = await quotes.deposit_invoice(session, q)
    if deposit is not None and deposit.status in ("sent", "partially_paid"):
        await invoice_actions.record_payment(session, paypal, ws, deposit, amount=None,
                                             method="BANK_TRANSFER", today=today)  # fmt: skip
    await session.refresh(q)
    say(f"Ms Chen        quote {q.number}: {q.status}, deposit "
        f"{deposit.status if deposit else 'none'}")  # fmt: skip

    # 8. Just sent.
    q = await quote("brennan", "Mr Brennan", None, [
        item("Fix leaking overflow pipe", "1", "75", unit="fixed"),
        item("Overflow fittings", "1", "20", material=True),
    ], deposit="0")  # fmt: skip
    say(f"Mr Brennan     quote {q.number}: {q.status}")


async def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--business", default="Dave's Plumbing & Heating")
    parser.add_argument("--owner", default="Dave")
    args = parser.parse_args(argv)

    settings = get_settings()
    if not settings.paypal_configured:
        print("PayPal isn't configured. Set SI_PAYPAL_CLIENT_ID and SI_PAYPAL_CLIENT_SECRET.")
        return 2
    if settings.paypal_env != "sandbox":
        print(
            "Refusing to seed a live PayPal account: it would send real invoices. Use the sandbox."
        )
        return 2

    paypal = get_paypal()
    async with get_sessionmaker()() as session:
        try:
            ws = await default_workspace(session, settings)
        except (OperationalError, ProgrammingError):
            print("The database isn't set up yet. Run: uv run alembic upgrade head")
            return 2
        if not ws.onboarded:
            ws.business_name, ws.owner_name, ws.onboarded = args.business, args.owner, True
            await session.commit()
        print(f"Seeding {ws.business_name} ({ws.currency}) in the PayPal sandbox…")
        await seed(session, paypal, ws)
    print("Done. Open the app and tap “Who owes me”.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main(sys.argv[1:])))
