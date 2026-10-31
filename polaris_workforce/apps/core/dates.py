"""The company's clock: its time zone, the current time there and today's work day.

A work day is a date in the company time zone (``CompanySettings.time_zone``), not on
the server and not on the phone. Every app reads "now" and "today" through these
helpers; pass the ``CompanySettings`` you already have to save a query.
"""

import datetime
import zoneinfo

from django.utils import timezone

from .models import CompanySettings


def company_tz(company: CompanySettings | None = None) -> datetime.tzinfo:
    """The company time zone (UTC if the setting is empty or unknown)."""
    company = company or CompanySettings.load()
    try:
        return zoneinfo.ZoneInfo(company.time_zone or "UTC")
    except (zoneinfo.ZoneInfoNotFoundError, ValueError):
        return datetime.UTC


def company_now(company: CompanySettings | None = None) -> datetime.datetime:
    """The current time, as an aware datetime in the company time zone."""
    return timezone.now().astimezone(company_tz(company))


def company_today(company: CompanySettings | None = None) -> datetime.date:
    """Today's work day in the company time zone."""
    return company_now(company).date()
