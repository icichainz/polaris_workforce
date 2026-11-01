"""Message outbox, per-company messaging settings and per-person preferences.

Every row belongs to one company (``apps.core.tenancy``). Messages are written to the
outbox (``Message``) by the payslip signal and the notification rules, and sent later by
``services.send_pending`` through the configured provider (``apps.messaging.providers``).
"""

import datetime

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.utils.translation import gettext_lazy as _

from apps.core.choices import Language
from apps.core.models import TimeStampedModel, company_field
from apps.core.tenancy import current_company


class Channel(models.TextChoices):
    SMS = "sms", _("SMS")
    WHATSAPP = "whatsapp", _("WhatsApp")


class Kind(models.TextChoices):
    """The message types; each has an EN and a FR template in ``templates/messaging/``."""

    PAYSLIP = "payslip", _("Payslip summary")
    MISSING_ROLL_CALL = "missing_roll_call", _("Missing roll call")
    AWAITING_APPROVAL = "awaiting_approval", _("Days waiting for approval")
    UNCLOSED_CASH_SHEET = "unclosed_cash_sheet", _("Cash sheets not closed")
    ADVANCE_LIMIT = "advance_limit", _("Advance near its limit")
    PORTAL_PIN = "portal_pin", _("Worker portal PIN")
    TEST = "test", _("Test message")


class MessagingSettings(TimeStampedModel):
    """A company's messaging rules: one row per company, read with ``load()``."""

    company = company_field(related_name="messaging_settings")
    enabled = models.BooleanField(
        _("send messages"),
        default=False,
        help_text=_("Off: nothing is queued or sent, for any channel."),
    )
    sms_enabled = models.BooleanField(_("SMS"), default=True)
    whatsapp_enabled = models.BooleanField(_("WhatsApp"), default=False)
    default_channel = models.CharField(
        _("default channel"), max_length=10, choices=Channel.choices, default=Channel.SMS
    )
    default_country_code = models.CharField(
        _("default country code"),
        max_length=4,
        blank=True,
        help_text=_(
            "Digits only, e.g. 228. Added to numbers written without one (a leading 0 is dropped)."
        ),
    )
    require_opt_in = models.BooleanField(
        _("only message people who opted in"),
        default=False,
        help_text=_("Off: everyone with a phone number gets messages unless they opted out."),
    )
    quiet_hours_start = models.TimeField(_("quiet hours from"), null=True, blank=True)
    quiet_hours_end = models.TimeField(_("quiet hours until"), null=True, blank=True)

    # Which notifications are on.
    notify_payslip = models.BooleanField(_("payslip summary to workers"), default=True)
    notify_missing_roll_call = models.BooleanField(
        _("missing roll call (foremen and site managers)"), default=True
    )
    notify_awaiting_approval = models.BooleanField(
        _("days waiting for approval (managers)"), default=True
    )
    approval_reminder_hours = models.PositiveSmallIntegerField(
        _("remind about approvals after (hours)"),
        default=2,
        validators=[MaxValueValidator(72)],
    )
    notify_unclosed_cash_sheet = models.BooleanField(
        _("cash sheets not closed after pay day (managers, accountants)"), default=True
    )
    notify_advance_limit = models.BooleanField(
        _("advance near its limit (managers)"), default=False
    )
    advance_limit_warn_percent = models.PositiveSmallIntegerField(
        _("warn at (% of the advance limit)"),
        default=80,
        validators=[MinValueValidator(1), MaxValueValidator(100)],
    )

    class Meta:
        verbose_name = _("messaging settings")
        verbose_name_plural = _("messaging settings")
        constraints = [
            models.UniqueConstraint(fields=["company"], name="unique_messaging_settings"),
        ]

    def __str__(self) -> str:
        return str(_("Messaging settings"))

    def clean(self):
        super().clean()
        errors = {}
        if self.default_country_code and not self.default_country_code.isdigit():
            errors["default_country_code"] = _("Use digits only, e.g. 228.")
        if (self.quiet_hours_start is None) != (self.quiet_hours_end is None):
            errors["quiet_hours_end"] = _("Set both ends of the quiet hours, or neither.")
        if not self.sms_enabled and not self.whatsapp_enabled and self.enabled:
            errors["sms_enabled"] = _("Turn on at least one channel.")
        if errors:
            raise ValidationError(errors)

    @classmethod
    def load(cls, company=None) -> "MessagingSettings":
        """The settings of ``company`` (a ``Company`` or its id), else of the current
        company; created with the defaults if needed."""
        if company is None:
            company = current_company()
        company_id = getattr(company, "pk", company)
        obj, _created = cls.objects.get_or_create(company_id=company_id)
        return obj

    @property
    def channels(self) -> list[str]:
        """The enabled channels, the default one first."""
        enabled = [
            channel
            for channel, on in (
                (Channel.SMS, self.sms_enabled),
                (Channel.WHATSAPP, self.whatsapp_enabled),
            )
            if on
        ]
        enabled.sort(key=lambda channel: channel != self.default_channel)
        return enabled

    def is_on(self, kind: str) -> bool:
        """Is messaging on, and this notification with it?"""
        if not self.enabled or not self.channels:
            return False
        flag = {
            Kind.PAYSLIP: self.notify_payslip,
            Kind.MISSING_ROLL_CALL: self.notify_missing_roll_call,
            Kind.AWAITING_APPROVAL: self.notify_awaiting_approval,
            Kind.UNCLOSED_CASH_SHEET: self.notify_unclosed_cash_sheet,
            Kind.ADVANCE_LIMIT: self.notify_advance_limit,
        }
        return flag.get(kind, True)

    def in_quiet_hours(self, at: datetime.time) -> bool:
        """``at`` is a local time (company time zone)."""
        start, end = self.quiet_hours_start, self.quiet_hours_end
        if start is None or end is None or start == end:
            return False
        if start < end:
            return start <= at < end
        return at >= start or at < end  # overnight, e.g. 21:00 → 06:00


