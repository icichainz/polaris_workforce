"""Translated labels for pay lines and periods (screens, API, payslip). No business rules."""

import datetime
from decimal import Decimal

from django.utils import formats
from django.utils.translation import gettext as _
from django.utils.translation import ngettext

from apps.core.money import format_money

from .models import PayPeriod


def number(value) -> str:
    """``Decimal("5.50")`` → ``"5.5"``, ``Decimal("1.00")`` → ``"1"``."""
    text = f"{Decimal(value):f}"
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text


def day_units_label(units, status: str | None = None) -> str:
    units = Decimal(units)
    if units == Decimal("0.5"):
        text = _("½ day")
    else:
        text = ngettext("%(n)s day", "%(n)s days", 1 if units <= 1 else 2) % {"n": number(units)}
    if status == "late":
        text = _("%(worked)s · late") % {"worked": text}
    return text


def worked_label(line, decimals: int = 0) -> str:
    """What the line pays for: "1 day", "½ day", "1 day · late", "8 h × 900", "5.5 days"."""
    days = line.days or []
    if len(days) == 1:
        day = days[0]
        if day.get("pay_type") == "hourly" and day.get("hours") is not None:
            return _("%(hours)s h × %(rate)s") % {
                "hours": number(day["hours"]),
                "rate": format_money(int(day["rate"]), "", decimals),
            }
        return day_units_label(day.get("units", line.units), day.get("status"))
    if days and all(d.get("pay_type") == "hourly" for d in days) and line.hours is not None:
        return _("%(hours)s h") % {"hours": number(line.hours)}
    return day_units_label(line.units)


def money(amount: int, currency: str, decimals: int) -> str:
    return format_money(int(amount or 0), currency, decimals)


def short_date(value: datetime.date) -> str:
    """ "Fri 30 Oct"."""
    return formats.date_format(value, "D j M")


def period_label(period: PayPeriod) -> str:
    """ "Fri 30 Oct" for a day, "25 Oct – 31 Oct" for longer periods."""
    if period.start_date == period.end_date:
        return short_date(period.start_date)
    start = formats.date_format(period.start_date, "j M")
    end = formats.date_format(period.end_date, "j M Y")
    return f"{start} – {end}"


def signed_money(amount: int, currency: str, decimals: int) -> str:
    """ "+ 3 000 XOF", "− 6 000 XOF" (the payslip and the cash sheet write deductions so)."""
    sign = "+ " if amount > 0 else "− " if amount < 0 else ""
    return sign + format_money(abs(int(amount)), currency, decimals)


def adjustment_label(adjustment) -> str:
    """ "Correction of 30 Oct" (the days whose correction raised the adjustment)."""
    dates = [datetime.date.fromisoformat(d) for d in adjustment.dates or []]
    if not dates:
        dates = [adjustment.source_line.period.end_date]
    days = ", ".join(formats.date_format(d, "j M") for d in dates)
    return _("Correction of %(days)s") % {"days": days}


def paid_difference_note(difference: int, currency: str, decimals: int) -> str:
    """The warning when a day already paid for is corrected (Approvals, cash sheet)."""
    amount = format_money(abs(int(difference)), currency, decimals)
    if difference < 0:
        return _(
            "Already paid for this day: the difference of %(amount)s will be recovered "
            "on the next pay."
        ) % {"amount": amount}
    return _(
        "Already paid for this day: the difference of %(amount)s will be added to the next pay."
    ) % {"amount": amount}


def raised_note(adjustment) -> str:
    """On the paid line whose day was corrected: where the difference goes."""
    currency, decimals = adjustment.currency, adjustment.currency_decimals
    if adjustment.remaining == 0 and adjustment.applied_to is not None:
        return _(
            "Corrected after payment: the difference of %(amount)s is settled on the pay "
            "of %(date)s."
        ) % {
            "amount": signed_money(adjustment.amount, currency, decimals),
            "date": short_date(adjustment.applied_to.period.end_date),
        }
    return paid_difference_note(adjustment.remaining, currency, decimals)
