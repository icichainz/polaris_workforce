"""Presence sessions as roll-call suggestions (contract 1.5, agent P5).

What must hold:
- A crew worker's suggestion comes from check-ins, presence or both (``sources``); with
  both, the check-ins decide ``status``/``arrival``/``departure`` and the presence is
  shown next to them.
- Presence counts only where ``tracking_enabled`` resolves on for the site and the
  worker (company → site → worker), only for the site's crew that day, only workers.
- A presence-only status: less than ``half_day_units`` of the working hours on site →
  half day (an open session counts until ``work_end``); else late after ``late_after``;
  else present.
- Presence never marks attendance: nothing is written until the foreman accepts, and
  accepting goes through the records upsert, audited with ``from_suggestion``.
- The endpoint stays site- and company-scoped; the office shows "Seen 07:02–16:48".
"""

import datetime
import zoneinfo
from decimal import Decimal

import pytest
from django.conf import settings as django_settings
from django.urls import reverse

from apps.attendance.models import AttendanceDay, AttendanceRecord
from apps.checkin import selectors
from apps.core.dates import company_today
from apps.core.models import AuditLog, CompanySettings
from apps.core.tenancy import use_company
from apps.presence.models import PresenceSession
from tests.e2e import Office, Phone, hire, make_site, new_uuid
from tests.e2e import record as mark
from tests.factories import CompanyFactory, CompanySettingsFactory, SiteFactory, WorkerFactory
from tests.test_checkin_services import check

pytestmark = pytest.mark.django_db
LOME = zoneinfo.ZoneInfo("Africa/Lome")
PIN = "4826"


@pytest.fixture
def crew():
    CompanySettingsFactory(working_days=[1, 2, 3, 4, 5, 6, 7], tracking_enabled=True)
    crew = make_site("Riverside", phone_number="+228 90 44 55 66", pin=PIN)
    crew.ama = hire(crew, "Ama", 5000)
    crew.kofi = hire(crew, "Kofi", 5000)
    crew.day = company_today() - datetime.timedelta(days=1)
    return crew


def at(day, hhmm: str) -> datetime.datetime:
    hour, minute = map(int, hhmm.split(":"))
    return datetime.datetime.combine(day, datetime.time(hour, minute), tzinfo=LOME)


def seen(site, worker_id, day, start, end=None, zones=("A",), kind="worker"):
    """One visit of ``worker_id`` to ``site`` (``end`` None: still on site)."""
    return PresenceSession.objects.create(
        company_id=site.company_id,
        site=site,
        subject_kind=kind,
        subject_id=worker_id,
        local_date=day,
        started_at=at(day, start),
        ended_at=at(day, end) if end else None,
        end_reason="timeout" if end else "",
        zones=list(zones),
        last_event_at=at(day, end or start),
        event_count=2,
    )


def suggestion_of(crew, worker):
    found = [
        s
        for s in selectors.day_suggestions(crew.site, crew.day).suggestions
        if s.worker_id == worker.pk
    ]
    return found[0] if found else None


def hm(value):
    return value.strftime("%H:%M") if value else None


def settings(**changes):
    company = CompanySettings.load()
    for name, value in changes.items():
        setattr(company, name, value)
    company.save()


# --- the selector ---------------------------------------------------------------------------


def test_check_ins_only(crew):
    check(crew, crew.ama, when="07:05")

    s = suggestion_of(crew, crew.ama)

    assert s.sources == ("checkin",) and s.presence is None
    assert (s.status, hm(s.arrival), s.checkin_count) == ("present", "07:05", 1)
    assert suggestion_of(crew, crew.kofi) is None  # neither: no suggestion


def test_presence_only(crew):
    seen(crew.site, crew.ama.pk, crew.day, "07:02", "16:48", zones=["A", "B"])

    s = suggestion_of(crew, crew.ama)

    assert s.sources == ("presence",)
    assert (s.status, hm(s.arrival), hm(s.departure)) == ("present", "07:02", "16:48")
    assert (s.first_in_at, s.last_out_at, s.checkin_count) == (None, None, 0)
    p = s.presence
    assert (hm(p.seen_from), hm(p.seen_until), p.open, p.zones, p.visits) == (
        "07:02",
        "16:48",
        False,
        ["A", "B"],
        1,
    )
    assert p.first_seen_at == at(crew.day, "07:02") and p.status == "present"


