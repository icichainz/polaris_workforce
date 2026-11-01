"""Nightly reconciliation of presence sessions with the tracker's replay.

What must hold:
- For each tracked site and past local day, Django's sessions end up as the tracker's
  replay: missing visits added, visits extended, shortened or closed, visits the replay
  does not support removed (only presence sessions: attendance never changes).
- Running again changes nothing; a reconciled day is skipped the next night; a late live
  event for a reconciled day cannot undo the correction.
- Sites with tracking off are skipped; a company only ever touches its own sessions and
  people; days are local days in the company time zone (daylight-saving changes too).
- An unreachable tracker, an error answer or a malformed one is recorded and tried again
  the next night; one site's failure does not stop the others; an empty replay for a day
  with visits changes nothing.
- One ``presence.reconcile`` audit entry per company and run.
"""

import datetime
import io
import json
import urllib.error
import urllib.parse
import uuid
import zoneinfo
from collections.abc import Callable
from pathlib import Path
from unittest import mock

import pytest
import yaml
from celery.exceptions import SoftTimeLimitExceeded
from django.conf import settings as django_settings
from django.core.management import CommandError, call_command
from jsonschema import Draft202012Validator

from apps.attendance.models import AttendanceRecord
from apps.core.models import AuditLog, CompanySettings
from apps.core.tenancy import use_company
from apps.presence import reconcile, services, tasks, tracker_client
from apps.presence.models import PresenceReconciliation, PresenceSession
from tests.factories import (
    AttendanceDayFactory,
    AttendanceRecordFactory,
    CompanyFactory,
    ForemanFactory,
    SiteFactory,
    WorkerFactory,
)

pytestmark = pytest.mark.django_db

UTC = datetime.UTC
TRACKER = "http://tracker.test:4100"
TRACKER_SPEC = Path(django_settings.BASE_DIR).parent / "docs" / "api" / "tracker-openapi.yaml"
DAY = datetime.date(2026, 10, 31)
NOW = datetime.datetime(2026, 11, 1, 3, 15, tzinfo=UTC)  # the nightly run


def at(hour, minute=0, day=DAY) -> datetime.datetime:
    return datetime.datetime.combine(day, datetime.time(hour, minute), tzinfo=UTC)


def iso(value: datetime.datetime | None) -> str | None:
    return None if value is None else value.isoformat().replace("+00:00", "Z")


class FakeTracker:
    """Stands in for ``urllib.request.urlopen``: answers ``GET /internal/sites/{id}/sessions``
    with the sessions set in ``days[(site_id, date)]`` (``SessionDay``, bounds computed from
    ``tz`` like the tracker), or with ``errors[site_id]`` / ``bodies[site_id]``."""

    def __init__(self):
        self.requests = []
        self.days: dict[tuple[int, datetime.date], list[dict]] = {}
        self.errors: dict[int, Callable[[], Exception]] = {}
        self.bodies: dict[int, bytes] = {}
        self.error: Exception | None = None

    def __call__(self, request, timeout=None):
        self.requests.append(request)
        url = urllib.parse.urlsplit(request.full_url)
        site_id = int(url.path.split("/")[-2])
        query = dict(urllib.parse.parse_qsl(url.query))
        if self.error is not None:
            raise self.error
        if site_id in self.errors:
            raise self.errors[site_id]()  # a fresh error each time: its body is read once
        body = self.bodies.get(site_id)
        if body is None:
            body = json.dumps(self.answer(site_id, query["date"], query["tz"])).encode()
        response = mock.MagicMock()
        response.status = 200
        response.read.return_value = body
        response.__enter__.return_value = response
        return response

    def answer(self, site_id: int, date: str, tz: str) -> dict:
        day = datetime.date.fromisoformat(date)
        start, end = tracker_client.local_day_bounds(day, zoneinfo.ZoneInfo(tz))
        return {
            "site_id": site_id,
            "date": date,
            "tz": tz,
            "from": iso(start),
            "to": iso(end),
            "sessions": self.days.get((site_id, day), []),
        }

    def queries(self) -> list[tuple[int, str, str]]:
        result = []
        for request in self.requests:
            url = urllib.parse.urlsplit(request.full_url)
            query = dict(urllib.parse.parse_qsl(url.query))
            result.append((int(url.path.split("/")[-2]), query["date"], query["tz"]))
        return result


