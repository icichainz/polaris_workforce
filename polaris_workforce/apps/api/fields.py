"""Serializer fields with the API's wire formats (money, clock times)."""

from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers


@extend_schema_field(
    {
        "type": "integer",
        "format": "int64",
        "description": (
            "Money: an integer in the currency's smallest unit (see the `currency` object "
            "of the same response). 5000 XOF (0 decimals) is 5000; 12.50 EUR is 1250."
        ),
    }
)
class MoneyField(serializers.IntegerField):
    """Money is never a float: an integer in the currency's smallest unit."""


@extend_schema_field(
    {
        "type": "string",
        "pattern": r"^([01]\d|2[0-3]):[0-5]\d$",
        "example": "07:00",
        "description": "Local wall-clock time on the site, 24-hour `HH:MM`.",
    }
)
class ClockTimeField(serializers.TimeField):
    """``"HH:MM"`` both ways."""

    def __init__(self, **kwargs):
        kwargs.setdefault("format", "%H:%M")
        kwargs.setdefault("input_formats", ["%H:%M"])
        super().__init__(**kwargs)