def test_both_prefers_the_check_ins_and_shows_the_presence(crew):
    check(crew, crew.ama, when="07:35")  # late by the check-in
    seen(crew.site, crew.ama.pk, crew.day, "07:02", "16:48")  # on time by presence

    s = suggestion_of(crew, crew.ama)

    assert s.sources == ("checkin", "presence")
    assert (s.status, hm(s.arrival), hm(s.departure), s.checkin_count) == (
        "late",
        "07:35",
        None,
        1,
    )
    assert s.first_in_at == at(crew.day, "07:35")
    assert (s.presence.status, hm(s.presence.seen_from)) == ("present", "07:02")


@pytest.mark.parametrize(
    ("visits", "status", "departure"),
    [
        ([("07:15", "17:00")], "present", "17:00"),  # on the dot: not late
        ([("07:16", "17:00")], "late", "17:00"),
        ([("07:00", "11:59")], "half_day", "11:59"),  # 4h59 of 07:00–17:00 < 5h
        ([("07:00", "12:00")], "present", "12:00"),  # 5h: not less than half
        ([("12:30", "17:00")], "half_day", "17:00"),
        ([("05:30", "11:30")], "half_day", "11:30"),  # only 4h30 inside working hours
        # Several visits fold: first sighting to last one.
        ([("07:05", "12:00"), ("13:00", "16:40")], "present", "16:40"),
    ],
)
def test_presence_only_status_rules(crew, visits, status, departure):
    for start, end in visits:
        seen(crew.site, crew.ama.pk, crew.day, start, end)

    s = suggestion_of(crew, crew.ama)

    assert (s.status, hm(s.departure)) == (status, departure)
    assert s.presence.visits == len(visits)


@pytest.mark.parametrize(
    ("start", "status"),
    [("07:10", "present"), ("09:00", "late"), ("12:30", "half_day")],
)
def test_an_open_session_counts_until_work_ends(crew, start, status):
    seen(crew.site, crew.ama.pk, crew.day, start)

    s = suggestion_of(crew, crew.ama)

    assert s.status == status and s.departure is None
    assert s.presence.open and s.presence.last_seen_at is None and s.presence.seen_until is None


def test_an_open_visit_after_a_closed_one_keeps_the_day_open(crew):
    seen(crew.site, crew.ama.pk, crew.day, "07:00", "12:00")
    seen(crew.site, crew.ama.pk, crew.day, "13:00")

    p = suggestion_of(crew, crew.ama).presence

    assert p.open and p.seen_until is None and p.visits == 2


def test_the_rules_read_the_settings(crew):
    settings(half_day_units=Decimal("0.25"), late_after=datetime.time(7, 45))
    crew.site.work_start = datetime.time(6, 0)
    crew.site.work_end = datetime.time(14, 0)
    crew.site.save()
    seen(crew.site, crew.ama.pk, crew.day, "07:30", "10:00")  # 2h30 of 8h ≥ 2h

    s = suggestion_of(crew, crew.ama)

    assert s.status == "present"  # not late (07:45), not a half day (a quarter is 2h)


def test_site_late_after_override(crew):
    crew.site.late_after = datetime.time(8, 0)
    crew.site.save()
    seen(crew.site, crew.ama.pk, crew.day, "07:45", "17:00")
    assert suggestion_of(crew, crew.ama).status == "present"


def test_tracking_off_for_the_company_hides_presence(crew):
    settings(tracking_enabled=False)
    seen(crew.site, crew.ama.pk, crew.day, "07:02", "16:48")
    check(crew, crew.kofi, when="07:00")
    seen(crew.site, crew.kofi.pk, crew.day, "07:00", "17:00")

    assert suggestion_of(crew, crew.ama) is None
    kofi = suggestion_of(crew, crew.kofi)
    assert kofi.sources == ("checkin",) and kofi.presence is None


