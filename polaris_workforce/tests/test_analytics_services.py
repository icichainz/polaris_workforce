"""Analytics services: budgets, cost breakdown, comparison, cash, ages and checks."""

import datetime
import uuid
from decimal import Decimal

import pytest
from django.utils import timezone

from apps.analytics import services as analytics
from apps.analytics.models import AnalyticsSettings
from apps.analytics.services import BudgetStatus, Change, Mismatch
from apps.attendance.models import AttendanceDay, AttendanceRecord
from apps.checkin.models import CheckIn
from apps.core.choices import PaymentMethod
from apps.core.models import AuditLog
from apps.dashboard.reports import build_report
from apps.payouts.models import PayoutAccount, TransferMethod
from apps.payroll.models import PayPeriod
from apps.workers.models import Worker
from apps.worksites.models import Site

from . import factories as f

pytestmark = pytest.mark.django_db

TODAY = datetime.date(2026, 6, 30)
P, A, L = (
    AttendanceRecord.Status.PRESENT,
    AttendanceRecord.Status.ABSENT,
    AttendanceRecord.Status.LATE,
)


def pay(site, date, gross, *, worker=None, status=PayPeriod.Status.PAID, **line):
    """A pay line on the site's one-day pay period of ``date``."""
    period = PayPeriod.objects.filter(site=site, start_date=date, end_date=date).first()
    period = period or f.PayPeriodFactory(site=site, start_date=date, end_date=date, status=status)
    return f.PayLineFactory(period=period, worker=worker or f.WorkerFactory(), gross=gross, **line)


def daily_pay(site, last: datetime.date, days: int, gross: int):
    worker = f.WorkerFactory()
    for back in range(days):
        pay(site, last - datetime.timedelta(days=back), gross, worker=worker)


# --- budgets ------------------------------------------------------------------------------


def budget_of(site, today=TODAY):
    return analytics.site_budgets([site], today=today)[0]


def test_budget_on_track_projects_when_it_runs_out():
    site = f.SiteFactory(labour_budget=1_000_000, planned_end_date=datetime.date(2026, 8, 31))
    daily_pay(site, TODAY, 28, 10_000)  # 280 000 spent, 70 000 a week

    item = budget_of(site)

    assert (item.spent, item.daily_rate, item.used_percent) == (280_000, 10_000, 28)
    assert item.weekly_spend == 70_000
    # 720 000 left at 10 000 a day: 72 days.
    assert item.runs_out_on == TODAY + datetime.timedelta(days=72)
    assert item.status == BudgetStatus.ON_TRACK
    assert item.days_before_end < 0  # runs out after the planned end


def test_budget_at_risk_when_it_runs_out_before_the_planned_end():
    site = f.SiteFactory(labour_budget=1_000_000, planned_end_date=datetime.date(2026, 12, 31))
    daily_pay(site, TODAY, 28, 10_000)

    item = budget_of(site)

    assert item.status == BudgetStatus.AT_RISK
    assert item.days_before_end == (datetime.date(2026, 12, 31) - item.runs_out_on).days > 0


def test_budget_at_risk_past_the_warning_percent_from_settings():
    site = f.SiteFactory(labour_budget=300_000, planned_end_date=None)
    daily_pay(site, TODAY, 28, 10_000)  # 93 %

    assert budget_of(site).status == BudgetStatus.AT_RISK
    AnalyticsSettings.objects.create(budget_warn_percent=95)
    assert budget_of(site).status == BudgetStatus.ON_TRACK


def test_budget_over_and_corrections_count():
    site = f.SiteFactory(labour_budget=100_000)
    pay(site, TODAY - datetime.timedelta(days=40), 90_000)
    pay(site, TODAY - datetime.timedelta(days=3), 5_000, adjustment=6_000)

    item = budget_of(site)

    assert (item.spent, item.used_percent, item.status) == (101_000, 101, BudgetStatus.OVER)
    assert item.bar_percent == 100