@pytest.fixture
def fake_tracker(settings, monkeypatch):
    settings.TRACKER_URL = TRACKER
    fake = FakeTracker()
    monkeypatch.setattr("urllib.request.urlopen", fake)
    return fake


@pytest.fixture
def frozen(monkeypatch):
    """The nightly run's clock, for the task and the command (which read the time)."""
    monkeypatch.setattr(reconcile.timezone, "now", lambda: NOW)


@pytest.fixture
def lome(default_company):
    company = CompanySettings.load(default_company)
    company.tracking_enabled = True
    company.time_zone = "Africa/Lome"
    company.save()
    site = SiteFactory(name="North")
    return {
        "company": default_company,
        "site": site,
        "yao": WorkerFactory(first_name="Yao"),
        "ama": WorkerFactory(first_name="Ama"),
        "kodjo": ForemanFactory(first_name="Kodjo"),
    }


def tracked(person, start, end, zones=()) -> dict:
    kind = "worker" if person.__class__.__name__ == "Worker" else "foreman"
    return {
        "subject": {"kind": kind, "id": person.pk},
        "started_at": iso(start),
        "ended_at": iso(end),
        "zones": list(zones),
    }


def live(site, person, *events):
    """Live events from the tracker's outbox: ``(type, at, zone[, reason])``."""
    kind = "worker" if person.__class__.__name__ == "Worker" else "foreman"
    services.ingest_events(
        services.EventIn(
            event_id=uuid.uuid4(),
            site_id=site.pk,
            subject_kind=kind,
            subject_id=person.pk,
            type=event[0],
            at=event[1],
            zone=event[2] if len(event) > 2 else "",
            reason=event[3] if len(event) > 3 else "",
        )
        for event in events
    )


def visit(site, person, start, end=None, zone="A"):
    events = [("entered", start, zone)]
    if end is not None:
        events.append(("left", end, zone, "timeout"))
    live(site, person, *events)


def sessions(site, person=None, day=DAY) -> list[tuple]:
    rows = PresenceSession.objects.filter(site=site, local_date=day)
    if person is not None:
        rows = rows.filter(subject_id=person.pk)
    return [
        (s.subject_id, s.started_at, s.ended_at, s.zones)
        for s in rows.order_by("started_at", "subject_id")
    ]


def record(site, day=DAY) -> PresenceReconciliation:
    return PresenceReconciliation.objects.get(site=site, local_date=day)


# --- corrections ---------------------------------------------------------------------------


def test_a_missing_visit_is_added(lome, fake_tracker):
    site, yao = lome["site"], lome["yao"]
    fake_tracker.days[(site.pk, DAY)] = [tracked(yao, at(7, 2), at(16, 48), ["A", "B"])]

    summary = reconcile.run(day=DAY, now=NOW)

    assert summary.reconciled == 1 and summary.failed == 0
    assert sessions(site) == [(yao.pk, at(7, 2), at(16, 48), ["A", "B"])]
    session = PresenceSession.objects.get()
    assert session.source == PresenceSession.Source.REPLAY
    assert session.company_id == site.company_id
    assert session.local_date == DAY
    assert session.subject_kind == "worker"
    done = record(site)
    assert (done.added, done.changed, done.removed, done.unchanged) == (1, 0, 0, 0)
    assert done.reconciled_at == NOW and done.last_error == "" and done.attempts == 1
    assert (done.window_start, done.window_end) == (at(0), at(0, day=DAY + datetime.timedelta(1)))


def test_the_tracker_is_asked_for_the_local_day_with_a_service_token(lome, fake_tracker):
    reconcile.run(day=DAY, now=NOW)

    request = fake_tracker.requests[-1]
    assert request.full_url == (
        f"{TRACKER}/internal/sites/{lome['site'].pk}/sessions?date=2026-10-31&tz=Africa%2FLome"
    )
    assert request.get_method() == "GET"
    token = request.get_header("Authorization").removeprefix("Bearer ")
    claims = tracker_client.tokens.decode(token, audience="polaris-tracker")
    assert claims["sub"] == "service:django" and claims["kind"] == "service"


def test_a_visit_is_extended(lome, fake_tracker):
    site, yao = lome["site"], lome["yao"]
    visit(site, yao, at(7), at(12))  # a gateway buffered the afternoon
    fake_tracker.days[(site.pk, DAY)] = [tracked(yao, at(7), at(16, 30), ["A"])]

    reconcile.run(day=DAY, now=NOW)

    assert sessions(site) == [(yao.pk, at(7), at(16, 30), ["A"])]
    session = PresenceSession.objects.get()
    assert session.end_reason == "timeout"  # kept: the replay does not say why
    assert (record(site).changed, record(site).added, record(site).removed) == (1, 0, 0)


