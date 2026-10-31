"""Reports: figures, filters, scoping and the CSV export."""

import csv
import datetime
import io
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest
from django.urls import reverse

from apps.attendance.models import AttendanceDay, AttendanceRecord
from apps.core.models import CompanySettings
from apps.dashboard import views
from apps.dashboard.forms import period_dates
from apps.dashboard.reports import build_report
from apps.worksites.models import Site

from . import factories as f

pytestmark = pytest.mark.django_db

NOW = datetime.datetime(2026, 10, 20, 9, 0, tzinfo=ZoneInfo("Africa/Lome"))
OCT = datetime.date(2026, 10, 1), datetime.date(2026, 10, 31)
P, A, H, L = (
    AttendanceRecord.Status.PRESENT,
    AttendanceRecord.Status.ABSENT,
    AttendanceRecord.Status.HALF_DAY,
    AttendanceRecord.Status.LATE,
)
CSV_HEADER = [
    "Worker number",
    "Worker",
    "Trade",
    "Site",
    "Present",
    "Half days",
    "Late",
    "Absent",
    "Worker-days",
    "Attendance rate (%)",
    "Earned",
    "Statutory deductions",
    "Employer contributions",
    "Currency",
]


@pytest.fixture(autouse=True)
def _fast_passwords(settings):
    # The factories hash a password for every user; the default hasher is slow on purpose.
    settings.PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]


def day(site, date, status, statuses):
    attendance_day = f.AttendanceDayFactory(site=site, date=date, status=status)
    for worker, record_status in statuses:
        f.AttendanceRecordFactory(day=attendance_day, worker=worker, status=record_status)


def period(site, end, lines):
    pay_period = f.PayPeriodFactory(site=site, start_date=end, end_date=end)
    for worker, gross in lines:
        f.PayLineFactory(period=pay_period, worker=worker, gross=gross)


@pytest.fixture
def seeded(monkeypatch):
    monkeypatch.setattr(views, "company_now", lambda company: NOW)
    f.CompanySettingsFactory()
    manager_a = f.SiteManagerFactory(username="manager_a")
    riverside = f.SiteFactory(name="Riverside", manager=manager_a, labour_budget=1_000_000)
    bridge = f.SiteFactory(name="Route 4 Bridge", manager=f.SiteManagerFactory())
    mason = f.TradeFactory(name="Mason")
    labourer = f.TradeFactory(name="Labourer")
    w1 = f.WorkerFactory(number="W-0001", first_name="Akouvi", last_name="Tete", trade=mason)
    w2 = f.WorkerFactory(number="W-0002", first_name="Kodjo", last_name="Atsu", trade=labourer)
    w3 = f.WorkerFactory(number="W-0003", first_name="Yao", last_name="Mensah", trade=mason)
    w4 = f.WorkerFactory(number="W-0004", first_name="Afi", last_name="Dosseh", trade=mason)

    s = AttendanceDay.Status
    day(riverside, datetime.date(2026, 9, 30), s.APPROVED, [(w1, P)])  # before the range
    day(riverside, datetime.date(2026, 10, 5), s.APPROVED, [(w1, P), (w2, H)])
    day(riverside, datetime.date(2026, 10, 6), s.APPROVED, [(w1, L), (w2, A)])
    day(riverside, datetime.date(2026, 10, 12), s.LOCKED, [(w1, P), (w2, P)])
    day(riverside, datetime.date(2026, 10, 13), s.DRAFT, [(w1, P)])  # not final
    day(riverside, datetime.date(2026, 10, 14), s.REJECTED, [(w1, P)])  # not final
    day(bridge, datetime.date(2026, 10, 6), s.SUBMITTED, [(w3, P), (w4, A)])

    period(riverside, datetime.date(2026, 10, 6), [(w1, 10_000), (w2, 3_000)])
    period(riverside, datetime.date(2026, 10, 12), [(w1, 5_000), (w2, 5_000)])
    period(bridge, datetime.date(2026, 10, 6), [(w3, 6_000)])
    period(riverside, datetime.date(2026, 11, 2), [(w1, 99_000)])  # after the range
    return {
        "admin": f.AdminUserFactory(username="boss"),
        "accountant": f.UserFactory(username="counts", role="accountant"),
        "manager_a": manager_a,
        "riverside": riverside,
        "bridge": bridge,
        "mason": mason,
    }


