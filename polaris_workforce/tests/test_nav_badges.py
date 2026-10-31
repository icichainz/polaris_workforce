"""The sidebar badges come from ``apps.core.context_processors.nav_badges``."""

import re

import pytest
from django.contrib.auth.models import AnonymousUser
from django.db import connection
from django.test.utils import CaptureQueriesContext

from apps.attendance.models import AttendanceDay
from apps.core.context_processors import NavBadges
from tests.factories import AdminUserFactory, AttendanceDayFactory, SiteFactory, UserFactory

pytestmark = pytest.mark.django_db

SUBMITTED = AttendanceDay.Status.SUBMITTED


def _approvals_badge(html: str) -> str | None:
    """The badge text of the Approvals sidebar entry, or None without a badge."""
    entry = re.search(r'<a href="/approvals/"[^>]*>(.*?)</a>', html, re.S)
    assert entry, "no Approvals entry in the sidebar"
    badge = re.search(r"rounded-full bg-amber-500[^>]*>(\d+)</span>", entry.group(1))
    return badge.group(1) if badge else None


@pytest.fixture
def manager_with_waiting_days():
    site = SiteFactory()
    other = SiteFactory()
    AttendanceDayFactory.create_batch(2, site=site, status=SUBMITTED)
    AttendanceDayFactory(site=site, status=AttendanceDay.Status.APPROVED)
    AttendanceDayFactory(site=site, status=AttendanceDay.Status.DRAFT)
    AttendanceDayFactory(site=other, status=SUBMITTED)  # not this manager's site
    return site.manager


def test_count_is_scoped_to_the_users_sites(manager_with_waiting_days):
    assert NavBadges(manager_with_waiting_days).approvals == 2
    assert NavBadges(AdminUserFactory()).approvals == 3


def test_count_is_one_query_and_cached(manager_with_waiting_days, django_assert_num_queries):
    badges = NavBadges(manager_with_waiting_days)
    with django_assert_num_queries(1):
        assert badges.approvals == 2
        assert badges.approvals == 2


def test_anonymous_gets_zero_without_a_query(django_assert_num_queries):
    with django_assert_num_queries(0):
        assert NavBadges(AnonymousUser()).approvals == 0
        assert NavBadges(None).approvals == 0


def test_anonymous_pages_do_not_query_for_badges(client):
    with CaptureQueriesContext(connection) as queries:
        response = client.get("/accounts/login/")
    assert response.status_code == 200
    assert not [q for q in queries.captured_queries if "attendance_attendanceday" in q["sql"]]


@pytest.mark.parametrize("url", ["/", "/workers/", "/sites/", "/approvals/", "/pay-periods/"])
def test_badge_shows_on_every_page(client, manager_with_waiting_days, url):
    client.force_login(manager_with_waiting_days)

    response = client.get(url)

    assert response.status_code == 200
    assert _approvals_badge(response.content.decode()) == "2"


def test_no_badge_when_nothing_waits(client):
    client.force_login(UserFactory(role="site_manager"))
    assert _approvals_badge(client.get("/workers/").content.decode()) is None
