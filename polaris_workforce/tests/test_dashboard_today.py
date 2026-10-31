"""The "Today" dashboard: figures for a seeded day, scoping and the query budget."""

import datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.attendance.models import AttendanceDay, AttendanceRecord
from apps.core.models import CompanySettings
from apps.dashboard import services
from apps.payroll.models import PayPeriod
from apps.worksites.models import Site

from . import factories as f

pytestmark = pytest.mark.django_db

TZ = ZoneInfo("Africa/Lome")
NOW = datetime.datetime(2026, 10, 30, 10, 12, tzinfo=TZ)  # a Friday
TODAY = NOW.date()
P, A, H, L = (
    AttendanceRecord.Status.PRESENT,
    AttendanceRecord.Status.ABSENT,
    AttendanceRecord.Status.HALF_DAY,
    AttendanceRecord.Status.LATE,
)


@pytest.fixture(autouse=True)
def _fast_passwords(settings):
    # The factories hash a password for every user; the default hasher is slow on purpose.
    settings.PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]


def day(site, date, status, statuses, **kwargs):
    attendance_day = f.AttendanceDayFactory(site=site, date=date, status=status, **kwargs)
    for worker, record_status in statuses:
        f.AttendanceRecordFactory(day=attendance_day, worker=worker, status=record_status)
    return attendance_day


@pytest.fixture
def seeded(monkeypatch):
    """Two active sites and a paused one, with a week of history at Riverside."""
    monkeypatch.setattr(services, "company_now", lambda company: NOW)
    f.CompanySettingsFactory(time_zone="Africa/Lome")

    manager_a = f.SiteManagerFactory(username="manager_a")
    manager_b = f.SiteManagerFactory(username="manager_b")
    foreman_a = f.ForemanFactory(username="kossi", first_name="Kossi", last_name="Amegah")
    foreman_b = f.ForemanFactory(username="sena", first_name="Sena", last_name="Lawson")
    riverside = f.SiteFactory(name="Riverside", manager=manager_a, foremen=[foreman_a])
    bridge = f.SiteFactory(name="Route 4 Bridge", manager=manager_b, foremen=[foreman_b])
    f.SiteFactory(name="Old Depot", manager=manager_a, status=Site.Status.PAUSED)

    mason = f.TradeFactory(name="Mason")
    riverside_crew = [f.WorkerFactory(trade=mason) for _ in range(5)]
    absentee = riverside_crew[4]
    absentee.first_name, absentee.last_name = "Kodjo", "Atsu"
    absentee.save()
    bridge_crew = [f.WorkerFactory(trade=mason) for _ in range(3)]
    for worker in riverside_crew:
        f.SiteAssignmentFactory(site=riverside, worker=worker)
    for worker in bridge_crew:
        f.SiteAssignmentFactory(site=bridge, worker=worker)

    w1, w2, w3, w4, w5 = riverside_crew
    approved = AttendanceDay.Status.APPROVED
    for back in (2, 1):
        day(
            riverside,
            TODAY - datetime.timedelta(days=back),
            approved,
            [(w1, P), (w2, P), (w3, P), (w4, P), (w5, A)],
        )
    day(
        riverside,
        TODAY,
        AttendanceDay.Status.SUBMITTED,
        [(w1, P), (w2, P), (w3, L), (w4, H), (w5, A)],
        foreman=foreman_a,
        submitted_at=datetime.datetime(2026, 10, 30, 7, 42, tzinfo=TZ),
    )
    yesterday_bridge = day(
        bridge,
        TODAY - datetime.timedelta(days=1),
        AttendanceDay.Status.SUBMITTED,
        [(worker, P) for worker in bridge_crew],
    )
    f.AttendanceCorrectionFactory(record=yesterday_bridge.records.first())
    f.AttendanceCorrectionFactory(record=yesterday_bridge.records.last())

    # Pay tonight at Riverside: 9 000 net, 4 000 already handed over.
    tonight = f.PayPeriodFactory(site=riverside, start_date=TODAY, end_date=TODAY)
    f.PayLineFactory(period=tonight, worker=w1, gross=5000)
    paid_line = f.PayLineFactory(period=tonight, worker=w2, gross=4000)
    f.PaymentFactory(pay_line=paid_line, amount=4000)
    # Yesterday's cash sheet is still open with one worker unpaid.
    yesterday = TODAY - datetime.timedelta(days=1)
    open_sheet = f.PayPeriodFactory(site=riverside, start_date=yesterday, end_date=yesterday)
    f.PayLineFactory(period=open_sheet, worker=w1, gross=5000)
    paid = f.PayLineFactory(period=open_sheet, worker=w2, gross=4000)
    f.PaymentFactory(pay_line=paid, amount=4000)
    # A paid and closed period is not an alert.
    closed = f.PayPeriodFactory(
        site=bridge, start_date=yesterday, end_date=yesterday, status=PayPeriod.Status.PAID
    )
    f.PayLineFactory(period=closed, worker=bridge_crew[0])

    return {
        "admin": f.AdminUserFactory(username="boss"),
        "manager_a": manager_a,
        "manager_b": manager_b,
        "riverside": riverside,
        "bridge": bridge,
    }


