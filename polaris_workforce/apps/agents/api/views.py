"""The office API, ``/api/v1/office/``: what an AI assistant may do for an office user.

Only agent tokens open it (``AgentTokenAuthentication``), and agent tokens open nothing
else. Reads follow the user's scope like the office screens; the one write is a roll-call
decision, in two calls (preview, then confirm), for tokens that may approve. Payments,
cash, advances, workers and settings cannot be changed here.
"""

import datetime

from django.core.exceptions import PermissionDenied
from django.utils.translation import gettext_lazy as _
from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework import serializers
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.accounts.permissions import get_site_or_403
from apps.api.idempotency import REPLAY_HEADER
from apps.api.schema import DATE, SITE_ID, error_responses
from apps.core import dates
from apps.workers.services import scoped_workers

from .. import selectors, services
from ..authentication import (
    AgentTokenAuthentication,
    AgentTokenCanApprove,
    AgentTokenThrottle,
    IsAgentToken,
)
from . import serializers as s

TAGS = ["office"]
MAX_WORKER_DAYS = 62
DEFAULT_WORKER_DAYS = 14

DAY_ID = OpenApiParameter("day_id", int, OpenApiParameter.PATH, description="Attendance day id.")
WORKER_ID = OpenApiParameter("worker_id", int, OpenApiParameter.PATH, description="Worker id.")


class OfficeView(APIView):
    authentication_classes = [AgentTokenAuthentication]
    permission_classes = [IsAuthenticated, IsAgentToken]
    throttle_classes = [AgentTokenThrottle]


class MeView(OfficeView):
    @extend_schema(
        tags=TAGS,
        operation_id="office_me",
        summary="Who the token acts for",
        description=(
            "The office user behind the agent token, their company (currency, time zone, "
            "today's date), what the token may do and the sites the user may see."
        ),
        responses={200: s.OfficeMeSerializer, **error_responses(401, 429)},
    )
    def get(self, request):
        return Response(s.OfficeMeSerializer(selectors.me_data(request.user, request.auth)).data)


class TodayView(OfficeView):
    @extend_schema(
        tags=TAGS,
        operation_id="office_today",
        summary="Today on the sites",
        description=(
            "The office dashboard: each active site's roll call (status, counts, cash to pay "
            "tonight), how many days wait for approval, and what needs attention."
        ),
        responses={200: s.TodaySerializer, **error_responses(401, 429)},
    )
    def get(self, request):
        return Response(s.TodaySerializer(selectors.today_data(request.user)).data)


class ApprovalsView(OfficeView):
    @extend_schema(
        tags=TAGS,
        operation_id="office_approvals",
        summary="Roll calls waiting for approval",
        description="Submitted days on the user's sites, oldest first, with what to check.",
        responses={200: s.ApprovalsSerializer, **error_responses(401, 429)},
    )
    def get(self, request):
        data = {"days": selectors.approvals_data(request.user)}
        return Response(s.ApprovalsSerializer(data).data)


class DayView(OfficeView):
    @extend_schema(
        tags=TAGS,
        operation_id="office_day",
        summary="A site's roll call for a date",
        description=(
            "Every worker's mark, arrival and departure, with the corrections made after "
            "submission and the reason given. `status` is `not_started` (and `day_id` null) "
            "when no roll call was taken."
        ),
        parameters=[SITE_ID, DATE],
        responses={200: s.OfficeDaySerializer, **error_responses(401, 403, 429)},
    )
    def get(self, request, site_id, date):
        site = get_site_or_403(request.user, site_id)
        return Response(s.OfficeDaySerializer(selectors.day_data(request.user, site, date)).data)


class CashSheetView(OfficeView):
    @extend_schema(
        tags=TAGS,
        operation_id="office_cash_sheet",
        summary="A site's cash sheet for a pay day",
        description=(
            "Who is paid how much, what was already paid and the cash reconciliation. Read "
            "only: payments are recorded by the foreman with proof of receipt, never here."
        ),
        parameters=[SITE_ID, DATE],
        responses={200: s.OfficeCashSheetSerializer, **error_responses(401, 403, 429)},
    )
    def get(self, request, site_id, date):
        site = get_site_or_403(request.user, site_id)
        return Response(s.OfficeCashSheetSerializer(selectors.cash_sheet_data(site, date)).data)


