"""Template filters for the payroll screens (formatting goes through ``apps.core.money``).

{% load payroll_tags %}
{{ line.net|money:sheet }}    → "4 000 XOF"  (a CashSheet, PayPeriod or CompanySettings)
{{ line.net|amount:sheet }}   → "4 000"      (no currency code)
{{ line|worked:sheet }}       → "1 day · late", "8 h × 900"
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
