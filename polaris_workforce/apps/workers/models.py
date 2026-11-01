"""Workers, their pay rates and their documents."""

import datetime

from django.conf import settings
from django.core.validators import MinValueValidator
from django.db import models
from django.utils.translation import gettext_lazy as _

from apps.core.choices import PayFrequency, PaymentMethod, PayType
from apps.core.models import TimeStampedModel, company_field
from apps.core.validators import UPLOAD_VALIDATORS

from .search import SOURCE_FIELDS as SEARCH_SOURCE_FIELDS
from .search import search_terms, search_text


class Trade(models.Model):
    """A trade such as mason, carpenter or labourer."""

    company = company_field(related_name="trades")
    name = models.CharField(_("name"), max_length=100)

    class Meta:
        verbose_name = _("trade")
        verbose_name_plural = _("trades")
        ordering = ["name"]
        constraints = [
            models.UniqueConstraint(fields=["company", "name"], name="unique_trade_per_company"),
        ]

    def __str__(self) -> str:
        return self.name


class WorkerQuerySet(models.QuerySet):
    def search(self, text: str):
        """Workers matching every word of ``text`` in their name, number or phone,
        ignoring case and accents (``apps.workers.search``)."""
        workers = self
        for term in search_terms(text):
            workers = workers.filter(search_text__contains=term)
        return workers


class Worker(TimeStampedModel):
    """Someone on a crew. Workers do not sign in; they are not users."""

    class IdDocumentType(models.TextChoices):
        NATIONAL_ID = "national_id", _("National ID card")
        PASSPORT = "passport", _("Passport")
        VOTER_CARD = "voter_card", _("Voter card")
        NONE = "none", _("None")

    company = company_field(related_name="workers")
    number = models.CharField(
        _("worker number"),
        max_length=16,
        help_text=_("For example W-0142. Unique within the company."),
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

    emergency_contact_name = models.CharField(
        _("emergency contact name"), max_length=150, blank=True
    )
    emergency_contact_relationship = models.CharField(
        _("emergency contact relationship"),
        max_length=60,
        blank=True,
        help_text=_("For example spouse, brother, neighbour."),
    )
    emergency_contact_phone = models.CharField(
        _("emergency contact phone"), max_length=32, blank=True
    )

    # Overrides of CompanySettings / Site (see apps.core.settings.WORKER_OVERRIDABLE).
    payment_method = models.CharField(  # noqa: DJ001 (None = inherit)
        _("payment method"), max_length=20, choices=PaymentMethod.choices, null=True, blank=True
    )
    pay_frequency = models.CharField(  # noqa: DJ001 (None = inherit)
        _("pay frequency"), max_length=20, choices=PayFrequency.choices, null=True, blank=True
    )
    # Presence tracking (apps.presence): off keeps the worker's BLE badge out of the
    # tracker's site configs, so their sightings are dropped.
    tracking_enabled = models.BooleanField(_("presence tracking"), null=True, blank=True)

    # Name, number and phone folded for search (apps.workers.search); set by save().
    search_text = models.CharField(_("search text"), max_length=500, blank=True, editable=False)

    objects = WorkerQuerySet.as_manager()

    class Meta:
        verbose_name = _("worker")
        verbose_name_plural = _("workers")
        ordering = ["last_name", "first_name", "number"]
        constraints = [
            models.UniqueConstraint(
                fields=["company", "number"], name="unique_worker_number_per_company"
            ),
        ]

    def __str__(self) -> str:
        return f"{self.full_name} ({self.number})"

    def save(self, *args, update_fields=None, **kwargs):
        self.search_text = search_text(self)
        if update_fields is not None and set(update_fields) & set(SEARCH_SOURCE_FIELDS):
            update_fields = {*update_fields, "search_text"}
        super().save(*args, update_fields=update_fields, **kwargs)

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


# How long before a document expires the profile and the workers list warn about it.
EXPIRY_WARNING_DAYS = 30


class WorkerDocument(models.Model):
    """A contract, ID scan, certificate… kept with the worker (PDF or photo).

    The file is served only through ``workers:document_file`` (``apps.core.files``):
    admins, accountants and site managers who may see the worker.
    """

    class Type(models.TextChoices):
        CONTRACT = "contract", _("Contract")
        ID_SCAN = "id_scan", _("ID scan")
        CERTIFICATE = "certificate", _("Certificate")
        OTHER = "other", _("Other")

    class Expiry(models.TextChoices):
        EXPIRED = "expired", _("Expired")
        EXPIRING = "expiring", _("Expires soon")

    worker = models.ForeignKey(
        Worker, verbose_name=_("worker"), on_delete=models.CASCADE, related_name="documents"
    )
    type = models.CharField(_("type"), max_length=20, choices=Type.choices, default=Type.OTHER)
    title = models.CharField(_("title"), max_length=150)
    file = models.FileField(_("file"), upload_to="worker-documents/", validators=UPLOAD_VALIDATORS)
    issued_on = models.DateField(_("issued on"), null=True, blank=True)
    expires_on = models.DateField(_("expires on"), null=True, blank=True)
    uploaded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name=_("uploaded by"),
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
    )
    uploaded_at = models.DateTimeField(_("uploaded at"), auto_now_add=True)

    class Meta:
        verbose_name = _("worker document")
        verbose_name_plural = _("worker documents")
        ordering = ["worker", "-uploaded_at", "-pk"]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(expires_on__isnull=True)
                | models.Q(issued_on__isnull=True)
                | models.Q(expires_on__gte=models.F("issued_on")),
                name="worker_document_expires_after_issue",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.worker} · {self.title}"

    def expiry(self, today: datetime.date) -> str | None:
        """``expired``, ``expiring`` (within ``EXPIRY_WARNING_DAYS``) or ``None``."""
        if self.expires_on is None:
            return None
        if self.expires_on < today:
            return self.Expiry.EXPIRED
        if self.expires_on <= today + datetime.timedelta(days=EXPIRY_WARNING_DAYS):
            return self.Expiry.EXPIRING
        return None
