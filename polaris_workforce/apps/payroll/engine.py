"""The pay engine: pure functions over dataclasses. No database, no settings lookups.

Callers (``apps.payroll.services``) read the settings and the attendance, build the
inputs and store the results. Everything here is deterministic and exact:

* Money is an ``int`` in the currency's smallest unit (``apps.core.money``). Rates are
  minor units per day or per hour.
* Intermediate amounts are exact fractions. A pay line is rounded **once**, half up, to
  a whole minor unit, at the end of the line (``round_half_up``).
* Units (days) and hours are reported as ``Decimal`` with 2 places, for display and
  storage; the money is computed from the exact values, never from the rounded ones.

Rules
-----
Units per attendance record (``day_units``):

* present: 1
* half day: ``half_day_units`` (a setting, 0.50 by default)
* late: 1 when ``late_rule`` is ``full_day_flagged``; with ``deduct_minutes`` the late
  minutes (arrival − ``work_start``) are taken off pro rata of the scheduled day
  (``work_end − work_start − break``). A late record without an arrival time counts 1.
* absent: 0

Daily-rate workers earn ``rate × units``. Hourly workers earn ``rate × hours``, where hours
come from arrival/departure minus the break (no break is taken off a half day); for a
late record under ``full_day_flagged`` the hours count from ``work_start``. When a time
is missing the hours are the scheduled hours × the day's units.

Overtime: hours above ``overtime_after_hours`` in a day are paid × ``overtime_multiplier``.
For hourly workers that applies to their hours; daily-rate workers earn overtime only when
both times are recorded, at ``daily rate / scheduled hours`` per hour.

Advances (``compute_line``): one deduction per pay, from the oldest open advance first:
its ``deduction_per_pay``, capped at its balance and at ``gross − other deductions``, so
net is never negative. The next advance starts when the previous one is repaid.

Pay adjustments (``compute_line``, ``settle_adjustments``): a day corrected after it was
paid leaves a difference to settle on the worker's next pay. A positive one (underpaid)
is added in full next to the gross, before the advance deduction is worked out. Negative
ones (overpaid) are recovered after the advance deduction, oldest first, from what is
left, so net is never negative; whatever does not fit stays on the adjustment and is
recovered from the following pays.

    net = gross − other deductions − advance deduction + adjustment
"""

from __future__ import annotations

import calendar
import datetime
import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from fractions import Fraction

PRESENT = "present"
ABSENT = "absent"
HALF_DAY = "half_day"
LATE = "late"
STATUSES = (PRESENT, ABSENT, HALF_DAY, LATE)

DAILY = "daily"
HOURLY = "hourly"
PAY_TYPES = (DAILY, HOURLY)

FULL_DAY_FLAGGED = "full_day_flagged"
DEDUCT_MINUTES = "deduct_minutes"
LATE_RULES = (FULL_DAY_FLAGGED, DEDUCT_MINUTES)

WEEKLY = "weekly"
BIWEEKLY = "biweekly"
MONTHLY = "monthly"
EVERY_N_DAYS = "every_n_days"
FREQUENCIES = (DAILY, WEEKLY, BIWEEKLY, MONTHLY, EVERY_N_DAYS)

# Fixed reference for fortnightly and every-N-days cycles (a Monday).
EPOCH = datetime.date(2024, 1, 1)

TWO_PLACES = Decimal("0.01")


# --- helpers -----------------------------------------------------------------------------


def round_half_up(value: Fraction | int) -> int:
    """Round an exact amount to a whole minor unit, halves away from zero."""
    value = Fraction(value)
    if value >= 0:
        return math.floor(value + Fraction(1, 2))
    return -math.floor(-value + Fraction(1, 2))


def to_decimal(value: Fraction | int, places: Decimal = TWO_PLACES) -> Decimal:
    """An exact fraction as a ``Decimal`` rounded half up to ``places`` (display/storage)."""
    value = Fraction(value)
    exact = Decimal(value.numerator) / Decimal(value.denominator)
    return exact.quantize(places, rounding=ROUND_HALF_UP)


def _fraction(value) -> Fraction:
    if isinstance(value, bool | float):
        raise TypeError("use Decimal, int or str for pay rules, never float")
    return Fraction(value)


