"""Money helpers.

Amounts are stored as integers in the currency's smallest unit ("minor units"):
3 500 XOF is ``3500`` (0 decimals), 1 234.50 EUR is ``123450`` (2 decimals).
The currency and its number of decimals come from ``CompanySettings``.
Floats are never accepted.
"""

from decimal import Decimal, InvalidOperation

SYMBOL_POSITIONS = ("prefix", "suffix")

# CompanySettings.number_format → format_money keyword arguments.
NUMBER_FORMATS: dict[str, dict[str, str]] = {
    "space": {"thousands_sep": " ", "decimal_sep": ".", "symbol_position": "suffix"},
    "comma": {"thousands_sep": ",", "decimal_sep": ".", "symbol_position": "suffix"},
    "dot": {"thousands_sep": ".", "decimal_sep": ",", "symbol_position": "prefix"},
}
DEFAULT_NUMBER_FORMAT = "space"


def number_format_options(code: str | None) -> dict[str, str]:
    """The separators and currency position for a number-format code (unknown → default)."""
    return NUMBER_FORMATS.get(code or DEFAULT_NUMBER_FORMAT, NUMBER_FORMATS[DEFAULT_NUMBER_FORMAT])


def _check_decimals(decimals: int) -> None:
    if isinstance(decimals, bool) or not isinstance(decimals, int) or decimals < 0:
        raise ValueError(f"decimals must be a non-negative integer, got {decimals!r}")


def _check_minor(amount: int) -> None:
    if isinstance(amount, bool) or not isinstance(amount, int):
        raise TypeError("minor-unit amounts must be int")


def to_minor(value: Decimal | str | int, decimals: int, *, rounding: str | None = None) -> int:
    """Convert a major-unit amount to an integer of minor units.

    The conversion is exact: a value with more decimal places than the currency allows
    raises ``ValueError`` (``to_minor("12.345", 2)``), unless a ``decimal`` rounding mode
    such as ``ROUND_HALF_UP`` is passed explicitly. Floats and bools raise ``TypeError``.
    """
    _check_decimals(decimals)
    if isinstance(value, bool | float):
        raise TypeError("money values must be Decimal, str or int, never float or bool")
    if isinstance(value, int):
        return value * 10**decimals
    if isinstance(value, str):
        try:
            value = Decimal(value.strip())
        except InvalidOperation as exc:
            raise ValueError(f"not a valid amount: {value!r}") from exc
    if not isinstance(value, Decimal):
        raise TypeError(f"unsupported money value type: {type(value).__name__}")
    if not value.is_finite():
        raise ValueError(f"not a finite amount: {value!r}")

    if rounding is not None:
        value = value.quantize(Decimal(1).scaleb(-decimals), rounding=rounding)

    # Integer arithmetic on the digits, so no Decimal context precision can round.
    sign, digits, exponent = value.as_tuple()
    number = int("".join(map(str, digits)) or "0")
    shift = exponent + decimals
    if shift >= 0:
        number *= 10**shift
    else:
        divisor = 10**-shift
        if number % divisor:
            raise ValueError(f"{value} has more than {decimals} decimal place(s)")
        number //= divisor
    return -number if sign else number


def from_minor(amount: int, decimals: int) -> Decimal:
    """Convert minor units back to a ``Decimal`` in major units (exact)."""
    _check_decimals(decimals)
    _check_minor(amount)
    return Decimal(f"{amount}e-{decimals}")


def format_money(
    amount: int,
    currency: str,
    decimals: int,
    thousands_sep: str = " ",
    decimal_sep: str = ".",
    symbol_position: str = "suffix",
) -> str:
    """Format minor units for display.

    >>> format_money(3500, "XOF", 0)
    '3 500 XOF'
    >>> format_money(123450, "EUR", 2)
    '1 234.50 EUR'
    >>> format_money(-123450, "EUR", 2, symbol_position="prefix")
    'EUR -1 234.50'
    """
    _check_decimals(decimals)
    _check_minor(amount)
    if symbol_position not in SYMBOL_POSITIONS:
        raise ValueError(f"symbol_position must be one of {SYMBOL_POSITIONS}")

    whole, fraction = divmod(abs(amount), 10**decimals)
    number = f"{whole:,}".replace(",", thousands_sep)
    if decimals:
        number = f"{number}{decimal_sep}{fraction:0{decimals}d}"
    if amount < 0:
        number = f"-{number}"
    if not currency:
        return number
    return f"{currency} {number}" if symbol_position == "prefix" else f"{number} {currency}"
