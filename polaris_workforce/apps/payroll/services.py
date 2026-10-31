"""Payroll services: pay periods, pay lines, cash sheets, payments and advances.

The arithmetic is in ``apps.payroll.engine``; this module reads the settings and the
attendance, stores the results and enforces the workflow:

    open → ready (every day approved) → paying → paid → locked

* **Pay periods** are generated per site from the ``pay_frequency`` setting (a worker may
  override it, so one site can have daily and weekly periods side by side). A period's
  pay day is its ``end_date``; the cash sheet of a site and date gathers every period
  that ends that day.
* **Pay lines** are recomputed from the attendance of the period whenever it is refreshed,
  so the cash sheet can be previewed before the roll call is approved. Only a period
  whose days are all approved (or locked) can be paid. A line that has a payment is
  frozen; a locked period never changes.
* **Advances** are repaid by the pay lines: when a line deducts, an ``AdvanceRepayment``
  row is written and the advance's balance goes down (and back up if the line is
  recomputed or closed unpaid).
* Every write records an ``AuditLog`` entry.
"""

from __future__ import annotations

import datetime
import zoneinfo
from collections import defaultdict
from dataclasses import dataclass, field

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.db.models import Q, Sum
from django.utils import timezone
from django.utils.translation import gettext as _

from apps.accounts.permissions import (
    Role,
    can_manage_site,
    can_view_site,
    has_role,
    is_admin,
    scoped_sites,
)
from apps.api.exceptions import Conflict, Locked
from apps.attendance.models import AttendanceDay, AttendanceRecord
from apps.core.choices import PayType, ProofType
from apps.core.models import AuditLog, CompanySettings
from apps.core.settings import resolve_setting
from apps.core.validators import UPLOAD_VALIDATORS
from apps.workers.models import PayRate, Worker
from apps.worksites.models import Site, SiteAssignment

from . import engine
from .models import Advance, AdvanceRepayment, CashIssue, PayLine, Payment, PayPeriod

PAYABLE_DAY_STATUSES = (AttendanceDay.Status.APPROVED, AttendanceDay.Status.LOCKED)

# The cash sheet as the phone sees it (apps.api.enums.PAYOUT_STATUSES).
SHEET_NOT_OPEN = "not_open"
SHEET_OPEN = "open"
SHEET_PAID = "paid"
SHEET_CLOSED = "closed"


# --- settings ----------------------------------------------------------------------------


def pay_rules(site: Site | None, company: CompanySettings) -> engine.PayRules:
    """The engine's rules for a site, resolved company → site."""

    def get(name):
        return resolve_setting(name, site, company=company)

    return engine.PayRules(
        half_day_units=get("half_day_units"),
        late_rule=get("late_rule"),
        work_start=get("work_start"),
        work_end=get("work_end"),
        break_minutes=get("break_minutes"),
        overtime_after_hours=get("overtime_after_hours"),
        overtime_multiplier=get("overtime_multiplier"),
    )


def company_today(company: CompanySettings | None = None) -> datetime.date:
    """Today in the company's time zone (a work day is a date there, not on the server)."""
    company = company or CompanySettings.load()
    try:
        zone = zoneinfo.ZoneInfo(company.time_zone)
    except (zoneinfo.ZoneInfoNotFoundError, ValueError):
        zone = timezone.get_current_timezone()
    return timezone.localdate(timezone=zone)


def period_bounds_for(
    site: Site, worker: Worker | None, day: datetime.date, company: CompanySettings
) -> tuple[datetime.date, datetime.date, str]:
    """``(start, end, frequency)`` of the period containing ``day`` for this worker."""
    frequency = resolve_setting("pay_frequency", site, worker, company=company)
    every_n_days = resolve_setting("pay_every_n_days", site, company=company)
    if frequency == engine.EVERY_N_DAYS and not every_n_days:
        frequency = engine.DAILY  # Misconfigured: pay daily rather than not at all.
    start, end = engine.period_bounds(
        day,
        frequency,
        every_n_days=every_n_days,
        anchor=site.started_on or engine.EPOCH,
        payday=engine.pay_weekday(company.working_days or []),
    )
    return start, end, frequency


def _audit(actor, action, target=None, *, before=None, after=None, reason=""):
    return AuditLog.record(
        actor=actor if getattr(actor, "is_authenticated", False) else None,
        action=action,
        target=target,
        before=before,
        after=after,
        reason=reason,
    )


