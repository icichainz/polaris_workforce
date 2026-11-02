"""The pay engine: table-driven tests for every rule × setting, plus property tests.

Property tests use plain loops over seeded ``random.Random`` instances (hypothesis is not
installed), so every failure is reproducible from its seed.
"""

import datetime
import random
from decimal import Decimal
from fractions import Fraction

import pytest

from apps.payroll import engine
from apps.payroll.engine import (
    ABSENT,
    DAILY,
    DEDUCT_MINUTES,
    FULL_DAY_FLAGGED,
    HALF_DAY,
    HOURLY,
    LATE,
    PRESENT,
    AdjustmentInput,
    AdvanceInput,
    DayInput,
    PayRules,
)

D = datetime.date(2026, 10, 30)
T = datetime.time


def day(status=PRESENT, rate=6000, pay_type=DAILY, arrival=None, departure=None, date=D):
    return DayInput(date, status, pay_type, rate, arrival, departure)


# --- rounding ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (Fraction(0), 0),
        (Fraction(1, 2), 1),
        (Fraction(3, 2), 2),
        (Fraction(5, 2), 3),  # half up, not banker's rounding
        (Fraction(2499, 1000), 2),
        (Fraction(2501, 1000), 3),
        (Fraction(-1, 2), -1),
        (Fraction(-3, 2), -2),
        (7, 7),
    ],
)
def test_round_half_up(value, expected):
    assert engine.round_half_up(value) == expected


def test_to_decimal_rounds_half_up_to_two_places():
    assert engine.to_decimal(Fraction(1, 8)) == Decimal("0.13")
    assert engine.to_decimal(Fraction(26, 27)) == Decimal("0.96")
    assert engine.to_decimal(5) == Decimal("5.00")


# --- units per record: every status × late rule × half-day setting -------------------------


UNIT_CASES = [
    # status, late_rule, half_day_units, arrival, expected units
    (PRESENT, FULL_DAY_FLAGGED, "0.50", None, Fraction(1)),
    (PRESENT, DEDUCT_MINUTES, "0.50", T(7, 40), Fraction(1)),
    (ABSENT, FULL_DAY_FLAGGED, "0.50", None, Fraction(0)),
    (ABSENT, DEDUCT_MINUTES, "0.50", None, Fraction(0)),
    (HALF_DAY, FULL_DAY_FLAGGED, "0.50", None, Fraction(1, 2)),
    (HALF_DAY, DEDUCT_MINUTES, "0.50", None, Fraction(1, 2)),
    (HALF_DAY, FULL_DAY_FLAGGED, "0.60", None, Fraction(3, 5)),
    (HALF_DAY, FULL_DAY_FLAGGED, "0.00", None, Fraction(0)),
    (HALF_DAY, FULL_DAY_FLAGGED, "1.00", None, Fraction(1)),
    (LATE, FULL_DAY_FLAGGED, "0.50", T(7, 45), Fraction(1)),
    (LATE, FULL_DAY_FLAGGED, "0.50", None, Fraction(1)),
    # 07:00–17:00 minus 60 min break = 540 scheduled minutes.
    (LATE, DEDUCT_MINUTES, "0.50", T(7, 54), 1 - Fraction(54, 540)),
    (LATE, DEDUCT_MINUTES, "0.50", T(8, 0), 1 - Fraction(60, 540)),
    (LATE, DEDUCT_MINUTES, "0.50", T(7, 0), Fraction(1)),  # marked late but on time
    (LATE, DEDUCT_MINUTES, "0.50", T(6, 50), Fraction(1)),
    (LATE, DEDUCT_MINUTES, "0.50", None, Fraction(1)),  # no arrival: nothing to deduct
    (LATE, DEDUCT_MINUTES, "0.50", T(23, 0), Fraction(0)),  # never below zero
]


@pytest.mark.parametrize(("status", "late_rule", "half", "arrival", "expected"), UNIT_CASES)
def test_day_units(status, late_rule, half, arrival, expected):
    rules = PayRules(half_day_units=Decimal(half), late_rule=late_rule)

    assert engine.day_units(status, rules, arrival) == expected


def test_deduct_minutes_uses_the_scheduled_day_of_the_settings():
    rules = PayRules(
        late_rule=DEDUCT_MINUTES,
        work_start=T(8, 0),
        work_end=T(16, 0),
        break_minutes=0,
    )

    assert engine.day_units(LATE, rules, T(9, 0)) == Fraction(7, 8)


def test_deduct_minutes_with_no_scheduled_time_counts_a_full_day():
    rules = PayRules(late_rule=DEDUCT_MINUTES, work_start=T(7), work_end=T(8), break_minutes=60)

    assert rules.scheduled_minutes == 0
    assert engine.day_units(LATE, rules, T(7, 30)) == 1


# --- daily-rate gross: status × late rule × rate --------------------------------------------


