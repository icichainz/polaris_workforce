"""Presence tracking: zones, gateways, BLE badges, presence events and sessions, consent.

The tracker service (``polaris_tracker/``) turns BLE sightings and the foremen's GPS fixes
into presence events and posts them here; Django keeps the durable records and pushes
each site's tracking config to the tracker (``services.push_site_config``). Design:
``docs/presence-tracking.md``.

Company scoping: badges, events, sessions and consents carry their company; zones,
gateways and the push state belong to a site (their company is the site's, like
``checkin.SiteGeofence``). Every query starts from the scoped querysets of
``apps.accounts.permissions`` or filters on the company.

A *subject* is ``worker:<Worker id>`` or ``foreman:<User id>`` (foremen are users).
"""

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import RegexValidator
from django.db import models
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from apps.core.models import TimeStampedModel, company_field


class SubjectKind(models.TextChoices):
    WORKER = "worker", _("Worker")
    FOREMAN = "foreman", _("Foreman")


ZONE_CODE_VALIDATOR = RegexValidator(
    r"^[A-Za-z0-9][A-Za-z0-9_-]*$",
    _("Use letters, digits, - and _ only, e.g. A, GATE or BLOCK-B."),
)


class Zone(TimeStampedModel):
    """A part of a site that gateways tell apart (the gate, block B…)."""

    site = models.ForeignKey(
        "worksites.Site",
        verbose_name=_("site"),
        on_delete=models.CASCADE,
        related_name="presence_zones",
    )
    code = models.CharField(
        _("code"),
        max_length=32,
        validators=[ZONE_CODE_VALIDATOR],
        help_text=_("Short and unique on the site, e.g. A or GATE. The tracker's zone id."),
    )
    name = models.CharField(_("name"), max_length=100)

    class Meta:
        verbose_name = _("zone")
        verbose_name_plural = _("zones")
        ordering = ["site", "code"]
        constraints = [
            models.UniqueConstraint(fields=["site", "code"], name="unique_zone_code_per_site"),
        ]

    def __str__(self) -> str:
        return f"{self.site} · {self.code} {self.name}".strip()


class Gateway(TimeStampedModel):
    """A fixed BLE gateway on a site, in a zone (or none: site level).

    It sends sightings to the tracker with a long-lived ``gateway`` token
    (``services.issue_gateway_token``). Only gateways with a token that is not revoked are
    in the site's config, and the tracker refuses sightings from any other. Issuing a new
    token changes ``tracker_id``, so an older token stops working too.
    """

    site = models.ForeignKey(
        "worksites.Site",
        verbose_name=_("site"),
        on_delete=models.CASCADE,
        related_name="presence_gateways",
    )
    zone = models.ForeignKey(
        Zone,
        verbose_name=_("zone"),
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="gateways",
    )
    label = models.CharField(_("label"), max_length=100, help_text=_("e.g. “Main gate”."))
    token_generation = models.PositiveIntegerField(_("token generation"), default=0, editable=False)
    token_issued_at = models.DateTimeField(_("token issued at"), null=True, blank=True)
    token_issued_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name=_("token issued by"),
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

    class Meta:
        verbose_name = _("gateway")
        verbose_name_plural = _("gateways")
        ordering = ["site", "label", "pk"]

    def __str__(self) -> str:
        return f"{self.site} · {self.label}"

    def clean(self):
        super().clean()
        if self.zone_id is not None and self.zone.site_id != self.site_id:
            raise ValidationError({"zone": _("Pick a zone of the gateway's site.")})

    @property
    def tracker_id(self) -> str:
        """The gateway's id in the tracker's site config (and in its token's ``sub``)."""
        return f"gw-{self.pk}-{self.token_generation}"

    @property
    def is_active(self) -> bool:
        return self.token_issued_at is not None and self.revoked_at is None


def normalize_badge_id(value: str) -> str:
    """Badge ids compare without case and surrounding spaces: they are stored (and pushed
    to the tracker) trimmed and lower-case, e.g. the phone's iBeacon form
    ``ib:f7826da6-4fa2-4e98-8024-bc5b71e0893e:100:7`` (``ib:<uuid>:<major>:<minor>``)."""
    return (value or "").strip().lower()


