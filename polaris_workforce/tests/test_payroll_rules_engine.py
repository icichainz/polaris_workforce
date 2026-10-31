"""The pay engine's pay rules (Wave 4): multipliers, night hours, stoppage days, piece
work, bonuses and statutory deductions. Table-driven tests for every rule, then seeded
property tests for the rules together (multiplier × deduction × advance × adjustment).

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
    HALF_DAY,
    HOURLY,
    LATE,
    PIECE,
    PRESENT,
    AdjustmentInput,
    AdvanceInput,
    BonusInput,
    DayInput,
    DeductionRuleInput,
    PayRules,
    PieceInput,
    Stoppage,
)

FRI = datetime.date(2026, 10, 30)
SUN = datetime.date(2026, 11, 1)
T = datetime.time


def day(status=PRESENT, rate=6000, pay_type=DAILY, arrival=None, departure=None, date=FRI, **kw):
    return DayInput(date, status, pay_type, rate, arrival, departure, **kw)


def gross(days, rules=None, **kwargs):
    return engine.compute_line(days, rules or PayRules(), **kwargs).gross


def rule(rule_id=1, pct="5", **kwargs):
    kwargs.setdefault("name", f"Rule {rule_id}")
    return DeductionRuleInput(rule_id=rule_id, employee_percent=Decimal(pct), **kwargs)


def piece(qty="1", rate=100, entry_id=1, date=FRI):
    return PieceInput(entry_id, date, Decimal(qty), rate, "Blocks", "block")


# --- day multipliers -----------------------------------------------------------------------


MULTIPLIER_CASES = [
    # date, holiday, sunday ×, holiday ×, the day, expected gross
    (FRI, False, "2", "1.5", day(), 6000),  # a Friday: no multiplier
    (SUN, False, "2", "1.5", day(date=SUN), 12000),
    (FRI, True, "2", "1.5", day(holiday=True), 9000),
    (SUN, True, "2", "1.5", day(date=SUN, holiday=True), 12000),  # larger one, not both
    (SUN, True, "1.25", "3", day(date=SUN, holiday=True), 18000),
    (SUN, False, "1", "1", day(date=SUN), 6000),  # neutral rules
    (SUN, False, "2", "1", day(HALF_DAY, date=SUN), 6000),  # ½ day × 2
    (SUN, False, "2", "1", day(ABSENT, date=SUN), 0),
    # hourly 750, 07:00–18:00: 9 h + 1 h overtime × 1.5 = 7875, × 2
    (SUN, False, "2", "1", day(rate=750, pay_type=HOURLY, arrival=T(7), departure=T(18),
                               date=SUN), 15750),
    # daily 4999 × 1.5 = 7498.5 → 7499 (rounded once, half up)
    (SUN, False, "1.5", "1", day(rate=4999, date=SUN), 7499),
]  # fmt: skip


@pytest.mark.parametrize(
    ("date", "holiday", "sunday_x", "holiday_x", "the_day", "expected"), MULTIPLIER_CASES
)
def test_day_multipliers(date, holiday, sunday_x, holiday_x, the_day, expected):  # noqa: PLR0913
    rules = PayRules(sunday_multiplier=Decimal(sunday_x), holiday_multiplier=Decimal(holiday_x))
    assert the_day.date == date and the_day.holiday == holiday
    assert gross([the_day], rules) == expected


def test_multiplier_is_in_the_day_snapshot_only_when_it_applies():
    rules = PayRules(sunday_multiplier=Decimal("2"))
    sunday = engine.compute_day(day(date=SUN), rules).as_dict()
    friday = engine.compute_day(day(), rules).as_dict()

    assert (sunday["multiplier"], sunday["day_kind"]) == ("2.00", engine.SUNDAY)
    assert "multiplier" not in friday and "night_hours" not in friday and "stoppage" not in friday


@pytest.mark.parametrize("value", ["0.5", "0", "-1"])
def test_multipliers_below_one_are_refused(value):
    for name in ("sunday_multiplier", "holiday_multiplier", "night_multiplier"):
        with pytest.raises(ValueError, match=name):
            PayRules(**{name: Decimal(value)})


def test_multipliers_refuse_floats():
    with pytest.raises(TypeError):
        PayRules(sunday_multiplier=2.0)


# --- night hours ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("arrival", "departure", "start", "end", "minutes"),
    [
        (T(22), T(6), T(22), T(6), 480),
        (T(20), T(23), T(22), T(6), 60),
        (T(5), T(8), T(22), T(6), 60),
        (T(7), T(17), T(22), T(6), 0),
        (T(23), T(1), T(0), T(5), 60),
        (T(11), T(13), T(12), T(14), 60),
        (T(18), T(2), T(22), T(6), 240),
        (T(4), T(23), T(22), T(6), 120 + 60),  # morning and evening ends of the window
        (T(13), T(15), T(12), T(14), 60),
    ],
)
def test_night_minutes(arrival, departure, start, end, minutes):
    assert engine.night_minutes(arrival, departure, start, end) == minutes


NIGHT = {"night_start": T(22), "night_end": T(6)}

NIGHT_CASES = [
    # pay type, rate, arrival, departure, night ×, night hours, gross
    # hourly 600, 18:00–02:00: 8 h − 1 h break = 7 h; 4 h at night × 1.5 → + 1200
    (HOURLY, 600, T(18), T(2), "1.5", Fraction(4), 4200 + 1200),
    # daily 6000 (9 scheduled h → 666.67/h), 14:00–23:00: 8 h, 1 h at night × 2
    (DAILY, 6000, T(14), T(23), "2", Fraction(1), 6667),
    (HOURLY, 600, T(7), T(17), "1.5", Fraction(0), 5400),  # day shift: no premium
    (HOURLY, 600, T(18), T(2), "1", Fraction(4), 4200),  # × 1: no premium
    # whole night 22:00–06:00: 8 h worked − 1 h break = 7 h paid; night hours capped at 7
    (HOURLY, 600, T(22), T(6), "2", Fraction(7), 4200 + 4200),
    # with overtime: hourly 600, 13:00–02:00 = 13 h − 1 = 12 h: 9 + 3 OT × 1.5, night 4 h × 0.5
    (HOURLY, 600, T(13), T(2), "1.5", Fraction(4), 5400 + 2700 + 1200),
]  # fmt: skip


@pytest.mark.parametrize(
    ("pay_type", "rate", "arrival", "departure", "night_x", "hours", "expected"), NIGHT_CASES
)
def test_night_premium(pay_type, rate, arrival, departure, night_x, hours, expected):  # noqa: PLR0913
    rules = PayRules(night_multiplier=Decimal(night_x), **NIGHT)
    result = engine.compute_day(day(PRESENT, rate, pay_type, arrival, departure), rules)

    assert result.night_hours == hours
    assert engine.round_half_up(result.amount) == expected


def test_night_premium_needs_both_times():
    rules = PayRules(night_multiplier=Decimal("2"), **NIGHT)
    assert gross([day(PRESENT, 600, HOURLY, T(18), None)], rules) == 600 * 9


def test_night_and_sunday_multiply_together():
    rules = PayRules(night_multiplier=Decimal("1.5"), sunday_multiplier=Decimal("2"), **NIGHT)
    assert gross([day(PRESENT, 600, HOURLY, T(18), T(2), date=SUN)], rules) == 2 * 5400


@pytest.mark.parametrize(
    "kwargs",
    [{"night_start": T(22)}, {"night_end": T(6)}, {"night_start": T(22), "night_end": T(22)}],
)
def test_night_window_needs_two_different_times(kwargs):
    with pytest.raises(ValueError, match="night"):
        PayRules(**kwargs)


# --- stoppage days -------------------------------------------------------------------------


HALF = Stoppage(percent=Decimal("50"))
FIXED = Stoppage(fixed=2000)

STOPPAGE_CASES = [
    # the day, expected gross
    (day(stoppage=HALF), 3000),
    (day(HALF_DAY, stoppage=HALF), 1500),
    (day(LATE, arrival=T(9), stoppage=HALF), 3000),
    (day(ABSENT, stoppage=HALF), 0),
    # 9 scheduled h × 750 × 50 %
    (day(rate=750, pay_type=HOURLY, arrival=T(7), departure=T(19), stoppage=HALF), 3375),
    (day(stoppage=FIXED), 2000),
    (day(HALF_DAY, stoppage=FIXED), 1000),
    (day(pay_type=PIECE, stoppage=HALF), 0),
    (day(pay_type=PIECE, stoppage=FIXED), 2000),
    (day(stoppage=Stoppage(percent=Decimal("0"))), 0),
    (day(stoppage=Stoppage(percent=Decimal("100"))), 6000),
    (day(rate=4999, stoppage=Stoppage(percent=Decimal("33.33"))), 1666),  # 1666.17
    (day(date=SUN, holiday=True, stoppage=HALF), 3000),  # no multiplier on stoppage pay
    (day(arrival=T(7), departure=T(22), stoppage=HALF), 3000),  # no overtime either
]  # fmt: skip


@pytest.mark.parametrize(("the_day", "expected"), STOPPAGE_CASES)
def test_stoppage_day(the_day, expected):
    rules = PayRules(sunday_multiplier=Decimal("2"), holiday_multiplier=Decimal("2"), **NIGHT)
    result = engine.compute_day(the_day, rules)

    assert engine.round_half_up(result.amount) == expected
    assert result.overtime_hours == 0 and result.night_hours == 0 and result.multiplier == 1
    assert result.as_dict().get("stoppage", False) is (the_day.status != ABSENT)


@pytest.mark.parametrize(
    "kwargs",
    [{}, {"percent": Decimal("50"), "fixed": 100}, {"percent": Decimal("101")}, {"fixed": -1}],
)
def test_stoppage_needs_one_valid_pay(kwargs):
    with pytest.raises((ValueError, TypeError)):
        Stoppage(**kwargs)


# --- piece work ----------------------------------------------------------------------------


PIECE_CASES = [
    # days, pieces, time pay, piece amount
    ([day(pay_type=PIECE)], [], 0, 0),
    ([day(pay_type=PIECE)], [piece("12.5", 400)], 0, 5000),
    ([day()], [piece("3", 333)], 6000, 999),  # piece work on top of a day rate
    ([day()], [piece("0.5", 1, 1), piece("0.5", 1, 2)], 6000, 1),  # rounded once: 1, not 2
    ([day(ABSENT)], [piece("2", 500)], 0, 1000),  # output is paid, the day is not
    ([day(pay_type=PIECE, date=SUN)], [piece("1", 500)], 0, 500),  # no multiplier on pieces
    ([], [piece("1.25", 1001)], 0, 1251),  # 1251.25
]


@pytest.mark.parametrize(("days", "pieces", "time_pay", "piece_amount"), PIECE_CASES)
def test_piece_work(days, pieces, time_pay, piece_amount):
    rules = PayRules(sunday_multiplier=Decimal("2"))
    line = engine.compute_line(days, rules, pieces=pieces)

    assert (line.time_pay, line.piece_amount) == (time_pay, piece_amount)
    assert line.gross == line.net == time_pay + piece_amount
    assert [p.as_dict()["amount"] for p in line.pieces] == [
        engine.round_half_up(p.amount) for p in pieces
    ]


def test_piece_rate_days_still_count_as_days_worked():
    line = engine.compute_line([day(pay_type=PIECE), day(HALF_DAY, pay_type=PIECE)], PayRules())
    assert line.units == Decimal("1.50")
    assert line.gross == 0


@pytest.mark.parametrize("kwargs", [{"quantity": Decimal("-1")}, {"unit_rate": 1.5}])
def test_piece_input_rejects_bad_values(kwargs):
    values = {"entry_id": 1, "date": FRI, "quantity": Decimal("1"), "unit_rate": 100, **kwargs}
    with pytest.raises((ValueError, TypeError)):
        PieceInput(**values)


def test_expected_weekly_pay_of_a_piece_rate_worker_uses_the_reference_rate():
    assert engine.expected_weekly_pay(PIECE, 5000, 6, 540) == 30000


# --- bonuses -------------------------------------------------------------------------------


def test_bonuses_are_added_to_the_gross():
    line = engine.compute_line(
        [day()], PayRules(), bonuses=[BonusInput(1, 2000), BonusInput(2, 500)]
    )
    assert (line.time_pay, line.bonus_amount, line.gross, line.net) == (6000, 2500, 8500, 8500)
    assert line.bonuses == ((1, 2000), (2, 500))


def test_bonus_amounts_must_be_minor_units():
    with pytest.raises(TypeError):
        BonusInput(1, 10.0)
    with pytest.raises(ValueError):
        BonusInput(1, -5)


# --- statutory deductions ------------------------------------------------------------------


DEDUCTION_CASES = [
    # gross, rules, pay type, employee amounts, employer total
    (10000, [rule(pct="5")], DAILY, [500], 0),
    (10000, [rule(pct="5", employer_percent=Decimal("10"))], DAILY, [500], 1000),
    (10000, [rule(pct="5", floor=12000)], DAILY, [], 0),  # under the floor: nothing
    (12000, [rule(pct="5", floor=12000)], DAILY, [600], 0),  # at the floor: the gross
    (10000, [rule(pct="5", ceiling=8000)], DAILY, [400], 0),
    (10000, [rule(pct="5", pay_types=(HOURLY,))], DAILY, [], 0),
    (10000, [rule(pct="5", pay_types=(HOURLY, DAILY))], DAILY, [500], 0),
    (10000, [rule(pct="5", pay_types=(PIECE,))], PIECE, [500], 0),
    (10000, [rule(1, "60"), rule(2, "60")], DAILY, [6000, 4000], 0),  # capped: net ≥ 0
    (1001, [rule(pct="3.5")], DAILY, [35], 0),  # 35.035
    (50, [rule(pct="1")], DAILY, [1], 0),  # 0.5 → 1, half up
    (0, [rule(pct="5", employer_percent=Decimal("5"))], DAILY, [], 0),
    (10000, [rule(pct="0", employer_percent=Decimal("16.5"))], DAILY, [0], 1650),
    (10000, [rule(1, "5", order=2), rule(2, "96", order=1)], DAILY, [9600, 400], 0),  # by order
]


@pytest.mark.parametrize(
    ("gross_amount", "rules", "pay_type", "employee", "employer"), DEDUCTION_CASES
)
def test_statutory_deductions(gross_amount, rules, pay_type, employee, employer):
    results = engine.statutory_deductions(gross_amount, rules, pay_day=FRI, pay_type=pay_type)

    assert [r.employee for r in results] == employee
    assert sum(r.employer for r in results) == employer


@pytest.mark.parametrize(
    ("active_from", "active_to", "applies"),
    [
        (None, None, True),
        (FRI, None, True),
        (FRI + datetime.timedelta(days=1), None, False),
        (None, FRI, True),
        (None, FRI - datetime.timedelta(days=1), False),
        (FRI - datetime.timedelta(days=30), FRI + datetime.timedelta(days=30), True),
    ],
)
def test_deduction_rules_apply_from_to_the_pay_day(active_from, active_to, applies):
    rules = [rule(active_from=active_from, active_to=active_to)]
    line = engine.compute_line([day()], PayRules(), deduction_rules=rules, pay_day=FRI)
    assert line.statutory == (300 if applies else 0)


def test_the_line_pay_type_is_that_of_its_last_day():
    days = [day(pay_type=HOURLY, rate=600, date=FRI), day(pay_type=DAILY, date=SUN)]
    hourly_only = [rule(pay_types=(HOURLY,))]

    assert engine.line_pay_type(days) == DAILY
    assert engine.compute_line(days, PayRules(), deduction_rules=hourly_only).statutory == 0
    assert engine.line_pay_type([]) == DAILY


def test_deductions_include_piece_work_and_bonuses_in_the_base():
    line = engine.compute_line(
        [day()],
        PayRules(),
        pieces=[piece("10", 100)],
        bonuses=[BonusInput(1, 3000)],
        deduction_rules=[rule(pct="10", employer_percent=Decimal("20"))],
    )
    assert line.gross == 10000
    assert (line.statutory, line.employer, line.net) == (1000, 2000, 9000)
    assert line.deductions[0].as_dict() == {
        "rule_id": 1,
        "name": "Rule 1",
        "base": 10000,
        "employee_percent": "10",
        "employee": 1000,
        "employer_percent": "20",
        "employer": 2000,
    }


@pytest.mark.parametrize(
    "kwargs",
    [
        {"employee_percent": Decimal("101")},
        {"employer_percent": Decimal("-1")},
        {"floor": 100, "ceiling": 50},
        {"floor": 1.5},
        {"pay_types": ("weekly",)},
        {"employee_percent": 5.0},
    ],
)
def test_deduction_rules_reject_bad_values(kwargs):
    values = {"rule_id": 1, "name": "x", "employee_percent": Decimal("5"), **kwargs}
    with pytest.raises((ValueError, TypeError)):
        DeductionRuleInput(**values)


COMBINED_CASES = [
    # gross 6000; rule %, advance (balance, per pay), adjustments →
    # statutory, advance, adjustment, net
    ("10", (5000, 2000), [], 600, 2000, 0, 3400),
    ("10", (5000, 2000), [-5000], 600, 2000, -3400, 0),
    ("10", (5000, 6000), [], 600, 5000, 0, 400),
    ("50", (10000, 10000), [1000], 3000, 4000, 1000, 0),  # + 1000 helps repay the advance
    ("100", (5000, 2000), [], 6000, 0, 0, 0),
    ("100", (5000, 2000), [3000], 6000, 2000, 3000, 1000),  # adjustments are never deducted
    ("0", (0, 0), [-100, 200], 0, 0, 100, 6100),
]


@pytest.mark.parametrize(
    ("pct", "advance", "adjustments", "statutory", "repaid", "adjustment", "net"), COMBINED_CASES
)
def test_deductions_come_before_advances_and_adjustments(  # noqa: PLR0913
    pct, advance, adjustments, statutory, repaid, adjustment, net
):
    line = engine.compute_line(
        [day()],
        PayRules(),
        [AdvanceInput(1, *advance)],
        adjustments=[AdjustmentInput(i, a) for i, a in enumerate(adjustments)],
        deduction_rules=[rule(pct=pct)],
    )
    assert (line.statutory, line.advance_deduction, line.adjustment, line.net) == (
        statutory,
        repaid,
        adjustment,
        net,
    )


def test_a_multiplied_sunday_is_taxed_on_the_multiplied_gross():
    rules = PayRules(sunday_multiplier=Decimal("2"))
    line = engine.compute_line(
        [day(date=SUN)], rules, [AdvanceInput(1, 1000, 1000)], deduction_rules=[rule(pct="5")]
    )
    assert (line.gross, line.statutory, line.advance_deduction, line.net) == (
        12000,
        600,
        1000,
        10400,
    )


# --- property tests ------------------------------------------------------------------------


def _random_rules(rng):
    night = rng.choice([None, (T(22), T(6)), (T(20), T(4)), (T(0), T(5))])
    return PayRules(
        half_day_units=Decimal(rng.choice(["0.00", "0.50", "0.60", "1.00"])),
        late_rule=rng.choice([engine.FULL_DAY_FLAGGED, engine.DEDUCT_MINUTES]),
        work_start=T(rng.randint(6, 8)),
        work_end=T(rng.randint(15, 18)),
        break_minutes=rng.choice([0, 30, 60]),
        overtime_after_hours=rng.choice([None, Decimal("8"), Decimal("9")]),
        overtime_multiplier=Decimal(rng.choice(["1", "1.5", "2"])),
        sunday_multiplier=Decimal(rng.choice(["1", "1.5", "2", "3"])),
        holiday_multiplier=Decimal(rng.choice(["1", "1.25", "2"])),
        night_start=night[0] if night else None,
        night_end=night[1] if night else None,
        night_multiplier=Decimal(rng.choice(["1", "1.25", "1.5"])),
    )


def _random_day(rng, i):
    arrival = rng.choice([None, T(rng.randint(5, 23), rng.choice([0, 15, 30]))])
    departure = rng.choice([None, T(rng.randint(0, 23), rng.choice([0, 10, 45]))])
    stoppage = rng.choice(
        [
            None,
            None,
            None,
            Stoppage(percent=Decimal(rng.choice(["0", "50", "100"]))),
            Stoppage(fixed=rng.choice([0, 1500])),
        ]
    )
    return DayInput(
        FRI + datetime.timedelta(days=i),
        rng.choice([PRESENT, ABSENT, HALF_DAY, LATE]),
        rng.choice([DAILY, HOURLY, PIECE]),
        rng.choice([0, 1, 333, 750, 4999, 6000]),
        arrival,
        departure,
        holiday=rng.random() < 0.2,
        stoppage=stoppage,
    )


def _random_deduction_rules(rng):
    return [
        DeductionRuleInput(
            rule_id=i,
            name=f"R{i}",
            employee_percent=Decimal(rng.choice(["0", "2.5", "5", "10", "33.3", "60", "100"])),
            employer_percent=Decimal(rng.choice(["0", "7", "16.5"])),
            floor=rng.choice([None, 0, 3000, 20000]),
            ceiling=rng.choice([None, 20000, 50000]),
            pay_types=rng.choice([(), (DAILY,), (HOURLY, PIECE)]),
            active_from=rng.choice([None, FRI, FRI + datetime.timedelta(days=30)]),
            active_to=rng.choice([None, FRI + datetime.timedelta(days=60)]),
            order=rng.randint(0, 3),
        )
        for i in range(rng.randint(0, 3))
    ]


@pytest.mark.parametrize("seed", range(40))
def test_property_every_rule_together_keeps_the_line_exact_and_positive(seed):
    rng = random.Random(seed)
    for _ in range(40):
        rules = _random_rules(rng)
        days = [_random_day(rng, i) for i in range(rng.randint(0, 10))]
        pieces = [
            piece(rng.choice(["0.5", "1", "12.25", "100"]), rng.choice([1, 50, 333]), i)
            for i in range(rng.randint(0, 3))
        ]
        bonuses = [BonusInput(i, rng.choice([0, 500, 2000])) for i in range(rng.randint(0, 2))]
        deduction_rules = _random_deduction_rules(rng)
        advances = [AdvanceInput(1, rng.randint(0, 40000), rng.choice([0, 1000, 10000]))]
        adjustments = [
            AdjustmentInput(i, rng.choice([-1, 1]) * rng.randint(1, 20000))
            for i in range(rng.randint(0, 2))
        ]
        other = rng.choice([0, 0, 500])
        pay_day = FRI + datetime.timedelta(days=rng.randint(0, 90))

        line = engine.compute_line(
            days,
            rules,
            advances,
            other_deductions=other,
            adjustments=adjustments,
            pieces=pieces,
            bonuses=bonuses,
            deduction_rules=deduction_rules,
            pay_day=pay_day,
        )

        assert line.net >= 0
        assert line.gross == line.time_pay + line.piece_amount + line.bonus_amount >= 0
        assert line.statutory == sum(d.employee for d in line.deductions)
        assert line.employer == sum(d.employer for d in line.deductions)
        assert line.statutory <= line.gross - line.other_deductions
        assert (
            line.net
            == line.gross
            - line.other_deductions
            - line.statutory
            - line.advance_deduction
            + line.adjustment
        )
        applying = {
            r.rule_id for r in deduction_rules if r.applies(pay_day, engine.line_pay_type(days))
        }
        for result in line.deductions:
            source = next(r for r in deduction_rules if r.rule_id == result.rule_id)
            assert result.rule_id in applying
            assert 0 <= result.base <= line.gross
            assert result.employee <= engine.round_half_up(
                result.base * Fraction(source.employee_percent) / 100
            )
            assert result.employer == engine.round_half_up(
                result.base * Fraction(source.employer_percent) / 100
            )
        # Rounded once per part: within half a unit of the exact amounts.
        exact = sum((d.amount for d in line.days), Fraction(0))
        assert abs(line.time_pay - exact) <= Fraction(1, 2)
        for result in line.days:
            assert result.amount >= 0
            if result.status == ABSENT:
                assert result.amount == 0


@pytest.mark.parametrize("seed", range(20))
def test_property_deductions_are_exact_when_they_fit(seed):
    """Rules adding up to 100 % or less are never capped: each is its percentage."""
    rng = random.Random(seed)
    for _ in range(100):
        gross_amount = rng.randint(0, 500000)
        shares = [Decimal(rng.randint(0, 3000)) / 100 for _ in range(rng.randint(1, 3))]
        rules = [rule(i, str(s)) for i, s in enumerate(shares)]
        results = engine.statutory_deductions(gross_amount, rules)
        expected = [engine.round_half_up(gross_amount * Fraction(s) / 100) for s in shares]
        assert sum(r.employee for r in results) == sum(expected)
        assert sum(r.employee for r in results) <= gross_amount


@pytest.mark.parametrize("seed", range(20))
def test_property_a_higher_multiplier_never_pays_less(seed):
    rng = random.Random(seed)
    for _ in range(50):
        rules = _random_rules(rng)
        the_day = _random_day(rng, 2)  # FRI + 2 = a Sunday
        low = engine.compute_day(the_day, rules).amount
        for name in ("sunday_multiplier", "holiday_multiplier", "night_multiplier"):
            higher = PayRules(
                **{
                    **{f: getattr(rules, f) for f in rules.__dataclass_fields__},
                    name: getattr(rules, name) + 1,
                }
            )
            assert engine.compute_day(the_day, higher).amount >= low


@pytest.mark.parametrize("seed", range(20))
def test_property_a_sunday_is_the_weekday_times_the_multiplier(seed):
    rng = random.Random(seed)
    for _ in range(50):
        rules = _random_rules(rng)
        weekday = _random_day(rng, 0)  # a Friday
        if weekday.holiday or weekday.stoppage:
            continue
        sunday = DayInput(
            SUN,
            *[getattr(weekday, f) for f in ("status", "pay_type", "rate", "arrival", "departure")],
        )
        friday = engine.compute_day(weekday, rules).amount
        assert engine.compute_day(sunday, rules).amount == friday * Fraction(
            rules.sunday_multiplier
        )


@pytest.mark.parametrize("seed", range(20))
def test_property_stoppage_pays_at_most_the_day(seed):
    """A stoppage share of 100 % or less never pays more than a normal day's base."""
    rng = random.Random(seed)
    rules = PayRules()
    for _ in range(100):
        rate = rng.choice([0, 333, 6000])
        status = rng.choice([PRESENT, HALF_DAY, LATE, ABSENT])
        percent = Decimal(rng.randint(0, 100))
        stopped = engine.compute_day(day(status, rate, stoppage=Stoppage(percent=percent)), rules)
        normal = engine.compute_day(day(status, rate), rules)
        assert 0 <= stopped.amount <= normal.amount
