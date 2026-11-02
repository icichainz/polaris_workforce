"""Reads for presence tracking: which sites are tracked, consent, a site's day, and the
office screens (setup overview, sync status, who a live map may name, timelines)."""

from __future__ import annotations

import datetime
from dataclasses import dataclass

from django.conf import settings
from django.db.models import Count, Exists, OuterRef, Q
from django.utils import timezone

from apps.accounts.permissions import scoped_sites
from apps.core.dates import company_tz
from apps.core.models import CompanySettings
from apps.core.settings import resolve_setting

from .models import (
    BleBadge,
    PresenceReconciliation,
    PresenceSession,
    SubjectKind,
    TrackerSync,
    TrackingConsent,
)

# --- settings ----------------------------------------------------------------------------


def is_tracked(site, company: CompanySettings | None = None) -> bool:
    """``tracking_enabled`` resolved for the site (company → site)."""
    company = company or CompanySettings.load(site.company_id)
    return bool(resolve_setting("tracking_enabled", site=site, company=company))


def tracked_sites(user) -> list:
    """The sites ``user`` may see where tracking is on, by id (``scoped_sites``)."""
    sites = list(scoped_sites(user).order_by("pk"))
    if not sites:
        return []
    company = CompanySettings.load(sites[0].company_id)
    return [site for site in sites if is_tracked(site, company)]


def company_sites_to_push(company_id: int) -> list[int]:
    """The company's sites whose config must follow a company-wide change: those tracked
    now, and those tracked at their last push (to turn tracking off there)."""
    from apps.worksites.models import Site

    company = CompanySettings.load(company_id)
    sites = Site.objects.filter(company_id=company_id).order_by("pk")
    was_on = set(
        TrackerSync.objects.filter(site__company_id=company_id, pushed_enabled=True).values_list(
            "site_id", flat=True
        )
    )
    return [site.pk for site in sites if site.pk in was_on or is_tracked(site, company)]


# --- consent -----------------------------------------------------------------------------


def current_text_version() -> str:
    return settings.TRACKING_CONSENT_TEXT_VERSION


@dataclass(frozen=True)
class ConsentState:
    """``accepted``: the current notice is accepted. ``refused``: the latest answer is a
    refusal (of any version). Neither: the foreman must be asked (``consent_required``)."""

    accepted: bool
    refused: bool
    latest: TrackingConsent | None


def consent_state(user) -> ConsentState:
    latest = TrackingConsent.objects.filter(user=user).order_by("-at", "-pk").first()
    if latest is None:
        return ConsentState(accepted=False, refused=False, latest=None)
    if not latest.accepted:
        return ConsentState(accepted=False, refused=True, latest=latest)
    return ConsentState(
        accepted=latest.text_version == current_text_version(), refused=False, latest=latest
    )


# --- a site's day ------------------------------------------------------------------------


@dataclass(frozen=True)
class DaySession:
    """One person's presence on a site for a day: every visit folded together."""

    subject_kind: str
    subject_id: int
    first_seen_at: datetime.datetime
    last_seen_at: datetime.datetime | None  # None while a visit is open
    open: bool
    zones: list[str]
    visits: int


def day_sessions(site, date: datetime.date) -> list[DaySession]:
    """Who was seen on ``site`` on ``date`` (company time zone), first arrival first."""
    rows = PresenceSession.objects.filter(site=site, local_date=date).order_by("started_at", "pk")
    people: dict[tuple[str, int], list[PresenceSession]] = {}
    for row in rows:
        people.setdefault((row.subject_kind, row.subject_id), []).append(row)
    result = []
    for (kind, subject_id), visits in people.items():
        is_open = any(visit.ended_at is None for visit in visits)
        zones: list[str] = []
        for visit in visits:
            zones.extend(zone for zone in visit.zones if zone not in zones)
        result.append(
            DaySession(
                subject_kind=kind,
                subject_id=subject_id,
                first_seen_at=visits[0].started_at,
                last_seen_at=None if is_open else max(visit.ended_at for visit in visits),
                open=is_open,
                zones=zones,
                visits=len(visits),
            )
        )
    return sorted(result, key=lambda s: (s.first_seen_at, s.subject_kind, s.subject_id))


# --- roll-call suggestions (apps.checkin.selectors) ---------------------------------------


