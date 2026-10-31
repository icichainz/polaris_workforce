"""Every contract endpoint exists, is protected, and answers 501 in the error format."""

import pytest
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.urls import reverse
from rest_framework.test import APIClient

DAY = "2026-10-31"
UUID = "6f1c2b9e-1d1a-4c55-9a52-3f0e8a9d7b10"

# (method, url name, kwargs, body)
PROTECTED = [
    ("post", "api:accounts:auth-logout", {}, None),
    ("get", "api:accounts:me", {}, None),
    ("get", "api:worksites:crew", {"site_id": 3}, None),
    ("get", "api:attendance:days", {"site_id": 3}, None),
    ("get", "api:attendance:day", {"site_id": 3, "date": DAY}, None),
    (
        "put",
        "api:attendance:day-records",
        {"site_id": 3, "date": DAY},
        {"records": [{"client_uuid": UUID, "worker_id": 44, "status": "present"}]},
    ),
    (
        "post",
        "api:attendance:day-submit",
        {"site_id": 3, "date": DAY},
        {"client_uuid": UUID, "foreman_note": ""},
    ),
    (
        "post",
        "api:attendance:record-corrections",
        {"record_id": 9001},
        {"client_uuid": UUID, "field": "arrival", "new_value": "07:20", "reason": "Gate log"},
    ),
    ("get", "api:attendance:sync", {}, None),
    # Payroll (payout, payments) is implemented: see tests/test_payroll_api.py.
]

PIN_BODY = {"phone": "+22890112233", "pin": "4821", "device_id": "abc", "device_name": "Tecno"}


@pytest.fixture(autouse=True)
def _clear_throttle_cache():
    cache.clear()
    yield
    cache.clear()


@pytest.fixture
def foreman(db):
    return get_user_model().objects.create_user(username="kossi", password="s3cret-pass")


def _call(client, method, name, kwargs, body):
    return getattr(client, method)(reverse(name, kwargs=kwargs), body, format="json")


def _assert_error(response, status_code, code):
    assert response.status_code == status_code
    assert response["Content-Type"].startswith("application/json")
    body = response.json()
    assert set(body) == {"code", "message", "details"}
    assert body["code"] == code
    assert isinstance(body["message"], str) and body["message"]
    assert isinstance(body["details"], dict)
    return body


@pytest.mark.parametrize(("method", "name", "kwargs", "body"), PROTECTED)
def test_stub_returns_501_when_authenticated(foreman, method, name, kwargs, body):
    client = APIClient()
    client.force_authenticate(foreman)

    _assert_error(_call(client, method, name, kwargs, body), 501, "not_implemented")


@pytest.mark.parametrize(("method", "name", "kwargs", "body"), PROTECTED)
def test_stub_requires_authentication(db, method, name, kwargs, body):
    response = _call(APIClient(), method, name, kwargs, body)

    _assert_error(response, 401, "not_authenticated")
    assert response["WWW-Authenticate"] == "Device"


@pytest.mark.parametrize(("method", "name", "kwargs", "body"), PROTECTED[:3])
def test_stub_works_with_session_login(foreman, method, name, kwargs, body):
    client = APIClient(enforce_csrf_checks=False)
    client.force_login(foreman)

    _assert_error(_call(client, method, name, kwargs, body), 501, "not_implemented")


def test_pin_login_is_reachable_anonymously(db):
    response = APIClient().post(reverse("api:accounts:auth-pin"), PIN_BODY, format="json")

    _assert_error(response, 501, "not_implemented")


def test_pin_login_ignores_a_bad_device_token(db):
    client = APIClient(HTTP_AUTHORIZATION="Device not-a-real-token")

    response = client.post(reverse("api:accounts:auth-pin"), PIN_BODY, format="json")

    _assert_error(response, 501, "not_implemented")


def test_pin_login_is_throttled(db):
    client = APIClient()
    url = reverse("api:accounts:auth-pin")
    for _ in range(10):
        assert client.post(url, PIN_BODY, format="json").status_code == 501

    response = client.post(url, PIN_BODY, format="json")

    body = _assert_error(response, 429, "throttled")
    assert body["details"]["retry_after"] > 0
    assert int(response["Retry-After"]) > 0


def test_invalid_date_in_path_is_not_routed(foreman):
    client = APIClient()
    client.force_authenticate(foreman)

    assert client.get("/api/v1/sites/3/days/2026-02-30/").status_code == 404
    assert client.get("/api/v1/sites/3/days/31-10-2026/").status_code == 404


def test_method_not_allowed_uses_error_format(foreman):
    client = APIClient()
    client.force_authenticate(foreman)

    response = client.delete(reverse("api:accounts:me"))

    _assert_error(response, 405, "method_not_allowed")


def test_responses_are_json_only(foreman):
    client = APIClient()
    client.force_authenticate(foreman)

    response = client.get(reverse("api:accounts:me"), HTTP_ACCEPT="application/json")

    assert response["Content-Type"] == "application/json"