def test_a_visit_is_shortened_and_moved(lome, fake_tracker):
    site, yao = lome["site"], lome["yao"]
    visit(site, yao, at(6, 40), at(18))
    fake_tracker.days[(site.pk, DAY)] = [tracked(yao, at(7), at(17), ["A", "B"])]

    reconcile.run(day=DAY, now=NOW)

    assert sessions(site) == [(yao.pk, at(7), at(17), ["A", "B"])]
    assert record(site).changed == 1


def test_a_visit_the_replay_does_not_support_is_removed(lome, fake_tracker):
    site, yao, ama = lome["site"], lome["yao"], lome["ama"]
    visit(site, yao, at(7), at(16))
    visit(site, ama, at(9), at(9, 5))  # a badge seen once through the fence
    fake_tracker.days[(site.pk, DAY)] = [tracked(yao, at(7), at(16), ["A"])]

    reconcile.run(day=DAY, now=NOW)

    assert sessions(site) == [(yao.pk, at(7), at(16), ["A"])]
    done = record(site)
    assert (done.unchanged, done.removed, done.added, done.changed) == (1, 1, 0, 0)


def test_an_open_session_is_closed(lome, fake_tracker):
    site, kodjo = lome["site"], lome["kodjo"]
    visit(site, kodjo, at(7))  # its `left` never arrived
    assert sessions(site)[0][2] is None
    fake_tracker.days[(site.pk, DAY)] = [tracked(kodjo, at(7), at(15, 10))]

    reconcile.run(day=DAY, now=NOW)

    assert sessions(site) == [(kodjo.pk, at(7), at(15, 10), [])]
    session = PresenceSession.objects.get()
    assert session.subject_kind == "foreman"
    assert session.end_reason == ""
    assert record(site).changed == 1


def test_two_visits_a_gateway_gap_split_become_one(lome, fake_tracker):
    site, yao = lome["site"], lome["yao"]
    visit(site, yao, at(7), at(11))
    visit(site, yao, at(11, 20), at(16))
    fake_tracker.days[(site.pk, DAY)] = [tracked(yao, at(7), at(16), ["A"])]

    reconcile.run(day=DAY, now=NOW)

    assert sessions(site) == [(yao.pk, at(7), at(16), ["A"])]
    done = record(site)
    assert (done.changed, done.removed, done.added) == (1, 1, 0)


def test_one_visit_the_replay_sees_as_two(lome, fake_tracker):
    site, yao = lome["site"], lome["yao"]
    visit(site, yao, at(7), at(17))
    fake_tracker.days[(site.pk, DAY)] = [
        tracked(yao, at(7), at(12), ["A"]),
        tracked(yao, at(13), at(17), ["B"]),
    ]

    reconcile.run(day=DAY, now=NOW)

    assert sessions(site) == [(yao.pk, at(7), at(12), ["A"]), (yao.pk, at(13), at(17), ["B"])]
    done = record(site)
    assert (done.changed, done.added, done.removed) == (1, 1, 0)


def test_the_plan_pairs_overlapping_visits_in_order():
    tz = zoneinfo.ZoneInfo("Africa/Lome")

    def ours(pk, start, end):
        return PresenceSession(
            pk=pk, subject_kind="worker", subject_id=1, started_at=start, ended_at=end, zones=[]
        )

    def theirs(start, end):
        return tracker_client.TrackedSession("worker", 1, start, end, ())

    plan = reconcile.plan_day(
        [theirs(at(7), at(9)), theirs(at(10), at(12)), theirs(at(14), at(15))],
        [ours(1, at(7), None), ours(2, at(16), at(17))],  # open from 07:00
        tz,
    )

    # The open session runs on, so all overlap: one group, paired in time order.
    assert [(s.pk, values["started_at"], values["ended_at"]) for s, values in plan.update] == [
        (1, at(7), at(9)),
        (2, at(10), at(12)),
    ]
    assert [values["started_at"] for values in plan.create] == [at(14)]
    assert plan.delete == []
    assert (plan.changed, plan.added, plan.removed) == (2, 1, 0)


