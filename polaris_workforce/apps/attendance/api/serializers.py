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

MAX_RECORDS_PER_BATCH = 200


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
