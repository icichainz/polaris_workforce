"""QR badges, site geofences, kiosk phones and check-in events.

None of these is a top-level record: a badge belongs to a worker, a geofence and a
check-in to a site, a kiosk to a foreman's device. Their company is their worker's,
site's or user's, so every query starts from the scoped querysets in
``apps.accounts.permissions`` (``scoped_sites``, ``company_workers``).
"""

from django.conf import settings
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from apps.core.models import TimeStampedModel
from apps.core.validators import UploadTypeValidator, validate_file_size

from . import tokens

PHOTO_EXTENSIONS = ["jpg", "jpeg", "png", "webp"]
PHOTO_VALIDATORS = [UploadTypeValidator(allowed_extensions=PHOTO_EXTENSIONS), validate_file_size]

MIN_RADIUS_M = 10
MAX_RADIUS_M = 5000


class Badge(models.Model):
    """A worker's QR badge. One active badge per worker; reissuing revokes the old one."""

    worker = models.ForeignKey(
        "workers.Worker",
        verbose_name=_("worker"),
        on_delete=models.CASCADE,
        related_name="badges",
    )
    public_id = models.CharField(
        _("badge ID"),
        max_length=16,
        unique=True,
        default=tokens.new_public_id,
        editable=False,
        help_text=_("Random; printed in the QR code with its signature. Not the worker ID."),
    )
    issued_at = models.DateTimeField(_("issued at"), default=timezone.now)
    issued_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name=_("issued by"),
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
    )
    revoked_at = models.DateTimeField(_("revoked at"), null=True, blank=True)
    revoked_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name=_("revoked by"),
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
    )
    revoke_reason = models.CharField(_("reason"), max_length=200, blank=True)

    class Meta:
        verbose_name = _("badge")
        verbose_name_plural = _("badges")
        ordering = ["-issued_at", "-pk"]
        constraints = [
            models.UniqueConstraint(
                fields=["worker"],
                condition=models.Q(revoked_at__isnull=True),
                name="one_active_badge_per_worker",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.worker} · {self.public_id}"

    @property
    def is_active(self) -> bool:
        return self.revoked_at is None

    @property
    def payload(self) -> str:
        """The QR text: ``PW1:<public id>.<signature>``."""
        return tokens.make_payload(self.public_id)

    @property
    def payload_sha256(self) -> str:
        return tokens.payload_sha256(self.payload)


class SiteGeofence(TimeStampedModel):
    """A circle around a site: check-ins with GPS outside it are flagged (not refused)."""

    site = models.OneToOneField(
        "worksites.Site",
        verbose_name=_("site"),
        on_delete=models.CASCADE,
        related_name="geofence",
    )
    latitude = models.DecimalField(
        _("latitude"),
        max_digits=9,
        decimal_places=6,
        validators=[MinValueValidator(-90), MaxValueValidator(90)],
    )
    longitude = models.DecimalField(
        _("longitude"),
        max_digits=9,
        decimal_places=6,
        validators=[MinValueValidator(-180), MaxValueValidator(180)],
    )
    radius_m = models.PositiveIntegerField(
        _("radius (metres)"),
        default=150,
        validators=[MinValueValidator(MIN_RADIUS_M), MaxValueValidator(MAX_RADIUS_M)],
    )
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name=_("updated by"),
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
    )

    class Meta:
        verbose_name = _("site geofence")
        verbose_name_plural = _("site geofences")

    def __str__(self) -> str:
        return f"{self.site} · {self.latitude}, {self.longitude} · {self.radius_m} m"


class KioskDevice(TimeStampedModel):
    """A foreman's phone switched to kiosk mode for one site (by an admin or the site's
    manager). Its token may then only create check-ins for that site."""

    device = models.OneToOneField(
        "accounts.ForemanDevice",
        verbose_name=_("device"),
        on_delete=models.CASCADE,
        related_name="kiosk",
    )
    site = models.ForeignKey(
        "worksites.Site",
        verbose_name=_("site"),
        on_delete=models.CASCADE,
        related_name="kiosk_devices",
    )
    enabled_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name=_("enabled by"),
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
    )

    class Meta:
        verbose_name = _("kiosk device")
        verbose_name_plural = _("kiosk devices")
        ordering = ["site", "pk"]

    def __str__(self) -> str:
        return f"{self.device} · {self.site}"


