"""Presence tracking: tokens for the tracker, consent, zones / gateways / badges, event
ingest and sessions, and the site configs pushed to the tracker.

Audit (rule 6 and ``docs/presence-tracking.md`` §7): consent answers, zones, gateways and
badges write ``presence.*`` entries; tracking settings are audited where they are edited
(``settings.update``, ``worksites.settings_update``, ``workers.update``). Presence events
are not attendance changes and write none.

Site configs: every change that alters what the tracker must know queues a push of the
site's config once the change commits (``queue_site_push``): changes made here directly,
the others from their audit entry (``signals``). A periodic task pushes them all again.
"""

from __future__ import annotations

import datetime
import json
import logging
import threading
import urllib.error
import urllib.request
from collections.abc import Iterable
from dataclasses import dataclass

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.db.models import Min
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from apps.accounts.permissions import is_admin
from apps.api.idempotency import IdempotentResult, run_once
from apps.core.dates import company_tz
from apps.core.models import AuditLog, CompanySettings
from apps.core.settings import TRACKING_SETTINGS, resolve_setting
from apps.core.tenancy import company_id_of
from apps.workers.models import Worker
from apps.worksites.models import Site

from . import engine, selectors, tokens
from .models import (
    BleBadge,
    Gateway,
    PresenceEvent,
    PresenceReconciliation,
    PresenceSession,
    SubjectKind,
    TrackerSync,
    TrackingConsent,
    Zone,
    normalize_badge_id,
)

logger = logging.getLogger(__name__)

# --- tokens for the tracker -------------------------------------------------------------


class TrackingRefused(Exception):
    """No tracker token for this user. ``code``: ``consent_required`` or
    ``tracking_disabled``; ``reason`` (for ``tracking_disabled``): ``settings`` or
    ``consent_refused``."""

    def __init__(self, code: str, message, *, details: dict | None = None):
        super().__init__(str(message))
        self.code = code
        self.message = message
        self.details = details or {}


@dataclass(frozen=True)
class TrackingTerms:
    """What tracking means for this user, over their tracked sites (company time zone):
    the shortest GPS interval, the earliest start and the latest end of the tracking
    hours, and how long raw positions are kept. The phone's notice shows them."""

    gps_interval_seconds: int
    tracking_start: datetime.time
    tracking_end: datetime.time
    retention_days: int

    def as_details(self) -> dict:
        return {
            "tracking_hours": {
                "start": self.tracking_start.strftime("%H:%M"),
                "end": self.tracking_end.strftime("%H:%M"),
            },
            "gps_interval_seconds": self.gps_interval_seconds,
            "retention_days": self.retention_days,
        }


def tracking_terms(sites, company: CompanySettings) -> TrackingTerms:
    def setting(name):
        return [resolve_setting(name, site=site, company=company) for site in sites]

    return TrackingTerms(
        gps_interval_seconds=min(setting("foreman_gps_interval_seconds")),
        tracking_start=min(setting("tracking_start")),
        tracking_end=max(setting("tracking_end")),
        retention_days=company.track_retention_days,
    )


@dataclass(frozen=True)
class DeviceTracking:
    """What the phone needs to track: its token and the resolved settings."""

    token: str
    expires_at: datetime.datetime
    tracker_url: str
    sites: list[int]
    terms: TrackingTerms


def _expiry(issued: tokens.IssuedToken) -> datetime.datetime:
    return datetime.datetime.fromtimestamp(issued.expires_at, tz=datetime.UTC)


def device_tracking(user) -> DeviceTracking:
    """A ``device`` token for a foreman's phone, or ``TrackingRefused``.

    Sites: the user's sites where ``tracking_enabled`` resolves on. A foreman is a user,
    not a worker, so no worker-level switch applies: their own switch is their answer to
    the notice. Off on every site → ``tracking_disabled`` (``reason: settings``) before any
    consent is asked; latest answer a refusal → ``tracking_disabled``
    (``reason: consent_refused``); never answered, or only an older notice accepted →
    ``consent_required``. Both consent refusals carry the notice version and the
    ``TrackingTerms`` in their details, for the phone's notice.
    """
    company_id = _company_or_refuse(user)
    sites = selectors.tracked_sites(user)
    if not sites:
        raise TrackingRefused(
            "tracking_disabled",
            _("Tracking is not enabled for you."),
            details={"reason": "settings"},
        )
    company = CompanySettings.load(company_id)
    terms = tracking_terms(sites, company)
    notice = {"text_version": selectors.current_text_version(), **terms.as_details()}
    state = selectors.consent_state(user)
    if state.refused:
        raise TrackingRefused(
            "tracking_disabled",
            _("You declined tracking. Accept the notice to turn it on."),
            details={"reason": "consent_refused", **notice},
        )
    if not state.accepted:
        raise TrackingRefused(
            "consent_required", _("Accept the tracking notice first."), details=notice
        )
    issued = tokens.mint(
        kind=tokens.DEVICE,
        sub=f"foreman:{user.pk}",
        company=company_id,
        sites=[site.pk for site in sites],
        travel=is_admin(user),
        lifetime=tokens.DEVICE_LIFETIME,
    )
    return DeviceTracking(
        token=issued.token,
        expires_at=_expiry(issued),
        tracker_url=settings.TRACKER_PUBLIC_URL,
        sites=issued.claims["sites"],
        terms=terms,
    )


