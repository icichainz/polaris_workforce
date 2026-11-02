"""What the office API answers, as plain dicts (the serializers in ``api/`` are the contract).

Built for an AI assistant reading on behalf of an office user, so:

- Every query starts from the user's scope (``scoped_sites``, ``scoped_workers``).
- Money is an integer of minor units *and* the amount written the company's way
  (``{"amount": 350000, "display": "350 000 XOF"}``), so nobody has to place a decimal
  point.
- Personal data stays out: no phone numbers, ID documents, photos or emergency contacts.
- The figures come from the same selectors and services as the office screens.
"""

from __future__ import annotations

import datetime

from django.db.models import OuterRef, Q, Subquery, Sum

from apps.accounts.permissions import scoped_sites
from apps.attendance import selectors as attendance
from apps.attendance.models import AttendanceDay, AttendanceRecord
from apps.core import dates
from apps.core.display import format_amount
from apps.core.models import CompanySettings
from apps.dashboard.services import today_overview
from apps.payroll import services as payroll
from apps.payroll.models import Advance, PayLine
from apps.workers.models import Worker
from apps.workers.services import scoped_workers
from apps.worksites.models import SiteAssignment

from .models import AgentToken
from .services import may_approve

MAX_WORKERS = 25


def money(amount: int | None, company: CompanySettings, *, currency=None, decimals=None):
    if amount is None:
        return None
    display = format_amount(amount, company, currency=currency, decimals=decimals)
    return {"amount": amount, "display": display}


def _clock(value: datetime.time | None) -> str | None:
    return value.strftime("%H:%M") if value else None


def _local(value: datetime.datetime | None, tz) -> str | None:
    return value.astimezone(tz).isoformat(timespec="minutes") if value else None


def _person(user) -> str:
    if user is None:
        return ""
    return user.get_full_name() or user.get_username()


def _site(site) -> dict:
    return {"id": site.pk, "name": site.name}


def _worker(worker: Worker) -> dict:
    return {
        "id": worker.pk,
        "name": worker.full_name,
        "number": worker.number,
        "trade": worker.trade.name if worker.trade_id else "",
    }


# --- who am I --------------------------------------------------------------------------


def me_data(user, token: AgentToken) -> dict:
    company = CompanySettings.load()
    sites = scoped_sites(user).order_by("name")
    return {
        "user": {"id": user.pk, "name": _person(user), "role": user.role},
        "company": {
            "name": company.company_name or company.company.name,
            "currency": {"code": company.currency, "decimals": company.currency_decimals},
            "time_zone": company.time_zone,
            "today": dates.company_today(company),
        },
        "token": {
            "name": token.name,
            "can_approve": token.can_approve and may_approve(user),
            "expires_at": token.expires_at,
        },
        "sites": [{**_site(site), "status": site.status} for site in sites],
    }


# --- today -----------------------------------------------------------------------------


def today_data(user) -> dict:
    company = CompanySettings.load()
    overview = today_overview(user, company=company)
    return {
        "date": overview.today,
        "now": overview.now.isoformat(timespec="minutes"),
        "is_working_day": overview.is_working_day,
        "totals": {
            **{
                key: overview.totals.get(key, 0)
                for key in ("expected", "present", "absent", "half", "late")
            },
            "cash_tonight": money(overview.totals.get("cash_tonight", 0), company),
        },
        "sites": [
            {
                "site": _site(row.site),
                "foreman": row.foreman_name,
                "day_id": row.day.pk if row.day else None,
                "status": row.status,
                "roll_call_due": _clock(row.roll_call_due),
                "overdue": row.overdue,
                "expected": row.expected,
                "present": row.present,
                "absent": row.absent,
                "half": row.half,
                "late": row.late,
                "cash_tonight": money(row.cash_tonight, company),
            }
            for row in overview.sites
        ],
        "waiting_for_approval": len(overview.awaiting),
        "alerts": [
            {
                "kind": alert.kind,
                "severity": alert.severity,
                "site": alert.site_name,
                "site_id": alert.site_id,
                "date": alert.date,
                "count": alert.count,
                "person": alert.person,
                "detail": alert.detail,
                "worker_id": alert.worker_id,
                "day_id": alert.day_id,
            }
            for alert in overview.alerts
        ],
    }