def _check_money(value, name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} must be an int in minor units")
    if value < 0:
        raise ValueError(f"{name} must not be negative")


def _minutes(value: datetime.time) -> int:
    return value.hour * 60 + value.minute


def span_minutes(start: datetime.time, end: datetime.time) -> int:
    """Minutes from ``start`` to ``end``; an end before the start is on the next day."""
    minutes = _minutes(end) - _minutes(start)
    return minutes + 24 * 60 if minutes < 0 else minutes


# --- rules and inputs --------------------------------------------------------------------


@dataclass(frozen=True)
class PayRules:
    """The settings the engine needs, already resolved for the site and worker."""

    half_day_units: Decimal = Decimal("0.50")
    late_rule: str = FULL_DAY_FLAGGED
    work_start: datetime.time = datetime.time(7, 0)
    work_end: datetime.time = datetime.time(17, 0)
    break_minutes: int = 60
    overtime_after_hours: Decimal | None = Decimal("9")
    overtime_multiplier: Decimal = Decimal("1.5")

    def __post_init__(self):
        if self.late_rule not in LATE_RULES:
            raise ValueError(f"unknown late rule {self.late_rule!r}")
        if not 0 <= _fraction(self.half_day_units) <= 1:
            raise ValueError("half_day_units must be between 0 and 1")
        if self.break_minutes < 0:
            raise ValueError("break_minutes must not be negative")
        if _fraction(self.overtime_multiplier) < 0:
            raise ValueError("overtime_multiplier must not be negative")
        if self.overtime_after_hours is not None and _fraction(self.overtime_after_hours) < 0:
            raise ValueError("overtime_after_hours must not be negative")

    @property
    def scheduled_minutes(self) -> int:
        """Paid minutes in a normal day: work_end − work_start − break (never negative)."""
        return max(0, span_minutes(self.work_start, self.work_end) - self.break_minutes)


@dataclass(frozen=True)
class DayInput:
    """One attendance record with the pay rate in effect that date."""

    date: datetime.date
    status: str
    pay_type: str
    rate: int
    arrival: datetime.time | None = None
    departure: datetime.time | None = None

    def __post_init__(self):
        if self.status not in STATUSES:
            raise ValueError(f"unknown attendance status {self.status!r}")
        if self.pay_type not in PAY_TYPES:
            raise ValueError(f"unknown pay type {self.pay_type!r}")
        _check_money(self.rate, "rate")


@dataclass(frozen=True)
class DayResult:
    date: datetime.date
    status: str
    pay_type: str
    rate: int
    units: Fraction  # days worked (0…1)
    hours: Fraction | None  # paid hours (hourly) or measured hours (daily, both times known)
    overtime_hours: Fraction
    late_minutes: int
    amount: Fraction  # exact, not rounded

    @property
    def gross(self) -> int:
        """This day alone, rounded (display only; the line rounds its exact total)."""
        return round_half_up(self.amount)

    def as_dict(self) -> dict:
        """JSON-friendly snapshot (stored on the pay line, shown on the payslip)."""
        return {
            "date": self.date.isoformat(),
            "status": self.status,
            "pay_type": self.pay_type,
            "rate": self.rate,
            "units": str(to_decimal(self.units)),
            "hours": None if self.hours is None else str(to_decimal(self.hours)),
            "overtime_hours": str(to_decimal(self.overtime_hours)),
            "late_minutes": self.late_minutes,
            "gross": self.gross,
        }


@dataclass(frozen=True)
class AdvanceInput:
    """An open advance, oldest first. ``balance`` is what is still owed."""

    advance_id: int
    balance: int
    deduction_per_pay: int

    def __post_init__(self):
        _check_money(self.balance, "balance")
        _check_money(self.deduction_per_pay, "deduction_per_pay")


@dataclass(frozen=True)
class AdjustmentInput:
    """A pay adjustment still to settle, oldest first.

    ``amount`` is what is left of it: positive is owed to the worker, negative is to be
    recovered from them.
    """

    adjustment_id: int
    amount: int

    def __post_init__(self):
        if isinstance(self.amount, bool) or not isinstance(self.amount, int):
            raise TypeError("amount must be an int in minor units")