def test_overview_numbers_for_a_seeded_day(seeded):
    overview = services.today_overview(seeded["admin"], now=NOW)

    assert overview.today == TODAY
    assert [row.site.name for row in overview.sites] == ["Riverside", "Route 4 Bridge"]
    riverside, bridge = overview.sites
    assert (riverside.present, riverside.absent, riverside.half, riverside.late) == (2, 1, 1, 1)
    assert riverside.expected == 5
    assert riverside.status == "submitted"
    assert riverside.foreman_name == "Kossi Amegah"
    assert riverside.cash_tonight == 5000
    assert not riverside.overdue
    assert (bridge.status, bridge.expected, bridge.present) == ("not_started", 3, 0)
    assert bridge.overdue
    assert bridge.cash_tonight is None

    assert overview.totals == {
        "expected": 8,
        "present": 2,
        "absent": 1,
        "half": 1,
        "late": 1,
        "cash_tonight": 5000,
    }
    assert overview.present_percent == 25
    assert [d.site.name for d in overview.awaiting] == ["Route 4 Bridge", "Riverside"]

    kinds = [(alert.kind, alert.site_name, alert.count) for alert in overview.alerts]
    assert kinds == [
        ("missing_roll_call", "Route 4 Bridge", 0),
        ("corrections", "Route 4 Bridge", 2),
        ("absent_streak", "Riverside", 3),
        ("unclosed_period", "Riverside", 1),
    ]
    streak = overview.alerts[2]
    assert streak.person == "Kodjo Atsu"
    assert streak.detail == "Mason · Riverside"

    chart = {point.date: point.value for point in overview.chart}
    assert len(overview.chart) == 7
    assert overview.chart[-1].is_today
    assert chart[TODAY] == Decimal("3.5")  # 2 present + 1 late + 1 half day × 0.5
    assert chart[TODAY - datetime.timedelta(days=1)] == 7  # 4 at Riverside + 3 at the bridge
    assert chart[TODAY - datetime.timedelta(days=2)] == 4
    assert TODAY - datetime.timedelta(days=5) not in chart  # Sunday is not a working day
    assert overview.chart[-2].height_percent == 100


def test_no_missing_roll_call_before_it_is_due(seeded):
    early = NOW.replace(hour=7, minute=30)

    overview = services.today_overview(seeded["admin"], now=early)

    assert not [alert for alert in overview.alerts if alert.kind == "missing_roll_call"]


def test_site_roll_call_due_override(seeded):
    bridge = seeded["bridge"]
    bridge.roll_call_due = datetime.time(11, 0)
    bridge.save()

    overview = services.today_overview(seeded["admin"], now=NOW)

    assert not overview.sites[1].overdue


def test_holiday_means_no_missing_roll_call(seeded):
    company = CompanySettings.load()
    company.public_holidays = [{"date": TODAY.isoformat(), "name": "Test"}]
    company.save()

    overview = services.today_overview(seeded["admin"], now=NOW)

    assert not overview.is_working_day
    assert overview.missing_roll_calls == 0


def test_site_manager_only_sees_their_sites(seeded):
    overview = services.today_overview(seeded["manager_a"], now=NOW)

    assert [row.site.name for row in overview.sites] == ["Riverside"]
    assert overview.totals["expected"] == 5
    assert [d.site.name for d in overview.awaiting] == ["Riverside"]
    assert {alert.site_name for alert in overview.alerts} == {"Riverside"}
    assert {point.date: point.value for point in overview.chart}[
        TODAY - datetime.timedelta(days=1)
    ] == 4


def test_dashboard_page(client, seeded):
    client.force_login(seeded["admin"])

    response = client.get(reverse("dashboard:home"))

    html = response.content.decode()
    assert response.status_code == 200
    assert "Friday 30 October 2026 · 10:12" in html
    assert "Route 4 Bridge — no roll call yet" in html
    assert "2 corrections after submission" in html
    assert "Kodjo Atsu absent 3 days in a row" in html
    assert "Cash sheet not closed" in html
    assert "5 000 XOF" in html
    assert "07:42 · waiting for approval" in html
    assert "Review 2 days" in html
    assert 'role="img" aria-label="Worker-days per day: ' in html
    assert "Fri 30 Oct 3.5 (today, so far)" in html
    assert "start=2026-10-30" in html


def test_dashboard_page_for_site_manager_hides_other_sites(client, seeded):
    client.force_login(seeded["manager_a"])

    html = client.get(reverse("dashboard:home")).content.decode()

    assert "Riverside" in html
    assert "Route 4 Bridge" not in html
    assert "Old Depot" not in html


def test_dashboard_query_budget(client, seeded, django_assert_max_num_queries):
    client.force_login(seeded["admin"])

    with django_assert_max_num_queries(20):
        response = client.get(reverse("dashboard:home"))

    assert response.status_code == 200


def test_query_count_does_not_grow_with_sites(client, seeded, django_assert_max_num_queries):
    for index in range(5):
        site = f.SiteFactory(name=f"Extra {index}")
        f.SiteAssignmentFactory(site=site)
        day(site, TODAY, AttendanceDay.Status.DRAFT, [(f.WorkerFactory(), P)])
    client.force_login(seeded["admin"])

    with django_assert_max_num_queries(20):
        assert client.get(reverse("dashboard:home")).status_code == 200


def test_empty_dashboard_renders(client):
    client.force_login(f.AdminUserFactory())

    response = client.get(reverse("dashboard:home"))

    assert response.status_code == 200
    assert "Nothing needs attention right now." in response.content.decode()


def test_company_now_uses_company_time_zone():
    company = f.CompanySettingsFactory(time_zone="Asia/Tokyo")

    now = services.company_now(company)

    assert now.utcoffset() == datetime.timedelta(hours=9)
    assert abs((now - timezone.now()).total_seconds()) < 5