def test_the_plan_keeps_separate_visits_apart():
    tz = zoneinfo.ZoneInfo("Africa/Lome")
    ours = [
        PresenceSession(
            pk=1, subject_kind="worker", subject_id=1, started_at=at(16), ended_at=at(17)
        )
    ]
    theirs = [tracker_client.TrackedSession("worker", 1, at(7), at(9), ())]

    plan = reconcile.plan_day(theirs, ours, tz)

    assert (plan.added, plan.removed, plan.changed) == (1, 1, 0)
    assert [s.pk for s in plan.delete] == [1]


# --- idempotency and live events -------------------------------------------------------------


def test_running_twice_changes_nothing(lome, fake_tracker):
    site, yao, ama = lome["site"], lome["yao"], lome["ama"]
    visit(site, yao, at(7), at(12))
    visit(site, ama, at(8), at(16))
    fake_tracker.days[(site.pk, DAY)] = [
        tracked(yao, at(7), at(16), ["A"]),
        tracked(ama, at(8), at(16), ["A"]),
    ]
    reconcile.run(now=NOW)
    before = list(PresenceSession.objects.values().order_by("pk"))

    again = reconcile.run(day=DAY, now=NOW + datetime.timedelta(hours=1))  # forced

    assert again.reconciled == 1
    assert list(PresenceSession.objects.values().order_by("pk")) == before
    done = record(site)
    assert (done.unchanged, done.changed, done.added, done.removed) == (2, 0, 0, 0)
    assert done.attempts == 2

    asked = len(fake_tracker.requests)
    assert reconcile.run(now=NOW + datetime.timedelta(hours=2)).outcomes == []
    assert len(fake_tracker.requests) == asked  # the night's day is done: not asked again


def test_a_late_live_event_does_not_undo_the_correction(lome, fake_tracker):
    site, yao = lome["site"], lome["yao"]
    visit(site, yao, at(7), at(12))
    fake_tracker.days[(site.pk, DAY)] = [tracked(yao, at(7), at(16), ["A"])]
    reconcile.run(day=DAY, now=NOW)

    # The outbox delivers a late event of that day, then the next morning's visit.
    live(site, yao, ("entered", at(13), "B"))
    tomorrow = DAY + datetime.timedelta(days=1)
    visit(site, yao, at(7, day=tomorrow), at(15, day=tomorrow))

    assert sessions(site, yao) == [(yao.pk, at(7), at(16), ["A"])]
    assert sessions(site, yao, day=tomorrow) == [
        (yao.pk, at(7, day=tomorrow), at(15, day=tomorrow), ["A"])
    ]
    assert PresenceSession.objects.get(local_date=tomorrow).source == "live"


def test_the_days_before_a_reconciled_day_still_rebuild_from_events(lome, fake_tracker):
    site, yao = lome["site"], lome["yao"]
    before = DAY - datetime.timedelta(days=1)
    reconcile.run(day=DAY, now=NOW)  # an empty day, reconciled

    visit(site, yao, at(7, day=before), at(16, day=before))

    assert sessions(site, yao, day=before) == [
        (yao.pk, at(7, day=before), at(16, day=before), ["A"])
    ]


def test_attendance_is_never_changed(lome, fake_tracker):
    site, yao = lome["site"], lome["yao"]
    visit(site, yao, at(7), at(12))
    attendance = AttendanceRecordFactory(day=AttendanceDayFactory(site=site, date=DAY), worker=yao)
    before = AttendanceRecord.objects.filter(pk=attendance.pk).values().get()
    fake_tracker.days[(site.pk, DAY)] = [tracked(yao, at(9), at(10), ["A"])]

    reconcile.run(day=DAY, now=NOW)

    assert AttendanceRecord.objects.filter(pk=attendance.pk).values().get() == before
    assert sessions(site) == [(yao.pk, at(9), at(10), ["A"])]


# --- which sites and days ---------------------------------------------------------------------


def test_sites_with_tracking_off_are_skipped(lome, fake_tracker):
    off = SiteFactory(name="Off", tracking_enabled=False)
    visit(off, lome["yao"], at(7), at(12))

    summary = reconcile.run(now=NOW)

    assert {site_id for site_id, _date, _tz in fake_tracker.queries()} == {lome["site"].pk}
    assert (off.pk, "tracking is off") in summary.skipped
    assert not PresenceReconciliation.objects.filter(site=off).exists()
    assert sessions(off)[0][2] == at(12)


