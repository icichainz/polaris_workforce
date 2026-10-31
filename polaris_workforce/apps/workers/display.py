"""Turning stored values into display text for the sites and workers screens."""

from django.utils.translation import gettext as _

from apps.core.choices import PayFrequency, PaymentMethod, PayType
from apps.core.models import CompanySettings
from apps.core.money import format_money


class Money:
    """Formats minor units with the company currency: ``Money(company)(6000) == '6 000 XOF'``."""

    def __init__(self, company: CompanySettings):
        self.currency = company.currency
        self.decimals = company.currency_decimals

    def __call__(self, amount: int | None, *, currency: bool = True) -> str:
        if amount is None:
            return "—"
        return format_money(int(amount), self.currency if currency else "", self.decimals)


def initials(worker) -> str:
    return f"{worker.first_name[:1]}{worker.last_name[:1]}".upper()


def pay_unit(pay_type: str) -> str:
    return _("hour") if pay_type == PayType.HOURLY else _("day")


def pay_frequency_text(value: str, every_n_days: int | None = None) -> str:
    if value == PayFrequency.EVERY_N_DAYS and every_n_days:
        return _("Every %(n)s days") % {"n": every_n_days}
    return str(PayFrequency(value).label) if value in PayFrequency.values else "—"


def payment_method_text(value: str) -> str:
    return str(PaymentMethod(value).label) if value in PaymentMethod.values else "—"


def time_text(value) -> str:
    return value.strftime("%H:%M") if value else "—"


def decimal_text(value) -> str:
    """``Decimal('9.00')`` → ``'9'``, ``Decimal('1.50')`` → ``'1.5'``."""
    if value is None:
        return "—"
    text = f"{value.normalize():f}"
    return text
