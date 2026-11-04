"""The Chase Agent: who gets chased, how hard, and what can never happen."""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.agents.chase import ReminderDraft, ReminderFacts, check_note, template_reminder
from app.db import get_sessionmaker
from app.main import app
from app.models import Event, Invoice, Reminder, Workspace
from app.paypal import get_paypal
from app.routers.chase import get_writer_factory
from app.services import chase
from app.services.live import broker
from tests.test_invoices_api import FakePayPal, _payload

TODAY = datetime.now(UTC).date()


class ScriptedWriter:
    """Stands in for the ADK agent. Records the facts it was given."""

    def __init__(self) -> None:
        self.facts: list[ReminderFacts] = []
        self.opened = 0
        self.next: ReminderDraft | Exception | None = None

    def factory(self):  # type: ignore[no-untyped-def]
        @asynccontextmanager
        async def cm() -> AsyncIterator[Any]:
            self.opened += 1
            yield self.write

        return cm

    async def write(self, f: ReminderFacts) -> ReminderDraft:
        self.facts.append(f)
        if isinstance(self.next, Exception):
            raise self.next
        if self.next is not None:
            return self.next
        tone = {1: "Friendly", 2: "Firm", 3: "Final"}[f.step]
        return ReminderDraft(
            subject=f"{tone}: invoice {f.number}", source="agent",
            note=f"{tone} note: £{f.balance} is due on invoice {f.number}. Dave",
            trace={"tool_calls": [{"tool": "get_invoice", "args": {"id": f.paypal_invoice_id}}]},
        )  # fmt: skip


@pytest.fixture
def paypal() -> Iterator[FakePayPal]:
    fake = FakePayPal()
    app.dependency_overrides[get_paypal] = lambda: fake
    yield fake
    app.dependency_overrides.pop(get_paypal, None)


@pytest.fixture
def writer() -> Iterator[ScriptedWriter]:
    w = ScriptedWriter()
    app.dependency_overrides[get_writer_factory] = w.factory
    yield w
    app.dependency_overrides.pop(get_writer_factory, None)


def _overdue_invoice(client: TestClient, days: int, key: str = "inv-a-000001") -> str:
    inv = client.post("/api/invoices", json=_payload(idempotency_key=key)).json()

    async def run() -> None:
        async with get_sessionmaker()() as s:
            row = await s.get(Invoice, inv["id"])
            row.due_date = TODAY - timedelta(days=days)
            await s.commit()

    client.portal.call(run)  # type: ignore[union-attr]
    return str(inv["id"])


def _run_at(client: TestClient, paypal: FakePayPal, writer: ScriptedWriter, now: datetime,
            days_shift: int = 0) -> dict[str, Any]:  # fmt: skip
    async def run() -> dict[str, Any]:
        async with get_sessionmaker()() as s:
            ws = await s.scalar(select(Workspace))
            return await chase.run(s, paypal, ws, writer.factory(),
                                   today=TODAY + timedelta(days=days_shift), now=now)  # fmt: skip

    return client.portal.call(run)  # type: ignore[union-attr]


def _reminders(client: TestClient) -> list[Reminder]:
    async def run() -> list[Reminder]:
        async with get_sessionmaker()() as s:
            return list(await s.scalars(select(Reminder).order_by(Reminder.created_at)))

    return client.portal.call(run)  # type: ignore[union-attr]


def _paid(status: str) -> dict[str, Any]:
    paid = {"paid_amount": {"currency_code": "GBP", "value": "145.00"}}
    return {"id": "INV2-1", "status": status, "payments": paid}


# ── the ladder ────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("days", "last", "expected"),
    [
        (0, 0, None),  # not overdue yet
        (1, 0, 1),
        (6, 1, None),  # friendly sent, firm not due until day 7
        (7, 1, 2),
        (8, 0, 1),  # never sent anything: start friendly, however late
        (20, 0, 1),
        (20, 1, 2),  # one rung at a time
        (20, 2, 3),
        (30, 3, None),  # ladder finished
    ],
)
def test_next_rung(days: int, last: int, expected: int | None) -> None:
    assert chase.next_rung(days, last) == expected


