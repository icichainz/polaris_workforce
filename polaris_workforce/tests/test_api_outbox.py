"""The phone's outbox survives a logout (docs/api/README.md, "Logout and the outbox").

Writes belong to the user and the site, not to the device token: what was queued before a
logout is accepted after the same user signs in again on the same phone (new token), and
replays of writes sent before the logout still return the original result.
"""

import uuid

import pytest
from django.core.cache import cache
from rest_framework.test import APIClient

from apps.attendance import selectors
from apps.attendance.models import AttendanceDay, AttendanceRecord
from apps.core.models import AuditLog
from apps.payroll.models import Payment
from tests.factories import CompanySettingsFactory, ForemanFactory
from tests.test_payroll_services import MON, hire, roll_call, setup_site

PIN = "4821"
PHONE = "+22890112233"
DEVICE_ID = "0b6f4c4e-61a2-4d5b-9a50-8f3c0f5d2a11"


@pytest.fixture(autouse=True)
def _clear_cache():
    cache.clear()
    yield
    cache.clear()


@pytest.fixture
def site_setup(db, settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path
    settings.PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]
    CompanySettingsFactory(time_zone="Africa/Lome")
    site, manager, foreman = setup_site()
    foreman.phone = PHONE
    foreman.set_pin(PIN)
    foreman.save()
    yao, afi = hire(site, 6000, first_name="Yao"), hire(site, 3500, first_name="Afi")
    return site, manager, foreman, yao, afi


def sign_in():
    response = APIClient().post(
        "/api/v1/auth/pin/",
        {"phone": PHONE, "pin": PIN, "device_id": DEVICE_ID},
        format="json",
    )
    assert response.status_code == 200, response.content
    return APIClient(HTTP_AUTHORIZATION=f"Device {response.json()['token']}")


def log_out(client):
    assert client.post("/api/v1/auth/logout/").status_code == 204


def day_url(site, date, action=""):
    return f"/api/v1/sites/{site.pk}/days/{date.isoformat()}/{action}"


def test_roll_call_queued_before_logout_is_accepted_after_signing_in_again(site_setup):
    site, _manager, foreman, yao, afi = site_setup
    today = selectors.company_today()
    first = sign_in()
    # Marked offline, before the logout: the outbox holds these two writes.
    sent = {
        "records": [{"client_uuid": str(uuid.uuid4()), "worker_id": yao.pk, "status": "present"}]
    }
    queued = {
        "records": [{"client_uuid": str(uuid.uuid4()), "worker_id": afi.pk, "status": "late"}]
    }
    submit = {"client_uuid": str(uuid.uuid4()), "submitted_at": "2026-10-31T07:42:01Z"}
    assert first.put(day_url(site, today, "records/"), sent, format="json").status_code == 200
    log_out(first)

    # The old token is dead...
    assert first.put(day_url(site, today, "records/"), queued, format="json").status_code == 401
    # ...but the same user signed in again sends the outbox with the new token.
    second = sign_in()
    audit_entries = AuditLog.objects.count()
    replay = second.put(day_url(site, today, "records/"), sent, format="json")
    assert replay.status_code == 200
    assert AuditLog.objects.count() == audit_entries  # the replay changed nothing
    assert second.put(day_url(site, today, "records/"), queued, format="json").status_code == 200
    response = second.post(day_url(site, today, "submit/"), submit, format="json")

    assert response.status_code == 200, response.content
    assert response.json()["status"] == "submitted"
    day = AttendanceDay.objects.get(site=site, date=today)
    assert day.submitted_by == foreman
    assert AttendanceRecord.objects.filter(day=day).count() == 2


def test_payments_queued_before_logout_are_accepted_after_signing_in_again(site_setup):
    site, _manager, foreman, yao, afi = site_setup
    roll_call(site, MON, {yao: "present", afi: "half_day"})
    first = sign_in()
    sheet = first.get(f"/api/v1/sites/{site.pk}/payouts/{MON.isoformat()}/").json()
    lines = {line["worker"]["id"]: line for line in sheet["lines"]}

    def pay(client, worker, client_uuid):
        line = lines[worker.pk]
        body = {
            "client_uuid": client_uuid,
            "pay_line_id": line["pay_line_id"],
            "amount": line["net"],
            "proof_type": "signature",
            "paid_at": "2025-09-01T16:31:00Z",
        }
        return client.post("/api/v1/payments/", body, format="json")

    yao_uuid, afi_uuid = str(uuid.uuid4()), str(uuid.uuid4())
    assert pay(first, yao, yao_uuid).status_code == 201  # the response was lost
    log_out(first)

    second = sign_in()
    replay = pay(second, yao, yao_uuid)
    queued = pay(second, afi, afi_uuid)

    assert replay.status_code == 200
    assert replay["Idempotent-Replayed"] == "true"
    assert queued.status_code == 201
    assert Payment.objects.count() == 2
    assert set(Payment.objects.values_list("paid_by", flat=True)) == {foreman.pk}


def test_another_user_cannot_replay_a_payment(site_setup):
    site, _manager, _foreman, yao, afi = site_setup
    roll_call(site, MON, {yao: "present", afi: "present"})
    first = sign_in()
    sheet = first.get(f"/api/v1/sites/{site.pk}/payouts/{MON.isoformat()}/").json()
    line = next(line for line in sheet["lines"] if line["worker"]["id"] == yao.pk)
    body = {
        "client_uuid": str(uuid.uuid4()),
        "pay_line_id": line["pay_line_id"],
        "amount": line["net"],
        "proof_type": "signature",
    }
    assert first.post("/api/v1/payments/", body, format="json").status_code == 201
    colleague = ForemanFactory()
    site.foremen.add(colleague)
    other = APIClient()
    other.force_authenticate(colleague)

    response = other.post("/api/v1/payments/", body, format="json")

    assert response.status_code == 409
    assert response.json()["code"] == "conflict"
