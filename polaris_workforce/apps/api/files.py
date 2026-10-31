"""``/api/v1/files/…`` — worker photos and payment proofs for the phone.

The URLs come in the API responses (``photo_url``, ``proof_url``); the phone fetches them
with its ``Authorization: Device <token>`` header (a browser session works too). Access
rules and serving: ``apps.core.files``. Out of scope, no file, or a file missing on disk:
``404 not_found``.
"""

from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiParameter, OpenApiResponse, extend_schema
from rest_framework.negotiation import BaseContentNegotiation
from rest_framework.views import APIView

from apps.core import files

from .schema import error_responses

TAGS = ["files"]

_FILE = OpenApiResponse(
    OpenApiTypes.BINARY,
    description=(
        "The file, with an `ETag`: send it back in `If-None-Match` to get "
        "`304 Not Modified` instead of the bytes."
    ),
)
IMAGE_TYPES = ("image/jpeg", "image/png", "image/webp")


def _file_responses(*media_types: str) -> dict:
    return {(200, media_type): _FILE for media_type in media_types}


_NOT_MODIFIED = OpenApiResponse(description="Not modified: the cached copy is current.")
_IF_NONE_MATCH = OpenApiParameter(
    "If-None-Match",
    OpenApiTypes.STR,
    OpenApiParameter.HEADER,
    required=False,
    description="The `ETag` of the copy the phone already has.",
)


class AnyAcceptNegotiation(BaseContentNegotiation):
    """Files answer whatever ``Accept`` says (an image loader sends ``image/*``); errors
    still use the JSON error format."""

    def select_parser(self, request, parsers):
        return parsers[0]

    def select_renderer(self, request, renderers, format_suffix=None):
        return renderers[0], renderers[0].media_type


class _FileView(APIView):
    content_negotiation_class = AnyAcceptNegotiation


class WorkerPhotoView(_FileView):
    @extend_schema(
        tags=TAGS,
        operation_id="file_worker_photo",
        summary="A worker's photo",
        description=(
            "The URL comes as `photo_url` in the crew list and the cash sheet. Send the "
            "`Authorization` header. `404` if the worker has no photo or is not on one "
            "of your sites."
        ),
        parameters=[
            OpenApiParameter("worker_id", OpenApiTypes.INT, OpenApiParameter.PATH),
            _IF_NONE_MATCH,
        ],
        responses={
            **_file_responses(*IMAGE_TYPES),
            304: _NOT_MODIFIED,
            **error_responses(401, 404),
        },
    )
    def get(self, request, worker_id):
        return files.file_response(request, files.worker_photo_or_404(request.user, worker_id))


class PaymentProofView(_FileView):
    @extend_schema(
        tags=TAGS,
        operation_id="file_payment_proof",
        summary="A payment's proof file",
        description=(
            "The URL comes as `proof_url` in `POST /payments/`. Send the `Authorization` "
            "header. `404` if the payment has no file or is not on one of your sites."
        ),
        parameters=[
            OpenApiParameter("payment_id", OpenApiTypes.INT, OpenApiParameter.PATH),
            _IF_NONE_MATCH,
        ],
        responses={
            **_file_responses(*IMAGE_TYPES, "application/pdf"),
            304: _NOT_MODIFIED,
            **error_responses(401, 404),
        },
    )
    def get(self, request, payment_id):
        return files.file_response(request, files.payment_proof_or_404(request.user, payment_id))