DAILY_GROSS_CASES = [
    # status, late_rule, half, rate, arrival, departure, gross
    (PRESENT, FULL_DAY_FLAGGED, "0.50", 6000, None, None, 6000),
    (ABSENT, FULL_DAY_FLAGGED, "0.50", 6000, None, None, 0),
    (HALF_DAY, FULL_DAY_FLAGGED, "0.50", 3500, None, None, 1750),
    (HALF_DAY, FULL_DAY_FLAGGED, "0.50", 3501, None, None, 1751),  # 1750.5 → half up
    (HALF_DAY, FULL_DAY_FLAGGED, "0.60", 4500, None, None, 2700),
    (LATE, FULL_DAY_FLAGGED, "0.50", 7500, T(7, 40), T(17, 0), 7500),
    # 7500 × (1 − 40/540) = 6944.44… → 6944
    (LATE, DEDUCT_MINUTES, "0.50", 7500, T(7, 40), T(17, 0), 6944),
    # 5400 × (1 − 30/540) = 5100 exactly
    (LATE, DEDUCT_MINUTES, "0.50", 5400, T(7, 30), None, 5100),
    (PRESENT, DEDUCT_MINUTES, "0.50", 5000, T(7, 0), T(17, 0), 5000),
]


@pytest.mark.parametrize(
    ("status", "late_rule", "half", "rate", "arrival", "departure", "gross"), DAILY_GROSS_CASES
)
def test_daily_gross(status, late_rule, half, rate, arrival, departure, gross):
    rules = PayRules(half_day_units=Decimal(half), late_rule=late_rule)

    line = engine.compute_line([day(status, rate, DAILY, arrival, departure)], rules)

    assert line.gross == gross
    assert line.net == gross


# --- hourly gross: status × late rule × times ----------------------------------------------


HOURLY_CASES = [
    # status, late_rule, arrival, departure, expected hours, gross at 900/h
    (PRESENT, FULL_DAY_FLAGGED, T(7, 0), T(16, 0), Fraction(8), 7200),  # 9 h − 1 h break
    (PRESENT, FULL_DAY_FLAGGED, T(7, 0), T(17, 0), Fraction(9), 8100),
    (PRESENT, FULL_DAY_FLAGGED, None, None, Fraction(9), 8100),  # scheduled hours
    (PRESENT, FULL_DAY_FLAGGED, T(7, 0), None, Fraction(9), 8100),
    (ABSENT, FULL_DAY_FLAGGED, T(7, 0), T(17, 0), Fraction(0), 0),
    (HALF_DAY, FULL_DAY_FLAGGED, T(7, 0), T(11, 0), Fraction(4), 3600),  # no break on ½ day
    (HALF_DAY, FULL_DAY_FLAGGED, None, None, Fraction(9, 2), 4050),
    # Late, full day flagged: counted from work_start.
    (LATE, FULL_DAY_FLAGGED, T(7, 30), T(17, 0), Fraction(9), 8100),
    # Late, deduct minutes: counted from the real arrival.
    (LATE, DEDUCT_MINUTES, T(7, 30), T(17, 0), Fraction(17, 2), 7650),
    # Late without departure: scheduled hours × units (1 − 30/540).
    (LATE, DEDUCT_MINUTES, T(7, 30), None, Fraction(17, 2), 7650),
    # Shorter than the break: zero, never negative.
    (PRESENT, FULL_DAY_FLAGGED, T(7, 0), T(7, 30), Fraction(0), 0),
]


@pytest.mark.parametrize(
    ("status", "late_rule", "arrival", "departure", "hours", "gross"), HOURLY_CASES
)
def test_hourly_gross(status, late_rule, arrival, departure, hours, gross):
    rules = PayRules(late_rule=late_rule)

    result = engine.compute_day(day(status, 900, HOURLY, arrival, departure), rules)
    line = engine.compute_line([day(status, 900, HOURLY, arrival, departure)], rules)

    assert result.hours == hours
    assert line.gross == gross
    assert line.hours == engine.to_decimal(hours)


def test_hourly_shift_past_midnight():
    rules = PayRules(overtime_after_hours=None)

    result = engine.compute_day(day(PRESENT, 1000, HOURLY, T(22, 0), T(6, 0)), rules)

    assert result.hours == 7  # 8 h − 1 h break
    assert result.gross == 7000


# --- overtime: threshold × multiplier × pay type -------------------------------------------


