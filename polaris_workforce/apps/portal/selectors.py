"""Read-only figures for the worker portal pages. Strictly one worker's own data.

Every query here starts from the signed-in worker (``worker=…`` / ``pay_line__worker=…``):
a worker never sees another worker's rows, whatever company or site. Callers run inside
``use_company(worker.company)`` (the portal views do), so settings and dates are the
worker's company's.
"""

from __future__ import annotations

import calendar
import datetime
from dataclasses import dataclass, field

from django.db.models import F, OuterRef, Q, Subquery, Sum
from django.db.models.functions import Coalesce
from django.utils.translation import gettext as _

from apps.attendance.models import AttendanceDay, AttendanceRecord
from apps.core import dates
from apps.core.choices import WEEKDAY_ABBRS
from apps.core.models import CompanySettings
from apps.payroll import labels
from apps.payroll.models import (
    Advance,
    AdvanceRepayment,
    PayAdjustment,
    PayLine,
    Payment,
    PayPeriod,
)
from apps.workers import services as worker_services

ATTENDANCE_STATUSES = ("present", "absent", "half_day", "late")
# Roll calls the worker sees: sent by the foreman (waiting for approval), approved, locked.
# Drafts are still being taken; a rejected day is being fixed by the foreman.
VISIBLE_DAY_STATUSES = (
    AttendanceDay.Status.SUBMITTED,
    AttendanceDay.Status.APPROVED,
    AttendanceDay.Status.LOCKED,
)


def month_from(value: str | None, today: datetime.date) -> datetime.date:
    """``"2025-09"`` → 1 Sep 2025; anything else (or a future month) → this month."""
    this_month = today.replace(day=1)
    try:
        year, month = (int(part) for part in (value or "").split("-"))
        first = datetime.date(year, month, 1)
    except ValueError:
        return this_month
    return min(first, this_month)


