"""Check-in over the API: badge scans, kiosk phones, roll-call suggestions.

Views stay thin: they check the site scope (``get_site_or_403``) and the kiosk rule,
validate the body and call ``apps/checkin/services.py`` (writes) or ``selectors.py``
(reads). Writes need a foreman (or an admin, for support), like the roll call.

A **kiosk** phone (a foreman's device switched to kiosk mode in the office) only works
for its own site here: any other site answers ``403 permission_denied``.
"""

from django.utils.translation import gettext_lazy as _
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiExample, OpenApiParameter, extend_schema
from rest_framework import exceptions
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.accounts.permissions import IsForeman, company_workers, get_site_or_403
from apps.api import examples as ex
from apps.api.files import _IF_NONE_MATCH, _NOT_MODIFIED, IMAGE_TYPES, _file_responses, _FileView
from apps.api.idempotency import IdempotentCreateMixin
from apps.api.schema import DATE, SITE_ID, error_responses
from apps.checkin import files, selectors, services
from apps.checkin.models import Badge
from apps.core import files as core_files
from apps.core.dates import company_today
from apps.core.models import CompanySettings
from apps.worksites.services import crew_ids

from . import examples as cx
from .serializers import (
    CheckInCreateSerializer,
    CheckInDeviceSerializer,
    CheckInListSerializer,
    CheckInSerializer,
    CheckInSetupSerializer,
    RollCallSuggestionsSerializer,
    badge_hash_payload,
    checkin_payload,
)

TAGS = ["checkin"]

KIOSK_403 = (
    "Kiosk for another site",
    "This phone is a kiosk for another site.",
    {},
)


def _site_for(request, site_id):
    """The site, if the user may see it and (for a kiosk phone) it is the kiosk's site."""
    site = get_site_or_403(request.user, site_id)
    kiosk = selectors.kiosk_of(request.auth)
    if kiosk is not None and kiosk.site_id != site.pk:
        raise exceptions.PermissionDenied(_("This phone is a kiosk for another site."))
    return site, kiosk


class CheckInCreateView(IdempotentCreateMixin, APIView):
    permission_classes = [IsAuthenticated, IsForeman]
    parser_classes = [MultiPartParser, FormParser, JSONParser]

    @extend_schema(
        tags=TAGS,
        operation_id="checkin_create",
        summary="Record a check-in or check-out",
        description=(
            "A badge scanned by the foreman's phone or a kiosk phone (`badge`: the QR "
            "text as read), or a worker picked by hand (`worker_id`, not on a kiosk). "
            "Optional GPS (`latitude`, `longitude`, `accuracy_m`) is checked against the "
            "site's geofence; an optional `photo` (multipart) is kept for the office.\n\n"
            "**Always recorded, never refused for what it says:** an unknown or revoked "
            "badge, a worker not on the crew, or a position outside the geofence still "
            "answers `201`, with `counted` and `flags` telling the phone what to show "
            '(e.g. `unknown_badge` → "Badge not recognised, see the foreman"). Only '
            "`counted: true` check-ins feed the roll-call suggestions.\n\n"
            "Attendance mode `self_check_in`: a counted check-in of a crew worker also "
            "writes that worker's record on the day's **draft** (`draft_updated: true`), "
            "never once the day was submitted. Marks the foreman changed by hand are not "
            "overwritten.\n\n"
            "Idempotent: `201` when created; the same `client_uuid` again returns the "
            "original check-in with `200` and `Idempotent-Replayed: true` (a photo sent "
            "again is ignored). The same `client_uuid` for a different check-in: "
            "`409 conflict`.\n\n"
            "Errors, in the order they are checked: no or bad token `401`; not a foreman "
            "(or a kiosk phone calling another endpoint) `403`; the body (`at` more than 10 "
            "minutes ahead or 31 days old, both or neither of `badge`/`worker_id`, "
            "coordinates, a bad photo) `400`; the site (one you are not on, or a kiosk phone "
            "sending for another site) `403`; `worker_id` not a worker of the company `400`; "
            "a kiosk sending `worker_id` `400`; the `client_uuid` used for another check-in "
            "`409`."
        ),
        request={
            "multipart/form-data": CheckInCreateSerializer,
            "application/json": CheckInCreateSerializer,
        },
        responses={
            201: CheckInSerializer,
            200: CheckInSerializer,
            **error_responses(
                400,
                401,
                403,
                409,
                examples={
                    400: [
                        (
                            "Badge and worker_id",
                            "Invalid input.",
                            {"non_field_errors": ["Send either badge or worker_id."]},
                        ),
                        (
                            "Phone clock ahead",
                            "Invalid input.",
                            {"at": ["This time is in the future. Check the phone's clock."]},
                        ),
                        (
                            "Kiosk without a badge",
                            "Invalid input.",
                            {"badge": ["A kiosk only records badge scans."]},
                        ),
                    ],
                    403: [
                        (
                            "Not your site",
                            "You do not have permission to perform this action.",
                            {},
                        ),
                        KIOSK_403,
                    ],
                },
            ),
        },
        examples=[
            OpenApiExample("Badge scan with GPS", request_only=True, value=cx.CREATE_BADGE),
            OpenApiExample("Manual check-out", request_only=True, value=cx.CREATE_MANUAL),
            OpenApiExample(
                "Recorded", response_only=True, status_codes=["201", "200"], value=cx.CHECKIN
            ),
            OpenApiExample(
                "Unknown badge, outside the geofence",
                response_only=True,
                status_codes=["201"],
                value=cx.UNKNOWN,
            ),
        ],
    )
    def post(self, request):
        body = CheckInCreateSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        data = body.validated_data
        site, _kiosk = _site_for(request, data["site_id"])
        worker = None
        if data.get("worker_id") is not None:
            worker = company_workers(request.user).filter(pk=data["worker_id"]).first()
            if worker is None:
                raise exceptions.ValidationError({"worker_id": [_("Unknown worker.")]})
        result = services.record_checkin(
            user=request.user,
            device=request.auth,
            site=site,
            direction=data["direction"],
            at=data["at"],
            client_uuid=data["client_uuid"],
            badge=data.get("badge"),
            worker=worker,
            latitude=data.get("latitude"),
            longitude=data.get("longitude"),
            accuracy_m=data.get("accuracy_m"),
            photo=data.get("photo"),
        )
        payload = checkin_payload(request, result.instance)
        return self.idempotent_response(result, CheckInSerializer(payload).data)