def test_tracking_off_for_the_site_or_the_worker(crew):
    seen(crew.site, crew.ama.pk, crew.day, "07:02", "16:48")
    seen(crew.site, crew.kofi.pk, crew.day, "07:02", "16:48")
    crew.kofi.tracking_enabled = False
    crew.kofi.save()

    assert suggestion_of(crew, crew.ama) is not None
    assert suggestion_of(crew, crew.kofi) is None

    crew.site.tracking_enabled = False
    crew.site.save()
    assert suggestion_of(crew, crew.ama) is None

    # The worker's own setting wins over the site's.
    crew.kofi.tracking_enabled = True
    crew.kofi.save()
    assert suggestion_of(crew, crew.kofi).sources == ("presence",)


def test_only_the_crew_and_only_workers(crew):
    outsider = WorkerFactory()
    seen(crew.site, outsider.pk, crew.day, "07:00", "17:00")
    seen(crew.site, crew.ama.pk, crew.day, "07:00", "17:00", kind="foreman")
    other_site = SiteFactory(name="Hilltop")
    seen(other_site, crew.kofi.pk, crew.day, "07:00", "17:00")
    seen(crew.site, crew.kofi.pk, crew.day - datetime.timedelta(days=1), "07:00", "17:00")

    assert selectors.day_suggestions(crew.site, crew.day).suggestions == []


def test_suggestions_are_by_worker_id(crew):
    seen(crew.site, crew.kofi.pk, crew.day, "07:00", "17:00")
    check(crew, crew.ama, when="07:00")
    ids = [s.worker_id for s in selectors.day_suggestions(crew.site, crew.day).suggestions]
    assert ids == sorted([crew.ama.pk, crew.kofi.pk])


def test_presence_never_writes_the_roll_call(crew):
    """Even in ``self_check_in`` mode, where check-ins write the draft."""
    settings(attendance_mode="self_check_in")
    seen(crew.site, crew.ama.pk, crew.day, "07:00", "17:00")

    assert selectors.day_suggestions(crew.site, crew.day).suggestions
    assert selectors.worker_suggestion(crew.site, crew.day, crew.ama.pk) is None
    assert not AttendanceRecord.objects.exists() and not AttendanceDay.objects.exists()

    check(crew, crew.kofi, when="07:20")  # a check-in writes Kofi's draft, not Ama's
    assert list(AttendanceRecord.objects.values_list("worker_id", flat=True)) == [crew.kofi.pk]


# --- the endpoint -------------------------------------------------------------------------------


def url(site, day):
    return f"/api/v1/sites/{site.pk}/days/{day}/suggestions/"


def test_endpoint_shape(crew):
    phone = Phone(crew.phone_number, PIN)
    check(crew, crew.ama, when="07:35")
    seen(crew.site, crew.ama.pk, crew.day, "07:02", "16:48", zones=["A", "B"])
    seen(crew.site, crew.kofi.pk, crew.day, "07:31", zones=["A"])

    response = phone.client.get(url(crew.site, crew.day))

    assert response.status_code == 200
    by_worker = {s["worker_id"]: s for s in response.json()["suggestions"]}
    assert by_worker[crew.ama.pk]["sources"] == ["checkin", "presence"]
    assert by_worker[crew.ama.pk]["status"] == "late"
    assert by_worker[crew.ama.pk]["presence"] == {
        "first_seen_at": f"{crew.day}T07:02:00Z",
        "last_seen_at": f"{crew.day}T16:48:00Z",
        "open": False,
        "seen_from": "07:02",
        "seen_until": "16:48",
        "zones": ["A", "B"],
        "visits": 1,
        "status": "present",
    }
    kofi = by_worker[crew.kofi.pk]
    assert {k: kofi[k] for k in ("status", "arrival", "departure", "checkin_count")} == {
        "status": "late",
        "arrival": "07:31",
        "departure": None,
        "checkin_count": 0,
    }
    assert kofi["sources"] == ["presence"] and kofi["presence"]["open"] is True
    assert (kofi["first_in_at"], kofi["last_out_at"]) == (None, None)


