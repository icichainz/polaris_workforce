"""Crew list for the roll call. Plain serializers: the contract does not follow the models."""

from rest_framework import serializers

from apps.api import enums


class CrewQuerySerializer(serializers.Serializer):
    date = serializers.DateField(
        required=False, help_text="Work day (YYYY-MM-DD). Default: today in the company time zone."
    )


class CrewWorkerSerializer(serializers.Serializer):
    id = serializers.IntegerField()
    number = serializers.CharField(help_text="Worker number, e.g. W-0142.")
    first_name = serializers.CharField()
    last_name = serializers.CharField()
    trade = serializers.CharField(allow_null=True, help_text="Trade name, translated.")
    photo_url = serializers.URLField(
        allow_null=True,
        help_text="Absolute URL of the photo (`GET /files/workers/{id}/photo/`). Send the "
        "`Authorization` header; cache it on the phone.",
    )
    pay_type = serializers.ChoiceField(
        choices=enums.PAY_TYPES, help_text="Pay type of the rate in effect that day."
    )
    absent_streak = serializers.IntegerField(
        min_value=0,
        help_text="Consecutive working days absent right before this date "
        "(review screen: 'Absent · 3rd day in a row').",
    )


class CrewSerializer(serializers.Serializer):
    site_id = serializers.IntegerField()
    date = serializers.DateField()
    count = serializers.IntegerField(help_text="Number of workers expected that day.")
    workers = CrewWorkerSerializer(many=True)