class SiteCheckInsView(APIView):
    @extend_schema(
        tags=TAGS,
        operation_id="site_checkins",
        summary="A site's check-ins for one day",
        description=(
            "Every check-in recorded on the site that work day (all phones and kiosks), "
            "oldest first, with `flags` (`duplicate`: the same worker and direction less "
            "than 5 minutes after another). Includes the ones that do not count."
        ),
        parameters=[SITE_ID, DATE],
        responses={200: CheckInListSerializer, **error_responses(401, 403, 404)},
        examples=[
            OpenApiExample(
                "Morning",
                response_only=True,
                value={
                    "site_id": ex.SITE_ID,
                    "date": ex.DATE,
                    "checkins": cx.SITE_CHECKINS,
                },
            )
        ],
    )
    def get(self, request, site_id, date):
        site, _kiosk = _site_for(request, site_id)
        checkins = selectors.day_checkins(site, date)
        duplicates = selectors.duplicate_ids(checkins)
        return Response(
            CheckInListSerializer(
                {
                    "site_id": site.pk,
                    "date": date,
                    "checkins": [checkin_payload(request, c, duplicates) for c in checkins],
                }
            ).data
        )


class SuggestionsView(APIView):
    @extend_schema(
        tags=TAGS,
        operation_id="site_day_suggestions",
        summary="Roll-call suggestions from check-ins",
        description=(
            "One suggestion per crew worker with counted check-ins (workers without any "
            "are not listed: no suggestion). Rules, in the company time zone: "
            "`arrival` = first `in`, `departure` = last `out`; an `in` and an `out` "
            "before 12:00, or an `in` at or after 12:00 → `half_day`; first `in` after "
            "`late_after` → `late`; else `present`. Only an `out` → `status: null`.\n\n"
            "The phone pre-fills the roll call with them (modes `both` and "
            "`self_check_in`); the foreman confirms and submits as usual with "
            "`PUT .../records/`. `day_status` says whether the day can still change."
        ),
        parameters=[SITE_ID, DATE],
        responses={200: RollCallSuggestionsSerializer, **error_responses(401, 403, 404)},
        examples=[OpenApiExample("Suggestions", response_only=True, value=cx.SUGGESTIONS)],
    )
    def get(self, request, site_id, date):
        site, _kiosk = _site_for(request, site_id)
        result = selectors.day_suggestions(site, date)
        return Response(
            RollCallSuggestionsSerializer(
                {
                    "site_id": site.pk,
                    "date": date,
                    "attendance_mode": result.attendance_mode,
                    "late_after": result.late_after,
                    "day_status": result.day_status,
                    "suggestions": [
                        {
                            "worker_id": s.worker_id,
                            "status": s.status,
                            "arrival": s.arrival,
                            "departure": s.departure,
                            "first_in_at": s.first_in_at,
                            "last_out_at": s.last_out_at,
                            "checkin_count": s.checkin_count,
                        }
                        for s in result.suggestions
                    ],
                }
            ).data
        )


