"""Choices shared by several apps (company settings, site and worker overrides, payroll)."""

from django.db import models
from django.utils.translation import gettext_lazy as _
from django.utils.translation import pgettext_lazy


class Language(models.TextChoices):
    ENGLISH = "en", _("English")
    FRENCH = "fr", _("French")


class AttendanceMode(models.TextChoices):
    FOREMAN_ROLL_CALL = "foreman_roll_call", _("Foreman roll call")
    SELF_CHECK_IN = "self_check_in", _("Self check-in")
    BOTH = "both", _("Both")


class PayFrequency(models.TextChoices):
    DAILY = "daily", _("Daily")
    WEEKLY = "weekly", _("Weekly")
    BIWEEKLY = "biweekly", _("Every 2 weeks")
    MONTHLY = "monthly", _("Monthly")
    EVERY_N_DAYS = "every_n_days", _("Every N days")


class PayType(models.TextChoices):
    DAILY = "daily", _("Daily rate")
    HOURLY = "hourly", _("Hourly rate")
    PIECE = "piece", _("Piece work")


class PaymentMethod(models.TextChoices):
    CASH = "cash", _("Cash")
    BANK_TRANSFER = "bank_transfer", _("Bank transfer")
    MOBILE_MONEY = "mobile_money", _("Mobile money")


class LateRule(models.TextChoices):
    FULL_DAY_FLAGGED = "full_day_flagged", _("Full day, flagged as late")
    DEDUCT_MINUTES = "deduct_minutes", _("Deduct late minutes")


class ProofType(models.TextChoices):
    PHOTO = "photo", _("Photo")
    SIGNATURE = "signature", _("Signature")
    THUMB_MARK = "thumb_mark", _("Thumb mark")
    NONE = "none", _("No proof")


class NumberFormat(models.TextChoices):
    """How amounts are written. The separators live in ``apps.core.money.NUMBER_FORMATS``."""

    SPACE = "space", _("12 500 XOF · 1 234.50 EUR")
    COMMA = "comma", _("12,500 XOF · 1,234.50 EUR")
    DOT = "dot", _("XOF 12.500 · EUR 1.234,50")


# Short weekday names, Monday first, for the working-days checkboxes and calendar headers.
# A context keeps them apart from Django's own "Mon" (the |date "D" abbreviation), which
# French writes in lowercase ("lun").
WEEKDAY_ABBRS = [
    pgettext_lazy("weekday", "Mon"),
    pgettext_lazy("weekday", "Tue"),
    pgettext_lazy("weekday", "Wed"),
    pgettext_lazy("weekday", "Thu"),
    pgettext_lazy("weekday", "Fri"),
    pgettext_lazy("weekday", "Sat"),
    pgettext_lazy("weekday", "Sun"),
]


# The letter shown next to an attendance status word (status is never shown by colour
# alone). Translatable because the words differ: "late" is "retard" in French, so its
# letter is R there. Keys are the attendance record statuses; the one-letter model codes
# (P, A, H, L) are accepted too.
STATUS_LETTERS = {
    "present": pgettext_lazy("attendance status letter (Present)", "P"),
    "absent": pgettext_lazy("attendance status letter (Absent)", "A"),
    "half_day": pgettext_lazy("attendance status letter (Half day)", "½"),
    "late": pgettext_lazy("attendance status letter (Late)", "L"),
}
_STATUS_CODES = {"P": "present", "A": "absent", "H": "half_day", "L": "late"}


def status_letter(status: str) -> str:
    """The translated letter for an attendance status (or its code); "–" if unknown."""
    status = _STATUS_CODES.get(status, status)
    letter = STATUS_LETTERS.get(status)
    return str(letter) if letter is not None else "–"
