"""URL converters shared by the per-app API routes.

Importing this module registers ``<isodate:date>`` (``YYYY-MM-DD`` → ``datetime.date``).
An impossible date such as ``2026-02-30`` does not match the route, so it is a 404.
"""

import datetime

from django.urls import register_converter


class IsoDateConverter:
    regex = r"\d{4}-\d{2}-\d{2}"

    def to_python(self, value: str) -> datetime.date:
        return datetime.date.fromisoformat(value)  # ValueError → no match

    def to_url(self, value) -> str:
        return value.isoformat() if isinstance(value, datetime.date) else str(value)


register_converter(IsoDateConverter, "isodate")
