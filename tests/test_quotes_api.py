from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.db import get_sessionmaker
from app.main import app
from app.models import Invoice, Quote, Workspace
from app.paypal import PayPalError, get_paypal
from app.services.live import broker
from tests.test_invoices_api import FakePayPal

TODAY = datetime.now(UTC).date()


@pytest.fixture
def paypal() -> Iterator[FakePayPal]:
    fake = FakePayPal()
    app.dependency_overrides[get_paypal] = lambda: fake
    yield fake
    app.dependency_overrides.pop(get_paypal, None)


def _quote(**overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "idempotency_key": "quote-tap-0001",
        "client_name": "Mrs Patel",
        "currency": "GBP",
        "items": [
            {"name": "Replace kitchen tap", "quantity": "1", "unit_price": "90", "unit": "fixed"},
            {"name": "Parts", "quantity": "1", "unit_price": "40", "is_material": True},
        ],
        "deposit_pct": "30",
    }
    body.update(overrides)
    return body


def _db(client: TestClient, fn: Any) -> Any:
    async def run() -> Any:
        async with get_sessionmaker()() as session:
            return await fn(session)

    return client.portal.call(run)  # type: ignore[union-attr]


def _token(q: dict[str, Any]) -> str:
    return str(q["public_url"].rsplit("/", 1)[-1])


# ── owner ─────────────────────────────────────────────────────────────────────


def test_create_quote_for_a_name_only(client: TestClient) -> None:
    resp = client.post("/api/quotes", json=_quote())
    assert resp.status_code == 201, resp.text
    q = resp.json()
    assert q["number"] == "Q-0001"
    assert q["status"] == "sent"
    assert q["total"] == "130.00"
    assert q["deposit_amount"] == "39.00"
    assert q["client_email"] == ""
    assert q["valid_until"] == (TODAY + timedelta(days=30)).isoformat()
    assert "/q/" in q["public_url"] and len(_token(q)) >= 20


def test_deposit_defaults_to_the_policy_minimum(client: TestClient) -> None:
    q = client.post("/api/quotes", json=_quote(deposit_pct=None)).json()
    assert q["deposit_pct"] == "25.00"
    assert q["deposit_amount"] == "32.50"


def test_double_tap_makes_one_quote(client: TestClient) -> None:
    a = client.post("/api/quotes", json=_quote()).json()
    b = client.post("/api/quotes", json=_quote()).json()
    assert a["id"] == b["id"]
    assert (
        client.post("/api/quotes", json=_quote(idempotency_key="quote-tap-0002")).json()["number"]
        == "Q-0002"
    )


@pytest.mark.parametrize(
    ("overrides", "fragment"),
    [
        ({"client_name": "", "client_email": ""}, "name or an email"),
        ({"currency": "NGN"}, "NGN"),
        ({"deposit_pct": "120"}, "less than or equal"),
        ({"job_date": "2020-01-01"}, "past"),
        ({"client_email": "patel at home"}, "email"),
    ],
)
def test_bad_quotes_are_refused(
    client: TestClient, overrides: dict[str, Any], fragment: str
) -> None:
    resp = client.post("/api/quotes", json=_quote(**overrides))
    assert resp.status_code == 422
    assert fragment in resp.text


# ── client page ───────────────────────────────────────────────────────────────


def test_client_page_shows_the_quote(client: TestClient) -> None:
    q = client.post(
        "/api/quotes", json=_quote(job_date=(TODAY + timedelta(days=3)).isoformat())
    ).json()
    resp = client.get(f"/q/{_token(q)}")
    assert resp.status_code == 200
    html = resp.text
    expected = [
        "Dave&#39;s Plumbing", "Q-0001", "Mrs Patel", "Replace kitchen tap", "£130.00",
        "Deposit to book (30%)", "£39.00", "£91.00", "Accept &amp; pay £39.00 deposit",
    ]  # fmt: skip
    for text in expected:
        assert text in html, text
    assert resp.headers["cache-control"] == "no-store"
    assert "noindex" in html
    assert "doc-trade" not in html  # no trade set: no empty line under the name


def test_client_page_shows_the_trade(client: TestClient) -> None:
    ws = client.get("/api/workspace").json()
    client.put("/api/workspace", json={**{k: ws[k] for k in ("business_name", "owner_name",
               "currency", "default_due_days")}, "trade": "Plumber & Gas Engineer"})  # fmt: skip
    q = client.post("/api/quotes", json=_quote()).json()
    assert (
        '<p class="doc-trade">Plumber &amp; Gas Engineer</p>' in client.get(f"/q/{_token(q)}").text
    )


def test_unknown_link_is_a_friendly_404(client: TestClient) -> None:
    resp = client.get("/q/not-a-real-token")
    assert resp.status_code == 404
    assert "find that page" in resp.text


def test_accept_needs_an_email_when_the_quote_has_none(
    client: TestClient, paypal: FakePayPal
) -> None:
    q = client.post("/api/quotes", json=_quote()).json()
    resp = client.post(f"/q/{_token(q)}/accept", json={})
    assert resp.status_code == 422
    assert paypal.calls == []