def _company_or_refuse(user) -> int:
    """The company ``user`` works on (``company_id_of``: a platform admin's is the one
    they picked), else ``PermissionDenied`` (``403 permission_denied`` on the API, with
    its usual message)."""
    company_id = company_id_of(user)
    if company_id is None:
        raise PermissionDenied("Tracking needs a company.")
    return company_id


def mint_dashboard_token(user) -> tuple[str, datetime.datetime]:
    """A ``dashboard`` token (1 hour) for an office page: the user's tracked sites;
    ``travel`` for company admins. For brief P4 (rendered into the page)."""
    company_id = company_id_of(user)
    if company_id is None:
        raise PermissionError("Only users of a company get a dashboard token.")
    issued = tokens.mint(
        kind=tokens.DASHBOARD,
        sub=f"user:{user.pk}",
        company=company_id,
        sites=[site.pk for site in selectors.tracked_sites(user)],
        travel=is_admin(user),
        lifetime=tokens.DASHBOARD_LIFETIME,
    )
    return issued.token, _expiry(issued)


# --- consent -----------------------------------------------------------------------------

CONSENT_ACTIONS = {True: "presence.consent.accept", False: "presence.consent.refuse"}


def record_consent(
    user, *, client_uuid, accepted: bool, text_version: str
) -> IdempotentResult[TrackingConsent]:
    """Record the foreman's answer to the tracking notice, once per ``client_uuid``.

    An acceptance must name the current notice (``TRACKING_CONSENT_TEXT_VERSION``). A
    refusal is taken at any time, whatever version the phone shows: withdrawing consent
    must never fail. A replay returns the original answer. The configs of the foreman's
    tracked sites are pushed (their ``foremen``).
    """
    company_id = _company_or_refuse(user)

    def create() -> TrackingConsent:
        current = selectors.current_text_version()
        if accepted and text_version != current:
            raise ValidationError(
                {
                    "text_version": _(
                        "The notice has changed: show version %(version)s and ask again."
                    )
                    % {"version": current}
                }
            )
        consent = TrackingConsent.objects.create(
            company_id=company_id,
            user=user,
            accepted=accepted,
            text_version=text_version,
            client_uuid=client_uuid,
        )
        AuditLog.record(
            actor=user,
            action=CONSENT_ACTIONS[bool(accepted)],
            target=consent,
            after={"accepted": bool(accepted), "text_version": text_version},
            company=company_id,
        )
        # The foreman joins or leaves the ``foremen`` of their tracked sites' configs.
        assigned = set(user.foreman_sites.values_list("pk", flat=True))
        queue_site_push(assigned & set(selectors.company_sites_to_push(company_id)))
        return consent

    return run_once(
        TrackingConsent.objects, client_uuid, create, matches=lambda c: c.user_id == user.pk
    )


# --- zones, gateways, badges (office screens: brief P4) ---------------------------------


def _zone_state(zone: Zone) -> dict:
    return {"site_id": zone.site_id, "code": zone.code, "name": zone.name}


def save_zone(zone: Zone, *, actor) -> Zone:
    """Create or update a zone (its code is the tracker's zone id)."""
    creating = zone.pk is None
    before = None if creating else _zone_state(Zone.objects.get(pk=zone.pk))
    with transaction.atomic():
        zone.full_clean()
        zone.save()
        AuditLog.record(
            actor=actor,
            action="presence.zone.create" if creating else "presence.zone.update",
            target=zone,
            before=before,
            after=_zone_state(zone),
            company=zone.site.company_id,
        )
        queue_site_push([zone.site_id])
    return zone


def delete_zone(zone: Zone, *, actor) -> None:
    """Delete a zone; its gateways stay, without a zone (site level)."""
    with transaction.atomic():
        AuditLog.record(
            actor=actor,
            action="presence.zone.delete",
            target=zone,
            before=_zone_state(zone),
            company=zone.site.company_id,
        )
        site_id = zone.site_id
        zone.delete()
        queue_site_push([site_id])


def _gateway_state(gateway: Gateway) -> dict:
    return {
        "site_id": gateway.site_id,
        "zone_id": gateway.zone_id,
        "label": gateway.label,
        "tracker_id": gateway.tracker_id if gateway.pk else None,
        "active": gateway.is_active,
    }