# --- permissions -------------------------------------------------------------------------


def can_pay(user, site) -> bool:
    """Hand out pay on a site: its foremen, its manager, admins."""
    if can_manage_site(user, site):
        return True
    return has_role(user, Role.FOREMAN) and can_view_site(user, site)


def can_lock(user, site) -> bool:
    """Close and lock pay: admins, the site's manager, accountants."""
    if can_manage_site(user, site):
        return True
    return has_role(user, Role.ACCOUNTANT) and can_view_site(user, site)


def scoped_workers(user):
    """Workers on the sites this user may see (all workers for admins and accountants)."""
    if is_admin(user) or has_role(user, Role.ACCOUNTANT):
        return Worker.objects.all()
    return Worker.objects.filter(assignments__site__in=scoped_sites(user)).distinct()


def current_site(worker: Worker, on: datetime.date) -> Site | None:
    assignment = (
        SiteAssignment.objects.active_on(on)
        .filter(worker=worker)
        .select_related("site")
        .order_by("-start_date")
        .first()
    )
    return assignment.site if assignment else None


# --- pay lines ---------------------------------------------------------------------------


def _rates_by_worker(worker_ids, until: datetime.date) -> dict[int, list[PayRate]]:
    rates = defaultdict(list)
    for rate in PayRate.objects.filter(
        worker_id__in=worker_ids, effective_from__lte=until
    ).order_by("worker_id", "effective_from"):
        rates[rate.worker_id].append(rate)
    return rates


def _rate_on(rates: list[PayRate], day: datetime.date) -> PayRate | None:
    current = None
    for rate in rates:  # sorted by effective_from
        if rate.effective_from <= day:
            current = rate
    return current


def _day_input(record: AttendanceRecord, rate: PayRate | None) -> engine.DayInput:
    return engine.DayInput(
        date=record.day.date,
        status=record.status,
        pay_type=rate.pay_type if rate else PayType.DAILY,
        rate=rate.amount if rate else 0,
        arrival=record.arrival,
        departure=record.departure,
    )


def _line_snapshot(line: PayLine) -> dict:
    return {
        "worker_id": line.worker_id,
        "units": str(line.units),
        "hours": None if line.hours is None else str(line.hours),
        "overtime_hours": str(line.overtime_hours),
        "gross": line.gross,
        "advance_deduction": line.advance_deduction,
        "other_deductions": line.other_deductions,
        "net": line.net,
    }


def _period_totals(period: PayPeriod) -> dict:
    totals = period.lines.aggregate(
        gross=Sum("gross"), advance=Sum("advance_deduction"), net=Sum("net")
    )
    return {
        "status": period.status,
        "lines": period.lines.count(),
        "gross": totals["gross"] or 0,
        "advance_deduction": totals["advance"] or 0,
        "net": totals["net"] or 0,
    }


def _apply_repayments(line: PayLine, wanted: tuple[tuple[int, int], ...]) -> None:
    """Make the line's advance repayments equal ``wanted``, keeping balances in step."""
    current = {r.advance_id: r for r in line.advance_repayments.all()}
    wanted_by_advance = dict(wanted)
    if {k: r.amount for k, r in current.items()} == wanted_by_advance:
        return
    touched = set(current) | set(wanted_by_advance)
    advances = {a.pk: a for a in Advance.objects.filter(pk__in=touched)}
    for advance_id, repayment in current.items():
        advances[advance_id].balance += repayment.amount
        repayment.delete()
    for advance_id, amount in wanted_by_advance.items():
        AdvanceRepayment.objects.create(advance_id=advance_id, pay_line=line, amount=amount)
        advances[advance_id].balance -= amount
    for advance in advances.values():
        advance.status = Advance.Status.REPAID if advance.balance == 0 else Advance.Status.OPEN
        advance.save(update_fields=["balance", "status", "updated_at"])


def _open_advances(worker_id: int, before: datetime.date, line: PayLine | None):
    """Advances this pay may deduct from: given before the pay day, oldest first."""
    query = Q(balance__gt=0)
    if line is not None and line.pk:
        query |= Q(repayments__pay_line=line)
    return (
        Advance.objects.filter(worker_id=worker_id, given_on__lt=before)
        .filter(query)
        .distinct()
        .order_by("given_on", "pk")
    )


