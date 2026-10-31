"""Foreman sign-in and profile. Plain serializers: the contract does not follow the models."""

from rest_framework import serializers

from apps.api import enums
from apps.api.fields import ClockTimeField
from apps.api.serializers import CurrencySerializer, PersonRefSerializer


class PinLoginSerializer(serializers.Serializer):
    phone = serializers.CharField(
        max_length=32, help_text="The foreman's phone number, as entered on the User record."
    )
    pin = serializers.RegexField(
        r"^\d{4}$", max_length=4, write_only=True, help_text="The 4-digit PIN."
    )
    device_id = serializers.CharField(
        max_length=128,
        help_text="Stable install ID made by the app on first launch (UUID). One token per device.",
    )
    device_name = serializers.CharField(
        max_length=100, required=False, allow_blank=True, help_text="e.g. 'Tecno Spark 10'."
    )


class DeviceSerializer(serializers.Serializer):
    id = serializers.IntegerField()
    device_id = serializers.CharField()
    name = serializers.CharField(allow_blank=True)


class UserProfileSerializer(serializers.Serializer):
    id = serializers.IntegerField()
    name = serializers.CharField(help_text="Full name; the app derives initials from it.")
    role = serializers.ChoiceField(choices=enums.USER_ROLES)
    phone = serializers.CharField(allow_blank=True)
    language = serializers.ChoiceField(
        choices=enums.LANGUAGES, help_text="UI language for this user (falls back to company)."
    )


class AssignedSiteSerializer(serializers.Serializer):
    id = serializers.IntegerField()
    name = serializers.CharField()
    address = serializers.CharField(allow_blank=True)
    manager = PersonRefSerializer(
        allow_null=True, help_text="Site manager who approves the roll call."
    )
    work_start = ClockTimeField(
        allow_null=True, help_text="Usual start of the work day: default arrival time."
    )
    roll_call_due = ClockTimeField(
        allow_null=True, help_text="Time by which the morning roll call should be submitted."
    )


class PaymentRulesSerializer(serializers.Serializer):
    accepted_proofs = serializers.ListField(
        child=serializers.ChoiceField(choices=enums.PROOF_TYPES),
        help_text="Proof types `POST /payments/` accepts (company setting). Offer only these "
        "on the payout screen; `none` (no proof) is accepted only when it is listed. "
        "Anything else: `400 validation_error` on `proof_type`.",
    )
    file_required_for = serializers.ListField(
        child=serializers.ChoiceField(choices=enums.PROOF_TYPES),
        help_text="Proof types that must come with a `proof_file` (today `photo`). For the "
        "others a file is optional. Missing file: `400 validation_error` on `proof_file`.",
    )


class CompanySerializer(serializers.Serializer):
    country = serializers.CharField(help_text="ISO 3166-1 alpha-2, e.g. TG.")
    time_zone = serializers.CharField(
        help_text="IANA time zone, e.g. Africa/Lome. Decides which date is 'today'."
    )
    currency = CurrencySerializer()
    payments = PaymentRulesSerializer(help_text="Proof-of-payment rules for the payout screen.")


class PinLoginResponseSerializer(serializers.Serializer):
    token = serializers.CharField(
        help_text="Device token. Shown once; keep it in secure storage. "
        "Send it as `Authorization: Device <token>`."
    )
    device = DeviceSerializer()
    user = UserProfileSerializer()
    sites = AssignedSiteSerializer(many=True)


class MeSerializer(UserProfileSerializer):
    company = CompanySerializer()
    sites = AssignedSiteSerializer(many=True)