def save_gateway(gateway: Gateway, *, actor) -> Gateway:
    """Create a gateway or change its label / zone (its token is issued separately)."""
    creating = gateway.pk is None
    before = None if creating else _gateway_state(Gateway.objects.get(pk=gateway.pk))
    with transaction.atomic():
        gateway.full_clean()
        gateway.save()
        AuditLog.record(
            actor=actor,
            action="presence.gateway.create" if creating else "presence.gateway.update",
            target=gateway,
            before=before,
            after=_gateway_state(gateway),
            company=gateway.site.company_id,
        )
        queue_site_push([gateway.site_id])
    return gateway


def issue_gateway_token(gateway: Gateway, *, actor=None) -> tuple[str, datetime.datetime]:
    """A new ``gateway`` token (1 year) for brief P4's office screen, shown once.

    It replaces any earlier token of the gateway (its ``tracker_id`` changes, so the
    tracker refuses the old one once the new config is pushed) and lifts a revocation.
    """
    with transaction.atomic():
        before = _gateway_state(gateway)
        gateway.token_generation += 1
        gateway.token_issued_at = timezone.now()
        gateway.token_issued_by = actor
        gateway.revoked_at = None
        gateway.revoked_by = None
        gateway.save(
            update_fields=[
                "token_generation",
                "token_issued_at",
                "token_issued_by",
                "revoked_at",
                "revoked_by",
                "updated_at",
            ]
        )
        site = gateway.site
        issued = tokens.mint(
            kind=tokens.GATEWAY,
            sub=f"gateway:{gateway.tracker_id}",
            company=site.company_id,
            sites=[site.pk],
            travel=False,
            lifetime=tokens.GATEWAY_LIFETIME,
        )
        AuditLog.record(
            actor=actor,
            action="presence.gateway.token_issue",
            target=gateway,
            before=before,
            after={**_gateway_state(gateway), "expires_at": _expiry(issued).isoformat()},
            company=site.company_id,
        )
        queue_site_push([site.pk])
    return issued.token, _expiry(issued)


def revoke_gateway(gateway: Gateway, *, actor=None, reason: str = "") -> Gateway:
    """Stop the gateway: it leaves the site config, so the tracker refuses its token."""
    if gateway.revoked_at is not None:
        return gateway
    with transaction.atomic():
        before = _gateway_state(gateway)
        gateway.revoked_at = timezone.now()
        gateway.revoked_by = actor
        gateway.save(update_fields=["revoked_at", "revoked_by", "updated_at"])
        AuditLog.record(
            actor=actor,
            action="presence.gateway.revoke",
            target=gateway,
            before=before,
            after=_gateway_state(gateway),
            reason=reason,
            company=gateway.site.company_id,
        )
        queue_site_push([gateway.site_id])
    return gateway


def _badge_state(badge: BleBadge) -> dict:
    return {
        "badge_id": badge.badge_id,
        "worker_id": badge.worker_id,
        "active_from": badge.active_from.isoformat() if badge.active_from else None,
        "active_to": badge.active_to.isoformat() if badge.active_to else None,
    }


def assign_ble_badge(worker: Worker, badge_id: str, *, actor=None) -> BleBadge:
    """Give ``worker`` the BLE badge ``badge_id``; their previous badge ends now.

    The id is compared without case (``normalize_badge_id``). A badge another worker of
    the company still holds is refused (end it first).
    """
    badge_id = normalize_badge_id(badge_id)
    if not badge_id or len(badge_id) > 64:
        raise ValidationError({"badge_id": _("Enter the badge ID (64 characters at most).")})
    with transaction.atomic():
        held = (
            BleBadge.objects.filter(
                company_id=worker.company_id, badge_id=badge_id, active_to__isnull=True
            )
            .select_related("worker")
            .first()
        )
        if held is not None and held.worker_id == worker.pk:
            return held
        if held is not None:
            raise ValidationError(
                {
                    "badge_id": _("This badge is already given to %(worker)s.")
                    % {"worker": held.worker.full_name}
                }
            )
        now = timezone.now()
        for old in BleBadge.objects.filter(worker=worker, active_to__isnull=True):
            _end_badge(old, now, actor)
        badge = BleBadge.objects.create(
            company_id=worker.company_id, worker=worker, badge_id=badge_id, active_from=now
        )
        AuditLog.record(
            actor=actor,
            action="presence.badge.assign",
            target=badge,
            after=_badge_state(badge),
            company=worker.company_id,
        )
        queue_site_push(selectors.company_sites_to_push(worker.company_id))
    return badge


