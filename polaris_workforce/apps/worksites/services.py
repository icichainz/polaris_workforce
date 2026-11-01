"""Site and crew rules: who is on a site's crew on a given day, assignments, site settings.

Views, the crew API and other apps call these functions; nothing here touches requests.
Every write to an assignment or to a site's pay settings writes an ``AuditLog`` entry.
"""

import datetime
from dataclasses import dataclass
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Count, Exists, OuterRef, Q, Subquery
from django.db.models.functions import Coalesce
from django.utils.translation import gettext as _

from apps.attendance.models import AttendanceDay, AttendanceRecord
from apps.core.dates import company_today  # noqa: F401 (re-exported for the crew API)
from apps.core.models import AuditLog, CompanySettings
from apps.core.settings import SITE_OVERRIDABLE, resolve_setting
from apps.workers.models import Worker

from .models import Site, SiteAssignment

# Statuses that count as "on site" in the present-today figures (½ and late included).
PRESENT_STATUSES = (
    AttendanceRecord.Status.PRESENT,
    AttendanceRecord.Status.HALF_DAY,
    AttendanceRecord.Status.LATE,
)

ONE_DAY = datetime.timedelta(days=1)


def _jsonable(value):
    if isinstance(value, datetime.date | datetime.time):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    return value


def _assignment_snapshot(assignment: SiteAssignment) -> dict:
    return {
        "worker_id": assignment.worker_id,
        "site_id": assignment.site_id,
        "start_date": _jsonable(assignment.start_date),
        "end_date": _jsonable(assignment.end_date),
    }


# --- crew -------------------------------------------------------------------------------
#
# The one definition of a site's crew (the workers "expected" on a roll call): active
# workers whose assignment covers the date, start and end dates inclusive. Attendance and
# payroll call these functions instead of querying assignments themselves.


def active_assignments(date: datetime.date):
    """Assignments covering ``date`` whose worker is active."""
    return SiteAssignment.objects.active_on(date).filter(worker__is_active=True)


def crew_for(site: Site, date: datetime.date):
    """Active workers assigned to ``site`` on ``date``, sorted by last name."""
    on_site = SiteAssignment.objects.active_on(date).filter(site=site, worker=OuterRef("pk"))
    return (
        Worker.objects.filter(company_id=site.company_id, is_active=True)
        .filter(Exists(on_site))
        .select_related("trade")
        .order_by("last_name", "first_name", "number")
    )


def crew_ids(site: Site, date: datetime.date) -> set[int]:
    """Ids of the workers on ``site``'s crew on ``date`` (see ``crew_for``)."""
    return set(active_assignments(date).filter(site=site).values_list("worker_id", flat=True))


def crew_ids_by_date(
    site: Site, start: datetime.date, end: datetime.date
) -> dict[datetime.date, set[int]]:
    """``{date: crew_ids(site, date)}`` for every date from ``start`` to ``end``, in one query."""
    rows = list(
        SiteAssignment.objects.overlapping(start, end)
        .filter(site=site, worker__is_active=True)
        .values_list("worker_id", "start_date", "end_date")
    )
    result = {}
    day = start
    while day <= end:
        result[day] = {
            worker_id
            for worker_id, first, last in rows
            if first <= day and (last is None or last >= day)
        }
        day += ONE_DAY
    return result


def crew_ids_by_site_and_date(
    spans: dict[int, tuple[datetime.date, datetime.date]],
) -> dict[int, dict[datetime.date, set[int]]]:
    """``crew_ids_by_date`` of several sites, each over its own ``(start, end)``, in one
    query: ``{site_id: {date: {worker_id, …}}}``."""
    if not spans:
        return {}
    first = min(start for start, _end in spans.values())
    last = max(end for _start, end in spans.values())
    rows = list(
        SiteAssignment.objects.overlapping(first, last)
        .filter(site_id__in=list(spans), worker__is_active=True)
        .values_list("site_id", "worker_id", "start_date", "end_date")
    )
    result = {}
    for site_id, (start, end) in spans.items():
        on_site = [(w, s, e) for site, w, s, e in rows if site == site_id]
        dates = {}
        day = start
        while day <= end:
            dates[day] = {w for w, s, e in on_site if s <= day and (e is None or e >= day)}
            day += ONE_DAY
        result[site_id] = dates
    return result


