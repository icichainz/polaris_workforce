"""Attendance API: days, records, submit, corrections, sync — to the contract."""

import datetime
import uuid
from types import SimpleNamespace

import pytest
from django.core.cache import cache
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APIClient

from apps.attendance import selectors, services
from apps.attendance.models import AttendanceCorrection, AttendanceDay, AttendanceRecord
from tests.factories import (
    CompanySettingsFactory,
    ForemanDeviceFactory,
    ForemanFactory,
    SiteAssignmentFactory,
    SiteFactory,
    SiteManagerFactory,
)

UUID = "6f1c2b9e-1d1a-4c55-9a52-3f0e8a9d7b10"


@pytest.fixture(autouse=True)
def _fast_passwords(settings):
    # Factories hash a password per user; the default hasher is slow on purpose.
    settings.PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]


@pytest.fixture(autouse=True)
def _clear_cache():
    cache.clear()
    yield
    cache.clear()


@pytest.fixture
def crew(db):
    CompanySettingsFactory(time_zone="Africa/Lome")
    today = selectors.company_today()
    foreman = ForemanFactory()
    manager = SiteManagerFactory()
    site = SiteFactory(manager=manager, foremen=[foreman])
    workers = [
        SiteAssignmentFactory(site=site, start_date=today - datetime.timedelta(days=30)).worker
        for _ in range(3)
    ]
    client = APIClient()
    client.force_authenticate(foreman)
    return SimpleNamespace(
        site=site, foreman=foreman, manager=manager, workers=workers, today=today, client=client
    )


def url(name, **kwargs):
    return reverse(f"api:attendance:{name}", kwargs=kwargs)


def day_url(crew, name="day", date=None):
    return url(name, site_id=crew.site.pk, date=(date or crew.today).isoformat())


def batch(workers, status="present", **extra):
    return {
        "records": [
            {
                "client_uuid": str(uuid.uuid4()),
                "worker_id": w.pk,
                "status": status,
                "arrival": "07:00",
                **extra,
            }
            for w in workers
        ]
    }


def put_marks(crew, body=None):
    body = body or batch(crew.workers)
    return crew.client.put(day_url(crew, "day-records"), body, format="json")


def submit(crew, client_uuid=None, note=""):
    body = {"client_uuid": client_uuid or str(uuid.uuid4()), "foreman_note": note}
    return crew.client.post(day_url(crew, "day-submit"), body, format="json")


def assert_error(response, status_code, code):
    assert response.status_code == status_code, response.content
    body = response.json()
    assert set(body) == {"code", "message", "details"}
    assert body["code"] == code
    return body


# --- Authentication and scoping --------------------------------------------------------

ENDPOINTS = [
    ("get", "days", {"site_id": 3}, None),
    ("get", "day", {"site_id": 3, "date": "2026-10-31"}, None),
    ("put", "day-records", {"site_id": 3, "date": "2026-10-31"}, {"records": []}),
    ("post", "day-submit", {"site_id": 3, "date": "2026-10-31"}, {"client_uuid": UUID}),
    (
        "post",
        "record-corrections",
        {"record_id": 9001},
        {"client_uuid": UUID, "field": "note", "new_value": "x", "reason": "y"},
    ),
    ("get", "sync", {}, None),
]


@pytest.mark.parametrize(("method", "name", "kwargs", "body"), ENDPOINTS)
def test_every_endpoint_requires_authentication(db, method, name, kwargs, body):
    response = getattr(APIClient(), method)(url(name, **kwargs), body, format="json")

    assert_error(response, 401, "not_authenticated")
    assert response["WWW-Authenticate"] == "Device"


def test_device_token_works(crew):
    device = ForemanDeviceFactory(user=crew.foreman, raw_token="phone-token-1")
    client = APIClient(HTTP_AUTHORIZATION="Device phone-token-1")
    assert device.user == crew.foreman

    assert client.get(day_url(crew)).status_code == 200