OVERTIME_CASES = [
    # pay_type, rate, departure, after_hours, multiplier, overtime hours, gross
    (HOURLY, 900, T(19, 0), "9", "1.5", Fraction(2), 9 * 900 + 2 * 1350),
    (HOURLY, 900, T(19, 0), "9", "2", Fraction(2), 9 * 900 + 2 * 1800),
    (HOURLY, 900, T(19, 0), "9", "1", Fraction(2), 11 * 900),
    (HOURLY, 900, T(19, 0), "8", "1.5", Fraction(3), 8 * 900 + 3 * 1350),
    (HOURLY, 900, T(17, 0), "9", "1.5", Fraction(0), 8100),
    (HOURLY, 900, T(18, 20), "9", "1.5", Fraction(4, 3), 8100 + 1800),
    (HOURLY, 900, T(19, 0), None, "1.5", Fraction(0), 11 * 900),  # overtime switched off
    # Daily rate 5400 over 9 scheduled hours = 600/h equivalent.
    (DAILY, 5400, T(19, 0), "9", "1.5", Fraction(2), 5400 + 2 * 900),
    (DAILY, 5400, T(19, 0), "10", "1.5", Fraction(1), 5400 + 900),
    (DAILY, 5400, T(17, 0), "9", "1.5", Fraction(0), 5400),
    (DAILY, 5400, T(16, 0), "9", "1.5", Fraction(0), 5400),  # left early: still a day
    (DAILY, 5000, T(17, 30), "9", "1.5", Fraction(1, 2), 5000 + 417),  # 416.67 → 417
]


@pytest.mark.parametrize(
    ("pay_type", "rate", "departure", "after", "multiplier", "ot_hours", "gross"),
    OVERTIME_CASES,
)
def test_overtime(pay_type, rate, departure, after, multiplier, ot_hours, gross):
    rules = PayRules(
        overtime_after_hours=None if after is None else Decimal(after),
        overtime_multiplier=Decimal(multiplier),
    )

    line = engine.compute_line([day(PRESENT, rate, pay_type, T(7, 0), departure)], rules)

    assert line.days[0].overtime_hours == ot_hours
    assert line.overtime_hours == engine.to_decimal(ot_hours)
    assert line.gross == gross


def test_daily_worker_without_times_has_no_overtime():
    line = engine.compute_line([day(PRESENT, 5400, DAILY, None, None)], PayRules())

    assert line.overtime_hours == 0
    assert line.days[0].hours is None
    assert line.hours is None


def test_half_day_daily_worker_earns_no_overtime():
    rules = PayRules(overtime_after_hours=Decimal("2"))

    line = engine.compute_line([day(HALF_DAY, 5400, DAILY, T(7, 0), T(13, 0))], rules)

    assert line.gross == 2700


# --- lines: rounding once, rate per date ----------------------------------------------------


def test_line_rounds_once_at_the_end():
    rules = PayRules(late_rule=DEDUCT_MINUTES)
    # Each day: 1000 × (1 − 3/540) = 994.44…; rounding per day would give 3 × 994 = 2982.
    days = [day(LATE, 1000, DAILY, T(7, 3), date=D + datetime.timedelta(days=i)) for i in range(3)]

    line = engine.compute_line(days, rules)

    assert [d.gross for d in line.days] == [994, 994, 994]
    assert line.gross == 2983  # 2983.33… rounded once


def test_each_day_uses_its_own_rate_and_pay_type():
    days = [
        day(PRESENT, 5000, DAILY, date=D),
        day(PRESENT, 6000, DAILY, date=D + datetime.timedelta(days=1)),
        day(PRESENT, 700, HOURLY, T(7, 0), T(16, 0), date=D + datetime.timedelta(days=2)),
        day(HALF_DAY, 6000, DAILY, date=D + datetime.timedelta(days=3)),
    ]

    line = engine.compute_line(days, PayRules())

    assert line.gross == 5000 + 6000 + 8 * 700 + 3000
    assert line.units == Decimal("3.50")
    assert line.hours == Decimal("8.00")


def test_empty_line():
    line = engine.compute_line([], PayRules())

    assert (line.gross, line.net, line.units, line.hours) == (0, 0, Decimal("0.00"), None)


def test_day_snapshot_is_json_friendly():
    result = engine.compute_day(day(LATE, 7500, DAILY, T(7, 40), T(17, 0)), PayRules())

    assert result.as_dict() == {
        "date": "2026-10-30",
        "status": "late",
        "pay_type": "daily",
        "rate": 7500,
        "units": "1.00",
        "hours": "8.33",
        "overtime_hours": "0.00",
        "late_minutes": 40,
        "gross": 7500,
    }


# --- advance deductions -------------------------------------------------------------------


ADVANCE_CASES = [
    # gross, other, advances [(id, balance, per pay)], expected repayments, net
    (6000, 0, [(1, 10000, 2000)], ((1, 2000),), 4000),
    (6000, 0, [(1, 1500, 2000)], ((1, 1500),), 4500),  # capped at the balance
    (1750, 0, [(1, 10000, 2000)], ((1, 1750),), 0),  # capped at gross: net 0
    (6000, 5000, [(1, 10000, 2000)], ((1, 1000),), 0),  # capped at gross − other
    (6000, 7000, [(1, 10000, 2000)], (), 0),  # other deductions alone eat the pay
    (6000, 0, [(1, 0, 2000), (2, 9000, 1500)], ((2, 1500),), 4500),  # repaid one skipped
    (6000, 0, [(1, 1000, 2000), (2, 9000, 1500)], ((1, 1000),), 5000),  # one at a time
    (6000, 0, [(1, 5000, 0)], (), 6000),  # nothing scheduled
    (0, 0, [(1, 5000, 2000)], (), 0),
    (6000, 0, [], (), 6000),
]