def _paid_amount(line: PayLine) -> int:
    return line.payments.aggregate(total=Sum("amount"))["total"] or 0


def refresh_period(
    period: PayPeriod,
    *,
    actor=None,
    today: datetime.date | None = None,
    company: CompanySettings | None = None,
) -> PayPeriod:
    """Recompute the period's unpaid lines from its attendance, then its status.

    Locked periods are left alone. Lines with a payment are frozen. Writes an AuditLog
    entry only when something changed, so refreshing twice is a no-op.
    """
    if period.is_locked:
        return period
    company = company or CompanySettings.load()
    site = period.site
    rules = pay_rules(site, company)

    records = (
        AttendanceRecord.objects.filter(
            day__site=site, day__date__gte=period.start_date, day__date__lte=period.end_date
        )
        .select_related("day", "worker")
        .order_by("day__date", "pk")
    )
    by_worker: dict[int, list[AttendanceRecord]] = defaultdict(list)
    for record in records:
        start, end, _frequency = period_bounds_for(site, record.worker, record.day.date, company)
        if (start, end) == (period.start_date, period.end_date):
            by_worker[record.worker_id].append(record)
    rates = _rates_by_worker(list(by_worker), period.end_date)

    with transaction.atomic():
        before = _period_totals(period)
        changed: list[int] = []
        lines = {
            line.worker_id: line
            for line in period.lines.prefetch_related("payments", "advance_repayments")
        }
        for worker_id, line in lines.items():
            if worker_id not in by_worker and not line.payments.all():
                _apply_repayments(line, ())
                line.delete()
                changed.append(worker_id)

        for worker_id, worker_records in by_worker.items():
            line = lines.get(worker_id)
            if line is not None and line.payments.all():
                continue  # Paid: frozen.
            days = [_day_input(r, _rate_on(rates[worker_id], r.day.date)) for r in worker_records]
            repaid_by_line = (
                {r.advance_id: r.amount for r in line.advance_repayments.all()} if line else {}
            )
            advances = [
                engine.AdvanceInput(
                    a.pk, a.balance + repaid_by_line.get(a.pk, 0), a.deduction_per_pay
                )
                for a in _open_advances(worker_id, period.end_date, line)
            ]
            other = line.other_deductions if line else 0
            result = engine.compute_line(days, rules, advances, other_deductions=other)
            worked = any(d.units > 0 or d.amount > 0 for d in result.days)
            if not worked:
                if line is not None:
                    _apply_repayments(line, ())
                    line.delete()
                    changed.append(worker_id)
                continue
            if line is None:
                line = PayLine(period=period, worker_id=worker_id)
            values = {
                "units": result.units,
                "hours": result.hours,
                "overtime_hours": result.overtime_hours,
                "gross": result.gross,
                "other_deductions": result.other_deductions,
                "advance_deduction": result.advance_deduction,
                "net": result.net,
                "days": [d.as_dict() for d in result.days],
            }
            dirty = line.pk is None or any(getattr(line, k) != v for k, v in values.items())
            if dirty:
                for name, value in values.items():
                    setattr(line, name, value)
                line.save()
                changed.append(worker_id)
            _apply_repayments(line, result.repayments)

        if changed:
            _audit(
                actor,
                "payroll.period.refresh",
                period,
                before=before,
                after={**_period_totals(period), "workers_changed": sorted(changed)},
            )
        update_period_status(period, actor=actor, today=today, company=company)
    return period


def _days_with_expected_crew(period: PayPeriod, company: CompanySettings) -> set[datetime.date]:
    """Working days of the period on which active workers are assigned to the site.

    Same rule as ``apps.attendance.selectors.expected_by_date``: assignment dates inclusive.
    """
    working_days = set(company.working_days or [])
    assignments = list(
        SiteAssignment.objects.filter(
            site=period.site, worker__is_active=True, start_date__lte=period.end_date
        )
        .filter(Q(end_date__isnull=True) | Q(end_date__gte=period.start_date))
        .values_list("start_date", "end_date")
    )
    dates = set()
    day = period.start_date
    while day <= period.end_date:
        if day.isoweekday() in working_days and any(
            first <= day and (last is None or last >= day) for first, last in assignments
        ):
            dates.add(day)
        day += datetime.timedelta(days=1)
    return dates


