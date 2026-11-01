"""Nightly reconciliation: correct a past day's presence sessions with the tracker's replay.

Live events can be wrong when data arrived late (a phone offline, a gateway buffering:
``docs/presence-tracking.md`` §5 "Late data", §11). The tracker keeps every observation in
its track and can replay a day through the same state machine (``presence.replay``,
``GET /internal/sites/{id}/sessions``, read by ``tracker_client``). For each tracked site
and each past local day not reconciled yet, this module fetches the replayed sessions and
makes Django's sessions of that day (``PresenceSession.local_date``) match them.

When it runs: beat starts ``presence.reconcile_sessions`` once a night at a fixed time
(``config/settings.py``). Companies have their own time zones, so each company's days are
worked out here: the last ``CATCH_UP_DAYS`` local days that ended at least
``SETTLE_TIME`` ago (late uploads and the tracker's ``absence_to_leave`` have run out),
minus those already reconciled. Normally that is "yesterday"; a night that failed (the
tracker down, an error answer) or was missed is caught up the next night.
``manage.py reconcile_presence [--site ID] [--date YYYY-MM-DD]`` runs it by hand; with
``--date`` that day is reconciled again even if it was already.

Correction rules, per person and site, over the day's sessions (Django's and the
tracker's, as time intervals, open ones running on):

- Overlapping sessions are grouped; inside a group, Django's and the tracker's are paired
  in time order. A paired session takes the tracker's start, end and zones (a visit is
  extended, shortened or closed); a tracker session left over is added (a missing visit,
  or one a gateway gap split in two); a Django session left over is removed (a visit the
  replay does not support, or two visits the replay sees as one).
- A tracker session of a subject the site's company does not know is ignored (counted).
- An empty replay for a day with sessions changes nothing and is recorded as a failure:
  it means a lost track (tracker data lost or pruned), not a day nobody came.

Afterwards every session of the day is ``source = replay``, and the day's bounds are kept
on its ``PresenceReconciliation``: the rebuild from live events
(``services.rebuild_sessions``) no longer folds that day's events nor replaces its
sessions, so a late live event cannot undo the correction. A visit across midnight is one
session per day in the replay (the tracker cuts it), so it is after reconciliation too.

Attendance is separate: roll-call suggestions accepted into attendance are attendance
records (audited there) and are never read or written here. Presence is not attendance,
so there is no audit entry per session; each run writes one ``presence.reconcile`` entry
per company with the totals (``apps.precompute`` ignores ``presence.*`` entries).

Each site's day is one short transaction (SQLite); a failure is recorded on the day's
``PresenceReconciliation`` and never stops the other sites. Once the tracker is
unreachable, the run stops calling it and records the remaining days as failed.
"""

from __future__ import annotations

import datetime
import logging
from collections import defaultdict
from dataclasses import dataclass, field

from celery.exceptions import SoftTimeLimitExceeded
from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import transaction
from django.db.models import F
from django.utils import timezone

from apps.core.dates import company_tz
from apps.core.models import AuditLog, Company, CompanySettings
from apps.core.settings import resolve_setting
from apps.core.tenancy import use_company
from apps.workers.models import Worker
from apps.worksites.models import Site

from . import selectors, tracker_client
from .models import PresenceReconciliation, PresenceSession, SubjectKind
from .tracker_client import TrackedSession

logger = logging.getLogger(__name__)

# How long after a local day ends before it is reconciled.
SETTLE_TIME = datetime.timedelta(hours=2)
# How many past days a night looks at (catch-up after failed or missed nights).
CATCH_UP_DAYS = 7

AUDIT_ACTION = "presence.reconcile"
_FAR = datetime.datetime.max.replace(tzinfo=datetime.UTC)
REPLAY = PresenceSession.Source.REPLAY


# --- which days ----------------------------------------------------------------------------


def tz_name(company: CompanySettings) -> str:
    """The IANA name sent to the tracker (``UTC`` when the setting is empty or unknown)."""
    return getattr(company_tz(company), "key", "UTC")


def is_settled(day: datetime.date, company: CompanySettings, *, now) -> bool:
    """Whether ``day`` (company time zone) ended at least ``SETTLE_TIME`` before ``now``."""
    _start, end = tracker_client.local_day_bounds(day, company_tz(company))
    return now - end >= SETTLE_TIME


def is_kept(day: datetime.date, company: CompanySettings, *, now) -> bool:
    """Whether the tracker still has the whole track of ``day``: it deletes points older
    than ``track_retention_days`` (a partial track would remove real visits)."""
    start, _end = tracker_client.local_day_bounds(day, company_tz(company))
    retention = int(resolve_setting("track_retention_days", company=company))
    return start >= now - datetime.timedelta(days=retention)


