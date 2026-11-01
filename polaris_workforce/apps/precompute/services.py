"""The precompute service: rebuild stored figures only where something changed.

Heavy figures (pay recomputed from attendance, and the snapshots built from it) are
rebuilt per site, over the dates a change made out of date:

1. A write marks what it changed (``mark``), in its own transaction. Every change to
   attendance or pay writes an audit entry, and ``signals.py`` marks from it
   (``marks.ranges_for``), so the services need not remember to.
2. Once the change commits, a Celery worker rebuilds the site (``tasks.rebuild_site``,
   queued once per site however many marks come in). The periodic run (Celery beat every
   5 minutes, or ``manage.py refresh_precomputed``) rebuilds any site still marked, and
   every active site once a day, since pay depends on the date as well (``Freshness``).
   The nightly sweep rebuilds every open period and records what changed although nobody
   marked it (``RefreshRun.mismatches``).
3. A screen that reads the figures calls ``ensure_fresh`` first: a site still marked for
   the dates it shows is rebuilt now, before the read. Otherwise the read costs nothing.

What a rebuild does is a list of steps (``register``): pay first (``apps.payroll``),
then the snapshot tables. Each step gets the site and the dates, ``start`` ``None``
meaning every open pay period.
"""

from __future__ import annotations

import datetime
import logging
import time
from collections.abc import Callable

from django.conf import settings
from django.contrib.contenttypes.models import ContentType
from django.db import transaction
from django.db.models import F, Q
from django.utils import timezone

from apps.core.dates import company_today
from apps.core.models import AuditLog, CompanySettings
from apps.payroll.models import PayPeriod
from apps.worksites.models import Site

from . import marks
from .models import Freshness, RefreshRun, ReportMonth, Stale

logger = logging.getLogger("polaris.precompute")

# A new day rebuilds this far back when the site was never rebuilt (the periods that may
# have become ready). Daily pay needs one day; the longest period is a month.
NEW_DAY_LOOKBACK = 31

Step = Callable[..., None]  # step(site, start, end, *, today, company)
_STEPS: list[tuple[int, Step]] = []


def register(step: Step, *, order: int = 100) -> Step:
    """Add a rebuild step (pay is 0; snapshots built from pay come after)."""
    if all(existing is not step for _order, existing in _STEPS):
        _STEPS.append((order, step))
        _STEPS.sort(key=lambda item: item[0])
    return step


def steps() -> list[Step]:
    return [step for _order, step in _STEPS]


# --- marking ------------------------------------------------------------------------------


def mark(site_id: int, start: datetime.date | None, end: datetime.date | None = None) -> None:
    """``site_id`` is out of date from ``start`` (``None``: every open period) to ``end``
    (``None``: today). Call it inside the transaction that makes the change: if that
    rolls back, so does the mark. Marks of one site merge into one row."""
    end = end or _today_of_site(site_id)
    if start is not None and start > end:
        end = start
    # The writer's transaction already holds SQLite's write lock (its audit entry is
    # written), so nobody changes the row between this read and the update.
    now = timezone.now()
    stale = Stale.objects.filter(site_id=site_id).first()
    if stale is None:
        queue = _queueing()
        Stale.objects.create(
            site_id=site_id, start=start, end=end, queued_at=now if queue else None
        )
        if queue:
            queue_rebuild(site_id)
        return
    # One rebuild for many marks: queue again only if none was queued, or long ago (lost).
    requeue = _queueing() and (
        stale.queued_at is None
        or stale.queued_at
        < now - datetime.timedelta(minutes=settings.PRECOMPUTE_REQUEUE_AFTER_MINUTES)
    )
    Stale.objects.filter(pk=stale.pk).update(
        start=None if stale.start is None or start is None else min(stale.start, start),
        end=max(stale.end, end),
        version=F("version") + 1,
        updated_at=now,
        **({"queued_at": now} if requeue else {}),
    )
    if requeue:
        queue_rebuild(site_id)


def _queueing() -> bool:
    """Rebuilds are queued for a worker only when a broker is set (``CELERY_BROKER_URL``)."""
    return bool(settings.CELERY_BROKER_URL)


def queue_rebuild(site_id: int) -> None:
    """Have a worker rebuild the site once the current transaction commits (at once
    outside one), after ``PRECOMPUTE_REBUILD_DELAY`` seconds so that the marks of the
    next few writes ride along. A broker that is down never fails the write: the
    periodic run and the screens rebuild the site anyway."""
    transaction.on_commit(lambda: _send_rebuild(site_id))


# After a failed send, rebuilds are not queued for this long (per process): a change that
# marks many sites (company settings) must not wait on a dead broker once per site.
BROKER_BACKOFF_SECONDS = 60
_broker_down_until = 0.0


