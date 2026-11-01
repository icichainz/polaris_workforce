"""Report rollups: the report's figures per site and month, built ahead of time.

The live report (``reports.build_report``) reads every roll-call record and pay line of
its range; over a year that is most of the time spent. This module stores the same
figures per site and month (``apps.precompute.models``: ``WorkerMonth``,
``SiteDayCounts``, ``DeductionTotal``, and ``ReportMonth`` once a month is built), from
the same data and rules:

- attendance: the records of submitted, approved and locked days of the month;
- pay: the lines of the pay periods that **end** in the month.

It is a precompute step (``apps.precompute.services.register``), run after pay over the
dates a change made out of date. A month whose roll calls and pay periods are all locked
is built once and then left alone: its figures cannot change, and each worker keeps the
trade they had then. A month that is not built is read live, so the rollups only ever
make the report faster, never different.
"""

from __future__ import annotations

import datetime
from collections import defaultdict

from django.db import transaction
from django.db.models import Count, F, Min, Q, Sum
from django.utils import timezone

from apps.attendance.models import AttendanceDay, AttendanceRecord
from apps.payroll.models import PayLine, PayPeriod
from apps.payroll.pay_rules import add_deductions
from apps.precompute.models import DeductionTotal, ReportMonth, SiteDayCounts, WorkerMonth

Status = AttendanceRecord.Status
REPORTED_DAY_STATUSES = (
    AttendanceDay.Status.SUBMITTED,
    AttendanceDay.Status.APPROVED,
    AttendanceDay.Status.LOCKED,
)
STATUS_COUNTS = {
    "present": Count("id", filter=Q(status=Status.PRESENT)),
    "half": Count("id", filter=Q(status=Status.HALF_DAY)),
    "late": Count("id", filter=Q(status=Status.LATE)),
    "absent": Count("id", filter=Q(status=Status.ABSENT)),
}
# Without a mark to say from when, a rebuild covers this far back.
DEFAULT_LOOKBACK_DAYS = 31
# Months of history the nightly sweep builds per site (oldest first), so that a site
# with years of data is filled in over a few nights, not in one long run.
BACKFILL_MONTHS = 6


def month_of(date: datetime.date) -> datetime.date:
    return date.replace(day=1)


def month_end(month: datetime.date) -> datetime.date:
    following = (month.replace(day=28) + datetime.timedelta(days=4)).replace(day=1)
    return following - datetime.timedelta(days=1)


def months_between(start: datetime.date, end: datetime.date) -> list[datetime.date]:
    months, month = [], month_of(start)
    while month <= end:
        months.append(month)
        month = month_end(month) + datetime.timedelta(days=1)
    return months


def rebuild_step(site, start, end, *, today, company) -> None:
    """The precompute step: rebuild the months of ``site`` between ``start`` (``None``:
    since its oldest open pay period) and ``end``. A rebuild of every open period (the
    nightly sweep) also builds a few months of history never built yet."""
    # A month a change marked by its dates is rebuilt even when locked (an admin edit of
    # locked pay); "every open period" leaves the locked months as they were.
    force = start is not None
    if start is None:
        for month in missing_months(site, today)[:BACKFILL_MONTHS]:
            build_month(site, month, today=today)
        oldest = (
            PayPeriod.objects.filter(site=site)
            .exclude(status=PayPeriod.Status.LOCKED)
            .order_by("start_date")
            .values_list("start_date", flat=True)
            .first()
        )
        start = min(oldest or end, end - datetime.timedelta(days=DEFAULT_LOOKBACK_DAYS))
    marked = set(months_between(start, end))
    # And the months whose pay changed meanwhile (a correction moved a later pay).
    stale = set(ReportMonth.objects.filter(site=site, stale=True).values_list("month", flat=True))
    for month in sorted(marked | stale):
        if month <= today:
            build_month(
                site, month, today=today, force=(force and month in marked) or month in stale
            )


def missing_months(site, today: datetime.date) -> list[datetime.date]:
    """The site's months with roll calls or pay that were never built, oldest first."""
    first_day = AttendanceDay.objects.filter(site=site).aggregate(first=Min("date"))["first"]
    first_pay = PayPeriod.objects.filter(site=site).aggregate(first=Min("end_date"))["first"]
    firsts = [date for date in (first_day, first_pay) if date is not None]
    if not firsts:
        return []
    built = set(ReportMonth.objects.filter(site=site).values_list("month", flat=True))
    return [month for month in months_between(min(firsts), today) if month not in built]


