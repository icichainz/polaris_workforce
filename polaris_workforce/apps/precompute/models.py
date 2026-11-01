"""What the precompute service knows: which sites are out of date, and since when.

- ``Stale``: one row per site whose figures must be rebuilt, with the dates they cover.
  Written in the same transaction as the change (``services.mark``), deleted once the
  figures are rebuilt (``services.refresh_site``).
- ``Freshness``: the company day a site's figures were last rebuilt for. Pay depends on
  the date too (a period is ready once its pay day has come), so a new day makes them
  out of date even when nothing was written.
- ``RefreshRun``: one run of ``manage.py refresh_precomputed``, with what the nightly
  sweep found changed that nobody had marked.
"""

from django.db import models
from django.utils.translation import gettext_lazy as _

from apps.core.models import TimeStampedModel


class Stale(TimeStampedModel):
    """A site whose figures are out of date between ``start`` and ``end``."""

    site = models.OneToOneField(
        "worksites.Site", on_delete=models.CASCADE, related_name="+", verbose_name=_("site")
    )
    start = models.DateField(
        _("from"), null=True, blank=True, help_text=_("Empty: every open pay period.")
    )
    end = models.DateField(_("until"))
    version = models.PositiveIntegerField(
        _("version"), default=1, help_text=_("Moves on every mark (a rebuild keeps newer marks).")
    )
    queued_at = models.DateTimeField(
        _("rebuild queued at"),
        null=True,
        blank=True,
        help_text=_("When a background rebuild was last queued (empty: none)."),
    )

    class Meta:
        verbose_name = _("out-of-date site")
        verbose_name_plural = _("out-of-date sites")

    def __str__(self) -> str:
        return f"{self.site_id}: {self.start or '…'} → {self.end}"


class Freshness(models.Model):
    """The company day the site's figures were last rebuilt for."""

    site = models.OneToOneField(
        "worksites.Site", on_delete=models.CASCADE, related_name="+", verbose_name=_("site")
    )
    fresh_on = models.DateField(_("fresh on"))

    class Meta:
        verbose_name = _("freshness")
        verbose_name_plural = _("freshness")

    def __str__(self) -> str:
        return f"{self.site_id}: {self.fresh_on}"


class RefreshRun(models.Model):
    """One run of ``manage.py refresh_precomputed`` for one company."""

    company = models.ForeignKey(
        "core.Company", on_delete=models.CASCADE, related_name="+", verbose_name=_("company")
    )
    sweep = models.BooleanField(_("full sweep"), default=False)
    started_at = models.DateTimeField(_("started at"), auto_now_add=True)
    finished_at = models.DateTimeField(_("finished at"), null=True, blank=True)
    sites = models.PositiveIntegerField(_("sites rebuilt"), default=0)
    mismatches = models.JSONField(
        _("unmarked changes"),
        default=list,
        blank=True,
        help_text=_("What a sweep had to change although nothing marked it out of date."),
    )

    class Meta:
        verbose_name = _("refresh run")
        verbose_name_plural = _("refresh runs")
        ordering = ["-started_at", "-pk"]

    def __str__(self) -> str:
        return f"{self.company_id} {self.started_at:%Y-%m-%d %H:%M}"


# --- report rollups (apps.dashboard.rollups builds them, apps.dashboard.reports reads) -----
#
# Built per site and month, from the same data and rules as the live report. A month
# whose roll calls and pay periods are all locked is not rebuilt once built: the counts
# cannot change, and each worker keeps the trade they had then.


