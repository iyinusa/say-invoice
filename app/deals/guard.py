"""PolicyGuard: the code that decides whether an offer may reach the client.

The Deal Agent only *proposes* structured changes to a quote. It never sets a
price: it can keep or drop lines, change how many of a material are used,
offer a labour discount as a percentage, ask for a deposit percentage and
suggest a date. Everything here is plain Python. It reprices the offer with
`app.money`, checks it against the tradesperson's Deal Policy, and checks that
every amount in the agent's reply is one this module computed.

Client chat is untrusted input, and the agent that reads it may be fooled.
That's fine: whatever the agent is talked into, an offer outside the policy
cannot get past these functions.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any, Literal

from pydantic import BaseModel, Field

from app.money import line_total, percent_of, subtotal, to_money

# ── inputs ───────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Policy:
    max_discount_pct: Decimal = Decimal(10)
    floor_pct: Decimal = Decimal(90)
    min_deposit_pct: Decimal = Decimal(25)
    max_instalments: int = 3
    max_plan_days: int = 60
    price_negotiable: bool = True
    scope_negotiable: bool = True
    date_negotiable: bool = True
    auto_agree_within_policy: bool = True

    @classmethod
    def from_model(cls, m: Any) -> Policy:
        return cls(**{name: getattr(m, name) for name in cls.__dataclass_fields__})

    def for_agent(self) -> dict[str, Any]:
        """What the Deal Agent may know. The floor is deliberately left out."""
        return {
            "max_labour_discount_pct": str(self.max_discount_pct),
            "materials_discountable": False,
            "min_deposit_pct": str(self.min_deposit_pct),
            "price_negotiable": self.price_negotiable,
            "scope_negotiable": self.scope_negotiable,
            "date_negotiable": self.date_negotiable,
            "max_instalments": self.max_instalments,
            "max_plan_days": self.max_plan_days,
        }


@dataclass(frozen=True)
class QuoteLine:
    id: str
    name: str
    quantity: Decimal
    unit_price: Decimal
    is_material: bool


@dataclass(frozen=True)
class QuoteTerms:
    """The current agreed-or-offered version of a quote."""

    currency: str
    lines: tuple[QuoteLine, ...]
    deposit_pct: Decimal
    job_date: date | None = None

    @property
    def total(self) -> Decimal:
        return subtotal(((ln.quantity, ln.unit_price) for ln in self.lines), self.currency)


class OfferLine(BaseModel):
    line_id: str = Field(description="id of a line in the current quote")
    quantity: Decimal = Field(description="usually unchanged; materials may change with scope")


class Offer(BaseModel):
    """What the Deal Agent proposes. No prices: code supplies those."""

    lines: list[OfferLine] = Field(description="the lines to keep; leave one out to drop it")
    add_requests: list[str] = Field(
        default_factory=list, description="new work the client asked for; the owner must price it"
    )
    labour_discount_pct: Decimal = Field(default=Decimal(0), description="% off labour lines only")
    deposit_pct: Decimal
    job_date: date | None = None


class PlanProposal(BaseModel):
    """A payment plan as the agent proposes it: a shape, never amounts."""

    instalments: int
    first_due_in_days: int = 0
    every_days: int


# ── outputs ──────────────────────────────────────────────────────────────────

ViolationKind = Literal["malformed", "outside_policy"]


@dataclass(frozen=True)
class Violation:
    code: str
    kind: ViolationKind  # malformed: the agent erred; outside_policy: needs a counter or the owner
    message: str  # safe to show the agent: never contains the floor


@dataclass(frozen=True)
class PricedLine:
    line_id: str
    name: str
    quantity: Decimal
    unit_price: Decimal
    is_material: bool
    line_total: Decimal


@dataclass(frozen=True)
class PricedOffer:
    currency: str
    lines: tuple[PricedLine, ...]
    list_total: Decimal  # the kept work at full price
    labour_total: Decimal
    discount_pct: Decimal
    discount_amount: Decimal
    total: Decimal
    deposit_pct: Decimal
    deposit_amount: Decimal
    balance: Decimal
    job_date: date | None
    removed: tuple[str, ...] = ()  # names of dropped lines
    add_requests: tuple[str, ...] = ()

    def to_json(self) -> dict[str, Any]:
        """Stored on the deal message; amounts as strings, like the API."""
        return {
            "currency": self.currency,
            "lines": [
                {
                    "line_id": ln.line_id,
                    "name": ln.name,
                    "quantity": str(ln.quantity),
                    "unit_price": str(ln.unit_price),
                    "is_material": ln.is_material,
                    "line_total": str(ln.line_total),
                }
                for ln in self.lines
            ],
            "list_total": str(self.list_total),
            "labour_total": str(self.labour_total),
            "discount_pct": str(self.discount_pct),
            "discount_amount": str(self.discount_amount),
            "total": str(self.total),
            "deposit_pct": str(self.deposit_pct),
            "deposit_amount": str(self.deposit_amount),
            "balance": str(self.balance),
            "job_date": self.job_date.isoformat() if self.job_date else None,
            "removed": list(self.removed),
            "add_requests": list(self.add_requests),
        }

    def amounts(self) -> set[Decimal]:
        """Every figure the agent may quote back to the client."""
        found = {
            self.list_total, self.labour_total, self.discount_amount, self.total,
            self.deposit_amount, self.balance,
        }  # fmt: skip
        for ln in self.lines:
            found |= {ln.unit_price, ln.line_total}
        return found


@dataclass
class GuardResult:
    priced: PricedOffer | None
    violations: list[Violation] = field(default_factory=list)
    # True when the offer is within policy but the owner wants to approve every deal.
    owner_must_approve: bool = False

    @property
    def ok(self) -> bool:
        return not self.violations

    @property
    def malformed(self) -> bool:
        return any(v.kind == "malformed" for v in self.violations)

    def feedback(self) -> str:
        """One line per problem, for the agent's retry."""
        return "\n".join(f"- {v.code}: {v.message}" for v in self.violations)