@pytest.mark.parametrize("name", ["days", "day", "day-records", "day-submit"])
def test_foreman_of_another_site_gets_403(crew, name):
    other = SiteFactory(foremen=[ForemanFactory()])
    kwargs = {"site_id": other.pk}
    if name != "days":
        kwargs["date"] = crew.today.isoformat()
    method = {"day-records": "put", "day-submit": "post"}.get(name, "get")
    body = {"day-records": batch([]), "day-submit": {"client_uuid": UUID}}.get(name)

    response = getattr(crew.client, method)(url(name, **kwargs), body, format="json")

    assert_error(response, 403, "permission_denied")
    assert not AttendanceDay.objects.exists()


def test_foreman_of_another_site_cannot_correct(crew):
    put_marks(crew)
    submit(crew)
    record = AttendanceRecord.objects.first()
    client = APIClient()
    client.force_authenticate(ForemanFactory())

    response = client.post(
        url("record-corrections", record_id=record.pk),
        {"client_uuid": UUID, "field": "status", "new_value": "absent", "reason": "Gone"},
        format="json",
    )

    assert_error(response, 403, "permission_denied")


def test_unknown_record_is_404(crew):
    response = crew.client.post(
        url("record-corrections", record_id=999999),
        {"client_uuid": UUID, "field": "status", "new_value": "absent", "reason": "Gone"},
        format="json",
    )

    assert_error(response, 404, "not_found")


def test_site_manager_reads_but_does_not_write_over_the_api(crew):
    client = APIClient()
    client.force_authenticate(crew.manager)

    assert client.get(day_url(crew)).status_code == 200
    response = client.put(day_url(crew, "day-records"), batch(crew.workers), format="json")
    assert_error(response, 403, "permission_denied")


# --- Days ------------------------------------------------------------------------------


def test_day_not_started(crew):
    response = crew.client.get(day_url(crew))

    assert response.status_code == 200
    body = response.json()
    assert body["id"] is None
    assert body["status"] == "not_started"
    assert body["records"] == []
    assert body["counts"] == {
        "expected": 3,
        "present": 0,
        "absent": 0,
        "half_day": 0,
        "late": 0,
        "unmarked": 3,
    }
    assert body["payout_status"] == "not_open"
    assert body["updated_at"] is None


def test_days_list_defaults_to_the_last_7_days(crew):
    put_marks(crew, batch(crew.workers[:2], "late"))

    response = crew.client.get(url("days", site_id=crew.site.pk))

    assert response.status_code == 200
    body = response.json()
    assert body["site_id"] == crew.site.pk
    dates = [d["date"] for d in body["days"]]
    assert dates == [(crew.today - datetime.timedelta(days=i)).isoformat() for i in range(7)]
    today = body["days"][0]
    assert today["status"] == "draft"
    assert today["counts"]["late"] == 2 and today["counts"]["unmarked"] == 1
    assert "records" not in today
    assert body["days"][1]["status"] == "not_started"


def test_days_list_range(crew):
    start = crew.today - datetime.timedelta(days=2)
    query = {"from": start.isoformat(), "to": crew.today.isoformat()}

    response = crew.client.get(url("days", site_id=crew.site.pk), query)

    assert len(response.json()["days"]) == 3


@pytest.mark.parametrize(
    "query",
    [
        {"from": "2026-10-01", "to": "2026-11-15"},  # more than 31 days
        {"from": "2026-10-10", "to": "2026-10-01"},  # backwards
        {"from": "yesterday"},
    ],
)
def test_days_list_bad_range(crew, query):
    response = crew.client.get(url("days", site_id=crew.site.pk), query)

    assert_error(response, 400, "validation_error")


# --- Records ---------------------------------------------------------------------------


def test_put_records_creates_the_draft(crew):
    body = batch(crew.workers[:1], "half_day", departure="12:00", note="Clinic")

    response = put_marks(crew, body)

    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "draft"
    (record,) = data["records"]
    assert record["client_uuid"] == body["records"][0]["client_uuid"]
    assert record["status"] == "half_day"
    assert (record["arrival"], record["departure"]) == ("07:00", "12:00")
    assert record["note"] == "Clinic"
    assert record["corrected"] is False
    assert data["counts"]["half_day"] == 1 and data["counts"]["unmarked"] == 2


