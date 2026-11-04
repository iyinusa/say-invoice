from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from app.agents.parser_rules import finalize, normalise_email, resolve_due_date
from app.schemas.job import ParsedItem, ParsedJob

WED = date(2026, 11, 4)  # a Wednesday


def _job(**overrides: object) -> ParsedJob:
    base: dict[str, object] = {
        "transcript": "Fixed Mr Kennedy's boiler, 2 hours at £60, plus £25 for a valve",
        "client_name": "Mr Kennedy",
        "client_email": "kennedy@example.com",
        "currency": "GBP",
        "items": [
            ParsedItem(name="Boiler repair", quantity=2, unit_price=60, unit="hours"),
            ParsedItem(name="Valve", quantity=1, unit_price=25, is_material=True),
        ],
    }
    base.update(overrides)
    return ParsedJob(**base)  # type: ignore[arg-type]


def _final(job: ParsedJob, mode: str = "auto"):  # type: ignore[no-untyped-def]
    return finalize(job, requested_mode=mode, default_currency="GBP", today=WED)  # type: ignore[arg-type]


def test_complete_invoice_is_ready_and_priced_in_code() -> None:
    card = _final(_job())
    assert card.ready
    assert card.missing_fields == []
    assert card.follow_up_question == ""
    assert [i.line_total for i in card.items] == [Decimal("120.00"), Decimal("25.00")]
    assert card.subtotal == Decimal("145.00")
    assert card.items[1].is_material


def test_round_quantities_stay_plain_numbers() -> None:
    card = _final(_job(items=[ParsedItem(name="Cupcakes", quantity=30, unit_price=2.5)]))
    assert str(card.items[0].quantity) == "30"
    assert card.subtotal == Decimal("75.00")


def test_heard_email_is_highlighted_but_not_blocking() -> None:
    heard = finalize(_job(), requested_mode="auto", default_currency="GBP", today=WED, heard=True)
    assert "client_email" in heard.uncertain_fields
    assert heard.ready
    assert "client_email" not in _final(_job()).uncertain_fields  # typed: no doubt


def test_float_noise_never_reaches_the_money() -> None:
    card = _final(_job(items=[ParsedItem(name="Washer", quantity=3, unit_price=0.1)]))
    assert card.subtotal == Decimal("0.30")


def test_missing_email_blocks_an_invoice_and_asks_for_it() -> None:
    card = _final(_job(client_email=""))
    assert not card.ready
    assert card.missing_fields == ["client_email"]
    assert card.follow_up_question == "What's Mr Kennedy's email address?"


def test_model_question_kept_when_it_asks_about_the_same_thing() -> None:
    job = _job(
        client_email="",
        language="es",
        missing_fields=["client_email"],
        follow_up_question="¿Cuál es el correo del Sr. Kennedy?",
    )
    assert _final(job).follow_up_question == "¿Cuál es el correo del Sr. Kennedy?"


def test_model_cannot_wave_through_a_missing_field() -> None:
    # The model says nothing is missing; the rules disagree and win.
    card = _final(_job(client_email="", missing_fields=[], follow_up_question=""))
    assert not card.ready
    assert card.missing_fields == ["client_email"]


def test_model_question_about_a_lesser_field_is_replaced() -> None:
    job = _job(
        client_email="",
        items=[ParsedItem(name="Green waste disposal", quantity=1, unit_price=0)],
        missing_fields=["client_email"],
        follow_up_question="What's the email?",
    )
    card = _final(job)
    assert card.missing_fields == ["items.0.unit_price", "client_email"]
    assert card.follow_up_question == "How much should I charge for the green waste disposal?"


def test_no_items_asks_what_was_done() -> None:
    card = _final(_job(items=[]))
    assert card.missing_fields[0] == "items"
    assert card.subtotal == Decimal("0.00")


def test_padded_empty_rows_are_dropped() -> None:
    card = _final(_job(items=[ParsedItem(name="", quantity=0, unit_price=0)]))
    assert card.items == []


def test_zero_quantity_becomes_one_and_is_flagged() -> None:
    card = _final(_job(items=[ParsedItem(name="Call-out", quantity=0, unit_price=45)]))
    assert card.items[0].quantity == Decimal(1)
    assert "items.0.quantity" in card.uncertain_fields
    assert card.ready


def test_approximate_and_huge_prices_are_flagged_not_blocked() -> None:
    items = [
        ParsedItem(name="Parts", quantity=1, unit_price=40, approximate=True),
        ParsedItem(name="Labour", quantity=1, unit_price=90_000),
    ]
    card = _final(_job(items=items))
    assert {"items.0.unit_price", "items.1.unit_price"} <= set(card.uncertain_fields)
    assert card.ready


