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
* **Pay adjustments** (owner decision M3): when a day is corrected after the worker was
  paid for it, and the period is approved again, the paid line stays as it was paid and
  the difference (corrected worth − paid gross) becomes a ``PayAdjustment`` on the
  worker's next pay at the site. It is settled like an advance: each unpaid line applies
  what it can (``PayAdjustmentApplication``), recomputed with the line. An adjustment
  is open — a further correction updates it — until part of it is on a paid line or a
  locked period; after that a further correction raises a new one for the rest.
* Every write records an ``AuditLog`` entry. Recomputation without a user (a screen or
  the phone reading the cash sheet) is the system's: ``actor=None`` and an action
  prefixed ``payroll.auto.``, and nothing is written when nothing changed.
"""

from __future__ import annotations

import contextlib
import contextvars
import dataclasses
import datetime
import operator
from collections import defaultdict
from dataclasses import dataclass, field
from functools import reduce

from django.core.cache import cache
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.db.models import F, Q, Sum
from django.utils import timezone
from django.utils.translation import gettext as _

from apps.accounts.permissions import (
    LOCK_PERIODS,
    Role,
    can,
    can_manage_site,
    can_view_site,
    company_workers,
    has_role,
    is_admin,
    scoped_sites,
)
from apps.api.exceptions import Conflict, Locked
from apps.attendance.models import AttendanceCorrection, AttendanceDay, AttendanceRecord
from apps.attendance.services import lock_day
from apps.core import changes
from apps.core.choices import PayType, ProofType
from apps.core.dates import company_today
from apps.core.models import AuditLog, CompanySettings
from apps.core.settings import resolve_setting
from apps.core.validators import UPLOAD_VALIDATORS
from apps.workers.models import PayRate, Worker
from apps.worksites.models import Site, SiteAssignment
from apps.worksites.services import crew_ids_by_date

from . import engine
from .models import (
    Advance,
    AdvanceRepayment,
    Bonus,
    CashIssue,
    DeductionRule,
    PayAdjustment,
    PayAdjustmentApplication,
    PayLine,
    Payment,
    PayPeriod,
    PayRuleSettings,
    PieceWorkEntry,
    SitePayRules,
    StoppageDay,
    StoppagePay,
)

PAYABLE_DAY_STATUSES = (AttendanceDay.Status.APPROVED, AttendanceDay.Status.LOCKED)

# The cash sheet as the phone sees it (apps.api.enums.PAYOUT_STATUSES).
SHEET_NOT_OPEN = "not_open"
SHEET_OPEN = "open"
SHEET_PAID = "paid"
SHEET_CLOSED = "closed"


# --- settings ----------------------------------------------------------------------------


def pay_rules(site: Site | None, company: CompanySettings) -> engine.PayRules:
    """The engine's rules for a site, resolved company → site.

    The multipliers and the night window come from the company's ``PayRuleSettings``,
    overridden by the site's ``SitePayRules`` (each field; the night window as a pair).
    """

    def get(name):
        return resolve_setting(name, site, company=company)

    rules = PayRuleSettings.load(company.company_id)
    override = SitePayRules.objects.filter(site_id=site.pk).first() if site is not None else None

    def pick(name):
        value = getattr(override, name, None)
        return value if value is not None else getattr(rules, name)

    night = (rules.night_start, rules.night_end)
    if override is not None and override.night_start and override.night_end:
        night = (override.night_start, override.night_end)
    return engine.PayRules(
        half_day_units=get("half_day_units"),
        late_rule=get("late_rule"),
        work_start=get("work_start"),
        work_end=get("work_end"),
        break_minutes=get("break_minutes"),
        overtime_after_hours=get("overtime_after_hours"),
        overtime_multiplier=get("overtime_multiplier"),
        sunday_multiplier=pick("sunday_multiplier"),
        holiday_multiplier=pick("holiday_multiplier"),
        night_start=night[0] if night[0] and night[1] else None,
        night_end=night[1] if night[0] and night[1] else None,
        night_multiplier=pick("night_multiplier"),
    )


def deduction_inputs(company_id) -> list[engine.DeductionRuleInput]:
    """The company's statutory deduction rules for the engine (it checks their dates)."""
    return [
        engine.DeductionRuleInput(
            rule_id=rule.pk,
            name=rule.name,
            employee_percent=rule.employee_percent,
            employer_percent=rule.employer_percent,
            floor=rule.base_floor,
            ceiling=rule.base_ceiling,
            pay_types=tuple(t for t in rule.pay_types or [] if t in engine.PAY_TYPES),
            active_from=rule.active_from,
            active_to=rule.active_to,
            order=rule.order,
        )
        for rule in DeductionRule.objects.filter(company_id=company_id)
    ]


def stoppage_input(stoppage: StoppageDay) -> engine.Stoppage:
    if stoppage.pay == StoppagePay.FIXED:
        return engine.Stoppage(fixed=stoppage.amount or 0)
    return engine.Stoppage(percent=stoppage.percent if stoppage.percent is not None else 0)


@dataclass(frozen=True)
class DayContext:
    """What a date means for pay at a site: public holidays and stoppage days."""

    holidays: frozenset = frozenset()
    stoppages: dict = field(default_factory=dict)  # date → engine.Stoppage


def day_context(
    site: Site, start: datetime.date, end: datetime.date, company: CompanySettings
) -> DayContext:
    stoppages = StoppageDay.objects.filter(site=site, date__gte=start, date__lte=end)
    return DayContext(
        holidays=frozenset(company.holiday_dates()),
        stoppages={s.date: stoppage_input(s) for s in stoppages},
    )


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


def _is_user(actor) -> bool:
    return bool(getattr(actor, "is_authenticated", False))


def _audit(actor, action, target=None, *, before=None, after=None, reason=""):
    return AuditLog.record(
        actor=actor if _is_user(actor) else None,
        action=action,
        target=target,
        before=before,
        after=after,
        reason=reason,
    )


def _pay_action(actor, name: str) -> str:
    """``payroll.<name>`` for a user's write, ``payroll.auto.<name>`` for the system's
    recomputation (refresh on read: no user to blame)."""
    return f"payroll.{name}" if _is_user(actor) else f"payroll.auto.{name}"


# --- permissions -------------------------------------------------------------------------