def test_a_check_in_suggestion_keeps_its_1_4_fields(crew):
    phone = Phone(crew.phone_number, PIN)
    check(crew, crew.ama, when="07:05")
    check(crew, crew.ama, direction="out", when="11:30")

    s = phone.client.get(url(crew.site, crew.day)).json()["suggestions"][0]

    assert s == {
        "worker_id": crew.ama.pk,
        "status": "half_day",
        "arrival": "07:05",
        "departure": "11:30",
        "first_in_at": f"{crew.day}T07:05:00Z",
        "last_out_at": f"{crew.day}T11:30:00Z",
        "checkin_count": 2,
        "sources": ["checkin"],
        "presence": None,
    }


def test_endpoint_is_site_scoped(crew):
    other = make_site("Hilltop", phone_number="+228 90 77 88 99", pin=PIN)
    stranger = hire(other, "Esi", 5000)
    seen(other.site, stranger.pk, crew.day, "07:00", "17:00")
    phone = Phone(crew.phone_number, PIN)

    response = phone.client.get(url(other.site, crew.day))

    assert response.status_code == 403
    assert response.json()["code"] == "permission_denied"
    assert Phone(other.phone_number, PIN).client.get(url(other.site, crew.day)).json()[
        "suggestions"
    ][0]["worker_id"] == (stranger.pk)


def test_another_companys_presence_never_shows(crew):
    """A session of another company's worker on our site (its sightings were stored with
    a subject id), or one on the other company's site: never in our suggestions."""
    other_company = CompanyFactory(name="Other Co")
    with use_company(other_company):
        CompanySettingsFactory(tracking_enabled=True)
        their_site = SiteFactory(name="Their site")
        theirs = WorkerFactory(first_name="Kwame")
    seen(their_site, crew.ama.pk, crew.day, "07:00", "17:00")
    seen(crew.site, theirs.pk, crew.day, "07:00", "17:00")
    phone = Phone(crew.phone_number, PIN)

    assert phone.client.get(url(crew.site, crew.day)).json()["suggestions"] == []
    assert phone.client.get(url(their_site, crew.day)).status_code == 403


# --- accepting a suggestion -----------------------------------------------------------------------


def records_url(site, day):
    return f"/api/v1/sites/{site.pk}/days/{day}/records/"


def last_upsert():
    return AuditLog.objects.filter(action="attendance.records.upsert").order_by("-pk").first()


def test_accepting_a_presence_suggestion_is_audited(crew):
    seen(crew.site, crew.ama.pk, crew.day, "07:31")
    phone = Phone(crew.phone_number, PIN)
    s = phone.client.get(url(crew.site, crew.day)).json()["suggestions"][0]
    item = {
        **mark(crew.ama, s["status"], s["arrival"], s["departure"]),
        "from_suggestion": s["sources"],
    }

    response = phone.put_records(crew.site.pk, crew.day, [item])

    assert response.status_code == 200
    record = AttendanceRecord.objects.get(worker=crew.ama)
    assert (record.status, hm(record.arrival), str(record.client_uuid)) == (
        "late",
        "07:31",
        item["client_uuid"],
    )
    entry = last_upsert()
    assert entry.actor == crew.foreman
    assert entry.after["from_suggestion"] == {str(crew.ama.pk): ["presence"]}
    assert entry.after["records"][str(crew.ama.pk)]["status"] == "late"

    # Idempotent: the same write again changes nothing and writes no audit entry.
    count = AuditLog.objects.count()
    assert phone.put_records(crew.site.pk, crew.day, [item]).status_code == 200
    assert AuditLog.objects.count() == count


def test_a_mark_by_hand_has_no_suggestion_in_its_audit(crew):
    phone = Phone(crew.phone_number, PIN)
    assert phone.put_records(crew.site.pk, crew.day, [mark(crew.ama, "present")]).status_code == 200
    assert "from_suggestion" not in last_upsert().after