def _facts(**kw: Any) -> ReminderFacts:
    base: dict[str, Any] = dict(
        business="Dave's Plumbing", owner="Dave", client_name="Tom Kennedy", number="0001",
        paypal_invoice_id="INV2-1", currency="GBP", total=Decimal("145.00"),
        amount_paid=Decimal("45.00"), balance=Decimal("100.00"), due_date=TODAY,
        days_overdue=9, step=2,
    )  # fmt: skip
    base.update(kw)
    return ReminderFacts(**base)


def test_template_wording_climbs_and_never_threatens() -> None:
    notes = [template_reminder(_facts(step=s)).note for s in (1, 2, 3)]
    assert "friendly reminder" in notes[0] and "£100.00" in notes[0]
    assert "9 days overdue" in notes[1]
    assert "final reminder" in notes[2]
    for note in notes:
        assert not any(w in note.lower() for w in ("legal", "court", "collector", "fee"))
        assert check_note(note, _facts()) == ""


def test_notes_may_only_quote_paypal_amounts() -> None:
    f = _facts()
    assert check_note("£100.00 is still due (£45.00 paid of £145.00).", f) == ""
    assert "amounts not on the invoice" in check_note("Please pay £1,000 today.", f)
    assert check_note("", f) == "empty note"


# ── a run ─────────────────────────────────────────────────────────────────────


def test_nothing_overdue_means_the_agent_never_starts(
    client: TestClient, paypal: FakePayPal, writer: ScriptedWriter
) -> None:
    client.post("/api/invoices", json=_payload())
    summary = client.post("/api/chase/run").json()
    assert summary["due"] == 0
    assert writer.opened == 0  # no MCP server spawned for nothing


def test_approval_mode_drafts_and_waits_for_the_owner(
    client: TestClient, paypal: FakePayPal, writer: ScriptedWriter
) -> None:
    _overdue_invoice(client, days=2)
    ws_id = client.portal.call(_ws_id)  # type: ignore[union-attr]
    with broker.subscribe(ws_id) as queue:
        summary = client.post("/api/chase/run").json()
        events = [queue.get_nowait() for _ in range(queue.qsize())]
    assert (summary["due"], summary["drafted"], summary["sent"]) == (1, 1, 0)
    assert paypal.count("remind") == 0  # nothing goes out without the owner
    card = next(e for e in events if e["type"] == "reminder")
    assert card["tone"] == "friendly" and card["tool_calls"] == ["get_invoice"]
    assert writer.facts[0].days_overdue == 2 and writer.facts[0].balance == Decimal("145.00")
    pending = client.get("/api/reminders").json()
    assert [p["tone"] for p in pending] == ["friendly"]

    # A second run doesn't draft a duplicate while one is waiting.
    assert client.post("/api/chase/run").json()["due"] == 0


async def _ws_id() -> str:
    async with get_sessionmaker()() as s:
        return str(await s.scalar(select(Workspace.id)))


def test_owner_sends_an_edited_draft(
    client: TestClient, paypal: FakePayPal, writer: ScriptedWriter
) -> None:
    _overdue_invoice(client, days=2)
    client.post("/api/chase/run")
    rid = client.get("/api/reminders").json()[0]["id"]
    resp = client.post(
        f"/api/reminders/{rid}/send", json={"note": "Hi Tom, a quick nudge on £145.00. Dave"}
    )
    assert resp.json() == {"status": "sent"}
    call = paypal.last("remind")
    assert call["subject"] == "Friendly: invoice 0001" and call["note"].startswith("Hi Tom")

    async def last_event() -> dict[str, Any]:
        async with get_sessionmaker()() as s:
            ev = await s.scalar(select(Event).where(Event.kind == "reminder_sent"))
            return dict(ev.data)

    data = client.portal.call(last_event)  # type: ignore[union-attr]
    assert (data["by"], data["step"], data["tone"]) == ("chase", 1, "friendly")
    assert client.post(f"/api/reminders/{rid}/send", json={}).status_code == 409  # once only