@pytest.mark.parametrize(
    ("said", "expected"),
    [
        ("Kennedy@Example.com", "kennedy@example.com"),
        (" kennedy @ example.com.", "kennedy@example.com"),
        ("kennedy at example dot com", None),
        ("not-an-email", None),
        ("", None),
    ],
)
def test_email_normalisation(said: str, expected: str | None) -> None:
    assert normalise_email(said) == expected


def test_bad_email_becomes_a_question_not_a_guess() -> None:
    card = _final(_job(client_email="kennedy at example"))
    assert card.client_email == ""
    assert card.missing_fields == ["client_email"]
    assert card.warnings


def test_currency_symbol_and_default() -> None:
    assert _final(_job(currency="€")).currency == "EUR"
    assert _final(_job(currency="")).currency == "GBP"
    assert _final(_job(currency="usd")).currency == "USD"


def test_unsupported_currency_is_flagged_and_not_ready() -> None:
    card = _final(_job(currency="NGN"))
    assert card.currency == "NGN"
    assert not card.ready
    assert "currency" in card.uncertain_fields
    assert "NGN" in card.warnings[0]


@pytest.mark.parametrize(
    ("fields", "expected", "uncertain"),
    [
        ({"due_weekday": "friday"}, date(2026, 11, 6), False),
        ({"due_weekday": "wednesday"}, date(2026, 11, 11), False),  # said on a Wednesday
        ({"due_weekday": "monday"}, date(2026, 11, 9), False),
        ({"due_weekday": "friday", "due_weeks_ahead": 1}, date(2026, 11, 13), False),
        ({"due_weekday": "friday", "due_weeks_ahead": 99}, None, True),
        ({"due_in_days": 14}, date(2026, 11, 18), False),
        ({"due_in_days": 0}, WED, False),
        ({"due_in_days": 9999}, None, True),
        ({"due_date": "2026-11-30"}, date(2026, 11, 30), False),
        ({"due_date": "2026-10-30"}, date(2026, 10, 30), True),  # in the past
        ({"due_date": "next week"}, None, True),
        ({"due_date": "2026-11-30", "due_weekday": "friday"}, date(2026, 11, 30), False),
        ({}, None, False),
    ],
)
def test_due_dates_are_resolved_in_code(
    fields: dict[str, object], expected: date | None, uncertain: bool
) -> None:
    assert resolve_due_date(_job(**fields), WED) == (expected, uncertain)


def test_quote_with_percent_deposit() -> None:
    items = [
        ParsedItem(name="Replace kitchen tap", quantity=1, unit_price=90, unit="fixed"),
        ParsedItem(name="Parts", quantity=1, unit_price=40, is_material=True, approximate=True),
    ]
    card = _final(_job(mode="quote", client_email="", items=items, deposit_pct=30))
    assert card.mode == "quote"
    assert card.ready  # a quote can go by text: a name is enough
    assert card.subtotal == Decimal("130.00")
    assert card.deposit_pct == Decimal(30)
    assert card.deposit_amount == Decimal("39.00")


def test_quote_with_fixed_deposit_derives_the_percent() -> None:
    items = [ParsedItem(name="Repaint hallway", quantity=1, unit_price=400, unit="fixed")]
    card = _final(_job(mode="quote", items=items, deposit_amount=50))
    assert card.deposit_amount == Decimal("50.00")
    assert card.deposit_pct == Decimal("12.50")


def test_impossible_deposit_is_flagged() -> None:
    card = _final(_job(mode="quote", deposit_pct=150))
    assert card.deposit_pct is None
    assert "deposit_pct" in card.uncertain_fields


def test_quote_needs_someone_to_send_it_to() -> None:
    card = _final(_job(mode="quote", client_name="", client_email=""))
    assert card.missing_fields == ["client_name"]
    assert card.follow_up_question == "Who is this quote for?"


def test_deposit_on_an_invoice_is_ignored_with_a_warning() -> None:
    card = _final(_job(deposit_pct=30))
    assert card.deposit_pct is None
    assert any("quote" in w for w in card.warnings)


def test_requested_mode_overrides_detected() -> None:
    assert _final(_job(mode="quote"), mode="invoice").mode == "invoice"
    assert _final(_job(mode="quote"), mode="auto").mode == "quote"


def test_missing_fields_are_not_also_listed_as_uncertain() -> None:
    card = _final(_job(client_email="kennedy at", uncertain_fields=["client_email"]))
    assert "client_email" in card.missing_fields
    assert "client_email" not in card.uncertain_fields