@pytest.mark.parametrize(("gross", "other", "advances", "repayments", "net"), ADVANCE_CASES)
def test_advance_deduction(gross, other, advances, repayments, net):
    line = engine.compute_line(
        [day(PRESENT, gross)],
        PayRules(),
        [AdvanceInput(*a) for a in advances],
        other_deductions=other,
    )

    assert line.gross == gross
    assert line.repayments == repayments
    assert line.advance_deduction == sum(amount for _id, amount in repayments)
    assert line.net == net
    assert line.other_deductions == min(other, gross)
    assert line.gross - line.other_deductions - line.advance_deduction == line.net


def test_repeated_deductions_repay_the_advance_to_exactly_zero():
    balance = 5000
    taken = []
    while True:
        line = engine.compute_line(
            [day(PRESENT, 6000)], PayRules(), [AdvanceInput(1, balance, 2000)]
        )
        if not line.repayments:
            break
        taken.append(line.advance_deduction)
        balance -= line.advance_deduction

    assert taken == [2000, 2000, 1000]
    assert balance == 0


# --- pay adjustments (a day corrected after it was paid) ---------------------------------

ADJUSTMENT_CASES = [
    # gross, advances [(id, balance, per pay)], adjustments [(id, left)],
    # expected applied, advance deduction, adjustment, net
    (6000, [], [(1, -6000)], ((1, -6000),), 0, -6000, 0),  # paid a day not worked
    (6000, [], [(1, -2500)], ((1, -2500),), 0, -2500, 3500),
    (3000, [], [(1, -6000)], ((1, -3000),), 0, -3000, 0),  # the rest waits: net ≥ 0
    (6000, [], [(1, 3000)], ((1, 3000),), 0, 3000, 9000),  # underpaid: added
    (0, [], [(1, 3000)], ((1, 3000),), 0, 3000, 3000),  # added even on a day off
    (0, [], [(1, -3000)], (), 0, 0, 0),  # nothing to recover from
    # Recovered after the advance deduction.
    (6000, [(7, 10000, 2000)], [(1, -6000)], ((1, -4000),), 2000, -4000, 0),
    (6000, [(7, 10000, 2000)], [(1, -1000)], ((1, -1000),), 2000, -1000, 3000),
    # A positive adjustment counts for the advance deduction too.
    (0, [(7, 10000, 2000)], [(1, 3000)], ((1, 3000),), 2000, 3000, 1000),
    # Oldest first; a positive one makes room for a negative one.
    (2000, [], [(1, -1500), (2, -1500)], ((1, -1500), (2, -500)), 0, -2000, 0),
    (2000, [], [(1, -3000), (2, 2000)], ((1, -3000), (2, 2000)), 0, -1000, 1000),
    (6000, [], [(1, 0), (2, -1000)], ((2, -1000),), 0, -1000, 5000),  # settled one skipped
    (6000, [], [], (), 0, 0, 6000),
]


@pytest.mark.parametrize(
    ("gross", "advances", "adjustments", "applied", "advance", "adjustment", "net"),
    ADJUSTMENT_CASES,
)
def test_adjustments(gross, advances, adjustments, applied, advance, adjustment, net):
    line = engine.compute_line(
        [day(PRESENT, gross)] if gross else [day(ABSENT, 6000)],
        PayRules(),
        [AdvanceInput(*a) for a in advances],
        adjustments=[AdjustmentInput(*a) for a in adjustments],
    )

    assert line.gross == gross  # earned only: adjustments are apart
    assert line.adjustments == applied
    assert line.advance_deduction == advance
    assert line.adjustment == adjustment == sum(amount for _id, amount in applied)
    assert line.net == net
    assert line.gross - line.other_deductions - line.advance_deduction + line.adjustment == net


@pytest.mark.parametrize(
    ("capacity", "adjustments", "expected"),
    [
        (5000, [(1, -6000)], ((1, -5000),)),
        (-2000, [(1, 3000), (2, -5000)], ((1, 3000), (2, -1000))),  # advance used 2000
        (0, [(1, -1), (2, 1)], ((1, -1), (2, 1))),
        (100, [], ()),
    ],
)
def test_settle_adjustments(capacity, adjustments, expected):
    assert (
        engine.settle_adjustments([AdjustmentInput(*a) for a in adjustments], capacity) == expected
    )


def test_a_large_recovery_is_spread_over_the_next_pays():
    left = -15000
    taken = []
    for gross in (6000, 3000, 0, 6000, 6000):
        line = engine.compute_line(
            [day(PRESENT, gross)] if gross else [],
            PayRules(),
            adjustments=[AdjustmentInput(1, left)],
        )
        taken.append(line.net)
        left -= line.adjustment

    assert taken == [0, 0, 0, 0, 6000]
    assert left == 0


