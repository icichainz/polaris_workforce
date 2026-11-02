"""Attendance days, records, corrections and sync.

Plain serializers: the contract does not follow the models. The API uses words for the
record status (``present``, ``absent``, ``half_day``, ``late``); map them to the model's
values here.
"""

from rest_framework import serializers

from apps.api import enums
from apps.api.fields import ClockTimeField
from apps.api.idempotency import client_uuid_field
from apps.api.serializers import PersonRefSerializer
from apps.attendance import selectors
from apps.attendance.models import AttendanceRecord
from apps.checkin.api.serializers import SUGGESTION_SOURCES

MAX_RECORDS_PER_BATCH = 200

# API word → model value for a record status, and back. (Today they are the same words;
# keep the mapping so the contract never depends on the database values.)
STATUS_TO_MODEL = {
    "present": AttendanceRecord.Status.PRESENT,
    "absent": AttendanceRecord.Status.ABSENT,
    "half_day": AttendanceRecord.Status.HALF_DAY,
    "late": AttendanceRecord.Status.LATE,
}
STATUS_FROM_MODEL = {value: word for word, value in STATUS_TO_MODEL.items()}


# --- Responses ---------------------------------------------------------------------------


class DayCountsSerializer(serializers.Serializer):
    expected = serializers.IntegerField(help_text="Workers on the crew list that day.")
    present = serializers.IntegerField()
    absent = serializers.IntegerField()
    half_day = serializers.IntegerField()
    late = serializers.IntegerField()
    unmarked = serializers.IntegerField(help_text="expected minus marked.")


class AttendanceRecordSerializer(serializers.Serializer):
    id = serializers.IntegerField()
    client_uuid = serializers.UUIDField(
        help_text="The server's client_uuid for this record. The phone adopts it if it "
        "differs from its own (the same worker was marked from two phones)."
    )
    day_id = serializers.IntegerField()
    site_id = serializers.IntegerField(help_text="The day's site (same as the day's `site_id`).")
    date = serializers.DateField(help_text="The day's date (same as the day's `date`).")
    worker_id = serializers.IntegerField()
    status = serializers.ChoiceField(choices=enums.ATTENDANCE_STATUSES)
    arrival = ClockTimeField(allow_null=True)
    departure = ClockTimeField(allow_null=True)
    note = serializers.CharField(allow_blank=True)
    corrected = serializers.BooleanField(
        help_text="True once a correction was applied after submission."
    )
    updated_at = serializers.DateTimeField()


class AttendanceDaySummarySerializer(serializers.Serializer):
    id = serializers.IntegerField(
        allow_null=True, help_text="Null while the day is `not_started` (no row on the server)."
    )
    site_id = serializers.IntegerField()
    date = serializers.DateField()
    status = serializers.ChoiceField(choices=enums.DAY_STATUSES)
    foreman_note = serializers.CharField(allow_blank=True)
    submitted_at = serializers.DateTimeField(allow_null=True)
    submitted_by = PersonRefSerializer(allow_null=True)
    approved_at = serializers.DateTimeField(allow_null=True)
    approved_by = PersonRefSerializer(allow_null=True)
    rejection_note = serializers.CharField(
        allow_blank=True, help_text="Why the site manager sent the day back (status rejected)."
    )
    counts = DayCountsSerializer()
    payout_status = serializers.ChoiceField(
        choices=enums.PAYOUT_STATUSES, help_text="State of the day's cash payout."
    )
    updated_at = serializers.DateTimeField(allow_null=True)


class AttendanceDaySerializer(AttendanceDaySummarySerializer):
    records = AttendanceRecordSerializer(many=True)


class AttendanceDayListSerializer(serializers.Serializer):
    site_id = serializers.IntegerField()
    days = AttendanceDaySummarySerializer(many=True)


class CorrectionSerializer(serializers.Serializer):
    id = serializers.IntegerField()
    client_uuid = serializers.UUIDField()
    record_id = serializers.IntegerField()
    field = serializers.ChoiceField(choices=enums.CORRECTABLE_FIELDS)
    old_value = serializers.CharField(allow_null=True, allow_blank=True)
    new_value = serializers.CharField(allow_null=True, allow_blank=True)
    reason = serializers.CharField()
    created_by = PersonRefSerializer()
    created_at = serializers.DateTimeField()
    record = AttendanceRecordSerializer(help_text="The record after the correction.")


class SyncSerializer(serializers.Serializer):
    server_time = serializers.DateTimeField(
        help_text="Store it and send it as `since` next time. Taken before reading changes, "
        "so nothing is missed (an item may come twice; apply as upserts by id)."
    )
    since = serializers.DateTimeField(allow_null=True)
    days = AttendanceDaySummarySerializer(many=True)
    records = AttendanceRecordSerializer(many=True)
    corrections = CorrectionSerializer(many=True)


# --- Requests ----------------------------------------------------------------------------