def test_accept_books_the_quote_and_sends_the_deposit_invoice(
    client: TestClient, paypal: FakePayPal
) -> None:
    q = client.post("/api/quotes", json=_quote()).json()

    async def ws_id(session: Any) -> str:
        return str(await session.scalar(select(Workspace.id)))

    with broker.subscribe(_db(client, ws_id)) as queue:
        resp = client.post(f"/q/{_token(q)}/accept", json={"email": "Patel@Example.com"})
        events = [queue.get_nowait() for _ in range(queue.qsize())]
    assert resp.status_code == 200, resp.text
    state = resp.json()
    assert state["status"] == "accepted"
    assert state["deposit"]["status"] == "sent"
    assert state["deposit"]["pay_url"].endswith("#INV2-1")
    assert {"quote"} <= {e["type"] for e in events}

    body = paypal.last("create")["body"]
    assert body["items"][0]["name"] == "Deposit (30%) for quote Q-0001"
    assert body["items"][0]["unit_amount"]["value"] == "39.00"
    assert body["primary_recipients"][0]["billing_info"]["email_address"] == "patel@example.com"
    assert body["detail"]["payment_term"]["due_date"] == TODAY.isoformat()

    async def stored(session: Any) -> tuple[Any, ...]:
        quote = await session.scalar(select(Quote))
        inv = await session.scalar(select(Invoice))
        return quote.agreed_terms, quote.client_email, inv.kind, inv.quote_id == quote.id

    terms, email, kind, linked = _db(client, stored)
    assert terms["total"] == "130.00" and terms["deposit_amount"] == "39.00"
    assert (email, kind, linked) == ("patel@example.com", "deposit", True)

    # A second tap doesn't take the deposit twice.
    again = client.post(f"/q/{_token(q)}/accept", json={})
    assert again.status_code == 200
    assert paypal.count("create") == 1


def test_paypal_outage_keeps_the_acceptance_and_retry_sends_the_deposit(
    client: TestClient, paypal: FakePayPal
) -> None:
    q = client.post("/api/quotes", json=_quote(client_email="patel@example.com")).json()
    paypal.fail["create"] = [PayPalError(503, {"name": "SERVICE_UNAVAILABLE"})]
    first = client.post(f"/q/{_token(q)}/accept", json={})  # email already on the quote
    assert first.status_code == 502
    status = client.get(f"/q/{_token(q)}/status").json()
    assert status["status"] == "accepted"
    assert status["deposit_required"] is True
    retry = client.post(f"/q/{_token(q)}/accept", json={})
    assert retry.status_code == 200
    assert retry.json()["deposit"]["status"] == "sent"


def test_no_deposit_means_booked_on_acceptance(client: TestClient, paypal: FakePayPal) -> None:
    q = client.post("/api/quotes", json=_quote(deposit_pct="0")).json()
    state = client.post(f"/q/{_token(q)}/accept", json={"email": "p@example.com"}).json()
    assert state["status"] == "accepted"
    assert state["deposit_required"] is False
    assert state["deposit"] is None
    assert paypal.calls == []


def test_expired_quote_cannot_be_accepted(client: TestClient, paypal: FakePayPal) -> None:
    q = client.post("/api/quotes", json=_quote()).json()

    async def expire(session: Any) -> None:
        row = await session.scalar(select(Quote))
        row.valid_until = date(2020, 1, 1)
        await session.commit()

    _db(client, expire)
    assert client.get(f"/q/{_token(q)}/status").json()["status"] == "expired"
    assert client.post(f"/q/{_token(q)}/accept", json={"email": "p@example.com"}).status_code == 409


def test_status_shows_the_deposit_paid(client: TestClient, paypal: FakePayPal) -> None:
    q = client.post("/api/quotes", json=_quote(client_email="patel@example.com")).json()
    client.post(f"/q/{_token(q)}/accept", json={})
    paypal.remote["INV2-1"] = {
        "id": "INV2-1",
        "status": "PAID",
        "payments": {"paid_amount": {"currency_code": "GBP", "value": "39.00"}},
    }

    async def ws_id(session: Any) -> str:
        return str(await session.scalar(select(Workspace.id)))

    with broker.subscribe(_db(client, ws_id)) as queue:
        state = client.get(f"/q/{_token(q)}/status").json()
        events = [queue.get_nowait() for _ in range(queue.qsize())]
    # The owner's phone hears about the quote, not just the deposit invoice.
    assert {"type": "quote", "id": q["id"], "number": "Q-0001", "status": "accepted",
            "deposit_status": "paid"} in events  # fmt: skip
    assert state["deposit"]["status"] == "paid"
    assert state["deposit"]["paid"] == "£39.00"
    owner = client.get(f"/api/quotes/{q['id']}").json()
    assert owner["deposit"]["status"] == "paid"


def test_deposit_qr(client: TestClient, paypal: FakePayPal) -> None:
    q = client.post("/api/quotes", json=_quote(client_email="patel@example.com")).json()
    assert client.get(f"/q/{_token(q)}/deposit-qr.png").status_code == 409
    client.post(f"/q/{_token(q)}/accept", json={})
    resp = client.get(f"/q/{_token(q)}/deposit-qr.png")
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "image/png"
