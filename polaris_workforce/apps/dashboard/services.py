"""The "Today" dashboard: read-only figures for the sites a user may see.

Everything here is a read. "Today" is the date in the company time zone. The page stays
at a fixed number of queries however many sites there are (see ``tests/test_dashboard_*``).
"""

import datetime
from collections import defaultdict
from dataclasses import dataclass, field
from decimal import Decimal
from zoneinfo import ZoneInfo

from django.db.models import Count, Q, Sum
from django.utils import timezone

from apps.accounts.permissions import scoped_sites
from apps.attendance.models import AttendanceCorrection, AttendanceDay, AttendanceRecord
from apps.core.models import CompanySettings
from apps.core.settings import resolve_setting
from apps.payroll.models import PayLine, Payment, PayPeriod
from apps.worksites.models import Site, SiteAssignment

Status = AttendanceRecord.Status
DayStatus = AttendanceDay.Status

CHART_DAYS = 7
ABSENT_STREAK = 3
# How far back to look for an absence streak (covers weekends and holidays).
STREAK_LOOKBACK_DAYS = 21
UNPAID_PERIOD_STATUSES = (PayPeriod.Status.OPEN, PayPeriod.Status.READY, PayPeriod.Status.PAYING)


def company_now(company: CompanySettings) -> datetime.datetime:
    """The current time in the company time zone."""
    return timezone.now().astimezone(ZoneInfo(company.time_zone))


def worker_days(present: int, late: int, half: int, half_day_units: Decimal) -> Decimal:
    """Days worked: present and late count as one day, a half day as ``half_day_units``."""
    return Decimal(present + late) + Decimal(half) * half_day_units


@dataclass
class SiteToday:
    site: Site
    foreman_name: str
    day: AttendanceDay | None
    roll_call_due: datetime.time
    overdue: bool
    expected: int = 0
    present: int = 0
    absent: int = 0
    half: int = 0
    late: int = 0
    cash_tonight: int | None = None

    @property
    def status(self) -> str:
        """``not_started`` or the attendance day status."""
        return self.day.status if self.day else "not_started"

    @property
    def present_percent(self) -> int:
        return round(100 * self.present / self.expected) if self.expected else 0


@dataclass
class Alert:
    """One "Needs attention" item; the template writes the sentence for ``kind``."""

    kind: str  # missing_roll_call | corrections | absent_streak | unclosed_period
    severity: str  # danger | warning
    site_name: str = ""
    date: datetime.date | None = None
    count: int = 0
    person: str = ""
    detail: str = ""
    due: datetime.time | None = None


@dataclass
class ChartDay:
    date: datetime.date
    value: Decimal
    is_today: bool
    height_percent: int = 0


@dataclass
class TodayOverview:
    now: datetime.datetime
    today: datetime.date
    is_working_day: bool
    sites: list[SiteToday]
    awaiting: list[AttendanceDay]
    alerts: list[Alert]
    chart: list[ChartDay]
    half_day_units: Decimal
    totals: dict[str, int] = field(default_factory=dict)

    @property
    def present_percent(self) -> int:
        expected = self.totals.get("expected", 0)
        return round(100 * self.totals.get("present", 0) / expected) if expected else 0

    @property
    def missing_roll_calls(self) -> int:
        return sum(1 for alert in self.alerts if alert.kind == "missing_roll_call")

    @property
    def oldest_awaiting(self) -> AttendanceDay | None:
        return self.awaiting[0] if self.awaiting else None