def _send_rebuild(site_id: int) -> None:
    from .tasks import rebuild_site  # tasks import this module

    global _broker_down_until
    if time.monotonic() < _broker_down_until:
        return  # the periodic run and the screens rebuild the site
    try:
        rebuild_site.apply_async((site_id,), countdown=settings.PRECOMPUTE_REBUILD_DELAY)
    except Exception:  # the broker is unreachable: kombu, redis and socket errors alike
        _broker_down_until = time.monotonic() + BROKER_BACKOFF_SECONDS
        logger.warning(
            "Precompute: could not queue the rebuild of site %s; not queueing for %ss",
            site_id,
            BROKER_BACKOFF_SECONDS,
            exc_info=True,
        )


def mark_company(company_id: int) -> None:
    """Every site of the company is out of date, every open period (a rule changed)."""
    for site_id in _sites_with_pay(company_id).values_list("pk", flat=True):
        mark(site_id, None)


def _sites_with_pay(company_id: int):
    """The company's active sites, and any other with an open pay period."""
    open_periods = PayPeriod.objects.exclude(status=PayPeriod.Status.LOCKED)
    return Site.objects.filter(company_id=company_id).filter(
        Q(status=Site.Status.ACTIVE) | Q(pk__in=open_periods.values("site_id"))
    )


def mark_from_audit(entry: AuditLog) -> None:
    """Mark what an audit entry says changed (``marks.ranges_for``)."""
    found = marks.ranges_for(entry)
    if found is None:
        return
    if isinstance(found, marks.Company):
        mark_company(found.company_id)
        return
    for item in found:
        mark(item.site_id, item.start, item.end)


# A recomputation that changed a period's lines says so (``payroll[.auto].period.refresh``):
# not an input of pay, but of the report rollups of the month the period ends in.
PERIOD_REFRESH_ACTIONS = ("payroll.period.refresh", "payroll.auto.period.refresh")


# A worker's profile (their trade) changed: the report rows of the months not locked yet
# are rebuilt with the new one (locked months keep the trade of their time).
WORKER_CHANGE_ACTIONS = ("workers.update", "admin.workers.worker.change")


def flag_report_month(entry: AuditLog) -> None:
    """The report rollups that ``entry`` makes out of date: the month a changed pay
    period ends in, or the unlocked months of a changed worker's sites. The report reads
    them live until the rebuild step builds them again; a build that started before the
    flag does not clear it (``generation``)."""
    if not entry.object_id:
        return
    months = ReportMonth.objects.none()
    if entry.action in PERIOD_REFRESH_ACTIONS:
        period = (
            PayPeriod.objects.filter(pk=entry.object_id).values_list("site_id", "end_date").first()
        )
        if period is not None:
            site_id, end_date = period
            months = ReportMonth.objects.filter(site_id=site_id, month=end_date.replace(day=1))
    elif entry.action in WORKER_CHANGE_ACTIONS:
        sites = marks.worker_sites(int(entry.object_id), None)
        months = ReportMonth.objects.filter(site_id__in=sites, locked=False)
    months.update(stale=True, generation=F("generation") + 1)


def _today_of_site(site_id: int) -> datetime.date:
    company_id = Site.objects.filter(pk=site_id).values_list("company_id", flat=True).first()
    return company_today(CompanySettings.load(company_id))


# --- rebuilding ---------------------------------------------------------------------------


def _overlaps(stale: Stale, start: datetime.date | None, end: datetime.date | None) -> bool:
    if stale.start is None or start is None or end is None:
        return True
    return stale.start <= end and stale.end >= start


def ensure_fresh(
    site: Site,
    start: datetime.date | None = None,
    end: datetime.date | None = None,
    *,
    today: datetime.date | None = None,
    company: CompanySettings | None = None,
    state: tuple[Stale | None, datetime.date | None] | None = None,
) -> bool:
    """Rebuild ``site`` now if what a screen is about to read is out of date: a mark that
    covers ``start``–``end`` (``None``: any date), or a new day since the last rebuild.
    True if it rebuilt. ``state``: the site's, when read with others (``states``)."""
    company = company or CompanySettings.load(site.company_id)
    today = today or company_today(company)
    stale, fresh_on = state if state is not None else states([site.pk])[site.pk]
    if fresh_on == today and (stale is None or not _overlaps(stale, start, end)):
        return False
    refresh_site(site, today=today, company=company, stale=stale, fresh_on=fresh_on)
    return True


def states(site_ids) -> dict[int, tuple[Stale | None, datetime.date | None]]:
    """``(mark, fresh_on)`` of each site, read together (a list screen checks every site
    it shows: pass each one's to ``ensure_fresh(state=…)``)."""
    site_ids = list(site_ids)
    stale_rows = {row.site_id: row for row in Stale.objects.filter(site_id__in=site_ids)}
    fresh = dict(Freshness.objects.filter(site_id__in=site_ids).values_list("site_id", "fresh_on"))
    return {pk: (stale_rows.get(pk), fresh.get(pk)) for pk in site_ids}


