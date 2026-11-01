"""Analytics settings: one row per company (the figures themselves are read live)."""

from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.utils.translation import gettext_lazy as _

from apps.core.models import TimeStampedModel, company_field
from apps.core.tenancy import current_company


class AnalyticsSettings(TimeStampedModel):
    """A company's analytics thresholds, read with ``load()``."""

    company = company_field(related_name="analytics_settings")
    budget_warn_percent = models.PositiveSmallIntegerField(
        _("warn when a site has used (% of its labour budget)"),
        default=90,
        validators=[MinValueValidator(1), MaxValueValidator(100)],
        help_text=_("A site is also at risk when its budget runs out before its planned end date."),
    )

    class Meta:
        verbose_name = _("analytics settings")
        verbose_name_plural = _("analytics settings")
        constraints = [
            models.UniqueConstraint(fields=["company"], name="unique_analytics_settings"),
        ]

    def __str__(self) -> str:
        return str(_("Analytics settings"))

    @classmethod
    def load(cls, company=None) -> "AnalyticsSettings":
        """The settings of ``company`` (a ``Company`` or its id), else of the current
        company. Reading never writes: a company that never saved them gets the
        defaults, unsaved (one query either way)."""
        if company is None:
            company = current_company()
        company_id = getattr(company, "pk", company)
        return cls.objects.filter(company_id=company_id).first() or cls(company_id=company_id)