def test_budget_needs_two_weeks_of_pay_to_project():
    site = f.SiteFactory(labour_budget=10_000_000)
    daily_pay(site, TODAY, 10, 10_000)

    item = budget_of(site)

    assert item.status == BudgetStatus.NOT_ENOUGH_DATA
    assert (item.weekly_spend, item.runs_out_on, item.used_percent) == (None, None, 1)


def test_budget_ignores_future_pay_and_paused_sites_are_not_projected():
    site = f.SiteFactory(labour_budget=10_000_000, status=Site.Status.PAUSED)
    daily_pay(site, TODAY, 28, 10_000)
    pay(site, TODAY + datetime.timedelta(days=1), 99_000)

    item = budget_of(site)

    assert item.spent == 280_000
    assert item.runs_out_on is None
    assert item.status == BudgetStatus.NOT_ENOUGH_DATA


def test_budgets_without_a_budget_and_the_order():
    no_budget = f.SiteFactory(name="No budget", labour_budget=None)
    pay(no_budget, TODAY, 5_000)
    idle = f.SiteFactory(name="Idle", labour_budget=None)
    over = f.SiteFactory(name="Over", labour_budget=1_000)
    pay(over, TODAY, 5_000)

    items = analytics.site_budgets([no_budget, idle, over], today=TODAY, with_spend_only=True)

    assert [(i.name, i.status) for i in items] == [
        ("Over", BudgetStatus.OVER),
        ("No budget", BudgetStatus.NO_BUDGET),
    ]
    assert items[1].used_percent is None


def test_budgets_take_ids_and_a_fixed_number_of_queries(django_assert_num_queries):
    sites = [f.SiteFactory(labour_budget=100_000) for _ in range(4)]
    for site in sites:
        daily_pay(site, TODAY, 3, 1_000)
    settings = AnalyticsSettings.load()

    with django_assert_num_queries(3):
        items = analytics.site_budgets([s.pk for s in sites], today=TODAY, settings=settings)
    with django_assert_num_queries(2):
        analytics.site_budgets(sites, today=TODAY, settings=settings)

    assert {item.spent for item in items} == {3_000}


def monthly(site, start: datetime.date, end: datetime.date, gross: int):
    period = f.PayPeriodFactory(site=site, start_date=start, end_date=end, frequency="monthly")
    return f.PayLineFactory(period=period, worker=f.WorkerFactory(), gross=gross)


def test_a_first_weekly_period_is_spread_over_its_days():
    site = f.SiteFactory(labour_budget=10_000_000)
    worker = f.WorkerFactory()
    for end in (datetime.date(2026, 6, 16), datetime.date(2026, 6, 23), TODAY):
        period = f.PayPeriodFactory(
            site=site, start_date=end - datetime.timedelta(days=6), end_date=end, frequency="weekly"
        )
        f.PayLineFactory(period=period, worker=worker, gross=7_000)

    item = budget_of(site)

    # Three weeks of 7 000 over the 21 days they cover, not over the 15 days since the
    # first pay day.
    assert (item.daily_rate, item.weekly_spend) == (1_000, 7_000)


def test_a_monthly_site_keeps_its_projection_at_the_end_of_the_month():
    site = f.SiteFactory(labour_budget=1_000_000, planned_end_date=datetime.date(2026, 12, 31))
    monthly(site, datetime.date(2026, 4, 1), datetime.date(2026, 4, 30), 300_000)
    monthly(site, datetime.date(2026, 5, 1), datetime.date(2026, 5, 31), 310_000)

    # 29 June: no month end in the last 28 days; May is still the latest month.
    item = budget_of(site, today=datetime.date(2026, 6, 29))

    assert item.daily_rate == 10_000
    assert item.runs_out_on == datetime.date(2026, 6, 29) + datetime.timedelta(days=39)
    assert item.status == BudgetStatus.AT_RISK