def can_pay(user, site) -> bool:
    """Hand out pay on a site: its foremen, its manager, admins."""
    if can_manage_site(user, site):
        return True
    return has_role(user, Role.FOREMAN) and can_view_site(user, site)


def can_lock(user, site) -> bool:
    """Close and lock pay: the ``LOCK_PERIODS`` permission (admins and accountants
    everywhere, site managers on their own sites)."""
    return can(user, LOCK_PERIODS, site)


def scoped_workers(user):
    """Workers on the sites this user may see (all workers for admins and accountants)."""
    workers = company_workers(user)
    if is_admin(user) or has_role(user, Role.ACCOUNTANT):
        return workers
    return workers.filter(assignments__site__in=scoped_sites(user)).distinct()


def current_site(worker: Worker, on: datetime.date) -> Site | None:
    assignment = (
        SiteAssignment.objects.active_on(on)
        .filter(worker=worker)
        .select_related("site")
        .order_by("-start_date", "-pk")
        .first()
    )
    return assignment.site if assignment else None


def current_sites(worker_ids, on: datetime.date) -> dict[int, Site]:
    """``current_site(worker, on)`` for many workers, in one query (workers on no site
    are absent)."""
    sites: dict[int, Site] = {}
    for assignment in (
        SiteAssignment.objects.active_on(on)
        .filter(worker_id__in=worker_ids)
        .select_related("site")
        .order_by("-start_date", "-pk")
    ):
        sites.setdefault(assignment.worker_id, assignment.site)
    return sites


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


def _day_input(
    record: AttendanceRecord, rate: PayRate | None, context: DayContext | None = None
) -> engine.DayInput:
    pay_type = rate.pay_type if rate else PayType.DAILY
    if pay_type not in engine.PAY_TYPES:  # pragma: no cover (rates are validated)
        pay_type = engine.DAILY
    date = record.day.date
    context = context or DayContext()
    return engine.DayInput(
        date=date,
        status=record.status,
        pay_type=pay_type,
        rate=rate.amount if rate else 0,
        arrival=record.arrival,
        departure=record.departure,
        holiday=date in context.holidays,
        stoppage=context.stoppages.get(date),
    )


def _line_snapshot(line: PayLine) -> dict:
    snapshot = {
        "worker_id": line.worker_id,
        "units": str(line.units),
        "hours": None if line.hours is None else str(line.hours),
        "overtime_hours": str(line.overtime_hours),
        "gross": line.gross,
        "advance_deduction": line.advance_deduction,
        "other_deductions": line.other_deductions,
        "adjustment": line.adjustment,
        "net": line.net,
    }
    # Pay rules (Wave 4): only when they are on the line, so older entries read the same.
    for name in ("piece_amount", "bonus_amount", "statutory_deductions"):
        if getattr(line, name):
            snapshot[name] = getattr(line, name)
    return snapshot


def _period_totals(period: PayPeriod) -> dict:
    totals = period.lines.aggregate(
        gross=Sum("gross"),
        statutory=Sum("statutory_deductions"),
        advance=Sum("advance_deduction"),
        adjustment=Sum("adjustment"),
        net=Sum("net"),
    )
    result = {
        "status": period.status,
        "lines": period.lines.count(),
        "gross": totals["gross"] or 0,
        "advance_deduction": totals["advance"] or 0,
        "adjustment": totals["adjustment"] or 0,
        "net": totals["net"] or 0,
    }
    if totals["statutory"]:
        result["statutory_deductions"] = totals["statutory"]
    return result


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


def _open_advances(
    worker_ids, before: datetime.date, lines: dict[int, PayLine]
) -> dict[int, list[Advance]]:
    """Per worker, the advances their pay may deduct from: given before the pay day, with
    a balance left or repaid by the worker's line (``lines``, with their
    ``advance_repayments`` prefetched), oldest first. One query for every worker."""
    repaid = {
        worker_id: {r.advance_id for r in line.advance_repayments.all()}
        for worker_id, line in lines.items()
    }
    query = Q(balance__gt=0) | Q(pk__in=set().union(*repaid.values()))
    by_worker: dict[int, list[Advance]] = defaultdict(list)
    for advance in (
        Advance.objects.filter(worker_id__in=worker_ids, given_on__lt=before)
        .filter(query)
        .order_by("given_on", "pk")
    ):
        if advance.balance > 0 or advance.pk in repaid.get(advance.worker_id, ()):
            by_worker[advance.worker_id].append(advance)
    return by_worker


def _paid_amount(line: PayLine) -> int:
    return line.payments.aggregate(total=Sum("amount"))["total"] or 0


# --- piece work and bonuses ----------------------------------------------------------------


def _pieces_by_worker(
    period: PayPeriod, company: CompanySettings, worker_ids
) -> dict[int, list[engine.PieceInput]]:
    """The piece work this period pays, per worker: entries on days whose roll call is
    approved (or locked) and that fall in the worker's period."""
    site = period.site
    payable = AttendanceDay.objects.filter(
        site=site,
        date__gte=period.start_date,
        date__lte=period.end_date,
        status__in=PAYABLE_DAY_STATUSES,
    ).values_list("date", flat=True)
    entries = (
        PieceWorkEntry.objects.filter(site=site, date__in=list(payable), worker_id__in=worker_ids)
        .select_related("task", "worker")
        .order_by("date", "pk")
    )
    by_worker: dict[int, list[engine.PieceInput]] = defaultdict(list)
    for entry in entries:
        start, end, _frequency = period_bounds_for(site, entry.worker, entry.date, company)
        if (start, end) != (period.start_date, period.end_date):
            continue
        by_worker[entry.worker_id].append(
            engine.PieceInput(
                entry_id=entry.pk,
                date=entry.date,
                quantity=entry.quantity,
                unit_rate=entry.unit_rate,
                task=entry.task.name,
                unit=entry.task.unit,
            )
        )
    return by_worker


