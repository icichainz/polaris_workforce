"""Foreman sign-in (PIN → device token), sign-out and profile.

The logic is in ``apps/accounts/services.py``; these views map it to the contract.
"""

from django.utils.translation import gettext_lazy as _
from drf_spectacular.utils import OpenApiExample, OpenApiResponse, extend_schema
from rest_framework import exceptions, serializers, status
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView

from apps.accounts import services
from apps.accounts.models import ForemanDevice
from apps.accounts.permissions import scoped_sites
from apps.api.authentication import KEYWORD
from apps.api.exceptions import Locked
from apps.api.schema import error_responses
from apps.core.models import CompanySettings
from apps.core.settings import resolve_setting
from apps.worksites.models import Site

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


class PinRejected(exceptions.AuthenticationFailed):
    """401 ``not_authenticated`` that carries ``details`` (e.g. ``attempts_left``)."""

    def __init__(self, message=None, *, details=None):
        super().__init__(detail=message)
        self.details = details or {}


def _datetime(value):
    return serializers.DateTimeField().to_representation(value)


def profile_data(user, company: CompanySettings) -> dict:
    return {
        "id": user.pk,
        "name": user.get_full_name() or user.username,
        "role": user.role,
        "phone": user.phone,
        "language": user.language or company.language,
    }


def company_data(company: CompanySettings) -> dict:
    return {
        "country": company.country,
        "time_zone": company.time_zone,
        "currency": {"code": company.currency, "decimals": company.currency_decimals},
    }


def sites_data(user, company: CompanySettings) -> list[dict]:
    """The sites in the user's scope that are still running (completed ones are left out)."""
    sites = (
        scoped_sites(user)
        .exclude(status=Site.Status.COMPLETED)
        .select_related("manager")
        .order_by("name", "pk")
    )
    return [
        {
            "id": site.pk,
            "name": site.name,
            "address": site.address,
            "manager": (
                {"id": site.manager.pk, "name": site.manager.get_full_name() or str(site.manager)}
                if site.manager
                else None
            ),
            "work_start": resolve_setting("work_start", site, company=company),
            "roll_call_due": resolve_setting("roll_call_due", site, company=company),
        }
        for site in sites
    ]


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
        body = PinLoginSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        try:
            result = services.pin_sign_in(**body.validated_data)
        except services.PinLocked as exc:
            raise Locked(
                _("Too many wrong PINs. Try again later."),
                details={"locked_until": _datetime(exc.locked_until)},
            ) from exc
        except services.WrongPin as exc:
            details = {"attempts_left": exc.attempts_left}
            if exc.locked_until is not None:
                details["locked_until"] = _datetime(exc.locked_until)
            raise PinRejected(_("Wrong PIN."), details=details) from exc
        except services.SignInRefused as exc:
            raise PinRejected(_("Wrong phone number or PIN.")) from exc
        company = CompanySettings.load()
        data = {
            "token": result.token,
            "device": {
                "id": result.device.pk,
                "device_id": result.device.device_id,
                "name": result.device.name,
            },
            "user": profile_data(result.user, company),
            "sites": sites_data(result.user, company),
        }
        return Response(PinLoginResponseSerializer(data).data)

    def get_authenticate_header(self, request):
        # No authentication classes here, but a wrong PIN must stay 401, not become 403.
        return KEYWORD


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
        # With a browser session there is no device to revoke: nothing to do.
        if isinstance(request.auth, ForemanDevice):
            services.revoke_device(
                request.auth, actor=request.user, reason="Signed out on the phone."
            )
        return Response(status=status.HTTP_204_NO_CONTENT)


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
        company = CompanySettings.load()
        data = {
            **profile_data(request.user, company),
            "company": company_data(company),
            "sites": sites_data(request.user, company),
        }
        return Response(MeSerializer(data).data)
