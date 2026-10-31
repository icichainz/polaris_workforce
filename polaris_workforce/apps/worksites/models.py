"""Construction sites and worker assignments."""

from django.conf import settings
from django.core.validators import MinValueValidator
from django.db import models
from django.utils.translation import gettext_lazy as _

from apps.core.choices import PayFrequency, PaymentMethod
from apps.core.models import TimeStampedModel


class Site(TimeStampedModel):
    """A construction site. Override fields are ``None`` when the company value applies."""

    class Status(models.TextChoices):
        ACTIVE = "active", _("Active")
        PAUSED = "paused", _("Paused")
        COMPLETED = "completed", _("Completed")

    name = models.CharField(_("name"), max_length=200)
    address = models.CharField(_("address"), max_length=255, blank=True)
    status = models.CharField(
        _("status"), max_length=20, choices=Status.choices, default=Status.ACTIVE
    )
    started_on = models.DateField(_("started on"), null=True, blank=True)
    manager = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name=_("site manager"),
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="managed_sites",
    )
    foremen = models.ManyToManyField(
        settings.AUTH_USER_MODEL,
        verbose_name=_("foremen"),
        blank=True,
        related_name="foreman_sites",
    )
    labour_budget = models.BigIntegerField(
        _("labour budget"),
        null=True,
        blank=True,
        validators=[MinValueValidator(0)],
        help_text=_("In the currency's smallest unit."),
    )

    # Overrides of CompanySettings (see apps.core.settings.SITE_OVERRIDABLE).
    pay_frequency = models.CharField(  # noqa: DJ001 (None = inherit)
        _("pay frequency"), max_length=20, choices=PayFrequency.choices, null=True, blank=True
    )
    pay_every_n_days = models.PositiveSmallIntegerField(
        _("pay every N days"), null=True, blank=True, validators=[MinValueValidator(1)]
    )
    payment_method = models.CharField(  # noqa: DJ001 (None = inherit)
        _("payment method"), max_length=20, choices=PaymentMethod.choices, null=True, blank=True
    )
    work_start = models.TimeField(_("work starts"), null=True, blank=True)
    work_end = models.TimeField(_("work ends"), null=True, blank=True)
    late_after = models.TimeField(_("late after"), null=True, blank=True)
    roll_call_due = models.TimeField(_("roll call due by"), null=True, blank=True)
    overtime_after_hours = models.DecimalField(
        _("overtime after (hours)"), max_digits=4, decimal_places=2, null=True, blank=True
    )
    overtime_multiplier = models.DecimalField(
        _("overtime multiplier"), max_digits=4, decimal_places=2, null=True, blank=True
    )

    class Meta:
        verbose_name = _("site")
        verbose_name_plural = _("sites")
        ordering = ["name"]

    def __str__(self) -> str:
        return self.name


class SiteAssignmentQuerySet(models.QuerySet):
    def active_on(self, date):
        """Assignments that cover ``date`` (end date inclusive)."""
        return self.overlapping(date, date)

    def overlapping(self, start, end):
        """Assignments that cover at least one day from ``start`` to ``end`` (inclusive)."""
        return self.filter(start_date__lte=end).filter(
            models.Q(end_date__isnull=True) | models.Q(end_date__gte=start)
        )


class SiteAssignment(TimeStampedModel):
    """A worker is on a site's crew from ``start_date`` to ``end_date`` (inclusive)."""

    worker = models.ForeignKey(
        "workers.Worker",
        verbose_name=_("worker"),
        on_delete=models.CASCADE,
        related_name="assignments",
    )
    site = models.ForeignKey(
        Site, verbose_name=_("site"), on_delete=models.CASCADE, related_name="assignments"
    )
    start_date = models.DateField(_("start date"))
    end_date = models.DateField(_("end date"), null=True, blank=True)

    objects = SiteAssignmentQuerySet.as_manager()

    class Meta:
        verbose_name = _("site assignment")
        verbose_name_plural = _("site assignments")
        ordering = ["-start_date"]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(end_date__isnull=True)
                | models.Q(end_date__gte=models.F("start_date")),
                name="assignment_end_after_start",
            ),
        ]
        indexes = [models.Index(fields=["site", "start_date"])]

    def __str__(self) -> str:
        return f"{self.worker} @ {self.site} ({self.start_date} → {self.end_date or '…'})"
