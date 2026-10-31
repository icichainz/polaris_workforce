"""Workers and their pay rates."""

from django.core.validators import MinValueValidator
from django.db import models
from django.utils.translation import gettext_lazy as _

from apps.core.choices import PayFrequency, PaymentMethod, PayType
from apps.core.models import TimeStampedModel
from apps.core.validators import UPLOAD_VALIDATORS


class Trade(models.Model):
    """A trade such as mason, carpenter or labourer."""

    name = models.CharField(_("name"), max_length=100, unique=True)

    class Meta:
        verbose_name = _("trade")
        verbose_name_plural = _("trades")
        ordering = ["name"]

    def __str__(self) -> str:
        return self.name


class Worker(TimeStampedModel):
    """Someone on a crew. Workers do not sign in; they are not users."""

    class IdDocumentType(models.TextChoices):
        NATIONAL_ID = "national_id", _("National ID card")
        PASSPORT = "passport", _("Passport")
        VOTER_CARD = "voter_card", _("Voter card")
        NONE = "none", _("None")

    number = models.CharField(
        _("worker number"), max_length=16, unique=True, help_text=_("For example W-0142.")
    )
    first_name = models.CharField(_("first name"), max_length=150)
    last_name = models.CharField(_("last name"), max_length=150)
    phone = models.CharField(_("phone"), max_length=32, blank=True)
    photo = models.FileField(
        _("photo"), upload_to="workers/", null=True, blank=True, validators=UPLOAD_VALIDATORS
    )
    id_document_type = models.CharField(
        _("ID document type"),
        max_length=20,
        choices=IdDocumentType.choices,
        default=IdDocumentType.NONE,
    )
    id_document_number = models.CharField(_("ID document number"), max_length=64, blank=True)
    trade = models.ForeignKey(
        Trade,
        verbose_name=_("trade"),
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="workers",
    )
    is_active = models.BooleanField(_("active"), default=True)

    # Overrides of CompanySettings / Site (see apps.core.settings.WORKER_OVERRIDABLE).
    payment_method = models.CharField(  # noqa: DJ001 (None = inherit)
        _("payment method"), max_length=20, choices=PaymentMethod.choices, null=True, blank=True
    )
    pay_frequency = models.CharField(  # noqa: DJ001 (None = inherit)
        _("pay frequency"), max_length=20, choices=PayFrequency.choices, null=True, blank=True
    )

    class Meta:
        verbose_name = _("worker")
        verbose_name_plural = _("workers")
        ordering = ["last_name", "first_name", "number"]

    def __str__(self) -> str:
        return f"{self.full_name} ({self.number})"

    @property
    def full_name(self) -> str:
        return f"{self.first_name} {self.last_name}".strip()

    def rate_on(self, date) -> "PayRate | None":
        """The pay rate in effect on ``date``: the latest one starting on or before it."""
        return self.pay_rates.filter(effective_from__lte=date).order_by("-effective_from").first()


class PayRate(TimeStampedModel):
    """A worker's rate from ``effective_from`` until the next rate starts."""

    worker = models.ForeignKey(
        Worker, verbose_name=_("worker"), on_delete=models.CASCADE, related_name="pay_rates"
    )
    pay_type = models.CharField(
        _("pay type"), max_length=10, choices=PayType.choices, default=PayType.DAILY
    )
    amount = models.BigIntegerField(
        _("amount"),
        validators=[MinValueValidator(0)],
        help_text=_("Per day or per hour, in the currency's smallest unit."),
    )
    effective_from = models.DateField(_("effective from"))

    class Meta:
        verbose_name = _("pay rate")
        verbose_name_plural = _("pay rates")
        ordering = ["worker", "-effective_from"]
        constraints = [
            models.UniqueConstraint(
                fields=["worker", "effective_from"], name="unique_rate_per_worker_per_date"
            ),
            models.CheckConstraint(
                condition=models.Q(amount__gte=0), name="pay_rate_amount_not_negative"
            ),
        ]

    def __str__(self) -> str:
        pay_type = self.get_pay_type_display()
        return f"{self.worker} · {self.amount} {pay_type} · {self.effective_from}"