def _open_bonuses(
    worker_ids, period: PayPeriod, lines: dict[int, PayLine]
) -> dict[int, list[Bonus]]:
    """Per worker, the bonuses their pay may add: dated on or before its pay day, at its
    site, not yet on another pay (or already on the worker's line in ``lines``), oldest
    first. One query for every worker."""
    line_ids = {worker_id: line.pk for worker_id, line in lines.items()}
    by_worker: dict[int, list[Bonus]] = defaultdict(list)
    for bonus in (
        Bonus.objects.filter(
            worker_id__in=worker_ids, site_id=period.site_id, date__lte=period.end_date
        )
        .filter(Q(applied_to__isnull=True) | Q(applied_to__in=line_ids.values()))
        .order_by("date", "pk")
    ):
        if bonus.applied_to_id is None or bonus.applied_to_id == line_ids.get(bonus.worker_id):
            by_worker[bonus.worker_id].append(bonus)
    return by_worker


def _apply_bonuses(line: PayLine, wanted: tuple[tuple[int, int], ...], actor) -> None:
    """Make the bonuses on this line exactly ``wanted`` (``(bonus_id, amount)``)."""
    current = {bonus.pk for bonus in line.bonuses.all()}
    wanted_ids = {bonus_id for bonus_id, _amount in wanted}
    if current == wanted_ids:
        return
    now = timezone.now()
    Bonus.objects.filter(pk__in=current - wanted_ids).update(applied_to=None, updated_at=now)
    Bonus.objects.filter(pk__in=wanted_ids - current).update(applied_to=line, updated_at=now)
    _audit(
        actor,
        _pay_action(actor, "bonus.apply"),
        line,
        before={"bonus_ids": sorted(current)},
        after={"bonus_ids": sorted(wanted_ids), "bonus_amount": line.bonus_amount},
    )


# --- pay adjustments (a day corrected after it was paid) ----------------------------------


def _adjustment_snapshot(adjustment: PayAdjustment) -> dict:
    return {
        "worker_id": adjustment.worker_id,
        "site_id": adjustment.site_id,
        "amount": adjustment.amount,
        "currency": adjustment.currency,
        "dates": adjustment.dates,
        "source_line_id": adjustment.source_line_id,
        "record_id": adjustment.record_id,
        "applied_amount": adjustment.applied_amount,
        "applied_to_id": adjustment.applied_to_id,
    }


def _open_adjustments(
    worker_ids, period: PayPeriod, lines: dict[int, PayLine]
) -> dict[int, list[PayAdjustment]]:
    """Per worker, the adjustments their pay may settle: raised by an earlier pay of the
    worker at the site and not settled yet (or settled by the worker's line in ``lines``,
    with their ``adjustment_applications`` prefetched), oldest first. One query for every
    worker."""
    applied = {
        worker_id: {a.adjustment_id for a in line.adjustment_applications.all()}
        for worker_id, line in lines.items()
    }
    query = ~Q(applied_amount=F("amount")) | Q(pk__in=set().union(*applied.values()))
    by_worker: dict[int, list[PayAdjustment]] = defaultdict(list)
    for adjustment in (
        PayAdjustment.objects.filter(
            worker_id__in=worker_ids,
            site_id=period.site_id,
            source_line__period__end_date__lt=period.end_date,
        )
        .filter(query)
        .order_by("created_at", "pk")
    ):
        if adjustment.remaining or adjustment.pk in applied.get(adjustment.worker_id, ()):
            by_worker[adjustment.worker_id].append(adjustment)
    return by_worker


def _sync_applied(adjustment: PayAdjustment, actor, line: PayLine) -> None:
    """Recompute ``applied_amount``/``applied_to`` from the applications; audit a change."""
    applications = list(
        adjustment.applications.select_related("pay_line__period").order_by(
            "pay_line__period__end_date", "pk"
        )
    )
    before = {
        "applied_amount": adjustment.applied_amount,
        "applied_to_id": adjustment.applied_to_id,
    }
    adjustment.applied_amount = sum(a.amount for a in applications)
    adjustment.applied_to = applications[-1].pay_line if applications else None
    after = {"applied_amount": adjustment.applied_amount, "applied_to_id": adjustment.applied_to_id}
    if after == before:
        return
    adjustment.save(update_fields=["applied_amount", "applied_to", "updated_at"])
    _audit(
        actor,
        _pay_action(actor, "adjustment.apply"),
        adjustment,
        before=before,
        after={**after, "pay_line_id": line.pk, "remaining": adjustment.remaining},
    )


def _apply_adjustments(line: PayLine, wanted: tuple[tuple[int, int], ...], actor) -> None:
    """Make the line's adjustment applications equal ``wanted`` (``(id, signed amount)``)."""
    current = {a.adjustment_id: a for a in line.adjustment_applications.all()}
    wanted_by_adjustment = dict(wanted)
    if {k: a.amount for k, a in current.items()} == wanted_by_adjustment:
        return
    for application in current.values():
        application.delete()
    for adjustment_id, amount in wanted_by_adjustment.items():
        PayAdjustmentApplication.objects.create(
            adjustment_id=adjustment_id, pay_line=line, amount=amount
        )
    touched = set(current) | set(wanted_by_adjustment)
    for adjustment in PayAdjustment.objects.filter(pk__in=touched).order_by("pk"):
        _sync_applied(adjustment, actor, line)


def _release(adjustment: PayAdjustment) -> None:
    """Take an open adjustment off the (unpaid) lines it was applied to.

    The lines are recomputed by the caller (``_refresh_later``)."""
    adjustment.applications.all().delete()
    adjustment.applied_amount = 0
    adjustment.applied_to = None


def _settled_adjustment_ids(adjustments) -> set[int]:
    """Adjustments partly or fully on a paid line or a locked period: their amount is final."""
    return set(
        PayAdjustmentApplication.objects.filter(adjustment__in=adjustments)
        .filter(
            Q(pay_line__payments__isnull=False)
            | Q(pay_line__period__status=PayPeriod.Status.LOCKED)
        )
        .values_list("adjustment_id", flat=True)
    )


def _drop_line(line: PayLine, actor) -> None:
    """Delete an unpaid line that has nothing left to pay, giving back what it took."""
    _apply_repayments(line, ())
    _apply_adjustments(line, (), actor)
    line.delete()


