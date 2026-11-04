from __future__ import annotations

import json
from collections.abc import Iterator, Mapping
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.paypal import PayPalError, get_paypal
from tests.test_invoices_api import FakePayPal, _payload


class WebhookPayPal(FakePayPal):
    def __init__(self, verified: bool = True) -> None:
        super().__init__()
        self.verified = verified
        self.seen_body = b""

    async def verify_webhook(self, _headers: Mapping[str, str], raw: bytes, _wid: str) -> bool:
        self.seen_body = raw
        return self.verified


@pytest.fixture
def fake() -> Iterator[WebhookPayPal]:
    fake = WebhookPayPal()
    app.dependency_overrides[get_paypal] = lambda: fake
    yield fake
    app.dependency_overrides.pop(get_paypal, None)


def _event(paypal_id: str, event_type: str = "INVOICING.INVOICE.PAID") -> dict[str, Any]:
    return {
        "id": "WH-1",
        "event_type": event_type,
        "resource": {"invoice": {"id": paypal_id, "status": "PAID"}},
    }


def _paid(paypal_id: str) -> dict[str, Any]:
    return {
        "id": paypal_id,
        "status": "PAID",
        "payments": {"paid_amount": {"currency_code": "GBP", "value": "145.00"}},
    }


def test_paid_webhook_marks_the_invoice_paid(client: TestClient, fake: WebhookPayPal) -> None:
    inv = client.post("/api/invoices", json=_payload()).json()
    fake.remote["INV2-1"] = _paid("INV2-1")
    raw = json.dumps(_event("INV2-1")).encode()
    resp = client.post("/api/webhooks/paypal", content=raw)
    assert resp.status_code == 200
    assert resp.json()["outcome"] == "updated"
    assert fake.seen_body == raw  # verified byte-for-byte
    got = client.get(f"/api/invoices/{inv['id']}").json()
    assert got["status"] == "paid"
    assert got["amount_paid"] == "145.00"
    assert got["paid_at"]


def test_the_webhook_body_is_not_trusted(client: TestClient, fake: WebhookPayPal) -> None:
    # The event claims PAID, but PayPal itself still says SENT: nothing changes.
    inv = client.post("/api/invoices", json=_payload()).json()
    resp = client.post("/api/webhooks/paypal", content=json.dumps(_event("INV2-1")))
    assert resp.json()["outcome"] == "unchanged"
    assert client.get(f"/api/invoices/{inv['id']}").json()["status"] == "sent"


def test_replayed_webhook_is_harmless(client: TestClient, fake: WebhookPayPal) -> None:
    client.post("/api/invoices", json=_payload())
    fake.remote["INV2-1"] = _paid("INV2-1")
    body = json.dumps(_event("INV2-1"))
    assert client.post("/api/webhooks/paypal", content=body).json()["outcome"] == "updated"
    assert client.post("/api/webhooks/paypal", content=body).json()["outcome"] == "unchanged"


def test_unknown_invoice_and_other_events_are_acknowledged(
    client: TestClient, fake: WebhookPayPal
) -> None:
    resp = client.post("/api/webhooks/paypal", content=json.dumps(_event("INV2-NOT-OURS")))
    assert resp.status_code == 200
    assert resp.json()["outcome"] == "unknown_invoice"
    other = {"id": "WH-2", "event_type": "PAYMENT.CAPTURE.COMPLETED", "resource": {}}
    assert client.post("/api/webhooks/paypal", content=json.dumps(other)).json()["outcome"] == (
        "ignored"
    )


def test_paypal_down_while_handling_asks_for_redelivery(
    client: TestClient, fake: WebhookPayPal
) -> None:
    client.post("/api/invoices", json=_payload())
    fake.fail["get"] = [PayPalError(503, {"name": "SERVICE_UNAVAILABLE"})]
    resp = client.post("/api/webhooks/paypal", content=json.dumps(_event("INV2-1")))
    assert resp.status_code == 502


def test_bad_signature_is_rejected(client: TestClient, fake: WebhookPayPal) -> None:
    fake.verified = False
    resp = client.post("/api/webhooks/paypal", content=json.dumps(_event("INV2-1")))
    assert resp.status_code == 400


def test_unconfigured_paypal_is_503_not_500(client: TestClient) -> None:
    from app import paypal

    def not_configured() -> None:
        raise paypal.PayPalNotConfigured("no credentials")

    app.dependency_overrides[get_paypal] = not_configured
    try:
        resp = client.post("/api/webhooks/paypal", content=b"{}")
    finally:
        app.dependency_overrides.pop(get_paypal, None)
    assert resp.status_code == 503
