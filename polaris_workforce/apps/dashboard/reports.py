"""Reports: labour cost, worker-days and attendance rates for a date range (read-only).

Which data counts:
- Attendance: records on days that were submitted, approved or locked (drafts and
  rejected days are not final). Present and late count as one worker-day; a half day
  counts as ``CompanySettings.half_day_units``.
- Attendance rate: worker-days ÷ recorded days (one recorded day per worker per
  attendance day, absences included).
- Labour cost: the gross of the pay lines of pay periods that **end** in the range, plus
  their correction adjustments (a recovered overpayment lowers the cost, a top-up raises it).
- Statutory deductions (Wave 4 pay rules): per worker, the employee share deducted from
  their pay and the employer contributions on top; for the company, the employer cost
  (labour cost + employer contributions) and the split per deduction rule
  (``apps.payroll.pay_rules.employer_costs``).
- Whole months are read from rollups built ahead of time (``rollups``), the rest live.
- Every query is limited to the sites passed in, which callers take from
  ``scoped_sites(user)``.
"""

import csv
import datetime
import io
from collections import defaultdict
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal

from django.db.models import Count, F, Q, Sum
from django.utils.translation import gettext as _

from apps.attendance.models import AttendanceDay, AttendanceRecord
from apps.core.models import CompanySettings
from apps.core.money import from_minor
from apps.payroll.models import PayLine
from apps.payroll.pay_rules import EmployerCost, employer_cost, line_deduction_totals
from apps.precompute.models import (
    DeductionTotal,
    ReportMonth,
    SiteDayCounts,
    Stale,
    WorkerMonth,
)
from apps.worksites.models import Site

from . import rollups
from .services import worker_days

Status = AttendanceRecord.Status
REPORTED_DAY_STATUSES = (
    AttendanceDay.Status.SUBMITTED,
    AttendanceDay.Status.APPROVED,
    AttendanceDay.Status.LOCKED,
)
# The weekly chart's axis starts here, so small differences stay visible.
WEEK_AXIS_FLOOR = 50
ABSENCE_FLAG = 3


def rate_percent(days: Decimal, recorded: int) -> int | None:
    """Worker-days as a whole percentage of recorded days (``None`` when nothing recorded)."""
    if not recorded:
        return None
    return int((days * 100 / recorded).quantize(Decimal(1), rounding=ROUND_HALF_UP))


@dataclass
class WorkerRow:
    worker_id: int
    number: str
    name: str
    trade: str
    site_name: str
    present: int = 0
    half: int = 0
    late: int = 0
    absent: int = 0
    earned: int = 0
    statutory: int = 0  # deducted from the worker's pay (employee share)
    employer: int = 0  # employer contributions on top of the pay
    days: Decimal = Decimal(0)

    @property
    def recorded(self) -> int:
        return self.present + self.half + self.late + self.absent

    @property
    def rate(self) -> int | None:
        return rate_percent(self.days, self.recorded)

    @property
    def flag_absences(self) -> bool:
        return self.absent >= ABSENCE_FLAG


@dataclass
class SiteCost:
    site_id: int
    name: str
    cost: int
    budget: int | None
    width_percent: int = 0


@dataclass
class WeekRate:
    year: int
    week: int
    start: datetime.date
    rate: int | None
    height_percent: int = 0