# --- approvals -------------------------------------------------------------------------


def approvals_data(user) -> list[dict]:
    """Submitted days waiting for approval on the user's sites, oldest first."""
    tz = dates.company_tz()
    rows = []
    days = list(attendance.days_waiting_for_approval(user))
    expected = attendance.expected_for_days(days)
    for day in days:
        records = list(day.records.all())
        rows.append(
            {
                "day_id": day.pk,
                "site": _site(day.site),
                "date": day.date,
                "foreman": _person(day.submitted_by or day.foreman),
                "submitted_at": _local(day.client_submitted_at or day.submitted_at, tz),
                # With the crew expected that day (the Approvals card skips it), so an
                # assistant does not read "expected 0" as a fact.
                "counts": attendance.day_counts(expected[day.pk], records),
                "flags": [
                    str(flag)
                    for flag in attendance.approval_flags(
                        day, sum(1 for record in records if record.corrected)
                    )
                ],
            }
        )
    return rows


# --- one day ---------------------------------------------------------------------------


def _record(row: dict) -> dict:
    record: AttendanceRecord = row["record"]
    return {
        "worker": _worker(record.worker),
        "status": record.status,
        "arrival": _clock(record.arrival),
        "departure": _clock(record.departure),
        "note": record.note,
        "corrected": record.corrected,
        "changes": [{**change, "field": str(change["field"])} for change in row["changes"]],
        "paid_note": str(row["paid_note"]),
    }


def day_data(user, site, date: datetime.date) -> dict:
    """The roll call of ``site`` on ``date``, as the Approvals detail panel shows it."""
    tz = dates.company_tz()
    day = (
        AttendanceDay.objects.filter(site=site, date=date)
        .select_related("site", "foreman", "submitted_by", "approved_by")
        .first()
    )
    if day is None:
        expected = attendance.expected_worker_ids(site, date)
        return {
            "day_id": None,
            "site": _site(site),
            "date": date,
            "status": attendance.NOT_STARTED,
            "foreman": "",
            "submitted_at": None,
            "approved_at": None,
            "approved_by": "",
            "rejection_note": "",
            "foreman_note": "",
            "counts": attendance.day_counts(expected, []),
            "flags": [],
            "records": [],
            "may_decide": False,
        }
    detail = attendance.approval_detail(day, tz)
    return {
        "day_id": day.pk,
        "site": _site(site),
        "date": day.date,
        "status": day.status,
        "foreman": _person(detail["foreman"]),
        "submitted_at": _local(day.client_submitted_at or day.submitted_at, tz),
        "approved_at": _local(day.approved_at, tz),
        "approved_by": _person(day.approved_by),
        "rejection_note": day.rejection_note,
        "foreman_note": day.foreman_note,
        "counts": detail["counts"],
        "flags": [str(flag) for flag in detail["flags"]],
        "records": [_record(row) for row in detail["rows"]],
        "may_decide": day.status == AttendanceDay.Status.SUBMITTED,
    }


# --- cash sheet ------------------------------------------------------------------------


def cash_sheet_data(site, date: datetime.date) -> dict:
    """Who is paid how much on ``site`` for the pay day ``date`` (read; nothing is paid)."""
    company = CompanySettings.load()
    sheet = payroll.read_cash_sheet(site, date)
    rec = sheet.reconciliation
    currency, decimals = sheet.currency, sheet.currency_decimals

    def amount(value):
        return money(value, company, currency=currency, decimals=decimals)

    return {
        "site": _site(site),
        "date": sheet.date,
        "status": sheet.status,
        "day_status": sheet.day_status,
        "currency": {"code": currency, "decimals": decimals},
        "cash_issued": amount(rec.cash_issued),
        "total_to_pay": amount(rec.total_to_pay),
        "paid_total": amount(rec.paid_total),
        "left_to_pay": amount(rec.left_to_pay),
        "cash_to_pay": amount(rec.cash_to_pay),
        "cash_in_hand": amount(rec.cash_in_hand),
        "line_count": rec.line_count,
        "paid_count": rec.paid_count,
        "lines": [
            {
                "worker": _worker(line.worker),
                "units": line.line.units,
                "gross": amount(line.line.gross),
                "advance_deduction": amount(line.line.advance_deduction),
                "net": amount(line.line.net),
                "paid": amount(line.paid),
                "is_paid": line.is_paid,
            }
            for line in sheet.lines
        ],
        "absent": [_worker(worker) for worker in sheet.absent],
    }