def _all_locked(site, month: datetime.date, last: datetime.date) -> bool:
    """Every roll call and every pay period of the month is locked (nothing can change)."""
    open_day = (
        AttendanceDay.objects.filter(site=site, date__gte=month, date__lte=last)
        .exclude(status=AttendanceDay.Status.LOCKED)
        .exists()
    )
    open_period = (
        PayPeriod.objects.filter(site=site, end_date__gte=month, end_date__lte=last)
        .exclude(status=PayPeriod.Status.LOCKED)
        .exists()
    )
    return not open_day and not open_period


def build_month(site, month: datetime.date, *, today: datetime.date, force: bool = False) -> bool:
    """(Re)build one site's month. False when it was already built and is locked (unless
    ``force``: a change marked its dates)."""
    last = month_end(month)
    locked = last < today and _all_locked(site, month, last)
    current = ReportMonth.objects.filter(site=site, month=month).first()
    if locked and not force and current and current.locked and not current.stale:
        return False
    # A flag set while this build reads (``generation`` moves) stays: the build may have
    # read the pay from before the change.
    generation = current.generation if current else None

    records = AttendanceRecord.objects.filter(
        day__site=site,
        day__date__gte=month,
        day__date__lte=last,
        day__status__in=REPORTED_DAY_STATUSES,
    )
    lines = PayLine.objects.filter(
        period__site=site, period__end_date__gte=month, period__end_date__lte=last
    )

    rows: dict[int, WorkerMonth] = {}

    def row(worker_id, trade_id) -> WorkerMonth:
        if worker_id not in rows:
            rows[worker_id] = WorkerMonth(
                site=site, worker_id=worker_id, month=month, trade_id=trade_id
            )
        return rows[worker_id]

    for values in (
        records.values("worker_id", "worker__trade_id").annotate(**STATUS_COUNTS).order_by()
    ):
        item = row(values["worker_id"], values["worker__trade_id"])
        item.present, item.half = values["present"], values["half"]
        item.late, item.absent = values["late"], values["absent"]
    for values in (
        lines.values("worker_id", "worker__trade_id")
        .annotate(
            earned=Sum(F("gross") + F("adjustment")),
            gross_sum=Sum("gross"),
            statutory=Sum("statutory_deductions"),
            employer=Sum("employer_contributions"),
            lines=Count("id"),
        )
        .order_by()
    ):
        item = row(values["worker_id"], values["worker__trade_id"])
        item.pay_lines = values["lines"]
        item.earned = values["earned"] or 0
        item.gross = values["gross_sum"] or 0
        item.statutory = values["statutory"] or 0
        item.employer = values["employer"] or 0

    days = [
        SiteDayCounts(
            site=site,
            date=values["day__date"],
            trade_id=values["worker__trade_id"],
            present=values["present"],
            half=values["half"],
            late=values["late"],
            absent=values["absent"],
        )
        for values in records.values("day__date", "worker__trade_id")
        .annotate(**STATUS_COUNTS)
        .order_by()
    ]

    by_rule: dict = defaultdict(lambda: defaultdict(lambda: [None, 0, 0]))
    for trade_id, deductions in lines.exclude(
        Q(deductions=[]) | Q(deductions__isnull=True)
    ).values_list("worker__trade_id", "deductions"):
        add_deductions(by_rule[trade_id], deductions)
    totals = [
        DeductionTotal(
            site=site,
            month=month,
            trade_id=trade_id,
            rule_key=str(key),
            name=name or "",
            employee=employee,
            employer=employer,
        )
        for trade_id, rules in by_rule.items()
        for key, (name, employee, employer) in rules.items()
    ]

    with transaction.atomic():
        WorkerMonth.objects.filter(site=site, month=month).delete()
        SiteDayCounts.objects.filter(site=site, date__gte=month, date__lte=last).delete()
        DeductionTotal.objects.filter(site=site, month=month).delete()
        WorkerMonth.objects.bulk_create(rows.values())
        SiteDayCounts.objects.bulk_create(days)
        DeductionTotal.objects.bulk_create(totals)
        if generation is None:
            ReportMonth.objects.create(site=site, month=month, locked=locked)
        else:
            built = ReportMonth.objects.filter(pk=current.pk, generation=generation).update(
                locked=locked, stale=False, built_at=timezone.now()
            )
            if not built:  # flagged meanwhile: the rows stay unread until the next build
                ReportMonth.objects.filter(pk=current.pk).update(locked=locked)
    return True
