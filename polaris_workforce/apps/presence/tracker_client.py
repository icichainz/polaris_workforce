"""Reading a day's recomputed sessions from the tracker, for the nightly reconciliation.

``GET {TRACKER_URL}/internal/sites/{site_id}/sessions?date=YYYY-MM-DD&tz=<IANA>`` with a
service token (``tokens.mint_service_token``), answered with the tracker's ``SessionDay``
(``docs/api/tracker-openapi.yaml``)::

    {"site_id": 3, "date": "2026-10-31", "tz": "Africa/Lome",
     "from": "2026-10-31T00:00:00Z", "to": "2026-11-01T00:00:00Z",
     "sessions": [{"subject": {"kind": "worker", "id": 12},
                   "started_at": "2026-10-31T07:02:40Z",
                   "ended_at": "2026-10-31T16:48:31Z" | null, "zones": ["A", "B"]}]}

A bare list of sessions (the first planned shape) is read too; the day's bounds are then
computed here. The HTTP call and the parsing live only in this module.
"""

from __future__ import annotations

import datetime
import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass

from django.conf import settings

from . import tokens
from .models import SubjectKind

MAX_RESPONSE_BYTES = 10 * 1024 * 1024
# A local day is 24 h, 23 or 25 h on a daylight-saving change; more is a wrong answer.
_SHORTEST_DAY = datetime.timedelta(hours=22)
_LONGEST_DAY = datetime.timedelta(hours=26)


class TrackerReadError(Exception):
    """The tracker gave no usable answer; ``str()`` is short enough to store."""


class TrackerUnreachable(TrackerReadError):
    """No answer (down, timed out, wrong URL)."""


class TrackerRefused(TrackerReadError):
    """An HTTP error status (401, 404 ``site_not_configured``, 5xx…)."""

    def __init__(self, status: int, code: str = ""):
        self.status = status
        self.code = code
        super().__init__(f"HTTP {status}" + (f" {code}" if code else ""))


class TrackerBadResponse(TrackerReadError):
    """A 2xx answer that is not a valid ``SessionDay``."""


@dataclass(frozen=True)
class TrackedSession:
    subject_kind: str
    subject_id: int
    started_at: datetime.datetime
    ended_at: datetime.datetime | None
    zones: tuple[str, ...]


@dataclass(frozen=True)
class SessionDay:
    """The tracker's sessions of one local day, ``window_start <= started_at < window_end``."""

    window_start: datetime.datetime
    window_end: datetime.datetime
    sessions: tuple[TrackedSession, ...]


def local_day_bounds(day: datetime.date, tz: datetime.tzinfo) -> tuple:
    """``[start, end)`` of ``day`` in ``tz``, in UTC."""
    start = datetime.datetime.combine(day, datetime.time.min, tzinfo=tz)
    end = datetime.datetime.combine(day + datetime.timedelta(days=1), datetime.time.min, tzinfo=tz)
    return start.astimezone(datetime.UTC), end.astimezone(datetime.UTC)


