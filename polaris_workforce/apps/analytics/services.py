"""Analytics: budgets, cost, cash, advances and integrity checks (read-only).

Every function takes the sites (or workers) the caller may see, which views take from
``scoped_sites(user)`` (``scoped_workers(user, today)`` for advances), and reads a fixed
number of queries however many sites there are. Money is in the currency's smallest unit.

Which data counts (the Reports rules, ``apps.dashboard.reports``):
- Labour cost: the gross of the pay lines of pay periods that **end** in the range, plus
  their correction adjustments. A site's cost to date is the same up to today.
- Attendance: records on days that were submitted, approved or locked.
"""

from __future__ import annotations

import datetime
import re
from dataclasses import dataclass, field
from decimal import ROUND_CEILING, ROUND_HALF_UP, Decimal

from django.contrib.contenttypes.models import ContentType
from django.db import transaction
from django.db.models import Count, Exists, F, Min, OuterRef, Q, Sum
from django.db.models.functions import Coalesce
from django.utils.translation import gettext, ngettext

from apps.attendance.models import AttendanceDay, AttendanceRecord
from apps.checkin.models import CheckIn
from apps.core.choices import PaymentMethod
from apps.core.models import AuditLog
from apps.payouts.models import PayoutAccount, TransferMethod
from apps.payroll.models import Advance, CashIssue, PayLine, Payment, PayPeriod
from apps.payroll.services import current_sites
from apps.worksites.models import Site

from .models import AnalyticsSettings

Status = AttendanceRecord.Status
REPORTED_DAY_STATUSES = (
    AttendanceDay.Status.SUBMITTED,
    AttendanceDay.Status.APPROVED,
    AttendanceDay.Status.LOCKED,
)
WORKED = (Status.PRESENT, Status.LATE, Status.HALF_DAY)


def _ids(sites) -> list[int]:
    return [getattr(site, "pk", site) for site in sites]


def _round(value: Decimal) -> int:
    return int(value.quantize(Decimal(1), rounding=ROUND_HALF_UP))


def _percent(part: int, whole: int) -> int | None:
    if not whole:
        return None
    return _round(Decimal(part) * 100 / Decimal(whole))


LABOUR_COST = Sum(F("gross") + F("adjustment"))


# --- settings -----------------------------------------------------------------------------


def save_settings(settings: AnalyticsSettings, *, actor) -> AnalyticsSettings:
    """Save the company's analytics settings (validated by the caller's form), audited."""
    before = AnalyticsSettings.objects.filter(pk=settings.pk).values("budget_warn_percent").first()
    with transaction.atomic():
        settings.save()
        AuditLog.record(
            actor=actor,
            action="analytics.settings.update",
            target=settings,
            before=before,
            after={"budget_warn_percent": settings.budget_warn_percent},
        )
    return settings


# --- site budgets -------------------------------------------------------------------------

# Spending is projected from this many days before today (four weeks: a crew that grew
# or shrank shows within a month, one quiet week does not swing it).
SPEND_WINDOW_DAYS = 28
# A site paid for fewer days than this has no projection yet.
MIN_HISTORY_DAYS = 14


class BudgetStatus:
    NO_BUDGET = "no_budget"
    OVER = "over"
    AT_RISK = "at_risk"
    NOT_ENOUGH_DATA = "not_enough_data"
    ON_TRACK = "on_track"


# Worst first: the order sites are listed in.
STATUS_ORDER = [
    BudgetStatus.OVER,
    BudgetStatus.AT_RISK,
    BudgetStatus.ON_TRACK,
    BudgetStatus.NOT_ENOUGH_DATA,
    BudgetStatus.NO_BUDGET,
]


@dataclass
class SiteBudget:
    site_id: int
    name: str
    site_status: str
    budget: int | None
    spent: int  # labour cost to date
    recent: int  # labour cost of the last SPEND_WINDOW_DAYS days
    planned_end: datetime.date | None
    first_pay: datetime.date | None
    status: str = BudgetStatus.NO_BUDGET
    used_percent: int | None = None
    weekly_spend: int | None = None
    runs_out_on: datetime.date | None = None

    @property
    def remaining(self) -> int | None:
        return None if not self.budget else self.budget - self.spent

    @property
    def bar_percent(self) -> int:
        """The used share for a progress bar (0–100)."""
        return max(0, min(100, self.used_percent or 0))

    @property
    def days_before_end(self) -> int | None:
        """How many days before the planned end the budget runs out (negative: after)."""
        if self.runs_out_on is None or self.planned_end is None:
            return None
        return (self.planned_end - self.runs_out_on).days

    @property
    def needs_attention(self) -> bool:
        return self.status in (BudgetStatus.OVER, BudgetStatus.AT_RISK)