def test_the_warning_compares_the_exact_share_not_a_rounded_one():
    site = f.SiteFactory(labour_budget=1_000)
    pay(site, TODAY, 895)  # 89.5 %
    nearly = f.SiteFactory(labour_budget=10_000)
    pay(nearly, TODAY, 9_996)  # 99.96 %

    assert budget_of(site).status == BudgetStatus.NOT_ENOUGH_DATA  # under 90 %
    item = budget_of(nearly)
    assert (item.used_percent, item.status) == (99, BudgetStatus.AT_RISK)


def test_an_active_site_past_its_planned_end_is_at_risk():
    site = f.SiteFactory(
        labour_budget=10_000_000, planned_end_date=TODAY - datetime.timedelta(days=1)
    )
    daily_pay(site, TODAY, 28, 1_000)

    assert budget_of(site).status == BudgetStatus.AT_RISK
    site.status = Site.Status.COMPLETED
    site.save()
    assert budget_of(site).status == BudgetStatus.NOT_ENOUGH_DATA


def test_settings_load_never_writes():
    settings = AnalyticsSettings.load()

    assert settings.pk is None and settings.budget_warn_percent == 90
    assert not AnalyticsSettings.objects.exists()


def test_save_settings_is_audited():
    admin = f.AdminUserFactory()
    settings = AnalyticsSettings.load()
    settings.budget_warn_percent = 75

    analytics.save_settings(settings, actor=admin)

    entry = AuditLog.objects.get(action="analytics.settings.update")
    assert (entry.actor, entry.before, entry.after) == (admin, None, {"budget_warn_percent": 75})
    assert AnalyticsSettings.load().budget_warn_percent == 75


# --- cost breakdown and comparison --------------------------------------------------------


def test_cost_breakdown_adds_up_to_the_report_labour_cost():
    site = f.SiteFactory(name="Market Hall")
    other = f.SiteFactory(name="Bridge")
    day = datetime.date(2026, 6, 10)
    pay(
        site,
        day,
        20_000,
        piece_amount=5_000,
        bonus_amount=1_000,
        adjustment=-2_000,
        employer_contributions=3_000,
        overtime_hours=Decimal("2.5"),
    )
    pay(other, day, 8_000)
    pay(site, datetime.date(2026, 7, 1), 99_000)  # outside the range

    costs = analytics.cost_breakdown([site, other], day, datetime.date(2026, 6, 30))
    report = build_report([site, other], day, datetime.date(2026, 6, 30))

    first = costs.rows[0]
    assert first.name == "Market Hall"
    assert (first.time_pay, first.piece_work, first.bonuses, first.corrections) == (
        14_000,
        5_000,
        1_000,
        -2_000,
    )
    assert (first.labour_cost, first.employer, first.employer_cost) == (18_000, 3_000, 21_000)
    assert first.overtime_hours == Decimal("2.5")
    assert costs.total.labour_cost == report.labour_cost == 26_000
    assert costs.total.pay_lines == 2


def test_cost_breakdown_by_trade():
    site = f.SiteFactory()
    mason = f.TradeFactory(name="Mason")
    pay(site, TODAY, 10_000, worker=f.WorkerFactory(trade=mason))
    pay(site, TODAY, 7_000)

    costs = analytics.cost_breakdown([site], TODAY, TODAY, trade=mason)

    assert costs.total.labour_cost == 10_000


def test_previous_range_of_a_custom_range_is_the_same_length_just_before():
    june = (datetime.date(2026, 6, 1), datetime.date(2026, 6, 30))
    assert analytics.previous_range(*june) == (
        datetime.date(2026, 5, 2),
        datetime.date(2026, 5, 31),
    )
    assert analytics.previous_range(TODAY, TODAY) == (TODAY - datetime.timedelta(days=1),) * 2


