"""Reads whose number of queries must not grow with the days, sheets or rows they show.

Each test builds the same screen twice, with a few and with many rows, and compares the
queries. A difference means a query per row came back.
"""

import datetime

from django.db import connection
from django.test.utils import CaptureQueriesContext

from apps.attendance.models import AttendanceDay
from apps.payroll import services as payroll
from tests.test_attendance_api import _clear_cache, _fast_passwords, crew, url  # noqa: F401
from tests.test_payroll_services import (  # noqa: F401
    MON,
    company,
    hire,
    line_of,
    media,
    pay,
    roll_call,
    setup_site,
)


def _queries(call) -> int:
    with CaptureQueriesContext(connection) as queries:
        response = call()
    assert response.status_code == 200
    return len(queries)


def _approved_days(crew, count: int) -> None:  # noqa: F811
    for back in range(count):
        AttendanceDay.objects.get_or_create(
            site=crew.site,
            date=crew.today - datetime.timedelta(days=back + 1),
            defaults={"status": AttendanceDay.Status.APPROVED},
        )


def test_phone_day_list_does_not_query_per_day(crew):  # noqa: F811
    def day_list():
        return crew.client.get(url("days", site_id=crew.site.pk))

    _approved_days(crew, 2)
    few = _queries(day_list)
    _approved_days(crew, 6)
    assert _queries(day_list) == few


def test_phone_sync_does_not_query_per_day(crew):  # noqa: F811
    def sync():
        return crew.client.get(url("sync"))

    _approved_days(crew, 2)
    few = _queries(sync)
    _approved_days(crew, 10)
    assert _queries(sync) == few


def test_paid_days_without_late_corrections_do_not_read_the_attendance(
    company,  # noqa: F811
    media,  # noqa: F811
):
    # The Approvals panel asks which paid workers a correction would change. Without a
    # correction made after the payment, nobody: the roll call is not read for it.
    site, _manager, foreman = setup_site()
    workers = [hire(site) for _ in range(4)]
    roll_call(site, MON, {worker: "present" for worker in workers})
    payroll.cash_sheet(site, MON)
    for worker in workers:
        pay(foreman, line_of(site, MON, worker))
    day = AttendanceDay.objects.get(site=site, date=MON)

    with CaptureQueriesContext(connection) as queries:
        assert payroll.paid_corrections(day) == {}
    assert not [q for q in queries if 'FROM "attendance_attendancerecord"' in q["sql"]]
