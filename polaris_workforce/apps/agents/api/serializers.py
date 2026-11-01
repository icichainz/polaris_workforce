"""The office API contract: what an AI assistant reads, and the two decision calls.

Responses are built as plain dicts by ``apps.agents.selectors`` and rendered through these
serializers, so the schema in ``docs/api/openapi.yaml`` is what the views send.
"""

from rest_framework import serializers

from apps.api.enums import ATTENDANCE_STATUSES, DAY_STATUSES, PAYOUT_STATUSES, USER_ROLES
from apps.api.serializers import CurrencySerializer
from apps.attendance.api.serializers import DayCountsSerializer

from ..services import DECISIONS

# --- shared ------------------------------------------------------------------------------


class MoneySerializer(serializers.Serializer):
    amount = serializers.IntegerField(help_text="Integer in the currency's smallest unit.")
    display = serializers.CharField(help_text="Written the company's way, e.g. `350 000 XOF`.")


class SiteRefSerializer(serializers.Serializer):
    id = serializers.IntegerField()
    name = serializers.CharField()


class WorkerRefSerializer(serializers.Serializer):
    id = serializers.IntegerField()
    name = serializers.CharField()
    number = serializers.CharField(help_text="Employee number.")
    trade = serializers.CharField(help_text="Empty when the worker has no trade.")


def _money(**kwargs):
    return MoneySerializer(**kwargs)


# --- me ----------------------------------------------------------------------------------


class OfficeUserSerializer(serializers.Serializer):
    id = serializers.IntegerField()
    name = serializers.CharField()
    role = serializers.ChoiceField(choices=USER_ROLES)


class OfficeCompanySerializer(serializers.Serializer):
    name = serializers.CharField()
    currency = CurrencySerializer()
    time_zone = serializers.CharField()
    today = serializers.DateField(help_text="Today in the company's time zone.")


class OfficeTokenSerializer(serializers.Serializer):
    name = serializers.CharField()
    can_approve = serializers.BooleanField(
        help_text="This token may approve or send back roll calls (after a preview)."
    )
    expires_at = serializers.DateTimeField()


class OfficeSiteSerializer(SiteRefSerializer):
    status = serializers.CharField()


class OfficeMeSerializer(serializers.Serializer):
    user = OfficeUserSerializer()
    company = OfficeCompanySerializer()
    token = OfficeTokenSerializer()
    sites = OfficeSiteSerializer(many=True, help_text="The sites this user may see.")


# --- today -------------------------------------------------------------------------------


class TodayTotalsSerializer(serializers.Serializer):
    expected = serializers.IntegerField()
    present = serializers.IntegerField()
    absent = serializers.IntegerField()
    half = serializers.IntegerField(help_text="Half days.")
    late = serializers.IntegerField()
    cash_tonight = _money(help_text="Cash to pay out tonight on every active site.")


class TodaySiteSerializer(serializers.Serializer):
    site = SiteRefSerializer()
    foreman = serializers.CharField()
    day_id = serializers.IntegerField(allow_null=True)
    status = serializers.ChoiceField(choices=DAY_STATUSES)
    roll_call_due = serializers.CharField(help_text="`HH:MM`.")
    overdue = serializers.BooleanField(help_text="The roll call is late (not yet submitted).")
    expected = serializers.IntegerField()
    present = serializers.IntegerField()
    absent = serializers.IntegerField()
    half = serializers.IntegerField()
    late = serializers.IntegerField()
    cash_tonight = _money(allow_null=True)


class AlertSerializer(serializers.Serializer):
    kind = serializers.ChoiceField(
        choices=[
            ("missing_roll_call", "Roll call not submitted after its due time"),
            ("corrections", "Records corrected after submission"),
            ("absent_streak", "A worker absent several days in a row"),
            ("unclosed_period", "A pay period past its end and not locked"),
        ]
    )
    severity = serializers.ChoiceField(choices=[("danger", "Danger"), ("warning", "Warning")])
    site = serializers.CharField()
    site_id = serializers.IntegerField(allow_null=True)
    date = serializers.DateField(allow_null=True)
    count = serializers.IntegerField()
    person = serializers.CharField()
    detail = serializers.CharField()
    worker_id = serializers.IntegerField(allow_null=True)
    day_id = serializers.IntegerField(allow_null=True)


class TodaySerializer(serializers.Serializer):
    date = serializers.DateField()
    now = serializers.CharField(help_text="Local time, ISO 8601.")
    is_working_day = serializers.BooleanField()
    totals = TodayTotalsSerializer()
    sites = TodaySiteSerializer(many=True)
    waiting_for_approval = serializers.IntegerField()
    alerts = AlertSerializer(many=True)


# --- approvals ---------------------------------------------------------------------------


class WaitingDaySerializer(serializers.Serializer):
    day_id = serializers.IntegerField()
    site = SiteRefSerializer()
    date = serializers.DateField()
    foreman = serializers.CharField()
    submitted_at = serializers.CharField(allow_null=True, help_text="Local time, ISO 8601.")
    counts = DayCountsSerializer()
    flags = serializers.ListField(
        child=serializers.CharField(),
        help_text="Why to look twice: corrections, a roll call that synced late.",
    )


class ApprovalsSerializer(serializers.Serializer):
    days = WaitingDaySerializer(many=True)


# --- one day -----------------------------------------------------------------------------


class ChangeSerializer(serializers.Serializer):
    field = serializers.CharField()
    old = serializers.CharField()
    new = serializers.CharField()
    reason = serializers.CharField()
    at = serializers.CharField(help_text="`HH:MM`, local time.")


