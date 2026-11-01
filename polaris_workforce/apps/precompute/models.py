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
