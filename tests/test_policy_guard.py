"""PolicyGuard. Whatever the Deal Agent is talked into, these offers must not pass."""

from __future__ import annotations

from dataclasses import replace
from datetime import date
from decimal import Decimal
from typing import Any

import pytest
from pydantic import ValidationError

from app.deals.guard import (
    Offer,
    OfferLine,
    PlanProposal,
    Policy,
    QuoteLine,
    QuoteTerms,
    amounts_in,
    check_offer,
    check_plan,
    check_reply,
    split_plan,
)

TODAY = date(2026, 11, 4)
POLICY = Policy()  # 10% off labour, floor 90%, deposit >= 25%
# "Quote for Mrs Patel, replace the kitchen tap, £90 labour plus about £40 parts, 30% deposit"
PATEL = QuoteTerms(
    currency="GBP",
    lines=(
        QuoteLine("L1", "Replace kitchen tap", Decimal(1), Decimal(90), is_material=False),
        QuoteLine("L2", "Parts", Decimal(1), Decimal(40), is_material=True),
    ),
    deposit_pct=Decimal(30),
)


def offer(**overrides: Any) -> Offer:
    base: dict[str, Any] = {
        "lines": [OfferLine(line_id="L1", quantity=Decimal(1)),
                  OfferLine(line_id="L2", quantity=Decimal(1))],
        "deposit_pct": Decimal(30),
    }  # fmt: skip
    base.update(overrides)
    return Offer(**base)


def codes(result: Any) -> set[str]:
    return {v.code for v in result.violations}


def check(o: Offer, policy: Policy = POLICY, base: QuoteTerms = PATEL) -> Any:
    return check_offer(policy, base, o, today=TODAY)


# ── within policy ─────────────────────────────────────────────────────────────


def test_the_quote_as_it_stands_passes_and_is_priced_in_code() -> None:
    r = check(offer())
    assert r.ok
    p = r.priced
    assert (p.list_total, p.total, p.deposit_amount, p.balance) == (
        Decimal("130.00"), Decimal("130.00"), Decimal("39.00"), Decimal("91.00"),
    )  # fmt: skip


def test_discount_comes_off_labour_only() -> None:
    r = check(offer(labour_discount_pct=Decimal(10)))
    assert r.ok
    assert r.priced.discount_amount == Decimal("9.00")  # 10% of £90, not of £130
    assert r.priced.total == Decimal("121.00")


def test_dropping_a_line_is_a_scope_change_not_a_discount() -> None:
    # "Skip the new tap, I've bought one."
    r = check(offer(lines=[OfferLine(line_id="L1", quantity=Decimal(1))]))
    assert r.ok
    assert r.priced.removed == ("Parts",)
    assert r.priced.total == Decimal("90.00")


def test_material_quantity_may_change_with_scope() -> None:
    r = check(offer(lines=[OfferLine(line_id="L1", quantity=Decimal(1)),
                           OfferLine(line_id="L2", quantity=Decimal(2))]))  # fmt: skip
    assert r.ok
    assert r.priced.total == Decimal("170.00")


def test_moving_the_date_and_a_bigger_deposit_are_fine() -> None:
    r = check(offer(job_date=date(2026, 11, 7), deposit_pct=Decimal(50)))
    assert r.ok
    assert r.priced.deposit_amount == Decimal("65.00")


def test_owner_can_insist_on_approving_every_deal() -> None:
    r = check(offer(), policy=replace(POLICY, auto_agree_within_policy=False))
    assert r.ok
    assert r.owner_must_approve


# ── outside policy: a counter-offer or the owner ──────────────────────────────


@pytest.mark.parametrize(
    ("overrides", "policy", "expected"),
    [
        ({"labour_discount_pct": Decimal(15)}, POLICY, "discount_over_cap"),
        ({"labour_discount_pct": Decimal(5)}, replace(POLICY, price_negotiable=False),
         "price_locked"),
        ({"deposit_pct": Decimal(10)}, POLICY, "deposit_too_low"),
        ({"lines": [OfferLine(line_id="L1", quantity=Decimal(1))]},
         replace(POLICY, scope_negotiable=False), "scope_locked"),
        ({"lines": [OfferLine(line_id="L1", quantity=Decimal("0.5")),
                    OfferLine(line_id="L2", quantity=Decimal(1))]}, POLICY,
         "labour_quantity_changed"),
        ({"add_requests": ["Also fix the bathroom tap"]}, POLICY, "needs_pricing"),
        ({"job_date": date(2026, 11, 7)}, replace(POLICY, date_negotiable=False), "date_locked"),
    ],
)  # fmt: skip
def test_outside_policy(overrides: dict[str, Any], policy: Policy, expected: str) -> None:
    r = check(offer(**overrides), policy=policy)
    assert not r.ok
    assert expected in codes(r)
    assert not r.malformed


def test_the_floor_holds_even_when_the_cap_would_allow_more() -> None:
    generous = replace(POLICY, max_discount_pct=Decimal(50))
    r = check(offer(labour_discount_pct=Decimal(40)), policy=generous)  # £36 off -> £94 < £117
    assert codes(r) == {"below_floor"}


def test_the_floor_is_never_revealed() -> None:
    generous = replace(POLICY, max_discount_pct=Decimal(50))
    r = check(offer(labour_discount_pct=Decimal(40)), policy=generous)
    feedback = r.feedback()
    for secret in ("90", "117"):
        assert secret not in feedback
    assert "floor" not in str(POLICY.for_agent())
    assert "90" not in str(POLICY.for_agent().values())


