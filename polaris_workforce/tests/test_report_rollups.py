"""Report rollups (``apps.dashboard.rollups``): the report reads whole months from them,
and must show exactly what it shows from the records and pay lines alone.

Two months of roll calls and daily pay on two sites, with trades, a statutory deduction
rule, half days, absences, payments and a correction after payment.
"""

import datetime
import uuid
from decimal import Decimal

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext

from apps.attendance import services as attendance
from apps.dashboard import rollups
from apps.dashboard.reports import _covered_months, build_report
from apps.payroll import services as payroll
from apps.payroll.models import DeductionRule, PayLine, PayPeriod
from apps.precompute import services as precompute
from apps.precompute.models import ReportMonth, Stale, WorkerMonth
from apps.workers.models import Trade
from apps.worksites.models import Site
from tests.factories import CompanySettingsFactory, TradeFactory
from tests.test_payroll_services import hire, pay, roll_call, setup_site

AUG_25 = datetime.date(2025, 8, 25)
SEP_1 = datetime.date(2025, 9, 1)
SEP_30 = datetime.date(2025, 9, 30)
OCT_1 = datetime.date(2025, 10, 1)
OCT_31 = datetime.date(2025, 10, 31)
TODAY = datetime.date(2025, 11, 20)
STATUSES = ["present", "present", "late", "half_day", "absent", "present"]


