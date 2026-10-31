"""Read-only attendance queries: crew expected on a day, counts, payout status, sync.

Writes live in ``services.py``. Both are shared by the API, the office screens and,
later, payroll.
"""

from __future__ import annotations

import datetime
from collections import Counter
from dataclasses import dataclass, field
from zoneinfo import ZoneInfo

from django.db.models import Q
from django.utils import timezone
from django.utils.translation import ngettext

from apps.accounts.permissions import scoped_sites
from apps.api.enums import CORRECTABLE_FIELDS
from apps.core.models import CompanySettings
from apps.worksites.models import SiteAssignment

from .models import AttendanceCorrection, AttendanceDay, AttendanceRecord

NOT_STARTED = "not_started"
SYNC_DEFAULT_DAYS = 14

# --- Company time zone -----------------------------------------------------------------


def company_tz(company: CompanySettings | None = None) -> ZoneInfo:
    company = company or CompanySettings.load()
    return ZoneInfo(company.time_zone or "UTC")


def company_today(company: CompanySettings | None = None) -> datetime.date:
    """Today's work day in the company time zone (not the server's, not the phone's)."""
    return timezone.now().astimezone(company_tz(company)).date()


# --- Crew expected on a day ------------------------------------------------------------


def _assignments(site, start: datetime.date, end: datetime.date):
    return (
        SiteAssignment.objects.filter(site=site, worker__is_active=True, start_date__lte=end)
        .filter(Q(end_date__isnull=True) | Q(end_date__gte=start))
        .values_list("worker_id", "start_date", "end_date")
    )


def expected_worker_ids(site, date: datetime.date) -> set[int]:
    """Active workers assigned to ``site`` on ``date`` (assignment dates inclusive)."""
    return {worker_id for worker_id, _start, _end in _assignments(site, date, date)}


def expected_by_date(site, start: datetime.date, end: datetime.date) -> dict:
    """``{date: {worker_id, …}}`` for every date from ``start`` to ``end`` inclusive."""
    rows = list(_assignments(site, start, end))
    result = {}
    day = start
    while day <= end:
        result[day] = {
            worker_id
            for worker_id, first, last in rows
            if first <= day and (last is None or last >= day)
        }
        day += datetime.timedelta(days=1)
    return result


# --- Counts ----------------------------------------------------------------------------


def day_counts(expected_ids: set[int], records) -> dict[str, int]:
    """The ``counts`` object of the API: expected, one count per status, unmarked."""
    statuses = Counter(record.status for record in records)
    marked = {record.worker_id for record in records}
    Status = AttendanceRecord.Status
    return {
        "expected": len(expected_ids),
        "present": statuses[Status.PRESENT],
        "absent": statuses[Status.ABSENT],
        "half_day": statuses[Status.HALF_DAY],
        "late": statuses[Status.LATE],
        "unmarked": len(expected_ids - marked),
    }


def unmarked_worker_ids(day: AttendanceDay) -> list[int]:
    marked = set(day.records.values_list("worker_id", flat=True))
    return sorted(expected_worker_ids(day.site, day.date) - marked)


# --- Payout status ---------------------------------------------------------------------


def payout_status(day: AttendanceDay | None) -> str:
    """``not_open`` until the day is approved; then the cash sheet's state for that pay day."""
    if day is None or day.pk is None:
        return "not_open"
    if day.status not in (AttendanceDay.Status.APPROVED, AttendanceDay.Status.LOCKED):
        return "not_open"
    # Imported here: payroll's services import the attendance modules.
    from apps.payroll.services import sheet_status

    return sheet_status(day.site, day.date)


# --- Sync ------------------------------------------------------------------------------


@dataclass
class SyncChanges:
    server_time: datetime.datetime
    since: datetime.datetime | None
    days: list[AttendanceDay] = field(default_factory=list)
    records: list[AttendanceRecord] = field(default_factory=list)
    corrections: list[AttendanceCorrection] = field(default_factory=list)


def changes_since(user, since: datetime.datetime | None) -> SyncChanges:
    """Days, records and corrections changed at or after ``since`` on the user's sites.

    ``server_time`` is read first, so a change made while this runs comes back next time
    (an item may come twice; the phone applies them as upserts). Without ``since``: every
    day of the last ``SYNC_DEFAULT_DAYS`` days, with their records and corrections.
    """
    server_time = timezone.now()
    sites = scoped_sites(user)
    days = AttendanceDay.objects.filter(site__in=sites).select_related(
        "site", "submitted_by", "approved_by"
    )
    records = AttendanceRecord.objects.filter(day__site__in=sites)
    corrections = AttendanceCorrection.objects.filter(record__day__site__in=sites).select_related(
        "record", "corrected_by"
    )
    if since is None:
        first_day = company_today() - datetime.timedelta(days=SYNC_DEFAULT_DAYS - 1)
        days = days.filter(date__gte=first_day)
        records = records.filter(day__date__gte=first_day)
        corrections = corrections.filter(record__day__date__gte=first_day)
    else:
        days = days.filter(updated_at__gte=since)
        records = records.filter(updated_at__gte=since)
        corrections = corrections.filter(created_at__gte=since)
    return SyncChanges(
        server_time=server_time,
        since=since,
        days=list(days.order_by("-date", "site_id")),
        records=list(records.order_by("day_id", "worker_id")),
        corrections=list(corrections.order_by("created_at", "id")),
    )