def today_overview(
    user, *, company: CompanySettings | None = None, now: datetime.datetime | None = None
) -> TodayOverview:
    """Build the dashboard for ``user``, limited to ``scoped_sites(user)``."""
    company = company or CompanySettings.load()
    now = now or company_now(company)
    today = now.date()
    is_working_day = company.is_working_day(today)

    scope = list(scoped_sites(user).prefetch_related("foremen"))
    scope_ids = [site.pk for site in scope]
    active = [site for site in scope if site.status == Site.Status.ACTIVE]
    active_ids = [site.pk for site in active]

    expected = dict(
        SiteAssignment.objects.active_on(today)
        .filter(site__in=active_ids, worker__is_active=True)
        .values("site")
        .annotate(n=Count("worker", distinct=True))
        .values_list("site", "n")
    )
    days = {
        day.site_id: day
        for day in AttendanceDay.objects.filter(site__in=active_ids, date=today)
        .select_related("foreman", "approved_by")
        .annotate(
            n_present=Count("records", filter=Q(records__status=Status.PRESENT)),
            n_absent=Count("records", filter=Q(records__status=Status.ABSENT)),
            n_half=Count("records", filter=Q(records__status=Status.HALF_DAY)),
            n_late=Count("records", filter=Q(records__status=Status.LATE)),
        )
    }
    cash = _cash_tonight(active_ids, today)

    rows = []
    alerts: list[Alert] = []
    for site in active:
        day = days.get(site.pk)
        due = resolve_setting("roll_call_due", site=site, company=company)
        foremen = list(site.foremen.all())
        foreman = day.foreman if day and day.foreman else (foremen[0] if foremen else None)
        row = SiteToday(
            site=site,
            foreman_name=_name(foreman),
            day=day,
            roll_call_due=due,
            overdue=False,
            expected=expected.get(site.pk, 0),
            cash_tonight=cash.get(site.pk),
        )
        if day:
            row.present, row.absent = day.n_present, day.n_absent
            row.half, row.late = day.n_half, day.n_late
            row.expected = max(row.expected, row.present + row.absent + row.half + row.late)
        not_submitted = day is None or day.status == DayStatus.DRAFT
        if is_working_day and row.expected and not_submitted and now.time() > due:
            row.overdue = True
            alerts.append(
                Alert(
                    kind="missing_roll_call",
                    severity="danger",
                    site_name=site.name,
                    person=row.foreman_name,
                    due=due,
                )
            )
        rows.append(row)

    awaiting = list(
        AttendanceDay.objects.filter(site__in=scope_ids, status=DayStatus.SUBMITTED)
        .select_related("site")
        .order_by("date", "site__name")
    )
    alerts += _correction_alerts(scope_ids)
    alerts += _absence_alerts(scope_ids, today)
    alerts += _unclosed_period_alerts(scope_ids, today)

    totals = {
        key: sum(getattr(row, key) for row in rows)
        for key in ("expected", "present", "absent", "half", "late")
    }
    totals["cash_tonight"] = sum(row.cash_tonight or 0 for row in rows)
    return TodayOverview(
        now=now,
        today=today,
        is_working_day=is_working_day,
        sites=rows,
        awaiting=awaiting,
        alerts=alerts,
        chart=_chart(scope_ids, company, today),
        half_day_units=company.half_day_units,
        totals=totals,
    )


def _name(user) -> str:
    if user is None:
        return ""
    return user.get_full_name() or user.username


def _cash_tonight(site_ids, today) -> dict[int, int]:
    """Net still to hand over for pay periods ending today, per site."""
    periods = Q(period__site__in=site_ids, period__end_date=today) & ~Q(
        period__status__in=(PayPeriod.Status.PAID, PayPeriod.Status.LOCKED)
    )
    net = dict(
        PayLine.objects.filter(periods)
        .values("period__site")
        .annotate(total=Sum("net"))
        .values_list("period__site", "total")
    )
    paid_filter = Q(pay_line__period__site__in=site_ids, pay_line__period__end_date=today) & ~Q(
        pay_line__period__status__in=(PayPeriod.Status.PAID, PayPeriod.Status.LOCKED)
    )
    paid = dict(
        Payment.objects.filter(paid_filter)
        .values("pay_line__period__site")
        .annotate(total=Sum("amount"))
        .values_list("pay_line__period__site", "total")
    )
    return {site: max(total - paid.get(site, 0), 0) for site, total in net.items()}