def period_is_ready(
    period: PayPeriod, today: datetime.date, company: CompanySettings | None = None
) -> bool:
    """The pay day has come and every roll call of the period is approved (or locked).

    A working day on which crew is expected needs its roll call; a day without one blocks
    the period (on a holiday the foreman submits the day with everyone absent).
    """
    if today < period.end_date:
        return False
    company = company or CompanySettings.load()
    days = dict(
        AttendanceDay.objects.filter(
            site=period.site, date__gte=period.start_date, date__lte=period.end_date
        ).values_list("date", "status")
    )
    if not days or not set(days.values()) <= set(PAYABLE_DAY_STATUSES):
        return False
    return _days_with_expected_crew(period, company) <= set(days)


def update_period_status(
    period: PayPeriod,
    *,
    actor=None,
    today: datetime.date | None = None,
    company: CompanySettings | None = None,
) -> str:
    """open → ready → paying → paid, from the attendance and the payments (never locks)."""
    if period.is_locked:
        return period.status
    today = today or company_today(company)
    lines = list(period.lines.annotate(paid=Sum("payments__amount")))
    has_payment = any(line.paid for line in lines)
    settled = bool(lines) and all((line.paid or 0) >= line.net for line in lines)
    ready = period_is_ready(period, today, company)
    if settled and (has_payment or ready):
        status = PayPeriod.Status.PAID
    elif has_payment:
        status = PayPeriod.Status.PAYING
    elif ready:
        status = PayPeriod.Status.READY
    else:
        status = PayPeriod.Status.OPEN
    if status != period.status:
        old = period.status
        period.status = status
        period.save(update_fields=["status", "updated_at"])
        _audit(
            actor, "payroll.period.status", period, before={"status": old}, after={"status": status}
        )
    return status


def ensure_period(
    site: Site, start: datetime.date, end: datetime.date, frequency: str
) -> PayPeriod:
    period, _created = PayPeriod.objects.get_or_create(
        site=site, start_date=start, end_date=end, defaults={"frequency": frequency}
    )
    return period


def sync_site(
    site: Site,
    start: datetime.date,
    end: datetime.date,
    *,
    ending_on: datetime.date | None = None,
    actor=None,
    today: datetime.date | None = None,
    company: CompanySettings | None = None,
) -> list[PayPeriod]:
    """Create and refresh the periods that hold attendance between ``start`` and ``end``.

    With ``ending_on``, only the periods paid that day (the day's cash sheet).
    """
    company = company or CompanySettings.load()
    today = today or company_today(company)
    wanted: dict[tuple[datetime.date, datetime.date], str] = {}
    records = AttendanceRecord.objects.filter(
        day__site=site, day__date__gte=start, day__date__lte=end
    ).select_related("worker", "day")
    for record in records:
        p_start, p_end, frequency = period_bounds_for(site, record.worker, record.day.date, company)
        if ending_on is None or p_end == ending_on:
            wanted.setdefault((p_start, p_end), frequency)

    existing = PayPeriod.objects.filter(site=site).exclude(status=PayPeriod.Status.LOCKED)
    if ending_on is not None:
        existing = existing.filter(end_date=ending_on)
    else:
        existing = existing.filter(start_date__lte=end, end_date__gte=start)
    periods = {(p.start_date, p.end_date): p for p in existing}
    for (p_start, p_end), frequency in wanted.items():
        if (p_start, p_end) not in periods:
            periods[(p_start, p_end)] = ensure_period(site, p_start, p_end, frequency)
    result = []
    for key in sorted(periods):
        result.append(refresh_period(periods[key], actor=actor, today=today, company=company))
    return result


def sync_sheet(site: Site, day: datetime.date, **kwargs) -> list[PayPeriod]:
    """Bring the periods paid on ``day`` up to date (all frequencies)."""
    company = kwargs.setdefault("company", CompanySettings.load())
    lookback = max(31, resolve_setting("pay_every_n_days", site, company=company) or 0)
    return sync_site(site, day - datetime.timedelta(days=lookback), day, ending_on=day, **kwargs)


def on_attendance_changed(day: AttendanceDay, *, actor=None) -> None:
    """Hook for the attendance services: refresh pay after a day is approved or corrected."""
    sync_site(day.site, day.date, day.date, actor=actor)


# --- cash sheets -------------------------------------------------------------------------


