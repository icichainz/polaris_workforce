"""Worker services: numbering, rate history, absence streaks, scoping."""

import datetime

import pytest
from django.core.exceptions import ValidationError

from apps.attendance.models import AttendanceRecord
from apps.core.choices import PayType
from apps.core.models import AuditLog
from apps.payroll.models import PayPeriod
from apps.workers import services
from apps.workers.models import Worker
from tests.factories import (
    AdminUserFactory,
    AttendanceDayFactory,
    AttendanceRecordFactory,
    PayLineFactory,
    PayPeriodFactory,
    PayRateFactory,
    SiteAssignmentFactory,
    SiteFactory,
    SiteManagerFactory,
    UserFactory,
    WorkerFactory,
)

pytestmark = pytest.mark.django_db

D = datetime.date


# --- numbering --------------------------------------------------------------------------


def test_first_number_is_w_0001():
    assert services.next_worker_number() == "W-0001"


def test_numbers_are_sequential_and_unique():
    created = [services.create_worker(first_name="A", last_name=str(i)) for i in range(12)]

    numbers = [w.number for w in created]
    assert numbers == [f"W-{n:04d}" for n in range(1, 13)]
    assert len(set(numbers)) == len(numbers)


def test_numbering_continues_after_the_highest_and_past_9999():
    WorkerFactory(number="W-0042")
    WorkerFactory(number="X-9999")  # not ours: ignored
    assert services.next_worker_number() == "W-0043"

    WorkerFactory(number="W-9999")
    assert services.next_worker_number() == "W-10000"
    WorkerFactory(number="W-10000")
    assert services.next_worker_number() == "W-10001"


def test_create_worker_writes_audit_assignment_and_rate():
    site = SiteFactory()
    actor = AdminUserFactory()

    worker = services.create_worker(
        actor=actor,
        first_name="Kafui",
        last_name="Amedome",
        site=site,
        start_date=D(2026, 11, 2),
        pay_type=PayType.HOURLY,
        amount=900,
        effective_from=D(2026, 11, 2),
    )

    assert worker.number == "W-0001"
    assert worker.assignments.get().site == site
    assert worker.rate_on(D(2026, 11, 2)).amount == 900
    actions = set(AuditLog.objects.values_list("action", flat=True))
    assert {
        "workers.create",
        "worksites.assignment_create",
        "workers.rate_change",
    } <= actions


def test_a_number_taken_between_read_and_insert_is_retried(monkeypatch):
    """Simulates two creates racing: the first read returns a number already in use."""
    WorkerFactory(number="W-0001")
    real = services.next_worker_number
    answers = iter(["W-0001"])  # stale read, as if another create just took it

    def racing():
        return next(answers, None) or real()

    monkeypatch.setattr(services, "next_worker_number", racing)
    monkeypatch.setattr(Worker, "validate_unique", lambda self, exclude=None: None)

    worker = services.create_worker(first_name="T", last_name="Race")

    assert worker.number == "W-0002"
    assert sorted(Worker.objects.values_list("number", flat=True)) == ["W-0001", "W-0002"]


# --- rates ------------------------------------------------------------------------------


def test_rate_change_keeps_history_and_rate_on_picks_the_right_one():
    worker = WorkerFactory()
    services.change_rate(worker, 5500, PayType.DAILY, D(2026, 3, 3))
    services.change_rate(worker, 6000, PayType.DAILY, D(2026, 9, 1))
    services.change_rate(worker, 900, PayType.HOURLY, D(2026, 11, 1))

    assert worker.pay_rates.count() == 3
    assert worker.rate_on(D(2026, 3, 2)) is None
    assert worker.rate_on(D(2026, 3, 3)).amount == 5500
    assert worker.rate_on(D(2026, 8, 31)).amount == 5500
    assert worker.rate_on(D(2026, 9, 1)).amount == 6000
    assert worker.rate_on(D(2026, 10, 31)).amount == 6000
    late = worker.rate_on(D(2026, 12, 25))
    assert (late.amount, late.pay_type) == (900, PayType.HOURLY)

    history = services.rate_history(worker)
    assert [h["rate"].amount for h in history] == [900, 6000, 5500]
    assert [h["until"] for h in history] == [None, D(2026, 10, 31), D(2026, 8, 31)]


def test_rate_change_writes_audit_with_before_and_after():
    worker = WorkerFactory()
    actor = SiteManagerFactory()
    services.change_rate(worker, 5500, PayType.DAILY, D(2026, 3, 3), actor=actor)
    services.change_rate(worker, 6000, PayType.DAILY, D(2026, 9, 1), actor=actor)

    entry = AuditLog.objects.filter(action="workers.rate_change").first()
    assert entry.actor == actor
    assert entry.before["amount"] == 5500
    assert entry.after == {"pay_type": "daily", "amount": 6000, "effective_from": "2026-09-01"}


