"""Payment plans: proposed by the agent as a shape, split by code, checked, then put on PayPal."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.db import get_sessionmaker
from app.deals.agent import get_plan_model
from app.deals.plans import AgentPlan, PlanTurn
from app.main import app
from app.models import Event, Invoice
from app.paypal import get_paypal
from app.routers.chase import get_writer_factory
from tests.test_chase import ScriptedWriter
from tests.test_invoices_api import FakePayPal, _payload

TODAY = datetime.now(UTC).date()


class ScriptedPlanAgent:
    def __init__(self) -> None:
        self.turns: list[PlanTurn] = []
        self.calls: list[tuple[str, str]] = []

    async def __call__(self, system: str, conversation: str) -> PlanTurn:
        self.calls.append((system, conversation))
        return self.turns.pop(0)


def plan(reply: str, n: int | None = None, first: int = 0, every: int = 30,
         needs_owner: bool = False, summary: str = "") -> PlanTurn:  # fmt: skip
    shape = AgentPlan(instalments=n, first_due_in_days=first, every_days=every) if n else None
    return PlanTurn(reply=reply, plan=shape, needs_owner=needs_owner, owner_summary=summary)


@pytest.fixture
def paypal() -> Iterator[FakePayPal]:
    fake = FakePayPal()
    app.dependency_overrides[get_paypal] = lambda: fake
    yield fake
    app.dependency_overrides.pop(get_paypal, None)


@pytest.fixture
def agent() -> Iterator[ScriptedPlanAgent]:
    fake = ScriptedPlanAgent()
    app.dependency_overrides[get_plan_model] = lambda: fake
    yield fake
    app.dependency_overrides.pop(get_plan_model, None)


def _invoice(client: TestClient) -> tuple[str, str]:
    inv = client.post("/api/invoices", json=_payload()).json()

    async def token() -> str:
        async with get_sessionmaker()() as s:
            return str((await s.get(Invoice, inv["id"])).public_token)

    return inv["id"], client.portal.call(token)  # type: ignore[union-attr]


def _row(client: TestClient, invoice_id: str) -> Invoice:
    async def run() -> Invoice:
        async with get_sessionmaker()() as s:
            return await s.get(Invoice, invoice_id)  # type: ignore[return-value]

    return client.portal.call(run)  # type: ignore[union-attr]


def _say(client: TestClient, token: str, text: str) -> Any:
    return client.post(f"/p/{token}/messages", json={"text": text})


def test_every_invoice_has_a_plan_page(client: TestClient, paypal: FakePayPal) -> None:
    _, token = _invoice(client)
    assert f"/p/{token}" in paypal.last("create")["body"]["detail"]["note"]
    page = client.get(f"/p/{token}")
    assert page.status_code == 200
    assert "pay it all at once?" in page.text and "£145.00" in page.text
    assert client.get("/p/nope").status_code == 404


def test_half_now_half_at_the_end_of_the_month(
    client: TestClient, paypal: FakePayPal, agent: ScriptedPlanAgent
) -> None:
    _, token = _invoice(client)
    agent.turns = [plan("Of course, two payments works.", n=2, first=0, every=30)]
    state = _say(client, token, "Can I pay half now and half at the end of the month?").json()
    offer = state["messages"][-1]
    assert offer["offer_status"] == "open"
    assert [r["amount"] for r in offer["plan"]] == ["£72.50", "£72.50"]  # code split it
    system = agent.calls[0][0]
    assert "up to 3 payments" in system and "£145.00" in system


def test_too_many_payments_gets_one_retry(
    client: TestClient, paypal: FakePayPal, agent: ScriptedPlanAgent
) -> None:
    _, token = _invoice(client)
    agent.turns = [
        plan("Six it is!", n=6, every=14),
        plan("I can do three payments.", n=3, every=20),
    ]
    offer = _say(client, token, "Can I pay over six months?").json()["messages"][-1]
    assert "too_many_instalments" in agent.calls[1][1]
    assert [r["amount"] for r in offer["plan"]] == ["£48.34", "£48.33", "£48.33"]


def test_amounts_the_agent_writes_are_caught(
    client: TestClient, paypal: FakePayPal, agent: ScriptedPlanAgent
) -> None:
    _, token = _invoice(client)
    agent.turns = [plan("Pay £10 a week!", n=2), plan("Two payments it is.", n=2)]
    msg = _say(client, token, "Can I split it?").json()["messages"][-1]
    assert "wrong_amount_in_reply" in agent.calls[1][1] and "£10" not in msg["text"]


def test_outside_the_rules_goes_to_the_owner_who_approves(
    client: TestClient, paypal: FakePayPal, agent: ScriptedPlanAgent
) -> None:
    inv_id, token = _invoice(client)
    long_plan = plan("Sure.", n=6, every=30, summary="Wants 6 monthly payments")
    agent.turns = [long_plan, long_plan]
    state = _say(client, token, "£25 a month for six months?").json()
    assert state["status"] == "awaiting_owner"
    assert "Let me check that with Dave" in state["messages"][-1]["text"]
    card = client.get("/api/approvals").json()[0]
    assert card["kind"] == "plan" and card["has_offer"] is True
    assert card["plan_text"].count("£24.1") == 6 and "too_many_instalments" in card["violations"]
    assert _say(client, token, "hello?").status_code == 409  # waits for the owner

    url = f"/api/invoices/{inv_id}/plan-messages/{card['message_id']}/decision"
    resp = client.post(url, json={"action": "approve"})
    assert resp.status_code == 200 and resp.json()["message"]["offer_status"] == "open"
    assert len(resp.json()["message"]["plan"]) == 6  # the owner may go past the rules


def test_agreeing_puts_the_plan_on_the_paypal_invoice(
    client: TestClient, paypal: FakePayPal, agent: ScriptedPlanAgent
) -> None:
    inv_id, token = _invoice(client)
    agent.turns = [plan("Two payments it is.", n=2, every=30)]
    offer = _say(client, token, "Half now, half next month?").json()["messages"][-1]
    state = client.post(f"/p/{token}/plans/{offer['id']}/accept").json()
    assert [r["status"] for r in state["agreed"]] == ["upcoming", "upcoming"]  # first is today

    body = paypal.last("update")["body"]
    assert body["configuration"]["partial_payment"] == {
        "allow_partial_payment": True,
        "minimum_amount_due": {"currency_code": "GBP", "value": "72.50"},
    }
    last_due = (TODAY + timedelta(days=30)).isoformat()
    assert body["detail"]["payment_term"]["due_date"] == last_due
    assert "Payment plan agreed: £72.50 by" in body["detail"]["note"]

    row = _row(client, inv_id)
    assert row.due_date.isoformat() == last_due and len(row.plan["instalments"]) == 2
    assert _say(client, token, "Actually...").status_code == 409  # one plan per invoice


def _put_on_plan(client: TestClient, inv_id: str, first_due_days_ago: int) -> None:
    async def run() -> None:
        async with get_sessionmaker()() as s:
            inv = await s.get(Invoice, inv_id)
            first = TODAY - timedelta(days=first_due_days_ago)
            inv.plan = {
                "currency": "GBP",
                "balance": "145.00",
                "instalments": [
                    {"due": first.isoformat(), "amount": "72.50"},
                    {"due": (first + timedelta(days=30)).isoformat(), "amount": "72.50"},
                ],
            }
            inv.due_date = first + timedelta(days=30)
            await s.commit()

    client.portal.call(run)  # type: ignore[union-attr]


@pytest.fixture
def writer() -> Iterator[ScriptedWriter]:
    w = ScriptedWriter()
    app.dependency_overrides[get_writer_factory] = w.factory
    yield w
    app.dependency_overrides.pop(get_writer_factory, None)


def test_the_chase_agent_follows_the_plan(
    client: TestClient, paypal: FakePayPal, writer: ScriptedWriter
) -> None:
    inv_id, _ = _invoice(client)
    _put_on_plan(client, inv_id, first_due_days_ago=3)  # final date is weeks away...
    client.post("/api/chase/run")
    f = writer.facts[0]  # ...but the first instalment is 3 days late
    assert (f.days_overdue, f.instalment, f.instalment_due) == (
        3, Decimal("72.50"), TODAY - timedelta(days=3),
    )  # fmt: skip
    from app.agents.chase import template_reminder

    note = template_reminder(f).note
    assert "instalment of £72.50" in note


def test_a_payment_resets_the_ladder(
    client: TestClient, paypal: FakePayPal, writer: ScriptedWriter
) -> None:
    inv_id, _ = _invoice(client)
    _put_on_plan(client, inv_id, first_due_days_ago=40)  # both instalments late
    client.post("/api/chase/run")
    rid = client.get("/api/reminders").json()[0]["id"]
    client.post(f"/api/reminders/{rid}/send", json={})

    async def paid_one_and_wait() -> None:
        async with get_sessionmaker()() as s:
            inv = await s.get(Invoice, inv_id)
            inv.amount_paid = Decimal("72.50")
            s.add(Event(workspace_id=inv.workspace_id, invoice_id=inv.id, kind="payment_received",
                        data={"amount": "72.50"}))  # fmt: skip
            # Make the last reminder older than 72 hours.
            for ev in await s.scalars(select(Event).where(Event.kind == "reminder_sent")):
                ev.created_at = datetime.now(UTC) - timedelta(days=4)
            await s.commit()

    client.portal.call(paid_one_and_wait)  # type: ignore[union-attr]
    paid = {"paid_amount": {"currency_code": "GBP", "value": "72.50"}}
    paypal.remote["INV2-1"] = {"id": "INV2-1", "status": "PARTIALLY_PAID", "payments": paid}
    client.post("/api/chase/run")
    assert writer.facts[-1].step == 1  # friendly again for the next instalment


def test_reminders_offer_a_plan_until_one_is_agreed(client: TestClient, paypal: FakePayPal) -> None:
    inv_id, token = _invoice(client)
    client.post(f"/api/invoices/{inv_id}/remind", json={})
    assert f"/p/{token}" in paypal.last("remind")["note"]


def test_no_plans_for_paid_invoices(
    client: TestClient, paypal: FakePayPal, agent: ScriptedPlanAgent
) -> None:
    inv_id, token = _invoice(client)
    client.post(f"/api/invoices/{inv_id}/record-payment", json={"method": "CASH"})
    assert _say(client, token, "Can I split it?").status_code == 409