def site_budgets(
    sites,
    *,
    today: datetime.date,
    settings: AnalyticsSettings | None = None,
    with_spend_only: bool = False,
) -> list[SiteBudget]:
    """Budget burn of each site in ``sites`` (Site objects, ids or a queryset), worst
    first: one query, plus one to read the sites given as ids and one for the settings
    when ``settings`` is not passed.

    ``with_spend_only``: leave out sites with neither a budget nor any labour cost.
    """
    settings = settings or AnalyticsSettings.load()
    site_list = list(sites)
    if not all(isinstance(site, Site) for site in site_list):
        site_list = list(Site.objects.filter(pk__in=_ids(site_list)))
    window_start = today - datetime.timedelta(days=SPEND_WINDOW_DAYS)
    spend = {
        row["period__site_id"]: row
        for row in PayLine.objects.filter(
            period__site__in=[site.pk for site in site_list], period__end_date__lte=today
        )
        .values("period__site_id")
        .annotate(
            spent=LABOUR_COST,
            recent=Sum(F("gross") + F("adjustment"), filter=Q(period__end_date__gt=window_start)),
            first_pay=Min("period__end_date"),
        )
        .order_by()
    }
    result = []
    for site in site_list:
        paid = spend.get(site.pk, {})
        item = SiteBudget(
            site_id=site.pk,
            name=site.name,
            site_status=site.status,
            budget=site.labour_budget,
            spent=paid.get("spent") or 0,
            recent=paid.get("recent") or 0,
            planned_end=site.planned_end_date,
            first_pay=paid.get("first_pay"),
        )
        if with_spend_only and not item.budget and not item.spent:
            continue
        _assess(item, today, settings.budget_warn_percent)
        result.append(item)
    result.sort(key=lambda item: (STATUS_ORDER.index(item.status), item.name.lower()))
    return result


def _assess(item: SiteBudget, today: datetime.date, warn_percent: int) -> None:
    """Fill in the projection and the status of ``item``."""
    history = (today - item.first_pay).days + 1 if item.first_pay else 0
    projecting = item.site_status == Site.Status.ACTIVE and history >= MIN_HISTORY_DAYS
    days = min(history, SPEND_WINDOW_DAYS)
    if projecting and item.recent > 0:
        item.weekly_spend = _round(Decimal(item.recent) * 7 / days)
    if not item.budget:
        item.status = BudgetStatus.NO_BUDGET
        return
    item.used_percent = _percent(item.spent, item.budget)
    if item.spent >= item.budget:
        item.status = BudgetStatus.OVER
        return
    if item.weekly_spend:
        daily = Decimal(item.recent) / days
        left = Decimal(item.budget - item.spent) / daily
        item.runs_out_on = today + datetime.timedelta(
            days=int(left.quantize(Decimal(1), rounding=ROUND_CEILING))
        )
    early = (
        item.runs_out_on is not None
        and item.planned_end is not None
        and item.runs_out_on < item.planned_end
    )
    if item.used_percent >= warn_percent or early:
        item.status = BudgetStatus.AT_RISK
    elif not projecting:
        item.status = BudgetStatus.NOT_ENOUGH_DATA
    else:
        item.status = BudgetStatus.ON_TRACK


# --- cost breakdown -----------------------------------------------------------------------


@dataclass
class CostRow:
    """What a site's pay is made of. ``labour_cost`` is the Reports figure."""

    site_id: int | None
    name: str
    time_pay: int = 0  # the days worked (overtime, night, Sunday, holiday and stoppage pay)
    piece_work: int = 0
    bonuses: int = 0
    corrections: int = 0  # adjustments of days already paid (negative: recovered)
    employer: int = 0  # employer contributions, on top of the pay
    overtime_hours: Decimal = Decimal(0)
    pay_lines: int = 0

    @property
    def labour_cost(self) -> int:
        return self.time_pay + self.piece_work + self.bonuses + self.corrections

    @property
    def employer_cost(self) -> int:
        return self.labour_cost + self.employer

    def add(self, other: CostRow) -> None:
        for name in ("time_pay", "piece_work", "bonuses", "corrections", "employer"):
            setattr(self, name, getattr(self, name) + getattr(other, name))
        self.overtime_hours += other.overtime_hours
        self.pay_lines += other.pay_lines


