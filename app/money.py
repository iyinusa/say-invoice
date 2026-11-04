"""Money arithmetic. The LLM never does sums; everything goes through here.

Amounts are `Decimal` end to end and only become strings at the PayPal
boundary, formatted to the currency's minor units.
"""

from __future__ import annotations

from collections.abc import Iterable
from decimal import ROUND_HALF_UP, Decimal

# PayPal rejects decimals for these currencies; everything else uses two places.
_ZERO_DECIMAL = {"HUF", "JPY", "TWD"}


def quantum(currency: str) -> Decimal:
    return Decimal("1") if currency.upper() in _ZERO_DECIMAL else Decimal("0.01")


def to_money(value: Decimal | int | float | str, currency: str) -> Decimal:
    # `str()` first so a float like 0.1 is read as written, not as its binary expansion.
    return Decimal(str(value)).quantize(quantum(currency), rounding=ROUND_HALF_UP)


def fmt(value: Decimal, currency: str) -> str:
    """The string PayPal expects in an `amount.value` field."""
    return str(to_money(value, currency))


def line_total(qty: Decimal | str, unit_amount: Decimal | str, currency: str) -> Decimal:
    return to_money(Decimal(str(qty)) * Decimal(str(unit_amount)), currency)


def subtotal(lines: Iterable[tuple[Decimal | str, Decimal | str]], currency: str) -> Decimal:
    """Sum of `(qty, unit_amount)` pairs, each line rounded first as PayPal does."""
    return sum((line_total(q, u, currency) for q, u in lines), start=to_money(0, currency))


def plain(value: Decimal) -> Decimal:
    """Drop trailing zeros without exponent notation: 30.00 -> 30, not 3E+1."""
    n = value.normalize()
    return n.quantize(Decimal(1)) if n == n.to_integral_value() else n


def percent_of(amount: Decimal, pct: Decimal | int | str, currency: str) -> Decimal:
    return to_money(amount * Decimal(str(pct)) / Decimal(100), currency)


_SYMBOL = {"GBP": "£", "USD": "$", "EUR": "€", "AUD": "A$", "CAD": "C$", "NZD": "NZ$", "JPY": "¥"}


def display(value: Decimal, currency: str) -> str:
    """For server-rendered pages: £1,234.50. The phone app formats with Intl instead."""
    amount = to_money(value, currency)
    places = 0 if currency.upper() in _ZERO_DECIMAL else 2
    symbol = _SYMBOL.get(currency.upper())
    number = f"{amount:,.{places}f}"
    return f"{symbol}{number}" if symbol else f"{number} {currency.upper()}"