def test_report_figures(seeded):
    report = build_report(Site.objects.all(), *OCT)

    assert report.labour_cost == 29_000
    assert report.worker_days == Decimal("5.5")
    assert report.recorded == 8
    assert report.attendance_rate == 69  # 5.5 / 8
    assert report.cost_per_worker_day == 5273  # 29 000 / 5.5, rounded
    assert [(item.name, item.cost, item.width_percent) for item in report.by_site] == [
        ("Riverside", 23_000, 100),
        ("Route 4 Bridge", 6_000, 26),
    ]
    assert report.by_site[0].budget == 1_000_000
    assert [(week.week, week.rate) for week in report.by_week] == [
        (40, None),
        (41, 58),  # 2 present + 1 late + 1 half day × 0.5 = 3.5 of 6
        (42, 100),
        (43, None),
        (44, None),
    ]
    assert report.by_week[1].height_percent == 16  # (58 − 50) / 50
    rows = {row.number: row for row in report.workers}
    assert (rows["W-0001"].present, rows["W-0001"].late, rows["W-0001"].earned) == (2, 1, 15_000)
    assert (rows["W-0002"].half, rows["W-0002"].absent, rows["W-0002"].rate) == (1, 1, 50)
    assert rows["W-0002"].days == Decimal("1.5")
    assert rows["W-0004"].rate == 0
    assert rows["W-0004"].earned == 0


def test_trade_filter(seeded):
    report = build_report(Site.objects.all(), *OCT, trade=seeded["mason"])

    assert {row.number for row in report.workers} == {"W-0001", "W-0003", "W-0004"}
    assert report.labour_cost == 21_000


def test_report_roles(client, seeded):
    for user, status in [
        (seeded["admin"], 200),
        (seeded["accountant"], 200),
        (seeded["manager_a"], 200),
        (f.ForemanFactory(), 403),
    ]:
        client.force_login(user)
        assert client.get(reverse("dashboard:reports")).status_code == status
        assert client.get(reverse("dashboard:reports_csv")).status_code == status


def test_reports_page(client, seeded):
    client.force_login(seeded["admin"])

    response = client.get(reverse("dashboard:reports"))

    html = response.content.decode()
    assert response.status_code == 200
    assert "1 Oct 2026 – 31 Oct 2026" in html
    assert "29 000 XOF" in html
    assert "5 273 XOF" in html
    assert "5.5" in html
    assert "Akouvi Tete" in html and "Route 4 Bridge" in html
    assert "week 41 58%" in html
    assert 'href="/reports/attendance.csv"' in html


def test_site_manager_only_sees_their_sites(client, seeded):
    client.force_login(seeded["manager_a"])

    response = client.get(reverse("dashboard:reports"))

    report = response.context["report"]
    assert [item.name for item in report.by_site] == ["Riverside"]
    assert {row.site_name for row in report.workers} == {"Riverside"}
    assert "Route 4 Bridge" not in response.content.decode()


def test_site_manager_cannot_pick_another_site(client, seeded):
    client.force_login(seeded["manager_a"])

    response = client.get(reverse("dashboard:reports"), {"site": seeded["bridge"].pk})

    assert "site" in response.context["form"].errors
    assert {row.site_name for row in response.context["report"].workers} == {"Riverside"}
    csv_response = client.get(reverse("dashboard:reports_csv"), {"site": seeded["bridge"].pk})
    assert "Route 4 Bridge" not in csv_response.content.decode("utf-8-sig")