@dataclass
class SheetLine:
    line: PayLine
    payment: Payment | None
    paid: int

    @property
    def worker(self) -> Worker:
        return self.line.worker

    @property
    def is_paid(self) -> bool:
        return self.paid >= self.line.net

    @property
    def remaining(self) -> int:
        return max(0, self.line.net - self.paid)


@dataclass
class CashSheet:
    site: Site
    date: datetime.date
    periods: list[PayPeriod]
    lines: list[SheetLine]
    cash_issues: list[CashIssue]
    day: AttendanceDay | None
    reconciliation: engine.CashReconciliation
    currency: str
    currency_decimals: int
    status: str = SHEET_NOT_OPEN
    absent: list[Worker] = field(default_factory=list)

    @property
    def is_closed(self) -> bool:
        return self.status == SHEET_CLOSED

    @property
    def can_pay(self) -> bool:
        return self.status == SHEET_OPEN

    @property
    def day_status(self) -> str:
        return self.day.status if self.day else "not_started"


def _sheet_status(
    periods: list[PayPeriod], lines: list[SheetLine], today, company: CompanySettings
) -> str:
    if not periods:
        return SHEET_NOT_OPEN
    if all(p.is_locked for p in periods):
        return SHEET_CLOSED
    if not all(period_is_ready(p, today, company) for p in periods if not p.is_locked):
        return SHEET_NOT_OPEN
    if all(line.is_paid for line in lines):
        return SHEET_PAID
    return SHEET_OPEN


def sheet_status(site: Site, day: datetime.date) -> str:
    """The phone's ``payout_status`` for a site and pay day (read only, no refresh)."""
    company = CompanySettings.load()
    periods = list(PayPeriod.objects.filter(site=site, end_date=day))
    lines = [
        SheetLine(line=line, payment=None, paid=line.paid or 0)
        for line in PayLine.objects.filter(period__in=periods).annotate(
            paid=Sum("payments__amount")
        )
    ]
    return _sheet_status(periods, lines, company_today(company), company)


def cash_sheet(
    site: Site,
    day: datetime.date,
    *,
    refresh: bool = True,
    actor=None,
    today: datetime.date | None = None,
) -> CashSheet:
    """The cash sheet of a site for a pay day, with the reconciliation."""
    company = CompanySettings.load()
    if refresh:
        sync_sheet(site, day, actor=actor, today=today, company=company)
    periods = list(PayPeriod.objects.filter(site=site, end_date=day).order_by("start_date"))
    pay_lines = (
        PayLine.objects.filter(period__in=periods)
        .select_related("worker__trade", "period")
        .prefetch_related("payments__paid_by")
        .order_by("worker__last_name", "worker__first_name", "worker__number")
    )
    lines = []
    for line in pay_lines:
        payments = list(line.payments.all())
        lines.append(
            SheetLine(
                line=line,
                payment=payments[0] if payments else None,
                paid=sum(p.amount for p in payments),
            )
        )
    issues = list(CashIssue.objects.filter(site=site, date=day).select_related("received_by"))
    reconciliation = engine.reconcile(
        sum(issue.amount for issue in issues), [(sl.line.net, sl.paid) for sl in lines]
    )
    attendance_day = AttendanceDay.objects.filter(site=site, date=day).first()
    absent = []
    if attendance_day is not None:
        on_sheet = {sl.line.worker_id for sl in lines}
        absent = [
            r.worker
            for r in attendance_day.records.select_related("worker").filter(
                status=AttendanceRecord.Status.ABSENT
            )
            if r.worker_id not in on_sheet
        ]
    currency = periods[0].currency if periods else company.currency
    decimals = periods[0].currency_decimals if periods else company.currency_decimals
    return CashSheet(
        site=site,
        date=day,
        periods=periods,
        lines=lines,
        cash_issues=issues,
        day=attendance_day,
        reconciliation=reconciliation,
        currency=currency,
        currency_decimals=decimals,
        status=_sheet_status(periods, lines, today or company_today(company), company),
        absent=absent,
    )


def _ensure_sheet_open_for_changes(site: Site, day: datetime.date) -> None:
    periods = list(PayPeriod.objects.filter(site=site, end_date=day))
    if periods and all(p.is_locked for p in periods):
        raise Locked(_("This cash sheet is closed and locked."), details={"status": "locked"})