@dataclass
class CostBreakdown:
    rows: list[CostRow]
    total: CostRow


def cost_breakdown(sites, start: datetime.date, end: datetime.date, *, trade=None) -> CostBreakdown:
    """Pay of the periods that end between ``start`` and ``end``, split by kind, per site
    (one query). ``trade`` filters on the workers' current trade."""
    lines = PayLine.objects.filter(
        period__site__in=_ids(sites), period__end_date__gte=start, period__end_date__lte=end
    )
    if trade is not None:
        lines = lines.filter(worker__trade_id=getattr(trade, "pk", trade))
    rows = []
    for values in (
        lines.values("period__site_id", "period__site__name")
        .annotate(
            gross=Sum("gross"),
            piece=Sum("piece_amount"),
            bonus=Sum("bonus_amount"),
            adjustment=Sum("adjustment"),
            employer=Sum("employer_contributions"),
            overtime=Sum("overtime_hours"),
            n=Count("id"),
        )
        .order_by()
    ):
        piece, bonus = values["piece"] or 0, values["bonus"] or 0
        rows.append(
            CostRow(
                site_id=values["period__site_id"],
                name=values["period__site__name"],
                time_pay=(values["gross"] or 0) - piece - bonus,
                piece_work=piece,
                bonuses=bonus,
                corrections=values["adjustment"] or 0,
                employer=values["employer"] or 0,
                overtime_hours=values["overtime"] or Decimal(0),
                pay_lines=values["n"],
            )
        )
    rows.sort(key=lambda row: (-row.labour_cost, row.name.lower()))
    total = CostRow(site_id=None, name="")
    for row in rows:
        total.add(row)
    return CostBreakdown(rows=rows, total=total)


# --- comparison with the previous period --------------------------------------------------


def previous_range(start: datetime.date, end: datetime.date) -> tuple[datetime.date, datetime.date]:
    """The same number of days just before ``start``."""
    days = (end - start).days + 1
    return start - datetime.timedelta(days=days), start - datetime.timedelta(days=1)


@dataclass
class Change:
    """A figure against the previous period: ``percent`` change, or ``points`` for rates."""

    current: int | Decimal | None
    previous: int | Decimal | None
    percent: int | None = None
    points: int | None = None

    @property
    def direction(self) -> str:
        """``up``, ``down``, ``same`` or ``""`` (nothing to compare with)."""
        delta = self.points if self.points is not None else self.percent
        if delta is None:
            return ""
        return "up" if delta > 0 else "down" if delta < 0 else "same"

    @property
    def size(self) -> int | None:
        delta = self.points if self.points is not None else self.percent
        return None if delta is None else abs(delta)

    @property
    def caption(self) -> str:
        return change_caption(self)


def change(current, previous) -> Change:
    """Percentage change; none when there was nothing before."""
    if current is None or previous is None or not previous:
        return Change(current, previous)
    return Change(current, previous, percent=_round((Decimal(current) - previous) * 100 / previous))


def rate_change(current: int | None, previous: int | None) -> Change:
    """Change of a percentage, in points."""
    if current is None or previous is None:
        return Change(current, previous)
    return Change(current, previous, points=current - previous)


def change_caption(item: Change) -> str:
    """The tile caption: "▲ 12% on the previous period", "▼ 3 pts on the previous period"…"""
    if not item.direction:
        return gettext("Nothing to compare with in the previous period")
    if item.direction == "same":
        return gettext("Same as the previous period")
    arrow = "▲" if item.direction == "up" else "▼"
    if item.points is not None:
        return ngettext(
            "%(arrow)s %(n)s pt on the previous period",
            "%(arrow)s %(n)s pts on the previous period",
            item.size,
        ) % {"arrow": arrow, "n": item.size}
    return gettext("%(arrow)s %(n)s%% on the previous period") % {"arrow": arrow, "n": item.size}