# ── checks ───────────────────────────────────────────────────────────────────


def check_offer(policy: Policy, base: QuoteTerms, offer: Offer, *, today: date) -> GuardResult:
    cur = base.currency
    out: list[Violation] = []

    def bad(code: str, kind: ViolationKind, message: str) -> None:
        out.append(Violation(code, kind, message))

    by_id = {ln.id: ln for ln in base.lines}
    seen: set[str] = set()
    kept: list[PricedLine] = []
    for ol in offer.lines:
        orig = by_id.get(ol.line_id)
        if orig is None:
            bad("unknown_line", "malformed", f"there is no line with id {ol.line_id!r}")
            continue
        if ol.line_id in seen:
            bad("duplicate_line", "malformed", f"line {ol.line_id!r} appears twice")
            continue
        seen.add(ol.line_id)
        try:
            qty = Decimal(ol.quantity)
        except (InvalidOperation, TypeError):
            qty = Decimal(-1)
        if not qty.is_finite() or qty <= 0 or qty > orig.quantity * 10 + 10:
            bad(
                "bad_quantity", "malformed", f"quantity for {orig.name!r} must be a sensible number"
            )
            continue
        qty = qty.quantize(Decimal("0.01"))
        if qty != orig.quantity:
            if not orig.is_material:
                # Fewer hours for the same job is a discount in disguise.
                bad(
                    "labour_quantity_changed",
                    "outside_policy",
                    f"labour on {orig.name!r} can't change; use the labour discount instead",
                )
            elif not policy.scope_negotiable:
                bad("scope_locked", "outside_policy", "the owner doesn't change the scope")
        kept.append(
            PricedLine(
                line_id=orig.id,
                name=orig.name,
                quantity=qty,
                unit_price=orig.unit_price,  # never from the agent
                is_material=orig.is_material,
                line_total=line_total(qty, orig.unit_price, cur),
            )
        )

    removed = tuple(ln.name for ln in base.lines if ln.id not in seen)
    if removed and not policy.scope_negotiable:
        bad("scope_locked", "outside_policy", "the owner doesn't drop lines from a quote")
    if not kept and not out:
        bad("empty_offer", "malformed", "an offer needs at least one line")
    if offer.add_requests:
        bad("needs_pricing", "outside_policy", "new work has to be priced by the owner")

    pct = Decimal(offer.labour_discount_pct)
    if not pct.is_finite() or pct < 0 or pct > 100:
        bad("bad_discount", "malformed", "the labour discount must be a percentage from 0 to 100")
        pct = Decimal(0)
    elif pct > 0 and not policy.price_negotiable:
        bad("price_locked", "outside_policy", "the owner doesn't discount; offer no discount")
    elif pct > policy.max_discount_pct:
        bad("discount_over_cap", "outside_policy",
            f"the labour discount can be at most {policy.max_discount_pct}%")  # fmt: skip

    list_total = subtotal(((ln.quantity, ln.unit_price) for ln in kept), cur)
    labour_total = subtotal(
        ((ln.quantity, ln.unit_price) for ln in kept if not ln.is_material), cur
    )
    discount = percent_of(labour_total, pct, cur)  # materials are never discounted
    total = list_total - discount
    if kept and total < percent_of(list_total, policy.floor_pct, cur):
        bad("below_floor", "outside_policy",
            "that total is lower than the owner accepts; offer a smaller discount")  # fmt: skip

    dep = Decimal(offer.deposit_pct)
    if not dep.is_finite() or dep < 0 or dep > 100:
        bad("bad_deposit", "malformed", "the deposit must be a percentage from 0 to 100")
        dep = policy.min_deposit_pct
    elif dep < policy.min_deposit_pct:
        bad("deposit_too_low", "outside_policy",
            f"the deposit must be at least {policy.min_deposit_pct}%")  # fmt: skip
    deposit_amount = percent_of(total, dep, cur) if total > 0 else to_money(0, cur)

    if offer.job_date != base.job_date and offer.job_date is not None:
        if not policy.date_negotiable:
            bad("date_locked", "outside_policy", "the owner doesn't move the job date")
        elif offer.job_date < today:
            bad("date_in_past", "malformed", "the job date can't be in the past")
        elif offer.job_date > today + timedelta(days=365):
            bad("date_too_far", "malformed", "the job date must be within a year")

    priced = PricedOffer(
        currency=cur,
        lines=tuple(kept),
        list_total=list_total,
        labour_total=labour_total,
        discount_pct=pct,
        discount_amount=discount,
        total=total,
        deposit_pct=dep,
        deposit_amount=deposit_amount,
        balance=total - deposit_amount,
        job_date=offer.job_date or base.job_date,
        removed=removed,
        add_requests=tuple(offer.add_requests),
    )
    return GuardResult(
        priced=priced,
        violations=out,
        owner_must_approve=not out and not policy.auto_agree_within_policy,
    )


