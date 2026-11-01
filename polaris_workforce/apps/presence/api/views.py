"""Presence over the API: the phone's tracker token and consent, a site's presence
sessions, and the tracker's event ingest.

Views stay thin: the scope check (``get_site_or_403``), the body, then
``apps/presence/services.py`` or ``selectors.py``.
"""

import logging

from django.utils.translation import gettext_lazy as _
from drf_spectacular.utils import OpenApiExample, OpenApiParameter, extend_schema
from rest_framework import status
from rest_framework.parsers import JSONParser
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView

from apps.accounts.permissions import IsForeman, get_site_or_403
from apps.api.exceptions import PolarisAPIException
from apps.api.idempotency import REPLAY_HEADER
from apps.api.schema import SITE_ID, error_responses

from .. import selectors, services
from ..authentication import IsTrackerService, TrackerServiceAuthentication
from . import examples as px
from . import serializers as s

logger = logging.getLogger(__name__)
TAGS = ["presence"]
TOO_MANY_EVENTS = f"Ensure this field has no more than {services.MAX_EVENTS} elements."


class ConsentRequired(PolarisAPIException):
    status_code = status.HTTP_403_FORBIDDEN
    default_detail = _("Accept the tracking notice first.")
    default_code = "consent_required"


class TrackingDisabled(PolarisAPIException):
    status_code = status.HTTP_403_FORBIDDEN
    default_detail = _("Tracking is not enabled for you.")
    default_code = "tracking_disabled"


_REFUSALS = {"consent_required": ConsentRequired, "tracking_disabled": TrackingDisabled}


class TrackingTokenView(APIView):
    permission_classes = [IsAuthenticated, IsForeman]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "tracking"

    @extend_schema(
        tags=TAGS,
        operation_id="tracking_token",
        summary="Get a tracker token for this foreman's phone",
        description=(
            'A `device` JWT for the tracker (claims v2): `sub = "foreman:<user id>"`, '
            "`company`, `sites` = your sites where `tracking_enabled` is on, `travel` "
            '(true for company admins), `aud = "polaris-tracker"`, 12 hours. Call it after '
            "the PIN sign-in and again before `expires_at`.\n\n"
            "`settings`: the resolved settings the phone needs, over the token's sites "
            "(shortest GPS interval, earliest start and latest end of the tracking hours, "
            "and the retention of raw positions).\n\n"
            "Refusals (`403`), in this order:\n"
            "- `tracking_disabled` with `details.reason = settings`: tracking is off on all "
            "of your sites; show no tracking screens.\n"
            "- `tracking_disabled` with `details.reason = consent_refused`: you declined "
            "the notice (the latest answer); `POST /tracking/consent/` with "
            "`accepted: true` turns it on.\n"
            "- `consent_required`: no answer yet, or the notice changed since you accepted "
            "it. Show the notice of version `details.text_version`, then "
            "`POST /tracking/consent/`.\n\n"
            "Both consent refusals give, in `details`, what the notice shows: "
            "`text_version`, `tracking_hours {start, end}`, `gps_interval_seconds` and "
            "`retention_days`.\n\n"
            "A foreman is a user, not a worker: the worker-level `tracking_enabled` does not "
            "apply; the foreman's own switch is their consent. Throttled per user "
            "(`tracking` scope, 30/minute)."
        ),
        request=None,
        responses={
            200: s.TrackingTokenSerializer,
            **error_responses(
                401,
                403,
                429,
                examples={
                    403: [
                        (
                            "Tracking off on your sites",
                            "Tracking is not enabled for you.",
                            {"reason": "settings"},
                        ),
                        (
                            "Consent refused",
                            "You declined tracking. Accept the notice to turn it on.",
                            {"reason": "consent_refused", **px.NOTICE},
                        ),
                        ("Consent required", "Accept the tracking notice first.", px.NOTICE),
                    ]
                },
                descriptions={
                    403: "`tracking_disabled` or `consent_required` (see above); "
                    "`permission_denied` for a user who is not a foreman or an admin.",
                },
            ),
        },
        examples=[OpenApiExample("Token", response_only=True, value=px.TRACKING_TOKEN)],
    )
    def post(self, request):
        try:
            tracking = services.device_tracking(request.user)
        except services.TrackingRefused as refusal:
            raise _REFUSALS[refusal.code](refusal.message, details=refusal.details) from None
        terms = tracking.terms
        data = {
            "token": tracking.token,
            "expires_at": tracking.expires_at,
            "tracker_url": tracking.tracker_url,
            "sites": tracking.sites,
            "settings": {
                "gps_interval_seconds": terms.gps_interval_seconds,
                "tracking_hours": {"start": terms.tracking_start, "end": terms.tracking_end},
                "retention_days": terms.retention_days,
            },
        }
        return Response(s.TrackingTokenSerializer(data).data)