def test_adjustment_amounts_must_be_minor_units():
    with pytest.raises(TypeError):
        AdjustmentInput(1, -6000.0)
    with pytest.raises(TypeError):
        AdjustmentInput(1, True)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"status": "holiday"},
        {"pay_type": "weekly"},
        {"rate": -1},
    ],
)
def test_day_input_rejects_bad_values(kwargs):
    with pytest.raises(ValueError):
        day(**kwargs)


@pytest.mark.parametrize("rate", [6000.0, True, "6000"])
def test_rates_must_be_int_minor_units(rate):
    with pytest.raises(TypeError):
        day(rate=rate)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"late_rule": "ignore"},
        {"half_day_units": Decimal("1.5")},
        {"break_minutes": -5},
        {"overtime_multiplier": Decimal("-1")},
        {"overtime_after_hours": Decimal("-1")},
    ],
)
def test_pay_rules_reject_bad_values(kwargs):
    with pytest.raises(ValueError):
        PayRules(**kwargs)


def test_pay_rules_reject_floats():
    with pytest.raises(TypeError):
        PayRules(half_day_units=0.5)


def test_other_deductions_must_be_minor_units():
    with pytest.raises(TypeError):
        engine.compute_line([], PayRules(), other_deductions=1.5)


# --- pay periods --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("frequency", "kwargs", "on", "expected"),
    [
        ("daily", {}, "2026-10-30", ("2026-10-30", "2026-10-30")),
        # Weekly pay on Saturday (working days Mon–Sat): Sunday → Saturday.
        ("weekly", {"payday": 6}, "2026-10-26", ("2026-10-25", "2026-10-31")),
        ("weekly", {"payday": 6}, "2026-10-31", ("2026-10-25", "2026-10-31")),
        ("weekly", {"payday": 6}, "2026-11-01", ("2026-11-01", "2026-11-07")),
        ("weekly", {"payday": 7}, "2026-10-26", ("2026-10-26", "2026-11-01")),
        ("weekly", {"payday": 5}, "2026-10-31", ("2026-10-31", "2026-11-06")),
        # Fortnights end on Saturdays counted from 6 Jan 2024 (the first after EPOCH).
        ("biweekly", {"payday": 6}, "2026-10-24", ("2026-10-11", "2026-10-24")),
        ("biweekly", {"payday": 6}, "2026-10-26", ("2026-10-25", "2026-11-07")),
        ("biweekly", {"payday": 6}, "2026-11-07", ("2026-10-25", "2026-11-07")),
        ("biweekly", {"payday": 6}, "2026-11-08", ("2026-11-08", "2026-11-21")),
        ("monthly", {}, "2026-02-14", ("2026-02-01", "2026-02-28")),
        ("monthly", {}, "2028-02-29", ("2028-02-01", "2028-02-29")),
        ("monthly", {}, "2026-12-31", ("2026-12-01", "2026-12-31")),
        (
            "every_n_days",
            {"every_n_days": 10, "anchor": datetime.date(2026, 10, 1)},
            "2026-10-25",
            ("2026-10-21", "2026-10-30"),
        ),
        (
            "every_n_days",
            {"every_n_days": 10, "anchor": datetime.date(2026, 10, 1)},
            "2026-09-30",
            ("2026-09-21", "2026-09-30"),
        ),
        ("every_n_days", {"every_n_days": 1}, "2026-10-25", ("2026-10-25", "2026-10-25")),
    ],
)
def test_period_bounds(frequency, kwargs, on, expected):
    start, end = engine.period_bounds(datetime.date.fromisoformat(on), frequency, **kwargs)

    assert (start.isoformat(), end.isoformat()) == expected


@pytest.mark.parametrize(
    ("frequency", "kwargs"),
    [
        ("yearly", {}),
        ("every_n_days", {}),
        ("every_n_days", {"every_n_days": 0}),
        ("weekly", {"payday": 0}),
    ],
)
def test_period_bounds_rejects_bad_settings(frequency, kwargs):
    with pytest.raises(ValueError):
        engine.period_bounds(D, frequency, **kwargs)


@pytest.mark.parametrize(
    ("working_days", "expected"), [([1, 2, 3, 4, 5, 6], 6), ([1, 2, 3, 4, 5], 5), ([], 7)]
)
def test_pay_weekday(working_days, expected):
    assert engine.pay_weekday(working_days) == expected