def test_72_hours_between_reminders_then_the_next_rung(
    client: TestClient, paypal: FakePayPal, writer: ScriptedWriter
) -> None:
    _overdue_invoice(client, days=8)
    now = datetime.now(UTC)
    _run_at(client, paypal, writer, now)
    rid = client.get("/api/reminders").json()[0]["id"]
    client.post(f"/api/reminders/{rid}/send", json={})

    assert _run_at(client, paypal, writer, now + timedelta(hours=71))["due"] == 0
    later = _run_at(client, paypal, writer, now + timedelta(hours=73), days_shift=3)
    assert later["results"][0]["tone"] == "firm"


def test_the_owners_own_reminder_counts_toward_the_gap(
    client: TestClient, paypal: FakePayPal, writer: ScriptedWriter
) -> None:
    inv = _overdue_invoice(client, days=3)
    client.post(f"/api/invoices/{inv}/remind", json={})
    assert client.post("/api/chase/run").json()["due"] == 0


def test_never_chases_money_already_paid(
    client: TestClient, paypal: FakePayPal, writer: ScriptedWriter
) -> None:
    _overdue_invoice(client, days=5)
    paypal.remote["INV2-1"] = _paid("PAID")
    assert client.post("/api/chase/run").json()["due"] == 0
    assert writer.facts == []


def test_paid_after_drafting_means_it_is_not_sent(
    client: TestClient, paypal: FakePayPal, writer: ScriptedWriter
) -> None:
    _overdue_invoice(client, days=2)
    client.post("/api/chase/run")
    rid = client.get("/api/reminders").json()[0]["id"]
    paypal.remote["INV2-1"] = _paid("MARKED_AS_PAID")
    resp = client.post(f"/api/reminders/{rid}/send", json={})
    assert resp.status_code == 409 and "paid or cancelled since" in resp.json()["detail"]
    assert paypal.count("remind") == 0
    assert _reminders(client)[0].status == "skipped"


def test_automatic_mode_sends_straight_away(
    client: TestClient, paypal: FakePayPal, writer: ScriptedWriter
) -> None:
    assert client.put("/api/chase/settings", json={"auto_chase": True}).json() == {
        "auto_chase": True
    }
    _overdue_invoice(client, days=2)
    summary = client.post("/api/chase/run").json()
    assert (summary["drafted"], summary["sent"], summary["auto"]) == (0, 1, True)
    assert paypal.count("remind") == 1
    assert client.get("/api/reminders").json() == []


def test_agent_failure_falls_back_to_the_template(
    client: TestClient, paypal: FakePayPal, writer: ScriptedWriter
) -> None:
    _overdue_invoice(client, days=2)
    writer.next = RuntimeError("MCP server crashed")
    client.post("/api/chase/run")
    r = _reminders(client)[0]
    assert r.source == "template" and "friendly reminder" in r.note
    assert "RuntimeError" in r.trace["reason"]


def test_made_up_amounts_fall_back_to_the_template(
    client: TestClient, paypal: FakePayPal, writer: ScriptedWriter
) -> None:
    _overdue_invoice(client, days=2)
    writer.next = ReminderDraft(subject="x", note="Pay £999 now or else.", source="agent")
    client.post("/api/chase/run")
    r = _reminders(client)[0]
    assert r.source == "template" and "£999" not in r.note


def test_agent_reports_paid_so_nothing_is_drafted(
    client: TestClient, paypal: FakePayPal, writer: ScriptedWriter
) -> None:
    _overdue_invoice(client, days=2)
    writer.next = ReminderDraft(subject="", note="", source="agent", skip_reason="PayPal says paid")
    summary = client.post("/api/chase/run").json()
    assert summary["skipped"] == 1 and _reminders(client) == []


def test_skip_a_draft(client: TestClient, paypal: FakePayPal, writer: ScriptedWriter) -> None:
    _overdue_invoice(client, days=2)
    client.post("/api/chase/run")
    rid = client.get("/api/reminders").json()[0]["id"]
    assert client.post(f"/api/reminders/{rid}/skip").json() == {"status": "skipped"}
    assert client.get("/api/reminders").json() == []
    # Skipped means "not now": the next run leaves it alone for 72 hours...
    assert client.post("/api/chase/run").json()["due"] == 0
    now = datetime.now(UTC)
    assert (
        _run_at(client, paypal, writer, now + timedelta(hours=73))["due"] == 1
    )  # ...then asks again
