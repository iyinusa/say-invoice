from __future__ import annotations

from collections.abc import Iterator, Mapping
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.paypal import PayPalError, get_paypal

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64


class FakePayPal:
    """Records every call; `fail[name]` holds errors to raise on the next calls."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, Any]] = []
        self.fail: dict[str, list[Exception]] = {}
        self.numbers = 0
        self.created: dict[str, str] = {}  # request id -> invoice id, like PayPal's idempotency
        self.remote: dict[str, dict[str, Any]] = {}  # what get_invoice returns, by PayPal id
        self.totals: dict[str, Decimal] = {}
        self.paid: dict[str, Decimal] = {}

    def _maybe_fail(self, name: str) -> None:
        if self.fail.get(name):
            raise self.fail[name].pop(0)

    def count(self, name: str) -> int:
        return sum(1 for c in self.calls if c[0] == name)

    def last(self, name: str) -> Any:
        return [c[1] for c in self.calls if c[0] == name][-1]

    async def next_invoice_number(self) -> str:
        self.calls.append(("number", None))
        self._maybe_fail("number")
        self.numbers += 1
        return f"{self.numbers:04d}"

    async def create_draft_invoice(
        self, body: Mapping[str, Any], *, request_id: str | None = None
    ) -> dict[str, Any]:
        self.calls.append(("create", {"body": body, "request_id": request_id}))
        self._maybe_fail("create")
        inv_id = self.created.setdefault(str(request_id), f"INV2-{len(self.created) + 1}")
        total = sum(
            Decimal(i["quantity"]) * Decimal(i["unit_amount"]["value"]) for i in body["items"]
        )
        disc = (body.get("amount") or {}).get("breakdown", {}).get("discount", {})
        if disc:
            total -= Decimal(disc["invoice_discount"]["amount"]["value"])
        self.totals[inv_id] = total
        return {"id": inv_id, "status": "DRAFT"}

    async def send_invoice(self, invoice_id: str, *, request_id: str | None = None) -> str:
        self.calls.append(("send", {"invoice_id": invoice_id, "request_id": request_id}))
        self._maybe_fail("send")
        return f"https://www.sandbox.paypal.com/invoice/p/#{invoice_id}"

    async def get_invoice(self, invoice_id: str) -> dict[str, Any]:
        self.calls.append(("get", invoice_id))
        self._maybe_fail("get")
        return self.remote.get(invoice_id, {"id": invoice_id, "status": "SENT"})

    async def cancel(self, invoice_id: str, *, subject: str = "", note: str = "") -> None:
        self.calls.append(("cancel", invoice_id))
        self._maybe_fail("cancel")
        self.remote[invoice_id] = {"id": invoice_id, "status": "CANCELLED"}

    async def update_invoice(
        self, invoice_id: str, body: Mapping[str, Any], *, notify_recipient: bool = True
    ) -> dict[str, Any]:
        self.calls.append(("update", {"invoice_id": invoice_id, "body": dict(body)}))
        self._maybe_fail("update")
        return dict(body)

    async def remind(self, invoice_id: str, *, subject: str = "", note: str = "") -> None:
        self.calls.append(("remind", {"invoice_id": invoice_id, "subject": subject, "note": note}))
        self._maybe_fail("remind")

    async def record_payment(
        self,
        invoice_id: str,
        *,
        amount: Decimal,
        currency: str,
        method: str = "CASH",
        paid_on: Any = None,
        note: str = "",
    ) -> str:
        self.calls.append(
            ("record", {"invoice_id": invoice_id, "amount": amount, "method": method})
        )
        paid = self.paid.get(invoice_id, Decimal(0)) + amount
        self.paid[invoice_id] = paid
        full = paid >= self.totals.get(invoice_id, paid)
        self.remote[invoice_id] = {
            "id": invoice_id,
            "status": "MARKED_AS_PAID" if full else "PARTIALLY_PAID",
            "payments": {"paid_amount": {"currency_code": currency, "value": str(paid)}},
        }
        return "PAY-1"

    async def generate_qr_code(self, invoice_id: str, *, size: int = 500) -> bytes:
        self.calls.append(("qr", invoice_id))
        return PNG


@pytest.fixture
def paypal() -> Iterator[FakePayPal]:
    fake = FakePayPal()
    app.dependency_overrides[get_paypal] = lambda: fake
    yield fake
    app.dependency_overrides.pop(get_paypal, None)


def _payload(**overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "idempotency_key": "tap-0001-abcdef",
        "client_name": "Mr Kennedy",
        "client_email": "Kennedy@Example.com",
        "currency": "gbp",
        "items": [
            {"name": "Boiler repair", "quantity": "2.00", "unit_price": "60.00", "unit": "hours"},
            {"name": "Valve", "quantity": "1", "unit_price": "25", "is_material": True},
        ],
        "notes": "Thanks!",
        "transcript": "Fixed Mr Kennedy's boiler...",
        "draft": {"transcript": "Fixed Mr Kennedy's boiler..."},
    }
    body.update(overrides)
    return body


def _boom(status: int = 500) -> PayPalError:
    return PayPalError(status, {"name": "INTERNAL_SERVER_ERROR", "message": "try later"})


def test_send_creates_and_sends_a_paypal_invoice(client: TestClient, paypal: FakePayPal) -> None:
    resp = client.post("/api/invoices", json=_payload())
    assert resp.status_code == 201, resp.text
    inv = resp.json()
    assert inv["status"] == "sent"
    assert inv["total"] == "145.00"
    assert inv["number"] == "0001"
    assert inv["client_email"] == "kennedy@example.com"
    assert inv["payer_view_url"].endswith("#INV2-1")
    assert [i["line_total"] for i in inv["items"]] == ["120.00", "25.00"]

    body = paypal.last("create")["body"]
    assert body["detail"]["currency_code"] == "GBP"
    assert body["detail"]["note"].startswith("Thanks!\n\nCan't pay it all at once?")
    assert "/p/" in body["detail"]["note"]  # every invoice offers a payment plan
    assert body["invoicer"]["business_name"] == "Dave's Plumbing"
    assert [i["unit_of_measure"] for i in body["items"]] == ["HOURS", "QUANTITY"]
    assert [i["quantity"] for i in body["items"]] == ["2", "1"]
    # No due date said: the workspace default (7 days) applies.
    due = (datetime.now(UTC).date() + timedelta(days=7)).isoformat()
    assert body["detail"]["payment_term"]["due_date"] == due
    assert inv["due_date"] == due


def test_double_tap_sends_once(client: TestClient, paypal: FakePayPal) -> None:
    first = client.post("/api/invoices", json=_payload()).json()
    second = client.post("/api/invoices", json=_payload())
    assert second.status_code == 201
    assert second.json()["id"] == first["id"]
    assert paypal.count("create") == 1
    assert paypal.count("send") == 1


def test_paypal_outage_keeps_the_draft_and_the_retry_finishes_it(
    client: TestClient, paypal: FakePayPal
) -> None:
    paypal.fail["create"] = [_boom()]
    resp = client.post("/api/invoices", json=_payload())
    assert resp.status_code == 502
    assert resp.json()["retryable"] is True
    assert "PayPal" in resp.json()["detail"]

    resp = client.post("/api/invoices", json=_payload())
    assert resp.status_code == 201
    assert resp.json()["number"] == "0001"  # the number was kept, not burned
    assert paypal.count("number") == 1
    ids = [c[1]["request_id"] for c in paypal.calls if c[0] == "create"]
    assert ids[0] == ids[1]  # PayPal-Request-Id repeats, so PayPal can dedupe


def test_failed_send_is_retried_without_creating_again(
    client: TestClient, paypal: FakePayPal
) -> None:
    paypal.fail["send"] = [_boom()]
    assert client.post("/api/invoices", json=_payload()).status_code == 502
    resp = client.post("/api/invoices", json=_payload())
    assert resp.status_code == 201
    assert paypal.count("create") == 1
    assert paypal.count("send") == 2


def test_edit_after_a_failure_before_paypal_saw_it_replaces_the_draft(
    client: TestClient, paypal: FakePayPal
) -> None:
    paypal.fail["number"] = [_boom()]
    assert client.post("/api/invoices", json=_payload()).status_code == 502
    edited = _payload(items=[{"name": "Boiler repair", "quantity": "3", "unit_price": "60"}])
    resp = client.post("/api/invoices", json=edited)
    assert resp.status_code == 201
    assert resp.json()["total"] == "180.00"
    assert len(resp.json()["items"]) == 1


def test_edit_after_paypal_has_the_draft_is_a_conflict(
    client: TestClient, paypal: FakePayPal
) -> None:
    paypal.fail["send"] = [_boom()]
    assert client.post("/api/invoices", json=_payload()).status_code == 502
    edited = _payload(items=[{"name": "Boiler repair", "quantity": "3", "unit_price": "60"}])
    assert client.post("/api/invoices", json=edited).status_code == 409


def test_paypal_4xx_is_explained_and_not_retryable(client: TestClient, paypal: FakePayPal) -> None:
    paypal.fail["create"] = [
        PayPalError(422, {"name": "UNPROCESSABLE_ENTITY", "details": [{"issue": "USER_NOT_FOUND"}]})
    ]
    resp = client.post("/api/invoices", json=_payload())
    assert resp.status_code == 502
    assert resp.json()["retryable"] is False
    assert "business account" in resp.json()["detail"]


@pytest.mark.parametrize(
    ("overrides", "fragment"),
    [
        ({"currency": "NGN"}, "PayPal can't invoice in NGN"),
        ({"client_email": "kennedy at example"}, "email"),
        ({"items": []}, "at least 1"),
        ({"items": [{"name": "Valve", "quantity": "1", "unit_price": "0"}]}, "greater than 0"),
        ({"items": [{"name": " ", "quantity": "1", "unit_price": "5"}]}, ""),
        ({"items": [{"name": "Valve", "quantity": "0.333", "unit_price": "5"}]}, "decimal"),
        ({"due_date": "2020-01-01"}, "past"),
        ({"idempotency_key": "x"}, ""),
    ],
)
def test_bad_cards_are_refused_before_paypal(
    client: TestClient, paypal: FakePayPal, overrides: dict[str, Any], fragment: str
) -> None:
    resp = client.post("/api/invoices", json=_payload(**overrides))
    assert resp.status_code == 422
    assert fragment in resp.text
    assert paypal.calls == []


def test_same_client_email_is_one_client(client: TestClient, paypal: FakePayPal) -> None:
    client.post("/api/invoices", json=_payload())
    client.post("/api/invoices", json=_payload(idempotency_key="tap-0002-abcdef", client_name=""))
    third = client.post(
        "/api/invoices", json=_payload(idempotency_key="tap-0003-abcdef", client_name="Tom Kennedy")
    )
    assert third.json()["client_name"] == "Tom Kennedy"
    assert paypal.count("send") == 3


def test_read_back_and_unknown_invoice(client: TestClient, paypal: FakePayPal) -> None:
    inv = client.post("/api/invoices", json=_payload()).json()
    got = client.get(f"/api/invoices/{inv['id']}")
    assert got.status_code == 200
    assert got.json()["total"] == "145.00"
    assert client.get("/api/invoices/nope").status_code == 404


def test_qr_code_is_fetched_once_then_cached(client: TestClient, paypal: FakePayPal) -> None:
    inv = client.post("/api/invoices", json=_payload()).json()
    for _ in range(2):
        resp = client.get(f"/api/invoices/{inv['id']}/qr.png")
        assert resp.status_code == 200
        assert resp.headers["content-type"] == "image/png"
        assert resp.content == PNG
    assert paypal.count("qr") == 1


def test_no_qr_code_for_an_unsent_draft(client: TestClient, paypal: FakePayPal) -> None:
    from sqlalchemy import select

    from app.db import get_sessionmaker
    from app.models import Invoice

    paypal.fail["send"] = [_boom()]
    assert client.post("/api/invoices", json=_payload()).status_code == 502

    async def draft_id() -> str:
        async with get_sessionmaker()() as session:
            return str(await session.scalar(select(Invoice.id).where(Invoice.status == "draft")))

    unsent = client.portal.call(draft_id)  # type: ignore[union-attr]
    assert client.get(f"/api/invoices/{unsent}/qr.png").status_code == 409
    assert paypal.count("qr") == 0


def test_app_page_renders(client: TestClient) -> None:
    resp = client.get("/")
    assert resp.status_code == 200
    assert "What did you do?" in resp.text
    assert "Dave&#39;s Plumbing" in resp.text
    assert client.get("/static/app.js").status_code == 200