@dataclass(frozen=True)
class LineResult:
    days: tuple[DayResult, ...]
    units: Decimal
    hours: Decimal | None
    overtime_hours: Decimal
    gross: int
    other_deductions: int
    advance_deduction: int
    repayments: tuple[tuple[int, int], ...]  # (advance_id, amount)
    net: int
    adjustment: int = 0  # signed: added − recovered
    adjustments: tuple[tuple[int, int], ...] = ()  # (adjustment_id, signed amount applied)


# --- per day -----------------------------------------------------------------------------


def late_minutes(arrival: datetime.time | None, rules: PayRules) -> int:
    """Minutes after ``work_start`` (0 when on time or unknown)."""
    if arrival is None:
        return 0
    minutes = _minutes(arrival) - _minutes(rules.work_start)
    return max(0, minutes)


def day_units(status: str, rules: PayRules, arrival: datetime.time | None = None) -> Fraction:
    """Days worked for one record (see the module docstring)."""
    if status == PRESENT:
        return Fraction(1)
    if status == ABSENT:
        return Fraction(0)
    if status == HALF_DAY:
        return _fraction(rules.half_day_units)
    if status == LATE:
        if rules.late_rule == FULL_DAY_FLAGGED or rules.scheduled_minutes == 0:
            return Fraction(1)
        lost = Fraction(late_minutes(arrival, rules), rules.scheduled_minutes)
        return max(Fraction(0), 1 - lost)
    raise ValueError(f"unknown attendance status {status!r}")


def _split_overtime(hours: Fraction, rules: PayRules) -> tuple[Fraction, Fraction]:
    if rules.overtime_after_hours is None:
        return hours, Fraction(0)
    threshold = _fraction(rules.overtime_after_hours)
    if hours <= threshold:
        return hours, Fraction(0)
    return threshold, hours - threshold


def _measured_hours(day: DayInput, rules: PayRules, *, from_work_start: bool) -> Fraction:
    start = day.arrival
    if from_work_start and _minutes(start) > _minutes(rules.work_start):
        start = rules.work_start
    minutes = span_minutes(start, day.departure)
    if day.status != HALF_DAY:
        minutes -= rules.break_minutes
    return Fraction(max(0, minutes), 60)


def compute_day(day: DayInput, rules: PayRules) -> DayResult:
    units = day_units(day.status, rules, day.arrival)
    lateness = late_minutes(day.arrival, rules) if day.status == LATE else 0
    has_times = day.arrival is not None and day.departure is not None
    multiplier = _fraction(rules.overtime_multiplier)

    if day.status == ABSENT:
        hours = Fraction(0) if day.pay_type == HOURLY else None
        return DayResult(
            day.date, day.status, day.pay_type, day.rate, units, hours, Fraction(0), 0, Fraction(0)
        )

    if day.pay_type == HOURLY:
        if has_times:
            hours = _measured_hours(
                day,
                rules,
                from_work_start=day.status == LATE and rules.late_rule == FULL_DAY_FLAGGED,
            )
        else:
            hours = Fraction(rules.scheduled_minutes, 60) * units
        regular, overtime = _split_overtime(hours, rules)
        amount = day.rate * regular + day.rate * overtime * multiplier
        return DayResult(
            day.date, day.status, day.pay_type, day.rate, units, hours, overtime, lateness, amount
        )

    # Daily rate.
    amount = day.rate * units
    hours = None
    overtime = Fraction(0)
    if has_times and day.status in (PRESENT, LATE):
        hours = _measured_hours(day, rules, from_work_start=False)
        _regular, overtime = _split_overtime(hours, rules)
        if overtime and rules.scheduled_minutes:
            hourly_equivalent = Fraction(day.rate * 60, rules.scheduled_minutes)
            amount += hourly_equivalent * overtime * multiplier
        else:
            overtime = Fraction(0)
    return DayResult(
        day.date, day.status, day.pay_type, day.rate, units, hours, overtime, lateness, amount
    )


# --- per line ----------------------------------------------------------------------------


def advance_deductions(
    advances: Sequence[AdvanceInput], capacity: int
) -> tuple[tuple[int, int], ...]:
    """What to take from each advance this pay: the oldest open one, within ``capacity``."""
    for advance in advances:
        if advance.balance <= 0:
            continue
        take = min(advance.deduction_per_pay, advance.balance, max(0, capacity))
        return ((advance.advance_id, take),) if take > 0 else ()
    return ()