def test_replaying_the_same_put_twice_changes_nothing(crew):
    body = batch(crew.workers)

    first = put_marks(crew, body).json()
    second = put_marks(crew, body).json()

    assert first == second
    assert AttendanceRecord.objects.count() == 3


def test_put_returns_the_servers_client_uuid(crew):
    first = put_marks(crew).json()
    other_phone = batch(crew.workers[:1], "absent")

    data = put_marks(crew, other_phone).json()

    record = next(r for r in data["records"] if r["worker_id"] == crew.workers[0].pk)
    original = next(r for r in first["records"] if r["worker_id"] == crew.workers[0].pk)
    assert record["client_uuid"] == original["client_uuid"]
    assert record["status"] == "absent"


def test_put_to_a_submitted_day_is_423_locked(crew):
    put_marks(crew)
    submit(crew)

    response = put_marks(crew, batch(crew.workers[:1], "absent"))

    body = assert_error(response, 423, "locked")
    assert body["details"] == {"status": "submitted"}


def test_put_worker_not_on_crew_is_400(crew):
    stranger = SiteAssignmentFactory().worker

    response = put_marks(crew, batch([crew.workers[0], stranger]))

    body = assert_error(response, 400, "validation_error")
    assert body["details"]["records"][0] == {}
    assert "worker_id" in body["details"]["records"][1]
    assert not AttendanceDay.objects.exists()


def test_put_bad_status_is_400(crew):
    response = put_marks(crew, batch(crew.workers[:1], "P"))

    body = assert_error(response, 400, "validation_error")
    # DRF keys list-item errors by index here (see the report), not a list.
    assert "status" in body["details"]["records"]["0"]


# --- Submit ----------------------------------------------------------------------------


def test_submit_with_unmarked_workers_is_409(crew):
    put_marks(crew, batch(crew.workers[:1]))

    body = assert_error(submit(crew), 409, "conflict")

    assert body["details"]["unmarked_worker_ids"] == sorted(w.pk for w in crew.workers[1:])


def test_submit_not_started_day_with_crew_is_409_and_creates_nothing(crew):
    assert_error(submit(crew), 409, "conflict")
    assert not AttendanceDay.objects.exists()


def test_submit_and_replay(crew):
    put_marks(crew)
    client_uuid = str(uuid.uuid4())

    first = submit(crew, client_uuid, "Concrete late")
    second = submit(crew, client_uuid, "Concrete late")

    assert first.status_code == second.status_code == 200
    assert first.json() == second.json()
    data = first.json()
    assert data["status"] == "submitted"
    assert data["foreman_note"] == "Concrete late"
    assert data["submitted_by"] == {"id": crew.foreman.pk, "name": str(crew.foreman)}
    assert len(data["records"]) == 3


def test_submit_twice_with_another_uuid_is_409(crew):
    put_marks(crew)
    submit(crew)

    assert_error(submit(crew), 409, "conflict")


def test_submit_locked_day_is_423(crew):
    put_marks(crew)
    submit(crew)
    day = AttendanceDay.objects.get()
    services.approve_day(day, crew.manager)
    services.lock_day(day)

    assert_error(submit(crew), 423, "locked")


# --- Corrections -----------------------------------------------------------------------


def _submitted_record(crew):
    put_marks(crew)
    submit(crew)
    return AttendanceRecord.objects.get(worker=crew.workers[0])


def correct(crew, record, **body):
    data = {"client_uuid": str(uuid.uuid4()), "field": "status", "new_value": "late"}
    data.update(body)
    return crew.client.post(url("record-corrections", record_id=record.pk), data, format="json")


def test_correction_created_then_replayed(crew):
    record = _submitted_record(crew)
    client_uuid = str(uuid.uuid4())

    first = correct(
        crew, record, client_uuid=client_uuid, field="arrival", new_value="07:20", reason="Gate log"
    )
    second = correct(
        crew, record, client_uuid=client_uuid, field="arrival", new_value="07:20", reason="Gate log"
    )

    assert first.status_code == 201
    assert second.status_code == 200
    assert second["Idempotent-Replayed"] == "true"
    assert first.json() == second.json()
    data = first.json()
    assert data["client_uuid"] == client_uuid
    assert (data["old_value"], data["new_value"]) == ("07:00", "07:20")
    assert data["created_by"]["id"] == crew.foreman.pk
    assert data["record"]["arrival"] == "07:20"
    assert data["record"]["corrected"] is True
    assert AttendanceCorrection.objects.count() == 1


