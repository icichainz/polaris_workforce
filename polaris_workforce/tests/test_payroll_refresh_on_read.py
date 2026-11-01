"""Refreshing pay on read without the write lock (``services.refresh_period``).

A refresh without an actor first runs refusing every write, outside any transaction; it
starts again in a transaction only when it reaches a write. The rest of the suite runs
each test inside a transaction, where that first pass is skipped (a refused write would
mark the transaction for rollback), so these tests, and the payroll scenarios re-run by
the ``*_outside_transaction`` modules, use the transactional database.
"""

import datetime

import pytest
from django.db import connection

from apps.core.models import AuditLog
from apps.payroll import services
from apps.payroll.models import PayLine, PayPeriod
from tests.test_payroll_services import MON, company, hire, roll_call, setup_site  # noqa: F401

pytestmark = pytest.mark.django_db(transaction=True)


class Statements:
    """Counts what reaches the database, by first word (SELECT, INSERT, BEGIN…)."""

    def __init__(self):
        self.seen: dict[str, int] = {}

    def __call__(self, execute, sql, params, many, context):
        word = sql.split(None, 1)[0].upper()
        self.seen[word] = self.seen.get(word, 0) + 1
        return execute(sql, params, many, context)

    @property
    def other_than_reads(self) -> dict[str, int]:
        return {word: n for word, n in self.seen.items() if word != "SELECT"}


def test_refreshing_unchanged_pay_takes_no_lock_and_writes_nothing(company):  # noqa: F811
    site, _m, _f = setup_site()
    roll_call(site, MON, {hire(site): "present", hire(site): "late"})
    services.cash_sheet(site, MON)
    entries = AuditLog.objects.count()

    statements = Statements()
    with connection.execute_wrapper(statements):
        services.cash_sheet(site, MON)

    assert statements.other_than_reads == {}  # No BEGIN: SQLite's write lock is free.
    assert AuditLog.objects.count() == entries


def test_a_refresh_that_must_write_starts_again_in_a_transaction(company):  # noqa: F811
    site, _m, _f = setup_site()
    roll_call(site, MON, {hire(site): "present"})

    statements = Statements()
    with connection.execute_wrapper(statements):
        sheet = services.cash_sheet(site, MON)

    assert statements.seen.get("BEGIN", 0) >= 1
    assert [sl.line.net for sl in sheet.lines] == [6000]
    assert sheet.periods[0].status == PayPeriod.Status.READY
    assert set(
        AuditLog.objects.filter(action__startswith="payroll.").values_list("action", flat=True)
    ) >= {"payroll.auto.period.refresh", "payroll.auto.period.status"}


def test_a_status_change_found_by_the_first_pass_is_saved(company):  # noqa: F811
    # The first pass sets the new status on the period in memory before it reaches the
    # save; the period is reloaded so that the second pass still sees the change.
    site, _m, _f = setup_site()
    roll_call(site, MON, {hire(site): "present"})
    period = services.sync_sheet(site, MON, today=MON - datetime.timedelta(days=1))[0]
    assert period.status == PayPeriod.Status.OPEN

    services.refresh_period(period, today=MON)

    period.refresh_from_db()
    assert period.status == PayPeriod.Status.READY
    assert AuditLog.objects.filter(
        action="payroll.auto.period.status", after={"status": "ready"}
    ).exists()


def test_a_refresh_with_an_actor_goes_straight_to_the_transaction(company):  # noqa: F811
    site, manager, _f = setup_site()
    roll_call(site, MON, {hire(site): "present"})
    services.cash_sheet(site, MON)
    period = PayPeriod.objects.get(site=site)

    statements = Statements()
    with connection.execute_wrapper(statements):
        services.refresh_period(period, actor=manager)

    assert statements.seen.get("BEGIN") == 1


def test_refresh_reads_do_not_grow_with_the_crew(company):  # noqa: F811
    def queries_for(crew_size):
        site, _m, _f = setup_site()
        roll_call(site, MON, {hire(site): "present" for _ in range(crew_size)})
        services.cash_sheet(site, MON)
        period = PayPeriod.objects.get(site=site)
        statements = Statements()
        with connection.execute_wrapper(statements):
            services.refresh_period(period)
        return sum(statements.seen.values())

    assert queries_for(8) == queries_for(2)
    assert PayLine.objects.count() == 10