class CheckIn(models.Model):
    """A worker checking in or out on a site: a badge scan (foreman phone or kiosk) or a
    manual entry by the foreman. Evidence for the roll call, which the foreman still
    confirms and submits."""

    class Direction(models.TextChoices):
        IN = "in", _("In")
        OUT = "out", _("Out")

    class Source(models.TextChoices):
        FOREMAN_SCAN = "foreman_scan", _("Badge scan (foreman)")
        KIOSK = "kiosk", _("Kiosk")
        MANUAL = "manual", _("Manual")

    class BadgeProblem(models.TextChoices):
        NONE = "", _("None")
        UNKNOWN = "unknown", _("Unknown badge")
        REVOKED = "revoked", _("Revoked badge")

    site = models.ForeignKey(
        "worksites.Site",
        verbose_name=_("site"),
        on_delete=models.PROTECT,
        related_name="checkins",
    )
    worker = models.ForeignKey(
        "workers.Worker",
        verbose_name=_("worker"),
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="checkins",
        help_text=_("Empty for a badge the server does not know."),
    )
    badge = models.ForeignKey(
        Badge,
        verbose_name=_("badge"),
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="checkins",
    )
    badge_problem = models.CharField(
        _("badge problem"), max_length=10, choices=BadgeProblem.choices, blank=True
    )
    scanned_text = models.CharField(
        _("scanned text"),
        max_length=tokens.MAX_SCAN_LENGTH,
        blank=True,
        help_text=_("What the phone scanned, kept only for a badge the server does not know."),
    )
    date = models.DateField(_("date"), help_text=_("Work day of `at` in the company time zone."))
    direction = models.CharField(_("direction"), max_length=3, choices=Direction.choices)
    at = models.DateTimeField(_("at"), help_text=_("Phone clock when the badge was scanned."))
    received_at = models.DateTimeField(_("received at"), auto_now_add=True)
    source = models.CharField(_("source"), max_length=20, choices=Source.choices)
    device = models.ForeignKey(
        "accounts.ForemanDevice",
        verbose_name=_("device"),
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="checkins",
    )
    recorded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name=_("recorded by"),
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="checkins",
    )
    latitude = models.DecimalField(
        _("latitude"), max_digits=9, decimal_places=6, null=True, blank=True
    )
    longitude = models.DecimalField(
        _("longitude"), max_digits=9, decimal_places=6, null=True, blank=True
    )
    accuracy_m = models.PositiveIntegerField(_("GPS accuracy (metres)"), null=True, blank=True)
    distance_m = models.PositiveIntegerField(
        _("distance from the site (metres)"), null=True, blank=True
    )
    inside_geofence = models.BooleanField(
        _("inside the geofence"),
        null=True,
        blank=True,
        help_text=_("Empty when there was no GPS or the site has no geofence."),
    )
    on_crew = models.BooleanField(
        _("on the crew"), default=True, help_text=_("The worker was on the site's crew that day.")
    )
    draft_updated = models.BooleanField(
        _("updated the draft roll call"),
        default=False,
        help_text=_("Self check-in mode: this check-in wrote the worker's draft record."),
    )
    photo = models.FileField(
        _("photo"), upload_to="checkins/%Y/%m/", blank=True, validators=PHOTO_VALIDATORS
    )
    client_uuid = models.UUIDField(_("client UUID"), unique=True)

    class Meta:
        verbose_name = _("check-in")
        verbose_name_plural = _("check-ins")
        ordering = ["at", "pk"]
        indexes = [
            models.Index(fields=["site", "date"]),
            models.Index(fields=["worker", "date"]),
        ]

    def __str__(self) -> str:
        return f"{self.worker or self.scanned_text} · {self.site} · {self.direction} {self.at}"

    @property
    def counted(self) -> bool:
        """Counts towards the roll call: a known worker, without a badge problem."""
        return self.worker_id is not None and not self.badge_problem
