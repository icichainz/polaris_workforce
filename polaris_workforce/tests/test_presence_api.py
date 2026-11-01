"""Presence API: the phone's tracker token and consent, the tracker's event ingest and a
site's presence sessions.

What must hold:
- The token lists only the foreman's own sites with tracking on; a foreman must have
  accepted the current notice (``consent_required``); a refusal or tracking off on every
  site is ``tracking_disabled``.
- Consent is idempotent by ``client_uuid`` and audited.
- Events are stored once by ``event_id``; sessions are rebuilt from every stored event, so
  duplicates, replays and out-of-order batches give the same sessions; unknown sites and
  subjects are stored and counted as ``ignored``.
- Only the tracker's service token opens the ingest; the sessions are site-scoped.
"""

import datetime
import uuid

import pytest
from django.core.cache import cache
from django.utils import timezone
from rest_framework.test import APIClient
from rest_framework.throttling import ScopedRateThrottle

from apps.core.models import AuditLog, CompanySettings
from apps.core.tenancy import use_company
from apps.presence import tokens
from apps.presence.models import PresenceEvent, PresenceSession, TrackingConsent
from tests.factories import (
    AdminUserFactory,
    CompanyFactory,
    ForemanDeviceFactory,
    ForemanFactory,
    SiteFactory,
    SiteManagerFactory,
    UserFactory,
    WorkerFactory,
)

pytestmark = pytest.mark.django_db

UTC = datetime.UTC
VERSION = "2026-11-01"
TOKEN_URL = "/api/v1/tracking/token/"
CONSENT_URL = "/api/v1/tracking/consent/"
EVENTS_URL = "/api/v1/presence/events/"


def phone(user) -> APIClient:
    """An API client with a device token of ``user`` (``Authorization: Device …``)."""
    raw = f"device-{uuid.uuid4().hex}"
    ForemanDeviceFactory(user=user, raw_token=raw)
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f"Device {raw}")
    return client


def tracker() -> APIClient:
    """The tracker's outbox: a fresh service token, as the tracker mints it."""
    claims = {
        "sub": "service:tracker",
        "kind": "service",
        "sites": [],
        "all_sites": False,
        "aud": "polaris-django",
        "exp": int(timezone.now().timestamp()) + 300,
    }
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {tokens.encode(claims)}")
    return client


def turn_tracking_on(company=None) -> CompanySettings:
    settings = CompanySettings.load(company)
    settings.tracking_enabled = True
    settings.save()
    return settings


def accept(client, version=VERSION, accepted=True, client_uuid=None):
    return client.post(
        CONSENT_URL,
        {
            "client_uuid": str(client_uuid or uuid.uuid4()),
            "accepted": accepted,
            "text_version": version,
        },
        format="json",
    )


@pytest.fixture
def crew(default_company):
    turn_tracking_on(default_company)
    foreman = ForemanFactory(first_name="Kodjo")
    north = SiteFactory(name="North", foremen=[foreman])
    south = SiteFactory(name="South", foremen=[foreman], foreman_gps_interval_seconds=30)
    off = SiteFactory(name="Off", foremen=[foreman], tracking_enabled=False)
    elsewhere = SiteFactory(name="Elsewhere")  # tracked, but not the foreman's
    worker = WorkerFactory(first_name="Yao")
    return {
        "foreman": foreman,
        "phone": phone(foreman),
        "north": north,
        "south": south,
        "off": off,
        "elsewhere": elsewhere,
        "worker": worker,
    }


# --- tracker token -----------------------------------------------------------------------------


def test_token_needs_consent_first(crew):
    response = crew["phone"].post(TOKEN_URL)

    assert response.status_code == 403
    assert response.json() == {
        "code": "consent_required",
        "message": "Accept the tracking notice first.",
        "details": {  # what the phone's notice shows
            "text_version": VERSION,
            "tracking_hours": {"start": "06:00", "end": "19:00"},
            "gps_interval_seconds": 30,
            "retention_days": 30,
        },
    }


