"""Serializers shared by every API package: errors, currency, references."""

from rest_framework import serializers

from . import enums


class ErrorSerializer(serializers.Serializer):
    code = serializers.ChoiceField(
        choices=enums.ERROR_CODES, help_text="Stable machine-readable code. Branch on this."
    )
    message = serializers.CharField(help_text="Human-readable, translated (Accept-Language).")
    details = serializers.DictField(
        help_text=(
            "Always an object. validation_error: field → list of messages (nested for lists, "
            "e.g. `records[2].status`); throttled: `retry_after` seconds; others: context."
        )
    )


class CurrencySerializer(serializers.Serializer):
    code = serializers.CharField(help_text="ISO 4217 code, e.g. XOF.")
    decimals = serializers.IntegerField(
        min_value=0,
        max_value=3,
        help_text="Decimal places of the smallest unit. Display amount = value / 10**decimals.",
    )


class PersonRefSerializer(serializers.Serializer):
    id = serializers.IntegerField()
    name = serializers.CharField()


class HealthSerializer(serializers.Serializer):
    status = serializers.CharField()
