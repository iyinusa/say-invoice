from __future__ import annotations

import json
from collections.abc import Iterator, Mapping

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.paypal import get_paypal


class FakePayPal:
    def __init__(self, verified: bool) -> None:
        self.verified = verified
        self.seen_body = b""

    async def verify_webhook(self, _headers: Mapping[str, str], raw: bytes, _wid: str) -> bool:
        self.seen_body = raw
        return self.verified


@pytest.fixture
def fake() -> Iterator[FakePayPal]:
    fake = FakePayPal(verified=True)
    app.dependency_overrides[get_paypal] = lambda: fake
    yield fake
    app.dependency_overrides.clear()


EVENT = {
    "id": "WH-1",
    "event_type": "INVOICING.INVOICE.PAID",
    "resource": {"invoice": {"id": "INV2-1", "status": "PAID"}},
}


def test_verified_event_is_accepted(fake: FakePayPal) -> None:
    raw = json.dumps(EVENT).encode()
    with TestClient(app) as client:
        resp = client.post("/api/webhooks/paypal", content=raw)
    assert resp.status_code == 200
    assert fake.seen_body == raw


def test_bad_signature_is_rejected(fake: FakePayPal) -> None:
    fake.verified = False
    with TestClient(app) as client:
        resp = client.post("/api/webhooks/paypal", content=json.dumps(EVENT))
    assert resp.status_code == 400


def test_unconfigured_paypal_is_503_not_500() -> None:
    from app import paypal

    def not_configured() -> None:
        raise paypal.PayPalNotConfigured("no credentials")

    app.dependency_overrides[get_paypal] = not_configured
    try:
        with TestClient(app) as client:
            resp = client.post("/api/webhooks/paypal", content=b"{}")
    finally:
        app.dependency_overrides.clear()
    assert resp.status_code == 503
