"""Check-in API: create (idempotent, multipart photo), lists, suggestions, setup, kiosk
phones, photo downloads, scoping and cross-company isolation."""

import datetime
import hashlib
import zoneinfo

import pytest
from django.core.cache import cache
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse
from rest_framework.test import APIClient

from apps.accounts.models import ForemanDevice, User
from apps.checkin import services
from apps.checkin.api import examples as cx
from apps.checkin.api.serializers import (
    CheckInDeviceSerializer,
    CheckInListSerializer,
    CheckInSerializer,
    CheckInSetupSerializer,
    RollCallSuggestionsSerializer,
)
from apps.checkin.models import CheckIn
from apps.core.dates import company_today
from apps.core.tenancy import use_company
from tests.e2e import Phone, hire, make_site, new_uuid
from tests.factories import CompanyFactory, CompanySettingsFactory, SiteFactory, UserFactory

pytestmark = pytest.mark.django_db
PIN = "9173"
LOME = zoneinfo.ZoneInfo("Africa/Lome")
JPEG = b"\xff\xd8\xff\xe0" + b"0" * 64
PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 64


@pytest.fixture(autouse=True)
def _isolated(settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path
    cache.clear()
    yield
    cache.clear()


@pytest.fixture
def crew():
    CompanySettingsFactory(working_days=[1, 2, 3, 4, 5, 6, 7])
    crew = make_site("Riverside", phone_number="+228 90 11 22 33", pin=PIN)
    crew.ama = hire(crew, "Ama", 5000)
    crew.kofi = hire(crew, "Kofi", 5000)
    crew.day = company_today() - datetime.timedelta(days=1)
    crew.badge = services.issue_badge(crew.ama, crew.manager)
    return crew


@pytest.fixture
def phone(crew):
    return Phone(crew.phone_number, PIN)


def stamp(crew, hhmm):
    hour, minute = map(int, hhmm.split(":"))
    moment = datetime.datetime.combine(crew.day, datetime.time(hour, minute), tzinfo=LOME)
    return moment.isoformat()


def body(crew, **changes):
    data = {
        "client_uuid": new_uuid(),
        "site_id": crew.site.pk,
        "direction": "in",
        "at": stamp(crew, "07:02"),
        "badge": crew.badge.payload,
    }
    data.update(changes)
    return {k: v for k, v in data.items() if v is not None}


def post(client, data, multipart=False):
    return client.post("/api/v1/checkins/", data, format="multipart" if multipart else "json")


# --- create -------------------------------------------------------------------------------


def test_badge_scan_created_then_replayed(crew, phone):
    data = body(crew, latitude=6.132101, longitude=1.223012, accuracy_m=12)

    first = post(phone.client, data)
    again = post(phone.client, data)

    assert first.status_code == 201, first.content
    payload = first.json()
    assert payload["worker_id"] == crew.ama.pk
    assert payload["source"] == "foreman_scan"
    assert payload["counted"] is True and payload["flags"] == []
    assert payload["latitude"] == 6.132101 and payload["inside_geofence"] is None
    assert payload["date"] == crew.day.isoformat()
    assert again.status_code == 200 and again["Idempotent-Replayed"] == "true"
    assert again.json() == payload
    assert CheckIn.objects.count() == 1
    assert CheckIn.objects.get().device.device_id == phone.device_id
    assert CheckInSerializer(data=payload).is_valid()


def test_same_uuid_for_another_scan_is_409(crew, phone):
    data = body(crew)
    assert post(phone.client, data).status_code == 201
    response = post(phone.client, {**data, "direction": "out"})
    assert response.status_code == 409
    assert response.json()["details"] == {"client_uuid": data["client_uuid"]}


def test_manual_check_in(crew, phone):
    response = post(phone.client, body(crew, badge=None, worker_id=crew.kofi.pk))
    assert response.status_code == 201
    assert response.json()["source"] == "manual"


def test_unknown_badge_is_201_with_flag(crew, phone):
    response = post(phone.client, body(crew, badge="PW1:AAAAAAAAAAAAAAAA.BBBBBBBBBBBBBBBB"))
    assert response.status_code == 201
    assert response.json()["worker_id"] is None
    assert response.json()["counted"] is False
    assert response.json()["flags"] == ["unknown_badge"]


@pytest.mark.parametrize(
    ("changes", "field"),
    [
        ({"worker_id": 1}, "non_field_errors"),  # badge and worker_id
        ({"badge": None}, "non_field_errors"),  # neither
        ({"latitude": 6.1}, "latitude"),  # without longitude
        ({"latitude": 91, "longitude": 1}, "latitude"),
        ({"direction": "sideways"}, "direction"),
        ({"at": "07:00"}, "at"),
        ({"client_uuid": "nope"}, "client_uuid"),
    ],
)
def test_bad_bodies_are_400(crew, phone, changes, field):
    response = post(phone.client, body(crew, **changes))
    assert response.status_code == 400, response.content
    assert response.json()["code"] == "validation_error"
    assert field in response.json()["details"]


def test_future_time_is_400(crew, phone):
    future = (datetime.datetime.now(LOME) + datetime.timedelta(hours=1)).isoformat()
    response = post(phone.client, body(crew, at=future))
    assert response.status_code == 400
    assert "at" in response.json()["details"]


def test_worker_of_another_company_is_400(crew, phone):
    other = CompanyFactory()
    with use_company(other):
        from tests.factories import WorkerFactory

        stranger = WorkerFactory()
    response = post(phone.client, body(crew, badge=None, worker_id=stranger.pk))
    assert response.status_code == 400
    assert response.json()["details"] == {"worker_id": ["Unknown worker."]}


def test_site_not_assigned_is_403(crew, phone):
    other = SiteFactory()
    response = post(phone.client, body(crew, site_id=other.pk))
    assert response.status_code == 403
    assert response.json()["code"] == "permission_denied"
    for url in (
        f"/api/v1/sites/{other.pk}/checkins/{crew.day}/",
        f"/api/v1/sites/{other.pk}/days/{crew.day}/suggestions/",
        f"/api/v1/sites/{other.pk}/checkin-setup/",
    ):
        assert phone.client.get(url).status_code == 403, url


@pytest.mark.parametrize("role", [User.Role.SITE_MANAGER, User.Role.ACCOUNTANT])
def test_office_sessions_cannot_create_check_ins(crew, role):
    client = APIClient()
    user = crew.manager if role == User.Role.SITE_MANAGER else UserFactory(role=role)
    client.force_login(user)
    assert post(client, body(crew)).status_code == 403
    assert not CheckIn.objects.exists()


def test_no_token_is_401(crew):
    assert post(APIClient(), body(crew)).status_code == 401


# --- photos ------------------------------------------------------------------------------


def test_photo_upload_and_scoped_download(crew, phone):
    photo = SimpleUploadedFile("me.jpg", JPEG, content_type="image/jpeg")
    response = post(phone.client, {**body(crew), "photo": photo}, multipart=True)

    assert response.status_code == 201, response.content
    url = response.json()["photo_url"]
    checkin = CheckIn.objects.get()
    assert url.endswith(reverse("api:checkin:file-photo", args=[checkin.pk]))
    assert "me.jpg" not in checkin.photo.name  # random file name
    download = phone.client.get(url)
    assert download.status_code == 200
    assert download["Content-Type"] == "image/jpeg"
    assert b"".join(download.streaming_content) == JPEG
    assert phone.client.get(url, HTTP_IF_NONE_MATCH=download["ETag"]).status_code == 304

    # Another site's foreman, and a site manager of another site: 404.
    other = make_site("Elsewhere", phone_number="+228 90 44 55 66", pin=PIN)
    assert Phone(other.phone_number, PIN).client.get(url).status_code == 404
    office = APIClient()
    office.force_login(other.manager)
    assert office.get(url).status_code == 404
    # The office page of the photo follows the same rule.
    office_url = reverse("checkin:photo", args=[checkin.pk])
    office.force_login(crew.manager)
    assert office.get(office_url).status_code == 200
    office.force_login(other.manager)
    assert office.get(office_url).status_code == 404


def test_check_in_without_photo_has_no_photo_url(crew, phone):
    response = post(phone.client, body(crew))
    checkin_id = response.json()["id"]
    assert response.json()["photo_url"] is None
    url = reverse("api:checkin:file-photo", args=[checkin_id])
    assert phone.client.get(url).status_code == 404


@pytest.mark.parametrize(
    ("name", "content"),
    [
        ("proof.pdf", b"%PDF-1.4" + b"0" * 64),  # not an image
        ("page.jpg", b"<html><script>alert(1)</script></html>"),  # lies about its type
        ("photo.png", JPEG),  # a JPEG named .png
        ("big.jpg", JPEG + b"0" * (5 * 1024 * 1024)),  # over 5 MB
        ("evil.svg", b"<svg></svg>"),
    ],
)
def test_bad_photos_are_refused(crew, phone, name, content):
    photo = SimpleUploadedFile(name, content)
    response = post(phone.client, {**body(crew), "photo": photo}, multipart=True)
    assert response.status_code == 400
    assert "photo" in response.json()["details"]
    assert not CheckIn.objects.exists()


def test_png_photo_is_accepted(crew, phone):
    photo = SimpleUploadedFile("me.png", PNG)
    response = post(phone.client, {**body(crew), "photo": photo}, multipart=True)
    assert response.status_code == 201


def test_company_admin_media_file_covers_checkin_photos(crew, phone):
    from tests.factories import AdminUserFactory

    photo = SimpleUploadedFile("me.jpg", JPEG)
    post(phone.client, {**body(crew), "photo": photo}, multipart=True)
    name = CheckIn.objects.get().photo.name
    url = reverse("core:media_file", args=[name])
    admin = APIClient()
    admin.force_login(AdminUserFactory())
    assert admin.get(url).status_code == 200
    other = CompanyFactory()
    with use_company(other):
        stranger_admin = AdminUserFactory()
    admin.force_login(stranger_admin)
    assert admin.get(url).status_code == 404


# --- reads ---------------------------------------------------------------------------------


def test_day_list_with_duplicate_flag(crew, phone):
    post(phone.client, body(crew, at=stamp(crew, "07:00")))
    post(phone.client, body(crew, at=stamp(crew, "07:03")))
    post(phone.client, body(crew, badge=None, worker_id=crew.kofi.pk, at=stamp(crew, "07:30")))

    response = phone.client.get(f"/api/v1/sites/{crew.site.pk}/checkins/{crew.day}/")

    assert response.status_code == 200
    data = response.json()
    assert [c["flags"] for c in data["checkins"]] == [[], ["duplicate"], []]
    assert CheckInListSerializer(data=data).is_valid()


def test_suggestions_endpoint(crew, phone):
    post(phone.client, body(crew, at=stamp(crew, "07:05")))
    post(phone.client, body(crew, direction="out", at=stamp(crew, "11:30")))
    post(phone.client, body(crew, badge=None, worker_id=crew.kofi.pk, at=stamp(crew, "07:40")))

    response = phone.client.get(f"/api/v1/sites/{crew.site.pk}/days/{crew.day}/suggestions/")

    assert response.status_code == 200
    data = response.json()
    assert data["attendance_mode"] == "foreman_roll_call"
    assert data["late_after"] == "07:15" and data["day_status"] == "not_started"
    by_worker = {s["worker_id"]: s for s in data["suggestions"]}
    assert by_worker[crew.ama.pk]["status"] == "half_day"
    assert by_worker[crew.ama.pk]["arrival"] == "07:05"
    assert by_worker[crew.ama.pk]["departure"] == "11:30"
    assert by_worker[crew.kofi.pk]["status"] == "late"
    assert RollCallSuggestionsSerializer(data=data).is_valid()


def test_suggestions_prefill_then_the_foreman_submits(crew, phone):
    """End to end in mode both: scan, read suggestions, PUT them as records, submit."""
    from apps.core.models import CompanySettings

    settings = CompanySettings.load()
    settings.attendance_mode = "both"
    settings.save()
    post(phone.client, body(crew, at=stamp(crew, "07:05")))
    post(phone.client, body(crew, badge=None, worker_id=crew.kofi.pk, at=stamp(crew, "07:01")))
    suggestions = phone.client.get(
        f"/api/v1/sites/{crew.site.pk}/days/{crew.day}/suggestions/"
    ).json()["suggestions"]
    records = [
        {
            "client_uuid": new_uuid(),
            "worker_id": s["worker_id"],
            "status": s["status"],
            "arrival": s["arrival"],
            "departure": s["departure"],
            "note": "",
        }
        for s in suggestions
    ]
    assert phone.put_records(crew.site.pk, crew.day, records).status_code == 200
    assert phone.submit(crew.site.pk, crew.day, new_uuid()).status_code == 200


def test_self_check_in_over_the_api(crew, phone):
    from apps.attendance.models import AttendanceRecord
    from apps.core.models import CompanySettings

    settings = CompanySettings.load()
    settings.attendance_mode = "self_check_in"
    settings.save()

    response = post(phone.client, body(crew, at=stamp(crew, "07:30")))

    assert response.json()["draft_updated"] is True
    record = AttendanceRecord.objects.get(worker=crew.ama)
    assert record.status == "late" and record.day.status == "draft"
    day = phone.day(crew.site.pk, crew.day).json()
    assert day["status"] == "draft"
    assert [r["status"] for r in day["records"]] == ["late"]


def test_setup_lists_hashes_of_active_badges_of_the_crew(crew, phone):
    services.issue_badge(crew.kofi, crew.manager)
    revoked = services.issue_badge(crew.kofi, crew.manager)  # reissued: the first is revoked
    services.set_geofence(
        crew.site, latitude="6.131944", longitude="1.222778", radius_m=150, actor=crew.manager
    )

    data = phone.client.get(f"/api/v1/sites/{crew.site.pk}/checkin-setup/").json()

    assert data["kiosk"] is False
    assert data["geofence"] == {"latitude": 6.131944, "longitude": 1.222778, "radius_m": 150}
    hashes = {b["worker_id"]: b["payload_sha256"] for b in data["badges"]}
    assert hashes == {
        crew.ama.pk: hashlib.sha256(crew.badge.payload.encode()).hexdigest(),
        crew.kofi.pk: hashlib.sha256(revoked.payload.encode()).hexdigest(),
    }
    assert crew.badge.payload not in str(data)  # the phone never gets the badge text
    assert CheckInSetupSerializer(data=data).is_valid()


# --- kiosk phones -----------------------------------------------------------------------


def _make_kiosk(crew, phone, site=None):
    device = ForemanDevice.objects.get(device_id=phone.device_id)
    services.enable_kiosk(device, site or crew.site, crew.manager)
    return device


def test_device_endpoint(crew, phone):
    assert phone.client.get("/api/v1/checkin/device/").json() == {
        "attendance_mode": "foreman_roll_call",
        "kiosk": None,
    }
    _make_kiosk(crew, phone)
    data = phone.client.get("/api/v1/checkin/device/").json()
    assert data["kiosk"]["site_id"] == crew.site.pk
    assert data["kiosk"]["site_name"] == "Riverside"
    assert CheckInDeviceSerializer(data=data).is_valid()
    session = APIClient()
    session.force_login(crew.manager)
    assert session.get("/api/v1/checkin/device/").json()["kiosk"] is None


def test_kiosk_token_only_works_for_its_site(crew, phone):
    other = SiteFactory(manager=crew.manager, name="Second site")
    other.foremen.add(crew.foreman)
    _make_kiosk(crew, phone)

    ok = post(phone.client, body(crew))
    assert ok.status_code == 201 and ok.json()["source"] == "kiosk"
    elsewhere = post(phone.client, body(crew, site_id=other.pk))
    assert elsewhere.status_code == 403
    assert elsewhere.json()["message"] == "This phone is a kiosk for another site."
    manual = post(phone.client, body(crew, badge=None, worker_id=crew.kofi.pk))
    assert manual.status_code == 400 and "badge" in manual.json()["details"]
    for url in (
        f"/api/v1/sites/{other.pk}/checkins/{crew.day}/",
        f"/api/v1/sites/{other.pk}/days/{crew.day}/suggestions/",
        f"/api/v1/sites/{other.pk}/checkin-setup/",
    ):
        assert phone.client.get(url).status_code == 403, url
    setup = phone.client.get(f"/api/v1/sites/{crew.site.pk}/checkin-setup/").json()
    assert setup["kiosk"] is True
    # The same foreman on another phone is not restricted.
    other_phone = Phone(crew.phone_number, PIN)
    assert post(other_phone.client, body(crew, site_id=other.pk)).status_code == 201


def test_kiosk_mode_survives_signing_in_again(crew, phone):
    _make_kiosk(crew, phone)
    again = Phone(crew.phone_number, PIN, device_id=phone.device_id)
    assert again.client.get("/api/v1/checkin/device/").json()["kiosk"] is not None


# --- across companies ------------------------------------------------------------------------


def test_company_b_cannot_see_or_use_company_a(crew, phone):
    post(phone.client, body(crew, photo=None))
    checkin = CheckIn.objects.get()
    company_b = CompanyFactory(name="Bravo Co")
    with use_company(company_b):
        CompanySettingsFactory(company_name="Bravo Co")
        b = make_site("Bravo Tower", phone_number="+228 90 77 88 99", pin=PIN)
        hire(b, "Zeb", 5000)
    b_phone = Phone(b.phone_number, PIN)

    # A's badge scanned on B's site is unknown there.
    scan = post(b_phone.client, body(crew, site_id=b.site.pk, client_uuid=new_uuid()))
    assert scan.status_code == 201 and scan.json()["flags"] == ["unknown_badge"]
    # A's site and files are out of reach.
    for url in (
        f"/api/v1/sites/{crew.site.pk}/checkins/{crew.day}/",
        f"/api/v1/sites/{crew.site.pk}/days/{crew.day}/suggestions/",
        f"/api/v1/sites/{crew.site.pk}/checkin-setup/",
    ):
        assert b_phone.client.get(url).status_code == 403, url
    assert post(b_phone.client, body(crew)).status_code == 403
    assert b_phone.client.get(f"/api/v1/files/checkins/{checkin.pk}/photo/").status_code == 404
    # Replaying A's client_uuid from B: 409 without data.
    replay = post(
        b_phone.client, body(crew, site_id=b.site.pk, client_uuid=str(checkin.client_uuid))
    )
    assert replay.status_code == 409
    assert "worker_id" not in replay.json()["details"]


# --- contract ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("serializer", "data"),
    [
        (CheckInSerializer, cx.CHECKIN),
        (CheckInSerializer, cx.UNKNOWN),
        (CheckInListSerializer, {"site_id": 3, "date": "2026-10-31", "checkins": [cx.AFI_IN]}),
        (RollCallSuggestionsSerializer, cx.SUGGESTIONS),
        (CheckInSetupSerializer, cx.SETUP),
        (CheckInDeviceSerializer, cx.DEVICE_KIOSK),
        (CheckInDeviceSerializer, cx.DEVICE_PHONE),
    ],
)
def test_examples_match_their_serializers(serializer, data):
    checked = serializer(data=data)
    assert checked.is_valid(), checked.errors


def test_example_badge_text_has_the_badge_format():
    from apps.checkin.tokens import _TOKEN, PREFIX

    assert cx.DELALI_BADGE.startswith(PREFIX)
    assert _TOKEN.fullmatch(cx.DELALI_BADGE[len(PREFIX) :])


def test_schema_documents_the_check_in_endpoints():
    schema = APIClient().get(reverse("api:schema"), {"format": "json"}).json()
    create = schema["paths"]["/api/v1/checkins/"]["post"]
    assert set(create["requestBody"]["content"]) == {"multipart/form-data", "application/json"}
    assert {"201", "200", "400", "401", "403", "409"} <= set(create["responses"])
    assert schema["components"]["schemas"]["FlagsEnum"]["enum"] == [
        "unknown_badge",
        "revoked_badge",
        "not_on_crew",
        "outside_geofence",
        "duplicate",
    ]
    status = schema["components"]["schemas"]["RollCallSuggestion"]["properties"]["status"]
    assert "AttendanceStatusEnum" in str(status)