def test_previous_range_of_a_month_is_the_calendar_month_before():
    july = (datetime.date(2026, 7, 1), datetime.date(2026, 7, 31))
    march = (datetime.date(2026, 3, 1), datetime.date(2026, 3, 31))

    # Not 31 May – 30 June, which would hold two monthly pay days.
    assert analytics.previous_range(*july, period="last_month") == (
        datetime.date(2026, 6, 1),
        datetime.date(2026, 6, 30),
    )
    assert analytics.previous_range(*march, period="month") == (
        datetime.date(2026, 2, 1),
        datetime.date(2026, 2, 28),
    )


def test_previous_range_of_a_running_period_covers_the_same_days_so_far():
    november = (datetime.date(2026, 11, 1), datetime.date(2026, 11, 30))
    week = (datetime.date(2026, 10, 26), datetime.date(2026, 11, 1))

    on_the_1st = analytics.previous_range(*november, period="month", today=november[0])
    on_the_10th = analytics.previous_range(
        *november, period="month", today=datetime.date(2026, 11, 10)
    )
    on_wednesday = analytics.previous_range(*week, period="week", today=datetime.date(2026, 10, 28))

    assert on_the_1st == (datetime.date(2026, 10, 1), datetime.date(2026, 10, 1))
    assert on_the_10th == (datetime.date(2026, 10, 1), datetime.date(2026, 10, 10))
    assert on_wednesday == (datetime.date(2026, 10, 19), datetime.date(2026, 10, 21))


def test_a_monthly_pay_is_compared_with_the_month_before_not_doubled():
    site = f.SiteFactory()
    worker = f.WorkerFactory()
    for start, end in (
        (datetime.date(2026, 5, 1), datetime.date(2026, 5, 31)),
        (datetime.date(2026, 6, 1), datetime.date(2026, 6, 30)),
        (datetime.date(2026, 7, 1), datetime.date(2026, 7, 31)),
    ):
        period = f.PayPeriodFactory(site=site, start_date=start, end_date=end, frequency="monthly")
        f.PayLineFactory(period=period, worker=worker, gross=300_000)
    july = (datetime.date(2026, 7, 1), datetime.date(2026, 7, 31))

    previous = build_report([site], *analytics.previous_range(*july, period="last_month"))

    assert previous.labour_cost == 300_000


def test_changes_and_captions():
    assert analytics.change(110, 100).percent == 10
    assert analytics.change(110, 100).caption == "▲ 10% on the previous period"
    assert analytics.change(Decimal("9"), Decimal("12")).direction == "down"
    assert analytics.change(5, 0) == Change(5, 0)
    assert analytics.change(5, 0).caption == "Nothing to compare with in the previous period"
    assert analytics.rate_change(80, 83).caption == "▼ 3 pts on the previous period"
    assert analytics.rate_change(80, 79).caption == "▲ 1 pt on the previous period"
    assert analytics.rate_change(80, 80).caption == "Same as the previous period"
    assert analytics.rate_change(None, 80).direction == ""


# --- cash, unpaid pay and advances --------------------------------------------------------


def test_cash_by_day_reconciles_each_sheet():
    site = f.SiteFactory(name="Market Hall")
    day = datetime.date(2026, 6, 10)
    paid_cash = pay(site, day, 10_000)
    paid_mobile = pay(site, day, 6_000)
    unpaid = pay(site, day, 4_000)
    f.PaymentFactory(pay_line=paid_cash, amount=10_000)
    f.PaymentFactory(pay_line=paid_mobile, amount=6_000, method=PaymentMethod.MOBILE_MONEY)
    f.CashIssueFactory(site=site, date=day, amount=15_000)
    f.CashIssueFactory(site=site, date=datetime.date(2026, 6, 11), amount=1_000)

    summary = analytics.cash_by_day([site], datetime.date(2026, 6, 1), TODAY)

    later, sheet = summary.days
    assert (later.date, later.to_pay, later.cash_issued, later.cash_in_hand) == (
        datetime.date(2026, 6, 11),
        0,
        1_000,
        1_000,
    )
    assert (sheet.to_pay, sheet.paid, sheet.left_to_pay) == (20_000, 16_000, 4_000)
    assert (sheet.workers, sheet.unpaid_workers) == (3, 1)
    assert (sheet.cash_issued, sheet.cash_paid, sheet.cash_in_hand) == (15_000, 10_000, 5_000)
    assert unpaid.payments.count() == 0
    assert (summary.total.to_pay, summary.total.cash_in_hand) == (20_000, 6_000)


