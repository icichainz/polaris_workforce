"""Pay periods, pay lines, advances, payments and cash sheets.

Every amount is an integer in the currency's smallest unit (see ``apps.core.money``).
"""

import uuid
from decimal import Decimal

from django.conf import settings
from django.core.validators import MinValueValidator
from django.db import models
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from apps.core.choices import PayFrequency, PaymentMethod, ProofType
from apps.core.models import CompanySettings, TimeStampedModel
from apps.core.validators import UPLOAD_VALIDATORS

PROOF_UPLOAD_TO = "proofs/%Y/%m/"


def _money_field(verbose_name, **kwargs):
    kwargs.setdefault("validators", [MinValueValidator(0)])
    return models.BigIntegerField(verbose_name, **kwargs)


class PayPeriod(TimeStampedModel):
    """The days a site is paid for together. Currency is snapshotted when created."""

    class Status(models.TextChoices):
        OPEN = "open", _("Open")
        READY = "ready", _("Ready to pay")
        PAYING = "paying", _("Paying")
        PAID = "paid", _("Paid")
        LOCKED = "locked", _("Locked")

    site = models.ForeignKey(
        "worksites.Site",
        verbose_name=_("site"),
        on_delete=models.PROTECT,
        related_name="pay_periods",
    )
    start_date = models.DateField(_("start date"))
    end_date = models.DateField(_("end date"))
    frequency = models.CharField(
        _("frequency"), max_length=20, choices=PayFrequency.choices, default=PayFrequency.DAILY
    )
    status = models.CharField(
        _("status"), max_length=20, choices=Status.choices, default=Status.OPEN
    )
    currency = models.CharField(_("currency"), max_length=3, blank=True)
    currency_decimals = models.PositiveSmallIntegerField(_("currency decimal places"), default=0)
    locked_at = models.DateTimeField(_("locked at"), null=True, blank=True)

    class Meta:
        verbose_name = _("pay period")
        verbose_name_plural = _("pay periods")
        ordering = ["-start_date", "site"]
        constraints = [
            models.UniqueConstraint(
                fields=["site", "start_date", "end_date"], name="unique_pay_period_per_site"
            ),
            models.CheckConstraint(
                condition=models.Q(end_date__gte=models.F("start_date")),
                name="pay_period_end_after_start",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.site} · {self.start_date} → {self.end_date}"

    def save(self, *args, **kwargs):
        # Snapshot the company currency so later settings changes never re-price a period.
        if not self.currency:
            company = CompanySettings.load()
            self.currency = company.currency
            self.currency_decimals = company.currency_decimals
        super().save(*args, **kwargs)

    @property
    def is_locked(self) -> bool:
        return self.status == self.Status.LOCKED


class PayLine(TimeStampedModel):
    """What one worker earns in one pay period."""

    period = models.ForeignKey(
        PayPeriod, verbose_name=_("pay period"), on_delete=models.CASCADE, related_name="lines"
    )
    worker = models.ForeignKey(
        "workers.Worker",
        verbose_name=_("worker"),
        on_delete=models.PROTECT,
        related_name="pay_lines",
    )
    units = models.DecimalField(
        _("days worked"), max_digits=6, decimal_places=2, default=Decimal("0")
    )
    hours = models.DecimalField(_("hours"), max_digits=6, decimal_places=2, null=True, blank=True)
    overtime_hours = models.DecimalField(
        _("overtime hours"), max_digits=6, decimal_places=2, default=Decimal("0")
    )
    gross = _money_field(_("gross"), default=0)
    advance_deduction = _money_field(_("advance deduction"), default=0)
    other_deductions = _money_field(_("other deductions"), default=0)
    net = models.BigIntegerField(_("net"), default=0)

    class Meta:
        verbose_name = _("pay line")
        verbose_name_plural = _("pay lines")
        ordering = ["period", "worker"]
        constraints = [
            models.UniqueConstraint(fields=["period", "worker"], name="unique_pay_line_per_worker"),
        ]

    def __str__(self) -> str:
        return f"{self.worker} · {self.period}"


class Advance(TimeStampedModel):
    """Money given to a worker ahead of pay, repaid by deductions from later pay lines."""

    class Status(models.TextChoices):
        OPEN = "open", _("Open")
        REPAID = "repaid", _("Repaid")

    worker = models.ForeignKey(
        "workers.Worker",
        verbose_name=_("worker"),
        on_delete=models.PROTECT,
        related_name="advances",
    )
    amount = _money_field(_("amount"))
    deduction_per_pay = _money_field(_("deduction per pay"))
    balance = _money_field(_("balance"))
    reason = models.TextField(_("reason"), blank=True)
    given_on = models.DateField(_("given on"), default=timezone.localdate)
    given_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name=_("given by"),
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="advances_given",
    )
    status = models.CharField(
        _("status"), max_length=20, choices=Status.choices, default=Status.OPEN
    )
    proof_type = models.CharField(
        _("proof type"), max_length=20, choices=ProofType.choices, default=ProofType.NONE
    )
    proof_file = models.FileField(
        _("proof file"),
        upload_to=PROOF_UPLOAD_TO,
        null=True,
        blank=True,
        validators=UPLOAD_VALIDATORS,
    )

    class Meta:
        verbose_name = _("advance")
        verbose_name_plural = _("advances")
        ordering = ["-given_on", "-id"]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(amount__gt=0), name="advance_amount_positive"
            ),
            models.CheckConstraint(
                condition=models.Q(balance__gte=0) & models.Q(balance__lte=models.F("amount")),
                name="advance_balance_within_amount",
            ),
            models.CheckConstraint(
                condition=models.Q(deduction_per_pay__gte=0),
                name="advance_deduction_not_negative",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.worker} · {self.amount} ({self.given_on})"


