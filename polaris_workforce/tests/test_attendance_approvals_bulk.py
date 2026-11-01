"""Approvals screen: approve several submitted days at once."""

import datetime
from unittest import mock

import pytest
from django.contrib.messages import get_messages
from django.urls import reverse
from django.utils import timezone

from apps.api.exceptions import Conflict
from apps.attendance import selectors, services
from apps.attendance.models import AttendanceDay, AttendanceRecord
from apps.core.models import AuditLog
from apps.core.tenancy import use_company
from tests.e2e import Office
from tests.factories import (
    AdminUserFactory,
    CompanyFactory,
    CompanySettingsFactory,
    ForemanFactory,
    SiteAssignmentFactory,
    SiteFactory,
    SiteManagerFactory,
    UserFactory,
)
from tests.test_attendance_approvals import submitted_day

pytestmark = pytest.mark.django_db

URL = reverse("attendance:bulk_approve")


@pytest.fixture
def world(db):
    CompanySettingsFactory(time_zone="Africa/Lome")
    today = selectors.company_today()
    manager, foreman = SiteManagerFactory(), ForemanFactory()

    def day_on(name, days_ago, *, manager=manager, **submit):
        site = SiteFactory(name=name, manager=manager, foremen=[foreman])
        return submitted_day(site, foreman, today - datetime.timedelta(days=days_ago), **submit)

    clean_a = day_on("Alpha Court", 2)
    clean_b = day_on("Beta Yard", 1)
    corrected = day_on("Corrected Hall", 1)
    AttendanceRecord.objects.filter(day=corrected).update(corrected=True)
    late = day_on(
        "Late Sync Plaza",
        1,
        client_submitted_at=timezone.now() - datetime.timedelta(hours=3),
    )
    unmarked = day_on("Unmarked Works", 1)
    SiteAssignmentFactory(site=unmarked.site, start_date=unmarked.date)  # hired, not marked
    theirs = day_on("Route 4 Bridge", 1, manager=SiteManagerFactory())
    draft = services.get_or_create_day(clean_a.site, today, foreman)
    return {
        "manager": manager,
        "foreman": foreman,
        "clean": [clean_a, clean_b],
        "corrected": corrected,
        "late": late,
        "unmarked": unmarked,
        "theirs": theirs,
        "draft": draft,
    }


def statuses(*days):
    return [AttendanceDay.objects.get(pk=day.pk).status for day in days]


def texts(response):
    return [str(message) for message in get_messages(response.wsgi_request)]


def test_approves_the_clean_days_and_skips_the_others_with_reasons(world):
    office = Office(world["manager"])
    picked = [*world["clean"], world["corrected"], world["late"], world["unmarked"], world["draft"]]

    response = office.post(URL, {"days": [day.pk for day in picked]})

    assert response.status_code == 302
    assert response["Location"] == reverse("attendance:approvals")
    assert statuses(*world["clean"]) == ["approved", "approved"]
    assert statuses(world["corrected"], world["late"], world["unmarked"]) == ["submitted"] * 3
    assert statuses(world["draft"]) == ["draft"]
    approvals = AuditLog.objects.filter(action="attendance.day.approve")
    assert {entry.object_id for entry in approvals} == {str(d.pk) for d in world["clean"]}
    assert all(entry.actor == world["manager"] for entry in approvals)

    messages = texts(response)
    assert messages[0] == "2 days approved."
    skipped = " ".join(messages[1:])
    assert "Corrected Hall" in skipped and "changed after submission" in skipped
    assert "Late Sync Plaza" in skipped and "Synced 3 h after submission" in skipped
    assert "Unmarked Works" in skipped and "1 worker not marked" in skipped
    assert "not waiting for approval (Draft)" in skipped
    assert len(messages) == 5


def test_a_day_of_another_managers_site_makes_the_whole_request_404(world):
    office = Office(world["manager"])

    response = office.post(URL, {"days": [world["clean"][0].pk, world["theirs"].pk]})

    assert response.status_code == 404
    assert statuses(world["clean"][0], world["theirs"]) == ["submitted", "submitted"]
    assert not AuditLog.objects.filter(action="attendance.day.approve").exists()