@pytest.fixture
def media(settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path


@pytest.fixture
def history(db, media):
    """Roll calls and daily pay from 25 Aug to 31 Oct 2025 on two sites."""
    CompanySettingsFactory(working_days=[1, 2, 3, 4, 5, 6])
    DeductionRule.objects.create(
        name="Social security",
        employee_percent=Decimal("4"),
        employer_percent=Decimal("16"),
        active_from=datetime.date(2025, 1, 1),
    )
    mason, carpenter = TradeFactory(name="Mason"), TradeFactory(name="Carpenter")
    first, _manager, foreman = setup_site(name="Riverside")
    second, _m, other_foreman = setup_site(name="Bridge")
    crews = {
        first: [hire(first, trade=mason), hire(first, 5000, trade=carpenter), hire(first)],
        second: [hire(second, 7000, trade=mason), hire(second, trade=carpenter)],
    }
    day, n = AUG_25, 0
    while day <= OCT_31:
        if day.isoweekday() <= 6:
            for site, crew in crews.items():
                marks = {worker: STATUSES[(n + i) % len(STATUSES)] for i, worker in enumerate(crew)}
                roll_call(site, day, marks)
                payroll.cash_sheet(site, day, today=TODAY)
                n += 1
        day += datetime.timedelta(days=1)
    # Some pay is handed out.
    for line in PayLine.objects.filter(net__gt=0).order_by("pk")[::3]:
        pay(foreman if line.period.site_id == first.pk else other_foreman, line)
    return {"sites": [first, second], "mason": mason, "crews": crews, "foreman": foreman}


def build_all(today=TODAY):
    """Build every month, and clear the marks as a finished rebuild does (otherwise the
    report would not trust the rollups of the marked dates, and read them live)."""
    for site in Site.objects.all():
        for month in rollups.months_between(AUG_25, OCT_31):
            rollups.build_month(site, month, today=today)
    Stale.objects.all().delete()


def assert_rollups_are_read(start, end):
    covered = sum(
        len(months)
        for months in _covered_months([s.pk for s in Site.objects.all()], start, end).values()
    )
    assert covered, "the report read nothing from the rollups"


def live_report(*args, **kwargs):
    """The report from the records and pay lines alone (no rollup is current)."""
    ReportMonth.objects.update(stale=True)
    try:
        return build_report(*args, **kwargs)
    finally:
        ReportMonth.objects.update(stale=False)


RANGES = [
    (SEP_1, SEP_30),  # one whole month
    (AUG_25, OCT_31),  # whole months and a partial one
    (datetime.date(2025, 9, 10), datetime.date(2025, 10, 20)),  # no whole month
    (SEP_1, OCT_31),  # two whole months
]


@pytest.mark.parametrize(("start", "end"), RANGES)
def test_rollups_give_the_live_figures(history, start, end):
    build_all()
    sites = Site.objects.all()
    assert build_report(sites, start, end) == live_report(sites, start, end)
    if (start, end) != RANGES[2]:
        assert_rollups_are_read(start, end)


def test_rollups_give_the_live_figures_for_a_trade(history):
    build_all()
    sites = Site.objects.all()
    report = build_report(sites, SEP_1, OCT_31, trade=history["mason"])
    assert report == live_report(sites, SEP_1, OCT_31, trade=history["mason"])
    assert_rollups_are_read(SEP_1, OCT_31)
    assert report.employer_cost.rules and report.labour_cost > 0


def test_whole_months_read_no_record_and_no_pay_line(history):
    build_all()
    with CaptureQueriesContext(connection) as queries:
        report = build_report(Site.objects.all(), SEP_1, OCT_31)
    assert report.labour_cost > 0 and report.workers
    sql = " ".join(q["sql"] for q in queries)
    assert 'FROM "attendance_attendancerecord"' not in sql
    assert '"payroll_payline"' not in sql


def test_a_changed_pay_period_is_read_live_until_rebuilt(history):
    # A correction after payment moves a later pay (its adjustment): that month's
    # rollups are flagged in the same transaction, read live, then rebuilt.
    build_all()
    first = history["sites"][0]
    paid = (
        PayLine.objects.filter(
            period__site=first, period__end_date__lte=SEP_30, payments__isnull=False, net__gt=0
        )
        .select_related("period")
        .first()
    )
    record = paid.period.site.attendance_days.get(date=paid.period.end_date).records.get(
        worker=paid.worker
    )
    PayPeriod.objects.filter(site=first).update(status=PayPeriod.Status.READY)
    attendance.correct_record(
        record, "status", "absent", "Was not there", first.manager, uuid.uuid4()
    )
    # The corrected day is marked (its attendance changed); the later pay the correction
    # moved is flagged (only its lines changed).
    flagged = list(ReportMonth.objects.filter(stale=True).values_list("month", flat=True))
    assert flagged
    assert Stale.objects.get(site=first).start == record.day.date

    sites = Site.objects.all()
    assert build_report(sites, SEP_1, OCT_31) == live_report(sites, SEP_1, OCT_31)
    precompute.refresh_site(first, today=TODAY)
    assert not ReportMonth.objects.filter(stale=True).exists()
    assert not Stale.objects.exists()
    assert build_report(sites, SEP_1, OCT_31) == live_report(sites, SEP_1, OCT_31)
    assert_rollups_are_read(SEP_1, OCT_31)


def test_a_marked_month_is_read_live_until_rebuilt(history):
    # A change not rebuilt yet (its site still marked): the report does not trust the
    # rollups of those dates, and does not rebuild them itself.
    build_all()
    first = history["sites"][0]
    record = first.attendance_days.get(date=datetime.date(2025, 9, 2)).records.first()
    record.status = "absent" if record.status != "absent" else "present"
    record.save()  # as if through a service: the audit entry would mark it
    precompute.mark(first.pk, record.day.date, record.day.date)

    sites = Site.objects.all()
    assert build_report(sites, SEP_1, SEP_30) == live_report(sites, SEP_1, SEP_30)
    assert first.pk not in _covered_months([first.pk], SEP_1, SEP_30)
    assert Stale.objects.filter(site=first).exists()  # the report did not rebuild it


def test_a_locked_month_keeps_the_trades_of_its_time(history):
    first = history["sites"][0]
    worker = history["crews"][first][0]  # a mason
    for period in PayPeriod.objects.filter(site=first, end_date__lte=SEP_30):
        period.lines.filter(payments__isnull=True).delete()
        PayPeriod.objects.filter(pk=period.pk).update(status=PayPeriod.Status.LOCKED)
    first.attendance_days.filter(date__gte=SEP_1, date__lte=SEP_30).update(status="locked")
    build_all()
    assert ReportMonth.objects.get(site=first, month=SEP_1).locked

    worker.trade = Trade.objects.get(name="Carpenter")
    worker.save()
    build_all()  # September is locked: not rebuilt; October is

    september = WorkerMonth.objects.get(site=first, worker=worker, month=SEP_1)
    october = WorkerMonth.objects.get(site=first, worker=worker, month=OCT_1)
    assert september.trade_id == history["mason"].pk
    assert october.trade_id == worker.trade_id
    masons = build_report([first], SEP_1, SEP_30, trade=history["mason"])
    assert worker.pk in {row.worker_id for row in masons.workers}


def test_the_rebuild_step_builds_the_marked_months(history):
    first = history["sites"][0]
    precompute.mark(first.pk, datetime.date(2025, 9, 15), datetime.date(2025, 10, 3))
    precompute.refresh_site(first, today=TODAY)
    months = set(ReportMonth.objects.filter(site=first).values_list("month", flat=True))
    assert {SEP_1, OCT_1} <= months


def test_rebuilding_a_month_again_changes_nothing(history):
    # Rebuilding a month twice changes nothing.
    build_all()
    before = list(WorkerMonth.objects.order_by("site_id", "month", "worker_id").values())
    build_all()
    after = list(WorkerMonth.objects.order_by("site_id", "month", "worker_id").values())
    strip = [{k: v for k, v in row.items() if k != "id"} for row in before]
    assert strip == [{k: v for k, v in row.items() if k != "id"} for row in after]


def test_the_sweep_fills_in_history_a_few_months_at_a_time(history, monkeypatch):
    first = history["sites"][0]
    monkeypatch.setattr(rollups, "BACKFILL_MONTHS", 2)
    assert rollups.missing_months(first, TODAY)[:3] == [
        datetime.date(2025, 8, 1),
        SEP_1,
        OCT_1,
    ]

    precompute.refresh_site(first, today=TODAY, everything=True)

    built = set(ReportMonth.objects.filter(site=first).values_list("month", flat=True))
    assert {datetime.date(2025, 8, 1), SEP_1} <= built
