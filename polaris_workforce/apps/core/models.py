"""Shared building blocks: money, company settings, audit log."""

import datetime
from decimal import Decimal

from django.conf import settings
from django.contrib.contenttypes.fields import GenericForeignKey
from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import ValidationError
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.utils.translation import gettext_lazy as _

from .choices import (
    AttendanceMode,
    Language,
    LateRule,
    PayFrequency,
    PaymentMethod,
    PayType,
    ProofType,
)
from .validators import validate_time_zone


class TimeStampedModel(models.Model):
    """Abstract base with creation and last-change timestamps."""

    created_at = models.DateTimeField(_("created at"), auto_now_add=True)
    updated_at = models.DateTimeField(_("updated at"), auto_now=True)

    class Meta:
        abstract = True


class AuditLog(models.Model):
    """Who changed what, when, and why. Written for every change to attendance or pay."""

    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name=_("actor"),
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="audit_logs",
    )
    action = models.CharField(_("action"), max_length=64)
    content_type = models.ForeignKey(
        ContentType,
        verbose_name=_("target type"),
        null=True,
        blank=True,
        on_delete=models.PROTECT,
    )
    object_id = models.CharField(_("target ID"), max_length=64, blank=True)
    target = GenericForeignKey("content_type", "object_id")
    before = models.JSONField(_("before"), null=True, blank=True)
    after = models.JSONField(_("after"), null=True, blank=True)
    reason = models.TextField(_("reason"), blank=True)
    created_at = models.DateTimeField(_("created at"), auto_now_add=True, db_index=True)

    class Meta:
        verbose_name = _("audit log entry")
        verbose_name_plural = _("audit log")
        ordering = ["-created_at", "-id"]
        indexes = [models.Index(fields=["content_type", "object_id"])]

    def __str__(self) -> str:
        return f"{self.action} · {self.content_type} {self.object_id}".strip()

    @classmethod
    def record(cls, *, actor, action: str, target=None, before=None, after=None, reason=""):
        """Create an entry. ``before``/``after`` must be JSON-serialisable."""
        entry = cls(actor=actor, action=action, before=before, after=after, reason=reason)
        if target is not None:
            entry.content_type = ContentType.objects.get_for_model(target)
            entry.object_id = str(target.pk)
        entry.save()
        return entry


def default_working_days() -> list[int]:
    """ISO weekdays, Monday (1) to Saturday (6)."""
    return [1, 2, 3, 4, 5, 6]


def default_accepted_proofs() -> list[str]:
    return [ProofType.PHOTO, ProofType.SIGNATURE, ProofType.THUMB_MARK]