def test_tracking_off_for_the_company_skips_every_site(lome, fake_tracker):
    company = CompanySettings.load(lome["company"])
    company.tracking_enabled = False
    company.save()

    summary = reconcile.run(now=NOW)

    assert fake_tracker.requests == []
    assert summary.skipped == [(lome["site"].pk, "tracking is off")]


def test_each_company_only_touches_its_own_sessions(lome, fake_tracker):
    site, yao = lome["site"], lome["yao"]
    with use_company(CompanyFactory(name="Bravo")) as bravo:
        bravo_settings = CompanySettings.load(bravo)
        bravo_settings.tracking_enabled = True
        bravo_settings.time_zone = "Europe/Paris"
        bravo_settings.save()
        bravo_site = SiteFactory(name="Bravo site")
        bravo_worker = WorkerFactory(first_name="Jean")
    visit(site, yao, at(7), at(12))
    visit(bravo_site, bravo_worker, at(8), at(17))
    fake_tracker.days[(site.pk, DAY)] = [
        tracked(yao, at(7), at(16), ["A"]),
        tracked(bravo_worker, at(8), at(17), ["A"]),  # not a person of this company
    ]
    fake_tracker.days[(bravo_site.pk, DAY)] = [
        tracked(bravo_worker, at(8), at(17), ["A"]),
        tracked(yao, at(9), at(10)),  # not a person of Bravo
    ]

    reconcile.run(day=DAY, now=NOW)

    assert sessions(site) == [(yao.pk, at(7), at(16), ["A"])]
    assert sessions(bravo_site) == [(bravo_worker.pk, at(8), at(17), ["A"])]
    assert PresenceSession.objects.get(site=bravo_site).company_id == bravo.pk
    assert record(site).ignored == 1 and record(bravo_site).ignored == 1
    assert record(bravo_site).company_id == bravo.pk
    assert sorted(fake_tracker.queries()) == sorted(
        [(site.pk, "2026-10-31", "Africa/Lome"), (bravo_site.pk, "2026-10-31", "Europe/Paris")]
    )


def test_one_site_only(lome, fake_tracker):
    other = SiteFactory(name="South")

    reconcile.run(site_ids=[other.pk], now=NOW)

    assert {site_id for site_id, _date, _tz in fake_tracker.queries()} == {other.pk}


def test_local_days_follow_the_company_time_zone(lome, fake_tracker):
    company = CompanySettings.load(lome["company"])
    company.time_zone = "Europe/Paris"  # UTC+1 in winter, UTC+2 in summer
    company.save()
    site, yao = lome["site"], lome["yao"]
    # 00:30 on 25 October in Paris is still summer time (UTC+2).
    visit(site, yao, datetime.datetime(2026, 10, 24, 22, 30, tzinfo=UTC), None)
    # 23:30 on 25 October in Paris is winter time (UTC+1): still the 25th.
    visit(site, yao, datetime.datetime(2026, 10, 25, 22, 30, tzinfo=UTC), None)
    # 00:30 on the 26th: not that day.
    visit(site, yao, datetime.datetime(2026, 10, 25, 23, 30, tzinfo=UTC), None)
    day = datetime.date(2026, 10, 25)  # 25 hours: summer time ends
    fake_tracker.days[(site.pk, day)] = [
        tracked(yao, datetime.datetime(2026, 10, 24, 22, 30, tzinfo=UTC), at(9, day=day)),
        tracked(
            yao,
            datetime.datetime(2026, 10, 25, 22, 30, tzinfo=UTC),
            datetime.datetime(2026, 10, 25, 22, 59, tzinfo=UTC),
        ),
    ]

    reconcile.run(day=day, now=datetime.datetime(2026, 10, 26, 3, 15, tzinfo=UTC))

    assert fake_tracker.queries() == [(site.pk, "2026-10-25", "Europe/Paris")]
    assert [(s[1], s[2]) for s in sessions(site, day=day)] == [
        (datetime.datetime(2026, 10, 24, 22, 30, tzinfo=UTC), at(9, day=day)),
        (
            datetime.datetime(2026, 10, 25, 22, 30, tzinfo=UTC),
            datetime.datetime(2026, 10, 25, 22, 59, tzinfo=UTC),
        ),
    ]
    next_day = sessions(site, day=datetime.date(2026, 10, 26))
    assert next_day == [(yao.pk, datetime.datetime(2026, 10, 25, 23, 30, tzinfo=UTC), None, ["A"])]
    done = record(site, day)
    assert done.window_start == datetime.datetime(2026, 10, 24, 22, 0, tzinfo=UTC)
    assert done.window_end == datetime.datetime(2026, 10, 25, 23, 0, tzinfo=UTC)


