from decimal import ROUND_HALF_EVEN, ROUND_HALF_UP, Decimal

import pytest

from apps.core.money import format_money, from_minor, to_minor


class TestToMinor:
    @pytest.mark.parametrize(
        ("value", "decimals", "expected"),
        [
            (3500, 0, 3500),
            ("3500", 0, 3500),
            (Decimal("3500"), 0, 3500),
            ("3500.00", 0, 3500),  # trailing zeros fit
            ("1234.5", 2, 123450),
            (Decimal("1234.50"), 2, 123450),
            (12, 2, 1200),
            ("-7.25", 2, -725),
            (" 0.01 ", 2, 1),
            ("0", 3, 0),
            ("1E+3", 0, 1000),
            ("123456789012345678.99", 2, 12345678901234567899),
        ],
    )
    def test_exact_conversions(self, value, decimals, expected):
        assert to_minor(value, decimals) == expected

    @pytest.mark.parametrize(
        ("value", "decimals"),
        [("3500.5", 0), ("12.345", 2), (Decimal("0.001"), 2), ("1.005", 2)],
    )
    def test_rejects_values_that_do_not_fit(self, value, decimals):
        with pytest.raises(ValueError):
            to_minor(value, decimals)

    @pytest.mark.parametrize(
        ("value", "decimals", "rounding", "expected"),
        [
            ("1.005", 2, ROUND_HALF_UP, 101),
            ("1.005", 2, ROUND_HALF_EVEN, 100),
            ("3500.5", 0, ROUND_HALF_UP, 3501),
            ("-3500.5", 0, ROUND_HALF_UP, -3501),
            ("1750.25", 0, ROUND_HALF_UP, 1750),
        ],
    )
    def test_explicit_rounding(self, value, decimals, rounding, expected):
        assert to_minor(value, decimals, rounding=rounding) == expected

    @pytest.mark.parametrize("value", [3.5, True])
    def test_rejects_float_and_bool(self, value):
        with pytest.raises(TypeError):
            to_minor(value, 2)

    @pytest.mark.parametrize("value", ["abc", "", "NaN", "Infinity"])
    def test_rejects_invalid_strings(self, value):
        with pytest.raises(ValueError):
            to_minor(value, 2)

    def test_rejects_negative_decimals(self):
        with pytest.raises(ValueError):
            to_minor("1", -1)


class TestFromMinor:
    def test_zero_decimals(self):
        assert from_minor(3500, 0) == Decimal("3500")

    def test_two_decimals_keeps_the_scale(self):
        result = from_minor(123450, 2)
        assert result == Decimal("1234.50")
        assert str(result) == "1234.50"

    def test_round_trip(self):
        for amount in (0, 1, -1, 99, 123450, 10**18):
            assert to_minor(from_minor(amount, 2), 2) == amount

    def test_rejects_non_int(self):
        with pytest.raises(TypeError):
            from_minor(Decimal("1"), 2)


class TestFormatMoney:
    @pytest.mark.parametrize(
        ("amount", "currency", "decimals", "expected"),
        [
            (3500, "XOF", 0, "3 500 XOF"),
            (0, "XOF", 0, "0 XOF"),
            (999, "XOF", 0, "999 XOF"),
            (1_250_000, "XOF", 0, "1 250 000 XOF"),
            (-3500, "XOF", 0, "-3 500 XOF"),
            (123450, "EUR", 2, "1 234.50 EUR"),
            (5, "EUR", 2, "0.05 EUR"),
            (0, "EUR", 2, "0.00 EUR"),
            (-123450, "EUR", 2, "-1 234.50 EUR"),
        ],
    )
    def test_defaults(self, amount, currency, decimals, expected):
        assert format_money(amount, currency, decimals) == expected

    def test_custom_separators_and_prefix(self):
        assert (
            format_money(
                123450, "€", 2, thousands_sep=".", decimal_sep=",", symbol_position="prefix"
            )
            == "€ 1.234,50"
        )

    def test_without_currency(self):
        assert format_money(3500, "", 0) == "3 500"

    def test_rejects_unknown_symbol_position(self):
        with pytest.raises(ValueError):
            format_money(1, "XOF", 0, symbol_position="middle")

    def test_rejects_float(self):
        with pytest.raises(TypeError):
            format_money(35.0, "XOF", 0)