def settle_adjustments(
    adjustments: Sequence[AdjustmentInput], capacity: int
) -> tuple[tuple[int, int], ...]:
    """What to apply of each adjustment on one pay, in the given (oldest first) order.

    ``capacity`` is what is left of the pay without the adjustments: gross − other
    deductions − advance deduction (negative when the advance deduction used part of a
    positive adjustment). Positive adjustments are applied in full and add to it;
    negative ones are recovered from it, oldest first, until it is used up, so the pay
    never goes below zero. Adjustments with nothing left are skipped.
    """
    room = max(0, capacity + sum(a.amount for a in adjustments if a.amount > 0))
    applied = []
    for adjustment in adjustments:
        if adjustment.amount > 0:
            applied.append((adjustment.adjustment_id, adjustment.amount))
        elif adjustment.amount < 0:
            take = min(-adjustment.amount, room)
            if take > 0:
                applied.append((adjustment.adjustment_id, -take))
                room -= take
    return tuple(applied)


def compute_line(
    days: Iterable[DayInput],
    rules: PayRules,
    advances: Sequence[AdvanceInput] = (),
    other_deductions: int = 0,
    adjustments: Sequence[AdjustmentInput] = (),
) -> LineResult:
    """One worker's pay for a period: gross, deductions, adjustments and net (never negative)."""
    _check_money(other_deductions, "other_deductions")
    results = tuple(compute_day(day, rules) for day in days)
    gross = round_half_up(sum((r.amount for r in results), Fraction(0)))
    other = min(other_deductions, gross)
    added = sum(a.amount for a in adjustments if a.amount > 0)
    repayments = advance_deductions(advances, gross + added - other)
    advance = sum(amount for _id, amount in repayments)
    applied = settle_adjustments(adjustments, gross - other - advance)
    adjustment = sum(amount for _id, amount in applied)
    hourly = [r.hours for r in results if r.pay_type == HOURLY and r.hours is not None]
    return LineResult(
        days=results,
        units=to_decimal(sum((r.units for r in results), Fraction(0))),
        hours=to_decimal(sum(hourly, Fraction(0))) if hourly else None,
        overtime_hours=to_decimal(sum((r.overtime_hours for r in results), Fraction(0))),
        gross=gross,
        other_deductions=other,
        advance_deduction=advance,
        repayments=repayments,
        net=gross - other - advance + adjustment,
        adjustment=adjustment,
        adjustments=applied,
    )


# --- pay periods -------------------------------------------------------------------------


def pay_weekday(working_days: Iterable[int]) -> int:
    """ISO weekday weekly pay falls on: the last working day of the week (Sunday if none)."""
    days = [d for d in working_days if 1 <= d <= 7]
    return max(days) if days else 7