def test_only_the_changed_records_carry_their_suggestion(crew):
    phone = Phone(crew.phone_number, PIN)
    ama = mark(crew.ama, "present", "07:00")
    phone.put_records(crew.site.pk, crew.day, [ama])
    kofi = {**mark(crew.kofi, "late", "07:31"), "from_suggestion": ["presence", "checkin"]}

    phone.put_records(crew.site.pk, crew.day, [{**ama, "from_suggestion": ["presence"]}, kofi])

    # Ama's record did not change: only Kofi's is listed, sources in the contract order.
    assert last_upsert().after["from_suggestion"] == {str(crew.kofi.pk): ["checkin", "presence"]}


def test_an_unknown_source_is_refused(crew):
    phone = Phone(crew.phone_number, PIN)
    item = {**mark(crew.ama, "present"), "from_suggestion": ["gps"]}
    response = phone.put_records(crew.site.pk, crew.day, [item])
    assert response.status_code == 400
    assert "from_suggestion" in response.json()["details"]["records"][0]
    assert not AttendanceRecord.objects.exists()


def test_self_check_in_drafts_are_audited_as_check_in_suggestions(crew):
    settings(attendance_mode="self_check_in")
    check(crew, crew.ama, when="07:05")
    entry = last_upsert()
    assert entry.after["from_suggestion"] == {str(crew.ama.pk): ["checkin"]}


# --- office screens -----------------------------------------------------------------------------


def submitted_day(crew):
    phone = Phone(crew.phone_number, PIN)
    phone.put_records(
        crew.site.pk,
        crew.day,
        [mark(crew.ama, "present", "07:00"), mark(crew.kofi, "present", "07:00")],
    )
    assert phone.submit(crew.site.pk, crew.day, new_uuid()).status_code == 200
    return AttendanceDay.objects.get(site=crew.site, date=crew.day)


def test_approvals_show_what_presence_saw(crew):
    seen(crew.site, crew.ama.pk, crew.day, "07:02", "16:48", zones=["B"])
    seen(crew.site, crew.kofi.pk, crew.day, "07:31", zones=["A", "B"])
    day = submitted_day(crew)

    response = Office(crew.manager).get(reverse("attendance:approvals"), {"day": day.pk})

    assert response.status_code == 200
    rows = {row["record"].worker_id: row for row in response.context["detail"]["rows"]}
    assert hm(rows[crew.ama.pk]["presence"].seen_until) == "16:48"
    html = response.content.decode()
    assert "Seen 07:02–16:48 · zone B" in html
    assert "Seen since 07:31, still on site · zones A, B" in html


def test_approvals_without_tracking_show_no_presence(crew):
    settings(tracking_enabled=False)
    seen(crew.site, crew.ama.pk, crew.day, "07:02", "16:48")
    day = submitted_day(crew)

    response = Office(crew.manager).get(reverse("attendance:approvals"), {"day": day.pk})

    assert all(row["presence"] is None for row in response.context["detail"]["rows"])
    assert "data-presence-seen" not in response.content.decode()


def test_check_in_site_day_shows_presence(crew):
    seen(crew.site, crew.ama.pk, crew.day, "07:02", "16:48", zones=["B"])

    response = Office(crew.manager).get(reverse("checkin:site_day", args=[crew.site.pk, crew.day]))

    assert response.status_code == 200
    assert [s["s"].sources for s in response.context["suggestions"]] == [("presence",)]
    assert response.context["presence_shown"] is True
    assert "Seen 07:02–16:48 · zone B" in response.content.decode()


def test_presence_line_in_french(crew):
    seen(crew.site, crew.ama.pk, crew.day, "07:31", zones=["A", "B"])
    office = Office(crew.manager)
    office.client.cookies[django_settings.LANGUAGE_COOKIE_NAME] = "fr"

    response = office.get(reverse("checkin:site_day", args=[crew.site.pk, crew.day]))

    html = response.content.decode()
    assert "Vu depuis 07:31, toujours sur le chantier · zones A, B" in html
    assert "Présence" in html  # the column header