def issue_cash(user, site: Site, day: datetime.date, amount: int, *, received_by=None, note=""):
    """Record cash handed to the site (usually the foreman) for a pay day."""
    if not can_manage_site(user, site):
        raise PermissionDenied
    if isinstance(amount, bool) or not isinstance(amount, int) or amount <= 0:
        raise ValidationError({"amount": _("Enter an amount greater than zero.")})
    _ensure_sheet_open_for_changes(site, day)
    with transaction.atomic():
        issue = CashIssue.objects.create(
            site=site, date=day, amount=amount, issued_by=user, received_by=received_by, note=note
        )
        _audit(
            user,
            "payroll.cash.issue",
            issue,
            after={"site_id": site.pk, "date": day.isoformat(), "amount": amount},
        )
    return issue


# --- payments ----------------------------------------------------------------------------


def validate_proof(proof_type: str, proof_file, company: CompanySettings) -> None:
    """The proof must be one of ``accepted_proofs``.

    "none" is allowed only when the setting lists it (or lists nothing). A photo proof
    needs the photo; a signature or thumb mark may come as an image or be on the paper
    sheet. Any file is checked like every upload (type, ≤ 5 MB).
    """
    errors = {}
    accepted = list(company.accepted_proofs or [])
    if proof_type not in ProofType.values:
        errors["proof_type"] = [_("Unknown proof type.")]
    elif proof_type == ProofType.NONE:
        if accepted and ProofType.NONE not in accepted:
            errors["proof_type"] = [_("A proof of receipt is required.")]
    elif accepted and proof_type not in accepted:
        errors["proof_type"] = [_("This proof type is not accepted (see Settings).")]
    elif proof_type == ProofType.PHOTO and not proof_file:
        errors["proof_file"] = [_("Attach the photo of the worker receiving the cash.")]
    if proof_file:
        file_errors = []
        for validator in UPLOAD_VALIDATORS:
            try:
                validator(proof_file)
            except ValidationError as exc:
                file_errors.extend(exc.messages)
        if file_errors:
            errors["proof_file"] = file_errors
    if errors:
        raise ValidationError(errors)


def _payment_snapshot(payment: Payment) -> dict:
    return {
        "pay_line_id": payment.pay_line_id,
        "amount": payment.amount,
        "method": payment.method,
        "proof_type": payment.proof_type,
        "paid_at": payment.paid_at.isoformat(),
        "client_uuid": str(payment.client_uuid),
    }


def record_payment(
    user,
    line: PayLine,
    *,
    amount: int,
    proof_type: str,
    proof_file=None,
    paid_at: datetime.datetime | None = None,
    client_uuid=None,
    method: str | None = None,
) -> Payment:
    """Record that the worker received the line's net, with proof of receipt.

    Raises ``PermissionDenied``, ``Locked`` (period locked), ``Conflict`` (sheet not open
    yet, or the line already paid) or ``ValidationError`` (amount, proof).
    """
    company = CompanySettings.load()
    # Checks and write in one transaction: SQLite's IMMEDIATE mode serialises two phones
    # paying the same line, so the second one sees the first payment.
    with transaction.atomic():
        return _record_payment(
            user,
            line,
            company,
            amount=amount,
            proof_type=proof_type,
            proof_file=proof_file,
            paid_at=paid_at,
            client_uuid=client_uuid,
            method=method,
        )


def _record_payment(
    user, line, company, *, amount, proof_type, proof_file, paid_at, client_uuid, method
):
    period = PayPeriod.objects.select_related("site").get(pk=line.period_id)
    site = period.site
    if not can_pay(user, site):
        raise PermissionDenied
    if period.is_locked:
        raise Locked(_("This pay period is locked."), details={"status": "locked"})
    # Checked live, not from the stored status: a correction can send an approved day
    # back to "submitted" after the sheet opened.
    if not period_is_ready(period, company_today(company), company):
        raise Conflict(
            _("The cash sheet is not open: the roll call is not approved yet."),
            details={"status": SHEET_NOT_OPEN},
        )
    line = PayLine.objects.select_related("worker").get(pk=line.pk)
    remaining = line.net - _paid_amount(line)
    if remaining <= 0:
        raise Conflict(_("This worker has already been paid."), details={"pay_line_id": line.pk})
    if isinstance(amount, bool) or not isinstance(amount, int) or amount != remaining:
        raise ValidationError(
            {
                "amount": [
                    _("The amount must be the net left to pay: %(remaining)s.")
                    % {"remaining": remaining}
                ]
            }
        )
    validate_proof(proof_type, proof_file, company)
    payment = Payment(
        pay_line=line,
        amount=amount,
        method=method or resolve_setting("payment_method", site, line.worker, company=company),
        paid_by=user,
        paid_at=paid_at or timezone.now(),
        proof_type=proof_type,
        proof_file=proof_file or None,
    )
    if client_uuid is not None:
        payment.client_uuid = client_uuid
    payment.save()
    _audit(user, "payroll.payment.create", payment, after=_payment_snapshot(payment))
    update_period_status(period, actor=user, company=company)
    return payment