def _correction_alerts(site_ids) -> list[Alert]:
    """Days waiting for approval that were changed after the foreman submitted them."""
    grouped = (
        AttendanceCorrection.objects.filter(
            record__day__site__in=site_ids, record__day__status=DayStatus.SUBMITTED
        )
        .values("record__day", "record__day__date", "record__day__site__name")
        .annotate(n=Count("id"))
        .order_by("record__day__date", "record__day__site__name")
    )
    return [
        Alert(
            kind="corrections",
            severity="warning",
            site_name=row["record__day__site__name"],
            date=row["record__day__date"],
            count=row["n"],
        )
        for row in grouped
    ]


def _absence_alerts(site_ids, today) -> list[Alert]:
    """Active workers whose last ``ABSENT_STREAK`` recorded days are all absences."""
    records = (
        AttendanceRecord.objects.filter(
            day__site__in=site_ids,
            day__date__gte=today - datetime.timedelta(days=STREAK_LOOKBACK_DAYS),
            day__date__lte=today,
            worker__is_active=True,
        )
        .exclude(day__status=DayStatus.REJECTED)
        .order_by("worker_id", "-day__date")
        .values(
            "worker_id",
            "status",
            "worker__first_name",
            "worker__last_name",
            "worker__trade__name",
            "day__site__name",
        )
    )
    streaks: dict[int, list] = defaultdict(list)
    finished: set[int] = set()
    for record in records:
        worker = record["worker_id"]
        if worker in finished:
            continue
        if record["status"] == Status.ABSENT:
            streaks[worker].append(record)
        else:
            finished.add(worker)
    alerts = []
    for rows in streaks.values():
        if len(rows) < ABSENT_STREAK:
            continue
        latest = rows[0]
        name = f"{latest['worker__first_name']} {latest['worker__last_name']}".strip()
        detail = " · ".join(
            part for part in (latest["worker__trade__name"], latest["day__site__name"]) if part
        )
        alerts.append(
            Alert(
                kind="absent_streak",
                severity="warning",
                person=name,
                count=len(rows),
                detail=detail,
                site_name=latest["day__site__name"],
            )
        )
    return sorted(alerts, key=lambda alert: (-alert.count, alert.person))


def _unclosed_period_alerts(site_ids, today) -> list[Alert]:
    """Pay periods (cash sheets) that ended before today and are not paid out yet."""
    periods = (
        PayPeriod.objects.filter(
            site__in=site_ids, end_date__lt=today, status__in=UNPAID_PERIOD_STATUSES
        )
        .select_related("site")
        .annotate(unpaid=Count("lines", filter=Q(lines__payments__isnull=True), distinct=True))
        .order_by("end_date", "site__name")
    )
    return [
        Alert(
            kind="unclosed_period",
            severity="warning",
            site_name=period.site.name,
            date=period.end_date,
            count=period.unpaid,
        )
        for period in periods
    ]


def chart_dates(company: CompanySettings, today: datetime.date) -> list[datetime.date]:
    """The last ``CHART_DAYS`` working days up to and including today, oldest first."""
    dates: list[datetime.date] = []
    day = today
    for _attempt in range(CHART_DAYS * 5):
        if company.is_working_day(day):
            dates.append(day)
            if len(dates) == CHART_DAYS:
                break
        day -= datetime.timedelta(days=1)
    return sorted(dates)


def _chart(site_ids, company, today) -> list[ChartDay]:
    dates = chart_dates(company, today)
    counts: dict[datetime.date, dict[str, int]] = defaultdict(dict)
    rows = (
        AttendanceRecord.objects.filter(day__site__in=site_ids, day__date__in=dates)
        .exclude(day__status=DayStatus.REJECTED)
        .values("day__date", "status")
        .annotate(n=Count("id"))
    )
    for row in rows:
        counts[row["day__date"]][row["status"]] = row["n"]
    chart = [
        ChartDay(
            date=date,
            value=worker_days(
                counts[date].get(Status.PRESENT, 0),
                counts[date].get(Status.LATE, 0),
                counts[date].get(Status.HALF_DAY, 0),
                company.half_day_units,
            ),
            is_today=date == today,
        )
        for date in dates
    ]
    top = max((day.value for day in chart), default=Decimal(0))
    for day in chart:
        day.height_percent = int(day.value * 100 / top) if top else 0
    return chart
