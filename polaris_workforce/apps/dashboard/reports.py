"""Reports: labour cost, worker-days and attendance rates for a date range (read-only).

Which data counts:
- Attendance: records on days that were submitted, approved or locked (drafts and
  rejected days are not final). Present and late count as one worker-day; a half day
  counts as ``CompanySettings.half_day_units``.
- Attendance rate: worker-days ÷ recorded days (one recorded day per worker per
  attendance day, absences included).
- Labour cost: the gross of the pay lines of pay periods that **end** in the range, plus
  their correction adjustments (a recovered overpayment lowers the cost, a top-up raises it).
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
        }


def build_report(
    sites,
    start: datetime.date,
    end: datetime.date,
    *,
    trade=None,
    company: CompanySettings | None = None,
) -> Report:
    """The report for ``sites`` (a queryset or list of sites) between two dates inclusive."""
    company = company or CompanySettings.load()
    half_units = company.half_day_units
    site_ids = [getattr(site, "pk", site) for site in sites]

    records = AttendanceRecord.objects.filter(
        day__site__in=site_ids,
        day__date__gte=start,
        day__date__lte=end,
        day__status__in=REPORTED_DAY_STATUSES,
    )
    lines = PayLine.objects.filter(
        period__site__in=site_ids, period__end_date__gte=start, period__end_date__lte=end
    )
    if trade is not None:
        records = records.filter(worker__trade=trade)
        lines = lines.filter(worker__trade=trade)

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

    for values in (
        records.values(*worker_fields, "day__site_id", "day__site__name")
        .annotate(**status_counts)
        .order_by()
    ):
        row = row_for(values, values["day__site_id"], values["day__site__name"])
        row.present, row.half = values["present"], values["half"]
        row.late, row.absent = values["late"], values["absent"]
        row.days = worker_days(row.present, row.late, row.half, half_units)

    for values in (
        lines.values(*worker_fields, "period__site_id", "period__site__name")
        .annotate(gross=Sum(F("gross") + F("adjustment")))
        .order_by()
    ):
        row = row_for(values, values["period__site_id"], values["period__site__name"])
        row.earned = values["gross"] or 0

    workers = sorted(rows.values(), key=lambda row: (row.name.lower(), row.number, row.site_name))

    # Labour cost by site.
    costs: dict[int, SiteCost] = {}
    for values in (
        lines.values("period__site_id", "period__site__name", "period__site__labour_budget")
        .annotate(cost=Sum(F("gross") + F("adjustment")))
        .order_by()
    ):
        costs[values["period__site_id"]] = SiteCost(
            site_id=values["period__site_id"],
            name=values["period__site__name"],
            cost=values["cost"] or 0,
            budget=values["period__site__labour_budget"],
        )
    by_site = sorted(costs.values(), key=lambda item: (-item.cost, item.name))
    top = max((item.cost for item in by_site), default=0)
    for item in by_site:
        item.width_percent = round(100 * item.cost / top) if top else 0

    # Attendance rate by ISO week.
    weekly: dict[tuple[int, int], dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for values in records.values("day__date", "status").annotate(n=Count("id")).order_by():
        iso = values["day__date"].isocalendar()
        weekly[(iso.year, iso.week)][values["status"]] += values["n"]
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
    )


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
            _("Currency"),
        ],
        [14, 28, 16, 28, 10, 10, 10, 10, 12, 14, 14, 10],
    )
    formats = {9: "0.##", 10: "0%", 11: money_format}
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

    workbook.remove(workbook.worksheets[0])  # the empty default sheet
    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()
