"""The demo clock: days pass for the app, never for PayPal."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.paypal import get_paypal
from app.routers.chase import get_writer_factory
from tests.test_chase import ScriptedWriter
from tests.test_invoices_api import FakePayPal, _payload
from tests.test_quotes_api import _quote, _token

REAL_TODAY = datetime.now(UTC).date()


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


def _move(client: TestClient, days: int) -> dict[str, object]:
    resp = client.put("/api/clock", json={"offset_days": days})
    assert resp.status_code == 200, resp.text
    return resp.json()


def test_the_clock_starts_at_today(client: TestClient) -> None:
    c = client.get("/api/clock").json()
    assert c["enabled"] is True and c["offset_days"] == 0 and c["today"] == REAL_TODAY.isoformat()


def test_moving_the_clock_makes_an_invoice_overdue(client: TestClient, paypal: FakePayPal) -> None:
    client.post("/api/invoices", json=_payload())  # due in 7 days
    assert client.get("/api/home").json()["totals"]["overdue_count"] == 0
    c = _move(client, 10)
    assert c["today"] == (REAL_TODAY + timedelta(days=10)).isoformat()
    row = client.get("/api/home").json()["tabs"]["overdue"][0]
    assert "3 days overdue" in row["subtitle"]


def test_friendly_then_firm_then_final_as_days_pass(
    client: TestClient, paypal: FakePayPal, writer: ScriptedWriter
) -> None:
    client.post("/api/invoices", json=_payload())  # due on day 7
    tones = []
    for day in (8, 15, 22):  # 1, 8 and 15 days late, a week apart each time
        _move(client, day)
        summary = client.post("/api/chase/run").json()
        tones.append(summary["results"][0]["tone"])
        rid = client.get("/api/reminders").json()[0]["id"]
        assert client.post(f"/api/reminders/{rid}/send", json={}).status_code == 200
    assert tones == ["friendly", "firm", "final"]
    _move(client, 23)
    assert client.post("/api/chase/run").json()["due"] == 0  # 72 hours apply on the clock too


def test_paypal_always_gets_real_dates(client: TestClient, paypal: FakePayPal) -> None:
    _move(client, 10)
    inv = client.post("/api/invoices", json=_payload()).json()
    body = paypal.last("create")["body"]
    assert body["detail"]["invoice_date"] == REAL_TODAY.isoformat()  # not the demo date
    assert (
        body["detail"]["payment_term"]["due_date"] == (REAL_TODAY + timedelta(days=17)).isoformat()
    )
    client.post(f"/api/invoices/{inv['id']}/record-payment", json={"method": "CASH"})
    assert paypal.last("record")["method"] == "CASH"


def test_quotes_expire_on_the_clock(client: TestClient) -> None:
    q = client.post("/api/quotes", json=_quote()).json()  # valid for 30 days
    _move(client, 31)
    assert client.get(f"/q/{_token(q)}/status").json()["status"] == "expired"


def test_bounds_and_switch(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    assert client.put("/api/clock", json={"offset_days": 121}).status_code == 422
    assert client.put("/api/clock", json={"offset_days": -1}).status_code == 422
    from app.settings import get_settings

    monkeypatch.setattr(get_settings(), "demo_clock_enabled", False)
    assert client.put("/api/clock", json={"offset_days": 3}).status_code == 403
