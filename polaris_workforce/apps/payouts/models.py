"""Bulk-payment exports for bank transfer and mobile money.

* ``PayoutAccount`` — where a worker is paid when their payment method is a transfer
  (one per worker; the company is the worker's).
* ``ExportLayout`` — the columns and formats of a bulk-payment file, per company. Two
  built-in layouts ("Generic bank CSV", "Generic mobile money CSV") are created for each
  company the first time the payout screens are used.
* ``PayoutBatch`` — the transfers of one pay day (optionally one site) for one method,
  per company: draft → exported → partly confirmed → confirmed → closed (or cancelled).
* ``PayoutLine`` — one pay line in a batch: the amount left to pay, a snapshot of the
  account it goes to, its reference, and its status (pending → confirmed / failed).

Every amount is an integer in the currency's smallest unit (``apps.core.money``).
"""

import re

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.utils.translation import gettext_lazy as _

from apps.core.choices import PaymentMethod
from apps.core.models import TimeStampedModel, company_field

# The payment methods paid by a bulk transfer file (cash stays on the cash sheet).
TRANSFER_METHODS = (PaymentMethod.BANK_TRANSFER, PaymentMethod.MOBILE_MONEY)


class TransferMethod(models.TextChoices):
    BANK_TRANSFER = PaymentMethod.BANK_TRANSFER.value, PaymentMethod.BANK_TRANSFER.label
    MOBILE_MONEY = PaymentMethod.MOBILE_MONEY.value, PaymentMethod.MOBILE_MONEY.label


# --- validation helpers -----------------------------------------------------------------

_IBAN_RE = re.compile(r"^[A-Z]{2}[0-9]{2}[A-Z0-9]{8,30}$")
_ACCOUNT_RE = re.compile(r"^[A-Z0-9]{4,34}$")
_PHONE_RE = re.compile(r"^\+?[0-9]{8,15}$")


def compact(value: str) -> str:
    """Account or phone number without spaces, dashes or dots, upper case."""
    return re.sub(r"[\s\-.]", "", value or "").upper()


def looks_like_iban(value: str) -> bool:
    return bool(_IBAN_RE.match(compact(value)))


def iban_is_valid(value: str) -> bool:
    """ISO 13616 check digits (mod 97)."""
    text = compact(value)
    if not _IBAN_RE.match(text):
        return False
    moved = text[4:] + text[:4]
    digits = "".join(str(int(ch, 36)) for ch in moved)
    return int(digits) % 97 == 1


def msisdn(value: str) -> str:
    """A mobile number as digits only (international form without "+"), for files."""
    return re.sub(r"\D", "", value or "")


class PayoutAccount(TimeStampedModel):
    """Where a worker's transfers go. Belongs to the worker's company."""

    worker = models.OneToOneField(
        "workers.Worker",
        verbose_name=_("worker"),
        on_delete=models.CASCADE,
        related_name="payout_account",
    )
    method = models.CharField(
        _("method"),
        max_length=20,
        choices=TransferMethod.choices,
        default=TransferMethod.MOBILE_MONEY,
        help_text=_("Used when the worker's payment method is this one."),
    )
    account_holder = models.CharField(
        _("account holder"),
        max_length=150,
        blank=True,
        help_text=_("Name on the account. Empty: the worker's name."),
    )
    bank_name = models.CharField(_("bank name"), max_length=100, blank=True)
    bank_code = models.CharField(
        _("bank code"), max_length=20, blank=True, help_text=_("Bank / branch code or BIC.")
    )
    account_number = models.CharField(_("account number or IBAN"), max_length=40, blank=True)
    mobile_operator = models.CharField(_("mobile money operator"), max_length=60, blank=True)
    mobile_number = models.CharField(
        _("mobile money number"),
        max_length=20,
        blank=True,
        help_text=_("International form, e.g. +228 90 12 34 56."),
    )

    class Meta:
        verbose_name = _("payout account")
        verbose_name_plural = _("payout accounts")

    def __str__(self) -> str:
        return f"{self.worker} · {self.get_method_display()}"

    def clean(self):
        super().clean()
        errors = {}
        self.account_number = compact(self.account_number)
        self.bank_code = self.bank_code.strip().upper()
        self.mobile_number = (self.mobile_number or "").strip()
        if self.method == TransferMethod.BANK_TRANSFER:
            if not self.bank_name.strip():
                errors["bank_name"] = _("Enter the bank name.")
            if not self.account_number:
                errors["account_number"] = _("Enter the account number or IBAN.")
            elif looks_like_iban(self.account_number) and not iban_is_valid(self.account_number):
                errors["account_number"] = _("This IBAN is not valid (check digits).")
            elif not _ACCOUNT_RE.match(self.account_number):
                errors["account_number"] = _(
                    "Use 4 to 34 letters and digits (spaces and dashes are ignored)."
                )
        if self.method == TransferMethod.MOBILE_MONEY:
            if not self.mobile_operator.strip():
                errors["mobile_operator"] = _("Enter the mobile money operator.")
            if not self.mobile_number:
                errors["mobile_number"] = _("Enter the mobile money number.")
        if self.mobile_number and not _PHONE_RE.match(compact(self.mobile_number)):
            errors["mobile_number"] = _("Use 8 to 15 digits, with the country code.")
        if errors:
            raise ValidationError(errors)

    @property
    def holder(self) -> str:
        return self.account_holder.strip() or self.worker.full_name

    def is_complete_for(self, method: str) -> bool:
        """The account has what a transfer by ``method`` needs."""
        if method != self.method:
            return False
        if method == TransferMethod.BANK_TRANSFER:
            return bool(self.bank_name and self.account_number)
        return bool(self.mobile_operator and self.mobile_number)

    @property
    def masked(self) -> str:
        """The number shown in lists: only the last four characters."""
        number = (
            self.account_number
            if self.method == TransferMethod.BANK_TRANSFER
            else msisdn(self.mobile_number)
        )
        return f"•••• {number[-4:]}" if number else ""