@dataclass
class Report:
    start: datetime.date
    end: datetime.date
    currency: str
    currency_decimals: int
    labour_cost: int
    worker_days: Decimal
    recorded: int
    by_site: list[SiteCost] = field(default_factory=list)
    by_week: list[WeekRate] = field(default_factory=list)
    workers: list[WorkerRow] = field(default_factory=list)
    employer_cost: EmployerCost = field(default_factory=EmployerCost)

    @property
    def employer_total(self) -> int:
        """What the pay costs the company: labour cost + employer contributions."""
        return self.labour_cost + self.employer_cost.employer

    @property
    def attendance_rate(self) -> int | None:
        return rate_percent(self.worker_days, self.recorded)

    @property
    def cost_per_worker_day(self) -> int | None:
        if not self.worker_days:
            return None
        return int(
            (Decimal(self.labour_cost) / self.worker_days).quantize(
                Decimal(1), rounding=ROUND_HALF_UP
            )
        )

    @property
    def totals(self) -> dict:
        rows = self.workers
        return {
            "present": sum(row.present for row in rows),
            "half": sum(row.half for row in rows),
            "late": sum(row.late for row in rows),
            "absent": sum(row.absent for row in rows),
            "earned": sum(row.earned for row in rows),
            "statutory": sum(row.statutory for row in rows),
            "employer": sum(row.employer for row in rows),
        }


