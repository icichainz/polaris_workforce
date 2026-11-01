"""Roll call over the API: days, batch records, submit, corrections, sync.

Views stay thin: they check the site scope, validate the body and call
``apps/attendance/services.py`` (writes) or ``selectors.py`` (reads). Reads are open to
anyone who can see the site; writes need a foreman (or an admin, for support).
"""

import datetime

from django.db import transaction
from django.db.models import prefetch_related_objects
from django.shortcuts import get_object_or_404
from django.utils.translation import gettext_lazy as _
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiExample, OpenApiParameter, extend_schema
from rest_framework import serializers
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.accounts.permissions import IsForeman, get_site_or_403
from apps.api import examples as ex
from apps.api.idempotency import IdempotentCreateMixin
from apps.api.schema import DATE, SITE_ID, error_responses
from apps.attendance import selectors, services
from apps.attendance.models import AttendanceDay, AttendanceRecord

from .serializers import (
    STATUS_TO_MODEL,
    AttendanceDayListSerializer,
    AttendanceDaySerializer,
    CorrectionCreateSerializer,
    CorrectionSerializer,
    RecordBatchSerializer,
    SubmitDaySerializer,
    SyncSerializer,
    correction_payload,
    day_payload,
    full_day_payload,
    record_payload,
)

MAX_DAYS_IN_RANGE = 31
WRITE_PERMISSIONS = [IsAuthenticated, IsForeman]


class _RangeQueryBase(serializers.Serializer):
    def validate(self, attrs):
        end = attrs.get("to") or selectors.company_today()
        start = attrs.get("from") or end - datetime.timedelta(days=6)
        if start > end:
            raise serializers.ValidationError({"from": [_("Must be on or before 'to'.")]})
        if (end - start).days + 1 > MAX_DAYS_IN_RANGE:
            raise serializers.ValidationError(
                {"from": [_("At most %(n)s days at a time.") % {"n": MAX_DAYS_IN_RANGE}]}
            )
        return {"from": start, "to": end}


# ``from`` is a Python keyword, hence type() to declare the field.
_RangeQuery = type(
    "_RangeQuery",
    (_RangeQueryBase,),
    {"from": serializers.DateField(required=False), "to": serializers.DateField(required=False)},
)


class _SinceQuery(serializers.Serializer):
    since = serializers.DateTimeField(required=False)


def _day(site, date):
    return (
        AttendanceDay.objects.filter(site=site, date=date)
        .select_related("submitted_by", "approved_by")
        .first()
    )


def _day_response(site, date):
    return Response(AttendanceDaySerializer(full_day_payload(site, date, _day(site, date))).data)


TAGS = ["attendance"]


class DayListView(APIView):
    @extend_schema(
        tags=TAGS,
        operation_id="site_days",
        summary="Day summaries for a date range",
        description=(
            "One summary per date in `from`…`to` (inclusive, at most 31 days), newest "
            "first, including `not_started` days. Feeds the 'This week' strip on the home "
            "screen. Default: the 7 days ending today."
        ),
        parameters=[
            SITE_ID,
            OpenApiParameter("from", OpenApiTypes.DATE, OpenApiParameter.QUERY),
            OpenApiParameter("to", OpenApiTypes.DATE, OpenApiParameter.QUERY),
        ],
        responses={200: AttendanceDayListSerializer, **error_responses(400, 401, 403, 404)},
        examples=[
            OpenApiExample(
                "This week",
                response_only=True,
                value={"site_id": ex.SITE_ID, "days": [ex.SUBMITTED_DAY, ex.PREVIOUS_DAY]},
            )
        ],
    )
    def get(self, request, site_id):
        site = get_site_or_403(request.user, site_id)
        query = _RangeQuery(data=request.query_params)
        query.is_valid(raise_exception=True)
        start, end = query.validated_data["from"], query.validated_data["to"]
        expected = selectors.expected_by_date(site, start, end)
        days = {
            day.date: day
            for day in AttendanceDay.objects.filter(site=site, date__range=(start, end))
            .select_related("submitted_by", "approved_by")
            .prefetch_related("records")
        }
        payouts = selectors.payout_statuses(days.values())
        summaries = []
        date = end
        while date >= start:
            day = days.get(date)
            records = day.records.all() if day else []
            payout = payouts[day.pk] if day else None
            summaries.append(
                day_payload(site, date, day, expected[date], records, payout_status=payout)
            )
            date -= datetime.timedelta(days=1)
        return Response(AttendanceDayListSerializer({"site_id": site.pk, "days": summaries}).data)


