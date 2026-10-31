"""Foreman sign-in (PIN → device token), sign-out and profile.

Stubs: Wave 2 agent A replaces each body with a call into ``apps/accounts/services.py``
(PIN check with lockout, token issue, revoke) and keeps the schema as is.
"""

from drf_spectacular.utils import OpenApiExample, OpenApiResponse, extend_schema
from rest_framework.permissions import AllowAny
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView

from apps.api.exceptions import NotImplementedYet
from apps.api.schema import error_responses

from .serializers import MeSerializer, PinLoginResponseSerializer, PinLoginSerializer

TAGS = ["accounts"]

USER_EXAMPLE = {
    "id": 7,
    "name": "Kossi Amegah",
    "role": "foreman",
    "phone": "+22890112233",
    "language": "fr",
}
SITE_EXAMPLE = {
    "id": 3,
    "name": "Riverside Apartments",
    "address": "Boulevard du Mono, Lomé",
    "manager": {"id": 4, "name": "Edem Kpodar"},
    "work_start": "07:00",
    "roll_call_due": "08:00",
}


class PinLoginView(APIView):
    authentication_classes = []
    permission_classes = [AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "pin"

    @extend_schema(
        tags=TAGS,
        operation_id="auth_pin",
        summary="Sign in with phone number and PIN",
        description=(
            "Exchanges phone + 4-digit PIN for a **device token**. No authentication; "
            "throttled per IP (`pin` scope, 10/minute).\n\n"
            "Signing in again from the same `device_id` revokes that device's previous "
            "token. After repeated wrong PINs the device is locked: `423 locked` with "
            "`details.locked_until`. A wrong PIN is `401 not_authenticated` with "
            "`details.attempts_left`.\n\n"
            "Keep the token in secure storage; it is shown only once."
        ),
        request=PinLoginSerializer,
        responses={
            200: PinLoginResponseSerializer,
            **error_responses(400, 401, 423, 429),
        },
        examples=[
            OpenApiExample(
                "Sign in",
                request_only=True,
                value={
                    "phone": "+22890112233",
                    "pin": "4821",
                    "device_id": "0b6f4c4e-61a2-4d5b-9a50-8f3c0f5d2a11",
                    "device_name": "Tecno Spark 10",
                },
            ),
            OpenApiExample(
                "Signed in",
                response_only=True,
                status_codes=["200"],
                value={
                    "token": "q9V3rM2xQk7f0bU8p1sZcW4yL6tN5hJaDgE_oRiK-Yw",
                    "device": {
                        "id": 12,
                        "device_id": "0b6f4c4e-61a2-4d5b-9a50-8f3c0f5d2a11",
                        "name": "Tecno Spark 10",
                    },
                    "user": USER_EXAMPLE,
                    "sites": [SITE_EXAMPLE],
                },
            ),
        ],
    )
    def post(self, request):
        raise NotImplementedYet()


class LogoutView(APIView):
    @extend_schema(
        tags=TAGS,
        operation_id="auth_logout",
        summary="Sign out this device",
        description=(
            "Revokes the device token used for this call (sets `revoked_at`). Later calls "
            "with it get `401`. The phone should first drain its outbox: unsent writes "
            "cannot be sent after logout."
        ),
        request=None,
        responses={
            204: OpenApiResponse(description="Signed out."),
            **error_responses(401),
        },
    )
    def post(self, request):
        raise NotImplementedYet()


class MeView(APIView):
    @extend_schema(
        tags=TAGS,
        operation_id="me",
        summary="Signed-in foreman and assigned sites",
        description=(
            "Profile, company basics (time zone, currency) and the sites this user is "
            "assigned to. Call after sign-in and on every app start when online."
        ),
        responses={200: MeSerializer, **error_responses(401)},
        examples=[
            OpenApiExample(
                "Foreman",
                response_only=True,
                value={
                    **USER_EXAMPLE,
                    "company": {
                        "country": "TG",
                        "time_zone": "Africa/Lome",
                        "currency": {"code": "XOF", "decimals": 0},
                    },
                    "sites": [SITE_EXAMPLE],
                },
            )
        ],
    )
    def get(self, request):
        raise NotImplementedYet()