def _records_by_worker(
    period: PayPeriod, company: CompanySettings, worker_ids=None
) -> dict[int, list[AttendanceRecord]]:
    """The attendance records paid by this period, per worker (in date order)."""
    site = period.site
    records = (
        AttendanceRecord.objects.filter(
            day__site=site, day__date__gte=period.start_date, day__date__lte=period.end_date
        )
        .select_related("day", "worker")
        .order_by("day__date", "pk")
    )
    if worker_ids is not None:
        records = records.filter(worker_id__in=worker_ids)
    by_worker: dict[int, list[AttendanceRecord]] = defaultdict(list)
    for record in records:
        start, end, _frequency = period_bounds_for(site, record.worker, record.day.date, company)
        if (start, end) == (period.start_date, period.end_date):
            by_worker[record.worker_id].append(record)
    return by_worker


def _late_corrections(period: PayPeriod, lines: list[PayLine]) -> dict[int, list]:
    """Corrections made after each paid line's first payment, per worker (oldest first)."""
    first_paid = {
        line.worker_id: min(p.created_at for p in line.payments.all())
        for line in lines
        if line.payments.all()
    }
    if not first_paid:
        return {}
    corrections = (
        AttendanceCorrection.objects.filter(
            record__day__site_id=period.site_id,
            record__day__date__gte=period.start_date,
            record__day__date__lte=period.end_date,
            record__worker_id__in=list(first_paid),
        )
        .select_related("record__day")
        .order_by("created_at", "pk")
    )
    late: dict[int, list] = defaultdict(list)
    for correction in corrections:
        worker_id = correction.record.worker_id
        if correction.created_at > first_paid[worker_id]:
            late[worker_id].append(correction)
    return late


def _paid_difference(line: PayLine, days: list[engine.DayInput], rules: engine.PayRules) -> int:
    """What the attendance is worth now minus the time pay that was paid.

    Days that were paid keep the rate they were paid at, so only the attendance counts.
    Piece work and bonuses are not attendance: they stay as they were paid.
    """
    paid_rates = {d["date"]: (d["pay_type"], d["rate"]) for d in line.days or []}
    priced = []
    for day in days:
        paid = paid_rates.get(day.date.isoformat())
        if paid is not None:
            day = dataclasses.replace(day, pay_type=paid[0], rate=paid[1])
        priced.append(day)
    paid_time_pay = line.gross - line.piece_amount - line.bonus_amount
    return engine.compute_line(priced, rules).time_pay - paid_time_pay


@dataclass
class PaidCorrection:
    """A paid line whose corrected attendance is worth a different amount."""

    line: PayLine
    worth: int  # what the corrected attendance is worth (gross)
    difference: int  # still to settle: worth − paid − adjustments already settled
    corrections: list = field(default_factory=list)


def _paid_corrections(
    period: PayPeriod,
    paid: list[tuple[PayLine, list[engine.DayInput]]],
    rules: engine.PayRules,
) -> list[PaidCorrection]:
    late = _late_corrections(period, [line for line, _days in paid])
    found = []
    for line, days in paid:
        corrections = late.get(line.worker_id)
        if not corrections:
            continue
        target = _paid_difference(line, days, rules)
        raised = list(line.raised_adjustments.all())
        settled = _settled_adjustment_ids(raised) if raised else set()
        difference = target - sum(a.amount for a in raised if a.pk in settled)
        found.append(PaidCorrection(line, line.gross + target, difference, corrections))
    return found


def _sync_raised(found: PaidCorrection, actor) -> bool:
    """Make the line's open adjustment hold ``found.difference``; True if it changed."""
    line = found.line
    period = line.period
    raised = sorted(line.raised_adjustments.all(), key=lambda a: a.pk)
    settled = _settled_adjustment_ids(raised) if raised else set()
    pending = [a for a in raised if a.pk not in settled]
    for extra in pending[1:]:  # Never more than one; merge defensively.
        _cancel_adjustment(extra, actor)
    current = pending[0] if pending else None
    # The corrections this adjustment is about: those after the last settled one.
    corrections = found.corrections
    if len(pending) < len(raised):
        since = max(a.created_at for a in raised if a.pk in settled)
        corrections = [c for c in corrections if c.created_at > since] or corrections
    latest = corrections[-1]
    values = {
        "amount": found.difference,
        "reason": "; ".join(dict.fromkeys(c.reason for c in corrections if c.reason)),
        "dates": sorted({c.record.day.date.isoformat() for c in corrections}),
        "day_id": latest.record.day_id,
        "record_id": latest.record_id,
    }
    if current is None:
        if not found.difference:
            return False
        adjustment = PayAdjustment.objects.create(
            worker_id=line.worker_id,
            site_id=period.site_id,
            currency=period.currency,
            currency_decimals=period.currency_decimals,
            source_line=line,
            created_by=actor if _is_user(actor) else None,
            **values,
        )
        _audit(
            actor,
            _pay_action(actor, "adjustment.create"),
            adjustment,
            after={**_adjustment_snapshot(adjustment), "worth": found.worth, "paid": line.gross},
            reason=values["reason"],
        )
        return True
    if not found.difference:
        _cancel_adjustment(current, actor)
        return True
    if all(getattr(current, k) == v for k, v in values.items()):
        return False
    before = _adjustment_snapshot(current)
    if current.amount != found.difference:
        _release(current)
    for name, value in values.items():
        setattr(current, name, value)
    current.save()
    _audit(
        actor,
        _pay_action(actor, "adjustment.update"),
        current,
        before=before,
        after={**_adjustment_snapshot(current), "worth": found.worth, "paid": line.gross},
        reason=current.reason,
    )
    return True


def _cancel_adjustment(adjustment: PayAdjustment, actor) -> None:
    """Delete an open adjustment (the correction was undone, or the payment)."""
    before = _adjustment_snapshot(adjustment)
    _release(adjustment)
    _audit(actor, _pay_action(actor, "adjustment.cancel"), adjustment, before=before)
    adjustment.delete()


def _refresh_later(period: PayPeriod, worker_ids, **kwargs) -> None:
    """Recompute the workers' later unpaid pay at the site, after an adjustment changed."""
    later = (
        PayPeriod.objects.filter(
            site_id=period.site_id,
            end_date__gt=period.end_date,
            lines__worker_id__in=list(worker_ids),
            lines__payments__isnull=True,
        )
        .exclude(status=PayPeriod.Status.LOCKED)
        .select_related("site")
        .distinct()
        .order_by("end_date", "start_date")
    )
    for later_period in later:
        refresh_period(later_period, **kwargs)


# --- refreshing pay -----------------------------------------------------------------------