class TrackingConsentView(APIView):
    permission_classes = [IsAuthenticated, IsForeman]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "tracking"

    @extend_schema(
        tags=TAGS,
        operation_id="tracking_consent",
        summary="Accept or refuse the tracking notice",
        description=(
            "The foreman's answer to the notice shown on the phone. Accepting the current "
            "version opens `POST /tracking/token/`; refusing, or withdrawing an earlier "
            "acceptance, is recorded too and makes it answer `403 tracking_disabled` "
            "(`reason: consent_refused`) until an acceptance. The latest answer counts. "
            "Each answer writes an audit entry.\n\n"
            "`accepted: false` is taken at any time, whatever `text_version` it names. "
            "`accepted: true` must name the current notice (`details.text_version` of "
            "`consent_required`), else `400`.\n\n"
            "Idempotent: the same `client_uuid` again returns the original answer (`200`, "
            "`Idempotent-Replayed: true`); used by another user, `409 conflict`."
        ),
        request=s.ConsentRequestSerializer,
        responses={
            200: s.ConsentSerializer,
            **error_responses(
                400,
                401,
                403,
                409,
                429,
                examples={
                    400: [
                        (
                            "Old notice",
                            "Invalid input.",
                            {
                                "text_version": [
                                    "The notice has changed: show version 2026-11-01 and ask again."
                                ]
                            },
                        )
                    ]
                },
            ),
        },
        examples=[
            OpenApiExample("Accept", request_only=True, value=px.CONSENT_REQUEST),
            OpenApiExample("Accepted", response_only=True, value=px.CONSENT),
        ],
    )
    def post(self, request):
        body = s.ConsentRequestSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        result = services.record_consent(request.user, **body.validated_data)
        consent = result.instance
        response = Response(
            s.ConsentSerializer(
                {
                    "accepted": consent.accepted,
                    "at": consent.at,
                    "text_version": consent.text_version,
                }
            ).data
        )
        if not result.created:
            response[REPLAY_HEADER] = "true"
        return response


class PresenceEventsView(APIView):
    authentication_classes = [TrackerServiceAuthentication]
    permission_classes = [IsTrackerService]
    parser_classes = [JSONParser]

    @extend_schema(
        tags=TAGS,
        operation_id="presence_events",
        summary="Presence events from the tracker",
        description=(
            "Called by the tracker's outbox (service token), in batches of up to "
            f"{services.MAX_EVENTS}, oldest first. Each event is stored once by `event_id`; "
            "the sessions of the people in the batch are then rebuilt from every stored "
            "event in time order (`entered` opens a visit, `moved_zone` adds its zone, "
            "`left` closes it), so late, out-of-order or repeated events give the same "
            "sessions, and replaying a batch changes nothing.\n\n"
            "Every item is counted once: `stored` (new, applied), `duplicates`, `ignored` "
            "(new and stored, but an unknown site or subject, or one of another company) "
            "and `rejected` (an item that does not match the schema: not stored, logged). "
            "Any `200` means the batch is delivered. `400` only for a body that is not "
            f"`{{events: [...]}}` or has more than {services.MAX_EVENTS} events.\n\n"
            "Presence events are not attendance changes: no audit entry each."
        ),
        request=s.PresenceEventBatchSerializer,
        responses={
            200: s.PresenceIngestResultSerializer,
            **error_responses(
                400,
                401,
                examples={
                    400: [
                        (
                            "Too many events",
                            "Invalid input.",
                            {"events": [TOO_MANY_EVENTS]},
                        )
                    ],
                    401: [
                        ("No token", "Authentication credentials were not provided.", {}),
                        ("Bad token", "Invalid or expired tracker service token.", {}),
                    ],
                },
                descriptions={
                    401: "No service token, or one that is invalid, expired, not the "
                    "tracker's or valid for more than 5 minutes.",
                },
            ),
        },
        examples=[
            OpenApiExample("Batch", request_only=True, value=px.EVENT_BATCH),
            OpenApiExample("Stored", response_only=True, value=px.INGEST_RESULT),
        ],
    )
    def post(self, request):
        body = s.EventBatchSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        events, rejected = [], 0
        for index, item in enumerate(body.validated_data["events"]):
            event = s.PresenceEventSerializer(data=item)
            if event.is_valid():
                events.append(event.to_event())
            else:
                rejected += 1
                logger.warning("Presence: event %s of a batch rejected: %s", index, event.errors)
        result = services.ingest_events(events, rejected=rejected)
        return Response(
            s.PresenceIngestResultSerializer(
                {
                    "stored": result.stored,
                    "duplicates": result.duplicates,
                    "ignored": result.ignored,
                    "rejected": result.rejected,
                }
            ).data
        )


DATE_QUERY = OpenApiParameter(
    "date",
    str,
    OpenApiParameter.QUERY,
    required=True,
    description="Work day, ISO 8601 `YYYY-MM-DD`, in the company time zone.",
)


class SiteSessionsView(APIView):
    @extend_schema(
        tags=TAGS,
        operation_id="site_presence_sessions",
        summary="Presence sessions of a site for a day",
        description=(
            "For the roll call and the approvals screen: one row per person seen on the "
            "site that day (visits started that day, company time zone), first arrival "
            "first, with the first and last sighting and the zones visited. `open` rows "
            "have no `last_seen_at`: the person is still on site. The phone shows "
            '"seen 07:02–16:48, zone B" next to the worker; accepting it goes through the '
            "normal records upsert (its own `client_uuid` and audit entry). Presence never "
            "marks attendance.\n\n"
            "Site-scoped like every endpoint: a site you are not assigned to is `403`."
        ),
        parameters=[SITE_ID, DATE_QUERY],
        responses={200: s.SitePresenceSessionsSerializer, **error_responses(400, 401, 403)},
        examples=[OpenApiExample("Sessions", response_only=True, value=px.SESSIONS)],
    )
    def get(self, request, site_id):
        site = get_site_or_403(request.user, site_id)
        query = s.SessionsQuerySerializer(data=request.query_params)
        query.is_valid(raise_exception=True)
        date = query.validated_data["date"]
        rows = selectors.day_sessions(site, date)
        data = {
            "site_id": site.pk,
            "date": date,
            "sessions": [
                {
                    "subject": {"kind": row.subject_kind, "id": row.subject_id},
                    "first_seen_at": row.first_seen_at,
                    "last_seen_at": row.last_seen_at,
                    "open": row.open,
                    "zones": row.zones,
                    "visits": row.visits,
                }
                for row in rows
            ],
        }
        return Response(s.SitePresenceSessionsSerializer(data).data)