@pytest.mark.parametrize(
    ("frequency", "kwargs", "length"),
    [
        ("daily", {}, {1}),
        ("weekly", {"payday": 6}, {7}),
        ("weekly", {"payday": 3}, {7}),
        ("biweekly", {"payday": 6}, {14}),
        ("biweekly", {"payday": 6, "anchor": datetime.date(2026, 3, 4)}, {14}),
        ("monthly", {}, {28, 29, 30, 31}),
        ("every_n_days", {"every_n_days": 3}, {3}),
        ("every_n_days", {"every_n_days": 10, "anchor": datetime.date(2026, 3, 1)}, {10}),
    ],
)
def test_periods_tile_the_calendar(frequency, kwargs, length):
    """Every day is in exactly one period; periods follow each other without gaps."""
    day_ = datetime.date(2025, 12, 1)
    start, end = engine.period_bounds(day_, frequency, **kwargs)
    for _ in range(500):
        assert start <= day_ <= end
        assert (end - start).days + 1 in length
        if "payday" in kwargs:
            assert end.isoweekday() == kwargs["payday"]
        day_ += datetime.timedelta(days=1)
        if day_ > end:
            next_start, next_end = engine.period_bounds(day_, frequency, **kwargs)
            assert next_start == end + datetime.timedelta(days=1)
            start, end = next_start, next_end
        else:
            assert engine.period_bounds(day_, frequency, **kwargs) == (start, end)


# --- cash reconciliation ------------------------------------------------------------------


def test_reconcile_matches_the_cash_sheet_artboard():
    lines = [(4700, 4700), (6000, 6000), (1750, 1750), (8000, 0), (4000, 0)]

    sheet = engine.reconcile(30000, lines)

    assert sheet.total_to_pay == 24450
    assert sheet.paid_total == 12450
    assert sheet.left_to_pay == 12000
    assert sheet.cash_in_hand == 17550
    assert sheet.return_amount == 5550
    assert (sheet.line_count, sheet.paid_count, sheet.left_count) == (5, 3, 2)


def test_reconcile_counts_a_zero_net_line_as_paid():
    sheet = engine.reconcile(0, [(0, 0)])

    assert (sheet.line_count, sheet.paid_count) == (1, 1)


def test_reconcile_reports_short_cash_as_a_negative_return():
    sheet = engine.reconcile(5000, [(6000, 0)])

    assert sheet.return_amount == -1000
    assert sheet.cash_in_hand == 5000


def test_reconcile_sets_the_cash_against_the_workers_paid_in_cash():
    """Market Hall: two workers in cash, one by mobile money (already transferred)."""
    lines = [
        engine.SheetAmounts(10000, 10000, 10000, True),
        engine.SheetAmounts(10000, 0, 0, True),
        engine.SheetAmounts(9960, 9960, 0, False),
    ]

    sheet = engine.reconcile(30000, lines)

    assert (sheet.total_to_pay, sheet.paid_total, sheet.left_to_pay) == (29960, 19960, 10000)
    assert (sheet.cash_to_pay, sheet.cash_paid, sheet.cash_left_to_pay) == (20000, 10000, 10000)
    assert sheet.cash_in_hand == 20000  # the transfer took no cash from the foreman
    assert (sheet.return_amount, sheet.short_by) == (10000, 0)


def test_reconcile_counts_a_failed_transfer_paid_in_cash():
    sheet = engine.reconcile(5000, [engine.SheetAmounts(6000, 6000, 6000, True)])

    assert (sheet.cash_to_pay, sheet.cash_in_hand, sheet.short_by) == (6000, -1000, 1000)


def test_reconcile_rejects_floats():
    with pytest.raises(TypeError):
        engine.reconcile(100.0, [])


# --- advances limit -----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("pay_type", "rate", "days", "scheduled", "expected"),
    [
        (DAILY, 6000, 6, 540, 36000),
        (DAILY, 6000, 5, 540, 30000),
        (HOURLY, 900, 6, 540, 48600),
        (HOURLY, 905, 5, 500, 37708),  # 905 × 500/60 × 5 = 37708.33…
    ],
)
def test_expected_weekly_pay(pay_type, rate, days, scheduled, expected):
    assert engine.expected_weekly_pay(pay_type, rate, days, scheduled) == expected


def test_check_advance_matches_the_advances_artboard():
    # Yao already owes 4 000; a new 10 000 at 2 000 per pay; 6 000/day × 6 days; limit 50 %.
    check = engine.check_advance(
        amount=10000,
        deduction_per_pay=2000,
        owed=[(4000, 2000)],
        expected_weekly=36000,
        limit_percent=50,
    )

    assert check.new_total == 14000
    assert check.percent_of_weekly_pay == 39
    assert check.limit_amount == 18000
    assert check.pays_to_repay == 7
    assert check.allowed


@pytest.mark.parametrize(
    ("amount", "owed", "expected_weekly", "limit", "allowed"),
    [
        (18000, [], 36000, 50, True),  # exactly at the limit
        (18001, [], 36000, 50, False),
        (10000, [(8001, 2000)], 36000, 50, False),
        (1, [], 36000, 0, False),  # advances switched off
        (36000, [], 36000, 100, True),
        (1000, [], 0, 50, False),  # no rate, no advance
        (0, [], 36000, 50, False),
    ],
)
def test_check_advance_limit(amount, owed, expected_weekly, limit, allowed):
    check = engine.check_advance(
        amount=amount,
        deduction_per_pay=1000,
        owed=owed,
        expected_weekly=expected_weekly,
        limit_percent=limit,
    )

    assert check.allowed is allowed


