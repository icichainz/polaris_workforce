"""Roll call over the API: days, batch records, submit, corrections, sync.

Views stay thin: they check the site scope, validate the body and call
``apps/attendance/services.py`` (writes) or ``selectors.py`` (reads). Reads are open to
anyone who can see the site; writes need a foreman (or an admin, for support).
"""

import datetime

from django.db import transaction
from django.shortcuts import get_object_or_404
from django.utils.translation import gettext_lazy as _
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiExample, OpenApiParameter, extend_schema
from rest_framework import serializers
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.accounts.permissions import IsForeman, get_site_or_403
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

FOREMAN = {"id": 7, "name": "Kossi Amegah"}
MANAGER = {"id": 4, "name": "Edem Kpodar"}


def _record(rid, uuid, worker_id, status, arrival, departure=None, note="", corrected=False):
    return {
        "id": rid,
        "client_uuid": uuid,
        "day_id": 210,
        "worker_id": worker_id,
        "status": status,
        "arrival": arrival,
        "departure": departure,
        "note": note,
        "corrected": corrected,
        "updated_at": "2026-10-31T07:41:12Z",
    }


RECORDS = [
    _record(9001, "3f2a7c1e-5b8d-4e0a-9c61-0d4b8e2f7a10", 44, "present", "07:00"),
    _record(9002, "8c4d1b2e-7f3a-4a9b-b2c5-6e1f0a3d9b21", 51, "half_day", "07:00", "12:00"),
    _record(9003, "a1e5f9c3-2d4b-4f7e-8a06-b3c2d1e0f932", 63, "late", "07:35"),
    _record(9004, "d7b3e2a1-9c8f-4b5d-a6e0-1f2c3b4a5d43", 58, "absent", None),
]

DAY_SUMMARY = {
    "id": 210,
    "site_id": 3,
    "date": "2026-10-31",
    "status": "submitted",
    "foreman_note": "Concrete delivery late, crew B started 07:30.",
    "submitted_at": "2026-10-31T07:42:05Z",
    "submitted_by": FOREMAN,
    "approved_at": None,
    "approved_by": None,
    "rejection_note": "",
    "counts": {
        "expected": 42,
        "present": 35,
        "absent": 4,
        "half_day": 2,
        "late": 1,
        "unmarked": 0,
    },
    "payout_status": "not_open",
    "updated_at": "2026-10-31T07:42:05Z",
}

DRAFT_DAY = {
    **DAY_SUMMARY,
    "status": "draft",
    "foreman_note": "",
    "submitted_at": None,
    "submitted_by": None,
    "updated_at": "2026-10-31T07:41:12Z",
    "records": RECORDS,
}

NOT_STARTED_DAY = {
    **DAY_SUMMARY,
    "id": None,
    "status": "not_started",
    "foreman_note": "",
    "submitted_at": None,
    "submitted_by": None,
    "counts": {
        "expected": 42,
        "present": 0,
        "absent": 0,
        "half_day": 0,
        "late": 0,
        "unmarked": 42,
    },
    "updated_at": None,
    "records": [],
}