def test_token_after_consent_lists_the_foreman_tracked_sites(crew, settings):
    settings.TRACKER_PUBLIC_URL = "https://tracker.example.com"
    assert accept(crew["phone"]).status_code == 200

    response = crew["phone"].post(TOKEN_URL)

    assert response.status_code == 200
    body = response.json()
    north, south = crew["north"], crew["south"]
    assert body["sites"] == sorted([north.pk, south.pk])
    assert body["tracker_url"] == "https://tracker.example.com"
    assert body["settings"] == {
        "gps_interval_seconds": 30,
        "tracking_hours": {"start": "06:00", "end": "19:00"},
        "retention_days": 30,
    }
    claims = tokens.decode(body["token"], audience="polaris-tracker")
    assert claims["kind"] == "device"
    assert claims["sub"] == f"foreman:{crew['foreman'].pk}"
    assert claims["company"] == crew["foreman"].company_id
    assert claims["sites"] == body["sites"]
    assert claims["travel"] is False
    lifetime = claims["exp"] - int(timezone.now().timestamp())
    assert tokens.DEVICE_LIFETIME - 5 <= lifetime <= tokens.DEVICE_LIFETIME
    expires = datetime.datetime.fromisoformat(body["expires_at"].replace("Z", "+00:00"))
    assert int(expires.timestamp()) == claims["exp"]


def test_tracking_hours_cover_every_site_of_the_token(crew):
    crew["north"].tracking_start = datetime.time(5, 30)
    crew["north"].save()
    crew["south"].tracking_end = datetime.time(20, 0)
    crew["south"].save()
    accept(crew["phone"])

    hours = crew["phone"].post(TOKEN_URL).json()["settings"]["tracking_hours"]

    assert hours == {"start": "05:30", "end": "20:00"}


def test_tracking_off_everywhere_is_tracking_disabled_before_any_consent(default_company):
    foreman = ForemanFactory()
    SiteFactory(foremen=[foreman])  # company setting: off by default

    response = phone(foreman).post(TOKEN_URL)

    assert response.status_code == 403
    assert response.json()["code"] == "tracking_disabled"
    assert response.json()["details"] == {"reason": "settings"}


def test_refusing_disables_tracking_until_accepted_again(crew):
    assert accept(crew["phone"]).status_code == 200
    refusal = accept(crew["phone"], accepted=False)
    assert refusal.status_code == 200
    assert refusal.json()["accepted"] is False

    refused = crew["phone"].post(TOKEN_URL)
    assert refused.status_code == 403
    assert refused.json()["code"] == "tracking_disabled"
    assert refused.json()["details"]["reason"] == "consent_refused"

    accept(crew["phone"])
    assert crew["phone"].post(TOKEN_URL).status_code == 200


def test_a_new_notice_version_asks_again(crew, settings):
    accept(crew["phone"])
    settings.TRACKING_CONSENT_TEXT_VERSION = "2027-01-01"

    response = crew["phone"].post(TOKEN_URL)

    assert response.status_code == 403
    assert response.json()["code"] == "consent_required"
    assert response.json()["details"]["text_version"] == "2027-01-01"


def test_token_needs_a_device_token_and_a_foreman(crew):
    assert APIClient().post(TOKEN_URL).status_code == 401
    for office_user in (SiteManagerFactory(), UserFactory(role="accountant")):
        assert phone(office_user).post(TOKEN_URL).status_code == 403


def test_an_admin_phone_gets_every_tracked_site_of_the_company_and_travel(crew):
    admin = AdminUserFactory()
    client = phone(admin)
    accept(client)
    with use_company(CompanyFactory(name="Bravo")) as bravo:
        turn_tracking_on(bravo)
        bravo_site = SiteFactory()

    claims = tokens.decode(client.post(TOKEN_URL).json()["token"], audience="polaris-tracker")

    expected = {crew["north"].pk, crew["south"].pk, crew["elsewhere"].pk}
    assert set(claims["sites"]) == expected
    assert bravo_site.pk not in claims["sites"]
    assert claims["travel"] is True


def test_token_requests_are_throttled(crew, monkeypatch):
    accept(crew["phone"])
    cache.clear()  # the consent counted in the same scope
    monkeypatch.setattr(ScopedRateThrottle, "THROTTLE_RATES", {"tracking": "2/minute"})

    statuses = [crew["phone"].post(TOKEN_URL).status_code for _ in range(3)]

    assert statuses == [200, 200, 429]


# --- consent ------------------------------------------------------------------------------------