def test_check_advance_without_deduction_never_repays():
    check = engine.check_advance(
        amount=1000, deduction_per_pay=0, expected_weekly=36000, limit_percent=50
    )

    assert check.pays_to_repay is None


# --- property tests (seeded loops) --------------------------------------------------------


def _random_time(rng):
    return T(rng.randint(5, 12), rng.choice([0, 5, 15, 30, 45, 59]))


def _random_rules(rng):
    return PayRules(
        half_day_units=Decimal(rng.choice(["0.00", "0.25", "0.50", "0.60", "1.00"])),
        late_rule=rng.choice([FULL_DAY_FLAGGED, DEDUCT_MINUTES]),
        work_start=T(rng.randint(6, 8), rng.choice([0, 30])),
        work_end=T(rng.randint(15, 18), rng.choice([0, 30])),
        break_minutes=rng.choice([0, 30, 45, 60]),
        overtime_after_hours=rng.choice([None, Decimal("8"), Decimal("9"), Decimal("9.5")]),
        overtime_multiplier=Decimal(rng.choice(["1", "1.25", "1.5", "2"])),
    )


def _random_day(rng, i):
    arrival = rng.choice([None, _random_time(rng)])
    departure = rng.choice([None, T(rng.randint(11, 21), rng.choice([0, 10, 30, 55]))])
    return DayInput(
        D + datetime.timedelta(days=i),
        rng.choice([PRESENT, ABSENT, HALF_DAY, LATE]),
        rng.choice([DAILY, HOURLY]),
        rng.choice([0, 1, 333, 900, 1250, 3500, 4999, 6000, 75000]),
        arrival,
        departure,
    )


@pytest.mark.parametrize("seed", range(40))
def test_property_net_is_never_negative_and_lines_add_up(seed):
    rng = random.Random(seed)
    for _ in range(50):
        rules = _random_rules(rng)
        days = [_random_day(rng, i) for i in range(rng.randint(0, 14))]
        advances = [
            AdvanceInput(i, rng.randint(0, 40000), rng.choice([0, 500, 2000, 10000]))
            for i in range(rng.randint(0, 3))
        ]
        other = rng.choice([0, 0, 0, 100, 5000, 100000])

        line = engine.compute_line(days, rules, advances, other_deductions=other)

        assert line.net >= 0
        assert line.gross >= 0
        assert line.gross - line.other_deductions - line.advance_deduction == line.net
        assert line.advance_deduction == sum(amount for _id, amount in line.repayments)
        assert len(line.repayments) <= 1
        for advance_id, amount in line.repayments:
            advance = next(a for a in advances if a.advance_id == advance_id)
            assert 0 < amount <= min(advance.balance, advance.deduction_per_pay)
        # Rounded once: within half a unit of the exact sum.
        exact = sum((d.amount for d in line.days), Fraction(0))
        assert abs(line.gross - exact) <= Fraction(1, 2)
        for result in line.days:
            assert 0 <= result.units <= 1
            assert result.amount >= 0
            assert result.overtime_hours >= 0
            if result.status == ABSENT:
                assert result.amount == 0


@pytest.mark.parametrize("seed", range(20))
def test_property_cash_reconciliation_always_adds_up(seed):
    rng = random.Random(seed)
    for _ in range(100):
        lines = []
        for _line in range(rng.randint(0, 40)):
            net = rng.choice([0, rng.randint(0, 100000)])
            paid = rng.choice([0, net])
            lines.append((net, paid))
        issued = rng.randint(0, 2_000_000)

        sheet = engine.reconcile(issued, lines)

        assert sheet.total_to_pay == sum(net for net, _ in lines)
        assert sheet.paid_total == sum(paid for _, paid in lines)
        assert sheet.left_to_pay == sheet.total_to_pay - sheet.paid_total
        assert sheet.cash_in_hand == sheet.cash_issued - sheet.paid_total
        assert sheet.return_amount == sheet.cash_issued - sheet.total_to_pay
        assert sheet.cash_in_hand - sheet.left_to_pay == sheet.return_amount
        assert sheet.left_to_pay >= 0
        assert 0 <= sheet.paid_count <= sheet.line_count == len(lines)


