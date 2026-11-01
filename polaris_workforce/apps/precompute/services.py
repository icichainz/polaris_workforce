"""The precompute service: rebuild stored figures only where something changed.

Heavy figures (pay recomputed from attendance, and the snapshots built from it) are
rebuilt per site, over the dates a change made out of date:

1. A write marks what it changed (``mark``), in its own transaction. Every change to
   attendance or pay writes an audit entry, and ``signals.py`` marks from it
   (``marks.ranges_for``), so the services need not remember to.
2. ``manage.py refresh_precomputed`` (cron, every few minutes) rebuilds the marked sites
   in the background (``process``), and every active site once a day, since pay depends
   on the date as well (``Freshness``). With ``--sweep`` (nightly) it rebuilds every open
   period and records what changed although nobody marked it (``RefreshRun.mismatches``).
3. A screen that reads the figures calls ``ensure_fresh`` first: a site still marked for
   the dates it shows is rebuilt now, before the read. Otherwise the read costs nothing.

What a rebuild does is a list of steps (``register``): pay first (``apps.payroll``),
then the snapshot tables. Each step gets the site and the dates, ``start`` ``None``
meaning every open pay period.
"""

from __future__ import annotations

import datetime
import logging
from collections.abc import Callable

from django.db import transaction
from django.db.models import F, Q
from django.utils import timezone

from apps.core.dates import company_today
from apps.core.models import AuditLog, CompanySettings
from apps.payroll.models import PayPeriod
from apps.worksites.models import Site

from . import marks
from .models import Freshness, RefreshRun, Stale

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
    stale = Stale.objects.filter(site_id=site_id).first()
    if stale is None:
        Stale.objects.create(site_id=site_id, start=start, end=end)
        return
    Stale.objects.filter(pk=stale.pk).update(
        start=None if stale.start is None or start is None else min(stale.start, start),
        end=max(stale.end, end),
        version=F("version") + 1,
        updated_at=timezone.now(),
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
) -> bool:
    """Rebuild ``site`` now if what a screen is about to read is out of date: a mark that
    covers ``start``–``end`` (``None``: any date), or a new day since the last rebuild.
    True if it rebuilt."""
    company = company or CompanySettings.load(site.company_id)
    today = today or company_today(company)
    stale = Stale.objects.filter(site=site).first()
    fresh_on = Freshness.objects.filter(site=site).values_list("fresh_on", flat=True).first()
    if fresh_on == today and (stale is None or not _overlaps(stale, start, end)):
        return False
    refresh_site(site, today=today, company=company, stale=stale, fresh_on=fresh_on)
    return True


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


def process(company_id: int, *, today: datetime.date | None = None) -> int:
    """Rebuild every marked site of the company, and every active site not rebuilt today.
    Returns the number of sites rebuilt."""
    company = CompanySettings.load(company_id)
    today = today or company_today(company)
    marked = Stale.objects.filter(site__company_id=company_id).values("site_id")
    fresh = Freshness.objects.filter(fresh_on=today).values("site_id")
    sites = Site.objects.filter(company_id=company_id).filter(
        Q(pk__in=marked) | (Q(status=Site.Status.ACTIVE) & ~Q(pk__in=fresh))
    )
    count = 0
    for site in sites.order_by("pk"):
        refresh_site(site, today=today, company=company)
        count += 1
    return count


def run(company_id: int, *, sweep: bool = False, today: datetime.date | None = None) -> RefreshRun:
    """One ``refresh_precomputed`` run for a company (logged as a ``RefreshRun``).

    The sweep first rebuilds what is marked, then every open period of every site with
    pay: anything that still changes was changed without a mark (a write that bypassed
    the services and the audit log). It is fixed, and listed in ``mismatches``.
    """
    company = CompanySettings.load(company_id)
    today = today or company_today(company)
    record = RefreshRun.objects.create(company_id=company_id, sweep=sweep)
    record.sites = process(company_id, today=today)
    if sweep:
        for site in _sites_with_pay(company_id).order_by("pk"):
            last = AuditLog.objects.order_by("-pk").values_list("pk", flat=True).first() or 0
            refresh_site(site, today=today, company=company, everything=True)
            changed = list(
                AuditLog.objects.filter(pk__gt=last, action__startswith="payroll.auto.")
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
