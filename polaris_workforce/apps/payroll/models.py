"""Pay periods, pay lines, advances, payments and cash sheets.

Every amount is an integer in the currency's smallest unit (see ``apps.core.money``).
"""

import uuid
from decimal import Decimal

from django.conf import settings
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from apps.core.choices import PayFrequency, PaymentMethod, ProofType
from apps.core.models import Company, CompanySettings, TimeStampedModel, company_field
from apps.core.tenancy import current_company
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
            company = CompanySettings.load(self.site.company_id)
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
    adjustment = models.BigIntegerField(
        _("adjustment"),
        default=0,
        help_text=_(
            "Corrections of days already paid, settled on this pay: positive is added, "
            "negative is recovered."
        ),
    )
    net = models.BigIntegerField(_("net"), default=0)
    days = models.JSONField(
        _("days"),
        default=list,
        blank=True,
        help_text=_("What the pay engine computed for each day (kept for the payslip)."),
    )
    # Pay rules (Wave 4): gross = time pay (the days) + piece_amount + bonus_amount.
    piece_amount = _money_field(_("piece work"), default=0)
    bonus_amount = _money_field(_("bonuses"), default=0)
    statutory_deductions = _money_field(
        _("statutory deductions"),
        default=0,
        help_text=_("Employee share of the statutory deduction rules, taken from this pay."),
    )
    employer_contributions = _money_field(
        _("employer contributions"),
        default=0,
        help_text=_("Employer share of the statutory deduction rules (a cost, not deducted)."),
    )
    pieces = models.JSONField(
        _("piece work entries"),
        default=list,
        blank=True,
        help_text=_("The piece work paid on this line (kept for the payslip)."),
    )
    deductions = models.JSONField(
        _("statutory deductions by rule"),
        default=list,
        blank=True,
        help_text=_("Each deduction rule applied: base, employee and employer amounts."),
    )

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


class PayAdjustment(TimeStampedModel):
    """A difference to settle on a later pay: a day was corrected after it was paid.

    ``amount`` is what the corrected attendance is worth minus what was paid (negative:
    the worker was overpaid and it is recovered; positive: underpaid and it is added).
    It is settled on the worker's next pays at the site (``PayAdjustmentApplication``);
    ``applied_amount`` is how much of it is settled so far and ``applied_to`` the last
    pay line that took part of it.
    """

    worker = models.ForeignKey(
        "workers.Worker",
        verbose_name=_("worker"),
        on_delete=models.PROTECT,
        related_name="pay_adjustments",
    )
    site = models.ForeignKey(
        "worksites.Site",
        verbose_name=_("site"),
        on_delete=models.PROTECT,
        related_name="pay_adjustments",
    )
    amount = models.BigIntegerField(_("amount"))
    currency = models.CharField(_("currency"), max_length=3, blank=True)
    currency_decimals = models.PositiveSmallIntegerField(_("currency decimal places"), default=0)
    reason = models.TextField(_("reason"), blank=True)
    dates = models.JSONField(
        _("corrected days"), default=list, blank=True, help_text=_("ISO dates.")
    )
    day = models.ForeignKey(
        "attendance.AttendanceDay",
        verbose_name=_("corrected day"),
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="pay_adjustments",
    )
    record = models.ForeignKey(
        "attendance.AttendanceRecord",
        verbose_name=_("corrected record"),
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="pay_adjustments",
    )
    source_line = models.ForeignKey(
        PayLine,
        verbose_name=_("paid pay line"),
        on_delete=models.PROTECT,
        related_name="raised_adjustments",
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name=_("created by"),
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="pay_adjustments_created",
    )
    applied_to = models.ForeignKey(
        PayLine,
        verbose_name=_("applied to"),
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
    )
    applied_amount = models.BigIntegerField(_("applied amount"), default=0)

    class Meta:
        verbose_name = _("pay adjustment")
        verbose_name_plural = _("pay adjustments")
        ordering = ["created_at", "id"]
        indexes = [models.Index(fields=["worker", "site"])]
        constraints = [
            models.CheckConstraint(
                condition=~models.Q(amount=0), name="pay_adjustment_amount_not_zero"
            ),
        ]

    def __str__(self) -> str:
        return f"{self.worker} · {self.amount} ({', '.join(self.dates)})"

    @property
    def remaining(self) -> int:
        return self.amount - self.applied_amount