@pytest.mark.parametrize("seed", range(20))
def test_property_cash_figures_take_only_the_lines_paid_in_cash(seed):
    rng = random.Random(seed)
    for _ in range(100):
        lines = []
        for _line in range(rng.randint(0, 40)):
            net = rng.choice([0, rng.randint(0, 100000)])
            in_cash = rng.random() < 0.6
            paid = rng.choice([0, net])
            lines.append(engine.SheetAmounts(net, paid, paid if in_cash else 0, in_cash))
        issued = rng.randint(0, 2_000_000)

        sheet = engine.reconcile(issued, lines)

        cash = [line for line in lines if line.in_cash]
        assert sheet.total_to_pay == sum(line.net for line in lines)
        assert sheet.left_to_pay == sheet.total_to_pay - sheet.paid_total
        assert sheet.cash_to_pay == sum(line.net for line in cash)
        assert sheet.cash_left_to_pay == sum(line.net - line.paid for line in cash)
        assert sheet.cash_in_hand == issued - sum(line.cash_paid for line in lines)
        assert sheet.return_amount == issued - sheet.cash_to_pay
        assert sheet.cash_in_hand - sheet.cash_left_to_pay == sheet.return_amount
        assert 0 <= sheet.cash_left_to_pay <= sheet.left_to_pay


@pytest.mark.parametrize("seed", range(20))
def test_property_sheet_totals_equal_the_sum_of_lines(seed):
    """Lines computed by the engine feed the cash sheet; its total is their sum."""
    rng = random.Random(seed)
    rules = _random_rules(rng)
    lines = [
        engine.compute_line(
            [_random_day(rng, i) for i in range(rng.randint(1, 6))],
            rules,
            [AdvanceInput(1, rng.randint(0, 20000), 2000)],
        )
        for _ in range(rng.randint(1, 30))
    ]
    paid = [line.net if rng.random() < 0.5 else 0 for line in lines]

    sheet = engine.reconcile(
        1_000_000, [(line.net, p) for line, p in zip(lines, paid, strict=True)]
    )

    assert sheet.total_to_pay == sum(line.net for line in lines)
    assert sum(line.gross for line in lines) == sheet.total_to_pay + sum(
        line.advance_deduction + line.other_deductions for line in lines
    )


@pytest.mark.parametrize("seed", range(10))
def test_property_advances_repay_to_exactly_zero(seed):
    rng = random.Random(seed)
    for _ in range(30):
        amount = rng.randint(1, 50000)
        per_pay = rng.randint(1, 10000)
        balance, pays = amount, 0
        while balance:
            gross = rng.choice([0, 1750, 3500, 6000, 12000])
            line = engine.compute_line(
                [day(PRESENT, gross)], PayRules(), [AdvanceInput(1, balance, per_pay)]
            )
            balance -= line.advance_deduction
            pays += 1
            assert balance >= 0
            assert pays < 10_000
        assert balance == 0


@pytest.mark.parametrize("seed", range(10))
def test_property_more_attendance_never_pays_less(seed):
    """Present ≥ late ≥ 0 and present ≥ half day ≥ absent, for the same worker and day."""
    rng = random.Random(seed)
    for _ in range(100):
        rules = _random_rules(rng)
        rate = rng.choice([500, 3500, 6000])
        pay_type = rng.choice([DAILY, HOURLY])
        arrival = _random_time(rng)

        def gross(status, rate=rate, pay_type=pay_type, arrival=arrival, rules=rules):
            return engine.compute_line([day(status, rate, pay_type, arrival, None)], rules).gross

        assert gross(PRESENT) >= gross(LATE) >= 0
        assert gross(PRESENT) >= gross(HALF_DAY) >= gross(ABSENT) == 0


@pytest.mark.parametrize("seed", range(40))
def test_property_adjustments_settle_exactly_and_net_stays_positive(seed):
    """Over enough pays, what is applied of each adjustment adds up to it exactly; no pay
    goes below zero and positive adjustments are always paid in full."""
    rng = random.Random(seed)
    rules = _random_rules(rng)
    adjustments = {i: rng.choice([-1, 1]) * rng.randint(1, 60000) for i in range(rng.randint(1, 4))}
    left = dict(adjustments)
    applied_total = dict.fromkeys(adjustments, 0)
    advance_balance = rng.choice([0, 20000])
    for pay in range(200):
        days = [_random_day(rng, i) for i in range(rng.randint(0, 3))]
        if pay > 100:  # a worker who keeps working settles everything eventually
            days.append(DayInput(D, PRESENT, DAILY, 6000))
        line = engine.compute_line(
            days,
            rules,
            [AdvanceInput(9, advance_balance, 2000)],
            adjustments=[AdjustmentInput(i, amount) for i, amount in left.items()],
        )

        assert line.net >= 0
        assert line.adjustment == sum(amount for _id, amount in line.adjustments)
        assert (
            line.gross - line.other_deductions - line.advance_deduction + line.adjustment
            == line.net
        )
        for adjustment_id, amount in line.adjustments:
            remaining = left[adjustment_id]
            assert amount != 0
            assert (amount > 0) == (remaining > 0)  # never crosses zero
            assert abs(amount) <= abs(remaining)
            if remaining > 0:
                assert amount == remaining  # positive: in full
            left[adjustment_id] -= amount
            applied_total[adjustment_id] += amount
        advance_balance -= line.advance_deduction
        if not any(left.values()):
            break

    assert applied_total == adjustments
    assert not any(left.values())