# --- workers ---------------------------------------------------------------------------


def find_workers(user, query: str = "", site=None) -> list[dict]:
    today = dates.company_today()
    workers = scoped_workers(user, today).filter(is_active=True).select_related("trade")
    query = (query or "").strip()
    if query:
        workers = workers.filter(
            Q(first_name__icontains=query)
            | Q(last_name__icontains=query)
            | Q(number__icontains=query)
        )
    if site is not None:
        # The worker's current site, as payroll.current_site picks it (latest start first).
        current = (
            SiteAssignment.objects.active_on(today)
            .filter(worker=OuterRef("pk"))
            .order_by("-start_date", "-pk")
            .values("site_id")[:1]
        )
        workers = workers.annotate(current_site_id=Subquery(current)).filter(
            current_site_id=site.pk
        )
    workers = list(workers.order_by("last_name", "first_name", "pk")[:MAX_WORKERS])
    current = payroll.current_sites([worker.pk for worker in workers], today)
    # A worker of the user's site can be on another site today (or join next week):
    # that site is named only if the user may see it, as in ``worker_data``.
    visible = set(scoped_sites(user).values_list("pk", flat=True))
    sites = {pk: site for pk, site in current.items() if site.pk in visible}
    return [
        {**_worker(worker), "site": _site(sites[worker.pk]) if worker.pk in sites else None}
        for worker in workers
    ]


def worker_data(user, worker: Worker, start: datetime.date, end: datetime.date) -> dict:
    """A worker's roll-call marks and pay lines between ``start`` and ``end``, on the user's
    sites only (a site manager does not see what the worker did elsewhere)."""
    company = CompanySettings.load()
    sites = scoped_sites(user)
    records = (
        AttendanceRecord.objects.filter(
            worker=worker, day__site__in=sites, day__date__range=(start, end)
        )
        .select_related("day__site")
        .order_by("day__date", "day__site__name")
    )
    lines = (
        PayLine.objects.filter(
            worker=worker,
            period__site__in=sites,
            period__start_date__lte=end,
            period__end_date__gte=start,
        )
        .select_related("period__site")
        .annotate(paid=Sum("payments__amount"))
        .order_by("period__start_date", "pk")
    )
    advances = Advance.objects.filter(worker=worker, status=Advance.Status.OPEN)
    current = payroll.current_site(worker, dates.company_today(company))

    def line_amount(line, value):
        return money(
            value, company, currency=line.period.currency, decimals=line.period.currency_decimals
        )

    return {
        **_worker(worker),
        "active": worker.is_active,
        "site": _site(current) if current and sites.filter(pk=current.pk).exists() else None,
        "from": start,
        "to": end,
        "days": [
            {
                "date": record.day.date,
                "site": _site(record.day.site),
                "day_status": record.day.status,
                "status": record.status,
                "arrival": _clock(record.arrival),
                "departure": _clock(record.departure),
                "note": record.note,
                "corrected": record.corrected,
            }
            for record in records
        ],
        "pay": [
            {
                "site": _site(line.period.site),
                "start_date": line.period.start_date,
                "end_date": line.period.end_date,
                "period_status": line.period.status,
                "units": line.units,
                "gross": line_amount(line, line.gross),
                "advance_deduction": line_amount(line, line.advance_deduction),
                "statutory_deductions": line_amount(line, line.statutory_deductions),
                "other_deductions": line_amount(line, line.other_deductions),
                "adjustment": line_amount(line, line.adjustment),
                "net": line_amount(line, line.net),
                "paid": line_amount(line, line.paid or 0),
            }
            for line in lines
        ],
        "advance_balance": money(advances.aggregate(total=Sum("balance"))["total"] or 0, company),
    }