def report_changes(report, previous) -> dict[str, Change]:
    """The Reports key figures against the same report for the previous period."""
    return {
        "labour_cost": change(report.labour_cost, previous.labour_cost),
        "worker_days": change(report.worker_days, previous.worker_days),
        "attendance_rate": rate_change(report.attendance_rate, previous.attendance_rate),
        "cost_per_worker_day": change(report.cost_per_worker_day, previous.cost_per_worker_day),
    }


# --- cash over a range --------------------------------------------------------------------


@dataclass
class CashDay:
    """One site's cash sheet (pay day): what was due, paid, and the cash given out."""

    site_id: int
    site_name: str
    date: datetime.date
    to_pay: int = 0  # net of the pay lines
    paid: int = 0  # every payment, whatever the method
    cash_issued: int = 0
    cash_paid: int = 0  # payments in cash
    workers: int = 0
    unpaid_workers: int = 0  # pay lines with something to pay and no payment

    @property
    def left_to_pay(self) -> int:
        return max(0, self.to_pay - self.paid)

    @property
    def cash_in_hand(self) -> int:
        """Cash issued and not paid out: to hand back (negative: paid more than issued)."""
        return self.cash_issued - self.cash_paid


@dataclass
class CashSummary:
    days: list[CashDay]
    total: CashDay


def cash_by_day(sites, start: datetime.date, end: datetime.date) -> CashSummary:
    """Every cash sheet of ``sites`` between ``start`` and ``end`` (three queries), most
    recent first. Days with neither pay nor cash issued are left out."""
    site_ids = _ids(sites)
    days: dict[tuple[int, datetime.date], CashDay] = {}

    def day(site_id, site_name, date) -> CashDay:
        key = (site_id, date)
        if key not in days:
            days[key] = CashDay(site_id=site_id, site_name=site_name, date=date)
        return days[key]

    has_payment = Exists(Payment.objects.filter(pay_line=OuterRef("pk")))
    for values in (
        PayLine.objects.filter(
            period__site__in=site_ids, period__end_date__gte=start, period__end_date__lte=end
        )
        .annotate(has_payment=has_payment)
        .values("period__site_id", "period__site__name", "period__end_date")
        .annotate(
            to_pay=Sum("net"),
            workers=Count("id"),
            unpaid=Count("id", filter=Q(net__gt=0, has_payment=False)),
        )
        .order_by()
    ):
        item = day(
            values["period__site_id"], values["period__site__name"], values["period__end_date"]
        )
        item.to_pay = values["to_pay"] or 0
        item.workers = values["workers"]
        item.unpaid_workers = values["unpaid"]
    for values in (
        Payment.objects.filter(
            pay_line__period__site__in=site_ids,
            pay_line__period__end_date__gte=start,
            pay_line__period__end_date__lte=end,
        )
        .values(
            "pay_line__period__site_id",
            "pay_line__period__site__name",
            "pay_line__period__end_date",
        )
        .annotate(paid=Sum("amount"), cash=Sum("amount", filter=Q(method=PaymentMethod.CASH)))
        .order_by()
    ):
        item = day(
            values["pay_line__period__site_id"],
            values["pay_line__period__site__name"],
            values["pay_line__period__end_date"],
        )
        item.paid = values["paid"] or 0
        item.cash_paid = values["cash"] or 0
    for values in (
        CashIssue.objects.filter(site__in=site_ids, date__gte=start, date__lte=end)
        .values("site_id", "site__name", "date")
        .annotate(total=Sum("amount"))
        .order_by()
    ):
        day(values["site_id"], values["site__name"], values["date"]).cash_issued = values["total"]

    ordered = sorted(days.values(), key=lambda item: (-item.date.toordinal(), item.site_name))
    total = CashDay(site_id=0, site_name="", date=end)
    for item in ordered:
        for name in ("to_pay", "paid", "cash_issued", "cash_paid", "workers", "unpaid_workers"):
            setattr(total, name, getattr(total, name) + getattr(item, name))
    return CashSummary(days=ordered, total=total)


# --- unpaid pay and advances, by age ------------------------------------------------------


@dataclass
class AgeBuckets:
    """Amounts (and how many) by age; ``labels`` are the bucket keys, youngest first."""

    keys: tuple[str, ...]
    amounts: dict[str, int] = field(default_factory=dict)
    counts: dict[str, int] = field(default_factory=dict)

    def add(self, key: str, amount: int) -> None:
        self.amounts[key] = self.amounts.get(key, 0) + amount
        self.counts[key] = self.counts.get(key, 0) + 1

    @property
    def total(self) -> int:
        return sum(self.amounts.values())

    @property
    def count(self) -> int:
        return sum(self.counts.values())

    def cells(self) -> list[tuple[str, int, int]]:
        return [(key, self.amounts.get(key, 0), self.counts.get(key, 0)) for key in self.keys]


