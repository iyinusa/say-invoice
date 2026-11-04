"""Turn the model's draft into a preview card the tradesperson can trust.

Pure functions, no I/O. The model only *reads* what was said; this module
decides what is missing, prices every line through `app.money`, turns
"due Friday" into a date, and picks the follow-up question. Whatever the model
claims about missing fields, the rules here are the ones that count.
"""

from __future__ import annotations

import re
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from typing import Literal

from email_validator import EmailNotValidError, validate_email

from app.money import line_total, percent_of, plain, subtotal, to_money
from app.schemas.job import Mode, ParsedItem, ParsedJob, ParseResult, PricedItem

RequestedMode = Literal["auto", "invoice", "quote"]

# Currencies PayPal Invoicing accepts. Anything else (e.g. NGN) can be quoted
# but not invoiced, so it is flagged rather than silently swapped.
PAYPAL_CURRENCIES = frozenset(
    "AUD BRL CAD CHF CNY CZK DKK EUR GBP HKD HUF ILS JPY MXN MYR NOK NZD PHP PLN "
    "RUB SEK SGD THB TWD USD".split()
)
_SYMBOLS = {"£": "GBP", "$": "USD", "€": "EUR", "₦": "NGN", "¥": "JPY", "₹": "INR"}
_WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")

# Beyond these a value is more likely misheard than real; flag it, don't block.
_MAX_QTY = Decimal(1000)
_MAX_LINE = Decimal(50_000)
_MAX_DUE_DAYS = 365


def resolve_mode(requested: RequestedMode, detected: Mode) -> Mode:
    return detected if requested == "auto" else requested


def normalise_currency(raw: str, default: str) -> str:
    """An ISO code. Nothing said means the workspace default, which is not a doubt."""
    code = _SYMBOLS.get(raw.strip(), raw.strip().upper())
    return code or default.upper()


def normalise_email(raw: str) -> str | None:
    """A valid address, or None. Never guesses: a bad address goes back as a question."""
    candidate = re.sub(r"\s+", "", raw).strip(".,;").lower()
    if not candidate:
        return None
    try:
        return validate_email(candidate, check_deliverability=False).normalized
    except EmailNotValidError:
        return None


def _decimal(value: float | None) -> Decimal | None:
    if value is None:
        return None
    try:
        d = Decimal(str(value))
    except InvalidOperation:
        return None
    return d if d.is_finite() else None


def resolve_due_date(job: ParsedJob, today: date) -> tuple[date | None, bool]:
    """(due date, uncertain). A stated calendar date wins, then a weekday, then N days."""
    if job.due_date:
        try:
            when = date.fromisoformat(job.due_date)
        except ValueError:
            return None, True
        # Said "the 2nd" on the 20th: the model probably picked the wrong month/year.
        return when, when < today
    if job.due_weekday in _WEEKDAYS:
        # "Due Friday" said on a Friday means next Friday, never today.
        ahead = (_WEEKDAYS.index(job.due_weekday) - today.weekday()) % 7 or 7
        weeks = job.due_weeks_ahead or 0
        if not 0 <= weeks <= 52:
            return None, True
        # "End of next week" said on a Wednesday: this Friday plus a week.
        return today + timedelta(days=ahead + 7 * weeks), False
    if job.due_in_days is not None:
        if 0 <= job.due_in_days <= _MAX_DUE_DAYS:
            return today + timedelta(days=job.due_in_days), False
        return None, True
    return None, False


def _price_items(
    items: list[ParsedItem], currency: str
) -> tuple[list[PricedItem], list[str], list[str]]:
    priced: list[PricedItem] = []
    missing: list[str] = []
    uncertain: list[str] = []
    for raw in items:
        name = raw.name.strip()
        qty = _decimal(raw.quantity)
        price = _decimal(raw.unit_price)
        if not name and not price:
            continue  # an empty row the model padded in
        i = len(priced)
        if qty is None or qty <= 0:
            qty = Decimal(1)
            uncertain.append(f"items.{i}.quantity")
        qty = plain(qty.quantize(Decimal("0.01")))
        if price is None or price <= 0:
            # "plus the valve" with no price: ask, never invent one.
            price = Decimal(0)
            missing.append(f"items.{i}.unit_price")
        unit_price = to_money(price, currency)
        total = line_total(qty, unit_price, currency)
        if raw.approximate:
            uncertain.append(f"items.{i}.unit_price")
        if qty > _MAX_QTY:
            uncertain.append(f"items.{i}.quantity")
        if total > _MAX_LINE:
            uncertain.append(f"items.{i}.unit_price")
        priced.append(
            PricedItem(
                name=name or ("Materials" if raw.is_material else "Labour"),
                description=raw.description.strip(),
                quantity=qty,
                unit_price=unit_price,
                unit=raw.unit,
                is_material=raw.is_material,
                approximate=raw.approximate,
                line_total=total,
            )
        )
        if not name:
            uncertain.append(f"items.{i}.name")
    return priced, missing, uncertain