def refresh_site(
    site: Site,
    *,
    today: datetime.date | None = None,
    company: CompanySettings | None = None,
    stale: Stale | None = None,
    fresh_on: datetime.date | None = None,
    everything: bool = False,
) -> None:
    """Run every step over what is out of date on ``site``, then clear its mark (unless
    a newer one came in meanwhile) and note the day. ``everything``: every open period."""
    company = company or CompanySettings.load(site.company_id)
    today = today or company_today(company)
    if stale is None:
        stale = Stale.objects.filter(site=site).first()
    if fresh_on is None:
        fresh_on = Freshness.objects.filter(site=site).values_list("fresh_on", flat=True).first()

    for start, end in _ranges_to_rebuild(stale, fresh_on, today, everything):
        for step in steps():
            step(site, start, end, today=today, company=company)

    with transaction.atomic():
        if stale is not None:
            Stale.objects.filter(pk=stale.pk, version=stale.version).delete()
        Freshness.objects.update_or_create(site=site, defaults={"fresh_on": today})


def _ranges_to_rebuild(stale, fresh_on, today, everything) -> list[tuple]:
    """The ``(start, end)`` ranges to rebuild, apart unless they overlap (a mark a year
    ago and a new day today are two small rebuilds, not every day in between)."""
    if everything:
        return [(None, today)]
    ranges = []
    if stale is not None:
        ranges.append((stale.start, stale.end))
    if fresh_on != today:
        # A new day: the periods whose pay day came since the last rebuild may be ready.
        since = fresh_on if fresh_on is not None and fresh_on < today else None
        ranges.append((since or today - datetime.timedelta(days=NEW_DAY_LOOKBACK), today))
    if len(ranges) == 2:
        (first_start, first_end), (second_start, second_end) = ranges
        if first_start is None or first_start <= second_end and first_end >= second_start:
            starts = (first_start, second_start)
            start = None if None in starts else min(starts)
            return [(start, max(first_end, second_end))]
    return ranges


# --- the cron command ---------------------------------------------------------------------


def process(
    company_id: int, *, today: datetime.date | None = None, failures: list | None = None
) -> int:
    """Rebuild every marked site of the company, and every active site not rebuilt today.
    Returns the number of sites rebuilt. A site that fails is logged (and added to
    ``failures``) and left marked; the others are rebuilt all the same."""
    company = CompanySettings.load(company_id)
    today = today or company_today(company)
    marked = Stale.objects.filter(site__company_id=company_id).values("site_id")
    fresh = Freshness.objects.filter(fresh_on=today).values("site_id")
    sites = Site.objects.filter(company_id=company_id).filter(
        Q(pk__in=marked) | (Q(status=Site.Status.ACTIVE) & ~Q(pk__in=fresh))
    )
    count = 0
    for site in sites.order_by("pk"):
        if _safely(refresh_site, site, failures, today=today, company=company):
            count += 1
    return count


def _safely(rebuild, site: Site, failures: list | None, **kwargs) -> bool:
    """Run ``rebuild(site, **kwargs)``; on an error, log it and carry on with the next site."""
    try:
        rebuild(site, **kwargs)
    except Exception as error:  # one site's bad data must not stop every other site
        logger.exception("Precompute: the rebuild of site %s failed", site.pk)
        if failures is not None:
            failures.append({"site": site.pk, "error": f"{type(error).__name__}: {error}"})
        return False
    return True


def run(company_id: int, *, sweep: bool = False, today: datetime.date | None = None) -> RefreshRun:
    """One ``refresh_precomputed`` run for a company (logged as a ``RefreshRun``).

    The sweep first rebuilds what is marked, then every open period of every site with
    pay: anything that still changes was changed without a mark (a write that bypassed
    the services and the audit log). It is fixed, and listed in ``mismatches``.
    """
    company = CompanySettings.load(company_id)
    today = today or company_today(company)
    record = RefreshRun.objects.create(company_id=company_id, sweep=sweep)
    record.sites = process(company_id, today=today, failures=record.mismatches)
    if sweep:
        period_type = ContentType.objects.get_for_model(PayPeriod)
        for site in _sites_with_pay(company_id).order_by("pk"):
            last = AuditLog.objects.order_by("-pk").values_list("pk", flat=True).first() or 0
            if not _safely(
                refresh_site,
                site,
                record.mismatches,
                today=today,
                company=company,
                everything=True,
            ):
                continue
            # What the system recomputed on this site's periods (other sites may be
            # rebuilt meanwhile, by the worker or a screen).
            periods = [
                str(pk) for pk in PayPeriod.objects.filter(site=site).values_list("pk", flat=True)
            ]
            changed = list(
                AuditLog.objects.filter(
                    pk__gt=last,
                    company_id=company_id,
                    action__startswith="payroll.auto.",
                    content_type=period_type,
                    object_id__in=periods,
                )
                .order_by("pk")
                .values_list("action", "object_id")
            )
            if changed:
                record.mismatches.append(
                    {"site": site.pk, "changes": [f"{a} #{pk}" for a, pk in changed]}
                )
                logger.warning(
                    "Precompute sweep: site %s changed without a mark: %s", site.pk, changed
                )
            record.sites += 1
    record.finished_at = timezone.now()
    record.save(update_fields=["sites", "mismatches", "finished_at"])
    return record