@pytest.mark.parametrize("value", ["999999", "abc"])
def test_unknown_ids_are_404(world, value):
    response = Office(world["manager"]).post(URL, {"days": [value]})

    assert response.status_code == 404


def test_admin_approves_across_sites(world):
    office = Office(AdminUserFactory())

    office.post(URL, {"days": [world["clean"][0].pk, world["theirs"].pk]})

    assert statuses(world["clean"][0], world["theirs"]) == ["approved", "approved"]


@pytest.mark.parametrize("role", ["foreman", "accountant"])
def test_other_roles_are_forbidden(world, role):
    user = world["foreman"] if role == "foreman" else UserFactory(role="accountant")

    response = Office(user).post(URL, {"days": [world["clean"][0].pk]})

    assert response.status_code == 403
    assert statuses(world["clean"][0]) == ["submitted"]


def test_nothing_selected(world):
    response = Office(world["manager"]).post(URL, {})

    assert response.status_code == 302
    assert texts(response) == ["Select at least one day."]


def test_too_many_days(world, monkeypatch):
    from apps.attendance.forms import BulkApproveForm

    monkeypatch.setattr(BulkApproveForm, "MAX_DAYS", 1)

    response = Office(world["manager"]).post(URL, {"days": [d.pk for d in world["clean"]]})

    assert texts(response) == ["Select at most 1 days at a time."]
    assert statuses(*world["clean"]) == ["submitted", "submitted"]


def test_a_day_the_service_refuses_is_skipped(world):
    real = services.approve_day
    first, second = world["clean"]

    def refuse_first(day, user):
        if day.pk == first.pk:
            raise Conflict("Someone approved it a moment ago.")
        return real(day, user)

    with mock.patch.object(services, "approve_day", side_effect=refuse_first):
        response = Office(world["manager"]).post(URL, {"days": [first.pk, second.pk]})

    assert statuses(first, second) == ["submitted", "approved"]
    assert "Someone approved it a moment ago." in " ".join(texts(response))


def test_a_day_with_workers_already_paid_is_skipped(world):
    day = world["clean"][0]

    with mock.patch.object(selectors, "paid_notes", return_value={1: "note"}):
        response = Office(world["manager"]).post(URL, {"days": [day.pk]})

    assert statuses(day) == ["submitted"]
    assert "1 worker already paid for this day" in " ".join(texts(response))


def test_htmx_gets_a_redirect_header(world):
    response = Office(world["manager"]).post(URL, {"days": [world["clean"][0].pk]}, htmx=True)

    assert response.status_code == 204
    assert response["HX-Redirect"] == reverse("attendance:approvals")


def test_boosted_form_gets_a_plain_redirect(world):
    response = Office(world["manager"]).post(
        URL, {"days": [world["clean"][0].pk]}, HTTP_HX_REQUEST="true", HTTP_HX_BOOSTED="true"
    )

    assert response.status_code == 302
    assert response.url == reverse("attendance:approvals")
    assert "HX-Redirect" not in response


def test_bulk_approve_needs_post(world):
    assert Office(world["manager"]).get(URL).status_code == 405


def test_another_company_cannot_approve_our_days(world):
    with use_company(CompanyFactory()):
        CompanySettingsFactory()
        other_admin = AdminUserFactory()

    response = Office(other_admin).post(URL, {"days": [world["clean"][0].pk]})

    assert response.status_code == 404
    assert statuses(world["clean"][0]) == ["submitted"]


def test_the_board_has_checkboxes_and_select_all(world):
    response = Office(world["manager"]).get(reverse("attendance:approvals"))

    html = response.content.decode()
    assert f'action="{URL}"' in html
    assert "Select all" in html and "Approve selected" in html
    for day in world["clean"]:
        assert f'name="days" value="{day.pk}"' in html
    assert f'value="{world["theirs"].pk}"' not in html
    assert response.context["card_ids"] == [str(c["day"].pk) for c in response.context["cards"]]


def test_no_bulk_form_when_nothing_waits(db):
    CompanySettingsFactory()
    html = Office(SiteManagerFactory()).get(reverse("attendance:approvals")).content.decode()

    assert "Approve selected" not in html
