"""Read-only check-in queries: a site's day, flags, roll-call suggestions, kiosks.

Every function takes objects the caller already fetched through the scoped querysets
(``scoped_sites``, ``company_workers``); nothing here widens a scope.

Roll-call suggestion rules (``suggest``), per worker on the crew, from the check-ins
that count (known worker, no badge problem), in the company's time zone:

* ``arrival`` = the earliest ``in``, ``departure`` = the latest ``out`` (``HH:MM``).
* ``in`` and ``out`` with the ``out`` before midday (12:00) → ``half_day``;
  an ``in`` at or after midday → ``half_day``;
  an ``in`` after ``late_after`` (the site's, else the company's) → ``late``;
  any other ``in`` → ``present``.
* Only an ``out``: departure only, ``status`` ``None`` (the foreman decides).
* No check-in: no suggestion.
"""

from __future__ import annotations

import datetime
from collections import defaultdict
from dataclasses import dataclass

from apps.accounts.models import ForemanDevice
from apps.attendance.models import AttendanceDay, AttendanceRecord
from apps.core.dates import company_tz
from apps.core.models import CompanySettings
from apps.core.settings import resolve_setting
from apps.worksites.services import crew_ids

from .models import CheckIn, KioskDevice

MIDDAY = datetime.time(12, 0)
DUPLICATE_WINDOW = datetime.timedelta(minutes=5)

# Flags, in the order they are listed (API ``flags`` and office badges).
UNKNOWN_BADGE = "unknown_badge"
REVOKED_BADGE = "revoked_badge"
NOT_ON_CREW = "not_on_crew"
OUTSIDE_GEOFENCE = "outside_geofence"
DUPLICATE = "duplicate"
FLAGS = [UNKNOWN_BADGE, REVOKED_BADGE, NOT_ON_CREW, OUTSIDE_GEOFENCE, DUPLICATE]

Status = AttendanceRecord.Status


# --- kiosks -----------------------------------------------------------------------------


def kiosk_of(device) -> KioskDevice | None:
    """The kiosk setting of a device token (``request.auth``), or ``None``: no device
    (a browser session), an ordinary phone."""
    if not isinstance(device, ForemanDevice) or device.pk is None:
        return None
    return KioskDevice.objects.select_related("site").filter(device=device).first()


# --- a site's day -----------------------------------------------------------------------


def day_checkins(site, date: datetime.date) -> list[CheckIn]:
    return list(
        CheckIn.objects.filter(site=site, date=date)
        .select_related("worker__trade", "badge", "device", "recorded_by")
        .order_by("at", "pk")
    )


def duplicate_ids(checkins) -> set[int]:
    """Check-ins of a known worker that follow another one of the same worker, site and
    direction by less than five minutes (a double scan)."""
    groups: dict[tuple, list[CheckIn]] = defaultdict(list)
    for checkin in checkins:
        if checkin.worker_id is not None:
            groups[(checkin.worker_id, checkin.site_id, checkin.direction)].append(checkin)
    found = set()
    for group in groups.values():
        group.sort(key=lambda c: (c.at, c.pk or 0))
        for previous, current in zip(group, group[1:], strict=False):
            if current.at - previous.at < DUPLICATE_WINDOW:
                found.add(current.pk)
    return found


def flags_of(checkin: CheckIn, duplicates: set[int] = frozenset()) -> list[str]:
    flags = []
    if checkin.badge_problem == CheckIn.BadgeProblem.UNKNOWN:
        flags.append(UNKNOWN_BADGE)
    if checkin.badge_problem == CheckIn.BadgeProblem.REVOKED:
        flags.append(REVOKED_BADGE)
    if checkin.worker_id is not None and not checkin.on_crew:
        flags.append(NOT_ON_CREW)
    if checkin.inside_geofence is False:
        flags.append(OUTSIDE_GEOFENCE)
    if checkin.pk in duplicates:
        flags.append(DUPLICATE)
    return flags