@pytest.mark.parametrize(
    ("time_zone", "latest"),
    [
        ("Africa/Lome", DAY),  # 03:15 there: yesterday ended 3 h ago
        ("Europe/Paris", DAY),
        ("Pacific/Kiritimati", DAY),  # UTC+14: 17:15 on 1 November
        ("America/Sao_Paulo", DAY - datetime.timedelta(days=1)),  # 00:15: too soon
        ("America/Los_Angeles", DAY - datetime.timedelta(days=1)),  # 20:15 on the 31st
    ],
)
def test_the_night_reconciles_each_companys_settled_days(default_company, time_zone, latest):
    company = CompanySettings.load(default_company)
    company.time_zone = time_zone
    company.save()

    days = reconcile.due_days(company, now=NOW)

    assert days[-1] == latest
    assert days == [latest - datetime.timedelta(days=n) for n in range(6, -1, -1)]


def test_days_whose_track_is_deleted_are_not_reconciled(default_company):
    company = CompanySettings.load(default_company)
    company.track_retention_days = 3
    company.save()

    assert reconcile.due_days(company, now=NOW) == [DAY - datetime.timedelta(days=1), DAY]


def test_a_day_that_is_not_over_is_skipped(lome, fake_tracker):
    summary = reconcile.run(day=DAY + datetime.timedelta(days=1), now=NOW)

    assert fake_tracker.requests == []
    assert summary.skipped == [(lome["site"].pk, "2026-11-01 is not over yet")]


def test_without_a_tracker_nothing_happens(lome, settings, monkeypatch):
    settings.TRACKER_URL = ""
    urlopen = mock.MagicMock()
    monkeypatch.setattr("urllib.request.urlopen", urlopen)

    summary = reconcile.run(day=DAY, now=NOW)

    assert summary.configured is False
    urlopen.assert_not_called()
    assert not PresenceReconciliation.objects.exists()


# --- failures --------------------------------------------------------------------------------


def test_an_unreachable_tracker_is_recorded_and_tried_again_the_next_night(lome, fake_tracker):
    site, yao = lome["site"], lome["yao"]
    south = SiteFactory(name="South")
    visit(site, yao, at(7), at(12))
    fake_tracker.error = urllib.error.URLError("connection refused")

    summary = reconcile.run(now=NOW)

    assert summary.reconciled == 0 and summary.failed == 14  # 7 days × 2 sites
    assert len(fake_tracker.requests) == 1  # not asked again once it is down
    failed = record(site)
    assert failed.reconciled_at is None and failed.attempts == 1
    assert "connection refused" in failed.last_error
    assert "connection refused" in record(south).last_error
    assert sessions(site) == [(yao.pk, at(7), at(12), ["A"])]

    fake_tracker.error = None
    fake_tracker.days[(site.pk, DAY)] = [tracked(yao, at(7), at(16), ["A"])]
    nextnight = reconcile.run(now=NOW + datetime.timedelta(days=1))

    asked = {(site_id, date) for site_id, date, _tz in fake_tracker.queries()[1:]}
    assert (site.pk, "2026-10-31") in asked and (site.pk, "2026-11-01") in asked
    assert nextnight.failed == 0
    assert sessions(site) == [(yao.pk, at(7), at(16), ["A"])]
    done = record(site)
    assert done.reconciled_at is not None and done.last_error == "" and done.attempts == 2


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (
            lambda: urllib.error.HTTPError(
                TRACKER, 404, "Not Found", {}, io.BytesIO(b'{"code":"site_not_configured"}')
            ),
            "HTTP 404 site_not_configured",
        ),
        (
            lambda: urllib.error.HTTPError(TRACKER, 401, "Unauthorized", {}, io.BytesIO(b"")),
            "HTTP 401",
        ),
        (lambda: urllib.error.HTTPError(TRACKER, 503, "Unavailable", {}, None), "HTTP 503"),
    ],
)
def test_an_error_answer_does_not_stop_the_other_sites(lome, fake_tracker, error, expected):
    site, yao = lome["site"], lome["yao"]
    south = SiteFactory(name="South")
    visit(south, yao, at(7), at(12))
    fake_tracker.errors[site.pk] = error
    fake_tracker.days[(south.pk, DAY)] = [tracked(yao, at(7), at(16), ["A"])]

    summary = reconcile.run(now=NOW)

    assert record(site).last_error == expected
    assert record(site).reconciled_at is None
    assert record(south).reconciled_at is not None
    assert sessions(south) == [(yao.pk, at(7), at(16), ["A"])]
    assert summary.failed == 7 and summary.reconciled == 7