def test_unpaid_by_age_buckets_and_leaves_out_locked_and_today():
    site = f.SiteFactory(name="Market Hall")
    open_, paying = PayPeriod.Status.OPEN, PayPeriod.Status.PAYING
    pay(site, TODAY - datetime.timedelta(days=3), 1_000, status=open_)
    pay(site, TODAY - datetime.timedelta(days=20), 2_000, status=paying)
    half = pay(site, TODAY - datetime.timedelta(days=45), 3_000, status=paying)
    f.PaymentFactory(pay_line=half, amount=1_000)
    paid = pay(site, TODAY - datetime.timedelta(days=5), 9_000, status=paying)
    f.PaymentFactory(pay_line=paid)
    pay(site, TODAY - datetime.timedelta(days=60), 7_000, status=PayPeriod.Status.LOCKED)
    pay(site, TODAY - datetime.timedelta(days=61), 5_000)  # a paid period is settled
    pay(site, TODAY, 8_000, status=open_)  # pay day is today: not late yet

    sites, total = analytics.unpaid_by_age([site], today=TODAY)

    assert total.cells() == [("1-7", 1_000, 1), ("8-30", 2_000, 1), ("31+", 2_000, 1)]
    assert [(row.name, row.ages.total) for row in sites] == [("Market Hall", 5_000)]


def test_advances_by_age_per_current_site():
    site = f.SiteFactory(name="Market Hall")
    other = f.SiteFactory(name="Bridge")
    on_site = f.WorkerFactory()
    f.SiteAssignmentFactory(worker=on_site, site=site, start_date=datetime.date(2026, 1, 1))
    on_other = f.WorkerFactory()
    f.SiteAssignmentFactory(worker=on_other, site=other, start_date=datetime.date(2026, 1, 1))
    nowhere = f.WorkerFactory()
    f.AdvanceFactory(worker=on_site, balance=4_000, given_on=TODAY - datetime.timedelta(days=10))
    f.AdvanceFactory(worker=on_site, balance=6_000, given_on=TODAY - datetime.timedelta(days=100))
    f.AdvanceFactory(worker=on_other, balance=2_000, given_on=TODAY - datetime.timedelta(days=40))
    f.AdvanceFactory(worker=nowhere, balance=1_000, given_on=TODAY)
    f.AdvanceFactory(worker=on_other, balance=0, given_on=TODAY)  # repaid

    every = [site, other]
    rows, total = analytics.advances_by_age(Worker.objects.all(), sites=every, today=TODAY)

    assert [(row.name, row.ages.total) for row in rows] == [
        ("Market Hall", 10_000),
        ("Bridge", 2_000),
        ("", 1_000),
    ]
    assert rows[-1].site_id is None
    assert total.cells() == [("0-30", 5_000, 2), ("31-90", 2_000, 1), ("91+", 6_000, 1)]

    only, only_total = analytics.advances_by_age(
        Worker.objects.all(), sites=every, today=TODAY, site=other
    )
    assert [(row.name, row.ages.total) for row in only] == [("Bridge", 2_000)]
    assert only_total.total == 2_000