def assignment_on(worker: Worker, date: datetime.date) -> SiteAssignment | None:
    """The worker's assignment covering ``date`` (the latest one if several overlap)."""
    return worker.assignments.active_on(date).select_related("site").order_by("-start_date").first()


def current_or_next_assignment(worker: Worker, date: datetime.date) -> SiteAssignment | None:
    """The assignment covering ``date``, else the next one that starts after it."""
    found = assignment_on(worker, date)
    if found is None:
        found = (
            worker.assignments.filter(start_date__gt=date)
            .select_related("site")
            .order_by("start_date")
            .first()
        )
    return found


# --- assignments ------------------------------------------------------------------------


def assign(
    worker: Worker,
    site: Site,
    start: datetime.date,
    end: datetime.date | None = None,
    *,
    actor=None,
) -> SiteAssignment:
    """Put ``worker`` on ``site`` from ``start`` (to ``end``, inclusive, if given).

    The worker's open assignment (no end date) is closed the day before ``start``. An
    open assignment that would only start on or after ``start`` on another site is
    replaced. Already open on the same site since ``start`` or earlier: it is kept (and
    given ``end`` if one is passed). Overlapping a closed assignment is refused.
    """
    if end is not None and end < start:
        raise ValidationError({"end": _("The end date must be on or after the start date.")})
    if worker.company_id != site.company_id:
        raise ValidationError(_("This worker belongs to another company."))

    with transaction.atomic():
        for current in worker.assignments.filter(end_date__isnull=True).order_by("start_date"):
            if current.site_id == site.pk and current.start_date <= start:
                if end is not None:
                    before = _assignment_snapshot(current)
                    current.end_date = end
                    current.save(update_fields=["end_date", "updated_at"])
                    AuditLog.record(
                        actor=actor,
                        action="worksites.assignment_close",
                        target=current,
                        before=before,
                        after=_assignment_snapshot(current),
                    )
                return current
            before = _assignment_snapshot(current)
            if current.start_date < start:
                current.end_date = start - ONE_DAY
                current.save(update_fields=["end_date", "updated_at"])
                AuditLog.record(
                    actor=actor,
                    action="worksites.assignment_close",
                    target=current,
                    before=before,
                    after=_assignment_snapshot(current),
                )
            else:
                AuditLog.record(
                    actor=actor,
                    action="worksites.assignment_remove",
                    target=current,
                    before=before,
                )
                current.delete()

        overlapping = worker.assignments.filter(Q(end_date__isnull=True) | Q(end_date__gte=start))
        if end is not None:
            overlapping = overlapping.filter(start_date__lte=end)
        if overlapping.exists():
            raise ValidationError(
                {"start": _("This worker is already assigned to a site for part of that period.")}
            )

        assignment = SiteAssignment.objects.create(
            worker=worker, site=site, start_date=start, end_date=end
        )
        AuditLog.record(
            actor=actor,
            action="worksites.assignment_create",
            target=assignment,
            after=_assignment_snapshot(assignment),
        )
    return assignment


def end_assignment(assignment: SiteAssignment, end: datetime.date, *, actor=None):
    """Take a worker off a site's crew after ``end`` (inclusive)."""
    if end < assignment.start_date:
        raise ValidationError({"end": _("The end date must be on or after the start date.")})
    with transaction.atomic():
        before = _assignment_snapshot(assignment)
        assignment.end_date = end
        assignment.save(update_fields=["end_date", "updated_at"])
        AuditLog.record(
            actor=actor,
            action="worksites.assignment_close",
            target=assignment,
            before=before,
            after=_assignment_snapshot(assignment),
        )
    return assignment


# --- sites ------------------------------------------------------------------------------


def settings_snapshot(site: Site) -> dict:
    return {name: _jsonable(getattr(site, name)) for name in sorted(SITE_OVERRIDABLE)}