def refresh_period(
    period: PayPeriod,
    *,
    actor=None,
    today: datetime.date | None = None,
    company: CompanySettings | None = None,
) -> PayPeriod:
    """Recompute the period's unpaid lines from its attendance, then its status.

    Locked periods are left alone. Lines with a payment are frozen; once every day of the
    period is approved again, a correction made after the payment becomes a
    ``PayAdjustment`` on the worker's next pay (and those later pays are recomputed).
    Writes AuditLog entries only when something changed, so refreshing twice is a no-op.
    Without an ``actor`` (refresh on read) the entries are the system's (``payroll.auto.``).

    A refresh on read usually changes nothing, so it first runs without a transaction,
    refusing every write (``_reads_only``): an IMMEDIATE transaction takes SQLite's write
    lock when it begins, and screens opening pay must not make the phones' writes wait.
    Only when it reaches a write does it start again, in a transaction.
    """
    if period.is_locked:
        return period
    company = company or CompanySettings.load()
    today = today or company_today(company)
    connection = transaction.get_connection()
    if actor is None and not connection.in_atomic_block:
        try:
            with connection.execute_wrapper(_reads_only):
                return _refresh_period(period, actor, today, company, atomic=False)
        except _WouldWrite:
            period.refresh_from_db()  # The dry run may have changed it in memory.
    return _refresh_period(period, actor, today, company, atomic=True)


class _WouldWrite(Exception):
    """A refresh running under ``_reads_only`` reached a write."""


def _reads_only(execute, sql, params, many, context):
    """A ``connection.execute_wrapper`` that lets reads through and refuses anything else
    (writes, and BEGIN or SAVEPOINT) before it reaches the database."""
    if not sql.lstrip()[:6].upper() == "SELECT":
        raise _WouldWrite
    return execute(sql, params, many, context)


def _refresh_period(
    period: PayPeriod, actor, today: datetime.date, company: CompanySettings, *, atomic: bool
) -> PayPeriod:
    site = period.site
    rules = pay_rules(site, company)
    by_worker = _records_by_worker(period, company)
    rates = _rates_by_worker(list(by_worker), period.end_date)
    context = day_context(site, period.start_date, period.end_date, company)
    pieces = _pieces_by_worker(period, company, list(by_worker))
    deduction_rules = deduction_inputs(site.company_id)
    adjusted: set[int] = set()

    with transaction.atomic() if atomic else contextlib.nullcontext():
        before = _period_totals(period)
        changed: list[int] = []
        lines = {
            line.worker_id: line
            for line in period.lines.select_related("period").prefetch_related(
                "payments",
                "advance_repayments",
                "adjustment_applications",
                "raised_adjustments",
                "bonuses",
            )
        }
        for worker_id, line in lines.items():
            if worker_id not in by_worker and not line.payments.all():
                _drop_line(line, actor)
                changed.append(worker_id)

        # What each unpaid line may take, read for every worker at once. A worker's line
        # only ever changes the worker's own advances, adjustments and bonuses.
        kept = {worker_id: line for worker_id, line in lines.items() if worker_id in by_worker}
        open_advances = _open_advances(list(by_worker), period.end_date, kept)
        open_adjustments = _open_adjustments(list(by_worker), period, kept)
        open_bonuses = _open_bonuses(list(by_worker), period, kept)

        paid: list[tuple[PayLine, list[engine.DayInput]]] = []
        for worker_id, worker_records in by_worker.items():
            line = lines.get(worker_id)
            days = [
                _day_input(r, _rate_on(rates[worker_id], r.day.date), context)
                for r in worker_records
            ]
            if line is not None and line.payments.all():
                paid.append((line, days))  # Paid: frozen (a correction is adjusted below).
                continue
            repaid_by_line = (
                {r.advance_id: r.amount for r in line.advance_repayments.all()} if line else {}
            )
            advances = [
                engine.AdvanceInput(
                    a.pk, a.balance + repaid_by_line.get(a.pk, 0), a.deduction_per_pay
                )
                for a in open_advances[worker_id]
            ]
            applied_by_line = (
                {a.adjustment_id: a.amount for a in line.adjustment_applications.all()}
                if line
                else {}
            )
            adjustments = [
                engine.AdjustmentInput(a.pk, a.remaining + applied_by_line.get(a.pk, 0))
                for a in open_adjustments[worker_id]
            ]
            other = line.other_deductions if line else 0
            bonuses = [engine.BonusInput(b.pk, b.amount) for b in open_bonuses[worker_id]]
            result = engine.compute_line(
                days,
                rules,
                advances,
                other_deductions=other,
                adjustments=adjustments,
                pieces=pieces.get(worker_id, ()),
                bonuses=bonuses,
                deduction_rules=deduction_rules,
                pay_day=period.end_date,
            )
            # A bonus alone does not make a pay: it waits for a day worked.
            worked = result.piece_amount > 0 or any(
                d.units > 0 or d.amount > 0 for d in result.days
            )
            topped_up = any(amount > 0 for _id, amount in result.adjustments)
            if not (worked or topped_up):
                if line is not None:
                    _drop_line(line, actor)
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
                "adjustment": result.adjustment,
                "net": result.net,
                "days": [d.as_dict() for d in result.days],
                "piece_amount": result.piece_amount,
                "bonus_amount": result.bonus_amount,
                "statutory_deductions": result.statutory,
                "employer_contributions": result.employer,
                "pieces": [p.as_dict() for p in result.pieces],
                "deductions": [d.as_dict() for d in result.deductions],
            }
            dirty = line.pk is None or any(getattr(line, k) != v for k, v in values.items())
            if dirty:
                for name, value in values.items():
                    setattr(line, name, value)
                line.save()
                changed.append(worker_id)
            _apply_repayments(line, result.repayments)
            _apply_adjustments(line, result.adjustments, actor)
            _apply_bonuses(line, result.bonuses, actor)

        # A paid line stays as paid; the difference goes on the next pay, once the
        # corrected attendance is approved (the period is payable again).
        if paid and period_is_ready(period, today, company):
            for found in _paid_corrections(period, paid, rules):
                if _sync_raised(found, actor):
                    adjusted.add(found.line.worker_id)

        if changed:
            _audit(
                actor,
                _pay_action(actor, "period.refresh"),
                period,
                before=before,
                after={**_period_totals(period), "workers_changed": sorted(changed)},
            )
        update_period_status(period, actor=actor, today=today, company=company)
    if adjusted:
        _refresh_later(period, adjusted, actor=actor, today=today, company=company)
    return period


