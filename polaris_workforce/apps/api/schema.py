"""OpenAPI helpers for the per-app API packages."""

from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiExample, OpenApiParameter, OpenApiResponse

from .serializers import ErrorSerializer

_ERRORS = {
    400: (
        "validation_error",
        "Invalid input.",
        {"client_uuid": ["This field is required."]},
        "The request is invalid; `details` maps each field to its errors.",
    ),
    401: (
        "not_authenticated",
        "Invalid or revoked device token.",
        {},
        "Missing, invalid or revoked device token. Sign in again with the PIN.",
    ),
    403: (
        "permission_denied",
        "You do not have permission to perform this action.",
        {},
        "Signed in, but not allowed (for example a site you are not assigned to).",
    ),
    404: ("not_found", "Not found.", {}, "The resource does not exist."),
    409: (
        "conflict",
        "This client_uuid was already used for a different request.",
        {"client_uuid": "6f1c2b9e-1d1a-4c55-9a52-3f0e8a9d7b10"},
        "The request conflicts with the current state.",
    ),
    423: (
        "locked",
        "This resource is locked and cannot be changed.",
        {"status": "locked"},
        "Locked: the day or pay period is locked, or the device is locked after failed PINs.",
    ),
    429: (
        "throttled",
        "Request was throttled. Expected available in 42 seconds.",
        {"retry_after": 42},
        "Too many requests. Wait `details.retry_after` seconds (also in `Retry-After`).",
    ),
}


def error_responses(
    *statuses: int,
    examples: dict[int, list[tuple[str, str, dict]]] | None = None,
    descriptions: dict[int, str] | None = None,
) -> dict[int, OpenApiResponse]:
    """Error responses in the shared format.

    Each status gets the generic example above, unless ``examples`` gives the
    endpoint's own: ``{status: [(example name, message, details), ...]}``.
    ``descriptions`` replaces the generic description of a status.
    """
    examples = examples or {}
    descriptions = descriptions or {}
    responses = {}
    for status_code in statuses:
        code, message, details, description = _ERRORS[status_code]
        own = examples.get(status_code) or [(code, message, details)]
        responses[status_code] = OpenApiResponse(
            ErrorSerializer,
            description=descriptions.get(status_code, description),
            examples=[
                OpenApiExample(
                    name,
                    value={"code": code, "message": example_message, "details": example_details},
                    response_only=True,
                    status_codes=[str(status_code)],
                )
                for name, example_message, example_details in own
            ],
        )
    return responses


SITE_ID = OpenApiParameter(
    "site_id",
    OpenApiTypes.INT,
    OpenApiParameter.PATH,
    description="A site the signed-in foreman is assigned to (from `GET /me/`).",
)
DATE = OpenApiParameter(
    "date",
    OpenApiTypes.DATE,
    OpenApiParameter.PATH,
    description="Work day, ISO 8601 `YYYY-MM-DD`, in the company time zone.",
)