def test_consent_is_recorded_once_per_client_uuid_and_audited(crew):
    client_uuid = uuid.uuid4()
    first = accept(crew["phone"], client_uuid=client_uuid)
    again = accept(crew["phone"], client_uuid=client_uuid, accepted=False)

    assert first.status_code == again.status_code == 200
    assert again["Idempotent-Replayed"] == "true"
    assert first.json() == again.json()
    assert first.json()["accepted"] is True
    assert first.json()["text_version"] == VERSION
    consent = TrackingConsent.objects.get()
    assert consent.user == crew["foreman"]
    assert consent.company_id == crew["foreman"].company_id
    entries = AuditLog.objects.filter(action="presence.consent.accept")
    assert entries.count() == 1
    assert entries.get().actor == crew["foreman"]


def test_a_refusal_is_audited_too(crew):
    accept(crew["phone"], accepted=False)
    assert AuditLog.objects.filter(action="presence.consent.refuse").count() == 1


def test_an_acceptance_must_name_the_current_notice(crew):
    response = accept(crew["phone"], version="2025-01-01")

    assert response.status_code == 400
    assert "text_version" in response.json()["details"]
    assert not TrackingConsent.objects.exists()


def test_a_withdrawal_is_taken_whatever_notice_the_phone_shows(crew, settings):
    accept(crew["phone"])
    settings.TRACKING_CONSENT_TEXT_VERSION = "2027-01-01"  # the phone is not updated yet

    response = accept(crew["phone"], version=VERSION, accepted=False)

    assert response.status_code == 200
    assert response.json()["accepted"] is False
    refused = crew["phone"].post(TOKEN_URL)
    assert refused.json()["code"] == "tracking_disabled"
    assert refused.json()["details"]["text_version"] == "2027-01-01"
    assert refused.json()["details"]["retention_days"] == 30


def test_the_notice_details_follow_the_settings(crew):
    company = CompanySettings.load()
    company.track_retention_days = 14
    company.save()

    details = crew["phone"].post(TOKEN_URL).json()["details"]

    assert details["retention_days"] == 14


def test_a_client_uuid_of_another_foreman_is_a_conflict(crew):
    client_uuid = uuid.uuid4()
    accept(crew["phone"], client_uuid=client_uuid)
    other = phone(ForemanFactory())

    response = accept(other, client_uuid=client_uuid)

    assert response.status_code == 409
    assert response.json()["code"] == "conflict"


# --- event ingest ------------------------------------------------------------------------------

T0 = datetime.datetime(2026, 10, 31, 7, 2, 40, tzinfo=UTC)


def event(kind, at, *, site, subject=("worker", 1), zone=None, from_zone=None, reason=None):
    return {
        "event_id": str(uuid.uuid4()),
        "site_id": site.pk if hasattr(site, "pk") else site,
        "subject": {"kind": subject[0], "id": subject[1]},
        "type": kind,
        "at": at.isoformat().replace("+00:00", "Z"),
        "zone_id": zone,
        "from_zone_id": from_zone,
        "reason": reason,
    }


def a_day(site, worker):
    """Entered A at 07:02, moved to B at 07:10, left at 12:00; back 13:00, left 16:48."""
    who = ("worker", worker.pk)
    return [
        event("entered", T0, site=site, subject=who, zone="A"),
        event("moved_zone", T0.replace(minute=10), site=site, subject=who, zone="B", from_zone="A"),
        event(
            "left",
            T0.replace(hour=12, minute=0),
            site=site,
            subject=who,
            zone="B",
            reason="timeout",
        ),
        event("entered", T0.replace(hour=13, minute=0), site=site, subject=who, zone="A"),
        event("left", T0.replace(hour=16, minute=48), site=site, subject=who, reason="timeout"),
    ]


def sessions_of(site, worker):
    return [
        (s.started_at, s.ended_at, s.zones, s.end_reason)
        for s in PresenceSession.objects.filter(
            site=site, subject_kind="worker", subject_id=worker.pk
        ).order_by("started_at")
    ]


EXPECTED_DAY = [
    (T0, T0.replace(hour=12, minute=0), ["A", "B"], "timeout"),
    (T0.replace(hour=13, minute=0), T0.replace(hour=16, minute=48), ["A"], "timeout"),
]


