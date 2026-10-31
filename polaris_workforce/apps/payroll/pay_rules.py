"""Pay rules services (Wave 4): the company's rules and the day-by-day extras.

* **Rules** (admins only, audited): multipliers, night window and stoppage pay
  (``PayRuleSettings``, one per company), a site's own multipliers (``SitePayRules``),
  statutory deduction rules (``DeductionRule``) and piece-work task types (``TaskType``).
  A change applies to pay that is not paid or locked yet, the next time it is computed.
* **Extras** (site managers on their sites, admins; audited): piece-work entries, bonuses
  and stoppage days. Each write recomputes the pay it touches (``services.sync_site``).
  A date whose pay is paid or locked is read-only: a correction there goes through the
  attendance corrections and pay adjustments instead.
* **Employer cost** (``employer_costs``): the statutory deductions of pay periods ending
  in a range, per rule, for the reports.

The arithmetic is in ``apps.payroll.engine``; every query starts from the user's company
(``scoped_sites`` / ``company_workers``) or from an object already checked.
"""

from __future__ import annotations

import datetime
from collections import defaultdict
from dataclasses import dataclass, field
from decimal import Decimal

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.db.models import Q, Sum
from django.utils.translation import gettext as _

from apps.accounts.permissions import can_manage_site, is_admin
from apps.api.exceptions import Locked
from apps.attendance.models import AttendanceDay
from apps.core.dates import company_today
from apps.core.models import CompanySettings
from apps.core.tenancy import company_id_of
from apps.worksites.models import Site, SiteAssignment

from . import services
from .models import (
    Bonus,
    DeductionRule,
    PayLine,
    PayPeriod,
    PayRuleSettings,
    PieceWorkEntry,
    SitePayRules,
    StoppageDay,
    StoppagePay,
    TaskType,
)

RULE_FIELDS = (
    "sunday_multiplier",
    "holiday_multiplier",
    "night_start",
    "night_end",
    "night_multiplier",
    "stoppage_pay",
    "stoppage_percent",
    "stoppage_amount",
)
DEDUCTION_FIELDS = (
    "name",
    "employee_percent",
    "employer_percent",
    "base_floor",
    "base_ceiling",
    "pay_types",
    "active_from",
    "active_to",
    "order",
)
TASK_FIELDS = ("name", "unit", "unit_rate", "is_active")