class DayDetailView(APIView):
    @extend_schema(
        tags=TAGS,
        operation_id="site_day",
        summary="The attendance day and its records",
        description=(
            "Always 200 for a valid site and date: a day with no row on the server comes "
            "back with `id: null`, `status: not_started` and no records."
        ),
        parameters=[SITE_ID, DATE],
        responses={200: AttendanceDaySerializer, **error_responses(401, 403, 404)},
        examples=[
            OpenApiExample(
                "Draft", response_only=True, value={**ex.DRAFT_DAY, "records": ex.RECORDS}
            ),
            OpenApiExample(
                "Not started", response_only=True, value={**ex.NOT_STARTED_DAY, "records": []}
            ),
        ],
    )
    def get(self, request, site_id, date):
        site = get_site_or_403(request.user, site_id)
        return _day_response(site, date)


class RecordBatchView(APIView):
    permission_classes = WRITE_PERMISSIONS

    @extend_schema(
        tags=TAGS,
        operation_id="site_day_records",
        summary="Save roll-call marks (batch upsert)",
        description=(
            "Creates the day (status `draft`) if needed and upserts every record in the "
            "batch (at most 200). Records not in the batch are left alone.\n\n"
            "**Idempotent per record.** The server finds each record by `client_uuid`, "
            "then by `worker_id` (one record per worker per day), and writes only fields "
            "that changed; replaying a batch changes nothing. The response carries the "
            "server's `client_uuid` per worker: adopt it.\n\n"
            "Changes are allowed while the day is `not_started`, `draft` or `rejected` (a "
            "rejected day goes back to `draft`). Once `submitted`, `approved` or "
            "`locked`, a batch that would change a record gets `423 locked` — use "
            "corrections; a batch that changes nothing (the server already has those "
            "values) answers `200` with the day, whatever its status. A worker not on the "
            "crew that day: `400 validation_error`, with one entry per batch item in "
            "`details.records` (`{}` for items without errors, also after the last "
            "error). A `client_uuid` already used for another worker or day: "
            "`409 conflict`."
        ),
        parameters=[SITE_ID, DATE],
        request=RecordBatchSerializer,
        responses={
            200: AttendanceDaySerializer,
            **error_responses(
                400,
                401,
                403,
                404,
                409,
                423,
                examples={
                    400: [
                        (
                            "Worker not on the crew",
                            "Invalid input.",
                            {
                                "records": [
                                    {},
                                    {
                                        "worker_id": [
                                            "This worker is not on the crew of this site that day."
                                        ]
                                    },
                                ]
                            },
                        )
                    ],
                    409: [
                        (
                            "client_uuid of another record",
                            "This client_uuid was already used for another record.",
                            {"client_uuid": ex.RECORDS[0]["client_uuid"]},
                        )
                    ],
                    423: [
                        (
                            "Already submitted",
                            "This day was already submitted: change records with a correction.",
                            {"status": "submitted"},
                        )
                    ],
                },
            ),
        },
        examples=[
            OpenApiExample(
                "Marks",
                request_only=True,
                value={
                    "records": [
                        {
                            "client_uuid": r["client_uuid"],
                            "worker_id": r["worker_id"],
                            "status": r["status"],
                            "arrival": r["arrival"],
                            "departure": r["departure"],
                            "note": r["note"],
                        }
                        for r in ex.RECORDS
                    ]
                },
            ),
            OpenApiExample(
                "Saved", response_only=True, value={**ex.DRAFT_DAY, "records": ex.RECORDS}
            ),
        ],
    )
    def put(self, request, site_id, date):
        site = get_site_or_403(request.user, site_id)
        body = RecordBatchSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        records = [
            {**item, "status": STATUS_TO_MODEL[item["status"]]}
            for item in body.validated_data["records"]
        ]
        with transaction.atomic():
            day = services.get_or_create_day(site, date, request.user)
            services.upsert_records(day, records, request.user)
        return _day_response(site, date)