def undo_payment(user, payment: Payment, reason: str = "") -> None:
    """Remove a payment recorded by mistake (office only, before the period is locked)."""
    period = payment.pay_line.period
    if not can_manage_site(user, period.site):
        raise PermissionDenied
    if period.is_locked:
        raise Locked(_("This pay period is locked."), details={"status": "locked"})
    with transaction.atomic():
        _audit(
            user,
            "payroll.payment.undo",
            payment.pay_line,
            before=_payment_snapshot(payment),
            reason=reason,
        )
        payment.delete()
        update_period_status(period, actor=user)


# --- locking -----------------------------------------------------------------------------


def _unpaid_lines(period: PayPeriod) -> list[PayLine]:
    return [
        line
        for line in period.lines.annotate(paid=Sum("payments__amount")).select_related("worker")
        if (line.paid or 0) < line.net
    ]


def _check_lockable(user, period: PayPeriod, reason: str, today) -> list[PayLine]:
    if not can_lock(user, period.site):
        raise PermissionDenied
    if period.is_locked:
        raise Locked(_("This pay period is already locked."), details={"status": "locked"})
    if not period_is_ready(period, today or company_today()):
        raise Conflict(
            _("Approve every day of the period before locking it."),
            details={"status": period.status},
        )
    unpaid = _unpaid_lines(period)
    if unpaid and not reason.strip():
        raise ValidationError(
            {"reason": [_("Some workers are not paid yet: give a reason to close anyway.")]}
        )
    return unpaid


def _lock_attendance_day(day: AttendanceDay, user) -> None:
    """Approved → locked, through the attendance service (agent C) when it is installed."""
    try:
        from apps.attendance.services import lock_day
    except ImportError:  # Before agent C's services are merged: same transition by hand.
        day.status = AttendanceDay.Status.LOCKED
        day.save(update_fields=["status", "updated_at"])
        _audit(
            user,
            "attendance.day.lock",
            day,
            before={"status": AttendanceDay.Status.APPROVED},
            after={"status": AttendanceDay.Status.LOCKED},
        )
    else:
        lock_day(day, user)


def _lock_checked(user, period: PayPeriod, unpaid: list[PayLine], reason: str) -> PayPeriod:
    for line in unpaid:
        # The worker did not receive this pay, so no advance was repaid from it.
        before = _line_snapshot(line)
        _apply_repayments(line, ())
        line.advance_deduction = 0
        line.net = line.gross - line.other_deductions
        line.save(update_fields=["advance_deduction", "net", "updated_at"])
        _audit(
            user,
            "payroll.line.closed_unpaid",
            line,
            before=before,
            after=_line_snapshot(line),
            reason=reason,
        )
    old = period.status
    period.status = PayPeriod.Status.LOCKED
    period.locked_at = timezone.now()
    period.save(update_fields=["status", "locked_at", "updated_at"])
    _audit(
        user,
        "payroll.period.lock",
        period,
        before={"status": old},
        after={"status": period.status, "unpaid_worker_ids": [line.worker_id for line in unpaid]},
        reason=reason,
    )
    # The attendance becomes read-only too.
    days = AttendanceDay.objects.filter(
        site=period.site,
        date__gte=period.start_date,
        date__lte=period.end_date,
        status=AttendanceDay.Status.APPROVED,
    )
    for day in days:
        _lock_attendance_day(day, user)
    return period


def lock_period(user, period: PayPeriod, reason: str = "", *, today=None) -> PayPeriod:
    """Lock a period: its pay and its attendance become read-only.

    The period must be ready (every day approved). Workers left unpaid need a reason;
    their advance deductions are given back.
    """
    refresh_period(period, actor=user, today=today)
    with transaction.atomic():
        unpaid = _check_lockable(user, period, reason, today)
        return _lock_checked(user, period, unpaid, reason)


