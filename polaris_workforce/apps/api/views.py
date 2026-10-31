from drf_spectacular.utils import OpenApiExample, extend_schema
from rest_framework.decorators import (
    api_view,
    authentication_classes,
    permission_classes,
    throttle_classes,
)
from rest_framework.permissions import AllowAny
from rest_framework.response import Response

from .serializers import HealthSerializer


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
