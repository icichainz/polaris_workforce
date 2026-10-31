"""Cash payout sheet and payments. Plain serializers: the contract does not follow the models.

Money is an integer in the currency's smallest unit (``MoneyField``), always next to a
``currency`` object in the same response.
"""

from rest_framework import serializers

from apps.api import enums
from apps.api.fields import MoneyField
from apps.api.idempotency import client_uuid_field
from apps.api.serializers import CurrencySerializer, PersonRefSerializer


class PayoutWorkerSerializer(serializers.Serializer):
    id = serializers.IntegerField()
    name = serializers.CharField()
    trade = serializers.CharField(allow_null=True)
    photo_url = serializers.URLField(allow_null=True)


class PaymentSummarySerializer(serializers.Serializer):
    id = serializers.IntegerField()
    client_uuid = serializers.UUIDField()
    amount = MoneyField()
    proof_type = serializers.ChoiceField(choices=enums.PROOF_TYPES)
    paid_at = serializers.DateTimeField()


class PayoutLineSerializer(serializers.Serializer):
    pay_line_id = serializers.IntegerField(help_text="Send this to `POST /payments/`.")
    worker = PayoutWorkerSerializer()
    worked = serializers.CharField(
        help_text="Translated label of what was worked, e.g. '1 day', '½ day', '1 day · late'."
    )
    units = serializers.DecimalField(
        max_digits=6,
        decimal_places=2,
        help_text="Units paid (days or hours, by pay type), as a decimal string.",
    )
    gross = MoneyField(help_text="Earned for the day.")
    advance_deduction = MoneyField(help_text="Advance repayment taken from this pay (0 if none).")
    net = MoneyField(help_text="Hand over: gross − advance_deduction.")
    payment = PaymentSummarySerializer(allow_null=True, help_text="Null until paid.")


class PayoutSerializer(serializers.Serializer):
    site_id = serializers.IntegerField()
    date = serializers.DateField()
    status = serializers.ChoiceField(
        choices=enums.PAYOUT_STATUSES,
        help_text="`not_open`: the roll call is not approved yet; figures are a preview "
        "and payments are refused (409).",
    )
    day_status = serializers.ChoiceField(choices=enums.DAY_STATUSES)
    approved_at = serializers.DateTimeField(allow_null=True)
    currency = CurrencySerializer()
    cash_issued = MoneyField(help_text="Cash handed to the foreman for this sheet.")
    total_to_pay = MoneyField(help_text="Sum of `net` over all lines.")
    paid_total = MoneyField(help_text="Sum of payments made.")
    left_to_pay = MoneyField(help_text="total_to_pay − paid_total.")
    cash_in_hand = MoneyField(
        help_text="cash_issued − paid_total: what the foreman should hold now "
        "(and return to the office once everyone is paid)."
    )
    line_count = serializers.IntegerField()
    paid_count = serializers.IntegerField()
    lines = PayoutLineSerializer(many=True)


class PaymentCreateSerializer(serializers.Serializer):
    client_uuid = client_uuid_field()
    pay_line_id = serializers.IntegerField()
    amount = MoneyField(min_value=0, help_text="Smallest unit. Normally the line's `net`.")
    proof_type = serializers.ChoiceField(choices=enums.PROOF_TYPES)
    proof_file = serializers.FileField(
        required=False,
        allow_null=True,
        help_text="JPEG/PNG of the photo, signature or thumb mark (compress on the phone; "
        "≤ 2 MB). Whether it is required depends on the company's proof setting.",
    )
    paid_at = serializers.DateTimeField(
        required=False,
        help_text="When the cash was handed over, from the phone clock (offline payments are "
        "sent later). Default: the time the server receives it.",
    )


class PaymentSerializer(serializers.Serializer):
    id = serializers.IntegerField()
    client_uuid = serializers.UUIDField()
    pay_line_id = serializers.IntegerField()
    amount = MoneyField()
    currency = CurrencySerializer()
    proof_type = serializers.ChoiceField(choices=enums.PROOF_TYPES)
    proof_url = serializers.URLField(allow_null=True)
    paid_at = serializers.DateTimeField()
    paid_by = PersonRefSerializer()