class PayAdjustmentApplication(models.Model):
    """Part of a pay adjustment settled on a pay line."""

    adjustment = models.ForeignKey(
        PayAdjustment,
        verbose_name=_("pay adjustment"),
        on_delete=models.CASCADE,
        related_name="applications",
    )
    pay_line = models.ForeignKey(
        PayLine,
        verbose_name=_("pay line"),
        on_delete=models.CASCADE,
        related_name="adjustment_applications",
    )
    amount = models.BigIntegerField(_("amount"))
    created_at = models.DateTimeField(_("created at"), auto_now_add=True)

    class Meta:
        verbose_name = _("pay adjustment application")
        verbose_name_plural = _("pay adjustment applications")
        ordering = ["created_at", "id"]
        constraints = [
            models.UniqueConstraint(
                fields=["adjustment", "pay_line"], name="unique_adjustment_per_pay_line"
            ),
            models.CheckConstraint(
                condition=~models.Q(amount=0), name="pay_adjustment_application_not_zero"
            ),
        ]

    def __str__(self) -> str:
        return f"{self.adjustment} → {self.pay_line} · {self.amount}"


# --- pay rules (Wave 4): multipliers, stoppage days, deductions, piece work, bonuses ------


def _multiplier_field(verbose_name, **kwargs):
    kwargs.setdefault("validators", [MinValueValidator(Decimal("1")), MaxValueValidator(10)])
    return models.DecimalField(verbose_name, max_digits=4, decimal_places=2, **kwargs)


def _percent_field(verbose_name, **kwargs):
    kwargs.setdefault("validators", [MinValueValidator(Decimal("0")), MaxValueValidator(100)])
    return models.DecimalField(verbose_name, max_digits=5, decimal_places=2, **kwargs)


class StoppagePay(models.TextChoices):
    PERCENT = "percent", _("A share of the day's pay")
    FIXED = "fixed", _("A fixed amount per day")


class RulePayType(models.TextChoices):
    """Pay types a deduction rule can be limited to (the rates' types, plus piece work)."""

    DAILY = "daily", _("Daily rate")
    HOURLY = "hourly", _("Hourly rate")
    PIECE = "piece", _("Piece work")


class PayRuleSettings(TimeStampedModel):
    """A company's pay rules beyond ``CompanySettings``: one row per company, read with
    ``PayRuleSettings.load()``. A site may override the multipliers (``SitePayRules``)."""

    company = company_field(related_name="pay_rule_settings")
    sunday_multiplier = _multiplier_field(
        _("Sunday multiplier"),
        default=Decimal("1"),
        help_text=_("Time worked on a Sunday is paid × this (1 = normal pay)."),
    )
    holiday_multiplier = _multiplier_field(
        _("public holiday multiplier"),
        default=Decimal("1"),
        help_text=_("Time worked on a public holiday (see Settings) is paid × this."),
    )
    night_start = models.TimeField(_("night starts"), null=True, blank=True)
    night_end = models.TimeField(_("night ends"), null=True, blank=True)
    night_multiplier = _multiplier_field(
        _("night multiplier"),
        default=Decimal("1"),
        help_text=_("Hours worked between the night times are paid × this."),
    )
    stoppage_pay = models.CharField(
        _("stoppage day pay"),
        max_length=10,
        choices=StoppagePay.choices,
        default=StoppagePay.PERCENT,
    )
    stoppage_percent = _percent_field(
        _("stoppage day share (%)"),
        default=Decimal("50"),
        help_text=_("Workers present on a stoppage day get this share of their day's pay."),
    )
    stoppage_amount = _money_field(
        _("stoppage day amount"),
        default=0,
        help_text=_("Or this fixed amount per worker present."),
    )

    class Meta:
        verbose_name = _("pay rules")
        verbose_name_plural = _("pay rules")
        constraints = [
            models.UniqueConstraint(fields=["company"], name="unique_pay_rules_per_company"),
        ]

    def __str__(self) -> str:
        return str(_("Pay rules · %(company)s") % {"company": self.company})

    @classmethod
    def load(cls, company=None) -> "PayRuleSettings":
        """The pay rules of ``company`` (a ``Company`` or its id), else of the current
        company; created with the defaults (no change to pay) if needed."""
        if company is None:
            company = current_company()
        company_id = company.pk if isinstance(company, Company) else company
        obj, _created = cls.objects.get_or_create(company_id=company_id)
        return obj