def due_days(company: CompanySettings, *, now) -> list[datetime.date]:
    """The days a night looks at, oldest first: the last ``CATCH_UP_DAYS`` settled local
    days whose track the tracker still keeps (``is_kept``)."""
    latest = now.astimezone(company_tz(company)).date() - datetime.timedelta(days=1)
    if not is_settled(latest, company, now=now):
        latest -= datetime.timedelta(days=1)
    days = [latest - datetime.timedelta(days=n) for n in reversed(range(CATCH_UP_DAYS))]
    return [day for day in days if is_kept(day, company, now=now)]


# --- the correction, without the database ------------------------------------------------


@dataclass
class DayPlan:
    """What to do to a site's day: sessions to create, update (with their new values) and
    delete, and the counts recorded for it."""

    create: list[dict] = field(default_factory=list)
    update: list[tuple[PresenceSession, dict]] = field(default_factory=list)
    delete: list[PresenceSession] = field(default_factory=list)
    added: int = 0
    changed: int = 0
    removed: int = 0
    unchanged: int = 0


def _end(value) -> datetime.datetime:
    return _FAR if value is None else value


def _groups(tracked: list[TrackedSession], stored: list[PresenceSession]) -> list[tuple]:
    """One person's sessions in groups that overlap in time (an open one runs on),
    as ``(tracker sessions, Django sessions)``, each in time order."""
    items = [(t.started_at, _end(t.ended_at), 0, i) for i, t in enumerate(tracked)]
    items += [(s.started_at, _end(s.ended_at), 1, i) for i, s in enumerate(stored)]
    items.sort(key=lambda item: (item[0], item[2], item[3]))
    groups: list[tuple[list, list]] = []
    reach = None
    for start, end, side, index in items:
        if not groups or start > reach:
            groups.append(([], []))
            reach = end
        else:
            reach = max(reach, end)
        groups[-1][side].append(tracked[index] if side == 0 else stored[index])
    return groups


def _values(tracked: TrackedSession, existing: PresenceSession | None, tz) -> dict:
    """A session's fields from the replay. The end reason is kept from a live session
    that had an end; the replay does not say why a visit ended."""
    keep_reason = existing is not None and existing.ended_at is not None
    return {
        "local_date": tracked.started_at.astimezone(tz).date(),
        "started_at": tracked.started_at,
        "ended_at": tracked.ended_at,
        "end_reason": existing.end_reason if keep_reason and tracked.ended_at else "",
        "zones": list(tracked.zones),
        "last_event_at": tracked.ended_at or tracked.started_at,
        "source": REPLAY,
    }


def plan_day(tracked: list[TrackedSession], stored: list[PresenceSession], tz) -> DayPlan:
    """How to make ``stored`` (Django's sessions of a site's day) match ``tracked``."""
    plan = DayPlan()
    people: dict[tuple, tuple[list, list]] = defaultdict(lambda: ([], []))
    for session in tracked:
        people[(session.subject_kind, session.subject_id)][0].append(session)
    for session in stored:
        people[(session.subject_kind, session.subject_id)][1].append(session)
    for (kind, subject_id), (theirs, ours) in sorted(people.items()):
        theirs.sort(key=lambda s: s.started_at)
        ours.sort(key=lambda s: (s.started_at, s.pk))
        for group_theirs, group_ours in _groups(theirs, ours):
            for index in range(max(len(group_theirs), len(group_ours))):
                replayed = group_theirs[index] if index < len(group_theirs) else None
                current = group_ours[index] if index < len(group_ours) else None
                if current is None:
                    values = _values(replayed, None, tz)
                    plan.create.append({"subject_kind": kind, "subject_id": subject_id, **values})
                    plan.added += 1
                elif replayed is None:
                    plan.delete.append(current)
                    plan.removed += 1
                else:
                    values = _values(replayed, current, tz)
                    same_visit = (current.started_at, current.ended_at, list(current.zones)) == (
                        values["started_at"],
                        values["ended_at"],
                        values["zones"],
                    )
                    if same_visit:
                        plan.unchanged += 1
                    else:
                        plan.changed += 1
                    if any(getattr(current, name) != value for name, value in values.items()):
                        plan.update.append((current, values))
    return plan


# --- one site's day ----------------------------------------------------------------------


@dataclass(frozen=True)
class Outcome:
    site_id: int
    company_id: int
    date: datetime.date
    ok: bool
    added: int = 0
    changed: int = 0
    removed: int = 0
    unchanged: int = 0
    ignored: int = 0
    error: str = ""
    unreachable: bool = False