def paid_corrections(day: AttendanceDay) -> dict[int, PaidCorrection]:
    """Workers already paid for ``day`` whose corrected attendance is worth a different
    amount, with the difference still to settle (read only: for the Approvals screen)."""
    company = CompanySettings.load()
    rules = pay_rules(day.site, company)
    periods = (
        PayPeriod.objects.filter(
            site=day.site,
            start_date__lte=day.date,
            end_date__gte=day.date,
            lines__payments__isnull=False,
        )
        .exclude(status=PayPeriod.Status.LOCKED)
        .select_related("site")
        .distinct()
    )
    found: dict[int, PaidCorrection] = {}
    for period in periods:
        lines = list(
            period.lines.filter(payments__isnull=False)
            .select_related("period")
            .prefetch_related("payments", "raised_adjustments")
            .distinct()
        )
        by_worker = _records_by_worker(period, company, [line.worker_id for line in lines])
        rates = _rates_by_worker(list(by_worker), period.end_date)
        context = day_context(period.site, period.start_date, period.end_date, company)
        paid = [
            (
                line,
                [
                    _day_input(r, _rate_on(rates[line.worker_id], r.day.date), context)
                    for r in by_worker.get(line.worker_id, [])
                ],
            )
            for line in lines
            if line.worker_id in by_worker
        ]
        for item in _paid_corrections(period, paid, rules):
            if item.difference:
                found[item.line.worker_id] = item
    return found


def _days_with_expected_crew(period: PayPeriod, company: CompanySettings) -> set[datetime.date]:
    """Working days of the period on which active workers are assigned to the site.

    The crew rule is ``apps.worksites.services.crew_ids_by_date`` (dates inclusive).
    """
    working_days = set(company.working_days or [])
    crews = crew_ids_by_date(period.site, period.start_date, period.end_date)
    return {day for day, crew in crews.items() if crew and day.isoweekday() in working_days}


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
            actor,
            _pay_action(actor, "period.status"),
            period,
            before={"status": old},
            after={"status": status},
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

    existing = (
        PayPeriod.objects.filter(site=site)
        .exclude(status=PayPeriod.Status.LOCKED)
        .select_related("site")
    )
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
    company = kwargs["company"] = kwargs.get("company") or CompanySettings.load()
    lookback = max(31, resolve_setting("pay_every_n_days", site, company=company) or 0)
    return sync_site(site, day - datetime.timedelta(days=lookback), day, ending_on=day, **kwargs)


# --- refreshing pay on read ---------------------------------------------------------------
#
# The screens that list every site's pay (Pay periods, Cash sheets) recompute it on read, a
# safety net behind the services that recompute it on write. Most of the time nothing has
# changed, and the recomputation costs dozens of queries per site, so it is skipped while
# the company's change marker (apps.core.changes, moved by every AuditLog entry) and the
# day are those of the site's last recomputation. A recomputation that changed something
# wrote audit entries of its own (payroll.auto.…), which move the marker: the next read
# recomputes once more, finds nothing to change, and the reads after it skip. Writes that
# bypass the services and the audit log (Django admin, a shell) show after
# READ_REFRESH_MAX_AGE seconds at the latest.

READ_REFRESH_MAX_AGE = 300


def _refresh_on_read(kind: str, site: Site, today: datetime.date, refresh) -> bool:
    """Run ``refresh()`` unless nothing changed since the last ``kind`` refresh of ``site``.

    True if it ran. The marker is read before the refresh: a change made during the
    refresh moves it, and the next read refreshes again.
    """
    key = f"payroll:read-refresh:{kind}:{site.pk}"
    marker = f"{changes.version(site.company_id)}|{today.isoformat()}"
    if cache.get(key) == marker:
        return False
    refresh()
    cache.set(key, marker, READ_REFRESH_MAX_AGE)
    return True


def sync_recent_on_read(
    sites, today: datetime.date, days: int, *, company: CompanySettings | None = None
) -> None:
    """Bring the last ``days`` days of pay of the active ``sites`` up to date (Pay periods)."""
    company = company or CompanySettings.load()
    start = today - datetime.timedelta(days=days - 1)
    for site in sites.filter(status=Site.Status.ACTIVE):
        _refresh_on_read(
            f"recent{days}",
            site,
            today,
            lambda site=site: sync_site(site, start, today, today=today, company=company),
        )


def sync_sheets_on_read(
    sites, today: datetime.date, *, company: CompanySettings | None = None
) -> None:
    """Bring today's cash sheet of the active ``sites`` up to date (Cash sheets)."""
    company = company or CompanySettings.load()
    for site in sites.filter(status=Site.Status.ACTIVE):
        _refresh_on_read(
            "sheet",
            site,
            today,
            lambda site=site: sync_sheet(site, today, today=today, company=company),
        )


def on_attendance_changed(day: AttendanceDay, *, actor=None) -> None:
    """Hook for the attendance services: refresh pay after a day is approved or corrected."""
    sync_site(day.site, day.date, day.date, actor=actor)


# --- cash sheets -------------------------------------------------------------------------


@dataclass
class SheetLine:
    line: PayLine
    payment: Payment | None
    paid: int
    # Adjustments settled on this pay: (adjustment, signed amount applied here).
    adjustments: list[tuple[PayAdjustment, int]] = field(default_factory=list)
    # Adjustments this (paid) line raised: its day was corrected after the payment.
    raised: list[PayAdjustment] = field(default_factory=list)

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
    periods: list[PayPeriod], amounts: list[tuple[int, int]], today, company: CompanySettings
) -> str:
    """The sheet's status from its periods and ``(net, paid)`` per pay line."""
    if not periods:
        return SHEET_NOT_OPEN
    if all(p.is_locked for p in periods):
        return SHEET_CLOSED
    if not all(period_is_ready(p, today, company) for p in periods if not p.is_locked):
        return SHEET_NOT_OPEN
    if all(paid >= net for net, paid in amounts):  # SheetLine.is_paid
        return SHEET_PAID
    return SHEET_OPEN


