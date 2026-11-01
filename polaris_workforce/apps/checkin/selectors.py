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
* No check-in: no suggestion from check-ins.

Presence (``suggest_from_presence``), per crew worker seen on the site that day by the
tracker (``apps.presence``) **and** with ``tracking_enabled`` resolved on for the site
and the worker: ``seen_from`` = first sighting, ``seen_until`` = last sighting (``None``
while still on site), in the company's time zone, minutes; then

* time on site inside working hours (``work_start``–``work_end``, the site's, else the
  company's) less than ``half_day_units`` of those hours → ``half_day``; an open session
  counts as if the worker stays until ``work_end``;
* else first sighting after ``late_after`` → ``late``;
* else ``present``.

A worker with both: the check-ins win (``status``, ``arrival``, ``departure`` as above),
the presence is shown next to them (``presence``), ``sources`` lists both. Presence
alone: ``status``/``arrival``/``departure`` from the presence, no check-in fields.
Presence never marks attendance: a suggestion is only written to the roll call by the
foreman (or by check-ins in ``self_check_in`` mode, ``worker_suggestion``, which never
looks at presence).
"""

from __future__ import annotations

import datetime
from collections import defaultdict
from dataclasses import dataclass, field, replace
from decimal import Decimal

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


SOURCE_CHECKIN = "checkin"
SOURCE_PRESENCE = "presence"
SOURCES = [SOURCE_CHECKIN, SOURCE_PRESENCE]


@dataclass(frozen=True)
class PresenceSeen:
    """What the tracker saw of one worker on the site that day (every visit folded)."""

    first_seen_at: datetime.datetime
    last_seen_at: datetime.datetime | None  # None while still on site
    open: bool
    seen_from: datetime.time
    seen_until: datetime.time | None
    zones: list[str]
    visits: int
    status: str  # AttendanceRecord.Status value: what the presence alone suggests


@dataclass(frozen=True)
class Suggestion:
    worker_id: int
    status: str | None  # AttendanceRecord.Status value
    arrival: datetime.time | None
    departure: datetime.time | None
    first_in_at: datetime.datetime | None
    last_out_at: datetime.datetime | None
    checkin_count: int
    sources: tuple[str, ...] = (SOURCE_CHECKIN,)
    presence: PresenceSeen | None = None


@dataclass(frozen=True)
class DayRules:
    """The settings the suggestion rules read, resolved for one site (company → site)."""

    late_after: datetime.time
    work_start: datetime.time
    work_end: datetime.time
    half_day_units: Decimal
    tz: object = field(compare=False)


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


def _minutes(value: datetime.time) -> int:
    return value.hour * 60 + value.minute


def presence_status(seen_from: datetime.time, seen_until: datetime.time | None, rules) -> str:
    """The status a presence alone suggests (rules in the module docstring)."""
    start, end = _minutes(rules.work_start), _minutes(rules.work_end)
    hours = end - start
    if hours > 0:
        last = _minutes(seen_until) if seen_until is not None else end
        on_site = max(0, min(last, end) - max(_minutes(seen_from), start))
        if on_site < rules.half_day_units * hours:
            return Status.HALF_DAY
    if seen_from > rules.late_after:
        return Status.LATE
    return Status.PRESENT


def suggest_from_presence(row, rules: DayRules) -> PresenceSeen:
    """``PresenceSeen`` of one ``apps.presence.selectors.DaySession``."""
    seen_from = _minute(row.first_seen_at, rules.tz)
    seen_until = None if row.last_seen_at is None else _minute(row.last_seen_at, rules.tz)
    return PresenceSeen(
        first_seen_at=row.first_seen_at,
        last_seen_at=row.last_seen_at,
        open=row.open,
        seen_from=seen_from,
        seen_until=seen_until,
        zones=list(row.zones),
        visits=row.visits,
        status=presence_status(seen_from, seen_until, rules),
    )


def combine(worker_id: int, checkins: Suggestion | None, presence: PresenceSeen | None):
    """One worker's suggestion from check-ins, presence or both (check-ins win)."""
    if checkins is not None:
        if presence is None:
            return checkins
        return replace(checkins, sources=(SOURCE_CHECKIN, SOURCE_PRESENCE), presence=presence)
    if presence is None:
        return None
    return Suggestion(
        worker_id=worker_id,
        status=presence.status,
        arrival=presence.seen_from,
        departure=presence.seen_until,
        first_in_at=None,
        last_out_at=None,
        checkin_count=0,
        sources=(SOURCE_PRESENCE,),
        presence=presence,
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


def day_rules(site, settings: CompanySettings | None = None) -> DayRules:
    settings = settings or CompanySettings.load(site.company_id)

    def get(name):
        return resolve_setting(name, site=site, company=settings)

    return DayRules(
        late_after=get("late_after"),
        work_start=get("work_start"),
        work_end=get("work_end"),
        half_day_units=Decimal(get("half_day_units")),
        tz=company_tz(settings),
    )


def presence_by_worker(
    site, date: datetime.date, worker_ids, settings: CompanySettings | None = None
) -> dict[int, PresenceSeen]:
    """``{worker id: PresenceSeen}`` for the tracked workers among ``worker_ids`` seen on
    ``site`` on ``date`` (for the office screens and the suggestions)."""
    from apps.presence.selectors import workers_day_presence

    settings = settings or CompanySettings.load(site.company_id)
    rows = workers_day_presence(site, date, worker_ids, settings)
    if not rows:
        return {}
    rules = day_rules(site, settings)
    return {worker_id: suggest_from_presence(row, rules) for worker_id, row in rows.items()}


def day_suggestions(site, date: datetime.date) -> DaySuggestions:
    """One suggestion per crew worker with check-ins or a presence, by worker id."""
    settings, tz, late_after = site_rules(site)
    crew = crew_ids(site, date)
    by_worker: dict[int, list[CheckIn]] = defaultdict(list)
    for checkin in CheckIn.objects.filter(site=site, date=date, worker_id__in=crew):
        by_worker[checkin.worker_id].append(checkin)
    from_checkins = {
        worker_id: found
        for worker_id, checkins in by_worker.items()
        if (found := suggest(checkins, late_after, tz)) is not None
    }
    presence = presence_by_worker(site, date, crew, settings)
    suggestions = [
        combine(worker_id, from_checkins.get(worker_id), presence.get(worker_id))
        for worker_id in sorted(set(from_checkins) | set(presence))
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
    """The suggestion for one worker from check-ins only (optionally ignoring one
    check-in): what ``self_check_in`` mode writes on the draft. Never presence."""
    _settings, tz, late_after = site_rules(site)
    checkins = CheckIn.objects.filter(site=site, date=date, worker_id=worker_id)
    if exclude_pk is not None:
        checkins = checkins.exclude(pk=exclude_pk)
    return suggest(list(checkins), late_after, tz)