def _template_question(field: str, client_name: str, items: list[PricedItem]) -> str:
    who = client_name or "the client"
    if field == "items":
        return "What did you do, and how much should I charge for it?"
    if m := re.fullmatch(r"items\.(\d+)\.unit_price", field):
        idx = int(m.group(1))
        name = items[idx].name if idx < len(items) else "that item"
        return f"How much should I charge for the {name.lower()}?"
    if field == "client_email":
        return f"What's {who}'s email address?"
    if field == "client_name":
        return "Who is this quote for?"
    if field == "currency":
        return "Which currency should I use?"
    return f"Can you tell me the {field.replace('_', ' ')}?"


def _dedupe(values: list[str]) -> list[str]:
    return list(dict.fromkeys(v.strip() for v in values if v.strip()))


def finalize(
    job: ParsedJob,
    *,
    requested_mode: RequestedMode,
    default_currency: str,
    today: date,
    heard: bool = False,
) -> ParseResult:
    """`heard`: the job came from audio. Names in emails are easy to mishear
    (Neill/Neil, Shah/Shaw), so a heard email is always highlighted to confirm."""
    mode = resolve_mode(requested_mode, job.mode)
    warnings: list[str] = []
    uncertain = list(job.uncertain_fields)

    currency = normalise_currency(job.currency, default_currency)
    currency_ok = currency in PAYPAL_CURRENCIES
    if not currency_ok:
        warnings.append(f"PayPal can't invoice in {currency}. Pick another currency to send it.")
        uncertain.append("currency")

    items, missing_prices, item_uncertain = _price_items(job.items, currency)
    uncertain += item_uncertain
    total = subtotal(((i.quantity, i.unit_price) for i in items), currency)

    email = normalise_email(job.client_email) if job.client_email.strip() else None
    if job.client_email.strip() and email is None:
        uncertain.append("client_email")
        warnings.append(f"'{job.client_email.strip()}' doesn't look like an email address.")
    elif email and heard:
        uncertain.append("client_email")
    client_name = job.client_name.strip()

    due, due_uncertain = resolve_due_date(job, today)
    if due_uncertain:
        uncertain.append("due_date")

    deposit_pct: Decimal | None = None
    deposit_amount: Decimal | None = None
    pct = _decimal(job.deposit_pct)
    fixed = _decimal(job.deposit_amount)
    if mode == "quote":
        if fixed is not None and fixed > 0 and total > 0:
            deposit_amount = to_money(fixed, currency)
            deposit_pct = (deposit_amount * 100 / total).quantize(Decimal("0.01"))
            if deposit_amount > total:
                uncertain.append("deposit_amount")
        elif pct is not None and 0 < pct <= 100:
            deposit_pct = plain(pct.quantize(Decimal("0.01")))
            deposit_amount = percent_of(total, deposit_pct, currency)
        elif pct is not None or fixed is not None:
            uncertain.append("deposit_pct")
    elif pct or fixed:
        warnings.append("Deposits belong on a quote; this invoice ignores it.")

    # Required fields, most important first. These rules, not the model's list,
    # decide whether the card can be sent.
    missing: list[str] = []
    if not items:
        missing.append("items")
    missing += missing_prices
    if mode == "invoice" and email is None:
        # PayPal delivers the invoice by email; there is no one to send it to.
        missing.append("client_email")
    if mode == "quote" and not client_name and email is None:
        # A quote link can go by text, so an email is optional, but not a name.
        missing.append("client_name")

    question = ""
    if missing:
        top = missing[0]
        model_q = job.follow_up_question.strip()
        # Keep the model's wording (it speaks the client's language) only when
        # it is asking about the same thing the rules want answered first.
        if model_q and top in job.missing_fields:
            question = model_q
        else:
            question = _template_question(top, client_name, items)

    return ParseResult(
        mode=mode,
        ready=not missing and currency_ok,
        transcript=job.transcript.strip(),
        language=job.language or "en",
        client_name=client_name,
        client_email=email or "",
        currency=currency,
        items=items,
        subtotal=total,
        due_date=due,
        deposit_pct=deposit_pct,
        deposit_amount=deposit_amount,
        notes=job.notes.strip(),
        missing_fields=missing,
        uncertain_fields=[f for f in _dedupe(uncertain) if f not in missing],
        follow_up_question=question,
        warnings=warnings,
        draft=job,
    )