class ExportLayout(TimeStampedModel):
    """The columns and formats of a bulk-payment file (what the bank or operator
    portal expects)."""

    class FileType(models.TextChoices):
        CSV = "csv", _("CSV")
        XLSX = "xlsx", _("Excel (XLSX)")

    class Separator(models.TextChoices):
        COMMA = ",", _("Comma ( , )")
        SEMICOLON = ";", _("Semicolon ( ; )")
        TAB = "\t", _("Tab")
        PIPE = "|", _("Pipe ( | )")

    class DateFormat(models.TextChoices):
        ISO = "%Y-%m-%d", "2026-10-31"
        DMY_SLASH = "%d/%m/%Y", "31/10/2026"
        DMY_DOT = "%d.%m.%Y", "31.10.2026"
        MDY_SLASH = "%m/%d/%Y", "10/31/2026"
        COMPACT = "%Y%m%d", "20261031"

    class AmountFormat(models.TextChoices):
        MAJOR_DOT = "major_dot", _("Major units, dot decimals (1234.50)")
        MAJOR_COMMA = "major_comma", _("Major units, comma decimals (1234,50)")
        MINOR = "minor", _("Smallest unit, no decimals (123450)")

    class Encoding(models.TextChoices):
        UTF8 = "utf-8", _("UTF-8")
        UTF8_BOM = "utf-8-sig", _("UTF-8 with byte-order mark (Excel)")
        WINDOWS = "cp1252", _("Windows-1252 (Latin)")

    company = company_field(related_name="payout_export_layouts")
    name = models.CharField(_("name"), max_length=100)
    method = models.CharField(_("method"), max_length=20, choices=TransferMethod.choices)
    file_type = models.CharField(
        _("file type"), max_length=10, choices=FileType.choices, default=FileType.CSV
    )
    columns = models.JSONField(
        _("columns"),
        default=list,
        help_text=_('In file order: [{"field": "reference", "header": "Reference"}, …].'),
    )
    header_row = models.BooleanField(_("header row"), default=True)
    separator = models.CharField(
        _("separator"), max_length=1, choices=Separator.choices, default=Separator.COMMA
    )
    date_format = models.CharField(
        _("date format"), max_length=20, choices=DateFormat.choices, default=DateFormat.ISO
    )
    amount_format = models.CharField(
        _("amount format"),
        max_length=20,
        choices=AmountFormat.choices,
        default=AmountFormat.MAJOR_DOT,
    )
    encoding = models.CharField(
        _("encoding"), max_length=10, choices=Encoding.choices, default=Encoding.UTF8
    )
    builtin = models.BooleanField(_("built in"), default=False, editable=False)
    is_active = models.BooleanField(_("active"), default=True)

    class Meta:
        verbose_name = _("export layout")
        verbose_name_plural = _("export layouts")
        ordering = ["method", "name"]
        constraints = [
            models.UniqueConstraint(
                fields=["company", "name"], name="unique_payout_layout_name_per_company"
            ),
        ]

    def __str__(self) -> str:
        return self.name


