"""Reads for presence tracking: which sites are tracked, consent, a site's day."""

from __future__ import annotations

import datetime
from dataclasses import dataclass

from django.conf import settings

from apps.accounts.permissions import scoped_sites
from apps.core.models import CompanySettings
from apps.core.settings import resolve_setting

from .models import PresenceSession, SubjectKind, TrackerSync, TrackingConsent

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