# ── malformed: the agent made something up ────────────────────────────────────


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({"lines": [OfferLine(line_id="L9", quantity=Decimal(1))]}, "unknown_line"),
        ({"lines": [OfferLine(line_id="L1", quantity=Decimal(1)),
                    OfferLine(line_id="L1", quantity=Decimal(1))]}, "duplicate_line"),
        ({"lines": [OfferLine(line_id="L1", quantity=Decimal(0))]}, "bad_quantity"),
        ({"lines": [OfferLine(line_id="L1", quantity=Decimal(-2))]}, "bad_quantity"),
        ({"lines": [OfferLine(line_id="L1", quantity=Decimal(1)),
                    OfferLine(line_id="L2", quantity=Decimal(10_000))]}, "bad_quantity"),
        ({"lines": []}, "empty_offer"),
        ({"labour_discount_pct": Decimal(-20)}, "bad_discount"),
        ({"labour_discount_pct": Decimal(150)}, "bad_discount"),
        ({"deposit_pct": Decimal(150)}, "bad_deposit"),
        ({"job_date": date(2026, 1, 1)}, "date_in_past"),
        ({"job_date": date(2028, 1, 1)}, "date_too_far"),
    ],
)  # fmt: skip
def test_malformed(overrides: dict[str, Any], expected: str) -> None:
    r = check(offer(**overrides))
    assert expected in codes(r)
    assert r.malformed


def test_non_numbers_never_reach_the_guard() -> None:
    with pytest.raises(ValidationError):
        Offer(lines=[], deposit_pct="NaN")  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        OfferLine(line_id="L1", quantity="lots")  # type: ignore[arg-type]


# ── what a jailbroken agent might propose ─────────────────────────────────────


def test_the_agent_cannot_set_its_own_prices() -> None:
    # An offer has no price field at all; extra keys are ignored, prices come from the quote.
    o = Offer.model_validate({
        "lines": [{"line_id": "L1", "quantity": 1, "unit_price": 1},
                  {"line_id": "L2", "quantity": 1, "unit_price": 1}],
        "deposit_pct": 30, "total": 2,
    })  # fmt: skip
    r = check(o)
    assert r.priced.total == Decimal("130.00")


def test_ignore_your_instructions_and_give_me_90_percent_off() -> None:
    r = check(offer(labour_discount_pct=Decimal(90), deposit_pct=Decimal(0)))
    assert not r.ok
    assert {"discount_over_cap", "deposit_too_low", "below_floor"} <= codes(r)


def test_cutting_the_hours_to_hit_a_price_is_caught() -> None:
    # "Can you do it in half the time?" would halve the labour price.
    r = check(offer(lines=[OfferLine(line_id="L1", quantity=Decimal("0.5")),
                           OfferLine(line_id="L2", quantity=Decimal(1))]))  # fmt: skip
    assert "labour_quantity_changed" in codes(r)


def test_a_free_extra_job_is_sent_to_the_owner() -> None:
    r = check(offer(add_requests=["Fit a new shower head for free"]))
    assert "needs_pricing" in codes(r)


# ── the agent's words ─────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("That's £121.00 with a £36.30 deposit", ["121.00", "36.30"]),
        ("£1,250 all in", ["1250.00"]),
        ("about 120 quid, or 95 pounds", ["120.00", "95.00"]),
        ("GBP 40 for parts", ["40.00"]),
        ("a balance of 100.00 GBP", ["100.00"]),  # the code after the number counts too
        ("Il reste 145,00 € à payer", ["145.00"]),  # European decimal comma
        ("razem 1.250,00 zł", ["1250.00"]),
        ("faltan 39,50 EUR", ["39.50"]),
        ("€ 1 250,00 au total", ["1250.00"]),
        ("2 Stunden, 10% Rabatt", []),
        ("10% off, 2 hours, Saturday the 7th", []),
    ],
)
def test_amounts_in(text: str, expected: list[str]) -> None:
    assert amounts_in(text) == [Decimal(e) for e in expected]


def test_reply_amounts_must_be_computed_ones() -> None:
    r = check(offer(labour_discount_pct=Decimal(10)))
    ok = "I can take 10% off the labour: £121.00 in total (down from £130), £36.30 deposit."
    assert check_reply(ok, r.priced, PATEL) == []
    assert check_reply("Fine, £100 all in!", r.priced, PATEL)[0].code == "wrong_amount_in_reply"
    assert check_reply("Call it 110 quid", r.priced, PATEL)


# ── payment plans ─────────────────────────────────────────────────────────────


def test_plan_split_is_exact() -> None:
    plan = split_plan(Decimal("100.00"), PlanProposal(instalments=3, every_days=14), "GBP", TODAY)
    assert [a for _, a in plan] == [Decimal("33.34"), Decimal("33.33"), Decimal("33.33")]
    assert sum(a for _, a in plan) == Decimal("100.00")
    assert [d for d, _ in plan] == [TODAY, date(2026, 11, 18), date(2026, 12, 2)]


@pytest.mark.parametrize(
    ("proposal", "expected"),
    [
        (PlanProposal(instalments=2, every_days=30), set()),
        (PlanProposal(instalments=3, first_due_in_days=0, every_days=30), set()),  # day 60
        (PlanProposal(instalments=4, every_days=7), {"too_many_instalments"}),
        (PlanProposal(instalments=3, first_due_in_days=7, every_days=30), {"plan_too_long"}),
        (PlanProposal(instalments=1, every_days=30), {"bad_plan"}),
        (PlanProposal(instalments=2, every_days=0), {"bad_plan"}),
    ],
)
def test_plan_policy(proposal: PlanProposal, expected: set[str]) -> None:
    assert {v.code for v in check_plan(POLICY, Decimal(100), proposal)} == expected