class SubmitDayView(APIView):
    permission_classes = WRITE_PERMISSIONS

    @extend_schema(
        tags=TAGS,
        operation_id="site_day_submit",
        summary="Submit the roll call",
        description=(
            "Moves the day from `draft` to `submitted` and notifies the site manager. "
            "After this, changes need a correction with a reason.\n\n"
            "Send `submitted_at` (phone clock, when the foreman tapped Submit) if you "
            "have it: the office shows when the roll call was sent, and flags one that "
            "reached the server an hour or more after it was submitted on the phone. The "
            "day's `submitted_at` in the response is the server time.\n\n"
            "Idempotent: repeating it with the same `client_uuid` returns the day (200). "
            "Every crew member must be marked: otherwise `409 conflict` with "
            "`details.unmarked_worker_ids`. Already submitted with another `client_uuid`, "
            "or approved: `409 conflict`. Locked: `423 locked`."
        ),
        parameters=[SITE_ID, DATE],
        request=SubmitDaySerializer,
        responses={
            200: AttendanceDaySerializer,
            **error_responses(
                400,
                401,
                403,
                404,
                409,
                423,
                examples={
                    409: [
                        (
                            "Unmarked workers",
                            "Mark every worker on the crew before submitting.",
                            {"unmarked_worker_ids": [ex.KODJO["id"]]},
                        ),
                        (
                            "Already submitted",
                            "This day was already submitted.",
                            {"status": "submitted"},
                        ),
                    ],
                    423: [
                        (
                            "Locked",
                            "This day is locked and cannot be changed.",
                            {"status": "locked"},
                        )
                    ],
                },
            ),
        },
        examples=[
            OpenApiExample(
                "Submit",
                request_only=True,
                value={
                    "client_uuid": ex.SUBMIT_UUID,
                    "foreman_note": ex.FOREMAN_NOTE,
                    "submitted_at": ex.SUBMITTED_ON_PHONE_AT,
                },
            ),
            OpenApiExample(
                "Submitted",
                response_only=True,
                value={**ex.SUBMITTED_DAY, "records": ex.RECORDS},
            ),
        ],
    )
    def post(self, request, site_id, date):
        site = get_site_or_403(request.user, site_id)
        body = SubmitDaySerializer(data=request.data)
        body.is_valid(raise_exception=True)
        with transaction.atomic():
            day = services.get_or_create_day(site, date, request.user)
            services.submit_day(
                day,
                request.user,
                body.validated_data["client_uuid"],
                body.validated_data["foreman_note"],
                client_submitted_at=body.validated_data["submitted_at"],
            )
        return _day_response(site, date)