class CompanySettings(TimeStampedModel):
    """Company-wide defaults. A singleton: there is only ever the row with ``pk=1``.

    Sites and workers can override some of these values; read them through
    ``apps.core.settings.resolve_setting``.
    """

    SINGLETON_PK = 1

    # Company
    company_name = models.CharField(_("company name"), max_length=200, blank=True)
    country = models.CharField(_("country"), max_length=100, blank=True)
    currency = models.CharField(
        _("currency"), max_length=3, default="XOF", help_text=_("ISO 4217 code, e.g. XOF.")
    )
    currency_decimals = models.PositiveSmallIntegerField(
        _("currency decimal places"), default=0, validators=[MaxValueValidator(4)]
    )
    time_zone = models.CharField(
        _("time zone"), max_length=64, default="UTC", validators=[validate_time_zone]
    )
    language = models.CharField(
        _("language"), max_length=2, choices=Language.choices, default=Language.ENGLISH
    )

    # Attendance
    attendance_mode = models.CharField(
        _("attendance mode"),
        max_length=20,
        choices=AttendanceMode.choices,
        default=AttendanceMode.FOREMAN_ROLL_CALL,
    )
    work_start = models.TimeField(_("work starts"), default=datetime.time(7, 0))
    work_end = models.TimeField(_("work ends"), default=datetime.time(17, 0))
    break_minutes = models.PositiveSmallIntegerField(_("break (minutes)"), default=60)
    late_after = models.TimeField(_("late after"), default=datetime.time(7, 15))
    roll_call_due = models.TimeField(_("roll call due by"), default=datetime.time(8, 0))
    half_day_units = models.DecimalField(
        _("half day counts as"),
        max_digits=3,
        decimal_places=2,
        default=Decimal("0.50"),
        validators=[MinValueValidator(Decimal("0")), MaxValueValidator(Decimal("1"))],
    )
    late_rule = models.CharField(
        _("late rule"),
        max_length=20,
        choices=LateRule.choices,
        default=LateRule.FULL_DAY_FLAGGED,
    )
    working_days = models.JSONField(
        _("working days"),
        default=default_working_days,
        help_text=_("ISO weekday numbers: 1 is Monday, 7 is Sunday."),
    )
    require_correction_reason = models.BooleanField(
        _("require a reason for corrections"), default=True
    )

    # Pay
    pay_frequency = models.CharField(
        _("pay frequency"),
        max_length=20,
        choices=PayFrequency.choices,
        default=PayFrequency.DAILY,
    )
    pay_every_n_days = models.PositiveSmallIntegerField(
        _("pay every N days"), null=True, blank=True, validators=[MinValueValidator(1)]
    )
    default_pay_type = models.CharField(
        _("default pay type"), max_length=10, choices=PayType.choices, default=PayType.DAILY
    )
    payment_method = models.CharField(
        _("payment method"),
        max_length=20,
        choices=PaymentMethod.choices,
        default=PaymentMethod.CASH,
    )
    overtime_after_hours = models.DecimalField(
        _("overtime after (hours)"), max_digits=4, decimal_places=2, default=Decimal("9")
    )
    overtime_multiplier = models.DecimalField(
        _("overtime multiplier"), max_digits=4, decimal_places=2, default=Decimal("1.5")
    )
    accepted_proofs = models.JSONField(
        _("accepted proofs of payment"), default=default_accepted_proofs
    )

    # Advances
    advance_limit_percent = models.PositiveSmallIntegerField(
        _("advance limit (% of period pay)"), default=50, validators=[MaxValueValidator(100)]
    )
    default_advance_deduction = models.BigIntegerField(
        _("default advance deduction per pay"),
        default=2000,
        validators=[MinValueValidator(0)],
        help_text=_("In the currency's smallest unit."),
    )

    class Meta:
        verbose_name = _("company settings")
        verbose_name_plural = _("company settings")
        constraints = [
            models.CheckConstraint(condition=models.Q(pk=1), name="company_settings_singleton"),
        ]

    def __str__(self) -> str:
        return str(self.company_name or _("Company settings"))

    def save(self, *args, **kwargs):
        self.pk = self.SINGLETON_PK
        if self._state.adding and self.created_at is None:
            # A fresh instance replaces the existing row: keep its creation time.
            self.created_at = (
                type(self)
                .objects.filter(pk=self.SINGLETON_PK)
                .values_list("created_at", flat=True)
                .first()
            )
        super().save(*args, **kwargs)

    def clean(self):
        super().clean()
        errors = {}
        if self.pay_frequency == PayFrequency.EVERY_N_DAYS and not self.pay_every_n_days:
            errors["pay_every_n_days"] = _("Set the number of days between pay days.")
        if not isinstance(self.working_days, list) or not all(
            isinstance(day, int) and 1 <= day <= 7 for day in self.working_days
        ):
            errors["working_days"] = _("Use ISO weekday numbers from 1 (Monday) to 7 (Sunday).")
        valid_proofs = set(ProofType.values)
        if not isinstance(self.accepted_proofs, list) or not set(self.accepted_proofs) <= (
            valid_proofs
        ):
            errors["accepted_proofs"] = _("Unknown proof type.")
        if errors:
            raise ValidationError(errors)

    @classmethod
    def load(cls) -> "CompanySettings":
        """Return the settings row, creating it with the defaults if needed."""
        obj, _created = cls.objects.get_or_create(pk=cls.SINGLETON_PK)
        return obj