def _known_subjects(sessions, company_id: int) -> set[tuple[str, int]]:
    wanted: dict[str, set[int]] = defaultdict(set)
    for session in sessions:
        wanted[session.subject_kind].add(session.subject_id)
    known = set()
    if wanted[SubjectKind.WORKER]:
        ids = Worker.objects.filter(pk__in=wanted[SubjectKind.WORKER], company_id=company_id)
        known |= {(SubjectKind.WORKER.value, pk) for pk in ids.values_list("pk", flat=True)}
    if wanted[SubjectKind.FOREMAN]:
        ids = get_user_model().objects.filter(
            pk__in=wanted[SubjectKind.FOREMAN], company_id=company_id
        )
        known |= {(SubjectKind.FOREMAN.value, pk) for pk in ids.values_list("pk", flat=True)}
    return known


def _record_failure(site: Site, day: datetime.date, error: str, now) -> None:
    try:
        with transaction.atomic():
            record, _created = PresenceReconciliation.objects.get_or_create(
                site=site, local_date=day, defaults={"company_id": site.company_id}
            )
            PresenceReconciliation.objects.filter(pk=record.pk).update(
                last_attempt_at=now, last_error=error[:300], attempts=F("attempts") + 1
            )
    except SoftTimeLimitExceeded:
        raise
    except Exception:  # the database itself failing: logged, tried again next night
        logger.exception("Presence: could not record the failed reconciliation of %s", site.pk)


def reconcile_site_day(
    site: Site,
    day: datetime.date,
    *,
    company: CompanySettings | None = None,
    now: datetime.datetime | None = None,
) -> Outcome:
    """Fetch the tracker's replay of ``site``'s ``day`` and correct the stored sessions.

    Never raises (but for the task's soft time limit): a failure is recorded on the day's
    ``PresenceReconciliation`` and returned (``ok = False``)."""
    company = company or CompanySettings.load(site.company_id)
    now = now or timezone.now()
    tz = company_tz(company)
    base = {"site_id": site.pk, "company_id": site.company_id, "date": day}
    try:
        replay = tracker_client.fetch_site_sessions(site.pk, day, tz, tz_name(company))
    except tracker_client.TrackerReadError as exc:
        unreachable = isinstance(exc, tracker_client.TrackerUnreachable)
        logger.warning("Presence: reconciling site %s on %s failed: %s", site.pk, day, exc)
        _record_failure(site, day, str(exc), now)
        return Outcome(**base, ok=False, error=str(exc)[:300], unreachable=unreachable)
    try:
        return _apply(site, day, replay, tz, now, base)
    except SoftTimeLimitExceeded:  # the task's time is up: stop the run, not just this day
        raise
    except Exception as exc:
        logger.exception("Presence: reconciling site %s on %s failed", site.pk, day)
        error = f"{type(exc).__name__}: {exc}"[:300]
        _record_failure(site, day, error, now)
        return Outcome(**base, ok=False, error=error)


def _apply(site, day, replay: tracker_client.SessionDay, tz, now, base) -> Outcome:
    known = _known_subjects(replay.sessions, site.company_id)
    tracked, ignored = [], 0
    for session in replay.sessions:
        on_day = session.started_at.astimezone(tz).date() == day
        if (session.subject_kind, session.subject_id) in known and on_day:
            tracked.append(session)
        else:
            ignored += 1
    with transaction.atomic():
        stored = list(
            PresenceSession.objects.filter(
                site=site, company_id=site.company_id, local_date=day
            ).order_by("started_at", "pk")
        )
        if not replay.sessions and stored:
            error = (
                f"The tracker replayed no visit for a day with {len(stored)} session(s): "
                "its track is probably lost or pruned. Nothing changed."
            )
        else:
            error = ""
            counts = _write(site, day, replay, plan_day(tracked, stored, tz), ignored, now)
    if error:
        logger.warning("Presence: reconciling site %s on %s: %s", site.pk, day, error)
        _record_failure(site, day, error, now)
        return Outcome(**base, ok=False, error=error)
    logger.info("Presence: reconciled site %s on %s: %s", site.pk, day, counts)
    return Outcome(**base, ok=True, **counts)