def test_same_day_change_replaces_that_days_rate():
    worker = WorkerFactory()
    services.change_rate(worker, 5500, PayType.DAILY, D(2026, 9, 1))
    services.change_rate(worker, 6000, PayType.DAILY, D(2026, 9, 1))

    assert worker.pay_rates.get().amount == 6000


def test_change_rate_if_different_skips_identical_rate():
    worker = WorkerFactory()
    services.change_rate(worker, 6000, PayType.DAILY, D(2026, 9, 1))

    assert services.change_rate_if_different(worker, 6000, PayType.DAILY, D(2026, 10, 1)) is None
    assert worker.pay_rates.count() == 1


def test_rate_rejects_floats_and_negative():
    worker = WorkerFactory()
    with pytest.raises(TypeError):
        services.change_rate(worker, 6000.0, PayType.DAILY, D(2026, 9, 1))
    with pytest.raises(ValidationError):
        services.change_rate(worker, -1, PayType.DAILY, D(2026, 9, 1))


def test_rate_change_inside_locked_period_is_refused():
    worker = WorkerFactory()
    period = PayPeriodFactory(
        start_date=D(2026, 10, 1), end_date=D(2026, 10, 15), status=PayPeriod.Status.LOCKED
    )
    PayLineFactory(period=period, worker=worker)

    with pytest.raises(ValidationError):
        services.change_rate(worker, 7000, PayType.DAILY, D(2026, 10, 10))
    services.change_rate(worker, 7000, PayType.DAILY, D(2026, 10, 16))
    assert worker.rate_on(D(2026, 10, 16)).amount == 7000


def test_rates_on_batches_by_worker():
    a, b = WorkerFactory(), WorkerFactory()
    PayRateFactory(worker=a, amount=5000, effective_from=D(2026, 1, 1))
    PayRateFactory(worker=a, amount=5500, effective_from=D(2026, 6, 1))
    PayRateFactory(worker=b, amount=4000, effective_from=D(2026, 12, 1))

    rates = services.rates_on([a, b], D(2026, 7, 1))
    assert rates[a.pk].amount == 5500
    assert b.pk not in rates


# --- absent streak ----------------------------------------------------------------------


def _mark(worker, site, date, status):
    day = AttendanceDayFactory(site=site, date=date)
    return AttendanceRecordFactory(day=day, worker=worker, status=status)


def test_absent_streak_counts_consecutive_absences_before_the_date():
    site = SiteFactory()
    worker = WorkerFactory()
    _mark(worker, site, D(2026, 10, 26), AttendanceRecord.Status.PRESENT)
    _mark(worker, site, D(2026, 10, 27), AttendanceRecord.Status.ABSENT)
    _mark(worker, site, D(2026, 10, 28), AttendanceRecord.Status.ABSENT)
    _mark(worker, site, D(2026, 10, 30), AttendanceRecord.Status.ABSENT)  # 29: no record
    _mark(worker, site, D(2026, 10, 31), AttendanceRecord.Status.PRESENT)  # the day itself

    assert services.absent_streak(worker, D(2026, 10, 31)) == 3
    assert services.absent_streak(worker, D(2026, 10, 28)) == 1
    assert services.absent_streak(worker, D(2026, 10, 27)) == 0
    assert services.absent_streak(WorkerFactory(), D(2026, 10, 31)) == 0


def test_late_or_half_day_breaks_the_streak():
    site = SiteFactory()
    worker = WorkerFactory()
    _mark(worker, site, D(2026, 10, 28), AttendanceRecord.Status.ABSENT)
    _mark(worker, site, D(2026, 10, 29), AttendanceRecord.Status.HALF_DAY)
    _mark(worker, site, D(2026, 10, 30), AttendanceRecord.Status.ABSENT)

    assert services.absent_streak(worker, D(2026, 10, 31)) == 1


# --- scoping ----------------------------------------------------------------------------


def test_scoped_workers_for_a_site_manager():
    manager = SiteManagerFactory()
    mine, other = SiteFactory(manager=manager), SiteFactory()
    on_mine = SiteAssignmentFactory(site=mine).worker
    ended = SiteAssignmentFactory(site=mine, end_date=D(2026, 5, 1)).worker
    elsewhere = SiteAssignmentFactory(site=other).worker
    today = D(2026, 10, 31)

    visible = set(services.scoped_workers(manager, today))
    assert visible == {on_mine}
    assert ended not in visible and elsewhere not in visible
    assert set(services.scoped_workers(AdminUserFactory(), today)) == {on_mine, ended, elsewhere}
    assert not services.can_edit_worker(UserFactory(), on_mine, today)  # foreman
    assert services.can_edit_worker(manager, on_mine, today)
    assert not services.can_edit_worker(manager, elsewhere, today)