def build_report(
    sites,
    start: datetime.date,
    end: datetime.date,
    *,
    trade=None,
    company: CompanySettings | None = None,
) -> Report:
    """The report for ``sites`` (a queryset or list of sites) between two dates inclusive.

    Whole months already rolled up (``rollups``) are read from the rollups; the rest of
    the range (the months at its ends, months not built yet) from the records and pay
    lines. Both give the same figures.
    """
    company = company or CompanySettings.load()
    half_units = company.half_day_units
    site_ids = [getattr(site, "pk", site) for site in sites]
    trade_id = getattr(trade, "pk", trade)
    covered = _covered_months(site_ids, start, end)
    live = _uncovered_ranges(site_ids, start, end, covered)

    records = AttendanceRecord.objects.filter(
        _on_ranges(live, "day__site_id", "day__date"), day__status__in=REPORTED_DAY_STATUSES
    )
    lines = PayLine.objects.filter(_on_ranges(live, "period__site_id", "period__end_date"))
    worker_months = WorkerMonth.objects.filter(_on_months(covered))
    day_counts = SiteDayCounts.objects.filter(_on_covered_days(covered))
    deduction_totals = DeductionTotal.objects.filter(_on_months(covered))
    if trade_id is not None:
        records = records.filter(worker__trade_id=trade_id)
        lines = lines.filter(worker__trade_id=trade_id)
        worker_months = worker_months.filter(trade_id=trade_id)
        day_counts = day_counts.filter(trade_id=trade_id)
        deduction_totals = deduction_totals.filter(trade_id=trade_id)

    status_counts = {
        "present": Count("id", filter=Q(status=Status.PRESENT)),
        "half": Count("id", filter=Q(status=Status.HALF_DAY)),
        "late": Count("id", filter=Q(status=Status.LATE)),
        "absent": Count("id", filter=Q(status=Status.ABSENT)),
    }
    worker_fields = (
        "worker_id",
        "worker__number",
        "worker__first_name",
        "worker__last_name",
        "worker__trade__name",
    )

    rows: dict[tuple[int, int], WorkerRow] = {}

    def row_for(values, site_id, site_name) -> WorkerRow:
        key = (values["worker_id"], site_id)
        if key not in rows:
            rows[key] = WorkerRow(
                worker_id=values["worker_id"],
                number=values["worker__number"],
                name=f"{values['worker__first_name']} {values['worker__last_name']}".strip(),
                trade=values["worker__trade__name"] or "",
                site_name=site_name,
            )
        return rows[key]

    def add_attendance(row: WorkerRow, values) -> None:
        row.present += values["present"] or 0
        row.half += values["half"] or 0
        row.late += values["late"] or 0
        row.absent += values["absent"] or 0

    def add_pay(row: WorkerRow, values) -> None:
        row.earned += values["earned"] or 0
        row.statutory += values["statutory"] or 0
        row.employer += values["employer"] or 0

    for values in (
        records.values(*worker_fields, "day__site_id", "day__site__name")
        .annotate(**status_counts)
        .order_by()
    ):
        add_attendance(row_for(values, values["day__site_id"], values["day__site__name"]), values)
    for values in (
        lines.values(*worker_fields, "period__site_id", "period__site__name")
        .annotate(
            earned=Sum(F("gross") + F("adjustment")),
            statutory=Sum("statutory_deductions"),
            employer=Sum("employer_contributions"),
        )
        .order_by()
    ):
        add_pay(row_for(values, values["period__site_id"], values["period__site__name"]), values)
    for values in (
        worker_months.values(*worker_fields, "site_id", "site__name")
        .annotate(
            present=Sum("present"),
            half=Sum("half"),
            late=Sum("late"),
            absent=Sum("absent"),
            earned=Sum("earned"),
            statutory=Sum("statutory"),
            employer=Sum("employer"),
        )
        .order_by()
    ):
        row = row_for(values, values["site_id"], values["site__name"])
        add_attendance(row, values)
        add_pay(row, values)
    for row in rows.values():
        row.days = worker_days(row.present, row.late, row.half, half_units)

    workers = sorted(rows.values(), key=lambda row: (row.name.lower(), row.number, row.site_name))

    # Labour cost by site.
    cost_by_site: dict[int, int] = defaultdict(int)
    for site_id, cost in (
        lines.values("period__site_id")
        .annotate(cost=Sum(F("gross") + F("adjustment")))
        .order_by()
        .values_list("period__site_id", "cost")
    ):
        cost_by_site[site_id] += cost or 0
    for site_id, cost in (
        worker_months.values("site_id")
        .annotate(cost=Sum("earned"))
        .order_by()
        .values_list("site_id", "cost")
    ):
        cost_by_site[site_id] += cost or 0
    by_site = [
        SiteCost(
            site_id=site.pk,
            name=site.name,
            cost=cost_by_site[site.pk],
            budget=site.labour_budget,
        )
        for site in Site.objects.filter(pk__in=list(cost_by_site)).only("name", "labour_budget")
    ]
    by_site.sort(key=lambda item: (-item.cost, item.name))
    top = max((item.cost for item in by_site), default=0)
    for item in by_site:
        item.width_percent = round(100 * item.cost / top) if top else 0

    # Attendance rate by ISO week.
    weekly: dict[tuple[int, int], dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for values in records.values("day__date", "status").annotate(n=Count("id")).order_by():
        iso = values["day__date"].isocalendar()
        weekly[(iso.year, iso.week)][values["status"]] += values["n"]
    for values in (
        day_counts.values("date")
        .annotate(present=Sum("present"), half=Sum("half"), late=Sum("late"), absent=Sum("absent"))
        .order_by()
    ):
        iso = values["date"].isocalendar()
        week = weekly[(iso.year, iso.week)]
        week[Status.PRESENT] += values["present"] or 0
        week[Status.HALF_DAY] += values["half"] or 0
        week[Status.LATE] += values["late"] or 0
        week[Status.ABSENT] += values["absent"] or 0
    by_week = []
    week_start = start - datetime.timedelta(days=start.weekday())
    while week_start <= end:
        iso = week_start.isocalendar()
        counts = weekly.get((iso.year, iso.week), {})
        recorded = sum(counts.values())
        days = worker_days(
            counts.get(Status.PRESENT, 0),
            counts.get(Status.LATE, 0),
            counts.get(Status.HALF_DAY, 0),
            half_units,
        )
        rate = rate_percent(days, recorded)
        height = 0
        if rate is not None:
            span = 100 - WEEK_AXIS_FLOOR
            height = max(0, min(100, round(100 * (rate - WEEK_AXIS_FLOOR) / span)))
        by_week.append(
            WeekRate(
                year=iso.year, week=iso.week, start=week_start, rate=rate, height_percent=height
            )
        )
        week_start += datetime.timedelta(days=7)

    # Statutory deductions: the live lines' own, plus the rolled-up totals.
    totals, by_rule = line_deduction_totals(lines)
    rolled = worker_months.aggregate(
        gross=Sum("gross"), employee=Sum("statutory"), employer=Sum("employer")
    )
    for key in totals:
        totals[key] += rolled[key] or 0
    for rule_key, name, employee, employer in deduction_totals.values_list(
        "rule_key", "name", "employee", "employer"
    ):
        entry = by_rule[int(rule_key) if rule_key.isdigit() else rule_key]
        entry[0] = entry[0] or name
        entry[1] += employee
        entry[2] += employer

    return Report(
        start=start,
        end=end,
        currency=company.currency,
        currency_decimals=company.currency_decimals,
        labour_cost=sum(item.cost for item in by_site),
        worker_days=sum((row.days for row in workers), Decimal(0)),
        recorded=sum(row.recorded for row in workers),
        by_site=by_site,
        by_week=by_week,
        workers=workers,
        employer_cost=employer_cost(totals, by_rule),
    )


# --- which part of the range the rollups cover -------------------------------------------


def _covered_months(site_ids, start, end) -> dict[int, list[datetime.date]]:
    """``{site_id: [month, …]}``: the whole months of the range whose rollups are current:
    built, not flagged since (a pay period of the month changed), and not in the dates a
    change marked out of date and the rebuild has not reached yet. The rest is read live,
    so reading a report never waits for a rebuild nor starts one."""
    months = [
        month
        for month in rollups.months_between(start, end)
        if month >= start and rollups.month_end(month) <= end
    ]
    covered: dict[int, list[datetime.date]] = defaultdict(list)
    if not months:
        return covered
    marked = {stale.site_id: stale for stale in Stale.objects.filter(site_id__in=site_ids)}
    for site_id, month in ReportMonth.objects.filter(
        site_id__in=site_ids, month__in=months, stale=False
    ).values_list("site_id", "month"):
        if not _marked_in(marked.get(site_id), month):
            covered[site_id].append(month)
    return covered


def _marked_in(mark, month: datetime.date) -> bool:
    """A change marked dates of ``month`` out of date (``Stale``), not rebuilt yet."""
    if mark is None:
        return False
    starts_before_its_end = mark.start is None or mark.start <= rollups.month_end(month)
    return starts_before_its_end and mark.end >= month


def _uncovered_ranges(site_ids, start, end, covered) -> dict[int, list[tuple]]:
    """``{site_id: [(first, last), …]}``: the dates of the range read live."""
    ranges: dict[int, list[tuple]] = {}
    for site_id in site_ids:
        gaps, cursor = [], start
        for month in sorted(covered.get(site_id, [])):
            if cursor < month:
                gaps.append((cursor, month - datetime.timedelta(days=1)))
            cursor = rollups.month_end(month) + datetime.timedelta(days=1)
        if cursor <= end:
            gaps.append((cursor, end))
        if gaps:
            ranges[site_id] = gaps
    return ranges


def _on_ranges(ranges, site_field: str, date_field: str) -> Q:
    """Rows of the sites' date ranges (none when there is no range)."""
    found = Q(pk__in=[])
    for site_id, gaps in ranges.items():
        for first, last in gaps:
            found |= Q(
                **{site_field: site_id, f"{date_field}__gte": first, f"{date_field}__lte": last}
            )
    return found


def _on_months(covered) -> Q:
    """Rollup rows of the sites' covered months."""
    found = Q(pk__in=[])
    for site_id, months in covered.items():
        found |= Q(site_id=site_id, month__in=months)
    return found


def _on_covered_days(covered) -> Q:
    """Day counts of the sites' covered months."""
    found = Q(pk__in=[])
    for site_id, months in covered.items():
        for month in months:
            found |= Q(site_id=site_id, date__gte=month, date__lte=rollups.month_end(month))
    return found


def _plain_days(value: Decimal) -> str:
    text = format(value.normalize(), "f") if value else "0"
    return text


FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


def _text_cell(value: str) -> str:
    """Typed text (names, trades, sites) that Excel must not run as a formula."""
    return f"'{value}" if value.startswith(FORMULA_PREFIXES) else value


def write_csv(report: Report, out) -> None:
    """Attendance by worker as CSV (comma separated, "." decimals, money in major units).

    ``out`` is a text stream. The caller writes the UTF-8 byte-order mark so Excel opens
    accents correctly.
    """
    writer = csv.writer(out)
    writer.writerow(
        [
            _("Worker number"),
            _("Worker"),
            _("Trade"),
            _("Site"),
            _("Present"),
            _("Half days"),
            _("Late"),
            _("Absent"),
            _("Worker-days"),
            _("Attendance rate (%)"),
            _("Earned"),
            _("Statutory deductions"),
            _("Employer contributions"),
            _("Currency"),
        ]
    )
    decimals = report.currency_decimals
    for row in report.workers:
        writer.writerow(
            [
                _text_cell(row.number),
                _text_cell(row.name),
                _text_cell(row.trade),
                _text_cell(row.site_name),
                row.present,
                row.half,
                row.late,
                row.absent,
                _plain_days(row.days),
                "" if row.rate is None else row.rate,
                from_minor(row.earned, decimals),
                from_minor(row.statutory, decimals),
                from_minor(row.employer, decimals),
                report.currency,
            ]
        )
    totals = report.totals
    writer.writerow(
        [
            "",
            _("Total"),
            "",
            "",
            totals["present"],
            totals["half"],
            totals["late"],
            totals["absent"],
            _plain_days(report.worker_days),
            "" if report.attendance_rate is None else report.attendance_rate,
            from_minor(totals["earned"], decimals),
            from_minor(totals["statutory"], decimals),
            from_minor(totals["employer"], decimals),
            report.currency,
        ]
    )


# --- Excel ------------------------------------------------------------------------------

XLSX_CONTENT_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def _money_format(decimals: int) -> str:
    return "#,##0" if decimals == 0 else "#,##0." + "0" * decimals


def _percent(rate: int | None) -> Decimal | None:
    """A whole-number rate (85) as the fraction Excel shows with a % format (0.85)."""
    return None if rate is None else Decimal(rate) / 100


def write_xlsx(report: Report) -> bytes:
    """The report as an Excel workbook: one sheet per section (by worker, by site, by week).

    Same columns as the CSV, with real numbers and dates (money in major units with the
    currency's decimals, rates as percentages) and a bold totals row. Typed text (names,
    trades, sites) is always stored as text, never as a formula.
    """
    # Imported here: only the Excel exports need openpyxl.
    from openpyxl import Workbook
    from openpyxl.styles import Font
    from openpyxl.utils import get_column_letter

    bold = Font(bold=True)
    money_format = _money_format(report.currency_decimals)
    decimals = report.currency_decimals
    workbook = Workbook()

    def sheet(title: str, headers: list[str], widths: list[int]):
        ws = workbook.create_sheet(title[:31])
        for index, (header, width) in enumerate(zip(headers, widths, strict=True), 1):
            cell = ws.cell(row=1, column=index, value=str(header))
            cell.data_type = "s"
            cell.font = bold
            ws.column_dimensions[get_column_letter(index)].width = width
        ws.freeze_panes = "A2"
        return ws

    def put(ws, row: int, values: list, formats: dict[int, str], *, total=False) -> None:
        for index, value in enumerate(values, 1):
            cell = ws.cell(row=row, column=index, value=value)
            if isinstance(value, str):
                cell.data_type = "s"  # "=1+1" typed as a name stays text
            if index in formats and value is not None and not isinstance(value, str):
                cell.number_format = formats[index]
            if total:
                cell.font = bold

    # By worker: the CSV's columns.
    ws = sheet(
        _("By worker"),
        [
            _("Worker number"),
            _("Worker"),
            _("Trade"),
            _("Site"),
            _("Present"),
            _("Half days"),
            _("Late"),
            _("Absent"),
            _("Worker-days"),
            _("Attendance rate (%)"),
            _("Earned"),
            _("Statutory deductions"),
            _("Employer contributions"),
            _("Currency"),
        ],
        [14, 28, 16, 28, 10, 10, 10, 10, 12, 14, 14, 14, 14, 10],
    )
    formats = {9: "0.##", 10: "0%", 11: money_format, 12: money_format, 13: money_format}
    row_number = 1
    for row_number, row in enumerate(report.workers, 2):
        put(
            ws,
            row_number,
            [
                row.number,
                row.name,
                row.trade,
                row.site_name,
                row.present,
                row.half,
                row.late,
                row.absent,
                row.days,
                _percent(row.rate),
                from_minor(row.earned, decimals),
                from_minor(row.statutory, decimals),
                from_minor(row.employer, decimals),
                report.currency,
            ],
            formats,
        )
    totals = report.totals
    put(
        ws,
        row_number + 1,
        [
            None,
            _("Total"),
            None,
            None,
            totals["present"],
            totals["half"],
            totals["late"],
            totals["absent"],
            report.worker_days,
            _percent(report.attendance_rate),
            from_minor(totals["earned"], decimals),
            from_minor(totals["statutory"], decimals),
            from_minor(totals["employer"], decimals),
            report.currency,
        ],
        formats,
        total=True,
    )

    # By site: labour cost (and the site's labour budget).
    ws = sheet(
        _("By site"),
        [_("Site"), _("Labour cost"), _("Labour budget"), _("Currency")],
        [32, 16, 16, 10],
    )
    formats = {2: money_format, 3: money_format}
    row_number = 1
    for row_number, item in enumerate(report.by_site, 2):
        budget = from_minor(item.budget, decimals) if item.budget is not None else None
        put(
            ws,
            row_number,
            [item.name, from_minor(item.cost, decimals), budget, report.currency],
            formats,
        )
    put(
        ws,
        row_number + 1,
        [_("Total"), from_minor(report.labour_cost, decimals), None, report.currency],
        formats,
        total=True,
    )

    # By week: attendance rate per ISO week.
    ws = sheet(
        _("By week"),
        [_("Year"), _("Week"), _("Week starting"), _("Attendance rate (%)")],
        [8, 8, 14, 18],
    )
    formats = {3: "yyyy-mm-dd", 4: "0%"}
    row_number = 1
    for row_number, week in enumerate(report.by_week, 2):
        put(ws, row_number, [week.year, week.week, week.start, _percent(week.rate)], formats)
    put(
        ws,
        row_number + 1,
        [_("Total"), None, None, _percent(report.attendance_rate)],
        formats,
        total=True,
    )

    # Employer cost: statutory deductions per rule, then the company's total cost.
    cost = report.employer_cost
    ws = sheet(
        _("Employer cost"),
        [_("Deduction rule"), _("Deducted from pay"), _("Employer contributions"), _("Currency")],
        [32, 18, 22, 10],
    )
    formats = {2: money_format, 3: money_format}
    row_number = 1
    for row_number, rule in enumerate(cost.rules, 2):
        put(
            ws,
            row_number,
            [
                rule.name,
                from_minor(rule.employee, decimals),
                from_minor(rule.employer, decimals),
                report.currency,
            ],
            formats,
        )
    row_number += 1
    put(
        ws,
        row_number,
        [
            _("Total"),
            from_minor(cost.employee, decimals),
            from_minor(cost.employer, decimals),
            report.currency,
        ],
        formats,
        total=True,
    )
    for label, amount in (
        (_("Labour cost"), report.labour_cost),
        (_("Employer cost (labour cost + employer contributions)"), report.employer_total),
    ):
        row_number += 1
        put(ws, row_number, [label, from_minor(amount, decimals), None, report.currency], formats)

    workbook.remove(workbook.worksheets[0])  # the empty default sheet
    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()