class SitePayRules(TimeStampedModel):
    """A site's own multipliers and night window; empty fields inherit the company's."""

    site = models.OneToOneField(
        "worksites.Site",
        verbose_name=_("site"),
        on_delete=models.CASCADE,
        related_name="pay_rules",
    )
    sunday_multiplier = _multiplier_field(_("Sunday multiplier"), null=True, blank=True)
    holiday_multiplier = _multiplier_field(_("public holiday multiplier"), null=True, blank=True)
    night_start = models.TimeField(_("night starts"), null=True, blank=True)
    night_end = models.TimeField(_("night ends"), null=True, blank=True)
    night_multiplier = _multiplier_field(_("night multiplier"), null=True, blank=True)

    OVERRIDES = (
        "sunday_multiplier",
        "holiday_multiplier",
        "night_start",
        "night_end",
        "night_multiplier",
    )

    class Meta:
        verbose_name = _("site pay rules")
        verbose_name_plural = _("site pay rules")

    def __str__(self) -> str:
        return str(self.site)

    @property
    def is_empty(self) -> bool:
        return all(getattr(self, name) is None for name in self.OVERRIDES)


class DeductionRule(TimeStampedModel):
    """A statutory deduction the company configures (no country is built in).

    The employee share is taken from the pay after the gross, before advances and
    adjustments; the employer share is a cost that is reported, never deducted. See
    ``apps.payroll.engine`` ("Pay line", step 3).
    """

    company = company_field(related_name="deduction_rules")
    name = models.CharField(_("name"), max_length=100)
    employee_percent = _percent_field(
        _("employee share (%)"), default=Decimal("0"), help_text=_("Deducted from the pay.")
    )
    employer_percent = _percent_field(
        _("employer share (%)"),
        default=Decimal("0"),
        help_text=_("Paid by the company on top: reported, not deducted."),
    )
    base_floor = models.BigIntegerField(
        _("floor"),
        null=True,
        blank=True,
        validators=[MinValueValidator(0)],
        help_text=_("No deduction on a pay whose gross is under this amount."),
    )
    base_ceiling = models.BigIntegerField(
        _("ceiling"),
        null=True,
        blank=True,
        validators=[MinValueValidator(0)],
        help_text=_("Only the gross up to this amount counts."),
    )
    pay_types = models.JSONField(
        _("pay types"),
        default=list,
        blank=True,
        help_text=_("The pay types it applies to; empty for all."),
    )
    active_from = models.DateField(_("active from"))
    active_to = models.DateField(_("active until"), null=True, blank=True)
    order = models.PositiveSmallIntegerField(
        _("order"), default=10, help_text=_("Rules are applied from the lowest number up.")
    )

    class Meta:
        verbose_name = _("deduction rule")
        verbose_name_plural = _("deduction rules")
        ordering = ["order", "name", "pk"]
        constraints = [
            models.UniqueConstraint(
                fields=["company", "name"], name="unique_deduction_rule_per_company"
            ),
            models.CheckConstraint(
                condition=models.Q(employee_percent__gte=0, employee_percent__lte=100)
                & models.Q(employer_percent__gte=0, employer_percent__lte=100),
                name="deduction_rule_percent_range",
            ),
            models.CheckConstraint(
                condition=models.Q(base_floor__isnull=True)
                | models.Q(base_ceiling__isnull=True)
                | models.Q(base_ceiling__gte=models.F("base_floor")),
                name="deduction_rule_ceiling_above_floor",
            ),
            models.CheckConstraint(
                condition=models.Q(active_to__isnull=True)
                | models.Q(active_to__gte=models.F("active_from")),
                name="deduction_rule_active_range",
            ),
        ]

    def __str__(self) -> str:
        return self.name

    def is_active_on(self, day) -> bool:
        return self.active_from <= day and (self.active_to is None or day <= self.active_to)


class TaskType(TimeStampedModel):
    """A kind of piece work (laying blocks, plastering…) and what one unit of it pays."""

    company = company_field(related_name="task_types")
    name = models.CharField(_("name"), max_length=100)
    unit = models.CharField(_("unit"), max_length=30, help_text=_("For example m², block, m."))
    unit_rate = _money_field(_("rate per unit"))
    is_active = models.BooleanField(_("active"), default=True)

    class Meta:
        verbose_name = _("task type")
        verbose_name_plural = _("task types")
        ordering = ["name", "pk"]
        constraints = [
            models.UniqueConstraint(
                fields=["company", "name"], name="unique_task_type_per_company"
            ),
        ]

    def __str__(self) -> str:
        return f"{self.name} ({self.unit})"