class BleBadge(TimeStampedModel):
    """A worker's BLE badge (lanyard or helmet tag), seen by gateways and phones.

    ``badge_id`` is what the badge broadcasts, as the phone's decoder and the gateways
    report it (opaque: the hardware is not chosen yet), normalized by
    ``normalize_badge_id``. One holder at a time per company, and one active badge per
    worker; ending a badge keeps its history.
    """

    company = company_field(related_name="ble_badges", db_index=False)
    badge_id = models.CharField(_("badge ID"), max_length=64)
    worker = models.ForeignKey(
        "workers.Worker",
        verbose_name=_("worker"),
        on_delete=models.CASCADE,
        related_name="ble_badges",
    )
    active_from = models.DateTimeField(_("active from"), default=timezone.now)
    active_to = models.DateTimeField(_("active to"), null=True, blank=True)

    class Meta:
        verbose_name = _("BLE badge")
        verbose_name_plural = _("BLE badges")
        ordering = ["-active_from", "-pk"]
        constraints = [
            models.UniqueConstraint(
                fields=["company", "badge_id"],
                condition=models.Q(active_to__isnull=True),
                name="one_holder_per_ble_badge",
            ),
            models.UniqueConstraint(
                fields=["worker"],
                condition=models.Q(active_to__isnull=True),
                name="one_active_ble_badge_per_worker",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.badge_id} · {self.worker}"

    def save(self, *args, **kwargs):
        self.badge_id = normalize_badge_id(self.badge_id)
        super().save(*args, **kwargs)

    @property
    def is_active(self) -> bool:
        return self.active_to is None


class PresenceEvent(models.Model):
    """An event from the tracker, stored once by ``event_id`` (``services.ingest_events``).

    ``site_ref`` and the subject are what the tracker sent; ``site`` and ``company`` are
    set when the site is known. ``applied`` is false for an unknown site or subject (or
    one of another company): those are kept but build no session.
    """

    class Type(models.TextChoices):
        ENTERED = "entered", _("Entered")
        MOVED_ZONE = "moved_zone", _("Moved zone")
        LEFT = "left", _("Left")

    class Reason(models.TextChoices):
        TIMEOUT = "timeout", _("Not seen for a while")
        LEFT_GEOFENCE = "left_geofence", _("Left the site area")

    event_id = models.UUIDField(_("event ID"), unique=True)
    company = company_field(
        null=True, blank=True, default=None, related_name="presence_events", db_index=False
    )
    site_ref = models.PositiveBigIntegerField(_("site ID sent"))
    site = models.ForeignKey(
        "worksites.Site",
        verbose_name=_("site"),
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="presence_events",
        db_index=False,  # the index below starts with it
    )
    subject_kind = models.CharField(_("subject kind"), max_length=10, choices=SubjectKind)
    subject_id = models.PositiveBigIntegerField(_("subject ID"))
    type = models.CharField(_("type"), max_length=12, choices=Type)
    at = models.DateTimeField(_("at"))
    zone_code = models.CharField(_("zone"), max_length=64, blank=True)
    from_zone_code = models.CharField(_("from zone"), max_length=64, blank=True)
    reason = models.CharField(_("reason"), max_length=16, choices=Reason, blank=True)
    applied = models.BooleanField(_("applied"), default=False)
    received_at = models.DateTimeField(_("received at"), auto_now_add=True)

    class Meta:
        verbose_name = _("presence event")
        verbose_name_plural = _("presence events")
        ordering = ["at", "pk"]
        indexes = [
            models.Index(
                fields=["site", "subject_kind", "subject_id", "at"],
                name="presence_event_subject_at",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.subject_kind}:{self.subject_id} {self.type} @ {self.at:%Y-%m-%d %H:%M}"


class PresenceSession(models.Model):
    """One visit of a person to a site: from ``entered`` to ``left``.

    Rebuilt from the stored events (``services.rebuild_sessions``), never edited by hand,
    so events that arrive late or twice give the same sessions. ``ended_at`` is empty
    while the person is still on site. ``local_date`` is the company-time-zone date of
    ``started_at``: the work day the session suggests attendance for.

    The nightly reconciliation (``reconcile``) replaces a past day's sessions with the
    tracker's replay of its stored track: those are ``source = replay``, and the rebuild
    from events leaves them, and the events of that day, alone from then on.
    """

    class Source(models.TextChoices):
        LIVE = "live", _("Live events")
        REPLAY = "replay", _("Nightly replay")

    company = company_field(related_name="presence_sessions", db_index=False)
    site = models.ForeignKey(
        "worksites.Site",
        verbose_name=_("site"),
        on_delete=models.PROTECT,
        related_name="presence_sessions",
        db_index=False,  # the indexes below start with it
    )
    subject_kind = models.CharField(_("subject kind"), max_length=10, choices=SubjectKind)
    subject_id = models.PositiveBigIntegerField(_("subject ID"))
    local_date = models.DateField(_("date"))
    started_at = models.DateTimeField(_("started at"))
    ended_at = models.DateTimeField(_("ended at"), null=True, blank=True)
    end_reason = models.CharField(_("end reason"), max_length=16, blank=True)
    zones = models.JSONField(
        _("zones visited"), default=list, blank=True, help_text=_("Zone codes, in order.")
    )
    last_event_at = models.DateTimeField(_("last event at"))
    event_count = models.PositiveIntegerField(_("events"), default=0)
    source = models.CharField(
        _("source"), max_length=8, choices=Source, default=Source.LIVE, editable=False
    )

    class Meta:
        verbose_name = _("presence session")
        verbose_name_plural = _("presence sessions")
        ordering = ["site", "local_date", "started_at", "pk"]
        indexes = [
            models.Index(fields=["site", "local_date"], name="presence_session_site_day"),
            models.Index(
                fields=["site", "subject_kind", "subject_id", "started_at"],
                name="presence_session_subject",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.subject_kind}:{self.subject_id} @ {self.site_id} {self.local_date}"

    @property
    def is_open(self) -> bool:
        return self.ended_at is None


class TrackingConsent(models.Model):
    """A foreman's answer to the tracking notice on the phone (accepted or refused).

    The latest answer counts (``selectors.consent_state``). Idempotent by ``client_uuid``.
    """

    company = company_field(related_name="tracking_consents", db_index=False)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name=_("user"),
        on_delete=models.CASCADE,
        related_name="tracking_consents",
        db_index=False,  # the index below starts with it
    )
    accepted = models.BooleanField(_("accepted"))
    text_version = models.CharField(_("notice version"), max_length=32)
    at = models.DateTimeField(_("at"), default=timezone.now)
    client_uuid = models.UUIDField(_("client UUID"), unique=True)

    class Meta:
        verbose_name = _("tracking consent")
        verbose_name_plural = _("tracking consents")
        ordering = ["-at", "-pk"]
        indexes = [models.Index(fields=["user", "at"], name="tracking_consent_user_at")]

    def __str__(self) -> str:
        answer = _("accepted") if self.accepted else _("refused")
        return f"{self.user} · {answer} · {self.text_version}"


class TrackerSync(models.Model):
    """The last push of a site's config to the tracker (``services.push_site_config``).

    The periodic resync pushes every site that is tracked now or was the last time, so
    a site whose tracking was turned off while the tracker was down is turned off there.
    """

    site = models.OneToOneField(
        "worksites.Site",
        verbose_name=_("site"),
        on_delete=models.CASCADE,
        related_name="tracker_sync",
    )
    pushed_enabled = models.BooleanField(_("tracking on at the last push"), default=False)
    last_attempt_at = models.DateTimeField(_("last attempt"), null=True, blank=True)
    last_success_at = models.DateTimeField(_("last success"), null=True, blank=True)
    last_error = models.CharField(_("last error"), max_length=300, blank=True)

    class Meta:
        verbose_name = _("tracker sync")
        verbose_name_plural = _("tracker syncs")

    def __str__(self) -> str:
        return f"{self.site} · {self.last_success_at or '—'}"


class PresenceReconciliation(models.Model):
    """The nightly reconciliation of one site's day with the tracker (``reconcile``).

    ``reconciled_at`` is set once the day's sessions were replaced by the tracker's replay
    (``window_start`` to ``window_end``, the local day in UTC); the counts are those of
    the latest successful run. A failed run keeps ``last_error`` and is tried again the
    next night (or by ``manage.py reconcile_presence``); an earlier success stays valid.
    """

    company = company_field(related_name="presence_reconciliations", db_index=False)
    site = models.ForeignKey(
        "worksites.Site",
        verbose_name=_("site"),
        on_delete=models.CASCADE,
        related_name="presence_reconciliations",
        db_index=False,  # the unique constraint below starts with it
    )
    local_date = models.DateField(_("date"))
    window_start = models.DateTimeField(_("day starts"), null=True, blank=True)
    window_end = models.DateTimeField(_("day ends"), null=True, blank=True)
    reconciled_at = models.DateTimeField(_("reconciled at"), null=True, blank=True)
    last_attempt_at = models.DateTimeField(_("last attempt"), null=True, blank=True)
    last_error = models.CharField(_("last error"), max_length=300, blank=True)
    attempts = models.PositiveIntegerField(_("attempts"), default=0)
    added = models.PositiveIntegerField(_("visits added"), default=0)
    changed = models.PositiveIntegerField(_("visits changed"), default=0)
    removed = models.PositiveIntegerField(_("visits removed"), default=0)
    unchanged = models.PositiveIntegerField(_("visits unchanged"), default=0)
    ignored = models.PositiveIntegerField(
        _("visits ignored"),
        default=0,
        help_text=_("Visits of people the site's company does not know."),
    )

    class Meta:
        verbose_name = _("presence reconciliation")
        verbose_name_plural = _("presence reconciliations")
        ordering = ["-local_date", "site"]
        constraints = [
            models.UniqueConstraint(
                fields=["site", "local_date"], name="one_presence_reconciliation_per_site_day"
            ),
        ]

    def __str__(self) -> str:
        return f"{self.site} · {self.local_date}"

    @property
    def is_reconciled(self) -> bool:
        return self.reconciled_at is not None

    @classmethod
    def replayed_windows(cls, site_id: int, *, after) -> list[tuple]:
        """The site's reconciled days that end after ``after``, as ``(start, end)``: the
        rebuild from events skips them (their sessions come from the replay)."""
        return list(
            cls.objects.filter(site_id=site_id, reconciled_at__isnull=False, window_end__gt=after)
            .order_by("window_start")
            .values_list("window_start", "window_end")
        )
