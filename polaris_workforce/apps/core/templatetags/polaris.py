"""Polaris template tags: money and numbers written the way the company settings say.

Load with ``{% load polaris %}``.

``{% money amount %}``
    Formats an integer amount in the currency's smallest unit with the company currency,
    its decimal places and the company number format:
    ``{% money line.net %}`` → ``3 500 XOF`` (or ``3,500 XOF``, or ``XOF 3.500``).
    ``None`` renders an em dash. Floats raise ``TypeError`` (money is never a float).

``{% money amount period %}``
    Explicit-currency variant for amounts that carry a currency snapshot: the second
    argument is any object with ``currency`` and ``currency_decimals`` (a ``PayPeriod``).
    Locked periods keep the currency they were created with even after the company
    currency changes. The number format still comes from the company settings.

``{% money amount currency="EUR" decimals=2 %}``
    The same, with the code and the decimals given directly.

``{% money amount plain=True %}``
    The number without the currency code, for layouts that show the code separately
    (``stat_tile.html`` with ``suffix=``). Use ``{{ status|status_letter }}``
    The translated attendance status letter (P, A, ½, L; R for "retard" in French).

``{% company_currency %}`` for the code.

``{% money amount as total %}``
    Every tag accepts ``as name`` to store the text instead of printing it, e.g. to pass
    it to an ``{% include ... with value=total %}``.

``{% number value %}``
    A count or a number of days with the company thousands/decimal separators, at most
    two decimals and no trailing zeros: ``Decimal("2412.50")`` → ``2 412.5``.
    ``{% number value max_decimals=1 %}`` to round further.

``{{ status|status_letter }}``
    The translated attendance status letter (P, A, ½, L; R for "retard" in French).

``{% company_currency %}``
    The company currency code ("XOF"), e.g. for ``money_field.html``'s ``currency``.

The company settings are read once per request (cached on the request object) or taken
from a ``company_settings`` context variable when the view already loaded them.
Python code uses the same formatting through ``apps.core.display``.
"""

from django import template

from apps.core.choices import status_letter as _status_letter
from apps.core.display import format_amount, format_quantity
from apps.core.models import CompanySettings

register = template.Library()

_CACHE_ATTR = "_polaris_company_settings"


def company_from_context(context) -> CompanySettings:
    """The company settings for this render, loading them at most once per request."""
    company = context.get("company_settings")
    if isinstance(company, CompanySettings):
        return company
    request = context.get("request")
    holder = request if request is not None else context.render_context
    if request is not None:
        company = getattr(request, _CACHE_ATTR, None)
    else:
        company = holder.get(_CACHE_ATTR)
    if company is None:
        company = CompanySettings.load()
        if request is not None:
            setattr(request, _CACHE_ATTR, company)
        else:
            holder[_CACHE_ATTR] = company
    return company


@register.simple_tag(takes_context=True)
def money(context, amount, source=None, *, currency=None, decimals=None, plain=False):
    if source is not None:
        currency = source.currency if currency is None else currency
        decimals = source.currency_decimals if decimals is None else decimals
    return format_amount(
        amount,
        company_from_context(context),
        currency=currency,
        decimals=decimals,
        show_currency=not plain,
    )


@register.simple_tag(takes_context=True)
def number(context, value, *, max_decimals=2):
    return format_quantity(value, company_from_context(context), max_decimals=int(max_decimals))


@register.simple_tag(takes_context=True)
def company_currency(context):
    return company_from_context(context).currency


@register.filter
def status_letter(status):
    return _status_letter(str(status))
