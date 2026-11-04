from __future__ import annotations

from typing import Any

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.db import get_sessionmaker
from app.models import Event, Invoice, Workspace
from app.services.live import broker
from app.services.payments import reconcile_once
from tests.test_invoices_api import FakePayPal, _payload, paypal  # noqa: F401 - fixture


def _remote(status: str, paid: str | None = None) -> dict[str, Any]:
    body: dict[str, Any] = {"id": "INV2-1", "status": status}
    if paid is not None:
        body["payments"] = {"paid_amount": {"currency_code": "GBP", "value": paid}}
    return body


def _db(client: TestClient, fn: Any) -> Any:
    async def run() -> Any:
        async with get_sessionmaker()() as session:
            return await fn(session)

    return client.portal.call(run)  # type: ignore[union-attr]


def _event_kinds(client: TestClient) -> list[str]:
    async def kinds(session: Any) -> list[str]:
        return list(await session.scalars(select(Event.kind).order_by(Event.created_at)))

    return _db(client, kinds)


def test_part_payment_then_full_payment(client: TestClient, paypal: FakePayPal) -> None:  # noqa: F811
    inv = client.post("/api/invoices", json=_payload()).json()

    paypal.remote["INV2-1"] = _remote("PARTIALLY_PAID", "50.00")
    part = client.post(f"/api/invoices/{inv['id']}/refresh").json()
    assert part["status"] == "partially_paid"
    assert part["amount_paid"] == "50.00"
    assert part["paid_at"] is None

    paypal.remote["INV2-1"] = _remote("MARKED_AS_PAID", "145.00")
    paid = client.post(f"/api/invoices/{inv['id']}/refresh").json()
    assert paid["status"] == "paid"
    assert paid["amount_paid"] == "145.00"
    assert paid["paid_at"]

    assert _event_kinds(client)[-4:] == [
        "payment_received", "invoice_partially_paid", "payment_received", "invoice_paid",
    ]  # fmt: skip


def test_no_change_means_no_new_events(client: TestClient, paypal: FakePayPal) -> None:  # noqa: F811
    inv = client.post("/api/invoices", json=_payload()).json()
    before = _event_kinds(client)
    client.post(f"/api/invoices/{inv['id']}/refresh")
    assert _event_kinds(client) == before


def test_changes_are_pushed_live(client: TestClient, paypal: FakePayPal) -> None:  # noqa: F811
    inv = client.post("/api/invoices", json=_payload()).json()

    async def ws_id(session: Any) -> str:
        return str(await session.scalar(select(Workspace.id)))

    with broker.subscribe(_db(client, ws_id)) as queue:
        paypal.remote["INV2-1"] = _remote("PAID", "145.00")
        client.post(f"/api/invoices/{inv['id']}/refresh")
        event = queue.get_nowait()
    assert event == {
        "type": "invoice",
        "id": inv["id"],
        "number": "0001",
        "status": "paid",
        "currency": "GBP",
        "total": "145.00",
        "amount_paid": "145.00",
        "paid_at": event["paid_at"],
    }
    assert event["paid_at"]


def test_overdue_is_ours_and_a_draft_never_comes_back(
    client: TestClient,
    paypal: FakePayPal,  # noqa: F811
) -> None:
    inv = client.post("/api/invoices", json=_payload()).json()

    async def mark_overdue(session: Any) -> None:
        row = await session.get(Invoice, inv["id"])
        row.status = "overdue"
        await session.commit()

    _db(client, mark_overdue)
    paypal.remote["INV2-1"] = _remote("SENT")
    assert client.post(f"/api/invoices/{inv['id']}/refresh").json()["status"] == "overdue"
    paypal.remote["INV2-1"] = _remote("DRAFT")
    assert client.post(f"/api/invoices/{inv['id']}/refresh").json()["status"] == "overdue"


def test_unknown_paypal_status_changes_nothing(client: TestClient, paypal: FakePayPal) -> None:  # noqa: F811
    inv = client.post("/api/invoices", json=_payload()).json()
    paypal.remote["INV2-1"] = _remote("SOMETHING_NEW")
    assert client.post(f"/api/invoices/{inv['id']}/refresh").json()["status"] == "sent"


def test_reconcile_checks_only_open_invoices(client: TestClient, paypal: FakePayPal) -> None:  # noqa: F811
    client.post("/api/invoices", json=_payload())
    client.post("/api/invoices", json=_payload(idempotency_key="tap-0002-abcdef"))
    paypal.remote["INV2-1"] = _remote("PAID", "145.00")

    async def run() -> int:
        return await reconcile_once(get_sessionmaker(), paypal)

    assert client.portal.call(run) == 1  # type: ignore[union-attr]
    assert paypal.count("get") == 2
    # INV2-1 is paid now, so the next pass only asks about the other one.
    assert client.portal.call(run) == 0  # type: ignore[union-attr]
    assert paypal.count("get") == 3