UNPAID_AGES = ("1-7", "8-30", "31+")
UNPAID_PERIOD_STATUSES = (PayPeriod.Status.OPEN, PayPeriod.Status.READY, PayPeriod.Status.PAYING)
ADVANCE_AGES = ("0-30", "31-90", "91+")


def _unpaid_age(days: int) -> str:
    return "1-7" if days <= 7 else "8-30" if days <= 30 else "31+"


def _advance_age(days: int) -> str:
    return "0-30" if days <= 30 else "31-90" if days <= 90 else "91+"


@dataclass
class SiteAges:
    site_id: int | None
    name: str
    ages: AgeBuckets


def unpaid_by_age(sites, *, today: datetime.date) -> tuple[list[SiteAges], AgeBuckets]:
    """Pay still owed after its pay day, per site and by days since the pay day (one
    query). Only the periods not paid out yet are read (open, ready, paying: the Today
    page's rule): a paid period is settled, and on a locked one whoever was not paid was
    closed with a reason."""
    total = AgeBuckets(UNPAID_AGES)
    by_site: dict[int, SiteAges] = {}
    rows = (
        PayLine.objects.filter(period__site__in=_ids(sites), period__end_date__lt=today, net__gt=0)
        .filter(period__status__in=UNPAID_PERIOD_STATUSES)
        .annotate(paid=Coalesce(Sum("payments__amount"), 0))
        .filter(net__gt=F("paid"))
        .values_list("period__site_id", "period__site__name", "period__end_date", "net", "paid")
    )
    for site_id, name, end_date, net, paid in rows:
        key = _unpaid_age((today - end_date).days)
        if site_id not in by_site:
            by_site[site_id] = SiteAges(site_id, name, AgeBuckets(UNPAID_AGES))
        by_site[site_id].ages.add(key, net - paid)
        total.add(key, net - paid)
    ordered = sorted(by_site.values(), key=lambda item: (-item.ages.total, item.name.lower()))
    return ordered, total


def advances_by_age(
    workers, *, today: datetime.date, site=None
) -> tuple[list[SiteAges], AgeBuckets]:
    """Advances still owed by ``workers``, per current site and by days since given (two
    queries). Workers on no site today are grouped under ``site_id`` ``None``.
    ``site``: only the workers on that site today."""
    total = AgeBuckets(ADVANCE_AGES)
    owed = list(
        Advance.objects.filter(worker__in=workers, balance__gt=0).values_list(
            "worker_id", "given_on", "balance"
        )
    )
    sites = current_sites({worker_id for worker_id, _given, _balance in owed}, today)
    only = getattr(site, "pk", site)
    by_site: dict[int | None, SiteAges] = {}
    for worker_id, given_on, balance in owed:
        current = sites.get(worker_id)
        key = current.pk if current else None
        if only is not None and key != only:
            continue
        if key not in by_site:
            by_site[key] = SiteAges(key, current.name if current else "", AgeBuckets(ADVANCE_AGES))
        age = _advance_age((today - given_on).days)
        by_site[key].ages.add(age, balance)
        total.add(age, balance)
    ordered = sorted(
        by_site.values(), key=lambda item: (item.site_id is None, -item.ages.total, item.name)
    )
    return ordered, total


# --- integrity checks ---------------------------------------------------------------------

LIST_LIMIT = 50  # rows listed per check (the counts are complete)


@dataclass
class PersonRef:
    worker_id: int
    name: str
    number: str


@dataclass
class SharedPayout:
    """Several active workers paid to the same mobile-money number or bank account."""

    method: str
    account: str  # as written on the first account
    workers: list[PersonRef]


def _digits(text: str) -> str:
    return re.sub(r"\D", "", text or "")


