"""Client memory and the client's language."""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from datetime import date
from decimal import Decimal
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.agents.chase import ReminderFacts, template_reminder
from app.agents.parser import JobParser, get_job_parser
from app.deals.agent import get_deal_model
from app.i18n import HERE, LANGUAGES, base, fmt_date, t
from app.main import app
from app.paypal import get_paypal
from app.schemas.job import ParsedItem, ParsedJob
from tests.test_invoices_api import FakePayPal, _payload
from tests.test_negotiation import ScriptedAgent, turn
from tests.test_parse_api import FakeModel
from tests.test_quotes_api import _quote, _token


@pytest.fixture
def paypal() -> Iterator[FakePayPal]:
    fake = FakePayPal()
    app.dependency_overrides[get_paypal] = lambda: fake
    yield fake
    app.dependency_overrides.pop(get_paypal, None)


@pytest.fixture
def model() -> Iterator[FakeModel]:
    fake = FakeModel()
    app.dependency_overrides[get_job_parser] = lambda: JobParser(fake)
    yield fake
    app.dependency_overrides.pop(get_job_parser, None)


def _spoken(name: str, email: str = "") -> ParsedJob:
    return ParsedJob(
        transcript=f"Tap for {name}", client_name=name, client_email=email, currency="GBP",
        items=[ParsedItem(name="Replace tap", quantity=1, unit_price=90, unit="fixed")],
        missing_fields=[] if email else ["client_email"],
        follow_up_question="" if email else f"What's {name}'s email?",
    )  # fmt: skip


def _invoice_to(client: TestClient, name: str, email: str, language: str, key: str) -> None:
    body = _payload(idempotency_key=key, client_name=name, client_email=email, language=language)
    assert client.post("/api/invoices", json=body).status_code == 201


# ── client memory ─────────────────────────────────────────────────────────────


def test_a_saved_client_is_remembered(
    client: TestClient, paypal: FakePayPal, model: FakeModel
) -> None:
    _invoice_to(client, "Priya Patel", "patel@example.com", "es", "mem-0000001")
    model.outcomes = [_spoken("Mrs Patel")]
    card = client.post("/api/parse", data={"text": "Tap for Mrs Patel, ninety pounds"}).json()
    assert (card["client_email"], card["client_name"]) == ("patel@example.com", "Priya Patel")
    assert card["client_source"] == "saved" and card["client_language"] == "es"
    assert card["missing_fields"] == [] and card["ready"] is True
    assert card["follow_up_question"] == ""  # no need to ask any more


def test_two_patels_are_offered_not_guessed(
    client: TestClient, paypal: FakePayPal, model: FakeModel
) -> None:
    _invoice_to(client, "Priya Patel", "priya@example.com", "en", "mem-0000001")
    _invoice_to(client, "Raj Patel", "raj@example.com", "en", "mem-0000002")
    model.outcomes = [_spoken("Mr Patel")]
    card = client.post("/api/parse", data={"text": "Tap for Mr Patel"}).json()
    assert card["client_email"] == "" and card["ready"] is False
    assert {c["email"] for c in card["client_suggestions"]} == {
        "priya@example.com",
        "raj@example.com",
    }


def test_a_new_name_is_just_asked(client: TestClient, paypal: FakePayPal, model: FakeModel) -> None:
    model.outcomes = [_spoken("Mr Okafor")]
    card = client.post("/api/parse", data={"text": "Garden for Mr Okafor"}).json()
    assert card["client_source"] == "" and card["missing_fields"] == ["client_email"]


def test_the_name_box_suggests_saved_clients(client: TestClient, paypal: FakePayPal) -> None:
    _invoice_to(client, "Priya Patel", "patel@example.com", "es", "mem-0000001")
    found = client.get("/api/clients", params={"q": "pat"}).json()
    assert found == [{"name": "Priya Patel", "email": "patel@example.com", "language": "es"}]


# ── the client's language ─────────────────────────────────────────────────────


def test_every_translation_keeps_every_placeholder() -> None:
    english = json.loads((HERE / "en.json").read_text(encoding="utf-8"))
    slots = re.compile(r"\{[a-z_]+\}")
    for code in LANGUAGES:
        if code == "en":
            continue
        local = json.loads((HERE / f"{code}.json").read_text(encoding="utf-8"))
        assert set(local) == set(english), code
        for key, text in local.items():
            assert sorted(slots.findall(text)) == sorted(slots.findall(english[key])), (code, key)


def test_language_codes_and_fallbacks() -> None:
    assert base("es-MX") == "es" and base("ja") == "en" and base(None) == "en"
    assert t("es", "send") != "Send" and t("xx", "send") == "Send"
    assert fmt_date(date(2026, 12, 4), "en", year=True) == "4 Dec 2026"
    assert fmt_date(date(2026, 12, 4), "fr", year=True) == "04.12.2026"


def test_a_spanish_clients_quote_page_is_in_spanish(client: TestClient) -> None:
    q = client.post("/api/quotes", json=_quote(language="es-ES")).json()
    page = client.get(f"/q/{_token(q)}").text
    assert '<html lang="es">' in page
    assert t("es", "ask_change") in page and t("es", "footer") in page
    assert "<h2>Ask for a change</h2>" not in page and "Prepared for" not in page


def test_a_spanish_clients_invoice_offers_a_plan_in_spanish(
    client: TestClient, paypal: FakePayPal
) -> None:
    _invoice_to(client, "Carmen López", "carmen@example.es", "es", "es-0000001")
    note = paypal.last("create")["body"]["detail"]["note"]
    assert t("es", "plan_line", url="X").split("X")[0].strip() in note
    assert "Can't pay it all at once?" not in note


def test_the_deal_agent_and_its_fixed_words_speak_spanish(client: TestClient) -> None:
    agent = ScriptedAgent()
    app.dependency_overrides[get_deal_model] = lambda: agent
    try:
        q = client.post("/api/quotes", json=_quote(language="es")).json()
        agent.turns = [turn("x", pct=50), turn("x", pct=50)]
        msgs = client.post(f"/q/{_token(q)}/messages", json={"text": "¿Mitad de precio?"}).json()
    finally:
        app.dependency_overrides.pop(get_deal_model, None)
    assert "Español" in agent.calls[0][0]  # the agent is told the client's language
    assert msgs["messages"][-1]["text"] == t("es", "msg_check_with_owner", owner="Dave")


def _facts(**kw: Any) -> ReminderFacts:
    base_facts: dict[str, Any] = dict(
        business="Dave's Plumbing", owner="Dave", client_name="Carmen López", number="0007",
        paypal_invoice_id="INV2-7", currency="EUR", total=Decimal("60.00"),
        amount_paid=Decimal(0), balance=Decimal("60.00"), due_date=date(2026, 11, 1),
        days_overdue=9, step=2, language="es",
    )  # fmt: skip
    base_facts.update(kw)
    return ReminderFacts(**base_facts)


def test_reminders_fall_back_to_the_clients_language() -> None:
    draft = template_reminder(_facts())
    assert draft.subject == t("es", "subject_2", number="0007", business="Dave's Plumbing")
    assert draft.note.startswith(t("es", "reminder_2", name="Carmen", number="0007",
                                   amount="€60.00", days=9))  # fmt: skip
    assert template_reminder(_facts(language="en")).note.startswith("Hi Carmen")