def update_site_settings(site: Site, changes: dict, *, actor=None) -> Site:
    """Save the site's setting overrides (``None`` = inherit the company value)."""
    unknown = set(changes) - SITE_OVERRIDABLE
    if unknown:
        raise KeyError(", ".join(sorted(unknown)))
    with transaction.atomic():
        before = settings_snapshot(site)
        for name, value in changes.items():
            setattr(site, name, None if value == "" else value)
        site.full_clean()
        site.save()
        after = settings_snapshot(site)
        if after != before:
            AuditLog.record(
                actor=actor,
                action="worksites.settings_update",
                target=site,
                before=before,
                after=after,
            )
    return site


def save_site(site: Site, *, actor=None, foremen=None) -> Site:
    """Create or update a site's identity (name, address, people, budget).

    The manager and the foremen must be people of the site's company.
    """
    creating = site.pk is None
    people = [site.manager] if site.manager_id else []
    people += list(foremen or [])
    if any(person.company_id != site.company_id for person in people):
        raise ValidationError(_("Site managers and foremen must belong to the site's company."))
    with transaction.atomic():
        site.save()
        if foremen is not None:
            site.foremen.set(foremen)
        AuditLog.record(
            actor=actor,
            action="worksites.site_create" if creating else "worksites.site_update",
            target=site,
            after={
                "name": site.name,
                "status": site.status,
                "manager_id": site.manager_id,
                "foremen": sorted(site.foremen.values_list("pk", flat=True)),
                "labour_budget": site.labour_budget,
            },
        )
    return site


def annotate_today(sites, date: datetime.date):
    """Add ``assigned_count`` and ``present_count`` (on ``date``) to a Site queryset."""
    assigned = (
        active_assignments(date)
        .filter(site=OuterRef("pk"))
        .order_by()
        .values("site")
        .annotate(n=Count("worker", distinct=True))
        .values("n")
    )
    present = (
        AttendanceRecord.objects.filter(
            day__site=OuterRef("pk"), day__date=date, status__in=PRESENT_STATUSES
        )
        .order_by()
        .values("day__site")
        .annotate(n=Count("pk"))
        .values("n")
    )
    return sites.annotate(
        assigned_count=Coalesce(Subquery(assigned), 0),
        present_count=Coalesce(Subquery(present), 0),
    )


def status_counts(sites) -> dict[str, int]:
    counts = dict.fromkeys(Site.Status.values, 0)
    for row in sites.order_by().values("status").annotate(n=Count("pk")):
        counts[row["status"]] = row["n"]
    return counts


@dataclass
class DaySummary:
    date: datetime.date
    status: str  # an AttendanceDay status, or "not_started" / "upcoming"
    present: int | None
    crew: int
    is_today: bool


def week_strip(site: Site, today: datetime.date, company: CompanySettings | None = None):
    """The working days of the current week (Monday first) with each day's roll-call status."""
    company = company or CompanySettings.load()
    working_days = set(company.working_days or [])
    monday = today - datetime.timedelta(days=today.weekday())
    dates = [
        monday + datetime.timedelta(days=i)
        for i in range(7)
        if (monday + datetime.timedelta(days=i)).isoweekday() in working_days
    ]
    days = {
        day.date: day
        for day in AttendanceDay.objects.filter(site=site, date__in=dates).annotate(
            present=Count("records", filter=Q(records__status__in=PRESENT_STATUSES))
        )
    }
    crews = crew_ids_by_date(site, dates[0], dates[-1]) if dates else {}
    summaries = []
    for date in dates:
        day = days.get(date)
        crew = len(crews[date])
        if day is not None:
            summaries.append(DaySummary(date, day.status, day.present, crew, date == today))
        elif date > today:
            summaries.append(DaySummary(date, "upcoming", None, crew, False))
        else:
            summaries.append(DaySummary(date, "not_started", None, crew, date == today))
    return summaries


@dataclass
class ResolvedSetting:
    name: str
    value: object
    overridden: bool


def site_setting(site: Site, name: str, company: CompanySettings) -> ResolvedSetting:
    """A setting's effective value on ``site`` and whether the site overrides it."""
    own = getattr(site, name) if name in SITE_OVERRIDABLE else None
    return ResolvedSetting(
        name=name,
        value=resolve_setting(name, site=site, company=company),
        overridden=own is not None and own != "",
    )