# ── the agent's words ────────────────────────────────────────────────────────

# A number in either style: 1,250.00 (UK/US) or 1.250,00 / 1 250,00 (most of Europe).
# Groups of three are thousands; one or two digits after the last separator are pennies.
_NUM = r"(\d{1,3}(?:[.,\u00a0\u202f ]\d{3})+(?:[.,]\d{1,2})?|\d+(?:[.,]\d{1,2})?)(?!\d)"
_BEFORE = r"(?:[£$€]|\b(?:GBP|USD|EUR|PLN)\b)\s?"
_AFTER = r"\s?(?:[£$€]|zł|(?:GBP|USD|EUR|PLN|pounds?|quid|euros?|dollars?|bucks|złotych|zlotych)\b)"
_AMOUNT = re.compile(rf"{_BEFORE}{_NUM}|{_NUM}{_AFTER}", re.IGNORECASE)


def _to_decimal(token: str) -> Decimal:
    token = token.strip()
    tail = re.search(r"[.,](\d{1,2})$", token)
    if tail:
        whole = re.sub(r"\D", "", token[: tail.start()])
        return Decimal(f"{whole or '0'}.{tail.group(1).ljust(2, '0')}")
    return Decimal(re.sub(r"\D", "", token) + ".00")


def amounts_in(text: str) -> list[Decimal]:
    """Money amounts written in `text`, in any of the formats a client might read."""
    return [_to_decimal(m.group(1) or m.group(2)) for m in _AMOUNT.finditer(text)]


def check_reply(
    text: str, priced: PricedOffer | None, base: QuoteTerms, also: set[Decimal] = frozenset()
) -> list[Violation]:
    """Every money amount the agent writes must be one code computed.

    "Sure, £10 all in!" with a real total of £117 is caught here, even if the
    structured offer itself is fine.
    """
    cur = base.currency
    allowed = (priced.amounts() if priced else set()) | {base.total} | set(also)
    allowed = {to_money(a, cur) for a in allowed}
    wrong = [a for a in amounts_in(text) if to_money(a, cur) not in allowed]
    if not wrong:
        return []
    return [
        Violation(
            "wrong_amount_in_reply",
            "malformed",
            f"the reply mentions {', '.join(str(a) for a in wrong)}, which isn't an amount in "
            "the offer; only quote the computed total, deposit and line totals",
        )
    ]


# ── payment plans ────────────────────────────────────────────────────────────


def split_plan(balance: Decimal, proposal: PlanProposal, currency: str, today: date
               ) -> list[tuple[date, Decimal]]:  # fmt: skip
    """Equal instalments; rounding pennies go on the first so the sum is exact."""
    n = proposal.instalments
    each = to_money(balance / n, currency)
    first = balance - each * (n - 1)
    return [
        (today + timedelta(days=proposal.first_due_in_days + i * proposal.every_days),
         first if i == 0 else each)
        for i in range(n)
    ]  # fmt: skip


def check_plan(policy: Policy, balance: Decimal, proposal: PlanProposal) -> list[Violation]:
    out: list[Violation] = []
    n, first, every = proposal.instalments, proposal.first_due_in_days, proposal.every_days
    if n < 2:
        out.append(Violation("bad_plan", "malformed", "a plan needs at least 2 instalments"))
    elif n > policy.max_instalments:
        out.append(Violation("too_many_instalments", "outside_policy",
                             f"at most {policy.max_instalments} instalments"))  # fmt: skip
    if first < 0 or every < 1:
        out.append(Violation("bad_plan", "malformed", "due days must be in the future"))
    elif first + every * (max(n, 1) - 1) > policy.max_plan_days:
        out.append(
            Violation(
                "plan_too_long",
                "outside_policy",
                f"the last instalment must be due within {policy.max_plan_days} days",
            )
        )
    if balance <= 0:
        out.append(Violation("nothing_owed", "malformed", "there is no balance to split"))
    return out