# --- Approvals screen ------------------------------------------------------------------


def days_waiting_for_approval(user):
    """Submitted days on the sites ``user`` may see, oldest first."""
    return (
        AttendanceDay.objects.filter(
            site__in=scoped_sites(user), status=AttendanceDay.Status.SUBMITTED
        )
        .select_related("site", "submitted_by", "foreman")
        .prefetch_related("records")
        .order_by("date", "submitted_at", "pk")
    )


SYNC_LAG_FLAG = datetime.timedelta(hours=1)
FIELD_LABELS = dict(CORRECTABLE_FIELDS)


def _hhmm(value, tz) -> str:
    return value.astimezone(tz).strftime("%H:%M") if value else ""


def _clock(value) -> str:
    return value.strftime("%H:%M") if value else "—"


def approval_flags(day: AttendanceDay, corrected_count: int) -> list[str]:
    """Why a manager should look twice: corrections, and a roll call that synced late."""
    flags = []
    if corrected_count:
        flags.append(
            ngettext(
                "%(n)s changed after submission",
                "%(n)s changed after submission",
                corrected_count,
            )
            % {"n": corrected_count}
        )
    if day.client_submitted_at and day.submitted_at:
        lag = day.submitted_at - day.client_submitted_at
        if lag >= SYNC_LAG_FLAG:
            hours = int(lag.total_seconds() // 3600)
            flags.append(
                ngettext(
                    "Synced %(n)s h after submission", "Synced %(n)s h after submission", hours
                )
                % {"n": hours}
            )
    return flags


def _times(day: AttendanceDay, tz) -> tuple[str, str]:
    """(sent, synced): when the foreman submitted, and when the server got it."""
    synced = _hhmm(day.submitted_at, tz)
    sent = _hhmm(day.client_submitted_at, tz) or synced
    return sent, synced


def approval_card(day: AttendanceDay, tz) -> dict:
    """One entry of the Approvals list."""
    records = list(day.records.all())
    sent, _synced = _times(day, tz)
    flags = approval_flags(day, sum(1 for record in records if record.corrected))
    return {
        "day": day,
        "foreman": day.submitted_by or day.foreman,
        "sent": sent,
        "counts": day_counts(set(), records),
        "flag": flags[0] if flags else "",
    }


def _value_label(field_name: str, value: str) -> str:
    if field_name == "status" and value in AttendanceRecord.Status.values:
        return str(AttendanceRecord.Status(value).label)
    return value or "—"


def approval_detail(day: AttendanceDay, tz) -> dict:
    """Everything the Approvals detail panel shows for one day."""
    records = list(
        day.records.select_related("worker", "worker__trade")
        .prefetch_related("corrections")
        .order_by("worker__last_name", "worker__first_name", "worker_id")
    )
    rows = []
    for record in records:
        worker = record.worker
        changes = [
            {
                "field": FIELD_LABELS.get(correction.field, correction.field),
                "old": _value_label(correction.field, correction.old_value),
                "new": _value_label(correction.field, correction.new_value),
                "reason": correction.reason,
                "at": _hhmm(correction.created_at, tz),
            }
            for correction in sorted(record.corrections.all(), key=lambda c: c.created_at)
        ]
        trade = worker.trade.name if worker.trade else ""
        rows.append(
            {
                "record": record,
                "name": worker.full_name,
                "initials": f"{worker.first_name[:1]}{worker.last_name[:1]}".upper(),
                "meta": " · ".join(part for part in (worker.number, trade) if part),
                "times": f"{_clock(record.arrival)} · {_clock(record.departure)}",
                "changes": changes,
            }
        )
    sent, synced = _times(day, tz)
    return {
        "day": day,
        "foreman": day.submitted_by or day.foreman,
        "sent": sent,
        "synced": synced,
        "counts": day_counts(expected_worker_ids(day.site, day.date), records),
        "flags": approval_flags(day, sum(1 for record in records if record.corrected)),
        "rows": rows,
    }