def test_events_build_sessions(crew):
    batch = a_day(crew["north"], crew["worker"])

    response = tracker().post(EVENTS_URL, {"events": batch}, format="json")

    assert response.status_code == 200
    assert response.json() == {"stored": 5, "duplicates": 0, "ignored": 0, "rejected": 0}
    assert sessions_of(crew["north"], crew["worker"]) == EXPECTED_DAY
    session = PresenceSession.objects.order_by("started_at").first()
    assert session.local_date == datetime.date(2026, 10, 31)
    assert session.company_id == crew["north"].company_id
    assert not AuditLog.objects.filter(action__startswith="presence.").exists()


def test_replaying_a_batch_changes_nothing(crew):
    batch = a_day(crew["north"], crew["worker"])
    tracker().post(EVENTS_URL, {"events": batch}, format="json")
    before = list(PresenceSession.objects.values().order_by("pk"))

    response = tracker().post(EVENTS_URL, {"events": batch}, format="json")

    assert response.json() == {"stored": 0, "duplicates": 5, "ignored": 0, "rejected": 0}
    assert PresenceEvent.objects.count() == 5
    assert list(PresenceSession.objects.values().order_by("pk")) == before


def test_duplicates_inside_a_batch_count_once(crew):
    batch = a_day(crew["north"], crew["worker"])

    response = tracker().post(EVENTS_URL, {"events": batch + batch[:2]}, format="json")

    assert response.json()["stored"] == 5
    assert response.json()["duplicates"] == 2
    assert sessions_of(crew["north"], crew["worker"]) == EXPECTED_DAY


@pytest.mark.parametrize(
    "order",
    [
        [4, 3, 2, 1, 0],  # newest first
        [2, 0, 4, 1, 3],
        [3, 4, 0, 1, 2],
    ],
)
def test_out_of_order_batches_give_the_same_sessions(crew, order):
    batch = a_day(crew["north"], crew["worker"])
    for index in order:  # one event per batch, in a shuffled order
        tracker().post(EVENTS_URL, {"events": [batch[index]]}, format="json")

    assert sessions_of(crew["north"], crew["worker"]) == EXPECTED_DAY


def test_a_late_left_closes_the_open_session(crew):
    batch = a_day(crew["north"], crew["worker"])
    tracker().post(EVENTS_URL, {"events": batch[:4]}, format="json")
    assert sessions_of(crew["north"], crew["worker"])[-1][1] is None  # still on site

    tracker().post(EVENTS_URL, {"events": batch[4:]}, format="json")

    assert sessions_of(crew["north"], crew["worker"]) == EXPECTED_DAY


def test_unknown_sites_and_subjects_are_stored_but_ignored(crew):
    north = crew["north"]
    with use_company(CompanyFactory(name="Bravo")):
        stranger = WorkerFactory()
    batch = [
        event("entered", T0, site=999_999, subject=("worker", crew["worker"].pk)),
        event("entered", T0, site=north, subject=("worker", 999_999)),
        event("entered", T0, site=north, subject=("worker", stranger.pk)),  # another company
        event("entered", T0, site=north, subject=("foreman", crew["foreman"].pk)),
    ]

    response = tracker().post(EVENTS_URL, {"events": batch}, format="json")

    assert response.json() == {"stored": 1, "duplicates": 0, "ignored": 3, "rejected": 0}
    assert PresenceEvent.objects.count() == 4
    assert PresenceEvent.objects.filter(applied=False).count() == 3
    unknown_site = PresenceEvent.objects.get(site_ref=999_999)
    assert unknown_site.site is None and unknown_site.company is None
    assert PresenceSession.objects.get().subject_kind == "foreman"


def test_an_invalid_item_is_rejected_without_holding_up_the_batch(crew):
    good = event("entered", T0, site=crew["north"], subject=("worker", crew["worker"].pk))
    bad = {**good, "event_id": "not-a-uuid"}

    response = tracker().post(EVENTS_URL, {"events": [bad, good]}, format="json")

    assert response.status_code == 200
    assert response.json() == {"stored": 1, "duplicates": 0, "ignored": 0, "rejected": 1}


def test_a_batch_of_more_than_500_events_is_refused(crew):
    one = event("entered", T0, site=crew["north"])
    response = tracker().post(EVENTS_URL, {"events": [one] * 501}, format="json")

    assert response.status_code == 400
    assert response.json()["code"] == "validation_error"
    assert not PresenceEvent.objects.exists()


def test_a_body_without_events_is_refused(crew):
    response = tracker().post(EVENTS_URL, {"nothing": []}, format="json")
    assert response.status_code == 400