def sheet_status(site: Site, day: datetime.date) -> str:
    """The phone's ``payout_status`` for a site and pay day (read only, no refresh)."""
    company = CompanySettings.load()
    periods = list(PayPeriod.objects.filter(site=site, end_date=day).select_related("site"))
    amounts = _line_amounts(periods)
    lines = [amount for period in periods for amount in amounts[period.pk]]
    return _sheet_status(periods, lines, company_today(company), company)


def _on_sheets(keys, site_field: str, date_field: str) -> Q:
    """Rows of any of the sheets ``keys`` ((site id, pay day) pairs)."""
    return reduce(
        operator.or_,
        (Q(**{site_field: site_id, date_field: day}) for site_id, day in set(keys)),
    )


def _sheet_periods(keys) -> dict[tuple, list[PayPeriod]]:
    """The periods of each sheet (all frequencies paid that day), oldest first."""
    periods: dict[tuple, list[PayPeriod]] = defaultdict(list)
    sheet_periods = (
        PayPeriod.objects.filter(_on_sheets(keys, "site_id", "end_date"))
        .select_related("site")  # period_is_ready reads it for every unlocked period
        .order_by("start_date")
    )
    for period in sheet_periods:
        periods[(period.site_id, period.end_date)].append(period)
    return periods


def _line_amounts(periods) -> dict[int, list[tuple[int, int]]]:
    """``(net, paid)`` of every pay line of ``periods``, per period id (one query)."""
    amounts: dict[int, list[tuple[int, int]]] = defaultdict(list)
    rows = (
        PayLine.objects.filter(period__in=list(periods))
        .annotate(paid=Sum("payments__amount"))
        .values_list("period_id", "net", "paid")
    )
    for period_id, net, paid in rows:
        amounts[period_id].append((net, paid or 0))
    return amounts


def cash_sheet(
    site: Site,
    day: datetime.date,
    *,
    refresh: bool = True,
    today: datetime.date | None = None,
) -> CashSheet:
    """The cash sheet of a site for a pay day, with the reconciliation.

    Reading it brings the pay up to date first (``refresh``); that recomputation is the
    system's (no viewer in the audit log, nothing written when nothing changed).
    """
    company = CompanySettings.load()
    if refresh:
        sync_sheet(site, day, today=today, company=company)
    return cash_sheets([(site, day)], today=today, company=company)[0]


def cash_sheets(
    sheets: list[tuple[Site, datetime.date]],
    *,
    today: datetime.date | None = None,
    company: CompanySettings | None = None,
) -> list[CashSheet]:
    """``cash_sheet(site, day, refresh=False)`` for each ``(site, day)``, in order.

    The periods, pay lines, cash issues, days and absences of every sheet are read
    together (a list screen shows a page of sheets); only the readiness of each unlocked
    period is still checked on its own.
    """
    if not sheets:
        return []
    company = company or CompanySettings.load()
    today = today or company_today(company)
    keys = [(site.pk, day) for site, day in sheets]

    def on_sheets(site_field: str, date_field: str) -> Q:
        return _on_sheets(keys, site_field, date_field)

    periods = _sheet_periods(keys)
    pay_lines = (
        PayLine.objects.filter(period__in=[p for group in periods.values() for p in group])
        .select_related("worker__trade", "period")
        .prefetch_related(
            "payments__paid_by",
            "adjustment_applications__adjustment__source_line__period",
            "raised_adjustments",
        )
        .order_by("worker__last_name", "worker__first_name", "worker__number")
    )
    lines: dict[tuple, list[SheetLine]] = defaultdict(list)
    for line in pay_lines:
        payments = list(line.payments.all())
        lines[(line.period.site_id, line.period.end_date)].append(
            SheetLine(
                line=line,
                payment=payments[0] if payments else None,
                paid=sum(p.amount for p in payments),
                adjustments=[(a.adjustment, a.amount) for a in line.adjustment_applications.all()],
                raised=list(line.raised_adjustments.all()),
            )
        )
    issues: dict[tuple, list[CashIssue]] = defaultdict(list)
    for issue in CashIssue.objects.filter(on_sheets("site_id", "date")).select_related(
        "received_by"
    ):
        issues[(issue.site_id, issue.date)].append(issue)
    days = {
        (day.site_id, day.date): day
        for day in AttendanceDay.objects.filter(on_sheets("site_id", "date"))
    }
    absent: dict[int, list[AttendanceRecord]] = defaultdict(list)
    for record in AttendanceRecord.objects.filter(
        day__in=list(days.values()), status=AttendanceRecord.Status.ABSENT
    ).select_related("worker"):
        absent[record.day_id].append(record)

    result = []
    for (site, day), key in zip(sheets, keys, strict=True):
        sheet_periods, sheet_lines = periods.get(key, []), lines.get(key, [])
        sheet_issues = issues.get(key, [])
        attendance_day = days.get(key)
        on_sheet = {sl.line.worker_id for sl in sheet_lines}
        first = sheet_periods[0] if sheet_periods else None
        result.append(
            CashSheet(
                site=site,
                date=day,
                periods=sheet_periods,
                lines=sheet_lines,
                cash_issues=sheet_issues,
                day=attendance_day,
                reconciliation=engine.reconcile(
                    sum(issue.amount for issue in sheet_issues),
                    [(sl.line.net, sl.paid) for sl in sheet_lines],
                ),
                currency=first.currency if first else company.currency,
                currency_decimals=first.currency_decimals if first else company.currency_decimals,
                status=_sheet_status(
                    sheet_periods, [(sl.line.net, sl.paid) for sl in sheet_lines], today, company
                ),
                absent=[
                    r.worker
                    for r in (absent.get(attendance_day.pk, []) if attendance_day else [])
                    if r.worker_id not in on_sheet
                ],
            )
        )
    return result


@dataclass
class CashSheetSummary:
    """A cash sheet's totals and status, without its lines (the Cash sheets list)."""

    site: Site
    date: datetime.date
    periods: list[PayPeriod]
    reconciliation: engine.CashReconciliation
    currency: str
    currency_decimals: int
    status: str = SHEET_NOT_OPEN