def session_payload(**changes) -> dict:
    item = {
        "subject": {"kind": "worker", "id": 1},
        "started_at": "2026-10-31T07:00:00Z",
        "ended_at": "2026-10-31T16:00:00Z",
        "zones": ["A"],
    }
    item.update(changes)
    return item


@pytest.mark.parametrize(
    "body",
    [
        b"not json",
        b'{"sessions": "none"}',
        b'"sessions"',
        json.dumps({"site_id": 999_999, "sessions": []}).encode(),
        json.dumps({"date": "2026-10-30", "sessions": []}).encode(),
        json.dumps(
            {"from": "2026-10-31T00:00:00Z", "to": "2026-11-03T00:00:00Z", "sessions": []}
        ).encode(),
        json.dumps({"sessions": [session_payload(subject={"kind": "robot", "id": 1})]}).encode(),
        json.dumps({"sessions": [session_payload(subject={"kind": "worker", "id": "1"})]}).encode(),
        json.dumps({"sessions": [session_payload(started_at="yesterday")]}).encode(),
        json.dumps({"sessions": [session_payload(started_at="2026-10-31T07:00:00")]}).encode(),
        json.dumps({"sessions": [session_payload(started_at="2026-11-01T07:00:00Z")]}).encode(),
        json.dumps({"sessions": [session_payload(ended_at="2026-10-31T06:00:00Z")]}).encode(),
        json.dumps({"sessions": [session_payload(zones="A")]}).encode(),
    ],
)
def test_a_malformed_answer_changes_nothing(lome, fake_tracker, body):
    site, yao = lome["site"], lome["yao"]
    visit(site, yao, at(7), at(12))
    fake_tracker.bodies[site.pk] = body

    summary = reconcile.run(day=DAY, now=NOW)

    assert summary.failed == 1
    assert record(site).last_error and record(site).reconciled_at is None
    assert sessions(site) == [(yao.pk, at(7), at(12), ["A"])]


def test_the_first_planned_shape_a_bare_list_is_read(lome, fake_tracker):
    site, yao = lome["site"], lome["yao"]
    fake_tracker.bodies[site.pk] = json.dumps([tracked(yao, at(7), at(16), ["A"])]).encode()

    reconcile.run(day=DAY, now=NOW)

    assert sessions(site) == [(yao.pk, at(7), at(16), ["A"])]
    assert record(site).window_start == at(0)


def test_an_empty_replay_for_a_day_with_visits_changes_nothing(lome, fake_tracker):
    site, yao = lome["site"], lome["yao"]
    visit(site, yao, at(7), at(12))

    summary = reconcile.run(day=DAY, now=NOW)

    assert summary.failed == 1
    assert "no visit" in record(site).last_error
    assert sessions(site) == [(yao.pk, at(7), at(12), ["A"])]
    assert PresenceSession.objects.get().source == "live"


def test_an_unexpected_error_is_recorded_and_the_run_goes_on(lome, fake_tracker, monkeypatch):
    south = SiteFactory(name="South")
    original = reconcile._apply

    def broken(site, *args):
        if site.pk == lome["site"].pk:
            raise RuntimeError("disk full")
        return original(site, *args)

    monkeypatch.setattr(reconcile, "_apply", broken)

    summary = reconcile.run(day=DAY, now=NOW)

    assert record(lome["site"]).last_error == "RuntimeError: disk full"
    assert record(south).reconciled_at is not None
    assert summary.failed == 1 and summary.reconciled == 1


# --- audit, task, command ----------------------------------------------------------------------