@pytest.mark.parametrize("who", ["nobody", "device", "session", "dashboard", "expired", "long"])
def test_only_the_tracker_service_token_opens_the_ingest(crew, who):
    batch = {"events": a_day(crew["north"], crew["worker"])}
    now = int(timezone.now().timestamp())
    client = APIClient()
    service = {"sub": "service:tracker", "kind": "service", "aud": "polaris-django"}
    if who == "device":
        client = crew["phone"]
    elif who == "session":
        client.force_login(AdminUserFactory())
    elif who == "dashboard":
        token = tokens.mint(kind="dashboard", sub="user:1", company=1, lifetime=60).token
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")
    elif who == "expired":
        token = tokens.encode({**service, "exp": now - 1})
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")
    elif who == "long":
        token = tokens.encode({**service, "exp": now + 86400})
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")

    response = client.post(EVENTS_URL, batch, format="json")

    assert response.status_code == 401
    assert response.json()["code"] == "not_authenticated"
    assert not PresenceEvent.objects.exists()


def test_the_service_token_opens_nothing_else(crew):
    response = tracker().get(f"/api/v1/sites/{crew['north'].pk}/presence-sessions/?date=2026-10-31")
    assert response.status_code == 401


# --- sessions of a site's day -------------------------------------------------------------------


def sessions_url(site, date="2026-10-31"):
    return f"/api/v1/sites/{getattr(site, 'pk', site)}/presence-sessions/?date={date}"


def test_a_site_day_folds_each_person_s_visits(crew):
    north = crew["north"]
    batch = a_day(north, crew["worker"])
    batch.append(
        event("entered", T0.replace(minute=5), site=north, subject=("foreman", crew["foreman"].pk))
    )
    tracker().post(EVENTS_URL, {"events": batch}, format="json")

    response = crew["phone"].get(sessions_url(north))

    assert response.status_code == 200
    assert response.json() == {
        "site_id": north.pk,
        "date": "2026-10-31",
        "sessions": [
            {
                "subject": {"kind": "worker", "id": crew["worker"].pk},
                "first_seen_at": "2026-10-31T07:02:40Z",
                "last_seen_at": "2026-10-31T16:48:40Z",
                "open": False,
                "zones": ["A", "B"],
                "visits": 2,
            },
            {
                "subject": {"kind": "foreman", "id": crew["foreman"].pk},
                "first_seen_at": "2026-10-31T07:05:40Z",
                "last_seen_at": None,
                "open": True,
                "zones": [],
                "visits": 1,
            },
        ],
    }
    assert crew["phone"].get(sessions_url(north, "2026-10-30")).json()["sessions"] == []


def test_the_day_is_the_company_time_zone_day(crew):
    company = CompanySettings.load()
    company.time_zone = "America/New_York"
    company.save()
    late = datetime.datetime(2026, 11, 1, 2, 30, tzinfo=UTC)  # 22:30 on the 31st in New York
    who = ("worker", crew["worker"].pk)
    tracker().post(
        EVENTS_URL,
        {"events": [event("entered", late, site=crew["north"], subject=who)]},
        format="json",
    )

    assert len(crew["phone"].get(sessions_url(crew["north"], "2026-10-31")).json()["sessions"]) == 1
    assert crew["phone"].get(sessions_url(crew["north"], "2026-11-01")).json()["sessions"] == []


def test_sessions_need_a_date(crew):
    response = crew["phone"].get(f"/api/v1/sites/{crew['north'].pk}/presence-sessions/")
    assert response.status_code == 400
    assert "date" in response.json()["details"]


def test_sessions_are_site_scoped(crew):
    other_foreman = phone(ForemanFactory())
    with use_company(CompanyFactory(name="Bravo")):
        bravo_site = SiteFactory()
        bravo_foreman = ForemanFactory()
        bravo_site.foremen.add(bravo_foreman)
    bravo_phone = phone(bravo_foreman)

    for client, site in [
        (crew["phone"], crew["elsewhere"]),  # same company, not the foreman's site
        (other_foreman, crew["north"]),
        (crew["phone"], bravo_site),  # another company's site
        (bravo_phone, crew["north"]),
        (crew["phone"], 999_999),  # does not exist: the same 403
    ]:
        response = client.get(sessions_url(site))
        assert response.status_code == 403, site
        assert response.json()["code"] == "permission_denied"
    assert APIClient().get(sessions_url(crew["north"])).status_code == 401