def test_correction_of_status_uses_api_words(crew):
    record = _submitted_record(crew)

    data = correct(crew, record, new_value="half_day", reason="Left at noon").json()

    assert (data["old_value"], data["new_value"]) == ("present", "half_day")
    assert data["record"]["status"] == "half_day"


def test_correction_clearing_a_time(crew):
    record = _submitted_record(crew)

    data = correct(crew, record, field="arrival", new_value=None, reason="Absent really").json()

    assert data["new_value"] is None
    assert data["record"]["arrival"] is None


def test_correction_without_reason_is_400(crew):
    record = _submitted_record(crew)

    body = assert_error(correct(crew, record, reason=""), 400, "validation_error")

    assert "reason" in body["details"]
    assert not AttendanceCorrection.objects.exists()


def test_correction_on_a_draft_is_409(crew):
    put_marks(crew)
    record = AttendanceRecord.objects.first()

    assert_error(correct(crew, record, reason="Typo"), 409, "conflict")


def test_correction_after_lock_is_423(crew):
    record = _submitted_record(crew)
    day = record.day
    services.approve_day(day, crew.manager)
    services.lock_day(day)

    assert_error(correct(crew, record, reason="Typo"), 423, "locked")


def test_correction_client_uuid_reused_for_another_record_is_409(crew):
    record = _submitted_record(crew)
    other = AttendanceRecord.objects.exclude(pk=record.pk).first()
    correct(crew, record, client_uuid=UUID, reason="Typo")

    assert_error(correct(crew, other, client_uuid=UUID, reason="Typo"), 409, "conflict")


# --- Sync ------------------------------------------------------------------------------


def test_sync_without_since_returns_the_last_14_days(crew):
    put_marks(crew)
    old = AttendanceDay.objects.create(
        site=crew.site, date=crew.today - datetime.timedelta(days=20), status="approved"
    )
    other_site_day = AttendanceDay.objects.create(site=SiteFactory(), date=crew.today)

    response = crew.client.get(url("sync"))

    assert response.status_code == 200
    data = response.json()
    assert data["since"] is None
    assert data["server_time"]
    ids = [d["id"] for d in data["days"]]
    assert old.pk not in ids and other_site_day.pk not in ids
    assert len(ids) == 1
    assert len(data["records"]) == 3
    assert data["days"][0]["counts"]["present"] == 3


def test_sync_returns_only_changes_after_since(crew):
    put_marks(crew)
    submit(crew)
    hour_ago = timezone.now() - datetime.timedelta(hours=1)
    AttendanceDay.objects.update(updated_at=hour_ago)
    AttendanceRecord.objects.update(updated_at=hour_ago)
    first = crew.client.get(url("sync")).json()
    since = first["server_time"]

    nothing = crew.client.get(url("sync"), {"since": since}).json()
    record = AttendanceRecord.objects.get(worker=crew.workers[1])
    correct(crew, record, new_value="absent", reason="Went home")
    changes = crew.client.get(url("sync"), {"since": since}).json()

    assert nothing["days"] == [] and nothing["records"] == [] and nothing["corrections"] == []
    assert [r["id"] for r in changes["records"]] == [record.pk]
    assert [d["id"] for d in changes["days"]] == [record.day_id]
    assert len(changes["corrections"]) == 1
    assert changes["corrections"][0]["record"]["status"] == "absent"
    assert changes["since"] is not None


def test_sync_shows_office_approval(crew):
    put_marks(crew)
    submit(crew)
    since = crew.client.get(url("sync")).json()["server_time"]

    services.approve_day(AttendanceDay.objects.get(), crew.manager)
    data = crew.client.get(url("sync"), {"since": since}).json()

    assert [d["status"] for d in data["days"]] == ["approved"]
    assert data["days"][0]["approved_by"]["id"] == crew.manager.pk


def test_sync_bad_since_is_400(crew):
    assert_error(crew.client.get(url("sync"), {"since": "last week"}), 400, "validation_error")
