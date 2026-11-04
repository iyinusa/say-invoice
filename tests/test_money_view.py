"""The "Who owes me" home, job pages, and the owner's actions on an invoice."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.db import get_sessionmaker
from app.main import app
from app.models import Invoice
from app.paypal import PayPalError, get_paypal
from tests.test_invoices_api import FakePayPal, _payload
from tests.test_quotes_api import _quote, _token

TODAY = datetime.now(UTC).date()


@pytest.fixture
def paypal() -> Iterator[FakePayPal]:
    fake = FakePayPal()
    app.dependency_overrides[get_paypal] = lambda: fake
    yield fake
    app.dependency_overrides.pop(get_paypal, None)


def _send(client: TestClient, key: str, email: str = "kennedy@example.com") -> dict[str, Any]:
    resp = client.post("/api/invoices", json=_payload(idempotency_key=key, client_email=email))
    assert resp.status_code == 201, resp.text
    return resp.json()


def _set(client: TestClient, invoice_id: str, **fields: Any) -> None:
    async def run() -> None:
        async with get_sessionmaker()() as s:
            inv = await s.get(Invoice, invoice_id)
            for k, v in fields.items():
                setattr(inv, k, v)
            await s.commit()

    client.portal.call(run)  # type: ignore[union-attr]


def test_empty_home(client: TestClient) -> None:
    home = client.get("/api/home").json()
    assert home["totals"]["owed"] == "£0.00"
    assert home["totals"]["overdue_count"] == 0
    assert all(rows == [] for rows in home["tabs"].values())


def test_who_owes_me(client: TestClient, paypal: FakePayPal) -> None:
    a = _send(client, "inv-a-000001")
    b = _send(client, "inv-b-000001", "b@example.com")
    _set(client, b["id"], due_date=TODAY - timedelta(days=9))
    q = client.post("/api/quotes", json=_quote()).json()

    home = client.get("/api/home").json()
    t = home["totals"]
    assert (t["owed"], t["owed_count"], t["overdue"], t["overdue_count"]) == (
        "£290.00",
        2,
        "£145.00",
        1,
    )
    assert t["quotes_out"] == "£130.00" and t["quotes_count"] == 1
    assert [r["id"] for r in home["tabs"]["owed"]] == [a["id"]]
    late = home["tabs"]["overdue"][0]
    assert (
        late["id"] == b["id"]
        and "9 days overdue" in late["subtitle"]
        and late["amount"] == "£145.00"
    )
    assert home["tabs"]["quotes"][0]["id"] == q["id"]


def test_paid_moves_to_paid_and_counts_this_month(client: TestClient, paypal: FakePayPal) -> None:
    inv = _send(client, "inv-a-000001")
    resp = client.post(
        f"/api/invoices/{inv['id']}/record-payment", json={"amount": "45", "method": "CASH"}
    )
    assert resp.json()["status"] == "partially_paid"
    home = client.get("/api/home").json()
    assert home["totals"]["owed"] == "£100.00"  # what's still owed, not the face value
    assert "£45.00 paid" in home["tabs"]["owed"][0]["subtitle"]
    client.post(f"/api/invoices/{inv['id']}/record-payment", json={"method": "BANK_TRANSFER"})
    home = client.get("/api/home").json()
    assert home["tabs"]["paid"][0]["id"] == inv["id"]
    assert home["totals"]["paid_month"] == "£145.00"
    assert home["totals"]["owed"] == "£0.00"


def test_booked_quote_shows_in_quotes_tab(client: TestClient, paypal: FakePayPal) -> None:
    q = client.post("/api/quotes", json=_quote()).json()
    client.post(f"/q/{_token(q)}/accept", json={"email": "p@example.com"})
    row = next(r for r in client.get("/api/home").json()["tabs"]["quotes"] if r["id"] == q["id"])
    assert row["state"] == "booked"
    assert "finish the job" in row["subtitle"]


def test_invoice_job_page(client: TestClient, paypal: FakePayPal) -> None:
    inv = _send(client, "inv-a-000001")
    page = client.get(f"/api/jobs/invoice/{inv['id']}").json()
    assert page["state"] == "owed" and page["balance_display"] == "£145.00"
    assert page["actions"] == {"remind": True, "record_payment": True, "cancel": True,
                               "retry": False, "qr": True}  # fmt: skip
    assert [e["label"] for e in page["timeline"]] == ["Invoice created", "Sent by PayPal (£145.00)"]


def test_remind_now_at_most_once_a_day(client: TestClient, paypal: FakePayPal) -> None:
    inv = _send(client, "inv-a-000001")
    assert client.post(f"/api/invoices/{inv['id']}/remind", json={}).status_code == 200
    call = paypal.last("remind")
    assert "invoice 0001" in call["subject"] and "£145.00" in call["note"]
    again = client.post(f"/api/invoices/{inv['id']}/remind", json={})
    assert again.status_code == 409
    assert "less than an hour ago" in again.json()["detail"]
    labels = [e["label"] for e in client.get(f"/api/jobs/invoice/{inv['id']}").json()["timeline"]]
    assert "Reminder sent by you" in labels


def test_no_reminders_once_paid(client: TestClient, paypal: FakePayPal) -> None:
    inv = _send(client, "inv-a-000001")
    client.post(f"/api/invoices/{inv['id']}/record-payment", json={"method": "CASH"})
    assert client.post(f"/api/invoices/{inv['id']}/remind", json={}).status_code == 409


def test_record_payment_bounds(client: TestClient, paypal: FakePayPal) -> None:
    inv = _send(client, "inv-a-000001")
    over = client.post(f"/api/invoices/{inv['id']}/record-payment", json={"amount": "500"})
    assert over.status_code == 409 and "£145.00" in over.json()["detail"]
    bad = client.post(f"/api/invoices/{inv['id']}/record-payment", json={"method": "BITCOIN"})
    assert bad.status_code == 409


def test_cancel_unpaid_but_not_part_paid(client: TestClient, paypal: FakePayPal) -> None:
    a = _send(client, "inv-a-000001")
    assert client.post(f"/api/invoices/{a['id']}/cancel", json={}).json()["status"] == "cancelled"
    b = _send(client, "inv-b-000001", "b@example.com")
    client.post(f"/api/invoices/{b['id']}/record-payment", json={"amount": "10"})
    resp = client.post(f"/api/invoices/{b['id']}/cancel", json={})
    assert resp.status_code == 409 and "can't be cancelled" in resp.json()["detail"]


def test_retry_a_failed_send(client: TestClient, paypal: FakePayPal) -> None:
    paypal.fail["send"] = [PayPalError(503, {"name": "SERVICE_UNAVAILABLE"})]
    client.post("/api/invoices", json=_payload(idempotency_key="inv-a-000001"))

    async def draft_id() -> str:
        async with get_sessionmaker()() as s:
            return str(await s.scalar(select(Invoice.id)))

    inv_id = client.portal.call(draft_id)  # type: ignore[union-attr]
    row = client.get("/api/home").json()["tabs"]["owed"][0]
    assert row["state"] == "draft" and "Not sent" in row["subtitle"]
    assert client.get(f"/api/jobs/invoice/{inv_id}").json()["actions"]["retry"] is True
    assert client.post(f"/api/invoices/{inv_id}/retry").json()["status"] == "sent"


def test_quote_job_page_tells_the_whole_story(client: TestClient, paypal: FakePayPal) -> None:
    q = client.post("/api/quotes", json=_quote()).json()
    client.post(f"/q/{_token(q)}/accept", json={"email": "p@example.com"})
    page = client.get(f"/api/jobs/quote/{q['id']}").json()
    labels = [e["label"] for e in page["timeline"]]
    assert labels[0] == "Quote created"
    assert "Client accepted the quote (£130.00)" in labels
    assert "Deposit created" in labels
    assert page["actions"]["finish"] is True
    assert page["invoices"][0]["subtitle"].startswith("Deposit for Q-0001")
    assert date.fromisoformat(page["timeline"][0]["at"][:10]) == TODAY
