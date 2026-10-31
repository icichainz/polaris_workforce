"""One error format for the whole API.

Every error response has the same body::

    {"code": "validation_error", "message": "Invalid input.", "details": {...}}

``code`` is stable (see ``apps.api.enums.ERROR_CODES``) and is what the phone branches
on. ``message`` is human-readable and translated (``Accept-Language``). ``details`` is
always an object: field errors for ``validation_error``, ``retry_after`` for
``throttled``, and whatever context the raising code passes otherwise.

Errors on the items of a list field are always a JSON list, one entry per item up to
the last item with an error (``{}`` for an item without errors)::

    {"records": [{}, {"worker_id": ["This worker is not on the crew..."]}]}

DRF reports them as a dict keyed by index (``{"records": {1: {...}}}``); ``_as_lists``
turns those into lists.

Unknown ``/api/`` URLs get the same format (``not_found_body``), see ``apps.api.views``.

Raise ``Conflict``, ``Locked`` or ``NotImplementedYet`` from views and services, with
optional ``details``. Django's ``Http404``, ``PermissionDenied`` and ``ValidationError``
are converted too, so services can raise plain Django exceptions.
"""

import logging

from django.core.exceptions import PermissionDenied as DjangoPermissionDenied
from django.core.exceptions import ValidationError as DjangoValidationError
from django.http import Http404
from django.utils.translation import gettext_lazy as _
from rest_framework import exceptions, status
from rest_framework.response import Response
from rest_framework.serializers import as_serializer_error
from rest_framework.views import exception_handler as drf_exception_handler

logger = logging.getLogger(__name__)


class PolarisAPIException(exceptions.APIException):
    """An API error that carries a ``details`` object."""

    def __init__(self, message=None, *, details=None, code=None):
        super().__init__(detail=message, code=code)
        self.details = details or {}


class Conflict(PolarisAPIException):
    status_code = status.HTTP_409_CONFLICT
    default_detail = _("The request conflicts with the current state of the resource.")
    default_code = "conflict"


class Locked(PolarisAPIException):
    status_code = status.HTTP_423_LOCKED
    default_detail = _("This resource is locked and cannot be changed.")
    default_code = "locked"


class NotImplementedYet(PolarisAPIException):
    status_code = status.HTTP_501_NOT_IMPLEMENTED
    default_detail = _("This endpoint is part of the API contract but is not implemented yet.")
    default_code = "not_implemented"


_CODES_BY_CLASS = [
    ((exceptions.NotAuthenticated, exceptions.AuthenticationFailed), "not_authenticated"),
    (exceptions.PermissionDenied, "permission_denied"),
    (exceptions.NotFound, "not_found"),
    ((exceptions.ValidationError, exceptions.ParseError), "validation_error"),
    (exceptions.Throttled, "throttled"),
]


def error_body(code: str, message, details: dict | None = None) -> dict:
    return {"code": code, "message": str(message), "details": details or {}}


def not_found_body() -> dict:
    """The body of a 404 for a URL that matches no API route."""
    return error_body("not_found", exceptions.NotFound.default_detail)


def _normalise(exc):
    """Turn Django exceptions into their DRF equivalents."""
    if isinstance(exc, Http404):
        return exceptions.NotFound()
    if isinstance(exc, DjangoPermissionDenied):
        return exceptions.PermissionDenied()
    if isinstance(exc, DjangoValidationError):
        return exceptions.ValidationError(as_serializer_error(exc))
    return exc


def _as_lists(detail):
    """Turn DRF's index-keyed list errors (``{0: {...}, 2: {...}}``) into JSON lists.

    Only dicts whose keys are all ``int`` are list errors: field names are strings.
    Items without an error become ``{}`` (or ``[]`` when the item errors are lists of
    messages, as for a ``ListField`` of plain values).
    """
    if isinstance(detail, dict):
        if detail and all(isinstance(key, int) for key in detail):
            values = {key: _as_lists(value) for key, value in detail.items()}
            empty = dict if any(isinstance(v, dict) for v in values.values()) else list
            return [
                values[index] if index in values else empty() for index in range(max(values) + 1)
            ]
        return {key: _as_lists(value) for key, value in detail.items()}
    if isinstance(detail, list):
        return [_as_lists(item) for item in detail]
    return detail


def _code_for(exc: exceptions.APIException) -> str:
    for classes, code in _CODES_BY_CLASS:
        if isinstance(exc, classes):
            return code
    return exc.default_code


def _message_and_details(exc: exceptions.APIException, code: str):
    if code == "validation_error" and isinstance(exc, exceptions.ValidationError):
        detail = exc.detail
        if not isinstance(detail, dict):
            detail = {"non_field_errors": detail if isinstance(detail, list) else [detail]}
        return _("Invalid input."), _as_lists(detail)
    message = exc.detail if isinstance(exc.detail, str) else exc.default_detail
    details = dict(getattr(exc, "details", {}) or {})
    if isinstance(exc, exceptions.Throttled) and exc.wait is not None:
        details.setdefault("retry_after", int(exc.wait))
    return message, details


def handler(exc, context):
    """``REST_FRAMEWORK["EXCEPTION_HANDLER"]``."""
    exc = _normalise(exc)
    response = drf_exception_handler(exc, context)
    if response is None:
        # Not an API exception: log it and still answer in the API format.
        view = context.get("view")
        logger.exception("Unhandled error in API view %s", type(view).__name__, exc_info=exc)
        return Response(
            error_body("server_error", _("Something went wrong on the server.")),
            status=status.HTTP_500_INTERNAL_SERVER_ERROR,
        )
    code = _code_for(exc)
    message, details = _message_and_details(exc, code)
    response.data = error_body(code, message, details)
    return response
