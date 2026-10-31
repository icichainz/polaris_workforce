"""The shared error format produced by apps.api.exceptions.handler."""

import pytest
from django.core.exceptions import PermissionDenied as DjangoPermissionDenied
from django.core.exceptions import ValidationError as DjangoValidationError
from django.http import Http404
from rest_framework import exceptions

from apps.api.exceptions import Conflict, Locked, NotImplementedYet, handler
from tests.factories import ForemanFactory, SiteFactory


def _handle(exc):
    response = handler(exc, {"view": None})
    assert set(response.data) == {"code", "message", "details"}
    assert isinstance(response.data["message"], str)
    return response


@pytest.mark.parametrize(
    ("exc", "status_code", "code"),
    [
        (exceptions.NotAuthenticated(), 401, "not_authenticated"),
        (exceptions.AuthenticationFailed(), 401, "not_authenticated"),
        (exceptions.PermissionDenied(), 403, "permission_denied"),
        (DjangoPermissionDenied(), 403, "permission_denied"),
        (exceptions.NotFound(), 404, "not_found"),
        (Http404(), 404, "not_found"),
        (exceptions.ParseError(), 400, "validation_error"),
        (exceptions.MethodNotAllowed("DELETE"), 405, "method_not_allowed"),
        (exceptions.Throttled(wait=30), 429, "throttled"),
        (Conflict(), 409, "conflict"),
        (Locked(), 423, "locked"),
        (NotImplementedYet(), 501, "not_implemented"),
    ],
)
def test_status_and_code(exc, status_code, code):
    response = _handle(exc)

    assert response.status_code == status_code
    assert response.data["code"] == code


def test_validation_error_puts_field_errors_in_details():
    response = _handle(exceptions.ValidationError({"reason": ["This field is required."]}))

    assert response.status_code == 400
    assert response.data["message"] == "Invalid input."
    assert response.data["details"] == {"reason": ["This field is required."]}


def test_validation_error_without_fields_uses_non_field_errors():
    response = _handle(exceptions.ValidationError("Bad batch."))

    assert response.data["details"] == {"non_field_errors": ["Bad batch."]}


def test_django_validation_error_from_a_service():
    response = _handle(DjangoValidationError({"amount": "Too much."}))

    assert response.status_code == 400
    assert response.data["code"] == "validation_error"
    assert response.data["details"] == {"amount": ["Too much."]}


def test_custom_message_and_details():
    response = _handle(Locked("Day is locked.", details={"status": "locked"}))

    assert response.data == {
        "code": "locked",
        "message": "Day is locked.",
        "details": {"status": "locked"},
    }


def test_throttled_has_retry_after():
    response = _handle(exceptions.Throttled(wait=12.3))

    assert response.data["details"]["retry_after"] in (12, 13)
    assert int(response["Retry-After"]) in (12, 13)


def test_unexpected_exception_becomes_server_error(caplog):
    response = _handle(RuntimeError("boom"))

    assert response.status_code == 500
    assert response.data["code"] == "server_error"
    assert "boom" not in response.data["message"]
    assert "Unhandled error" in caplog.text


# --- List fields: details always use JSON lists -------------------------------------------


def test_list_item_errors_become_a_json_list():
    # What DRF raises for a `many=True` field: errors keyed by item index.
    detail = {"records": {1: {"worker_id": ["Not on the crew."]}, 3: {"status": ["Bad."]}}}

    response = _handle(exceptions.ValidationError(detail))

    assert response.data["details"] == {
        "records": [{}, {"worker_id": ["Not on the crew."]}, {}, {"status": ["Bad."]}]
    }


def test_nested_list_errors_and_plain_value_lists():
    detail = {
        "records": {0: {"tags": {2: ["Too long."]}}},
        "ids": {1: ["A valid integer is required."]},
        "reason": ["This field is required."],
    }

    details = _handle(exceptions.ValidationError(detail)).data["details"]

    assert details == {
        "records": [{"tags": [[], [], ["Too long."]]}],
        "ids": [[], ["A valid integer is required."]],
        "reason": ["This field is required."],
    }


def test_a_list_from_a_service_stays_a_list():
    # apps.attendance.services raises its batch errors as a list already.
    exc = exceptions.ValidationError({"records": [{}, {"worker_id": ["Not on the crew."]}]})

    assert _handle(exc).data["details"] == {"records": [{}, {"worker_id": ["Not on the crew."]}]}


def test_batch_errors_over_http_are_a_list(db, client):
    foreman = ForemanFactory()
    site = SiteFactory(foremen=[foreman])
    client.force_login(foreman)
    body = {
        "records": [
            {"client_uuid": "6f1c2b9e-1d1a-4c55-9a52-3f0e8a9d7b10", "worker_id": 1},
            {"client_uuid": "not-a-uuid", "worker_id": 2, "status": "present"},
        ]
    }

    response = client.put(
        f"/api/v1/sites/{site.pk}/days/2026-10-30/records/", body, content_type="application/json"
    )

    assert response.status_code == 400
    records = response.json()["details"]["records"]
    assert isinstance(records, list) and len(records) == 2
    assert set(records[0]) == {"status"}
    assert set(records[1]) == {"client_uuid"}


# --- Unknown URLs under /api/ -----------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        "/api/v1/nothing-here/",
        "/api/v1/sites/3/days/2026-02-30/",  # an impossible date matches no route
        "/api/v1/sites/abc/crew/",
        "/api/v1/",
    ],
)
def test_unknown_api_url_is_a_json_404(client, path):
    response = client.get(path)

    assert response.status_code == 404
    assert response["Content-Type"] == "application/json"
    assert response.json() == {"code": "not_found", "message": "Not found.", "details": {}}


def test_unknown_url_outside_v1_uses_the_json_handler(client):
    response = client.get("/api/v2/me/")

    assert response.status_code == 404
    assert response.json()["code"] == "not_found"


def test_unknown_api_url_message_is_translated(client):
    response = client.get("/api/v1/nothing-here/", headers={"accept-language": "fr"})

    assert response.json()["code"] == "not_found"
    assert response.json()["message"] != "Not found."


def test_missing_trailing_slash_still_redirects(client):
    response = client.get("/api/v1/health")

    assert response.status_code == 301
    assert response["Location"] == "/api/v1/health/"


def test_office_pages_keep_the_html_404(admin_client):
    response = admin_client.get("/no-such-office-page/")

    assert response.status_code == 404
    assert response["Content-Type"].startswith("text/html")
