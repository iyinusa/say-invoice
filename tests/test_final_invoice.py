"""Finishing a booked job: the work as done, less the agreed discount and the deposit received."""

from __future__ import annotations

from collections.abc import Iterator
from decimal import Decimal
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.db import get_sessionmaker
from app.main import app
from app.models import Invoice, Quote
from app.paypal import get_paypal
from tests.test_invoices_api import FakePayPal
from tests.test_quotes_api import _quote, _token

AS_QUOTED = [
    {"name": "Replace kitchen tap", "quantity": "1", "unit_price": "90", "unit": "fixed"},
    {"name": "Parts", "quantity": "1", "unit_price": "40", "is_material": True},
]


@pytest.fixture
def paypal() -> Iterator[FakePayPal]:
    fake = FakePayPal()
    app.dependency_overrides[get_paypal] = lambda: fake
    yield fake
    app.dependency_overrides.pop(get_paypal, None)


def _paid(amount: str) -> dict[str, Any]:
    return {"id": "INV2-1", "status": "PAID",
            "payments": {"paid_amount": {"currency_code": "GBP", "value": amount}}}  # fmt: skip


def _booked(client: TestClient, paypal: FakePayPal, *, deposit_paid: bool = True) -> dict[str, Any]:
    q = client.post("/api/quotes", json=_quote()).json()
    client.post(f"/q/{_token(q)}/accept", json={"email": "patel@example.com"})
    if deposit_paid:
        paypal.remote["INV2-1"] = _paid("39.00")
        client.get(f"/q/{_token(q)}/status")  # the page's poll picks the payment up
    return q


def _finish(
    client: TestClient, q: dict[str, Any], items: list[dict[str, Any]] | None = None
) -> Any:
    return client.post(
        f"/api/quotes/{q['id']}/final-invoice",
        json={"client_email": "patel@example.com", "items": items or AS_QUOTED},
    )


def _db(client: TestClient, fn: Any) -> Any:
    async def run() -> Any:
        async with get_sessionmaker()() as s:
            return await fn(s)

    return client.portal.call(run)  # type: ignore[union-attr]


def test_booked_job_is_listed_to_finish(client: TestClient, paypal: FakePayPal) -> None:
    q = _booked(client, paypal)
    jobs = client.get("/api/jobs/to-finish").json()
    assert len(jobs) == 1
    job = jobs[0]
    assert (job["number"], job["client_email"], job["total"]) == (
        "Q-0001",
        "patel@example.com",
        "130.00",
    )
    assert job["deposit"] == {"status": "paid", "amount": "39.00", "paid": "39.00"}
    assert [ln["name"] for ln in job["lines"]] == ["Replace kitchen tap", "Parts"]
    assert job["quote_id"] == q["id"]


def test_final_invoice_takes_off_the_deposit_received(
    client: TestClient, paypal: FakePayPal
) -> None:
    q = _booked(client, paypal)
    resp = _finish(client, q)
    assert resp.status_code == 201, resp.text
    inv = resp.json()
    assert (inv["total"], inv["discount"], inv["status"]) == ("91.00", "39.00", "sent")
    body = paypal.last("create")["body"]
    assert body["amount"]["breakdown"]["discount"]["invoice_discount"]["amount"]["value"] == "39.00"
    assert "Final invoice for quote Q-0001." in body["detail"]["note"]
    assert "Deposit received (−£39.00) deducted." in body["detail"]["note"]
    assert client.get("/api/jobs/to-finish").json() == []  # finished


def test_changes_at_the_end_of_the_job(client: TestClient, paypal: FakePayPal) -> None:
    q = _booked(client, paypal)
    done = [
        AS_QUOTED[0],
        {"name": "Cartridge", "quantity": "1", "unit_price": "45", "is_material": True},
    ]
    inv = _finish(client, q, done).json()
    assert inv["total"] == "96.00"  # £135 of work less the £39 deposit
    assert [i["name"] for i in inv["items"]] == ["Replace kitchen tap", "Cartridge"]


def test_the_agreed_labour_discount_carries_through(client: TestClient, paypal: FakePayPal) -> None:
    q = _booked(client, paypal)

    async def negotiated(session: Any) -> None:
        row = await session.get(Quote, q["id"])
        row.labour_discount_pct = Decimal(10)
        await session.commit()

    _db(client, negotiated)
    inv = _finish(client, q).json()
    assert (inv["discount"], inv["total"]) == ("48.00", "82.00")  # £9 off labour + £39 deposit
    note = paypal.last("create")["body"]["detail"]["note"]
    assert "agreed 10% off labour (−£9.00)" in note


def test_an_unpaid_deposit_is_cancelled_not_charged_twice(
    client: TestClient, paypal: FakePayPal
) -> None:
    q = _booked(client, paypal, deposit_paid=False)
    inv = _finish(client, q).json()
    assert inv["total"] == "130.00"  # nothing was paid, so nothing is deducted
    assert ("cancel", "INV2-1") in paypal.calls

    async def deposit_status(session: Any) -> str:
        return str(await session.scalar(select(Invoice.status).where(Invoice.kind == "deposit")))

    assert _db(client, deposit_status) == "cancelled"


def test_a_retried_tap_returns_the_same_invoice(client: TestClient, paypal: FakePayPal) -> None:
    q = _booked(client, paypal)
    first = _finish(client, q).json()
    again = _finish(client, q)
    assert again.status_code == 201
    assert again.json()["id"] == first["id"]  # the same invoice, not an error
    assert paypal.count("create") == 2  # deposit + final, never a second final


def test_deposit_covering_everything_is_refused(client: TestClient, paypal: FakePayPal) -> None:
    q = _booked(client, paypal)
    small = [{"name": "Call-out", "quantity": "1", "unit_price": "30", "unit": "fixed"}]
    resp = _finish(client, q, small)
    assert resp.status_code == 422
    assert "deposit already covers" in resp.json()["detail"]


def test_only_an_accepted_quote_can_be_finished(client: TestClient, paypal: FakePayPal) -> None:
    q = client.post("/api/quotes", json=_quote()).json()
    assert _finish(client, q).status_code == 409