def shared_payout_accounts(workers) -> list[SharedPayout]:
    """Payout accounts that two or more of the active ``workers`` share (one query).

    Mobile numbers are compared by their digits, bank accounts by bank code and number.
    Families and crews do share phones: these are things to check, not errors.
    """
    groups: dict[tuple, SharedPayout] = {}
    for values in (
        PayoutAccount.objects.filter(worker__in=workers, worker__is_active=True)
        .values(
            "method",
            "mobile_number",
            "bank_code",
            "account_number",
            "worker_id",
            "worker__first_name",
            "worker__last_name",
            "worker__number",
        )
        .order_by("worker__last_name", "worker__first_name", "worker_id")
    ):
        if values["method"] == TransferMethod.MOBILE_MONEY:
            number = _digits(values["mobile_number"])
            key, shown = (values["method"], number), values["mobile_number"]
        else:
            number = (values["account_number"] or "").upper()
            key = (values["method"], (values["bank_code"] or "").upper(), number)
            shown = values["account_number"]
        if not number:
            continue
        person = PersonRef(
            worker_id=values["worker_id"],
            name=f"{values['worker__first_name']} {values['worker__last_name']}".strip(),
            number=values["worker__number"],
        )
        if key not in groups:
            groups[key] = SharedPayout(method=values["method"], account=shown, workers=[])
        groups[key].workers.append(person)
    shared = [group for group in groups.values() if len(group.workers) > 1]
    shared.sort(key=lambda group: (-len(group.workers), group.account))
    return shared


class Mismatch:
    WORKED_WITHOUT_CHECK_IN = "worked_without_check_in"
    ABSENT_WITH_CHECK_IN = "absent_with_check_in"


@dataclass
class CheckInMismatch:
    kind: str
    site_id: int
    site_name: str
    date: datetime.date
    worker_id: int
    name: str
    status: str


@dataclass
class MismatchSummary:
    items: list[CheckInMismatch]  # the first LIST_LIMIT, most recent first
    worked_without_check_in: int = 0
    absent_with_check_in: int = 0
    days_checked: int = 0  # site days that used check-in

    @property
    def count(self) -> int:
        return self.worked_without_check_in + self.absent_with_check_in


def check_in_mismatches(sites, start: datetime.date, end: datetime.date) -> MismatchSummary:
    """The roll call against the check-ins, on the days a site used check-in (someone
    checked in): marked worked without checking in, or absent although checked in (two
    queries). Days without any check-in are not compared."""
    checked_in: set[tuple[int, datetime.date, int]] = set()
    site_days: set[tuple[int, datetime.date]] = set()
    for site_id, date, worker_id in CheckIn.objects.filter(
        site__in=_ids(sites), date__gte=start, date__lte=end, direction=CheckIn.Direction.IN
    ).values_list("site_id", "date", "worker_id"):
        site_days.add((site_id, date))
        if worker_id is not None:
            checked_in.add((site_id, date, worker_id))
    summary = MismatchSummary(items=[], days_checked=len(site_days))
    if not site_days:
        return summary
    used = sorted({site_id for site_id, _date in site_days})
    records = (
        AttendanceRecord.objects.filter(
            day__site__in=used,
            day__date__gte=start,
            day__date__lte=end,
            day__status__in=REPORTED_DAY_STATUSES,
        )
        .values_list(
            "day__site_id",
            "day__site__name",
            "day__date",
            "worker_id",
            "worker__first_name",
            "worker__last_name",
            "status",
        )
        .order_by("-day__date", "day__site__name", "worker__last_name", "worker__first_name")
    )
    for site_id, site_name, date, worker_id, first, last, status in records:
        if (site_id, date) not in site_days:
            continue
        here = (site_id, date, worker_id) in checked_in
        if status in WORKED and not here:
            kind = Mismatch.WORKED_WITHOUT_CHECK_IN
            summary.worked_without_check_in += 1
        elif status == Status.ABSENT and here:
            kind = Mismatch.ABSENT_WITH_CHECK_IN
            summary.absent_with_check_in += 1
        else:
            continue
        if len(summary.items) < LIST_LIMIT:
            summary.items.append(
                CheckInMismatch(
                    kind=kind,
                    site_id=site_id,
                    site_name=site_name,
                    date=date,
                    worker_id=worker_id,
                    name=f"{first} {last}".strip(),
                    status=status,
                )
            )
    return summary


@dataclass
class ForemanCorrections:
    """Corrections to one foreman's roll calls in the range."""

    foreman_id: int | None
    name: str
    records: int = 0  # records on their submitted, approved or locked days
    corrections: int = 0
    after_approval: int = 0  # of which made while the day was approved

    @property
    def per_hundred(self) -> Decimal | None:
        """Corrections per 100 records (one decimal)."""
        if not self.records:
            return None
        return (Decimal(self.corrections) * 100 / self.records).quantize(
            Decimal("0.1"), rounding=ROUND_HALF_UP
        )


