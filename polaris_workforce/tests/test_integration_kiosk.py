"""Kiosk phones may only call the check-in endpoints (Wave 4 integration).

A kiosk is a foreman's phone left on the site for badge scans. With its token the server
answers ``403 permission_denied`` to every endpoint a kiosk does not need — roll call,
corrections, sync, payouts, payments — while the same foreman's ordinary phone keeps
working. Implemented once in ``DeviceTokenAuthentication`` (``apps.checkin.kiosk``)."""

import pytest
from django.core.cache import cache
from django.urls import get_resolver

from apps.checkin import kiosk
from tests.e2e import Phone, new_uuid
from tests.test_checkin_api import PIN, _make_kiosk, body, crew, phone, post  # noqa: F401

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _isolated(settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path
    cache.clear()
    yield
    cache.clear()


def refused(response, what):
    assert response.status_code == 403, (what, response.status_code, response.content)
    data = response.json()
    assert data["code"] == "permission_denied", what
    assert "kiosk mode" in data["message"], what


def blocked_calls(crew):  # noqa: F811
    site, day = crew.site.pk, crew.day.isoformat()
    return [
        ("get", f"/api/v1/sites/{site}/days/", {"from": day, "to": day}),
        ("get", f"/api/v1/sites/{site}/days/{day}/", None),
        (
            "put",
            f"/api/v1/sites/{site}/days/{day}/records/",
            {
                "records": [
                    {"client_uuid": new_uuid(), "worker_id": crew.ama.pk, "status": "present"}
                ]
            },
        ),
        ("post", f"/api/v1/sites/{site}/days/{day}/submit/", {"client_uuid": new_uuid()}),
        ("post", "/api/v1/records/1/corrections/", {"client_uuid": new_uuid()}),
        ("get", "/api/v1/sync/", None),
        ("get", f"/api/v1/sites/{site}/payouts/{day}/", None),
        ("post", "/api/v1/payments/", {"client_uuid": new_uuid(), "pay_line_id": 1}),
        ("get", "/api/v1/files/proofs/1/", None),
        ("put", f"/api/v1/sites/{site}/crew/", {}),
    ]


def call(client, method, url, data):
    if method == "get":
        return client.get(url, data or {})
    return getattr(client, method)(url, data, format="json")


def test_a_kiosk_phone_is_refused_everything_but_check_in(crew, phone):  # noqa: F811
    _make_kiosk(crew, phone)

    for method, url, data in blocked_calls(crew):
        refused(call(phone.client, method, url, data), f"{method.upper()} {url}")


def test_a_kiosk_phone_keeps_what_it_needs(crew, phone):  # noqa: F811
    _make_kiosk(crew, phone)
    site, day = crew.site.pk, crew.day.isoformat()

    assert phone.client.get("/api/v1/health/").status_code == 200
    assert phone.client.get("/api/v1/me/").status_code == 200
    assert phone.client.get("/api/v1/checkin/device/").json()["kiosk"]["site_id"] == site
    assert phone.client.get(f"/api/v1/sites/{site}/checkin-setup/").status_code == 200
    assert phone.client.get(f"/api/v1/sites/{site}/crew/").status_code == 200
    assert phone.client.get(f"/api/v1/sites/{site}/checkins/{day}/").status_code == 200
    assert phone.client.get(f"/api/v1/sites/{site}/days/{day}/suggestions/").status_code == 200
    assert post(phone.client, body(crew)).status_code == 201
    photo = phone.client.get(f"/api/v1/files/workers/{crew.ama.pk}/photo/")
    assert photo.status_code in (200, 404)  # no photo uploaded: 404, never 403
    assert phone.client.post("/api/v1/auth/logout/").status_code == 204


def test_the_same_foreman_on_another_phone_is_not_restricted(crew, phone):  # noqa: F811
    _make_kiosk(crew, phone)
    other = Phone(crew.phone_number, PIN)
    site, day = crew.site.pk, crew.day.isoformat()

    assert other.client.get(f"/api/v1/sites/{site}/days/{day}/").status_code == 200
    assert other.client.get("/api/v1/sync/").status_code == 200
    assert other.client.get(f"/api/v1/sites/{site}/payouts/{day}/").status_code == 200


def test_ending_kiosk_mode_lifts_the_restriction(crew, phone):  # noqa: F811
    from apps.checkin import services
    from apps.checkin.models import KioskDevice

    device = _make_kiosk(crew, phone)
    refused(phone.client.get("/api/v1/sync/"), "sync")
    services.disable_kiosk(KioskDevice.objects.get(device=device), crew.manager)

    assert phone.client.get("/api/v1/sync/").status_code == 200


def test_an_office_session_is_never_a_kiosk(crew):  # noqa: F811
    from rest_framework.test import APIClient

    session = APIClient()
    session.force_login(crew.foreman)
    assert session.get("/api/v1/sync/").status_code == 200


def test_every_api_endpoint_is_either_allowed_or_refused_on_purpose():
    """A new endpoint is refused to kiosks unless it is added to ``apps.checkin.kiosk``."""
    names = set()

    def walk(patterns, prefix=""):
        for p in patterns:
            if hasattr(p, "url_patterns"):
                walk(p.url_patterns, prefix + (f"{p.namespace}:" if p.namespace else ""))
            elif p.name:
                names.add(prefix + p.name)

    walk(get_resolver().url_patterns)
    api = {n for n in names if n.startswith("api:")}
    allowed = {n for n in api if kiosk.kiosk_may_call(n, "POST")}
    read_only = {n for n in api if kiosk.kiosk_may_call(n, "GET")} - allowed
    assert allowed == {
        "api:health",
        "api:schema",
        "api:not-found",
        "api:accounts:me",
        "api:accounts:auth-logout",
        "api:file-worker-photo",
        *{n for n in api if n.startswith("api:checkin:")},
    }
    assert read_only == {"api:worksites:crew"}
    assert "api:attendance:sync" in api - allowed - read_only
    assert "api:payroll:payments" in api - allowed - read_only