class PieceWorkEntry(TimeStampedModel):
    """Work done by the piece: counted on the pay once the day's roll call is approved."""

    worker = models.ForeignKey(
        "workers.Worker",
        verbose_name=_("worker"),
        on_delete=models.PROTECT,
        related_name="piece_work",
    )
    site = models.ForeignKey(
        "worksites.Site",
        verbose_name=_("site"),
        on_delete=models.PROTECT,
        related_name="piece_work",
    )
    date = models.DateField(_("date"))
    task = models.ForeignKey(
        TaskType, verbose_name=_("task"), on_delete=models.PROTECT, related_name="entries"
    )
    quantity = models.DecimalField(_("quantity"), max_digits=10, decimal_places=2)
    unit_rate = _money_field(
        _("rate per unit"), help_text=_("The task's rate when the entry was made.")
    )
    day = models.ForeignKey(
        "attendance.AttendanceDay",
        verbose_name=_("attendance day"),
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="piece_work",
    )
    entered_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name=_("entered by"),
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="piece_work_entered",
    )
    note = models.CharField(_("note"), max_length=200, blank=True)

    class Meta:
        verbose_name = _("piece work entry")
        verbose_name_plural = _("piece work entries")
        ordering = ["date", "pk"]
        indexes = [models.Index(fields=["site", "date"])]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(quantity__gt=0), name="piece_work_quantity_positive"
            ),
        ]

    def __str__(self) -> str:
        return f"{self.worker} · {self.date} · {self.quantity} {self.task.unit}"


class Bonus(TimeStampedModel):
    """A one-off bonus, added to the worker's next pay at the site (``applied_to``)."""

    worker = models.ForeignKey(
        "workers.Worker",
        verbose_name=_("worker"),
        on_delete=models.PROTECT,
        related_name="bonuses",
    )
    site = models.ForeignKey(
        "worksites.Site",
        verbose_name=_("site"),
        on_delete=models.PROTECT,
        related_name="bonuses",
    )
    amount = _money_field(_("amount"))
    reason = models.CharField(_("reason"), max_length=200)
    date = models.DateField(_("date"))
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name=_("created by"),
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="bonuses_created",
    )
    applied_to = models.ForeignKey(
        PayLine,
        verbose_name=_("paid on"),
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="bonuses",
    )

    class Meta:
        verbose_name = _("bonus")
        verbose_name_plural = _("bonuses")
        ordering = ["date", "pk"]
        indexes = [models.Index(fields=["worker", "site"])]
        constraints = [
            models.CheckConstraint(condition=models.Q(amount__gt=0), name="bonus_amount_positive"),
        ]

    def __str__(self) -> str:
        return f"{self.worker} · {self.amount} ({self.date})"


class StoppageDay(TimeStampedModel):
    """A day the site was stopped (rain, no materials…): workers present get part pay."""

    site = models.ForeignKey(
        "worksites.Site",
        verbose_name=_("site"),
        on_delete=models.PROTECT,
        related_name="stoppage_days",
    )
    date = models.DateField(_("date"))
    reason = models.CharField(_("reason"), max_length=200)
    pay = models.CharField(
        _("pay"), max_length=10, choices=StoppagePay.choices, default=StoppagePay.PERCENT
    )
    percent = _percent_field(_("share of the day (%)"), null=True, blank=True)
    amount = models.BigIntegerField(
        _("fixed amount"), null=True, blank=True, validators=[MinValueValidator(0)]
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name=_("created by"),
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="stoppage_days_created",
    )

    class Meta:
        verbose_name = _("stoppage day")
        verbose_name_plural = _("stoppage days")
        ordering = ["-date", "site"]
        constraints = [
            models.UniqueConstraint(fields=["site", "date"], name="unique_stoppage_per_site_day"),
            models.CheckConstraint(
                condition=(
                    models.Q(pay="percent", percent__isnull=False)
                    | models.Q(pay="fixed", amount__isnull=False)
                ),
                name="stoppage_day_pay_set",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.site} · {self.date} · {self.reason}"