def tracked_worker_ids(site, workers, company: CompanySettings | None = None) -> set[int]:
    """The ids of ``workers`` (of the site's company) whose ``tracking_enabled`` resolves
    on for ``site`` (company → site → worker)."""
    company = company or CompanySettings.load(site.company_id)
    return {
        worker.pk
        for worker in workers
        if worker.company_id == site.company_id
        and resolve_setting("tracking_enabled", site=site, worker=worker, company=company)
    }


def workers_day_presence(
    site, date: datetime.date, worker_ids, company: CompanySettings | None = None
) -> dict[int, DaySession]:
    """``{worker id: DaySession}`` for the workers among ``worker_ids`` seen on ``site``
    on ``date`` and tracked there (``tracked_worker_ids``). A worker with tracking off
    has no presence, whatever sessions are stored."""
    from apps.workers.models import Worker

    wanted = set(worker_ids)
    if not wanted:
        return {}
    seen = {
        row.subject_id: row
        for row in day_sessions(site, date)
        if row.subject_kind == SubjectKind.WORKER and row.subject_id in wanted
    }
    if not seen:
        return {}
    workers = Worker.objects.filter(pk__in=list(seen), company_id=site.company_id).only(
        "pk", "company_id", "tracking_enabled"
    )
    tracked = tracked_worker_ids(site, workers, company)
    return {worker_id: row for worker_id, row in seen.items() if worker_id in tracked}


# --- office screens (brief P4) -----------------------------------------------------------


@dataclass(frozen=True)
class SyncStatus:
    """The last push of a site's config to the tracker, for the office.

    ``state``: ``not_configured`` (no ``TRACKER_URL`` on this server), ``never`` (not
    pushed yet), ``ok`` (the last push was taken) or ``failed`` (``error`` says why).
    """

    state: str
    last_attempt_at: datetime.datetime | None = None
    last_success_at: datetime.datetime | None = None
    error: str = ""
    pushed_enabled: bool = False


def _sync_status(sync: TrackerSync | None) -> SyncStatus:
    values = {}
    if sync is not None:
        values = {
            "last_attempt_at": sync.last_attempt_at,
            "last_success_at": sync.last_success_at,
            "error": sync.last_error,
            "pushed_enabled": sync.pushed_enabled,
        }
    if not settings.TRACKER_URL:
        return SyncStatus(state="not_configured", **values)
    if sync is None or sync.last_attempt_at is None:
        return SyncStatus(state="never", **values)
    return SyncStatus(state="failed" if sync.last_error else "ok", **values)


def sync_status(site) -> SyncStatus:
    return _sync_status(TrackerSync.objects.filter(site=site).first())


def latest_reconciliation(site) -> PresenceReconciliation | None:
    """The site's most recent nightly check of a day against the tracker's replay (the
    latest day tried, successful or not), or ``None`` before the first one."""
    return (
        PresenceReconciliation.objects.filter(site=site)
        .order_by("-local_date", "-last_attempt_at")
        .first()
    )


@dataclass(frozen=True)
class SiteSetup:
    """One row of the presence overview: a site and what is set up for the tracker."""

    site: object
    tracked: bool
    zones: int
    gateways: int
    active_gateways: int
    sync: SyncStatus


def site_setups(sites) -> list[SiteSetup]:
    """The overview rows for ``sites`` (a scoped queryset), in its order."""
    sites = list(
        sites.annotate(
            n_zones=Count("presence_zones", distinct=True),
            n_gateways=Count("presence_gateways", distinct=True),
            n_active=Count(
                "presence_gateways",
                filter=Q(
                    presence_gateways__token_issued_at__isnull=False,
                    presence_gateways__revoked_at__isnull=True,
                ),
                distinct=True,
            ),
        )
    )
    if not sites:
        return []
    company = CompanySettings.load(sites[0].company_id)
    syncs = {s.site_id: s for s in TrackerSync.objects.filter(site__in=[s.pk for s in sites])}
    return [
        SiteSetup(
            site=site,
            tracked=is_tracked(site, company),
            zones=site.n_zones,
            gateways=site.n_gateways,
            active_gateways=site.n_active,
            sync=_sync_status(syncs.get(site.pk)),
        )
        for site in sites
    ]


def site_people(user, site, today: datetime.date):
    """Who the live map of ``site`` may name, as ``(workers, foremen)`` querysets.

    Workers: those ``user`` may see (``scoped_workers``) who are on the site's crew today
    or hold a BLE badge (a badge is in every site's config, so its holder can be seen on
    any site of the company). Foremen: the site's. Names always come from Django: the
    tracker only knows ids.
    """
    from apps.workers.services import scoped_workers
    from apps.worksites.models import SiteAssignment

    on_crew = SiteAssignment.objects.active_on(today).filter(site=site, worker=OuterRef("pk"))
    badge = BleBadge.objects.filter(worker=OuterRef("pk"), active_to__isnull=True)
    workers = (
        scoped_workers(user, today)
        .filter(Exists(on_crew) | Exists(badge))
        .select_related("trade")
        .order_by("last_name", "first_name", "pk")
    )
    foremen = site.foremen.order_by("last_name", "first_name", "pk")
    return workers, foremen