def fetch_site_sessions(
    site_id: int, day: datetime.date, tz: datetime.tzinfo, tz_name: str
) -> SessionDay:
    """The tracker's replayed sessions of ``site_id`` for ``day`` in ``tz_name``."""
    query = urllib.parse.urlencode({"date": day.isoformat(), "tz": tz_name})
    request = urllib.request.Request(
        f"{settings.TRACKER_URL}/internal/sites/{int(site_id)}/sessions?{query}",
        method="GET",
        headers={
            "Authorization": f"Bearer {tokens.mint_service_token()}",
            "Accept": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(  # noqa: S310 (the URL is a setting, http/https)
            request, timeout=settings.TRACKER_TIMEOUT_SECONDS
        ) as response:
            status = response.status
            body = response.read(MAX_RESPONSE_BYTES + 1)
    except urllib.error.HTTPError as exc:
        raise TrackerRefused(exc.code, _error_code(exc)) from exc
    except (urllib.error.URLError, OSError) as exc:
        raise TrackerUnreachable(f"Unreachable: {exc}"[:300]) from exc
    if not 200 <= status < 300:
        raise TrackerRefused(status)
    if len(body) > MAX_RESPONSE_BYTES:
        raise TrackerBadResponse("The answer is too large.")
    try:
        payload = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise TrackerBadResponse("The answer is not JSON.") from exc
    return parse_session_day(payload, site_id=site_id, day=day, tz=tz)


def _error_code(exc: urllib.error.HTTPError) -> str:
    """The ``code`` of the tracker's error body (``{"code", "message", "details"}``)."""
    try:
        body = json.loads(exc.read(64 * 1024) or b"{}")
    except Exception:  # no body, not JSON: the status says enough
        return ""
    code = body.get("code") if isinstance(body, dict) else None
    return code[:64] if isinstance(code, str) else ""


def parse_session_day(payload, *, site_id: int, day: datetime.date, tz) -> SessionDay:
    """A ``SessionDay`` from the decoded JSON, else ``TrackerBadResponse``."""
    if isinstance(payload, list):
        window_start, window_end = local_day_bounds(day, tz)
        items = payload
    elif isinstance(payload, dict):
        if "site_id" in payload and payload["site_id"] != site_id:
            raise TrackerBadResponse("The answer is about another site.")
        if "date" in payload and payload["date"] != day.isoformat():
            raise TrackerBadResponse("The answer is about another day.")
        if "from" in payload or "to" in payload:
            window_start = _timestamp(payload.get("from"), "from")
            window_end = _timestamp(payload.get("to"), "to")
        else:
            window_start, window_end = local_day_bounds(day, tz)
        items = payload.get("sessions")
    else:
        raise TrackerBadResponse("The answer is not a list of sessions.")
    if not _SHORTEST_DAY <= window_end - window_start <= _LONGEST_DAY:
        raise TrackerBadResponse("The day's bounds are wrong.")
    if not isinstance(items, list):
        raise TrackerBadResponse("`sessions` is not a list.")
    sessions = tuple(_session(item, window_start, window_end) for item in items)
    return SessionDay(window_start=window_start, window_end=window_end, sessions=sessions)


def _session(item, window_start, window_end) -> TrackedSession:
    if not isinstance(item, dict):
        raise TrackerBadResponse("A session is not an object.")
    subject = item.get("subject")
    if not isinstance(subject, dict):
        raise TrackerBadResponse("A session has no subject.")
    kind, subject_id = subject.get("kind"), subject.get("id")
    if kind not in SubjectKind.values:
        raise TrackerBadResponse("A session's subject kind is unknown.")
    if not isinstance(subject_id, int) or isinstance(subject_id, bool) or subject_id <= 0:
        raise TrackerBadResponse("A session's subject id is not a positive integer.")
    started_at = _timestamp(item.get("started_at"), "started_at")
    ended_raw = item.get("ended_at")
    ended_at = None if ended_raw is None else _timestamp(ended_raw, "ended_at")
    if not window_start <= started_at < window_end:
        raise TrackerBadResponse("A session starts outside the day.")
    if ended_at is not None and ended_at < started_at:
        raise TrackerBadResponse("A session ends before it starts.")
    zones = item.get("zones", [])
    if not isinstance(zones, list) or not all(isinstance(z, str) and z for z in zones):
        raise TrackerBadResponse("A session's zones are not a list of zone ids.")
    unique: list[str] = []
    for zone in (z[:64] for z in zones):
        if zone not in unique:
            unique.append(zone)
    return TrackedSession(
        subject_kind=kind,
        subject_id=subject_id,
        started_at=started_at,
        ended_at=ended_at,
        zones=tuple(unique),
    )


def _timestamp(value, name: str) -> datetime.datetime:
    if not isinstance(value, str):
        raise TrackerBadResponse(f"`{name}` is not a timestamp.")
    try:
        parsed = datetime.datetime.fromisoformat(value)
    except ValueError as exc:
        raise TrackerBadResponse(f"`{name}` is not a timestamp.") from exc
    if parsed.tzinfo is None:
        raise TrackerBadResponse(f"`{name}` has no time zone.")
    return parsed.astimezone(datetime.UTC)