def period_bounds(
    day: datetime.date,
    frequency: str,
    *,
    every_n_days: int | None = None,
    anchor: datetime.date = EPOCH,
    payday: int = 6,
) -> tuple[datetime.date, datetime.date]:
    """The pay period (first day, last day = pay day) that contains ``day``.

    * daily: the day itself.
    * weekly: the 7 days ending on ``payday`` (an ISO weekday, normally the last working
      day), so the cash is paid on a working day.
    * biweekly: 14 days ending on ``payday``, in fortnights counted from ``anchor``.
    * monthly: the calendar month.
    * every_n_days: blocks of ``every_n_days`` counted from ``anchor``.
    """
    if frequency == DAILY:
        return day, day
    if frequency in (WEEKLY, BIWEEKLY):
        if not 1 <= payday <= 7:
            raise ValueError("payday must be an ISO weekday (1–7)")
        end = day + datetime.timedelta(days=(payday - day.isoweekday()) % 7)
        if frequency == WEEKLY:
            return end - datetime.timedelta(days=6), end
        first_payday = anchor + datetime.timedelta(days=(payday - anchor.isoweekday()) % 7)
        if ((end - first_payday).days // 7) % 2:
            end += datetime.timedelta(days=7)
        return end - datetime.timedelta(days=13), end
    if frequency == MONTHLY:
        last = calendar.monthrange(day.year, day.month)[1]
        return day.replace(day=1), day.replace(day=last)
    if frequency == EVERY_N_DAYS:
        if not every_n_days or every_n_days < 1:
            raise ValueError("every_n_days needs a number of days (1 or more)")
        offset = (day - anchor).days // every_n_days
        start = anchor + datetime.timedelta(days=offset * every_n_days)
        return start, start + datetime.timedelta(days=every_n_days - 1)
    raise ValueError(f"unknown pay frequency {frequency!r}")


# --- cash sheet --------------------------------------------------------------------------


@dataclass(frozen=True)
class CashReconciliation:
    cash_issued: int
    total_to_pay: int
    paid_total: int
    left_to_pay: int
    cash_in_hand: int
    return_amount: int  # cash_issued − total_to_pay; negative means the cash was short
    line_count: int
    paid_count: int

    @property
    def left_count(self) -> int:
        return self.line_count - self.paid_count

    @property
    def short_by(self) -> int:
        """How much cash is missing to pay everyone (0 when enough was issued)."""
        return max(0, -self.return_amount)


def reconcile(cash_issued: int, lines: Iterable[tuple[int, int]]) -> CashReconciliation:
    """Cash sheet totals from the cash issued and ``(net, paid)`` per line.

    A line counts as paid once its payments cover its net (a net of 0 needs no payment).
    """
    _check_money(cash_issued, "cash_issued")
    total = paid = count = paid_count = 0
    for net, paid_amount in lines:
        _check_money(net, "net")
        _check_money(paid_amount, "paid")
        total += net
        paid += paid_amount
        count += 1
        paid_count += paid_amount >= net
    return CashReconciliation(
        cash_issued=cash_issued,
        total_to_pay=total,
        paid_total=paid,
        left_to_pay=total - paid,
        cash_in_hand=cash_issued - paid,
        return_amount=cash_issued - total,
        line_count=count,
        paid_count=paid_count,
    )


# --- advances ----------------------------------------------------------------------------


def expected_weekly_pay(
    pay_type: str, rate: int, working_days_per_week: int, scheduled_minutes: int
) -> int:
    """A normal week's gross: rate × working days (× scheduled hours for hourly workers)."""
    _check_money(rate, "rate")
    if pay_type == HOURLY:
        return round_half_up(Fraction(rate * scheduled_minutes, 60) * working_days_per_week)
    if pay_type == DAILY:
        return rate * working_days_per_week
    raise ValueError(f"unknown pay type {pay_type!r}")


@dataclass(frozen=True)
class AdvanceCheck:
    owed: int  # already owed on open advances
    amount: int  # the new advance
    new_total: int
    expected_weekly_pay: int
    limit_percent: int
    limit_amount: int
    percent_of_weekly_pay: int | None  # None when there is no expected pay
    pays_to_repay: int | None  # None when nothing would be deducted
    allowed: bool


def check_advance(
    *,
    amount: int,
    deduction_per_pay: int,
    owed: Sequence[tuple[int, int]] = (),
    expected_weekly: int,
    limit_percent: int,
) -> AdvanceCheck:
    """Whether a new advance fits the limit: owed + amount ≤ limit % of expected weekly pay.

    ``owed`` is ``(balance, deduction_per_pay)`` per open advance. Advances are repaid one
    after the other, so the number of pays is the sum of each advance's pays.
    """
    _check_money(amount, "amount")
    _check_money(deduction_per_pay, "deduction_per_pay")
    _check_money(expected_weekly, "expected_weekly")
    already = sum(balance for balance, _per in owed)
    new_total = already + amount
    limit_amount = expected_weekly * limit_percent // 100
    percent = round_half_up(Fraction(100 * new_total, expected_weekly)) if expected_weekly else None
    pays = None
    schedule = [*owed, (amount, deduction_per_pay)]
    if all(per > 0 for balance, per in schedule if balance > 0):
        pays = sum(math.ceil(Fraction(balance, per)) for balance, per in schedule if balance > 0)
    return AdvanceCheck(
        owed=already,
        amount=amount,
        new_total=new_total,
        expected_weekly_pay=expected_weekly,
        limit_percent=limit_percent,
        limit_amount=limit_amount,
        percent_of_weekly_pay=percent,
        pays_to_repay=pays,
        allowed=amount > 0 and expected_weekly > 0 and new_total <= limit_amount,
    )
