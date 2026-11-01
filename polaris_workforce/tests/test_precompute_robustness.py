"""The precompute service under failures and races (review of the precompute branch)."""

import datetime

import pytest
from django.contrib.admin.sites import site as admin_site
from django.test import RequestFactory
from kombu.exceptions import OperationalError

from apps.core.models import AuditLog
from apps.dashboard import rollups
from apps.dashboard.reports import _on_covered_days, _on_months, _on_ranges, build_report
from apps.payroll.models import PayLine, PayPeriod
from apps.precompute import services, tasks
from apps.precompute.models import ReportMonth, SiteDayCounts, Stale, WorkerMonth
from apps.workers.models import PayRate, Trade
from apps.worksites.models import Site
from tests.factories import UserFactory
from tests.test_payroll_services import MON, TUE, company, hire, roll_call, setup_site  # noqa: F401
from tests.test_report_rollups import (  # noqa: F401
    OCT_31,
    SEP_1,
    SEP_30,
    TODAY,
    build_all,
    history,
    live_report,
    media,
)


@pytest.fixture
def admin_request(db):
    request = RequestFactory().post("/admin/")
    request.user = UserFactory(is_superuser=True, is_staff=True, company=None)
    return request


class _Form:
    def __init__(self, changed_data):
        self.changed_data = changed_data


# --- 1. one failing site --------------------------------------------------------------------


def test_a_failing_site_does_not_stop_the_others(company, monkeypatch):  # noqa: F811
    broken, *_ = setup_site()
    fine, *_ = setup_site()
    services.mark(broken.pk, MON, MON)
    services.mark(fine.pk, MON, MON)
    rebuilt = []

    def step(site, *args, **kwargs):
        if site.pk == broken.pk:
            raise ValueError("bad rule data")
        rebuilt.append(site.pk)

    monkeypatch.setattr(services, "_STEPS", [(0, step)])
    run = services.run(broken.company_id, sweep=True, today=TUE)

    assert fine.pk in rebuilt
    assert run.finished_at is not None
    assert {"site": broken.pk, "error": "ValueError: bad rule data"} in run.mismatches
    assert Stale.objects.filter(site=broken).exists()  # still marked: tried again next run
    assert not Stale.objects.filter(site=fine).exists()


# --- 2. many sites and months ---------------------------------------------------------------


def test_report_conditions_grow_with_months_not_sites_times_months(company):  # noqa: F811
    sites = [setup_site()[0].pk for _ in range(3)]
    months = rollups.months_between(datetime.date(2022, 1, 1), datetime.date(2025, 12, 1))
    covered = {site_id: months for site_id in sites * 400}  # as if 1 200 sites, 48 months
    covered = {**covered, **{10_000 + i: months for i in range(1_200)}}
    assert len(_on_months(covered).children) <= len(months) + 1
    assert len(_on_covered_days(covered).children) <= len(months) + 1
    ranges = {site_id: [(MON, TUE)] for site_id in covered}
    assert len(_on_ranges(ranges, "site_id", "date").children) <= 2
    # And SQLite runs them (a term per site and month would pass its depth of 1000).
    assert WorkerMonth.objects.filter(_on_months(covered)).count() == 0
    assert SiteDayCounts.objects.filter(_on_covered_days(covered)).count() == 0


# --- 3. a flag set while a month is being built -----------------------------------------------


def test_a_flag_set_during_a_build_is_kept(history, monkeypatch):  # noqa: F811
    first = history["sites"][0]
    rollups.build_month(first, SEP_1, today=TODAY)
    real = rollups.add_deductions

    def flag_meanwhile(*args):
        ReportMonth.objects.filter(site=first, month=SEP_1).update(
            stale=True, generation=ReportMonth.objects.get(site=first, month=SEP_1).generation + 1
        )
        monkeypatch.setattr(rollups, "add_deductions", real)
        real(*args)

    monkeypatch.setattr(rollups, "add_deductions", flag_meanwhile)
    rollups.build_month(first, SEP_1, today=TODAY)

    assert ReportMonth.objects.get(site=first, month=SEP_1).stale
    rollups.build_month(first, SEP_1, today=TODAY)
    assert not ReportMonth.objects.get(site=first, month=SEP_1).stale


# --- 4. an admin edit in a locked month ----------------------------------------------------


def _lock_september(site):
    PayPeriod.objects.filter(site=site, end_date__lte=SEP_30).update(status=PayPeriod.Status.LOCKED)
    site.attendance_days.filter(date__lte=SEP_30).update(status="locked")


