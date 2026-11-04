from __future__ import annotations

from decimal import Decimal

from app.money import fmt, line_total, percent_of, subtotal, to_money


def test_rounds_half_up_not_bankers() -> None:
    assert to_money("2.675", "GBP") == Decimal("2.68")
    assert to_money("0.125", "USD") == Decimal("0.13")


def test_float_input_is_read_as_written() -> None:
    assert to_money(0.1 + 0.2, "GBP") == Decimal("0.30")


def test_zero_decimal_currency() -> None:
    assert fmt(Decimal("1234.5"), "JPY") == "1235"
    assert fmt(Decimal("60"), "GBP") == "60.00"


def test_subtotal_rounds_each_line_first() -> None:
    lines = [(Decimal("2"), Decimal("60")), (Decimal("1"), Decimal("25"))]
    assert subtotal(lines, "GBP") == Decimal("145.00")
    assert line_total("1.5", "33.33", "GBP") == Decimal("50.00")


def test_percent_of() -> None:
    assert percent_of(Decimal("130"), 30, "GBP") == Decimal("39.00")


def test_plain_never_uses_exponent_notation() -> None:
    from app.money import plain

    assert str(plain(Decimal("30.00"))) == "30"
    assert str(plain(Decimal("100"))) == "100"
    assert str(plain(Decimal("1.50"))) == "1.5"
    assert str(plain(Decimal("0.25"))) == "0.25"