def test_advances_never_name_a_site_the_viewer_does_not_see():
    mine, theirs = f.SiteFactory(name="Mine"), f.SiteFactory(name="Secret Bridge")
    worker = f.WorkerFactory()
    f.SiteAssignmentFactory(worker=worker, site=theirs, start_date=datetime.date(2026, 1, 1))
    f.SiteAssignmentFactory(worker=worker, site=mine, start_date=TODAY + datetime.timedelta(days=7))
    f.AdvanceFactory(worker=worker, balance=5_000, given_on=TODAY)

    rows, total = analytics.advances_by_age(Worker.objects.all(), sites=[mine], today=TODAY)

    assert [(row.site_id, row.name) for row in rows] == [(None, "")]
    assert total.total == 5_000


def test_the_cash_left_to_pay_adds_up_sheet_by_sheet():
    site = f.SiteFactory()
    over = pay(site, datetime.date(2026, 6, 10), 1_000)
    f.PaymentFactory(pay_line=over, amount=1_010)
    pay(site, datetime.date(2026, 6, 11), 10)

    summary = analytics.cash_by_day([site], datetime.date(2026, 6, 1), TODAY)

    assert [day.left_to_pay for day in summary.days] == [10, 0]
    assert summary.left_to_pay == 10


# --- checks -------------------------------------------------------------------------------


def account(worker, **fields):
    return PayoutAccount.objects.create(worker=worker, account_holder=worker.first_name, **fields)


def test_shared_payout_accounts_compare_digits_and_skip_inactive_workers():
    yao, afi, kofi, gone, alone = (
        f.WorkerFactory(first_name=n) for n in "Yao Afi Kofi Gone Alone".split()
    )
    gone.is_active = False
    gone.save()
    mobile = {"method": TransferMethod.MOBILE_MONEY, "mobile_operator": "Flooz"}
    account(yao, mobile_number="+228 90 11 22 33", **mobile)
    account(afi, mobile_number="+22890112233", **mobile)
    account(gone, mobile_number="+228 90 11 22 33", **mobile)
    bank = {"method": TransferMethod.BANK_TRANSFER, "bank_name": "Ecobank", "bank_code": "TG055"}
    account(kofi, account_number="TG53TG0550000123", **bank)
    account(alone, account_number="TG53TG0550000999", **bank)

    shared = analytics.shared_payout_accounts(Worker.objects.all())

    assert len(shared) == 1
    assert shared[0].method == TransferMethod.MOBILE_MONEY
    assert sorted(p.name.split()[0] for p in shared[0].workers) == ["Afi", "Yao"]


def roll_call(site, date, marks, foreman=None, status=AttendanceDay.Status.APPROVED):
    day = f.AttendanceDayFactory(site=site, date=date, status=status, foreman=foreman)
    return {
        worker: f.AttendanceRecordFactory(day=day, worker=worker, status=mark)
        for worker, mark in marks.items()
    }


def check_in(site, date, worker, **fields):
    fields.setdefault("direction", CheckIn.Direction.IN)
    fields.setdefault("source", CheckIn.Source.FOREMAN_SCAN)
    return CheckIn.objects.create(
        site=site,
        worker=worker,
        date=date,
        at=timezone.now(),
        client_uuid=uuid.uuid4(),
        **fields,
    )


def test_check_in_mismatches_only_on_days_that_used_check_in():
    site = f.SiteFactory(name="Market Hall")
    yao, afi, kofi = f.WorkerFactory(), f.WorkerFactory(), f.WorkerFactory()
    day = datetime.date(2026, 6, 10)
    roll_call(site, day, {yao: P, afi: A, kofi: L})
    check_in(site, day, afi)  # marked absent, checked in
    check_in(site, day, yao, direction=CheckIn.Direction.OUT)  # out only: no check-in
    # Kofi: marked late, no check-in.
    roll_call(site, datetime.date(2026, 6, 11), {yao: P})  # nobody checked in that day
    roll_call(site, day + datetime.timedelta(days=2), {afi: P}, status=AttendanceDay.Status.DRAFT)
    check_in(site, day + datetime.timedelta(days=2), yao)  # a draft day is not compared

    summary = analytics.check_in_mismatches([site], datetime.date(2026, 6, 1), TODAY)

    assert summary.days_checked == 2
    assert (summary.worked_without_check_in, summary.absent_with_check_in) == (2, 1)
    assert {(item.worker_id, item.kind) for item in summary.items} == {
        (yao.pk, Mismatch.WORKED_WITHOUT_CHECK_IN),
        (kofi.pk, Mismatch.WORKED_WITHOUT_CHECK_IN),
        (afi.pk, Mismatch.ABSENT_WITH_CHECK_IN),
    }