def test_each_run_writes_one_audit_entry_per_company(lome, fake_tracker):
    site, yao = lome["site"], lome["yao"]
    SiteFactory(name="South")
    visit(site, yao, at(7), at(12))
    fake_tracker.days[(site.pk, DAY)] = [tracked(yao, at(7), at(16), ["A"])]

    reconcile.run(day=DAY, now=NOW)

    entry = AuditLog.objects.get(action="presence.reconcile")
    assert entry.company_id == lome["company"].pk
    assert entry.actor is None
    assert entry.after == {
        "days": ["2026-10-31"],
        "sites": 2,
        "reconciled": 2,
        "failed": 0,
        "added": 0,
        "changed": 1,
        "removed": 0,
        "ignored": 0,
    }
    assert AuditLog.objects.count() == 1  # nothing per session

    reconcile.run(now=NOW)  # the night's other days
    reconcile.run(now=NOW)  # nothing left to do: no entry
    assert AuditLog.objects.filter(action="presence.reconcile").count() == 2


def test_the_task_reconciles_the_night(lome, fake_tracker, frozen):
    fake_tracker.days[(lome["site"].pk, DAY)] = [tracked(lome["yao"], at(7), at(16))]

    assert tasks.reconcile_sessions() == 7

    assert sessions(lome["site"]) == [(lome["yao"].pk, at(7), at(16), [])]


def test_the_task_never_raises_for_the_tracker(lome, fake_tracker, frozen):
    fake_tracker.error = urllib.error.URLError("down")

    assert tasks.reconcile_sessions() == 0


def test_two_reconciliations_never_run_at_once(lome, fake_tracker):
    with tasks.held(tasks.RECONCILE_LOCK, 60) as taken:
        assert taken
        assert tasks.reconcile_sessions() == 0
    assert fake_tracker.requests == []


def test_the_reconciliation_stops_at_its_time_limit(lome, fake_tracker, frozen):
    SiteFactory(name="South")
    fake_tracker.error = SoftTimeLimitExceeded()

    assert tasks.reconcile_sessions() == 0

    assert len(fake_tracker.requests) == 1  # not caught as one site's failure
    with tasks.held(tasks.RECONCILE_LOCK, 60) as taken:
        assert taken  # released


def test_the_beat_schedule_reconciles_every_night():
    entry = django_settings.CELERY_BEAT_SCHEDULE["reconcile-presence"]
    assert entry["task"] == "presence.reconcile_sessions"
    assert entry["schedule"].hour == {3} and entry["schedule"].minute == {15}


def test_the_command_reconciles_a_site_and_day(lome, fake_tracker, frozen):
    site, yao = lome["site"], lome["yao"]
    SiteFactory(name="South")
    fake_tracker.days[(site.pk, DAY)] = [tracked(yao, at(7), at(16))]
    out = io.StringIO()

    call_command("reconcile_presence", "--site", str(site.pk), "--date", "2026-10-31", stdout=out)

    assert fake_tracker.queries() == [(site.pk, "2026-10-31", "Africa/Lome")]
    assert "1 added" in out.getvalue()
    assert "1 day(s) reconciled, 0 failed" in out.getvalue()


def test_the_command_reports_failures(lome, fake_tracker, frozen):
    fake_tracker.error = urllib.error.URLError("down")
    err = io.StringIO()

    with pytest.raises(CommandError, match="tried again tonight"):
        call_command("reconcile_presence", "--date", "2026-10-31", stdout=io.StringIO(), stderr=err)

    assert "Unreachable" in err.getvalue()


def test_the_command_refuses_an_unknown_site(lome, fake_tracker):
    with pytest.raises(CommandError, match="Unknown site"):
        call_command("reconcile_presence", "--site", "999999")


def test_the_command_needs_a_tracker(lome, settings):
    settings.TRACKER_URL = ""
    with pytest.raises(CommandError, match="TRACKER_URL"):
        call_command("reconcile_presence", stdout=io.StringIO())


# --- the tracker's contract ------------------------------------------------------------------


def test_the_fake_answers_like_the_tracker_contract(lome, fake_tracker):
    """Once the tracker's ``SessionDay`` schema is in ``tracker-openapi.yaml`` (brief P1),
    the fake's answer must follow it."""
    spec = yaml.safe_load(TRACKER_SPEC.read_text())
    if "SessionDay" not in spec["components"]["schemas"]:
        pytest.skip("tracker-openapi.yaml has no SessionDay yet (brief P1)")
    fake_tracker.days[(lome["site"].pk, DAY)] = [
        tracked(lome["yao"], at(7), at(16), ["A"]),
        tracked(lome["kodjo"], at(7), None),
    ]
    schema = {"$ref": "#/components/schemas/SessionDay", "components": spec["components"]}

    answer = fake_tracker.answer(lome["site"].pk, "2026-10-31", "Africa/Lome")

    Draft202012Validator(schema).validate(answer)