class ReportMonth(models.Model):
    """The rollups of one site's month are built (the report may read them)."""

    site = models.ForeignKey(
        "worksites.Site", on_delete=models.CASCADE, related_name="+", verbose_name=_("site")
    )
    month = models.DateField(_("month"), help_text=_("Its first day."))
    locked = models.BooleanField(
        _("locked"), default=False, help_text=_("Every roll call and pay period was locked.")
    )
    stale = models.BooleanField(
        _("out of date"),
        default=False,
        help_text=_("A pay period ending in the month changed since: read live until rebuilt."),
    )
    built_at = models.DateTimeField(_("built at"), auto_now=True)

    class Meta:
        verbose_name = _("report month")
        verbose_name_plural = _("report months")
        constraints = [
            models.UniqueConstraint(fields=["site", "month"], name="report_month_unique")
        ]

    def __str__(self) -> str:
        return f"{self.site_id}: {self.month:%Y-%m}"


class WorkerMonth(models.Model):
    """One worker's month on one site: the report's worker row.

    Attendance counts the records of submitted, approved and locked days of the month;
    pay sums the lines of the pay periods that end in the month (amounts in the smallest
    currency unit)."""

    site = models.ForeignKey(
        "worksites.Site", on_delete=models.CASCADE, related_name="+", verbose_name=_("site")
    )
    worker = models.ForeignKey(
        "workers.Worker", on_delete=models.CASCADE, related_name="+", verbose_name=_("worker")
    )
    month = models.DateField(_("month"))
    trade_id = models.BigIntegerField(_("trade"), null=True, blank=True)
    present = models.PositiveIntegerField(_("present"), default=0)
    half = models.PositiveIntegerField(_("half days"), default=0)
    late = models.PositiveIntegerField(_("late"), default=0)
    absent = models.PositiveIntegerField(_("absent"), default=0)
    earned = models.BigIntegerField(_("earned"), default=0)
    statutory = models.BigIntegerField(_("statutory deductions"), default=0)
    employer = models.BigIntegerField(_("employer contributions"), default=0)
    gross = models.BigIntegerField(_("gross"), default=0)

    class Meta:
        verbose_name = _("worker month")
        verbose_name_plural = _("worker months")
        constraints = [
            models.UniqueConstraint(fields=["site", "month", "worker"], name="worker_month_unique")
        ]

    def __str__(self) -> str:
        return f"{self.site_id}/{self.worker_id}: {self.month:%Y-%m}"


class SiteDayCounts(models.Model):
    """One site's reported attendance on one day, per trade (the weekly chart)."""

    site = models.ForeignKey(
        "worksites.Site", on_delete=models.CASCADE, related_name="+", verbose_name=_("site")
    )
    date = models.DateField(_("date"))
    trade_id = models.BigIntegerField(_("trade"), null=True, blank=True)
    present = models.PositiveIntegerField(_("present"), default=0)
    half = models.PositiveIntegerField(_("half days"), default=0)
    late = models.PositiveIntegerField(_("late"), default=0)
    absent = models.PositiveIntegerField(_("absent"), default=0)

    class Meta:
        verbose_name = _("site day counts")
        verbose_name_plural = _("site day counts")
        indexes = [models.Index(fields=["site", "date"], name="site_day_counts_site_date")]

    def __str__(self) -> str:
        return f"{self.site_id}: {self.date}"


class DeductionTotal(models.Model):
    """One statutory deduction rule's amounts on one site's month, per trade (the
    report's employer cost per rule), summed from the pay lines' ``deductions``."""

    site = models.ForeignKey(
        "worksites.Site", on_delete=models.CASCADE, related_name="+", verbose_name=_("site")
    )
    month = models.DateField(_("month"))
    trade_id = models.BigIntegerField(_("trade"), null=True, blank=True)
    rule_key = models.CharField(_("rule"), max_length=120, help_text=_("Its id, else its name."))
    name = models.CharField(_("name"), max_length=100, blank=True)
    employee = models.BigIntegerField(_("employee share"), default=0)
    employer = models.BigIntegerField(_("employer share"), default=0)

    class Meta:
        verbose_name = _("deduction total")
        verbose_name_plural = _("deduction totals")
        indexes = [models.Index(fields=["site", "month"], name="deduction_total_site_month")]

    def __str__(self) -> str:
        return f"{self.site_id}: {self.month:%Y-%m} {self.name}"