def _write(site, day, replay, plan: DayPlan, ignored: int, now) -> dict:
    """Apply ``plan`` and record the day as reconciled (inside the caller's transaction)."""
    if plan.delete:
        PresenceSession.objects.filter(pk__in=[s.pk for s in plan.delete]).delete()
    for session, values in plan.update:
        for name, value in values.items():
            setattr(session, name, value)
        session.save(update_fields=list(values))
    PresenceSession.objects.bulk_create(
        PresenceSession(company_id=site.company_id, site=site, event_count=0, **values)
        for values in plan.create
    )
    counts = {
        "added": plan.added,
        "changed": plan.changed,
        "removed": plan.removed,
        "unchanged": plan.unchanged,
        "ignored": ignored,
    }
    record, _created = PresenceReconciliation.objects.get_or_create(
        site=site, local_date=day, defaults={"company_id": site.company_id}
    )
    PresenceReconciliation.objects.filter(pk=record.pk).update(
        window_start=replay.window_start,
        window_end=replay.window_end,
        reconciled_at=now,
        last_attempt_at=now,
        last_error="",
        attempts=F("attempts") + 1,
        **counts,
    )
    return counts


# --- a run ---------------------------------------------------------------------------------


@dataclass
class RunSummary:
    outcomes: list[Outcome] = field(default_factory=list)
    skipped: list[tuple[int, str]] = field(default_factory=list)  # (site id, why)
    configured: bool = True

    @property
    def reconciled(self) -> int:
        return sum(outcome.ok for outcome in self.outcomes)

    @property
    def failed(self) -> int:
        return sum(not outcome.ok for outcome in self.outcomes)


def _audit(company: Company, outcomes: list[Outcome]) -> None:
    done = [outcome for outcome in outcomes if outcome.ok]
    after = {
        "days": sorted({outcome.date.isoformat() for outcome in outcomes}),
        "sites": len({outcome.site_id for outcome in outcomes}),
        "reconciled": len(done),
        "failed": len(outcomes) - len(done),
        **{
            name: sum(getattr(outcome, name) for outcome in done)
            for name in ("added", "changed", "removed", "ignored")
        },
    }
    try:
        AuditLog.record(actor=None, action=AUDIT_ACTION, company=company, after=after)
    except SoftTimeLimitExceeded:
        raise
    except Exception:
        logger.exception("Presence: could not audit the reconciliation of company %s", company.pk)


def run(
    *,
    site_ids=None,
    day: datetime.date | None = None,
    now: datetime.datetime | None = None,
) -> RunSummary:
    """Reconcile every tracked site of every active company (or the sites ``site_ids``).

    Without ``day``: each company's ``due_days`` not reconciled yet. With ``day``: that
    day again, wherever it is over (``is_settled``).
    """
    summary = RunSummary()
    if not settings.TRACKER_URL:
        logger.info("Presence: TRACKER_URL is not set; sessions not reconciled")
        summary.configured = False
        return summary
    now = now or timezone.now()
    down = ""  # the tracker's error once it is unreachable: the other days are not tried
    for company in Company.objects.filter(is_active=True).order_by("pk"):
        sites = Site.objects.filter(company=company).order_by("pk")
        if site_ids is not None:
            sites = sites.filter(pk__in=list(site_ids))
        sites = list(sites)
        if not sites:
            continue
        outcomes: list[Outcome] = []
        with use_company(company):
            settings_ = CompanySettings.load(company.pk)
            for site in sites:
                if not selectors.is_tracked(site, settings_):
                    summary.skipped.append((site.pk, "tracking is off"))
                    continue
                if day is not None:
                    if not is_settled(day, settings_, now=now):
                        summary.skipped.append((site.pk, f"{day} is not over yet"))
                        continue
                    if not is_kept(day, settings_, now=now):
                        summary.skipped.append((site.pk, f"the track of {day} is not kept"))
                        continue
                    days = [day]
                else:
                    done = set(
                        PresenceReconciliation.objects.filter(
                            site=site, reconciled_at__isnull=False
                        ).values_list("local_date", flat=True)
                    )
                    days = [d for d in due_days(settings_, now=now) if d not in done]
                for target in days:
                    if down:
                        _record_failure(site, target, down, now)
                        outcome = Outcome(
                            site_id=site.pk,
                            company_id=company.pk,
                            date=target,
                            ok=False,
                            error=down,
                            unreachable=True,
                        )
                    else:
                        outcome = reconcile_site_day(site, target, company=settings_, now=now)
                        if outcome.unreachable:
                            down = outcome.error
                    outcomes.append(outcome)
            if outcomes:
                _audit(company, outcomes)
        summary.outcomes.extend(outcomes)
    logger.info(
        "Presence: reconciliation done: %s day(s) reconciled, %s failed, %s site(s) skipped",
        summary.reconciled,
        summary.failed,
        len(summary.skipped),
    )
    return summary