def _json(value):
    if isinstance(value, datetime.time):
        return value.strftime("%H:%M")
    if isinstance(value, datetime.date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return format(value.normalize(), "f")
    return value


def snapshot(obj, names) -> dict:
    return {name: _json(getattr(obj, name)) for name in names}


def _require_admin(user) -> None:
    if not is_admin(user):
        raise PermissionDenied


def _save_audited(user, obj, names, before: dict | None, action: str) -> bool:
    """Save ``obj`` (already validated) and audit the changed fields; False if none."""
    after = snapshot(obj, names)
    if before is not None:
        changed = [name for name in names if after[name] != before.get(name)]
        if not changed:
            return False
        before = {name: before.get(name) for name in changed}
        after = {name: after[name] for name in changed}
    with transaction.atomic():
        obj.save()
        services._audit(user, action, obj, before=before, after=after)
    return True


# --- the company's rules (admins) ------------------------------------------------------------


def save_rules(user, rules: PayRuleSettings, before: dict) -> bool:
    """Save the company's multipliers and stoppage pay (``before``: ``snapshot`` taken
    before the form copied the new values on)."""
    _require_admin(user)
    return _save_audited(user, rules, RULE_FIELDS, before, "payroll.rules.update")


def save_site_rules(user, site_rules: SitePayRules, before: dict | None) -> bool:
    _require_admin(user)
    if not services.scoped_sites(user).filter(pk=site_rules.site_id).exists():
        raise PermissionDenied
    return _save_audited(
        user, site_rules, SitePayRules.OVERRIDES, before, "payroll.site_rules.update"
    )


def save_deduction_rule(user, rule: DeductionRule, before: dict | None) -> bool:
    """Create (``before`` None) or change a deduction rule of the user's company."""
    _require_admin(user)
    if rule.pk is None:
        rule.company_id = _company_id(user)
        action = "payroll.deduction_rule.create"
    else:
        action = "payroll.deduction_rule.update"
    return _save_audited(user, rule, DEDUCTION_FIELDS, before, action)


def save_task_type(user, task: TaskType, before: dict | None) -> bool:
    _require_admin(user)
    if task.pk is None:
        task.company_id = _company_id(user)
    action = "payroll.task_type.create" if task.pk is None else "payroll.task_type.update"
    return _save_audited(user, task, TASK_FIELDS, before, action)


def company_rules(user):
    """The deduction rules and task types of the user's company (empty without one)."""
    company_id = _company_id(user)
    if company_id is None:
        return DeductionRule.objects.none(), TaskType.objects.none()
    return (
        DeductionRule.objects.filter(company_id=company_id),
        TaskType.objects.filter(company_id=company_id),
    )


def _company_id(user):
    """The company the user works in (``None`` when signed out or inactive)."""
    if not getattr(user, "is_authenticated", False) or not user.is_active:
        return None
    return company_id_of(user)


# --- what can still change on a date ------------------------------------------------------


def _periods_covering(site: Site, date: datetime.date):
    return PayPeriod.objects.filter(site=site, start_date__lte=date, end_date__gte=date)


def check_open(site: Site, date: datetime.date, worker_id: int | None = None) -> None:
    """Refuse a change to the pay of ``date`` once it is paid or locked (``Locked``).

    With ``worker_id``, only that worker's pay counts as paid; without it (a stoppage
    day concerns the whole site), anyone's.
    """
    periods = _periods_covering(site, date)
    if periods.filter(status=PayPeriod.Status.LOCKED).exists():
        raise Locked(_("This pay period is locked."), details={"status": "locked"})
    paid = PayLine.objects.filter(period__in=periods, payments__isnull=False)
    if worker_id is not None:
        paid = paid.filter(worker_id=worker_id)
    if paid.exists():
        raise Locked(
            _("This pay is already paid: correct the attendance instead."),
            details={"status": "paid"},
        )


def _refresh(site: Site, start: datetime.date, end: datetime.date, user) -> None:
    company = CompanySettings.load(site.company_id)
    services.sync_site(site, start, end, actor=user, company=company)


def _on_crew(worker, site: Site, date: datetime.date) -> bool:
    return (
        worker.company_id == site.company_id
        and SiteAssignment.objects.active_on(date).filter(worker=worker, site=site).exists()
    )


def _check_site(user, site: Site) -> None:
    if not can_manage_site(user, site) or site.company_id != _company_id(user):
        raise PermissionDenied


# --- piece work ----------------------------------------------------------------------------


def piece_entry_snapshot(entry: PieceWorkEntry) -> dict:
    return {
        "worker_id": entry.worker_id,
        "site_id": entry.site_id,
        "date": entry.date.isoformat(),
        "task_id": entry.task_id,
        "task": entry.task.name,
        "quantity": _json(entry.quantity),
        "unit_rate": entry.unit_rate,
    }


def add_piece_work(
    user, site: Site, worker, date: datetime.date, task: TaskType, quantity, note=""
) -> PieceWorkEntry:
    """Record work done by the piece; paid once the day's roll call is approved."""
    _check_site(user, site)
    errors = {}
    if not _on_crew(worker, site, date):
        errors["worker"] = [_("This worker is not on the site's crew that day.")]
    if task.company_id != site.company_id or not task.is_active:
        errors["task"] = [_("Choose one of the company's active tasks.")]
    quantity = Decimal(quantity)
    if quantity <= 0:
        errors["quantity"] = [_("Enter a quantity greater than zero.")]
    if errors:
        raise ValidationError(errors)
    check_open(site, date, worker.pk)
    with transaction.atomic():
        entry = PieceWorkEntry.objects.create(
            worker=worker,
            site=site,
            date=date,
            task=task,
            quantity=quantity,
            unit_rate=task.unit_rate,
            day=AttendanceDay.objects.filter(site=site, date=date).first(),
            entered_by=user,
            note=note,
        )
        services._audit(user, "payroll.piece.create", entry, after=piece_entry_snapshot(entry))
    _refresh(site, date, date, user)
    return entry


def delete_piece_work(user, entry: PieceWorkEntry, reason: str = "") -> None:
    site = entry.site
    _check_site(user, site)
    check_open(site, entry.date, entry.worker_id)
    with transaction.atomic():
        services._audit(
            user, "payroll.piece.delete", entry, before=piece_entry_snapshot(entry), reason=reason
        )
        entry.delete()
    _refresh(site, entry.date, entry.date, user)


# --- bonuses -------------------------------------------------------------------------------


def bonus_snapshot(bonus: Bonus) -> dict:
    return {
        "worker_id": bonus.worker_id,
        "site_id": bonus.site_id,
        "amount": bonus.amount,
        "date": bonus.date.isoformat(),
        "applied_to_id": bonus.applied_to_id,
    }


def add_bonus(user, site: Site, worker, date: datetime.date, amount: int, reason: str) -> Bonus:
    """A one-off bonus, added to the worker's next pay at the site (from ``date`` on)."""
    _check_site(user, site)
    errors = {}
    if not _on_crew(worker, site, date):
        errors["worker"] = [_("This worker is not on the site's crew that day.")]
    if isinstance(amount, bool) or not isinstance(amount, int) or amount <= 0:
        errors["amount"] = [_("Enter an amount greater than zero.")]
    if not (reason or "").strip():
        errors["reason"] = [_("Give the reason for the bonus.")]
    if errors:
        raise ValidationError(errors)
    with transaction.atomic():
        bonus = Bonus.objects.create(
            worker=worker, site=site, date=date, amount=amount, reason=reason, created_by=user
        )
        services._audit(
            user, "payroll.bonus.create", bonus, after=bonus_snapshot(bonus), reason=reason
        )
    company = CompanySettings.load(site.company_id)
    _refresh(site, date, max(date, company_today(company)), user)
    return bonus


def bonus_is_settled(bonus: Bonus) -> bool:
    line = bonus.applied_to
    return line is not None and (line.payments.exists() or line.period.is_locked)


def delete_bonus(user, bonus: Bonus, reason: str = "") -> None:
    site = bonus.site
    _check_site(user, site)
    if bonus_is_settled(bonus):
        raise Locked(_("This bonus is already paid."), details={"status": "paid"})
    line = bonus.applied_to
    with transaction.atomic():
        services._audit(
            user, "payroll.bonus.delete", bonus, before=bonus_snapshot(bonus), reason=reason
        )
        bonus.delete()
    if line is not None:
        services.refresh_period(line.period, actor=user)


# --- stoppage days -------------------------------------------------------------------------


def stoppage_snapshot(stoppage: StoppageDay) -> dict:
    return {
        "site_id": stoppage.site_id,
        "date": stoppage.date.isoformat(),
        "reason": stoppage.reason,
        "pay": stoppage.pay,
        "percent": _json(stoppage.percent),
        "amount": stoppage.amount,
    }


def set_stoppage(
    user,
    site: Site,
    date: datetime.date,
    reason: str,
    *,
    pay: str | None = None,
    percent: Decimal | None = None,
    amount: int | None = None,
) -> StoppageDay:
    """Mark ``date`` as a stoppage day at the site (or change it). Pay defaults to the
    company's pay rules."""
    _check_site(user, site)
    if not (reason or "").strip():
        raise ValidationError({"reason": [_("Give the reason for the stoppage.")]})
    rules = PayRuleSettings.load(site.company_id)
    pay = pay or rules.stoppage_pay
    if pay == StoppagePay.FIXED:
        percent, amount = None, rules.stoppage_amount if amount is None else amount
        if isinstance(amount, bool) or not isinstance(amount, int) or amount < 0:
            raise ValidationError({"amount": [_("Enter an amount of zero or more.")]})
    else:
        pay, amount = StoppagePay.PERCENT, None
        percent = rules.stoppage_percent if percent is None else Decimal(percent)
        if not 0 <= percent <= 100:
            raise ValidationError({"percent": [_("Enter a share between 0 and 100 %.")]})
    check_open(site, date)
    with transaction.atomic():
        stoppage = StoppageDay.objects.filter(site=site, date=date).first()
        before = stoppage_snapshot(stoppage) if stoppage else None
        if stoppage is None:
            stoppage = StoppageDay(site=site, date=date, created_by=user)
        stoppage.reason, stoppage.pay = reason, pay
        stoppage.percent, stoppage.amount = percent, amount
        stoppage.save()
        after = stoppage_snapshot(stoppage)
        if after != before:
            services._audit(
                user, "payroll.stoppage.set", stoppage, before=before, after=after, reason=reason
            )
    _refresh(site, date, date, user)
    return stoppage


def clear_stoppage(user, site: Site, date: datetime.date, reason: str = "") -> bool:
    _check_site(user, site)
    stoppage = StoppageDay.objects.filter(site=site, date=date).first()
    if stoppage is None:
        return False
    check_open(site, date)
    with transaction.atomic():
        services._audit(
            user,
            "payroll.stoppage.clear",
            stoppage,
            before=stoppage_snapshot(stoppage),
            reason=reason,
        )
        stoppage.delete()
    _refresh(site, date, date, user)
    return True


# --- the extras of a site and date (screen) ------------------------------------------------


@dataclass
class DayExtras:
    site: Site
    date: datetime.date
    pieces: list = field(default_factory=list)
    bonuses: list = field(default_factory=list)
    stoppage: StoppageDay | None = None
    is_open: bool = True  # nothing paid or locked yet on this date


def day_extras(site: Site, date: datetime.date) -> DayExtras:
    try:
        check_open(site, date)
        is_open = True
    except Locked:
        is_open = False
    return DayExtras(
        site=site,
        date=date,
        pieces=list(
            PieceWorkEntry.objects.filter(site=site, date=date)
            .select_related("worker", "task", "entered_by")
            .order_by("worker__last_name", "worker__first_name", "pk")
        ),
        bonuses=list(
            Bonus.objects.filter(site=site, date=date)
            .select_related("worker", "created_by", "applied_to__period")
            .order_by("worker__last_name", "pk")
        ),
        stoppage=StoppageDay.objects.filter(site=site, date=date).first(),
        is_open=is_open,
    )


def crew_on(user, site: Site, date: datetime.date):
    """Workers on the site's crew that day, within the user's company (form choices)."""
    worker_ids = SiteAssignment.objects.active_on(date).filter(site=site).values("worker_id")
    return (
        services.company_workers(user)
        .filter(pk__in=worker_ids)
        .order_by("last_name", "first_name", "number")
    )


# --- employer cost (for the reports) -------------------------------------------------------


@dataclass
class RuleCost:
    rule_id: int | None
    name: str
    employee: int = 0
    employer: int = 0


@dataclass
class EmployerCost:
    """Statutory deductions of the pay periods ending in a range."""

    gross: int = 0
    employee: int = 0  # deducted from the workers' pay
    employer: int = 0  # the company's cost on top of the gross
    rules: list[RuleCost] = field(default_factory=list)

    @property
    def total_cost(self) -> int:
        """What the pay costs the company: gross + employer contributions."""
        return self.gross + self.employer


def employer_costs(sites, start: datetime.date, end: datetime.date) -> EmployerCost:
    """Statutory deductions and employer contributions of the pay periods that **end**
    between ``start`` and ``end`` (inclusive) at ``sites`` (a queryset the caller has
    scoped, e.g. ``scoped_sites(user)``), in total and per rule (in rule order)."""
    lines = PayLine.objects.filter(
        period__site__in=sites, period__end_date__gte=start, period__end_date__lte=end
    )
    totals = lines.aggregate(
        gross=Sum("gross"),
        employee=Sum("statutory_deductions"),
        employer=Sum("employer_contributions"),
    )
    result = EmployerCost(
        gross=totals["gross"] or 0,
        employee=totals["employee"] or 0,
        employer=totals["employer"] or 0,
    )
    by_rule: dict = defaultdict(lambda: [None, 0, 0])
    for deductions in lines.exclude(Q(deductions=[]) | Q(deductions__isnull=True)).values_list(
        "deductions", flat=True
    ):
        for item in deductions or []:
            key = item.get("rule_id") or item.get("name")
            entry = by_rule[key]
            entry[0] = entry[0] or item.get("name", "")
            entry[1] += int(item.get("employee") or 0)
            entry[2] += int(item.get("employer") or 0)
    order = {r.pk: (r.order, r.name) for r in DeductionRule.objects.filter(pk__in=list(by_rule))}
    for key in sorted(by_rule, key=lambda k: (order.get(k, (9999, str(k))), str(k))):
        name, employee, employer = by_rule[key]
        result.rules.append(
            RuleCost(
                rule_id=key if isinstance(key, int) else None,
                name=name,
                employee=employee,
                employer=employer,
            )
        )
    return result
