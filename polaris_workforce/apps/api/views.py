from django.http import JsonResponse
from django.views import defaults
from drf_spectacular.utils import OpenApiExample, extend_schema
from rest_framework.decorators import (
    api_view,
    authentication_classes,
    permission_classes,
    throttle_classes,
)
from rest_framework.permissions import AllowAny
from rest_framework.response import Response

from .exceptions import not_found_body
from .serializers import HealthSerializer

API_PREFIX = "/api/"


@extend_schema(
    tags=["system"],
    operation_id="health",
    summary="Server reachable",
    description=(
        "Lets the phone check that the server is reachable before syncing. "
        "No authentication. Cheap: call it before draining the outbox."
    ),
    responses={200: HealthSerializer},
    examples=[OpenApiExample("ok", value={"status": "ok"}, response_only=True)],
)
@api_view(["GET"])
@authentication_classes([])
@permission_classes([AllowAny])
@throttle_classes([])
def health(request):
    return Response({"status": "ok"})


def unknown_url(request, *args, **kwargs):
    """Last route under ``/api/v1/``: a URL that matches no endpoint gets a JSON 404.

    A plain Django view (not DRF), so it stays out of the OpenAPI schema. It also
    covers DEBUG, where Django shows its technical 404 page instead of ``handler404``.
    """
    return JsonResponse(not_found_body(), status=404)


def page_not_found(request, exception=None):
    """``handler404``: the API error format under ``/api/``, the HTML page elsewhere."""
    if request.path.startswith(API_PREFIX):
        return JsonResponse(not_found_body(), status=404)
    return defaults.page_not_found(request, exception)
