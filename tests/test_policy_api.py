from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.db import get_sessionmaker
from app.models import Event

DEFAULTS = {
    "max_discount_pct": "10.00",
    "floor_pct": "90.00",
    "min_deposit_pct": "25.00",
    "max_instalments": 3,
    "max_plan_days": 60,
    "price_negotiable": True,
    "scope_negotiable": True,
    "date_negotiable": True,
    "auto_agree_within_policy": True,
}


def test_new_workspace_gets_sensible_defaults(client: TestClient) -> None:
    resp = client.get("/api/policy")
    assert resp.status_code == 200
    got = resp.json()
    assert {k: str(v) if isinstance(v, str) else v for k, v in got.items()} == DEFAULTS


def test_saving_the_rules_is_logged(client: TestClient) -> None:
    new = {**DEFAULTS, "max_discount_pct": 15, "auto_agree_within_policy": False}
    resp = client.put("/api/policy", json=new)
    assert resp.status_code == 200
    assert resp.json()["max_discount_pct"] == "15.00"
    assert client.get("/api/policy").json()["auto_agree_within_policy"] is False

    async def changes() -> list[Any]:
        async with get_sessionmaker()() as s:
            return list(await s.scalars(select(Event.data).where(Event.kind == "policy_changed")))

    logged = client.portal.call(changes)  # type: ignore[union-attr]
    assert logged == [
        {"max_discount_pct": ["10.00", "15.00"], "auto_agree_within_policy": [True, False]}
    ]


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("max_discount_pct", 80),  # a slip of the thumb must not give the work away
        ("max_discount_pct", -1),
        ("floor_pct", 20),
        ("floor_pct", 120),
        ("min_deposit_pct", 101),
        ("max_instalments", 0),
        ("max_plan_days", 1000),
    ],
)
def test_out_of_range_rules_are_refused(client: TestClient, field: str, value: Any) -> None:
    assert client.put("/api/policy", json={**DEFAULTS, field: value}).status_code == 422
    assert client.get("/api/policy").json()[field] == DEFAULTS[field]