class OfficeRecordSerializer(serializers.Serializer):
    worker = WorkerRefSerializer()
    status = serializers.ChoiceField(choices=ATTENDANCE_STATUSES)
    arrival = serializers.CharField(allow_null=True)
    departure = serializers.CharField(allow_null=True)
    note = serializers.CharField(allow_blank=True)
    corrected = serializers.BooleanField()
    changes = ChangeSerializer(many=True, help_text="Corrections made after submission.")
    paid_note = serializers.CharField(
        allow_blank=True, help_text="Set when the worker was already paid for this day."
    )


class OfficeDaySerializer(serializers.Serializer):
    day_id = serializers.IntegerField(allow_null=True, help_text="Null before any roll call.")
    site = SiteRefSerializer()
    date = serializers.DateField()
    status = serializers.ChoiceField(choices=DAY_STATUSES)
    foreman = serializers.CharField()
    submitted_at = serializers.CharField(allow_null=True)
    approved_at = serializers.CharField(allow_null=True)
    approved_by = serializers.CharField()
    rejection_note = serializers.CharField(allow_blank=True)
    foreman_note = serializers.CharField(allow_blank=True)
    counts = DayCountsSerializer()
    flags = serializers.ListField(child=serializers.CharField())
    records = OfficeRecordSerializer(many=True)
    may_decide = serializers.BooleanField(
        help_text="Submitted, so it can be approved or sent back."
    )


# --- cash sheet --------------------------------------------------------------------------


class OfficeCashLineSerializer(serializers.Serializer):
    worker = WorkerRefSerializer()
    units = serializers.DecimalField(max_digits=6, decimal_places=2, help_text="Days worked.")
    gross = MoneySerializer()
    advance_deduction = MoneySerializer()
    net = MoneySerializer()
    paid = MoneySerializer()
    is_paid = serializers.BooleanField()


class OfficeCashSheetSerializer(serializers.Serializer):
    site = SiteRefSerializer()
    date = serializers.DateField()
    status = serializers.ChoiceField(choices=PAYOUT_STATUSES)
    day_status = serializers.ChoiceField(choices=DAY_STATUSES)
    currency = CurrencySerializer()
    cash_issued = MoneySerializer()
    total_to_pay = MoneySerializer()
    paid_total = MoneySerializer()
    left_to_pay = MoneySerializer()
    cash_in_hand = MoneySerializer()
    line_count = serializers.IntegerField()
    paid_count = serializers.IntegerField()
    lines = OfficeCashLineSerializer(many=True)
    absent = WorkerRefSerializer(many=True)


# --- workers -----------------------------------------------------------------------------


class WorkerQuerySerializer(serializers.Serializer):
    q = serializers.CharField(required=False, allow_blank=True, max_length=100)
    site = serializers.IntegerField(required=False, min_value=1)


class WorkerMatchSerializer(WorkerRefSerializer):
    site = SiteRefSerializer(allow_null=True, help_text="Where the worker is assigned today.")


class WorkerListSerializer(serializers.Serializer):
    workers = WorkerMatchSerializer(many=True)


class WorkerDaySerializer(serializers.Serializer):
    date = serializers.DateField()
    site = SiteRefSerializer()
    day_status = serializers.ChoiceField(choices=DAY_STATUSES)
    status = serializers.ChoiceField(choices=ATTENDANCE_STATUSES)
    arrival = serializers.CharField(allow_null=True)
    departure = serializers.CharField(allow_null=True)
    note = serializers.CharField(allow_blank=True)
    corrected = serializers.BooleanField()


class WorkerPaySerializer(serializers.Serializer):
    site = SiteRefSerializer()
    start_date = serializers.DateField()
    end_date = serializers.DateField()
    period_status = serializers.CharField()
    units = serializers.DecimalField(max_digits=6, decimal_places=2)
    gross = MoneySerializer()
    advance_deduction = MoneySerializer()
    statutory_deductions = MoneySerializer()
    other_deductions = MoneySerializer()
    adjustment = MoneySerializer()
    net = MoneySerializer()
    paid = MoneySerializer()


class WorkerDetailSerializer(WorkerRefSerializer):
    active = serializers.BooleanField()
    site = SiteRefSerializer(allow_null=True)
    days = WorkerDaySerializer(many=True)
    pay = WorkerPaySerializer(many=True)
    advance_balance = MoneySerializer(help_text="Still owed on open salary advances.")

    def get_fields(self):
        # ``from`` is a Python keyword.
        fields = super().get_fields()
        fields["from"] = serializers.DateField()
        fields["to"] = serializers.DateField()
        return fields


class WorkerRangeSerializer(serializers.Serializer):
    def get_fields(self):
        return {
            "from": serializers.DateField(required=False),
            "to": serializers.DateField(required=False),
        }


# --- decisions ---------------------------------------------------------------------------


class DecisionPreviewRequestSerializer(serializers.Serializer):
    decision = serializers.ChoiceField(choices=[(d, d) for d in DECISIONS])
    note = serializers.CharField(
        required=False,
        allow_blank=True,
        max_length=1000,
        help_text="Required to reject: what the foreman should check.",
    )


class DecisionPreviewSerializer(serializers.Serializer):
    decision = serializers.ChoiceField(choices=[(d, d) for d in DECISIONS])
    note = serializers.CharField(allow_blank=True)
    day = OfficeDaySerializer()
    confirmation = serializers.CharField(
        help_text="Send it to the decision endpoint to carry out exactly this decision."
    )
    expires_at = serializers.DateTimeField()


class DecisionConfirmRequestSerializer(serializers.Serializer):
    confirmation = serializers.CharField(max_length=4000)


class DecisionResultSerializer(serializers.Serializer):
    decision = serializers.ChoiceField(choices=[(d, d) for d in DECISIONS])
    day = OfficeDaySerializer()