def test_an_admin_edit_of_locked_pay_is_rolled_up(history, admin_request):  # noqa: F811
    first = history["sites"][0]
    _lock_september(first)
    build_all()
    assert ReportMonth.objects.get(site=first, month=SEP_1).locked
    line = PayLine.objects.filter(period__site=first, period__end_date__lte=SEP_30).first()
    line.gross += 1000

    admin_site._registry[PayLine].save_model(admin_request, line, _Form(["gross"]), change=True)
    services.refresh_site(first, today=TODAY)

    sites = Site.objects.all()
    assert build_report(sites, SEP_1, SEP_30) == live_report(sites, SEP_1, SEP_30)
    assert not Stale.objects.exists()


# --- 5. a trade change ----------------------------------------------------------------------


def test_a_trade_change_rebuilds_the_unlocked_months(history):  # noqa: F811
    first = history["sites"][0]
    build_all()
    worker = history["crews"][first][0]
    worker.trade = Trade.objects.get(name="Carpenter")
    worker.save()
    AuditLog.record(actor=None, action="workers.update", target=worker)

    assert set(ReportMonth.objects.filter(site=first, stale=True).values_list("month", flat=True))
    services.refresh_site(first, today=TODAY)
    sites = Site.objects.all()
    carpenters = worker.trade
    assert build_report(sites, SEP_1, OCT_31, trade=carpenters) == live_report(
        sites, SEP_1, OCT_31, trade=carpenters
    )


# --- 6. Redis down ------------------------------------------------------------------------------


def test_a_dead_broker_is_tried_once_then_left_alone(
    company,  # noqa: F811
    settings,
    monkeypatch,
    django_capture_on_commit_callbacks,
):
    settings.CELERY_BROKER_URL = "memory://"
    monkeypatch.setattr(services, "_broker_down_until", 0.0)
    attempts = []

    def unreachable(*args, **kwargs):
        attempts.append(1)
        raise OperationalError("Connection refused")

    monkeypatch.setattr(tasks.rebuild_site, "apply_async", unreachable)
    sites = [setup_site()[0] for _ in range(4)]
    with django_capture_on_commit_callbacks(execute=True):
        AuditLog.record(actor=None, action="settings.update")  # marks every site

    assert Stale.objects.filter(site__in=sites).count() == 4
    assert len(attempts) == 1


# --- 7. the sweep's report --------------------------------------------------------------------


def test_the_sweep_reports_a_sites_own_changes_only(company, monkeypatch):  # noqa: F811
    quiet, *_ = setup_site()
    other, *_ = setup_site()
    roll_call(other, MON, {hire(other): "present"})
    period = PayPeriod.objects.create(site=other, start_date=MON, end_date=MON, frequency="daily")

    def step(site, *args, **kwargs):
        # While the quiet site is rebuilt, a screen recomputes the other one.
        if site.pk == quiet.pk:
            AuditLog.record(actor=None, action="payroll.auto.period.refresh", target=period)

    monkeypatch.setattr(services, "_STEPS", [(0, step)])
    run = services.run(quiet.company_id, sweep=True, today=TUE)

    assert quiet.pk not in [item.get("site") for item in run.mismatches if "changes" in item]


# --- 8. a site with attendance but no pay -----------------------------------------------------


def test_a_site_without_pay_is_not_in_the_cost_chart(history):  # noqa: F811
    unpaid, *_ = setup_site(name="No pay yet")
    worker = hire(unpaid)
    for day in (datetime.date(2025, 9, 2), datetime.date(2025, 9, 3)):
        roll_call(unpaid, day, {worker: "present"})
    build_all()

    sites = Site.objects.all()
    report = build_report(sites, SEP_1, SEP_30)
    assert report == live_report(sites, SEP_1, SEP_30)
    assert unpaid.pk not in [item.site_id for item in report.by_site]


# --- 9. an admin edit that moves a record --------------------------------------------------------


def test_an_admin_edit_marks_where_a_rate_moved_from(company, admin_request):  # noqa: F811
    site, *_ = setup_site()
    worker = hire(site)
    rate = PayRate.objects.get(worker=worker)
    old = rate.effective_from
    Stale.objects.all().delete()
    rate.effective_from = old + datetime.timedelta(days=30)

    admin_site._registry[PayRate].save_model(
        admin_request, rate, _Form(["effective_from"]), change=True
    )

    assert Stale.objects.get(site=site).start == old