def end_ble_badge(badge: BleBadge, *, actor=None) -> BleBadge:
    """Take a BLE badge back (lost, worker left): it leaves every site config."""
    if badge.active_to is not None:
        return badge
    with transaction.atomic():
        _end_badge(badge, timezone.now(), actor)
        queue_site_push(selectors.company_sites_to_push(badge.company_id))
    return badge


def _end_badge(badge: BleBadge, when, actor) -> None:
    before = _badge_state(badge)
    badge.active_to = when
    badge.save(update_fields=["active_to", "updated_at"])
    AuditLog.record(
        actor=actor,
        action="presence.badge.end",
        target=badge,
        before=before,
        after=_badge_state(badge),
        company=badge.company_id,
    )


# --- a worker's presence history (office export, design §10) ----------------------------

# Typed text that a spreadsheet would run as a formula (as in apps.dashboard.reports).
_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


def _text_cell(value: str) -> str:
    return f"'{value}" if value.startswith(_FORMULA_PREFIXES) else value


def write_worker_presence_csv(user, worker: Worker, out) -> int:
    """The presence sessions of ``worker`` that ``user`` may see, as CSV, oldest first;
    returns the number of sessions written.

    One row per visit: the work day, the site, arrival and departure in the company time
    zone, the minutes on site, the zones in order and why the visit ended (empty while
    the worker is still on site). ``out`` is a text stream; the caller writes the UTF-8
    byte-order mark. Raw track points are not in it: they stay in the tracker and are
    deleted after ``track_retention_days``.
    """
    import csv

    tz = company_tz(CompanySettings.load(worker.company_id))
    reasons = dict(PresenceEvent.Reason.choices)
    writer = csv.writer(out)
    writer.writerow(
        [
            _("Worker number"),
            _("Worker"),
            _("Date"),
            _("Site"),
            _("Arrived"),
            _("Left"),
            _("Minutes on site"),
            _("Zones"),
            _("Ended because"),
        ]
    )
    count = 0
    for session in selectors.worker_sessions(user, worker):
        started = session.started_at.astimezone(tz)
        ended = session.ended_at.astimezone(tz) if session.ended_at else None
        minutes = ""
        if ended is not None:
            minutes = int((session.ended_at - session.started_at).total_seconds() // 60)
        writer.writerow(
            [
                _text_cell(worker.number),
                _text_cell(worker.full_name),
                session.local_date.isoformat(),
                _text_cell(session.site.name),
                started.strftime("%Y-%m-%d %H:%M"),
                ended.strftime("%Y-%m-%d %H:%M") if ended else "",
                minutes,
                _text_cell(" > ".join(session.zones)),
                str(reasons.get(session.end_reason, session.end_reason)) if ended else "",
            ]
        )
        count += 1
    return count


# --- events from the tracker -------------------------------------------------------------

MAX_EVENTS = 500


@dataclass(frozen=True)
class EventIn:
    """One validated event of a batch (``api.serializers.PresenceEventSerializer``)."""

    event_id: object  # uuid.UUID
    site_id: int
    subject_kind: str
    subject_id: int
    type: str
    at: datetime.datetime
    zone: str = ""
    from_zone: str = ""
    reason: str = ""


@dataclass(frozen=True)
class IngestResult:
    """``stored``: new and applied; ``duplicates``: seen before (or twice in the batch);
    ``ignored``: new, stored, but not applied (``_Applicability``); ``rejected``: invalid
    items, not stored. Together they count every item of the batch."""

    stored: int
    duplicates: int
    ignored: int
    rejected: int = 0


# Ids are 64-bit signed integers in the database (SQLite INTEGER, BigAutoField).
MAX_ID = 2**63 - 1
_EVENT_TYPES = frozenset({engine.ENTERED, engine.MOVED_ZONE, engine.LEFT})


def _well_formed(event: EventIn) -> bool:
    """What the serializer checks, again: a bad item must be rejected, not fail the
    batch (an id the database cannot hold would)."""
    return (
        event.subject_kind in SubjectKind.values
        and event.type in _EVENT_TYPES
        and all(
            isinstance(value, int) and 1 <= value <= MAX_ID
            for value in (event.site_id, event.subject_id)
        )
        and isinstance(event.at, datetime.datetime)
        and event.at.tzinfo is not None
    )


class _Applicability:
    """Which new events of a batch count for the sessions (``PresenceEvent.applied``).

    Not applied: an unknown site, a subject unknown or of another company, a site of an
    inactive company; a worker who is inactive or whose ``tracking_enabled`` (company →
    site → worker) is off; a foreman whose latest answer to the notice at the event's
    time is not an acceptance (refused, or never accepted). Settings are read as they are
    now; consent as it was at the event.
    """

    def __init__(self, events: list[EventIn]):
        def ids(kind):
            return {e.subject_id for e in events if e.subject_kind == kind}

        self.sites = Site.objects.select_related("company").in_bulk({e.site_id for e in events})
        self.workers = Worker.objects.in_bulk(ids(SubjectKind.WORKER))
        foreman_ids = ids(SubjectKind.FOREMAN)
        self.foremen = dict(
            get_user_model().objects.filter(pk__in=foreman_ids).values_list("pk", "company_id")
        )
        self.consents: dict[int, list[tuple]] = {}
        answers = TrackingConsent.objects.filter(user_id__in=foreman_ids).order_by("at", "pk")
        for user_id, at, accepted, version in answers.values_list(
            "user_id", "at", "accepted", "text_version"
        ):
            self.consents.setdefault(user_id, []).append((at, accepted, version))
        self.current_version = selectors.current_text_version()
        self._settings: dict[int, CompanySettings] = {}

    def company_id(self, event: EventIn) -> int | None:
        site = self.sites.get(event.site_id)
        return site.company_id if site is not None else None

    def applied(self, event: EventIn) -> bool:
        site = self.sites.get(event.site_id)
        if site is None or not site.company.is_active:
            return False
        if event.subject_kind == SubjectKind.WORKER:
            worker = self.workers.get(event.subject_id)
            return (
                worker is not None
                and worker.company_id == site.company_id
                and worker.is_active
                and bool(
                    resolve_setting(
                        "tracking_enabled",
                        site=site,
                        worker=worker,
                        company=self._company_settings(site.company_id),
                    )
                )
            )
        if self.foremen.get(event.subject_id) != site.company_id:
            return False
        return self._consented(event.subject_id, event.at)

    def _consented(self, user_id: int, at: datetime.datetime) -> bool:
        """The latest answer at or before ``at`` accepts a notice no newer than the
        current one (versions are ISO dates: they compare as text)."""
        latest = None
        for answer in self.consents.get(user_id, []):
            if answer[0] > at:
                break
            latest = answer
        if latest is None:
            return False
        _at, accepted, version = latest
        return bool(accepted) and version <= self.current_version

    def _company_settings(self, company_id: int) -> CompanySettings:
        if company_id not in self._settings:
            self._settings[company_id] = CompanySettings.load(company_id)
        return self._settings[company_id]


def ingest_events(events: Iterable[EventIn], *, rejected: int = 0) -> IngestResult:
    """Store a batch from the tracker's outbox once per ``event_id`` and rebuild the
    sessions of every person it is about. Replaying a batch changes nothing."""
    batch: dict = {}
    duplicates = 0
    for event in events:
        if not _well_formed(event):
            rejected += 1
            logger.warning("Presence: event %s of a batch rejected: malformed", event.event_id)
        elif event.event_id in batch:
            duplicates += 1
        else:
            batch[event.event_id] = event
    known = set(
        PresenceEvent.objects.filter(event_id__in=list(batch)).values_list("event_id", flat=True)
    )
    duplicates += len(known)
    new = [event for event_id, event in batch.items() if event_id not in known]

    rules = _Applicability(new)
    rows, ignored = [], 0
    for event in new:
        company_id = rules.company_id(event)
        applied = rules.applied(event)
        ignored += not applied
        rows.append(
            PresenceEvent(
                event_id=event.event_id,
                company_id=company_id,
                site_ref=event.site_id,
                site_id=event.site_id if company_id is not None else None,
                subject_kind=event.subject_kind,
                subject_id=event.subject_id,
                type=event.type,
                at=event.at,
                zone_code=event.zone,
                from_zone_code=event.from_zone,
                reason=event.reason,
                applied=applied,
            )
        )
    with transaction.atomic():
        # ignore_conflicts: a copy of the batch stored meanwhile is not an error.
        PresenceEvent.objects.bulk_create(rows, ignore_conflicts=True)

    # Every applied event of the batch, new or replayed: a rebuild that failed after the
    # events were stored is done again when the tracker resends the batch.
    affected = (
        PresenceEvent.objects.filter(event_id__in=list(batch), applied=True)
        .exclude(site_id=None)  # its site was deleted since
        .values("site_id", "subject_kind", "subject_id")
        .annotate(since=Min("at"))
        .order_by("site_id", "subject_kind", "subject_id")
    )
    for key in affected:
        rebuild_sessions(key["site_id"], key["subject_kind"], key["subject_id"], key["since"])
    return IngestResult(
        stored=len(rows) - ignored, duplicates=duplicates, ignored=ignored, rejected=rejected
    )


def rebuild_sessions(site_id: int, subject_kind: str, subject_id: int, since) -> None:
    """Recompute one person's sessions on a site from ``since`` on (``engine.fold``).

    The fold starts at a time (``_fold_start``) before which no live session can change,
    and replaces the stored live sessions that began at or after it. Nothing is written
    when the result is what is stored.

    Days the nightly reconciliation replaced (``PresenceReconciliation.replayed_windows``)
    are left out: their events are not folded and their ``replay`` sessions are kept.
    """
    site = Site.objects.only("pk", "company_id").get(pk=site_id)
    tz = company_tz(CompanySettings.load(site.company_id))
    with transaction.atomic():
        sessions = PresenceSession.objects.filter(
            site_id=site_id, subject_kind=subject_kind, subject_id=subject_id
        )
        start = _fold_start(sessions.filter(source=PresenceSession.Source.LIVE), since)
        events = PresenceEvent.objects.filter(
            site_id=site_id,
            subject_kind=subject_kind,
            subject_id=subject_id,
            applied=True,
            at__gte=start,
        )
        for window_start, window_end in PresenceReconciliation.replayed_windows(
            site_id, after=start
        ):
            events = events.exclude(at__gte=window_start, at__lt=window_end)
        visits = engine.fold(
            engine.Event(
                event_id=str(e.event_id),
                type=e.type,
                at=e.at,
                zone=e.zone_code,
                from_zone=e.from_zone_code,
                reason=e.reason,
            )
            for e in events
        )
        stored = list(
            sessions.filter(started_at__gte=start, source=PresenceSession.Source.LIVE).order_by(
                "started_at", "pk"
            )
        )
        if [_session_key(s) for s in stored] == [visit.key() for visit in visits]:
            return
        PresenceSession.objects.filter(pk__in=[s.pk for s in stored]).delete()
        PresenceSession.objects.bulk_create(
            PresenceSession(
                company_id=site.company_id,
                site_id=site_id,
                subject_kind=subject_kind,
                subject_id=subject_id,
                local_date=visit.started_at.astimezone(tz).date(),
                started_at=visit.started_at,
                ended_at=visit.ended_at,
                end_reason=visit.end_reason,
                zones=visit.zones,
                last_event_at=visit.last_event_at,
                event_count=visit.event_count,
            )
            for visit in visits
        )


def _fold_start(sessions, since):
    """The earliest time the events from ``since`` on can change ``sessions`` (live) from.

    Folding the events at or after ``start`` gives the stored sessions that began at or
    after it only if the fold is between visits there: the session before ``start``
    ended before it (none of its events are at or after ``start``, even at the same
    instant) and was closed by a ``left`` (it has a reason; a visit closed by the next
    ``entered``, or still open, would take events of the window). Else ``start`` moves
    back to that session's start, and again.
    """
    start = since
    while True:
        previous = (
            sessions.filter(started_at__lt=start)
            .order_by("-started_at", "-pk")
            .values_list("started_at", "ended_at", "end_reason")
            .first()
        )
        if previous is None:
            return start
        started_at, ended_at, end_reason = previous
        if ended_at is not None and ended_at < start and end_reason:
            return start
        start = started_at


def _session_key(session: PresenceSession) -> tuple:
    return (
        session.started_at,
        session.ended_at,
        session.end_reason,
        tuple(session.zones),
        session.last_event_at,
        session.event_count,
    )


# --- site configs for the tracker --------------------------------------------------------


class TrackerPushError(Exception):
    """The tracker did not take a site config (refused it, or unreachable)."""


class TrackerUnreachable(TrackerPushError):
    """No answer from the tracker (down, timed out, wrong URL)."""


class TrackerServerError(TrackerPushError):
    """The tracker failed (HTTP 5xx): worth trying again, unlike a refusal (4xx)."""


def site_config(site: Site) -> dict:
    """The tracker's ``SiteConfig`` for ``site`` (``docs/api/tracker-openapi.yaml``).

    Geofence from ``checkin.SiteGeofence`` (none: gateways only); zones; gateways with a
    token that is not revoked; badges of active workers with ``tracking_enabled``
    (company → site → worker) only; the foremen who may be tracked (``_tracked_foremen``);
    rules from the resolved settings. With tracking off on the site, or the company
    inactive, the lists are empty and the tracker stops ingest.
    """
    from apps.checkin.models import SiteGeofence

    company = CompanySettings.load(site.company_id)

    def setting(name, worker=None):
        return resolve_setting(name, site=site, worker=worker, company=company)

    rules = {
        "sightings_to_enter": int(setting("presence_sightings_to_enter")),
        "arrival_window_seconds": int(setting("presence_arrival_window_seconds")),
        "absence_to_leave_seconds": int(setting("presence_absence_to_leave_seconds")),
    }
    if not setting("tracking_enabled") or not site.company.is_active:
        return {
            "tracking_enabled": False,
            "geofence": None,
            "zones": [],
            "gateways": [],
            "badges": [],
            "foremen": [],
            "rules": rules,
        }
    fence = SiteGeofence.objects.filter(site=site).first()
    geofence = (
        {
            "lat": float(fence.latitude),
            "lng": float(fence.longitude),
            "radius_m": float(fence.radius_m),
        }
        if fence is not None
        else None
    )
    zones = Zone.objects.filter(site=site).order_by("code")
    gateways = (
        Gateway.objects.filter(site=site, token_issued_at__isnull=False, revoked_at__isnull=True)
        .select_related("zone")
        .order_by("pk")
    )
    badges = (
        BleBadge.objects.filter(
            company_id=site.company_id,
            active_to__isnull=True,
            active_from__lte=timezone.now(),
            worker__is_active=True,
        )
        .select_related("worker")
        .order_by("badge_id")
    )
    return {
        "tracking_enabled": True,
        "geofence": geofence,
        "zones": [{"id": zone.code, "name": zone.name} for zone in zones],
        "gateways": [
            {"id": gw.tracker_id, "zone_id": gw.zone.code if gw.zone_id else None}
            for gw in gateways
        ],
        "badges": [
            {"badge_id": badge.badge_id, "worker_id": badge.worker_id}
            for badge in badges
            if setting("tracking_enabled", worker=badge.worker)
        ],
        "foremen": _tracked_foremen(site),
        "rules": rules,
    }


def _tracked_foremen(site: Site) -> list[int]:
    """The active foremen assigned to ``site`` whose latest answer to the notice accepts
    the current version: the tracker drops GPS from any other foreman."""
    foremen = list(
        site.foremen.filter(is_active=True, company_id=site.company_id)
        .order_by("pk")
        .values_list("pk", flat=True)
    )
    latest: dict[int, tuple[bool, str]] = {}
    answers = TrackingConsent.objects.filter(user_id__in=foremen).order_by("at", "pk")
    for user_id, accepted, version in answers.values_list("user_id", "accepted", "text_version"):
        latest[user_id] = (accepted, version)
    current = selectors.current_text_version()
    return [pk for pk in foremen if latest.get(pk) == (True, current)]


def push_site_config(site: Site) -> bool:
    """PUT the site's config to the tracker with a fresh service token (5 minutes).

    ``False`` when ``TRACKER_URL`` is not set (logged, nothing sent); ``True`` once the
    tracker answered 2xx; ``TrackerPushError`` otherwise. The outcome is kept on the
    site's ``TrackerSync`` row.
    """
    if not settings.TRACKER_URL:
        logger.info("Presence: TRACKER_URL is not set; config of site %s not pushed", site.pk)
        return False
    config = site_config(site)
    request = urllib.request.Request(
        f"{settings.TRACKER_URL}/internal/sites/{site.pk}/config",
        data=json.dumps(config, separators=(",", ":")).encode("utf-8"),
        method="PUT",
        headers={
            "Authorization": f"Bearer {tokens.mint_service_token()}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
    )
    now = timezone.now()
    try:
        with urllib.request.urlopen(  # noqa: S310 (the URL is a setting, http/https)
            request, timeout=settings.TRACKER_TIMEOUT_SECONDS
        ) as response:
            status = response.status
    except urllib.error.HTTPError as exc:
        _record_push(site, now, enabled=config["tracking_enabled"], error=f"HTTP {exc.code}")
        error = TrackerServerError if exc.code >= 500 else TrackerPushError
        raise error(f"The tracker answered {exc.code} for site {site.pk}") from exc
    except (urllib.error.URLError, OSError) as exc:
        _record_push(site, now, enabled=config["tracking_enabled"], error=str(exc)[:300])
        raise TrackerUnreachable(f"The tracker is unreachable: {exc}") from exc
    if not 200 <= status < 300:
        _record_push(site, now, enabled=config["tracking_enabled"], error=f"HTTP {status}")
        raise TrackerPushError(f"The tracker answered {status} for site {site.pk}")
    _record_push(site, now, enabled=config["tracking_enabled"], error="")
    return True


def _record_push(site: Site, when, *, enabled: bool, error: str) -> None:
    values = {"last_attempt_at": when, "last_error": error}
    if not error:
        values.update(last_success_at=when, pushed_enabled=enabled)
    TrackerSync.objects.update_or_create(site=site, defaults=values)


def sites_to_resync() -> list[Site]:
    """For the periodic resync: every site of an active company that is tracked now or
    was at its last push, or whose last push failed; and every site of an inactive
    company that the tracker still has as tracked, or whose last push failed (its config
    turns tracking off: ``site_config``)."""
    from apps.core.models import Company

    failed = set(TrackerSync.objects.exclude(last_error="").values_list("site_id", flat=True))
    was_on = set(TrackerSync.objects.filter(pushed_enabled=True).values_list("site_id", flat=True))
    result = []
    for company in Company.objects.order_by("pk"):
        if company.is_active:
            wanted = set(selectors.company_sites_to_push(company.pk)) | failed
        else:
            wanted = was_on | failed
        sites = Site.objects.filter(company=company).select_related("company").order_by("pk")
        result.extend(site for site in sites if site.pk in wanted)
    return result


# --- queueing pushes ---------------------------------------------------------------------

# Seconds a queued push waits: the changes of one screen save ride along.
PUSH_DELAY_SECONDS = 2
_pending = threading.local()


def _pending_sites() -> set[int]:
    if not hasattr(_pending, "sites"):
        _pending.sites = set()
    return _pending.sites


def queue_site_push(site_ids: Iterable[int]) -> None:
    """Push the configs of ``site_ids`` once the current transaction commits (at once
    outside one), each site once however many changes asked for it.

    With a Celery broker the push is a task; without one it runs in this process after
    the commit, and a failure is only logged (the periodic resync tries again). Whatever
    goes wrong there, the change is committed: it never turns into an error (``robust``).
    """
    ids = {int(site_id) for site_id in site_ids if site_id is not None}
    if not ids:
        return
    _pending_sites().update(ids)
    transaction.on_commit(_flush_pending, robust=True)


def _flush_pending() -> None:
    pending = _pending_sites()
    ids = sorted(pending)
    pending.clear()
    for site_id in ids:
        try:
            _send_push(site_id)
        except Exception:
            # After the commit: log it, and push the other sites still.
            logger.exception("Presence: config push of site %s failed", site_id)


def _send_push(site_id: int) -> None:
    if not settings.TRACKER_URL:
        logger.info("Presence: TRACKER_URL is not set; config of site %s not pushed", site_id)
        return
    if settings.CELERY_BROKER_URL:
        from .tasks import push_site_config_task  # tasks import this module

        try:
            push_site_config_task.apply_async((site_id,), countdown=PUSH_DELAY_SECONDS)
        except Exception:  # broker down: kombu, redis and socket errors alike
            logger.warning(
                "Presence: could not queue the config push of site %s", site_id, exc_info=True
            )
        return
    site = Site.objects.filter(pk=site_id).first()
    if site is None:
        return
    try:
        push_site_config(site)
    except TrackerPushError:
        logger.warning("Presence: config push of site %s failed", site_id, exc_info=True)


# --- which sites a change concerns (signals) ---------------------------------------------


def _changed(entry: AuditLog) -> set[str]:
    before, after = entry.before or {}, entry.after or {}
    if not isinstance(before, dict) or not isinstance(after, dict):
        return set()
    return {key for key in set(before) | set(after) if before.get(key) != after.get(key)}


def _has_ble_badge(worker_id) -> bool:
    return BleBadge.objects.filter(worker_id=worker_id, active_to__isnull=True).exists()


def _admin_target_sites(entry: AuditLog) -> set[int]:
    """Edits in the Django admin (``admin.<app>.<model>.<verb>``)."""
    parts = entry.action.split(".")
    if len(parts) < 4 or entry.content_type_id is None:
        return set()
    label = f"{parts[1]}.{parts[2]}"
    company_wide = {"core.companysettings", "presence.blebadge", "workers.worker"}
    if label in company_wide:
        if label == "workers.worker" and not _has_ble_badge(entry.object_id):
            return set()
        return set(selectors.company_sites_to_push(entry.company_id)) if entry.company_id else set()
    if label == "worksites.site":
        return {int(entry.object_id)}
    if label in {"checkin.sitegeofence", "presence.zone", "presence.gateway"}:
        target = entry.content_type.model_class()._default_manager.filter(pk=entry.object_id)
        return set(target.values_list("site_id", flat=True))
    return set()


def sites_for_audit_entry(entry: AuditLog) -> set[int]:
    """The sites whose tracker config an audited change may alter (none for most)."""
    action = entry.action
    if action == "settings.update":
        if _changed(entry) & TRACKING_SETTINGS and entry.company_id:
            return set(selectors.company_sites_to_push(entry.company_id))
        return set()
    if action == "worksites.settings_update":
        return {int(entry.object_id)} if _changed(entry) & TRACKING_SETTINGS else set()
    if action in {"worksites.site_create", "checkin.geofence.set", "checkin.geofence.clear"}:
        return {int(entry.object_id)} if entry.object_id else set()
    if action == "worksites.site_update":  # its foremen may have changed (``foremen``)
        if not entry.object_id or not entry.company_id:
            return set()
        return {int(entry.object_id)} & set(selectors.company_sites_to_push(entry.company_id))
    if action == "workers.update":
        if _changed(entry) & {"tracking_enabled", "is_active"} and _has_ble_badge(entry.object_id):
            return set(selectors.company_sites_to_push(entry.company_id))
        return set()
    if action.startswith("admin."):
        return _admin_target_sites(entry)
    return set()  # presence.* entries queue their push themselves