class ContactPreference(models.Model):
    """One person's choice: a user or a worker opted in or out, and their channel."""

    company = company_field(related_name="messaging_preferences")
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name=_("user"),
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name="messaging_preferences",
    )
    worker = models.ForeignKey(
        "workers.Worker",
        verbose_name=_("worker"),
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name="messaging_preferences",
    )
    opted_in = models.BooleanField(_("gets messages"), default=True)
    channel = models.CharField(
        _("channel"),
        max_length=10,
        choices=Channel.choices,
        blank=True,
        help_text=_("Empty: the company's default channel."),
    )
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name=_("changed by"),
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
    )
    updated_at = models.DateTimeField(_("changed at"), auto_now=True)

    class Meta:
        verbose_name = _("messaging preference")
        verbose_name_plural = _("messaging preferences")
        constraints = [
            models.CheckConstraint(
                condition=(
                    models.Q(user__isnull=False, worker__isnull=True)
                    | models.Q(user__isnull=True, worker__isnull=False)
                ),
                name="messaging_preference_one_person",
            ),
            models.UniqueConstraint(
                fields=["company", "user"],
                condition=models.Q(user__isnull=False),
                name="unique_messaging_preference_per_user",
            ),
            models.UniqueConstraint(
                fields=["company", "worker"],
                condition=models.Q(worker__isnull=False),
                name="unique_messaging_preference_per_worker",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.user or self.worker} · {'in' if self.opted_in else 'out'}"


class Message(models.Model):
    """One text in the outbox: queued, then sent (or failed after retries, or skipped)."""

    class Status(models.TextChoices):
        QUEUED = "queued", _("Queued")
        SENT = "sent", _("Sent")
        FAILED = "failed", _("Failed")
        SKIPPED = "skipped", _("Skipped")

    # Not indexed on its own: the dedupe key and outbox indexes start with it.
    company = company_field(related_name="messages", db_index=False)
    channel = models.CharField(
        _("channel"), max_length=10, choices=Channel.choices, default=Channel.SMS
    )
    to = models.CharField(_("to"), max_length=20, help_text=_("E.164, e.g. +22890010101."))
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name=_("user"),
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="messages_received",
    )
    worker = models.ForeignKey(
        "workers.Worker",
        verbose_name=_("worker"),
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="messages_received",
    )
    kind = models.CharField(_("type"), max_length=32, choices=Kind.choices)
    text = models.TextField(_("text"))
    language = models.CharField(
        _("language"), max_length=2, choices=Language.choices, default=Language.ENGLISH
    )
    status = models.CharField(
        _("status"), max_length=10, choices=Status.choices, default=Status.QUEUED
    )
    provider = models.CharField(_("provider"), max_length=64, blank=True)
    provider_id = models.CharField(_("provider message ID"), max_length=128, blank=True)
    attempts = models.PositiveSmallIntegerField(_("attempts"), default=0)
    next_attempt_at = models.DateTimeField(_("next attempt at"), null=True, blank=True)
    error = models.TextField(_("error"), blank=True)
    dedupe_key = models.CharField(
        _("dedupe key"),
        max_length=200,
        help_text=_("The same key is never queued twice in a company."),
    )
    created_at = models.DateTimeField(_("created at"), auto_now_add=True, db_index=True)
    sent_at = models.DateTimeField(_("sent at"), null=True, blank=True)
    updated_at = models.DateTimeField(_("updated at"), auto_now=True)

    class Meta:
        verbose_name = _("message")
        verbose_name_plural = _("messages")
        ordering = ["-created_at", "-id"]
        constraints = [
            models.UniqueConstraint(
                fields=["company", "dedupe_key"], name="unique_message_dedupe_key_per_company"
            ),
        ]
        indexes = [
            models.Index(fields=["company", "status", "next_attempt_at"]),
        ]

    def __str__(self) -> str:
        return f"{self.get_kind_display()} → {self.to} ({self.status})"

    @property
    def recipient_name(self) -> str:
        if self.user_id:
            return self.user.get_full_name() or self.user.username
        if self.worker_id:
            return self.worker.full_name
        return ""