class PayoutBatch(TimeStampedModel):
    """The transfers of one pay day for one method (and optionally one site)."""

    class Status(models.TextChoices):
        DRAFT = "draft", _("Draft")
        EXPORTED = "exported", _("Exported")
        PARTLY_CONFIRMED = "partly_confirmed", _("Partly confirmed")
        CONFIRMED = "confirmed", _("Confirmed")
        CLOSED = "closed", _("Closed")
        CANCELLED = "cancelled", _("Cancelled")

    company = company_field(related_name="payout_batches")
    number = models.PositiveIntegerField(_("number"))
    method = models.CharField(_("method"), max_length=20, choices=TransferMethod.choices)
    pay_date = models.DateField(_("pay day"))
    site = models.ForeignKey(
        "worksites.Site",
        verbose_name=_("site"),
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="payout_batches",
        help_text=_("Empty: every site."),
    )
    layout = models.ForeignKey(
        ExportLayout,
        verbose_name=_("export layout"),
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="batches",
    )
    provider = models.CharField(_("provider"), max_length=30, default="file")
    status = models.CharField(
        _("status"), max_length=20, choices=Status.choices, default=Status.DRAFT
    )
    currency = models.CharField(_("currency"), max_length=3)
    currency_decimals = models.PositiveSmallIntegerField(_("currency decimal places"), default=0)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name=_("created by"),
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
    )
    exported_at = models.DateTimeField(_("exported at"), null=True, blank=True)
    export_count = models.PositiveSmallIntegerField(_("times exported"), default=0)
    closed_at = models.DateTimeField(_("closed at"), null=True, blank=True)

    class Meta:
        verbose_name = _("payout batch")
        verbose_name_plural = _("payout batches")
        ordering = ["-pay_date", "-number"]
        constraints = [
            models.UniqueConstraint(
                fields=["company", "number"], name="unique_payout_batch_number_per_company"
            ),
        ]

    def __str__(self) -> str:
        return self.reference

    @property
    def reference(self) -> str:
        return f"PAY-{self.number:05d}"

    @property
    def is_open(self) -> bool:
        return self.status not in (self.Status.CLOSED, self.Status.CANCELLED)


class PayoutLine(TimeStampedModel):
    """One pay line paid by transfer in a batch."""

    class Status(models.TextChoices):
        PENDING = "pending", _("Waiting for confirmation")
        CONFIRMED = "confirmed", _("Confirmed")
        FAILED = "failed", _("Failed")
        CANCELLED = "cancelled", _("Cancelled")

    batch = models.ForeignKey(
        PayoutBatch, verbose_name=_("batch"), on_delete=models.CASCADE, related_name="lines"
    )
    pay_line = models.ForeignKey(
        "payroll.PayLine",
        verbose_name=_("pay line"),
        on_delete=models.PROTECT,
        related_name="payout_lines",
    )
    amount = models.BigIntegerField(_("amount"))
    reference = models.CharField(_("reference"), max_length=40)
    status = models.CharField(
        _("status"), max_length=20, choices=Status.choices, default=Status.PENDING
    )
    # Snapshot of the account when the batch was prepared.
    method = models.CharField(_("method"), max_length=20, choices=TransferMethod.choices)
    account_holder = models.CharField(_("account holder"), max_length=150, blank=True)
    bank_name = models.CharField(_("bank name"), max_length=100, blank=True)
    bank_code = models.CharField(_("bank code"), max_length=20, blank=True)
    account_number = models.CharField(_("account number or IBAN"), max_length=40, blank=True)
    mobile_operator = models.CharField(_("mobile money operator"), max_length=60, blank=True)
    mobile_number = models.CharField(_("mobile money number"), max_length=20, blank=True)
    # Confirmation.
    provider_reference = models.CharField(
        _("transfer reference"),
        max_length=100,
        blank=True,
        help_text=_("The bank's or operator's transaction reference."),
    )
    payment = models.ForeignKey(
        "payroll.Payment",
        verbose_name=_("payment"),
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="payout_lines",
    )
    failure_reason = models.CharField(_("failure reason"), max_length=300, blank=True)
    resolved_at = models.DateTimeField(_("confirmed or failed at"), null=True, blank=True)
    resolved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name=_("confirmed or failed by"),
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
    )

    class Meta:
        verbose_name = _("payout line")
        verbose_name_plural = _("payout lines")
        ordering = ["batch", "reference"]
        constraints = [
            models.UniqueConstraint(
                fields=["batch", "reference"], name="unique_payout_line_reference_per_batch"
            ),
            # A pay line is in at most one batch at a time (failed / cancelled lines free it).
            models.UniqueConstraint(
                fields=["pay_line"],
                condition=models.Q(status__in=["pending", "confirmed"]),
                name="one_active_payout_line_per_pay_line",
            ),
            models.CheckConstraint(
                condition=models.Q(amount__gt=0), name="payout_line_amount_positive"
            ),
        ]

    def __str__(self) -> str:
        return self.reference

    @property
    def account_display(self) -> str:
        if self.method == TransferMethod.BANK_TRANSFER:
            return " · ".join(p for p in (self.bank_name, self.account_number) if p)
        return " · ".join(p for p in (self.mobile_operator, self.mobile_number) if p)
