"""End to end: a site that overrides the pay frequency to weekly.

The company pays daily; the site is weekly. Working days are Monday–Saturday, so the week
is paid on Saturday (the last working day) and the whole week is one pay period
(Sunday 31 Aug – Saturday 6 Sep 2025). A daily site next door keeps its daily periods.
"""

import datetime

import pytest
from django.core.cache import cache

from apps.attendance.models import AttendanceDay
from apps.payroll.models import PayPeriod
from tests.e2e import (
    Office,
    Phone,
    admin_office,
    hire,
    lines_by_worker,
    make_site,
    new_uuid,
    record,
)
from tests.factories import CompanySettingsFactory

pytestmark = pytest.mark.django_db

PIN = "8642"
PHONE = "+228 90 77 88 99"
MON = datetime.date(2025, 9, 1)
WEEK = [MON + datetime.timedelta(days=i) for i in range(6)]  # Mon … Sat
WED, SAT = WEEK[2], WEEK[5]


@pytest.fixture(autouse=True)
def _isolated(settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path
    cache.clear()
    yield
    cache.clear()


def test_weekly_site_has_one_period_paid_on_the_last_working_day():
    CompanySettingsFactory(pay_frequency="daily")
    crew = make_site("Harbour Depot", phone_number=PHONE, pin=PIN, pay_frequency="weekly")
    ama = hire(crew, "Ama", 6000)
    esi = hire(crew, "Esi", 750, pay_type="hourly")
    site_id = crew.site.pk
    phone = Phone(PHONE, PIN)
    manager = Office(crew.manager)

    for day in WEEK:
        ama_mark = "half_day" if day == WED else "present"
        esi_mark = "absent" if day == SAT else "present"  # no times: 9 scheduled hours
        marks = [record(ama, ama_mark), record(esi, esi_mark)]
        assert phone.put_records(site_id, day, marks).status_code == 200
        assert phone.submit(site_id, day, new_uuid()).status_code == 200
        if day != SAT:
            manager.approve(AttendanceDay.objects.get(site=crew.site, date=day).pk)

    # Mid-week there is nothing to pay; on Saturday the sheet waits for the last approval.
    midweek = phone.payout(site_id, WED).json()
    assert (midweek["status"], midweek["lines"]) == ("not_open", [])
    assert phone.payout(site_id, SAT).json()["status"] == "not_open"
    manager.approve(AttendanceDay.objects.get(site=crew.site, date=SAT).pk)

    payout = phone.payout(site_id, SAT).json()
    assert payout["status"] == "open"
    period = PayPeriod.objects.get(site=crew.site)
    assert (period.start_date, period.end_date, period.frequency) == (
        datetime.date(2025, 8, 31),
        SAT,
        "weekly",
    )
    lines = lines_by_worker(payout)
    # Ama: 5 days + a half day of 6000. Esi: 5 days × 9 h × 750.
    assert (lines[ama.pk]["units"], lines[ama.pk]["net"]) == ("5.50", 33000)
    assert (lines[esi.pk]["units"], lines[esi.pk]["net"]) == ("45.00", 33750)
    assert payout["total_to_pay"] == 66750

    for line in payout["lines"]:
        assert phone.pay(line["pay_line_id"], line["net"], new_uuid()).status_code == 201
    assert phone.payout(site_id, SAT).json()["status"] == "paid"
    locked = admin_office().lock_sheet(site_id, SAT)
    assert locked.status_code == 302
    period.refresh_from_db()
    assert period.is_locked
    statuses = set(AttendanceDay.objects.filter(site=crew.site).values_list("status", flat=True))
    assert statuses == {AttendanceDay.Status.LOCKED}


def test_daily_site_next_door_keeps_daily_periods():
    CompanySettingsFactory(pay_frequency="daily")
    crew = make_site("Corner Shop", phone_number=PHONE, pin=PIN)
    ama = hire(crew, "Ama", 6000)
    phone = Phone(PHONE, PIN)
    manager = Office(crew.manager)
    for day in WEEK[:2]:
        phone.put_records(crew.site.pk, day, [record(ama, "present")])
        phone.submit(crew.site.pk, day, new_uuid())
        manager.approve(AttendanceDay.objects.get(site=crew.site, date=day).pk)
        assert phone.payout(crew.site.pk, day).json()["total_to_pay"] == 6000
    ends = sorted(PayPeriod.objects.filter(site=crew.site).values_list("end_date", flat=True))
    assert ends == WEEK[:2]