def test_site_filter_and_custom_period(client, seeded):
    client.force_login(seeded["admin"])

    response = client.get(
        reverse("dashboard:reports"),
        {
            "period": "custom",
            "start": "2026-10-06",
            "end": "2026-10-06",
            "site": seeded["bridge"].pk,
        },
    )

    report = response.context["report"]
    assert (report.start, report.end) == (datetime.date(2026, 10, 6), datetime.date(2026, 10, 6))
    assert {row.number for row in report.workers} == {"W-0003", "W-0004"}
    assert report.labour_cost == 6_000


def test_custom_period_needs_valid_dates(client, seeded):
    client.force_login(seeded["admin"])

    response = client.get(
        reverse("dashboard:reports"),
        {"period": "custom", "start": "2026-10-10", "end": "2026-10-01"},
    )

    assert "end" in response.context["form"].errors
    assert (response.context["report"].start, response.context["report"].end) == OCT


def test_csv_export_columns_and_totals(client, seeded):
    client.force_login(seeded["admin"])

    response = client.get(reverse("dashboard:reports_csv"))

    assert response.status_code == 200
    assert response["Content-Type"] == "text/csv; charset=utf-8"
    assert response["Content-Disposition"] == (
        'attachment; filename="polaris-attendance-2026-10-01-2026-10-31.csv"'
    )
    assert response.content.startswith("﻿".encode())
    rows = list(csv.reader(io.StringIO(response.content.decode("utf-8-sig"))))
    assert rows[0] == CSV_HEADER
    body, total = rows[1:-1], rows[-1]
    assert [row[0] for row in body] == ["W-0004", "W-0001", "W-0002", "W-0003"]  # by name
    assert body[1] == [
        "W-0001",
        "Akouvi Tete",
        "Mason",
        "Riverside",
        "2",
        "0",
        "1",
        "0",
        "3",
        "100",
        "15000",
        "0",
        "0",
        "XOF",
    ]
    assert body[2][4:11] == ["1", "1", "0", "1", "1.5", "50", "8000"]
    assert total == [
        "",
        "Total",
        "",
        "",
        "4",
        "1",
        "1",
        "2",
        "5.5",
        "69",
        "29000",
        "0",
        "0",
        "XOF",
    ]
    assert sum(int(row[10]) for row in body) == int(total[10])


def test_csv_export_with_two_decimals(client, seeded):
    company = CompanySettings.load()
    company.currency, company.currency_decimals = "EUR", 2
    company.save()
    client.force_login(seeded["admin"])

    rows = list(
        csv.reader(io.StringIO(client.get(reverse("dashboard:reports_csv")).content.decode()))
    )

    assert rows[-1][10:] == ["290.00", "0.00", "0.00", "EUR"]


def test_period_dates():
    today = datetime.date(2026, 10, 20)  # a Tuesday

    assert period_dates("week", today) == (datetime.date(2026, 10, 19), datetime.date(2026, 10, 25))
    assert period_dates("month", today) == OCT
    assert period_dates("last_month", today) == (
        datetime.date(2026, 9, 1),
        datetime.date(2026, 9, 30),
    )
    assert period_dates("month", datetime.date(2026, 12, 5))[1] == datetime.date(2026, 12, 31)


def test_labour_cost_includes_correction_adjustments(seeded):
    # A day paid on 6 Oct was corrected to absent: −3 000 recovered on W-0001's 12 Oct pay.
    from apps.payroll.models import PayLine

    line = PayLine.objects.get(
        worker__number="W-0001", period__end_date=datetime.date(2026, 10, 12)
    )
    line.adjustment = -3_000
    line.save(update_fields=["adjustment"])

    report = build_report(Site.objects.all(), *OCT)

    assert report.labour_cost == 26_000
    assert report.by_site[0].cost == 20_000
    rows = {row.number: row for row in report.workers}
    assert rows["W-0001"].earned == 12_000