CORRECTION = {
    "id": 77,
    "client_uuid": "5e9d0c4b-3a2f-4e1d-9b8c-7a6f5e4d3c21",
    "record_id": 9003,
    "field": "arrival",
    "old_value": "07:35",
    "new_value": "07:20",
    "reason": "Signed in at the gate at 07:20; marked late by mistake.",
    "created_by": FOREMAN,
    "created_at": "2026-10-31T09:15:00Z",
    "record": {**RECORDS[2], "arrival": "07:20", "corrected": True},
}


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
                value={
                    "site_id": 3,
                    "days": [
                        {
                            **DAY_SUMMARY,
                            "id": 209,
                            "date": "2026-10-30",
                            "status": "approved",
                            "approved_at": "2026-10-30T16:10:00Z",
                            "approved_by": MANAGER,
                            "counts": {
                                **DAY_SUMMARY["counts"],
                                "present": 40,
                                "absent": 2,
                                "half_day": 0,
                                "late": 0,
                            },
                            "payout_status": "paid",
                        },
                    ],
                },
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
        summaries = []
        date = end
        while date >= start:
            day = days.get(date)
            records = day.records.all() if day else []
            summaries.append(day_payload(site, date, day, expected[date], records))
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
            OpenApiExample("Draft", response_only=True, value=DRAFT_DAY),
            OpenApiExample("Not started", response_only=True, value=NOT_STARTED_DAY),
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
            "Allowed while the day is `not_started`, `draft` or `rejected` (a rejected "
            "day goes back to `draft`). Once `submitted`, `approved` or `locked`: "
            "`423 locked` — use corrections. A worker not on the crew that day: "
            "`400 validation_error`."
        ),
        parameters=[SITE_ID, DATE],
        request=RecordBatchSerializer,
        responses={200: AttendanceDaySerializer, **error_responses(400, 401, 403, 404, 423)},
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
                        for r in RECORDS
                    ]
                },
            ),
            OpenApiExample("Saved", response_only=True, value=DRAFT_DAY),
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
            "Idempotent: repeating it with the same `client_uuid` returns the day (200). "
            "Every crew member must be marked: otherwise `409 conflict` with "
            "`details.unmarked_worker_ids`. Already submitted with another `client_uuid`, "
            "or approved: `409 conflict`. Locked: `423 locked`."
        ),
        parameters=[SITE_ID, DATE],
        request=SubmitDaySerializer,
        responses={
            200: AttendanceDaySerializer,
            **error_responses(400, 401, 403, 404, 409, 423),
        },
        examples=[
            OpenApiExample(
                "Submit",
                request_only=True,
                value={
                    "client_uuid": "c0a8012e-4b7d-4f3e-9a1b-2d3c4e5f6a71",
                    "foreman_note": "Concrete delivery late, crew B started 07:30.",
                },
            ),
            OpenApiExample(
                "Submitted",
                response_only=True,
                value={**DAY_SUMMARY, "records": RECORDS},
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
                    "client_uuid": CORRECTION["client_uuid"],
                    "field": "arrival",
                    "new_value": "07:20",
                    "reason": CORRECTION["reason"],
                },
            ),
            OpenApiExample(
                "Created",
                response_only=True,
                status_codes=["201", "200"],
                value=CORRECTION,
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
                    "server_time": "2026-10-31T16:12:30Z",
                    "since": "2026-10-31T07:45:00Z",
                    "days": [
                        {
                            **DAY_SUMMARY,
                            "status": "approved",
                            "approved_at": "2026-10-31T16:10:00Z",
                            "approved_by": MANAGER,
                            "payout_status": "open",
                            "updated_at": "2026-10-31T16:10:00Z",
                        }
                    ],
                    "records": [CORRECTION["record"]],
                    "corrections": [CORRECTION],
                },
            )
        ],
    )
    def get(self, request):
        query = _SinceQuery(data=request.query_params)
        query.is_valid(raise_exception=True)
        changes = selectors.changes_since(request.user, query.validated_data.get("since"))
        days = changes.days
        expected = {day.pk: selectors.expected_worker_ids(day.site, day.date) for day in days}
        records_by_day = {}
        for record in AttendanceRecord.objects.filter(day__in=days):
            records_by_day.setdefault(record.day_id, []).append(record)
        payload = {
            "server_time": changes.server_time,
            "since": changes.since,
            "days": [
                day_payload(
                    day.site, day.date, day, expected[day.pk], records_by_day.get(day.pk, [])
                )
                for day in days
            ],
            "records": [record_payload(record) for record in changes.records],
            "corrections": [correction_payload(c) for c in changes.corrections],
        }
        return Response(SyncSerializer(payload).data)