class CorrectionCreateView(IdempotentCreateMixin, APIView):
    permission_classes = WRITE_PERMISSIONS

    @extend_schema(
        tags=TAGS,
        operation_id="record_correction",
        summary="Correct a record after submission",
        description=(
            "For days that are `submitted` or `approved`. `reason` is required. Writes an "
            "AttendanceCorrection and an AuditLog entry; an approved day goes back to the "
            "site manager for review.\n\n"
            "201 when created; repeating the same `client_uuid` returns the original "
            "correction with 200 and `Idempotent-Replayed: true`. `client_uuid` reused for "
            "another record: `409 conflict`. Day still `draft`/`rejected` (use the batch "
            "PUT): `409 conflict`. Locked day: `423 locked`.\n\n"
            "The record id comes from a records/day response; an offline phone sends its "
            "corrections only after the outbox has stored the record ids."
        ),
        parameters=[
            OpenApiParameter("record_id", OpenApiTypes.INT, OpenApiParameter.PATH),
        ],
        request=CorrectionCreateSerializer,
        responses={
            201: CorrectionSerializer,
            200: CorrectionSerializer,
            **error_responses(400, 401, 403, 404, 409, 423),
        },
        examples=[
            OpenApiExample(
                "Correct arrival",
                request_only=True,
                value={
                    "client_uuid": ex.CORRECTION["client_uuid"],
                    "field": "arrival",
                    "new_value": "07:20",
                    "reason": ex.CORRECTION["reason"],
                },
            ),
            OpenApiExample(
                "Created",
                response_only=True,
                status_codes=["201", "200"],
                value=ex.CORRECTION,
            ),
        ],
    )
    def post(self, request, record_id):
        record = get_object_or_404(AttendanceRecord.objects.select_related("day"), pk=record_id)
        get_site_or_403(request.user, record.day.site_id)
        body = CorrectionCreateSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        data = body.validated_data
        new_value = data["new_value"]
        if data["field"] == "status":
            new_value = STATUS_TO_MODEL.get(new_value, new_value)
        result = services.correct_record(
            record, data["field"], new_value, data["reason"], request.user, data["client_uuid"]
        )
        payload = correction_payload(result.instance)
        return self.idempotent_response(result, CorrectionSerializer(payload).data)


class SyncView(APIView):
    @extend_schema(
        tags=TAGS,
        operation_id="sync",
        summary="Changes since the last sync",
        description=(
            "Days (statuses, approvals, rejections, payout status), records and "
            "corrections changed after `since`, for every site the user is assigned to. "
            "Without `since` (first sync): the last 14 days.\n\n"
            "Store `server_time` and send it as `since` next time. **The server wins**: "
            "apply every item as an upsert by id; a record corrected in the office "
            "replaces the phone's copy (keep unsent outbox writes and resend them; they "
            "get `423`/`409` if no longer allowed, which the app shows to the foreman)."
        ),
        parameters=[
            OpenApiParameter(
                "since",
                OpenApiTypes.DATETIME,
                OpenApiParameter.QUERY,
                required=False,
                description="`server_time` of the previous sync (ISO 8601 with offset).",
            )
        ],
        responses={200: SyncSerializer, **error_responses(400, 401)},
        examples=[
            OpenApiExample(
                "Changes",
                response_only=True,
                value={
                    "server_time": ex.SERVER_TIME,
                    "since": ex.SYNCED_SINCE,
                    "days": [ex.APPROVED_DAY],
                    "records": [ex.CORRECTED_RECORD],
                    "corrections": [ex.CORRECTION],
                },
            )
        ],
    )
    def get(self, request):
        query = _SinceQuery(data=request.query_params)
        query.is_valid(raise_exception=True)
        changes = selectors.changes_since(request.user, query.validated_data.get("since"))
        prefetch_related_objects(changes.records, "day")
        prefetch_related_objects(changes.corrections, "record__day")
        days = changes.days
        expected = selectors.expected_for_days(days)
        payouts = selectors.payout_statuses(days)
        records_by_day = {}
        for record in AttendanceRecord.objects.filter(day__in=days):
            records_by_day.setdefault(record.day_id, []).append(record)
        payload = {
            "server_time": changes.server_time,
            "since": changes.since,
            "days": [
                day_payload(
                    day.site,
                    day.date,
                    day,
                    expected[day.pk],
                    records_by_day.get(day.pk, []),
                    payout_status=payouts[day.pk],
                )
                for day in days
            ],
            "records": [record_payload(record) for record in changes.records],
            "corrections": [correction_payload(c) for c in changes.corrections],
        }
        return Response(SyncSerializer(payload).data)