class WorkerListView(OfficeView):
    @extend_schema(
        tags=TAGS,
        operation_id="office_workers",
        summary="Find workers",
        description=(
            f"Active workers the user may see, by name or number (`q`) and/or by today's "
            f"site. At most {selectors.MAX_WORKERS}. No phone numbers or ID documents."
        ),
        parameters=[s.WorkerQuerySerializer],
        responses={200: s.WorkerListSerializer, **error_responses(400, 401, 403, 429)},
    )
    def get(self, request):
        query = s.WorkerQuerySerializer(data=request.query_params)
        query.is_valid(raise_exception=True)
        site_id = query.validated_data.get("site")
        site = get_site_or_403(request.user, site_id) if site_id else None
        workers = selectors.find_workers(request.user, query.validated_data.get("q", ""), site)
        return Response(s.WorkerListSerializer({"workers": workers}).data)


class WorkerDetailView(OfficeView):
    @extend_schema(
        tags=TAGS,
        operation_id="office_worker",
        summary="A worker's days and pay",
        description=(
            f"Roll-call marks and pay lines between `from` and `to` (default: the last "
            f"{DEFAULT_WORKER_DAYS} days; at most {MAX_WORKER_DAYS} days), on the user's "
            f"sites only, and what the worker still owes on salary advances."
        ),
        parameters=[WORKER_ID, s.WorkerRangeSerializer],
        responses={200: s.WorkerDetailSerializer, **error_responses(400, 401, 403, 429)},
    )
    def get(self, request, worker_id):
        today = dates.company_today()
        worker = scoped_workers(request.user, today).select_related("trade").filter(pk=worker_id)
        worker = worker.first()
        if worker is None:
            raise PermissionDenied
        query = s.WorkerRangeSerializer(data=request.query_params)
        query.is_valid(raise_exception=True)
        end = query.validated_data.get("to") or today
        start = query.validated_data.get("from") or end - datetime.timedelta(
            days=DEFAULT_WORKER_DAYS - 1
        )
        if start > end:
            raise serializers.ValidationError({"from": [_("Must be on or before 'to'.")]})
        if (end - start).days + 1 > MAX_WORKER_DAYS:
            raise serializers.ValidationError(
                {"from": [_("At most %(n)s days at a time.") % {"n": MAX_WORKER_DAYS}]}
            )
        data = selectors.worker_data(request.user, worker, start, end)
        return Response(s.WorkerDetailSerializer(data).data)


class DecisionPreviewView(OfficeView):
    permission_classes = [IsAuthenticated, IsAgentToken, AgentTokenCanApprove]

    @extend_schema(
        tags=TAGS,
        operation_id="office_day_decision_preview",
        summary="Preview approving or sending back a roll call",
        description=(
            "Step 1 of 2. Checks that the decision can be taken and returns the day as it "
            "will be decided, plus a `confirmation`. Nothing changes. Show the preview to the "
            "person and send the confirmation only once they agree. `reject` sends the day "
            "back to the foreman and needs a `note`. The confirmation expires after "
            f"{int(services.CONFIRMATION_MAX_AGE.total_seconds() // 60)} minutes."
        ),
        parameters=[DAY_ID],
        request=s.DecisionPreviewRequestSerializer,
        responses={200: s.DecisionPreviewSerializer, **error_responses(400, 401, 403, 409, 429)},
    )
    def post(self, request, day_id):
        body = s.DecisionPreviewRequestSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        day = services.scoped_day(request.user, day_id)
        preview = services.preview_decision(
            request.auth, day, body.validated_data["decision"], body.validated_data.get("note", "")
        )
        data = {
            "decision": preview.decision,
            "note": preview.note,
            "day": selectors.day_data(request.user, day.site, day.date),
            "confirmation": preview.confirmation,
            "expires_at": preview.expires_at,
        }
        return Response(s.DecisionPreviewSerializer(data).data)


class DecisionConfirmView(OfficeView):
    permission_classes = [IsAuthenticated, IsAgentToken, AgentTokenCanApprove]

    @extend_schema(
        tags=TAGS,
        operation_id="office_day_decision",
        summary="Carry out a previewed decision",
        description=(
            "Step 2 of 2. Approves or sends back the day exactly as previewed. Fails with "
            "`409` if the roll call changed after the preview, and `400` if the confirmation "
            "expired: preview again. Sending the same confirmation twice answers the current "
            f"day with `{REPLAY_HEADER}: true` instead of deciding twice."
        ),
        request=s.DecisionConfirmRequestSerializer,
        responses={200: s.DecisionResultSerializer, **error_responses(400, 401, 403, 409, 429)},
    )
    def post(self, request):
        body = s.DecisionConfirmRequestSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        result = services.confirm_decision(request.auth, body.validated_data["confirmation"])
        day = result.day
        day.refresh_from_db()
        data = {
            "decision": result.decision,
            "day": selectors.day_data(request.user, day.site, day.date),
        }
        response = Response(s.DecisionResultSerializer(data).data)
        if result.replayed:
            response[REPLAY_HEADER] = "true"
        return response