def day_bounds(date: datetime.date, tz) -> tuple[datetime.datetime, datetime.datetime]:
    """The work day ``date`` in the company time zone, as aware ``[start, end)``."""
    start = datetime.datetime.combine(date, datetime.time.min, tzinfo=tz)
    end = datetime.datetime.combine(date + datetime.timedelta(days=1), datetime.time.min, tzinfo=tz)
    return start, end


@dataclass(frozen=True)
class TimelineRow:
    """One visit on the day's timeline: times in the company time zone, and where its bar
    sits on the axis (percent of the axis width)."""

    session: PresenceSession
    started: datetime.datetime
    ended: datetime.datetime | None
    minutes: int
    left: float
    width: float


@dataclass(frozen=True)
class Timeline:
    rows: list[TimelineRow]
    ticks: list[tuple[str, float]]  # ("06:00", percent from the left)
    total_minutes: int


def person_sessions(user, subject_kind: str, subject_id: int, date: datetime.date):
    """The person's sessions that started on ``date``, on the sites ``user`` may see."""
    return (
        PresenceSession.objects.filter(
            site__in=scoped_sites(user),
            subject_kind=subject_kind,
            subject_id=subject_id,
            local_date=date,
        )
        .select_related("site")
        .order_by("started_at", "pk")
    )


def _hour_up(moment: datetime.datetime) -> int:
    return moment.hour + (1 if (moment.minute or moment.second or moment.microsecond) else 0)


def timeline(sessions, date: datetime.date, company: CompanySettings, now=None) -> Timeline:
    """Bars for ``sessions`` on an hour axis: the company's tracking hours, widened to
    whole hours to hold every visit. A visit still open runs to ``now``."""
    tz = company_tz(company)
    now = now or timezone.now()
    day_start, day_end = day_bounds(date, tz)
    spans = []
    for session in sessions:
        end = session.ended_at or min(max(now, session.started_at), day_end)
        spans.append((session, session.started_at.astimezone(tz), end.astimezone(tz)))
    tracking_start = resolve_setting("tracking_start", company=company)
    tracking_end = resolve_setting("tracking_end", company=company)
    first_hour = tracking_start.hour
    last_hour = _hour_up(datetime.datetime.combine(date, tracking_end))
    for _session, start, end in spans:
        first_hour = min(first_hour, start.hour)
        last_hour = 24 if end.date() > date else max(last_hour, _hour_up(end))
    last_hour = min(max(last_hour, first_hour + 1), 24)
    axis_start = day_start + datetime.timedelta(hours=first_hour)
    axis_seconds = (last_hour - first_hour) * 3600

    def percent(moment) -> float:
        seconds = (moment - axis_start).total_seconds()
        return round(min(max(seconds / axis_seconds, 0.0), 1.0) * 100, 2)

    rows, total = [], 0
    for session, start, end in spans:
        minutes = max(int((end - start).total_seconds() // 60), 0)
        total += minutes
        left = percent(start)
        rows.append(
            TimelineRow(
                session=session,
                started=start,
                ended=session.ended_at.astimezone(tz) if session.ended_at else None,
                minutes=minutes,
                left=left,
                width=max(round(percent(end) - left, 2), 0.5),
            )
        )
    step = 2 if last_hour - first_hour > 8 else 1
    ticks = [
        (f"{hour:02d}:00", round((hour - first_hour) * 3600 / axis_seconds * 100, 2))
        for hour in range(first_hour, last_hour + 1, step)
    ]
    return Timeline(rows=rows, ticks=ticks, total_minutes=total)


def worker_sessions(user, worker):
    """Every presence session of ``worker`` on the sites ``user`` may see, oldest first
    (the worker's own presence history, ``docs/presence-tracking.md`` §10)."""
    return (
        PresenceSession.objects.filter(
            company_id=worker.company_id,
            site__in=scoped_sites(user),
            subject_kind=SubjectKind.WORKER,
            subject_id=worker.pk,
        )
        .select_related("site")
        .order_by("started_at", "pk")
    )