class AdvanceRepayment(models.Model):
    """Part of an advance paid back, usually by a deduction on a pay line."""

    advance = models.ForeignKey(
        Advance, verbose_name=_("advance"), on_delete=models.PROTECT, related_name="repayments"
    )
    pay_line = models.ForeignKey(
        PayLine,
        verbose_name=_("pay line"),
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="advance_repayments",
    )
    amount = _money_field(_("amount"))
    created_at = models.DateTimeField(_("created at"), auto_now_add=True)

    class Meta:
        verbose_name = _("advance repayment")
        verbose_name_plural = _("advance repayments")
        ordering = ["-created_at"]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(amount__gt=0), name="advance_repayment_amount_positive"
            ),
        ]

    def __str__(self) -> str:
        return f"{self.advance} · {self.amount}"


class CashIssue(TimeStampedModel):
    """Cash handed to a site (usually to the foreman) to pay the crew."""

    site = models.ForeignKey(
        "worksites.Site",
        verbose_name=_("site"),
        on_delete=models.PROTECT,
        related_name="cash_issues",
    )
    date = models.DateField(_("date"))
    amount = _money_field(_("amount"))
    issued_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name=_("issued by"),
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="cash_issued",
    )
    received_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name=_("received by"),
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="cash_received",
    )
    note = models.TextField(_("note"), blank=True)

    class Meta:
        verbose_name = _("cash issue")
        verbose_name_plural = _("cash issues")
        ordering = ["-date", "site"]
        indexes = [models.Index(fields=["site", "date"])]

    def __str__(self) -> str:
        return f"{self.site} · {self.date} · {self.amount}"


class Payment(TimeStampedModel):
    """Money actually handed over against a pay line, with proof of receipt."""

    pay_line = models.ForeignKey(
        PayLine, verbose_name=_("pay line"), on_delete=models.PROTECT, related_name="payments"
    )
    method = models.CharField(
        _("method"), max_length=20, choices=PaymentMethod.choices, default=PaymentMethod.CASH
    )
    amount = _money_field(_("amount"))
    paid_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name=_("paid by"),
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="payments_made",
    )
    paid_at = models.DateTimeField(_("paid at"), default=timezone.now)
    proof_type = models.CharField(
        _("proof type"), max_length=20, choices=ProofType.choices, default=ProofType.NONE
    )
    proof_file = models.FileField(
        _("proof file"),
        upload_to=PROOF_UPLOAD_TO,
        null=True,
        blank=True,
        validators=UPLOAD_VALIDATORS,
    )
    client_uuid = models.UUIDField(_("client UUID"), unique=True, default=uuid.uuid4)

    class Meta:
        verbose_name = _("payment")
        verbose_name_plural = _("payments")
        ordering = ["-paid_at"]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(amount__gt=0), name="payment_amount_positive"
            ),
        ]

    def __str__(self) -> str:
        return f"{self.pay_line.worker} · {self.amount} · {self.paid_at:%Y-%m-%d}"