class SetupView(APIView):
    @extend_schema(
        tags=TAGS,
        operation_id="site_checkin_setup",
        summary="Check-in setup of a site (badges, geofence, mode)",
        description=(
            "What the phone needs to scan offline: the attendance mode, whether this "
            "phone is the site's kiosk, the geofence, and one entry per **active** badge "
            "of today's crew with the SHA-256 of its QR text. Hash what you scan "
            "(`sha256(utf8(text))`, hex) and look it up to show the worker's name at "
            "once; queue the check-in in the outbox either way (the server decides). "
            "Refresh it with the crew list."
        ),
        parameters=[SITE_ID],
        responses={200: CheckInSetupSerializer, **error_responses(401, 403, 404)},
        examples=[OpenApiExample("Setup", response_only=True, value=cx.SETUP)],
    )
    def get(self, request, site_id):
        site, kiosk = _site_for(request, site_id)
        settings, _tz, late_after = selectors.site_rules(site)
        today = company_today(settings)
        badges = Badge.objects.filter(
            worker_id__in=crew_ids(site, today), revoked_at__isnull=True
        ).order_by("worker_id")
        geofence = getattr(site, "geofence", None)
        return Response(
            CheckInSetupSerializer(
                {
                    "site_id": site.pk,
                    "attendance_mode": settings.attendance_mode,
                    "late_after": late_after,
                    "kiosk": kiosk is not None,
                    "geofence": geofence,
                    "badges": [badge_hash_payload(badge) for badge in badges],
                }
            ).data
        )


class DeviceView(APIView):
    @extend_schema(
        tags=TAGS,
        operation_id="checkin_device",
        summary="This phone's check-in role",
        description=(
            "Call after sign-in and with `GET /me/`. `kiosk` is set when the office has "
            "switched this phone to kiosk mode: show only the kiosk screen for that site "
            "(the token may then only record check-ins there). `null`: an ordinary "
            "foreman phone. A browser session has no device: `kiosk` is `null`."
        ),
        responses={200: CheckInDeviceSerializer, **error_responses(401)},
        examples=[
            OpenApiExample("Kiosk phone", response_only=True, value=cx.DEVICE_KIOSK),
            OpenApiExample("Foreman phone", response_only=True, value=cx.DEVICE_PHONE),
        ],
    )
    def get(self, request):
        settings = CompanySettings.load()
        kiosk = selectors.kiosk_of(request.auth)
        return Response(
            CheckInDeviceSerializer(
                {
                    "attendance_mode": settings.attendance_mode,
                    "kiosk": (
                        {
                            "site_id": kiosk.site_id,
                            "site_name": kiosk.site.name,
                            "since": kiosk.updated_at,
                        }
                        if kiosk
                        else None
                    ),
                }
            ).data
        )


class CheckInPhotoView(_FileView):
    @extend_schema(
        tags=["files"],
        operation_id="file_checkin_photo",
        summary="A check-in's photo",
        description=(
            "The URL comes as `photo_url` in a check-in. Send the `Authorization` header. "
            "`404` if the check-in has no photo or is not on one of your sites."
        ),
        parameters=[
            OpenApiParameter("checkin_id", OpenApiTypes.INT, OpenApiParameter.PATH),
            _IF_NONE_MATCH,
        ],
        responses={
            **_file_responses(*IMAGE_TYPES),
            304: _NOT_MODIFIED,
            **error_responses(401, 404),
        },
    )
    def get(self, request, checkin_id):
        return core_files.file_response(
            request, files.checkin_photo_or_404(request.user, checkin_id)
        )
