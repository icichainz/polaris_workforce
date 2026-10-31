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
from apps.api import examples as ex
from apps.api.authentication import KEYWORD
from apps.api.exceptions import Locked
from apps.api.schema import error_responses
from apps.core.choices import ProofType
from apps.core.models import CompanySettings
from apps.core.settings import resolve_setting
from apps.worksites.models import Site

from .serializers import MeSerializer, PinLoginResponseSerializer, PinLoginSerializer

TAGS = ["accounts"]


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


# Mirrors apps.payroll.services.validate_proof: only a photo proof needs its file (a
# signature or thumb mark may be on the paper sheet). tests/test_accounts_api.py checks
# that the two stay in step.
PROOF_TYPES_NEEDING_FILE = [ProofType.PHOTO]


def payment_rules(company: CompanySettings) -> dict:
    """What ``validate_proof`` accepts. An empty setting accepts every proof type."""
    accepted = set(company.accepted_proofs or ProofType.values)
    return {
        "accepted_proofs": [value for value in ProofType.values if value in accepted],
        "file_required_for": [str(value) for value in PROOF_TYPES_NEEDING_FILE],
    }


def company_data(company: CompanySettings) -> dict:
    return {
        "country": company.country,
        "time_zone": company.time_zone,
        "currency": {"code": company.currency, "decimals": company.currency_decimals},
        "payments": payment_rules(company),
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
            "Signing in again from the same `device_id` replaces that device's previous "
            "token. Signing in again after a logout gives a new token for the same "
            "user: writes queued in the outbox before the logout are accepted with it.\n\n"
            "Errors:\n"
            "- A wrong PIN for a foreman (or admin) who has one: `401 not_authenticated` "
            "with `details.attempts_left` (tries left before the lock; the 5th wrong PIN "
            "answers `attempts_left: 0` plus `details.locked_until`).\n"
            "- An unknown phone number, a user without a PIN, a disabled user, or a role "
            "that cannot use the phone (site manager, accountant): the same plain "
            "`401 not_authenticated` with empty `details`. The answer does not say which.\n"
            "- While locked after 5 wrong PINs (15 minutes), even with the right PIN: "
            "`423 locked` with `details.locked_until` (ISO datetime).\n\n"
            "Keep the token in secure storage; it is shown only once."
        ),
        request=PinLoginSerializer,
        responses={
            200: PinLoginResponseSerializer,
            **error_responses(
                400,
                401,
                423,
                429,
                examples={
                    400: [
                        (
                            "Bad PIN format",
                            "Invalid input.",
                            {"pin": ["This value does not match the required pattern."]},
                        )
                    ],
                    401: [
                        ("Wrong PIN", "Wrong PIN.", {"attempts_left": 2}),
                        ("Unknown phone or no PIN", "Wrong phone number or PIN.", {}),
                    ],
                    423: [
                        (
                            "Locked after wrong PINs",
                            "Too many wrong PINs. Try again later.",
                            {"locked_until": "2026-10-31T06:57:00Z"},
                        )
                    ],
                },
                descriptions={
                    401: "Wrong PIN (`details.attempts_left`), or an unknown phone / no PIN / "
                    "a role that cannot sign in on the phone (empty `details`).",
                    423: "Locked after 5 wrong PINs: `details.locked_until` (ISO datetime).",
                },
            ),
        },
        examples=[
            OpenApiExample(
                "Sign in",
                request_only=True,
                value={
                    "phone": ex.USER["phone"],
                    "pin": "4821",
                    "device_id": ex.DEVICE["device_id"],
                    "device_name": ex.DEVICE["name"],
                },
            ),
            OpenApiExample(
                "Signed in",
                response_only=True,
                status_codes=["200"],
                value={
                    "token": ex.TOKEN,
                    "device": ex.DEVICE,
                    "user": ex.USER,
                    "sites": [ex.SITE],
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
        company = CompanySettings.load(result.user.company)
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
            "with it get `401`.\n\n"
            "**The outbox survives a logout.** Records, submissions, corrections and "
            "payments belong to the user and the site, not to the token: writes queued "
            "before the logout are accepted once the same user signs in again on the "
            "device (new token), and replays of writes already sent still return the "
            "original result. Try to drain the outbox first anyway; keep what is left "
            "and send it after the next sign-in of the same user. Another user signed "
            "in on the phone cannot replay a payment (`409 conflict`), so don't hand a "
            "foreman's outbox to someone else."
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
            "Profile, company basics (time zone, currency, proof-of-payment rules) and the "
            "sites this user is assigned to. Call after sign-in and on every app start "
            "when online."
        ),
        responses={200: MeSerializer, **error_responses(401)},
        examples=[
            OpenApiExample(
                "Foreman",
                response_only=True,
                value={**ex.USER, "company": ex.COMPANY, "sites": [ex.SITE]},
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
