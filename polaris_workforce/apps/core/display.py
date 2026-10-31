"""Amounts and quantities written the company's way (currency, decimals, number format).

Python code (views, exports, services) uses these; templates use the ``{% money %}`` and
``{% number %}`` tags from ``apps.core.templatetags.polaris``, which call them.
"""

from decimal import ROUND_HALF_UP, Decimal

from .models import CompanySettings
from .money import format_money, number_format_options

EMPTY = "—"


def format_amount(
    amount: int | None,
    company: CompanySettings | None = None,
    *,
    currency: str | None = None,
    decimals: int | None = None,
    show_currency: bool = True,
) -> str:
    """Format minor units with the company number format.

    ``currency``/``decimals`` default to the company's; pass them for amounts that carry
    their own snapshot (a pay period keeps the currency it was created with).
    ``None`` gives an em dash.
    """
    if amount is None:
        return EMPTY
    if company is None:
        company = CompanySettings.load()
    code = company.currency if currency is None else currency
    places = company.currency_decimals if decimals is None else int(decimals)
    return format_money(
        amount,
        code if show_currency else "",
        places,
        **number_format_options(company.number_format),
    )


def format_quantity(
    value: Decimal | int | None,
    company: CompanySettings | None = None,
    *,
    max_decimals: int = 2,
) -> str:
    """Format a count or a number of days (``Decimal("2412.50")`` → ``"2 412.5"``)."""
    if value is None:
        return EMPTY
    if isinstance(value, bool | float):
        raise TypeError("quantities must be int or Decimal, never float or bool")
    if company is None:
        company = CompanySettings.load()
    options = number_format_options(company.number_format)
    number = Decimal(value).quantize(Decimal(1).scaleb(-max_decimals), rounding=ROUND_HALF_UP)
    sign = "-" if number < 0 else ""
    whole, _, fraction = f"{abs(number):f}".partition(".")
    fraction = fraction.rstrip("0")
    text = f"{int(whole):,}".replace(",", options["thousands_sep"])
    if fraction:
        text = f"{text}{options['decimal_sep']}{fraction}"
    return f"{sign}{text}"