def cash_sheet_summaries(
    sheets: list[tuple[Site, datetime.date]],
    *,
    today: datetime.date | None = None,
    company: CompanySettings | None = None,
) -> list[CashSheetSummary]:
    """The totals and status of ``cash_sheets(sheets)``, in order, for a page of sheets.

    The same figures from the same rules (``engine.reconcile``, ``_sheet_status``), read
    as ``(net, paid)`` per pay line and the cash issued per sheet: three queries, plus
    the readiness check of each unlocked period, however many workers the sheets hold.
    """
    if not sheets:
        return []
    company = company or CompanySettings.load()
    today = today or company_today(company)
    keys = [(site.pk, day) for site, day in sheets]
    periods = _sheet_periods(keys)
    amounts = _line_amounts(p for group in periods.values() for p in group)
    issued = {
        (row["site_id"], row["date"]): row["total"]
        for row in CashIssue.objects.filter(_on_sheets(keys, "site_id", "date"))
        .values("site_id", "date")
        .annotate(total=Sum("amount"))
        .order_by()
    }

    result = []
    for (site, day), key in zip(sheets, keys, strict=True):
        sheet_periods = periods.get(key, [])
        sheet_amounts = [a for p in sheet_periods for a in amounts.get(p.pk, [])]
        first = sheet_periods[0] if sheet_periods else None
        result.append(
            CashSheetSummary(
                site=site,
                date=day,
                periods=sheet_periods,
                reconciliation=engine.reconcile(issued.get(key, 0), sheet_amounts),
                currency=first.currency if first else company.currency,
                currency_decimals=first.currency_decimals if first else company.currency_decimals,
                status=_sheet_status(sheet_periods, sheet_amounts, today, company),
            )
        )
    return result


def reconciliations(site_ids, day: datetime.date) -> dict[int, engine.CashReconciliation]:
    """``cash_sheet(site, day, refresh=False).reconciliation`` for many sites, in two queries.

    For overviews such as the dashboard, which must not run a query per site. Locked
    periods are left out (nothing more is paid on them: workers left unpaid were closed
    with a reason). Sites with neither a pay line nor cash issued that day are absent.
    """
    lines: dict[int, list[tuple[int, int]]] = defaultdict(list)
    rows = (
        PayLine.objects.filter(period__site__in=site_ids, period__end_date=day)
        .exclude(period__status=PayPeriod.Status.LOCKED)
        .annotate(paid=Sum("payments__amount"))
        .values_list("period__site", "net", "paid")
    )
    for site_id, net, paid in rows:
        lines[site_id].append((net, paid or 0))
    issued = dict(
        CashIssue.objects.filter(site__in=site_ids, date=day)
        .values("site")
        .annotate(total=Sum("amount"))
        .values_list("site", "total")
    )
    return {
        site_id: engine.reconcile(issued.get(site_id, 0), lines.get(site_id, []))
        for site_id in set(lines) | set(issued)
    }


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


# A bank transfer or mobile-money payment (``apps.payouts``) has no receipt to photograph:
# its transfer reference is the proof. ``apps.payouts`` records such payments inside
# ``transfer_proof(reference)``; ``validate_proof`` then accepts "none".
_transfer_reference: contextvars.ContextVar[str] = contextvars.ContextVar(
    "payroll_transfer_reference", default=""
)


@contextlib.contextmanager
def transfer_proof(reference: str):
    """Payments recorded in this block are transfers with ``reference`` as their proof."""
    token = _transfer_reference.set(reference.strip())
    try:
        yield
    finally:
        _transfer_reference.reset(token)


def validate_proof(proof_type: str, proof_file, company: CompanySettings) -> None:
    """The proof must be one of ``accepted_proofs``.

    "none" is allowed only when the setting lists it (or lists nothing), or for a
    transfer recorded with its reference (``transfer_proof``). A photo proof needs the
    photo; a signature or thumb mark may come as an image or be on the paper sheet. Any
    file is checked like every upload (type, ≤ 5 MB).
    """
    errors = {}
    accepted = list(company.accepted_proofs or [])
    if proof_type == ProofType.NONE and not proof_file and _transfer_reference.get():
        return
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
    """Remove a payment recorded by mistake (office only, before the period is locked).

    A pay corrected after the payment raised an adjustment on a later pay: while that
    adjustment is open it is cancelled with the payment (the line is recomputed from the
    corrected attendance instead); once it is settled on a paid line the payment can no
    longer be undone (``Conflict``).
    """
    line = payment.pay_line
    period = line.period
    if not can_manage_site(user, period.site):
        raise PermissionDenied
    if period.is_locked:
        raise Locked(_("This pay period is locked."), details={"status": "locked"})
    raised = list(line.raised_adjustments.all())
    if raised and _settled_adjustment_ids(raised):
        raise Conflict(
            _(
                "This pay was corrected after it was paid and the difference is already "
                "settled on a later pay: the payment cannot be undone."
            ),
            details={"pay_line_id": line.pk},
        )
    with transaction.atomic():
        _audit(
            user,
            "payroll.payment.undo",
            line,
            before=_payment_snapshot(payment),
            reason=reason,
        )
        payment.delete()
        for adjustment in raised:
            _cancel_adjustment(adjustment, user)
        update_period_status(period, actor=user)
    if raised:
        _refresh_later(period, {line.worker_id}, actor=user)


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
    """Approved → locked, through the attendance service."""
    lock_day(day, user)


def _lock_checked(user, period: PayPeriod, unpaid: list[PayLine], reason: str) -> PayPeriod:
    for line in unpaid:
        # The worker did not receive this pay, so no advance was repaid from it and no
        # adjustment was settled on it (they wait for a later pay).
        before = _line_snapshot(line)
        _apply_repayments(line, ())
        _apply_adjustments(line, (), user)
        line.advance_deduction = 0
        line.adjustment = 0
        line.net = line.gross - line.other_deductions - line.statutory_deductions
        line.save(update_fields=["advance_deduction", "adjustment", "net", "updated_at"])
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
    if not can_lock(user, period.site):
        raise PermissionDenied
    refresh_period(period, today=today)
    with transaction.atomic():
        unpaid = _check_lockable(user, period, reason, today)
        return _lock_checked(user, period, unpaid, reason)


def lock_sheet(user, site: Site, day: datetime.date, reason: str = "", *, today=None):
    """Close and lock every period paid on this cash sheet."""
    if not can_lock(user, site):
        raise PermissionDenied
    periods = sync_sheet(site, day, today=today)
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