class RecordUpsertSerializer(serializers.Serializer):
    client_uuid = client_uuid_field(
        help_text="One per worker per day, made on the phone the first time the worker is "
        "marked; reuse it for every later change of that record."
    )
    worker_id = serializers.IntegerField()
    status = serializers.ChoiceField(choices=enums.ATTENDANCE_STATUSES)
    arrival = ClockTimeField(allow_null=True, required=False, default=None)
    departure = ClockTimeField(allow_null=True, required=False, default=None)
    note = serializers.CharField(max_length=500, allow_blank=True, required=False, default="")
    from_suggestion = serializers.ListField(
        child=serializers.ChoiceField(choices=SUGGESTION_SOURCES),
        required=False,
        default=list,
        max_length=len(SUGGESTION_SOURCES),
        help_text="1.5: the mark is a roll-call suggestion the foreman accepted (or the "
        "phone pre-filled), with its `sources` from `GET .../suggestions/`: "
        "`[checkin]`, `[presence]` or `[checkin, presence]`. Empty or left out: marked "
        "by hand. Recorded in the audit entry of the batch; it changes nothing else.",
    )


class RecordBatchSerializer(serializers.Serializer):
    records = RecordUpsertSerializer(many=True, max_length=MAX_RECORDS_PER_BATCH)


class SubmitDaySerializer(serializers.Serializer):
    client_uuid = client_uuid_field()
    foreman_note = serializers.CharField(
        max_length=1000,
        allow_blank=True,
        required=False,
        default="",
        help_text="Optional note for the site manager.",
    )
    submitted_at = serializers.DateTimeField(
        required=False,
        allow_null=True,
        default=None,
        help_text="When the foreman tapped Submit, from the phone clock (ISO 8601 with "
        "offset). Optional. The office flags a roll call that reached the server an hour or "
        "more later. The day's own `submitted_at` stays the server time.",
    )


class CorrectionCreateSerializer(serializers.Serializer):
    client_uuid = client_uuid_field()
    field = serializers.ChoiceField(choices=enums.CORRECTABLE_FIELDS)
    new_value = serializers.CharField(
        allow_null=True,
        allow_blank=True,
        max_length=500,
        help_text="status: one of present/absent/half_day/late; arrival/departure: "
        "'HH:MM' or null; note: text.",
    )
    reason = serializers.CharField(
        max_length=500, help_text="Required: why the record changes after submission."
    )


# --- Payloads (model → the dicts the response serializers render) -----------------------


def person(user):
    if user is None:
        return None
    return {"id": user.pk, "name": str(user)}


def record_payload(record: AttendanceRecord) -> dict:
    """Uses ``record.day``: select or prefetch it when rendering many records."""
    return {
        "id": record.pk,
        "client_uuid": record.client_uuid,
        "day_id": record.day_id,
        "site_id": record.day.site_id,
        "date": record.day.date,
        "worker_id": record.worker_id,
        "status": STATUS_FROM_MODEL[record.status],
        "arrival": record.arrival,
        "departure": record.departure,
        "note": record.note,
        "corrected": record.corrected,
        "updated_at": record.updated_at,
    }


def day_payload(
    site, date, day, expected_ids: set[int], records, *, with_records=False, payout_status=None
) -> dict:
    """One day summary (``not_started`` when ``day`` is None).

    ``payout_status``: the day's, when the caller read it with others
    (``selectors.payout_statuses``); else it is read here.
    """
    records = list(records)
    if payout_status is None:
        payout_status = selectors.payout_status(day)
    data = {
        "id": day.pk if day else None,
        "site_id": site.pk,
        "date": date,
        "status": day.status if day else selectors.NOT_STARTED,
        "foreman_note": day.foreman_note if day else "",
        "submitted_at": day.submitted_at if day else None,
        "submitted_by": person(day.submitted_by) if day else None,
        "approved_at": day.approved_at if day else None,
        "approved_by": person(day.approved_by) if day else None,
        "rejection_note": day.rejection_note if day else "",
        "counts": selectors.day_counts(expected_ids, records),
        "payout_status": payout_status,
        "updated_at": day.updated_at if day else None,
    }
    if with_records:
        data["records"] = [record_payload(record) for record in records]
    return data


def full_day_payload(site, date, day) -> dict:
    """``AttendanceDaySerializer`` data: the day and its records."""
    records = list(day.records.all()) if day else []
    expected = selectors.expected_worker_ids(site, date)
    return day_payload(site, date, day, expected, records, with_records=True)


def correction_payload(correction) -> dict:
    new_value, old_value = correction.new_value, correction.old_value
    if correction.field == "status":
        old_value = STATUS_FROM_MODEL.get(old_value, old_value)
        new_value = STATUS_FROM_MODEL.get(new_value, new_value)
    elif correction.field in ("arrival", "departure"):
        old_value = old_value or None
        new_value = new_value or None
    return {
        "id": correction.pk,
        "client_uuid": correction.client_uuid,
        "record_id": correction.record_id,
        "field": correction.field,
        "old_value": old_value,
        "new_value": new_value,
        "reason": correction.reason,
        "created_by": person(correction.corrected_by),
        "created_at": correction.created_at,
        "record": record_payload(correction.record),
    }