def test_check_in_flags_per_site():
    site = f.SiteFactory(name="Market Hall")
    worker = f.WorkerFactory()
    day = datetime.date(2026, 6, 10)
    check_in(site, day, worker, inside_geofence=True)
    check_in(site, day, worker, inside_geofence=False, source=CheckIn.Source.MANUAL)
    check_in(site, day, None, badge_problem=CheckIn.BadgeProblem.UNKNOWN)
    check_in(site, day, worker, badge_problem=CheckIn.BadgeProblem.REVOKED, on_crew=False)
    check_in(f.SiteFactory(), datetime.date(2026, 5, 1), worker)  # outside the range

    (row,) = analytics.check_in_flags([site], datetime.date(2026, 6, 1), TODAY)

    assert (row.total, row.outside_area, row.unknown_badge, row.revoked_badge) == (4, 1, 1, 1)
    assert (row.not_on_crew, row.manual, row.flagged) == (1, 1, 4)
    assert (row.outside_share, row.manual_share) == (25, 25)


def correct(record, user, day_status):
    f.AttendanceCorrectionFactory(record=record, corrected_by=user)
    AuditLog.record(
        actor=user,
        action="attendance.record.correct",
        target=record,
        before={"status": "absent", "day_status": day_status},
        after={"status": "present", "day_status": day_status},
        reason="Arrived late.",
    )


def test_corrections_by_foreman_counts_after_approval_from_the_audit_log():
    site = f.SiteFactory()
    ama, kodjo = f.ForemanFactory(first_name="Ama"), f.ForemanFactory(first_name="Kodjo")
    manager = site.manager
    workers = [f.WorkerFactory() for _ in range(4)]
    day = datetime.date(2026, 6, 10)
    ama_records = roll_call(site, day, dict.fromkeys(workers, P), foreman=ama)
    kodjo_records = roll_call(
        site, day + datetime.timedelta(days=1), {workers[0]: P}, foreman=kodjo
    )
    correct(ama_records[workers[0]], manager, AttendanceDay.Status.SUBMITTED)
    correct(ama_records[workers[1]], manager, AttendanceDay.Status.APPROVED)
    correct(ama_records[workers[1]], manager, AttendanceDay.Status.APPROVED)
    correct(kodjo_records[workers[0]], kodjo, AttendanceDay.Status.SUBMITTED)

    rows = analytics.corrections_by_foreman([site], datetime.date(2026, 6, 1), TODAY)

    assert [(r.name.split()[0], r.records, r.corrections, r.after_approval) for r in rows] == [
        ("Ama", 4, 3, 2),
        ("Kodjo", 1, 1, 0),
    ]
    assert rows[0].per_hundred == Decimal("75.0")


def test_checks_are_limited_to_the_sites_given():
    mine, theirs = f.SiteFactory(), f.SiteFactory()
    worker = f.WorkerFactory()
    day = datetime.date(2026, 6, 10)
    roll_call(theirs, day, {worker: P})
    check_in(theirs, day, f.WorkerFactory())
    pay(theirs, day, 5_000)

    assert analytics.check_in_mismatches([mine], day, day).days_checked == 0
    assert analytics.check_in_flags([mine], day, day) == []
    assert analytics.corrections_by_foreman([mine], day, day) == []
    assert analytics.cash_by_day([mine], day, day).days == []
    assert analytics.cost_breakdown([mine], day, day).rows == []