def lock_sheet(user, site: Site, day: datetime.date, reason: str = "", *, today=None):
    """Close and lock every period paid on this cash sheet."""
    periods = sync_sheet(site, day, actor=user, today=today)
    if not periods:
        raise Conflict(_("There is nothing to pay on this sheet."), details={})
    with transaction.atomic():
        checked = [(p, _check_lockable(user, p, reason, today)) for p in periods if not p.is_locked]
        if not checked:
            raise Locked(_("This cash sheet is closed and locked."), details={"status": "locked"})
        return [_lock_checked(user, p, unpaid, reason) for p, unpaid in checked]


# --- advances ----------------------------------------------------------------------------


def advance_check(
    worker: Worker,
    amount: int,
    deduction_per_pay: int,
    *,
    on: datetime.date | None = None,
    company: CompanySettings | None = None,
) -> engine.AdvanceCheck:
    """How a new advance compares with the limit (for the form's live explanation)."""
    company = company or CompanySettings.load()
    on = on or company_today(company)
    rate = worker.rate_on(on)
    site = current_site(worker, on)
    expected = 0
    if rate is not None:
        expected = engine.expected_weekly_pay(
            rate.pay_type,
            rate.amount,
            len(company.working_days or []),
            pay_rules(site, company).scheduled_minutes,
        )
    owed = [
        (a.balance, a.deduction_per_pay)
        for a in worker.advances.filter(balance__gt=0).order_by("given_on", "pk")
    ]
    return engine.check_advance(
        amount=max(0, amount),
        deduction_per_pay=max(0, deduction_per_pay),
        owed=owed,
        expected_weekly=expected,
        limit_percent=company.advance_limit_percent,
    )


def can_give_advance(user, worker: Worker, on: datetime.date) -> bool:
    if is_admin(user):
        return True
    site = current_site(worker, on)
    return site is not None and can_manage_site(user, site)


def create_advance(
    user,
    worker: Worker,
    *,
    amount: int,
    deduction_per_pay: int | None = None,
    reason: str = "",
    given_on: datetime.date | None = None,
    proof_type: str = ProofType.NONE,
    proof_file=None,
) -> Advance:
    """Give a worker an advance, within ``advance_limit_percent`` of a week's pay."""
    company = CompanySettings.load()
    given_on = given_on or company_today(company)
    if not can_give_advance(user, worker, given_on):
        raise PermissionDenied
    if deduction_per_pay is None:
        deduction_per_pay = company.default_advance_deduction
    errors = {}
    if isinstance(amount, bool) or not isinstance(amount, int) or amount <= 0:
        errors["amount"] = [_("Enter an amount greater than zero.")]
    if (
        isinstance(deduction_per_pay, bool)
        or not isinstance(deduction_per_pay, int)
        or deduction_per_pay <= 0
    ):
        errors["deduction_per_pay"] = [_("Enter a deduction greater than zero.")]
    elif not errors and deduction_per_pay > amount:
        errors["deduction_per_pay"] = [_("The deduction cannot be more than the advance.")]
    if errors:
        raise ValidationError(errors)
    check = advance_check(worker, amount, deduction_per_pay, on=given_on, company=company)
    if not check.allowed:
        raise ValidationError(
            {
                "amount": [
                    _(
                        "Over the limit: %(worker)s may owe at most %(limit)s "
                        "(%(percent)s%% of a week's pay); this would make %(total)s."
                    )
                    % {
                        "worker": worker.full_name,
                        "limit": check.limit_amount,
                        "percent": check.limit_percent,
                        "total": check.new_total,
                    }
                ]
            }
        )
    validate_proof(proof_type, proof_file, company)
    with transaction.atomic():
        advance = Advance.objects.create(
            worker=worker,
            amount=amount,
            deduction_per_pay=deduction_per_pay,
            balance=amount,
            reason=reason,
            given_on=given_on,
            given_by=user,
            proof_type=proof_type,
            proof_file=proof_file or None,
        )
        _audit(
            user,
            "payroll.advance.create",
            advance,
            after={
                "worker_id": worker.pk,
                "amount": amount,
                "deduction_per_pay": deduction_per_pay,
                "given_on": given_on.isoformat(),
                "new_total_owed": check.new_total,
            },
            reason=reason,
        )
    return advance
