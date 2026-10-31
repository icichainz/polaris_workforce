"""Sites screens: list, detail tabs, settings tab, new site, assigning workers, scoping."""

import datetime
from unittest import mock

import pytest
from django.urls import reverse

from apps.attendance.models import AttendanceDay
from apps.core.choices import PayFrequency
from apps.core.models import AuditLog
from apps.worksites.models import Site
from tests.factories import (
    AdminUserFactory,
    AttendanceDayFactory,
    AttendanceRecordFactory,
    CashIssueFactory,
    ForemanFactory,
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

TODAY = datetime.date(2026, 10, 30)  # a Friday


@pytest.fixture(autouse=True)
def _fixed_today():
    with mock.patch("apps.core.dates.company_today", return_value=TODAY):
        yield


@pytest.fixture
def admin_client(client):
    client.force_login(AdminUserFactory())
    return client


@pytest.fixture
def busy_site():
    foreman = ForemanFactory(first_name="Kossi", last_name="Amegah")
    site = SiteFactory(name="Riverside Apartments", foremen=[foreman])
    workers = [SiteAssignmentFactory(site=site).worker for _ in range(3)]
    for worker in workers:
        PayRateFactory(worker=worker, amount=6000)
    day = AttendanceDayFactory(
        site=site, date=TODAY, foreman=foreman, status=AttendanceDay.Status.SUBMITTED
    )
    AttendanceRecordFactory(day=day, worker=workers[0], status="present")
    AttendanceRecordFactory(day=day, worker=workers[1], status="absent")
    period = PayPeriodFactory(site=site, start_date=datetime.date(2026, 10, 1))
    PayLineFactory(period=period, worker=workers[0], gross=6000)
    CashIssueFactory(site=site, amount=150_000)
    return site


# --- render for an admin ----------------------------------------------------------------


@pytest.mark.parametrize(
    "name", ["detail", "crew", "attendance", "pay", "settings", "assign", "edit"]
)
def test_site_screens_render_for_admin(admin_client, busy_site, name):
    response = admin_client.get(reverse(f"worksites:{name}", args=[busy_site.pk]))

    assert response.status_code == 200
    html = response.content.decode()
    assert "Riverside Apartments" in html
    assert 'href="/sites/" aria-current="page"' in html


def test_site_list_shows_cards_with_counts(admin_client, busy_site):
    SiteFactory(status=Site.Status.PAUSED, name="Route 4 Bridge")

    html = admin_client.get(reverse("worksites:list")).content.decode()

    assert "Riverside Apartments" in html
    assert "Route 4 Bridge" not in html  # paused tab not selected
    assert "1 active · 1 paused · 0 completed" in html
    assert "1 / 3" in html  # present today / assigned
    assert "company default" in html
    assert reverse("worksites:create") in html


def test_site_list_tabs_and_search(admin_client, busy_site):
    SiteFactory(status=Site.Status.PAUSED, name="Route 4 Bridge")

    paused = admin_client.get(reverse("worksites:list"), {"status": "paused"}).content.decode()
    assert "Route 4 Bridge" in paused and "Riverside Apartments" not in paused

    found = admin_client.get(reverse("worksites:list"), {"q": "river"}).content.decode()
    assert "Riverside Apartments" in found
    missed = admin_client.get(reverse("worksites:list"), {"q": "nowhere"}).content.decode()
    assert "No site matches your search" in missed


def test_site_list_shows_site_override(admin_client):
    SiteFactory(pay_frequency=PayFrequency.WEEKLY)

    html = admin_client.get(reverse("worksites:list")).content.decode()

    assert "Weekly" in html and "site override" in html


def test_overview_shows_week_crew_people_and_pay_sources(admin_client, busy_site):
    html = admin_client.get(reverse("worksites:detail", args=[busy_site.pk])).content.decode()

    assert "This week" in html
    assert "Submitted" in html
    assert "Kossi Amegah" in html
    assert "P 1 · ½ 0 · L 0 · A 1" in html
    assert "6 000 XOF" in html
    assert "Company default" in html
    assert "Labour cost this month" in html


# --- scoping ----------------------------------------------------------------------------


def test_site_manager_cannot_open_another_sites_detail(client):
    manager = SiteManagerFactory()
    mine, other = SiteFactory(manager=manager), SiteFactory()
    client.force_login(manager)

    assert client.get(reverse("worksites:detail", args=[mine.pk])).status_code == 200
    for name in ("detail", "crew", "attendance", "pay", "settings", "assign"):
        assert client.get(reverse(f"worksites:{name}", args=[other.pk])).status_code == 403


def test_site_list_is_scoped(client):
    manager = SiteManagerFactory()
    SiteFactory(manager=manager, name="Mine")
    SiteFactory(name="Theirs")
    client.force_login(manager)

    html = client.get(reverse("worksites:list")).content.decode()

    assert "Mine" in html and "Theirs" not in html
    assert reverse("worksites:create") not in html


def test_only_admins_create_sites(client):
    client.force_login(SiteManagerFactory())
    assert client.get(reverse("worksites:create")).status_code == 403


def test_foreman_sees_settings_read_only(client):
    foreman = ForemanFactory()
    site = SiteFactory(foremen=[foreman])
    client.force_login(foreman)

    response = client.get(reverse("worksites:settings", args=[site.pk]))
    assert response.status_code == 200
    assert "Only an admin" in response.content.decode()
    post = client.post(reverse("worksites:settings", args=[site.pk]), {"pay_frequency": "weekly"})
    assert post.status_code == 403


# --- writes -----------------------------------------------------------------------------


def test_settings_tab_saves_overrides_and_blank_inherits(client):
    manager = SiteManagerFactory()
    site = SiteFactory(manager=manager)
    client.force_login(manager)
    url = reverse("worksites:settings", args=[site.pk])

    response = client.post(url, {"pay_frequency": "weekly", "work_start": "06:30"})

    assert response.status_code == 302
    site.refresh_from_db()
    assert site.pay_frequency == "weekly"
    assert site.work_start == datetime.time(6, 30)
    assert site.payment_method is None
    assert AuditLog.objects.filter(action="worksites.settings_update", actor=manager).exists()

    client.post(url, {"pay_frequency": ""})
    site.refresh_from_db()
    assert site.pay_frequency is None and site.work_start is None


def test_settings_tab_validates(client):
    manager = SiteManagerFactory()
    site = SiteFactory(manager=manager)
    client.force_login(manager)

    response = client.post(
        reverse("worksites:settings", args=[site.pk]),
        {"pay_frequency": "every_n_days", "work_start": "18:00"},
    )

    assert response.status_code == 200
    html = response.content.decode()
    assert "Set the number of days between pay days." in html
    assert "Work must end after it starts." in html


def test_admin_creates_a_site(admin_client):
    manager = SiteManagerFactory()
    foreman = ForemanFactory()

    response = admin_client.post(
        reverse("worksites:create"),
        {
            "name": "Market Hall",
            "address": "Central market",
            "status": "active",
            "started_on": "2026-03-03",
            "manager": manager.pk,
            "foremen": [foreman.pk],
            "labour_budget": "25 000 000",
        },
    )

    site = Site.objects.get(name="Market Hall")
    assert response.status_code == 302
    assert response.url == reverse("worksites:detail", args=[site.pk])
    assert site.labour_budget == 25_000_000
    assert list(site.foremen.all()) == [foreman]
    assert AuditLog.objects.filter(action="worksites.site_create").exists()


def test_assign_workers_from_the_site(client):
    manager = SiteManagerFactory()
    site = SiteFactory(manager=manager)
    free = WorkerFactory()  # not on any site yet: assignable by a manager
    elsewhere = SiteAssignmentFactory().worker  # another manager's worker
    client.force_login(manager)
    url = reverse("worksites:assign", args=[site.pk])

    html = client.get(url).content.decode()
    assert free.full_name in html and elsewhere.full_name not in html

    response = client.post(url, {"workers": [free.pk], "start_date": "2026-11-02"})
    assert response.status_code == 302
    assert free.assignments.get().site == site


def test_end_assignment_from_the_crew_tab(admin_client, busy_site):
    assignment = busy_site.assignments.first()

    response = admin_client.post(
        reverse("worksites:end_assignment", args=[busy_site.pk, assignment.pk]),
        {"end_date": "2026-10-30"},
    )

    assert response.status_code == 302
    assignment.refresh_from_db()
    assert assignment.end_date == datetime.date(2026, 10, 30)


def test_end_assignment_requires_post_and_manage_rights(client, busy_site):
    assignment = busy_site.assignments.first()
    url = reverse("worksites:end_assignment", args=[busy_site.pk, assignment.pk])
    client.force_login(UserFactory(role="accountant"))

    assert client.get(url).status_code == 405
    assert client.post(url).status_code == 403
