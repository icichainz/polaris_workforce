"""The shared error format produced by apps.api.exceptions.handler."""

import pytest
from django.core.exceptions import PermissionDenied as DjangoPermissionDenied
from django.core.exceptions import ValidationError as DjangoValidationError
from django.http import Http404
from rest_framework import exceptions

from apps.api.exceptions import Conflict, Locked, NotImplementedYet, handler


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
