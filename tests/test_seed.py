"""The demo seed runs end to end through the real services, and twice is the same as once."""

from __future__ import annotations

from typing import Any

from fastapi.testclient import TestClient

from app.db import get_sessionmaker
from app.settings import get_settings
from scripts import seed_demo
from tests.test_invoices_api import FakePayPal


def _run(client: TestClient, paypal: FakePayPal) -> None:
    async def go() -> None:
        from app.services.invoicing import default_workspace

        async with get_sessionmaker()() as session:
            ws = await default_workspace(session, get_settings())
            await seed_demo.seed(session, paypal, ws)

    client.portal.call(go)  # type: ignore[union-attr]


def test_seed_gives_a_realistic_mix(client: TestClient) -> None:
    paypal = FakePayPal()
    _run(client, paypal)
    home: dict[str, Any] = client.get("/api/home").json()
    tabs = {t: [r["title"] for r in rows] for t, rows in home["tabs"].items()}
    flat = " ".join(f"{t}:{c}" for t, cs in tabs.items() for c in cs)
    for who in ("Mr Kennedy", "Ms Murphy", "Mrs Okafor", "Mr Wójcik", "Mrs Patel", "Mr Adeyemi",
                "Ms Chen", "Mr Brennan"):  # fmt: skip
        assert who in flat, (who, tabs)
    assert "Mrs Okafor" in tabs["overdue"]
    approvals = client.get("/api/approvals").json()
    assert any("25%" in (a.get("summary") or a.get("owner_summary") or str(a)) for a in approvals)
    # Real PayPal work: 4 invoices + 1 deposit sent, 3 payments recorded, 1 plan put on an invoice.
    assert paypal.count("send") == 5 and paypal.count("record") == 3 and paypal.count("update") == 1


def test_seed_twice_adds_nothing(client: TestClient) -> None:
    paypal = FakePayPal()
    _run(client, paypal)
    before = (paypal.count("create"), paypal.count("record"), client.get("/api/home").json())
    _run(client, paypal)
    after = (paypal.count("create"), paypal.count("record"), client.get("/api/home").json())
    assert before[:2] == after[:2]
    assert before[2]["totals"] == after[2]["totals"]
