"""GET /api/v1/sites/{site_id}/crew/ — scoping, shape, dates."""

import datetime
from unittest import mock

import pytest
from django.core.files.base import ContentFile
from django.urls import reverse
from rest_framework.test import APIClient

from apps.attendance.models import AttendanceRecord
from apps.core.choices import PayType
from tests.factories import (
    AttendanceDayFactory,
    AttendanceRecordFactory,
    ForemanDeviceFactory,
    ForemanFactory,
    PayRateFactory,
    SiteAssignmentFactory,
    SiteFactory,
    SiteManagerFactory,
    TradeFactory,
    WorkerFactory,
)

pytestmark = pytest.mark.django_db

D = datetime.date


def _url(site_id):
    return reverse("api:worksites:crew", kwargs={"site_id": site_id})


@pytest.fixture
def foreman():
    return ForemanFactory()


@pytest.fixture
def site(foreman):
    return SiteFactory(foremen=[foreman])


@pytest.fixture
def client(foreman):
    api = APIClient()
    api.force_authenticate(foreman)
    return api


def test_crew_shape_inside_scope(client, site):
    mason = TradeFactory(name="Mason")
    yao = WorkerFactory(first_name="Yao", last_name="Mensah", trade=mason)
    kodjo = WorkerFactory(first_name="Kodjo", last_name="Atsu", trade=None)
    for worker in (yao, kodjo):
        SiteAssignmentFactory(site=site, worker=worker, start_date=D(2026, 3, 1))
    PayRateFactory(worker=yao, pay_type=PayType.HOURLY, effective_from=D(2026, 1, 1))
    for n, date in enumerate([D(2026, 10, 29), D(2026, 10, 30)]):
        day = AttendanceDayFactory(site=site, date=date)
        AttendanceRecordFactory(day=day, worker=kodjo, status=AttendanceRecord.Status.ABSENT)
        AttendanceRecordFactory(
            day=day, worker=yao, status=AttendanceRecord.Status.PRESENT if n else "absent"
        )

    response = client.get(_url(site.pk), {"date": "2026-10-31"})

    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"site_id", "date", "count", "workers"}
    assert (body["site_id"], body["date"], body["count"]) == (site.pk, "2026-10-31", 2)
    first, second = body["workers"]
    assert set(first) == {
        "id",
        "number",
        "first_name",
        "last_name",
        "trade",
        "photo_url",
        "pay_type",
        "absent_streak",
    }
    assert [w["last_name"] for w in body["workers"]] == ["Atsu", "Mensah"]
    assert first == {
        "id": kodjo.pk,
        "number": kodjo.number,
        "first_name": "Kodjo",
        "last_name": "Atsu",
        "trade": None,
        "photo_url": None,
        "pay_type": "daily",  # no rate: the company default
        "absent_streak": 2,
    }
    assert second["trade"] == "Mason"
    assert second["pay_type"] == "hourly"
    assert second["absent_streak"] == 0


def test_crew_respects_dates_and_inactive_workers(client, site):
    SiteAssignmentFactory(site=site, start_date=D(2026, 11, 2))
    SiteAssignmentFactory(site=site, start_date=D(2026, 3, 1), end_date=D(2026, 10, 30))
    SiteAssignmentFactory(site=site, worker=WorkerFactory(is_active=False))
    on = SiteAssignmentFactory(site=site, start_date=D(2026, 10, 31)).worker

    body = client.get(_url(site.pk), {"date": "2026-10-31"}).json()

    assert [w["id"] for w in body["workers"]] == [on.pk]
    assert client.get(_url(site.pk), {"date": "2026-11-02"}).json()["count"] == 2


def test_crew_defaults_to_today_in_the_company_time_zone(client, site):
    SiteAssignmentFactory(site=site, start_date=D(2026, 3, 1))
    with mock.patch("apps.worksites.services.company_today", return_value=D(2026, 10, 31)):
        body = client.get(_url(site.pk)).json()
    assert body["date"] == "2026-10-31"
    assert body["count"] == 1


def test_crew_photo_url_is_absolute(client, site, settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path
    worker = SiteAssignmentFactory(site=site).worker
    worker.photo.save("yao.jpg", ContentFile(b"\xff\xd8\xffjpeg"), save=True)

    body = client.get(_url(site.pk), {"date": "2026-10-31"}).json()
    # The protected API download (H2), not a public /media/ path.
    expected = f"http://testserver/api/v1/files/workers/{worker.pk}/photo/"
    assert body["workers"][0]["photo_url"] == expected
    photo = client.get(expected)
    assert photo.status_code == 200
    assert b"".join(photo.streaming_content) == b"\xff\xd8\xffjpeg"


def test_crew_outside_scope_is_403(client):
    other = SiteFactory()
    SiteAssignmentFactory(site=other)

    response = client.get(_url(other.pk))

    assert response.status_code == 403
    assert response.json()["code"] == "permission_denied"


def test_crew_for_a_missing_site_is_403_too(client):
    assert client.get(_url(999_999)).status_code == 403


def test_site_manager_sees_only_their_sites():
    manager = SiteManagerFactory()
    mine, other = SiteFactory(manager=manager), SiteFactory()
    api = APIClient()
    api.force_authenticate(manager)

    assert api.get(_url(mine.pk)).status_code == 200
    assert api.get(_url(other.pk)).status_code == 403


def test_bad_date_is_400(client, site):
    response = client.get(_url(site.pk), {"date": "31/10/2026"})

    assert response.status_code == 400
    body = response.json()
    assert body["code"] == "validation_error"
    assert "date" in body["details"]


def test_crew_needs_authentication(site):
    response = APIClient().get(_url(site.pk))
    assert response.status_code == 401


def test_crew_with_a_device_token(site, foreman):
    device = ForemanDeviceFactory(user=foreman, raw_token="tok-123")
    api = APIClient()
    api.credentials(HTTP_AUTHORIZATION="Device tok-123")

    assert device.user == foreman
    assert api.get(_url(site.pk)).status_code == 200