def shift_month(first: datetime.date, months: int) -> datetime.date:
    index = first.year * 12 + first.month - 1 + months
    return datetime.date(index // 12, index % 12 + 1, 1)


def _records(worker, first: datetime.date, last: datetime.date):
    return (
        AttendanceRecord.objects.filter(
            worker=worker,
            day__date__range=(first, last),
            day__status__in=VISIBLE_DAY_STATUSES,
        )
        .select_related("day__site")
        .order_by("day__date", "pk")
    )


def _day_kind(date, record, today, working_days) -> tuple[str, str]:
    if record is not None:
        return record.status, record.get_status_display()
    if date.isoweekday() not in working_days:
        return "off", _("Day off")
    if date > today:
        return "future", ""
    return "none", _("Not recorded")


def _cell(date, record, today, working_days) -> dict:
    kind, title = _day_kind(date, record, today, working_days)
    return {
        "date": date,
        "kind": kind,
        "title": title,
        "is_today": date == today,
        "waiting": record is not None and record.day.status == AttendanceDay.Status.SUBMITTED,
    }


def _day_pay(worker, first: datetime.date, last: datetime.date) -> dict:
    """``{date: (gross of that day, its pay period)}`` from the pay lines' day snapshots."""
    lines = PayLine.objects.filter(
        worker=worker, period__start_date__lte=last, period__end_date__gte=first
    ).select_related("period")
    pay = {}
    for line in lines:
        for day in line.days or []:
            try:
                date = datetime.date.fromisoformat(day.get("date", ""))
            except (TypeError, ValueError):
                continue
            if first <= date <= last:
                gross = int(day.get("gross") or 0)
                previous = pay.get(date)
                pay[date] = (gross + (previous[0] if previous else 0), line.period)
    return pay


# --- Home ------------------------------------------------------------------------------


def home(worker, company: CompanySettings) -> dict:
    today = dates.company_today(company)
    monday = today - datetime.timedelta(days=today.isoweekday() - 1)
    sunday = monday + datetime.timedelta(days=6)
    working_days = set(company.working_days or [])
    records = list(_records(worker, monday, sunday))
    by_date = {r.day.date: r for r in records}
    week = [
        _cell(
            monday + datetime.timedelta(days=n),
            by_date.get(monday + datetime.timedelta(days=n)),
            today,
            working_days,
        )
        for n in range(7)
    ]
    summary = worker_services.month_summary(records, company.half_day_units)
    last_payment = (
        Payment.objects.filter(pay_line__worker=worker)
        .select_related("pay_line__period__site")
        .order_by("-paid_at", "-pk")
        .first()
    )
    return {
        "today": today,
        "week": week,
        "week_units": summary["units"],
        "last_payment": last_payment,
        "last_payment_at": last_payment.paid_at.astimezone(dates.company_tz(company))
        if last_payment
        else None,
        "advance": worker_services.advance_summary(worker),
        "to_pay": _left_to_pay(worker),
    }


def _left_to_pay(worker) -> int:
    """What is still owed on the worker's pay lines past their open period (one query)."""
    paid = (
        Payment.objects.filter(pay_line=OuterRef("pk"))
        .values("pay_line")
        .annotate(total=Sum("amount"))
        .values("total")
    )
    return (
        PayLine.objects.filter(worker=worker)
        .exclude(period__status=PayPeriod.Status.OPEN)
        .annotate(paid=Coalesce(Subquery(paid), 0))
        .filter(net__gt=F("paid"))
        .aggregate(total=Sum(F("net") - F("paid")))["total"]
        or 0
    )


# --- Days ------------------------------------------------------------------------------


def month_days(worker, company: CompanySettings, first: datetime.date) -> dict:
    today = dates.company_today(company)
    last = shift_month(first, 1) - datetime.timedelta(days=1)
    working_days = set(company.working_days or [])
    records = list(_records(worker, first, last))
    by_date = {r.day.date: r for r in records}
    weeks = []
    for week in calendar.Calendar(firstweekday=0).monthdatescalendar(first.year, first.month):
        weeks.append(
            [
                _cell(date, by_date.get(date), today, working_days)
                if date.month == first.month
                else None
                for date in week
            ]
        )
    pay = _day_pay(worker, first, last)
    rows = []
    for record in reversed(records):
        gross, period = pay.get(record.day.date, (None, None))
        rows.append(
            {
                "record": record,
                "date": record.day.date,
                "site": record.day.site.name,
                "waiting": record.day.status == AttendanceDay.Status.SUBMITTED,
                "gross": gross,
                "period": period,
            }
        )
    summary = worker_services.month_summary(records, company.half_day_units)
    counts = summary["counts"]
    next_month = shift_month(first, 1)
    return {
        "month": first,
        "prev_month": shift_month(first, -1),
        "next_month": next_month if next_month <= today else None,
        "cells": [cell for week in weeks for cell in week],
        "weekday_names": WEEKDAY_ABBRS,
        "rows": rows,
        "units": summary["units"],
        "counts": {status: counts[status] for status in ATTENDANCE_STATUSES},
    }


# --- Pay -------------------------------------------------------------------------------


def _lines(worker):
    """The worker's pay lines with what was paid on each (``paid``), newest first.

    Lines with nothing on them (a day absent, nothing deducted, nothing paid) are left
    out.
    """
    empty = Q(gross=0, net=0, adjustment=0, advance_deduction=0, other_deductions=0)
    return (
        PayLine.objects.filter(worker=worker)
        .select_related("period__site")
        .prefetch_related("bonuses")
        .annotate(paid=Coalesce(Sum("payments__amount"), 0))
        .exclude(empty & Q(paid=0))
        .order_by("-period__end_date", "-pk")
    )


@dataclass
class PayRow:
    line: PayLine
    period_label: str
    worked: str
    deductions: int
    adjustment_size: int  # the adjustment without its sign (the template writes + or −)
    state: str  # paid | part_paid | to_pay | in_progress
    state_label: str
    # Wave 4 pay rules, as on the payslip: piece work and bonuses (in the gross), and the
    # statutory deductions (employee share, one per rule, in ``deductions``).
    pieces: list[dict] = field(default_factory=list)  # {label, amount}
    bonuses: list = field(default_factory=list)  # Bonus rows paid on this line
    statutory: list[dict] = field(default_factory=list)  # {label, amount}


def _state(line: PayLine) -> tuple[str, str]:
    if line.paid and line.paid >= line.net:
        return "paid", _("Paid")
    if line.paid:
        return "part_paid", _("Partly paid")
    if line.period.status == PayPeriod.Status.OPEN:
        return "in_progress", _("In progress")
    return "to_pay", _("To pay")


def pay_lines(worker):
    return _lines(worker)


def pay_rows(lines) -> list[PayRow]:
    rows = []
    for line in lines:
        state, state_label = _state(line)
        rows.append(
            PayRow(
                line=line,
                period_label=labels.period_label(line.period),
                worked=labels.worked_label(line, line.period.currency_decimals),
                deductions=(
                    line.advance_deduction + line.other_deductions + line.statutory_deductions
                ),
                adjustment_size=abs(line.adjustment),
                state=state,
                state_label=state_label,
                pieces=[
                    {
                        "label": labels.piece_label(piece, line.period.currency_decimals),
                        "amount": int(piece.get("amount") or 0),
                    }
                    for piece in line.pieces or []
                ],
                bonuses=sorted(line.bonuses.all(), key=lambda b: (b.date, b.pk)),
                statutory=[
                    {
                        "label": _("%(name)s (%(percent)s %%)")
                        % {
                            "name": item.get("name", ""),
                            "percent": labels.number(item.get("employee_percent", "0")),
                        },
                        "amount": int(item["employee"]),
                    }
                    for item in line.deductions or []
                    if item.get("employee")
                ],
            )
        )
    return rows


def payments(worker, company: CompanySettings, limit: int = 20) -> list[dict]:
    tz = dates.company_tz(company)
    rows = []
    for payment in (
        Payment.objects.filter(pay_line__worker=worker)
        .select_related("pay_line__period__site")
        .order_by("-paid_at", "-pk")[:limit]
    ):
        rows.append(
            {
                "payment": payment,
                "paid_at": payment.paid_at.astimezone(tz),
                "period": payment.pay_line.period,
                "site": payment.pay_line.period.site.name,
            }
        )
    return rows


def adjustments(worker) -> list[dict]:
    """Corrections of days already paid, settled (or to settle) on later pays."""
    rows = []
    for adjustment in (
        PayAdjustment.objects.filter(worker=worker)
        .select_related("site", "source_line__period")
        .order_by("-created_at", "-pk")
    ):
        if adjustment.applied_amount == adjustment.amount:
            state, state_label = "paid", _("Settled")
        elif adjustment.applied_amount:
            state, state_label = "part_paid", _("Partly settled")
        else:
            state, state_label = "to_pay", _("On a next pay")
        rows.append(
            {
                "adjustment": adjustment,
                "label": labels.adjustment_label(adjustment),
                "site": adjustment.site.name,
                "magnitude": abs(adjustment.amount),
                # Its currency snapshot (older rows may not have one: the paid period's).
                "source": adjustment if adjustment.currency else adjustment.source_line.period,
                "added": adjustment.amount > 0,
                "state": state,
                "state_label": state_label,
            }
        )
    return rows


def own_line(worker, line_id: int) -> PayLine | None:
    """One of the worker's own pay lines (``None`` for anyone else's)."""
    return (
        PayLine.objects.select_related("period__site", "worker__trade")
        .filter(pk=line_id, worker=worker)
        .first()
    )


# --- Advances --------------------------------------------------------------------------


def advances(worker) -> dict:
    items = list(Advance.objects.filter(worker=worker).order_by("-given_on", "-pk"))
    repayments = (
        AdvanceRepayment.objects.filter(advance__worker=worker)
        .select_related("pay_line__period")
        .order_by("-created_at", "-pk")
    )
    by_advance: dict[int, list] = {}
    for repayment in repayments:
        by_advance.setdefault(repayment.advance_id, []).append(
            {
                "repayment": repayment,
                "label": labels.period_label(repayment.pay_line.period)
                if repayment.pay_line_id
                else None,
            }
        )
    return {
        "summary": worker_services.advance_summary(worker),
        "items": [
            {
                "advance": advance,
                "repaid": advance.amount - advance.balance,
                "repayments": by_advance.get(advance.pk, []),
            }
            for advance in items
        ],
    }
