"""Approvals screen: scope, the detail panel, approve and send back."""

import datetime
import uuid

import pytest
from django.urls import reverse

from apps.attendance import selectors, services
from apps.attendance.models import AttendanceDay, AttendanceRecord
from apps.core.models import CompanySettings
from tests.factories import (
    AdminUserFactory,
    CompanySettingsFactory,
    ForemanFactory,
    SiteAssignmentFactory,
    SiteFactory,
    SiteManagerFactory,
    UserFactory,
)

URL = "/approvals/"


@pytest.fixture(autouse=True)
def _fast_passwords(settings):
    # Factories hash a password per user; the default hasher is slow on purpose.
    settings.PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]


def submitted_day(site, foreman, date, workers=2, **submit_kwargs):
    for _ in range(workers):
        SiteAssignmentFactory(site=site, start_date=date - datetime.timedelta(days=10))
    day = services.get_or_create_day(site, date, foreman)
    records = [
        {
            "client_uuid": uuid.uuid4(),
            "worker_id": worker_id,
            "status": AttendanceRecord.Status.PRESENT,
            "arrival": datetime.time(7, 0),
            "departure": None,
            "note": "",
        }
        for worker_id in sorted(selectors.expected_worker_ids(site, date))
    ]
    services.upsert_records(day, records, foreman)
    return services.submit_day(day, foreman, uuid.uuid4(), **submit_kwargs)


@pytest.fixture
def setup(db):
    CompanySettingsFactory(time_zone="Africa/Lome")
    today = selectors.company_today()
    manager = SiteManagerFactory()
    foreman = ForemanFactory()
    mine = SiteFactory(name="Riverside Apartments", manager=manager, foremen=[foreman])
    theirs = SiteFactory(name="Route 4 Bridge", foremen=[foreman])
    my_day = submitted_day(mine, foreman, today)
    their_day = submitted_day(theirs, foreman, today)
    draft = services.get_or_create_day(mine, today - datetime.timedelta(days=1), foreman)
    return {
        "manager": manager,
        "foreman": foreman,
        "mine": mine,
        "my_day": my_day,
        "their_day": their_day,
        "draft": draft,
        "today": today,
    }


def decide_url(day):
    return reverse("attendance:decide", kwargs={"day_id": day.pk})


def test_lists_only_submitted_days_in_scope(client, setup):
    client.force_login(setup["manager"])

    response = client.get(URL)

    html = response.content.decode()
    assert response.status_code == 200
    assert [c["day"] for c in response.context["cards"]] == [setup["my_day"]]
    assert "Riverside Apartments" in html
    assert "Route 4 Bridge" not in html


def test_admin_sees_every_site(client, setup):
    client.force_login(AdminUserFactory())

    response = client.get(URL)

    days = {c["day"] for c in response.context["cards"]}
    assert days == {setup["my_day"], setup["their_day"]}


@pytest.mark.parametrize("role", ["foreman", "accountant"])
def test_other_roles_are_forbidden(client, setup, role):
    client.force_login(UserFactory(role=role))

    assert client.get(URL).status_code == 403


def test_empty_state(client, db):
    client.force_login(SiteManagerFactory())

    response = client.get(URL)

    assert response.status_code == 200
    assert "Nothing to approve" in response.content.decode()


def test_cannot_open_a_day_of_another_site(client, setup):
    client.force_login(setup["manager"])

    assert client.get(URL, {"day": setup["their_day"].pk}).status_code == 404
    response = client.post(decide_url(setup["their_day"]), {"action": "approve"})
    assert response.status_code == 404
    setup["their_day"].refresh_from_db()
    assert setup["their_day"].status == AttendanceDay.Status.SUBMITTED


def test_detail_highlights_corrected_rows(client, setup):
    day = setup["my_day"]
    record = day.records.first()
    services.correct_record(
        record, "status", "half_day", "Left at noon, sick.", setup["foreman"], uuid.uuid4()
    )
    client.force_login(setup["manager"])

    response = client.get(URL, {"day": day.pk})

    html = response.content.decode()
    assert "1 changed after submission" in html
    assert "Status was Present → Half day." in html
    assert "Left at noon, sick." in html
    assert html.count("bg-amber-tint") == 1
    assert 'name="action" value="approve"' in html


def test_sync_lag_flag(client, setup):
    day = setup["my_day"]
    day.client_submitted_at = day.submitted_at - datetime.timedelta(hours=9, minutes=5)
    day.save()
    client.force_login(setup["manager"])

    html = client.get(URL).content.decode()

    assert "Synced 9 h after submission" in html


def test_approve(client, setup):
    client.force_login(setup["manager"])

    response = client.post(decide_url(setup["my_day"]), {"action": "approve"})

    assert response.status_code == 302
    day = AttendanceDay.objects.get(pk=setup["my_day"].pk)
    assert day.status == AttendanceDay.Status.APPROVED
    assert day.approved_by == setup["manager"]


def test_approve_with_htmx_redirects(client, setup):
    client.force_login(setup["manager"])

    response = client.post(
        decide_url(setup["my_day"]), {"action": "approve"}, HTTP_HX_REQUEST="true"
    )

    assert response.status_code == 204
    assert response["HX-Redirect"] == URL


def test_send_back_needs_a_note(client, setup):
    client.force_login(setup["manager"])

    response = client.post(decide_url(setup["my_day"]), {"action": "reject", "note": " "})

    assert response.status_code == 200
    assert "Tell the foreman what to check." in response.content.decode()
    setup["my_day"].refresh_from_db()
    assert setup["my_day"].status == AttendanceDay.Status.SUBMITTED


def test_send_back(client, setup):
    client.force_login(setup["manager"])

    client.post(decide_url(setup["my_day"]), {"action": "reject", "note": "Check Kodjo"})

    day = AttendanceDay.objects.get(pk=setup["my_day"].pk)
    assert day.status == AttendanceDay.Status.REJECTED
    assert day.rejection_note == "Check Kodjo"


def test_deciding_twice_shows_an_error(client, setup):
    client.force_login(setup["manager"])
    client.post(decide_url(setup["my_day"]), {"action": "approve"})

    response = client.post(
        decide_url(setup["my_day"]), {"action": "reject", "note": "x"}, follow=True
    )

    assert response.status_code == 200
    assert "cannot go from Approved to Rejected" in response.content.decode()


def test_decide_is_post_only(client, setup):
    client.force_login(setup["manager"])

    assert client.get(decide_url(setup["my_day"])).status_code == 405


def test_times_are_shown_in_the_company_time_zone(client, setup):
    CompanySettings.objects.update(time_zone="Asia/Tokyo")  # UTC+9
    day = setup["my_day"]
    day.submitted_at = datetime.datetime(2026, 10, 31, 7, 42, tzinfo=datetime.UTC)
    day.save()
    client.force_login(setup["manager"])

    html = client.get(URL).content.decode()

    assert "sent 16:42" in html