def flags_for(checkin: CheckIn) -> list[str]:
    """Flags of one check-in, looking at the other check-ins of its worker that day."""
    if checkin.worker_id is None:
        return flags_of(checkin)
    same = CheckIn.objects.filter(
        site_id=checkin.site_id,
        date=checkin.date,
        worker_id=checkin.worker_id,
        direction=checkin.direction,
    )
    return flags_of(checkin, duplicate_ids(list(same)))


# --- suggestions --------------------------------------------------------------------------


@dataclass(frozen=True)
class Suggestion:
    worker_id: int
    status: str | None  # AttendanceRecord.Status value
    arrival: datetime.time | None
    departure: datetime.time | None
    first_in_at: datetime.datetime | None
    last_out_at: datetime.datetime | None
    checkin_count: int


def _minute(moment: datetime.datetime, tz) -> datetime.time:
    return moment.astimezone(tz).time().replace(second=0, microsecond=0)


def suggest(checkins, late_after: datetime.time, tz) -> Suggestion | None:
    """The roll-call suggestion for one worker's check-ins of one day (rules above)."""
    counted = [c for c in checkins if c.counted]
    if not counted:
        return None
    ins = [c.at for c in counted if c.direction == CheckIn.Direction.IN]
    outs = [c.at for c in counted if c.direction == CheckIn.Direction.OUT]
    first_in = min(ins) if ins else None
    last_out = max(outs) if outs else None
    arrival = _minute(first_in, tz) if first_in else None
    departure = _minute(last_out, tz) if last_out else None
    status = None
    if arrival is not None:
        if (departure is not None and departure < MIDDAY) or arrival >= MIDDAY:
            status = Status.HALF_DAY
        elif arrival > late_after:
            status = Status.LATE
        else:
            status = Status.PRESENT
    return Suggestion(
        worker_id=counted[0].worker_id,
        status=status,
        arrival=arrival,
        departure=departure,
        first_in_at=first_in,
        last_out_at=last_out,
        checkin_count=len(counted),
    )


@dataclass
class DaySuggestions:
    site: object
    date: datetime.date
    attendance_mode: str
    late_after: datetime.time
    day_status: str
    suggestions: list[Suggestion]


def site_rules(site):
    """``(CompanySettings, tz, late_after)`` for a site."""
    settings = CompanySettings.load(site.company_id)
    late_after = resolve_setting("late_after", site=site, company=settings)
    return settings, company_tz(settings), late_after


def day_suggestions(site, date: datetime.date) -> DaySuggestions:
    """One suggestion per crew worker with check-ins, by worker id."""
    settings, tz, late_after = site_rules(site)
    crew = crew_ids(site, date)
    by_worker: dict[int, list[CheckIn]] = defaultdict(list)
    for checkin in CheckIn.objects.filter(site=site, date=date, worker_id__in=crew):
        by_worker[checkin.worker_id].append(checkin)
    suggestions = [
        suggestion
        for worker_id in sorted(by_worker)
        if (suggestion := suggest(by_worker[worker_id], late_after, tz)) is not None
    ]
    day = AttendanceDay.objects.filter(site=site, date=date).only("status").first()
    return DaySuggestions(
        site=site,
        date=date,
        attendance_mode=settings.attendance_mode,
        late_after=late_after,
        day_status=day.status if day else "not_started",
        suggestions=suggestions,
    )


def worker_suggestion(site, date, worker_id, *, exclude_pk=None) -> Suggestion | None:
    """The suggestion for one worker (optionally ignoring one check-in)."""
    _settings, tz, late_after = site_rules(site)
    checkins = CheckIn.objects.filter(site=site, date=date, worker_id=worker_id)
    if exclude_pk is not None:
        checkins = checkins.exclude(pk=exclude_pk)
    return suggest(list(checkins), late_after, tz)