CORRECT_ACTION = "attendance.record.correct"


def corrections_by_foreman(
    sites, start: datetime.date, end: datetime.date
) -> list[ForemanCorrections]:
    """Per foreman (of the day): records, corrections, and corrections made while the
    day was approved, read from the audit log since a correction can send the day back
    for approval (three queries). Most corrected first."""
    site_ids = _ids(sites)
    in_range = {
        "day__site__in": site_ids,
        "day__date__gte": start,
        "day__date__lte": end,
    }
    rows: dict[int | None, ForemanCorrections] = {}

    def row(foreman_id, first, last, username) -> ForemanCorrections:
        if foreman_id not in rows:
            name = f"{first or ''} {last or ''}".strip() or (username or "")
            rows[foreman_id] = ForemanCorrections(foreman_id=foreman_id, name=name)
        return rows[foreman_id]

    foreman = (
        "day__foreman_id",
        "day__foreman__first_name",
        "day__foreman__last_name",
        "day__foreman__username",
    )
    for values in (
        AttendanceRecord.objects.filter(**in_range, day__status__in=REPORTED_DAY_STATUSES)
        .values(*foreman)
        .annotate(n=Count("id"))
        .order_by()
    ):
        row(*(values[name] for name in foreman)).records = values["n"]

    corrected = {
        values["id"]: values
        for values in AttendanceRecord.objects.filter(**in_range, corrections__isnull=False)
        .values("id", *foreman)
        .annotate(n=Count("corrections"))
        .order_by()
    }
    for values in corrected.values():
        row(*(values[name] for name in foreman)).corrections += values["n"]
    if corrected:
        record_type = ContentType.objects.get_for_model(AttendanceRecord)
        for object_id, before in AuditLog.objects.filter(
            action=CORRECT_ACTION,
            content_type=record_type,
            object_id__in=[str(pk) for pk in corrected],
        ).values_list("object_id", "before"):
            if (before or {}).get("day_status") == AttendanceDay.Status.APPROVED:
                values = corrected[int(object_id)]
                row(*(values[name] for name in foreman)).after_approval += 1
    result = [item for item in rows.values() if item.corrections]
    result.sort(key=lambda item: (-item.corrections, -item.after_approval, item.name.lower()))
    return result


@dataclass
class SiteCheckIns:
    """What the check-ins of one site flagged in the range."""

    site_id: int
    name: str
    total: int = 0
    outside_area: int = 0
    unknown_badge: int = 0
    revoked_badge: int = 0
    not_on_crew: int = 0
    manual: int = 0

    @property
    def flagged(self) -> int:
        return self.outside_area + self.unknown_badge + self.revoked_badge + self.not_on_crew

    @property
    def outside_share(self) -> int | None:
        return _percent(self.outside_area, self.total)

    @property
    def manual_share(self) -> int | None:
        return _percent(self.manual, self.total)


def check_in_flags(sites, start: datetime.date, end: datetime.date) -> list[SiteCheckIns]:
    """Per site, the check-ins of the range and how many were flagged (one query), the
    most flagged first. Sites without check-ins are left out."""
    problem = CheckIn.BadgeProblem
    result = [
        SiteCheckIns(
            site_id=values["site_id"],
            name=values["site__name"],
            total=values["total"],
            outside_area=values["outside"],
            unknown_badge=values["unknown"],
            revoked_badge=values["revoked"],
            not_on_crew=values["off_crew"],
            manual=values["manual"],
        )
        for values in CheckIn.objects.filter(site__in=_ids(sites), date__gte=start, date__lte=end)
        .values("site_id", "site__name")
        .annotate(
            total=Count("id"),
            outside=Count("id", filter=Q(inside_geofence=False)),
            unknown=Count("id", filter=Q(badge_problem=problem.UNKNOWN)),
            revoked=Count("id", filter=Q(badge_problem=problem.REVOKED)),
            off_crew=Count("id", filter=Q(worker__isnull=False, on_crew=False)),
            manual=Count("id", filter=Q(source=CheckIn.Source.MANUAL)),
        )
        .order_by()
    ]
    result.sort(key=lambda item: (-item.flagged, item.name.lower()))
    return result
