"""Template filters for the payroll screens (formatting goes through ``apps.core.money``).

{% load payroll_tags %}
{{ line.net|money:sheet }}    → "4 000 XOF"  (a CashSheet, PayPeriod or CompanySettings)
{{ line.net|amount:sheet }}   → "4 000"      (no currency code)
{{ line|worked:sheet }}       → "1 day · late", "8 h × 900"
{{ amount|signed:sheet }}     → "− 6 000", "+ 3 000" (no currency code)
{{ adjustment|adjustment_label }} → "Correction of 30 Oct"
{{ adjustment|raised_note }}  → "Already paid for this day: the difference of …"
"""

from django import template

from apps.core.money import format_money

from .. import labels

register = template.Library()


def _currency(source) -> tuple[str, int]:
    if isinstance(source, dict):
        return source.get("code", ""), int(source.get("decimals", 0))
    code = getattr(source, "currency", "") or ""
    decimals = getattr(source, "currency_decimals", 0) or 0
    return code, int(decimals)


@register.filter
def money(value, source):
    code, decimals = _currency(source)
    return format_money(int(value or 0), code, decimals)


@register.filter
def amount(value, source):
    _code, decimals = _currency(source)
    return format_money(int(value or 0), "", decimals)


@register.filter
def worked(line, source):
    return labels.worked_label(line, _currency(source)[1])


@register.filter
def period_label(period):
    return labels.period_label(period)


@register.filter
def initials(worker):
    return "".join(part[:1] for part in (worker.first_name, worker.last_name)).upper()


@register.filter
def signed(value, source):
    _code, decimals = _currency(source)
    return labels.signed_money(int(value or 0), "", decimals)


@register.filter
def adjustment_label(adjustment):
    return labels.adjustment_label(adjustment)


@register.filter
def raised_note(adjustment):
    return labels.raised_note(adjustment)
