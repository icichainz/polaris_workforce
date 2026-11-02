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

import dataclasses
import datetime
import random
import uuid

import pytest
from django.core.cache import cache
from django.core.exceptions import PermissionDenied
from django.db import OperationalError
from django.utils import timezone
from rest_framework.test import APIClient
from rest_framework.throttling import ScopedRateThrottle

from apps.core.models import AuditLog, CompanySettings
from apps.core.tenancy import use_company
from apps.presence import engine, services, tokens
from apps.presence.api.serializers import PresenceEventSerializer
from apps.presence.models import PresenceEvent, PresenceSession, TrackingConsent
from tests.factories import (
    AdminUserFactory,
    CompanyFactory,
    ForemanDeviceFactory,
    ForemanFactory,
    PlatformAdminFactory,
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


def test_a_platform_admin_tracks_for_the_company_they_picked(crew):
    admin = PlatformAdminFactory()  # a superuser of no company: signed in, no device token
    client = APIClient()
    client.force_login(admin)

    assert accept(client).status_code == 200
    response = client.post(TOKEN_URL)

    assert response.status_code == 200
    company_id = crew["north"].company_id
    assert tokens.decode(response.json()["token"], audience="polaris-tracker")["company"] == (
        company_id
    )
    assert TrackingConsent.objects.get(user=admin).company_id == company_id


def test_without_a_company_token_and_consent_are_forbidden(crew, monkeypatch):
    monkeypatch.setattr(services, "company_id_of", lambda user: None)

    for response in (crew["phone"].post(TOKEN_URL), accept(crew["phone"])):
        assert response.status_code == 403
        assert response.json()["code"] == "permission_denied"
    assert not TrackingConsent.objects.exists()


def test_a_platform_admin_with_no_company_picked_is_refused(default_company):
    admin = PlatformAdminFactory()
    with use_company(None):
        with pytest.raises(PermissionDenied):
            services.device_tracking(admin)
        with pytest.raises(PermissionDenied):
            services.record_consent(
                admin, client_uuid=uuid.uuid4(), accepted=True, text_version=VERSION
            )


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


def on_record(user, *, accepted=True, at=None, version=VERSION) -> TrackingConsent:
    """An answer to the notice recorded at ``at`` (default: the day before ``T0``)."""
    return TrackingConsent.objects.create(
        company_id=user.company_id,
        user=user,
        accepted=accepted,
        text_version=version,
        at=at or T0 - datetime.timedelta(days=1),
        client_uuid=uuid.uuid4(),
    )


def test_unknown_sites_and_subjects_are_stored_but_ignored(crew):
    on_record(crew["foreman"])
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


def test_an_id_beyond_64_bits_is_rejected_not_a_server_error(crew):
    north, who = crew["north"], ("worker", crew["worker"].pk)
    batch = [
        event("entered", T0, site=2**63, subject=who),
        event("entered", T0, site=north, subject=("worker", 2**63)),
        event("entered", T0, site=north, subject=who),
    ]

    response = tracker().post(EVENTS_URL, {"events": batch}, format="json")

    assert response.status_code == 200
    assert response.json() == {"stored": 1, "duplicates": 0, "ignored": 0, "rejected": 2}
    assert PresenceEvent.objects.count() == 1


def test_the_ingest_rejects_malformed_events_it_is_handed(crew):
    """Without the serializer (a service caller): rejected, never a failed batch."""
    good = services.EventIn(
        event_id=uuid.uuid4(),
        site_id=crew["north"].pk,
        subject_kind="worker",
        subject_id=crew["worker"].pk,
        type="entered",
        at=T0,
    )
    bad = [
        dataclasses.replace(good, event_id=uuid.uuid4(), site_id=2**64),
        dataclasses.replace(good, event_id=uuid.uuid4(), subject_id=0),
        dataclasses.replace(good, event_id=uuid.uuid4(), subject_kind="robot"),
        dataclasses.replace(good, event_id=uuid.uuid4(), type="teleported"),
        dataclasses.replace(good, event_id=uuid.uuid4(), at=T0.replace(tzinfo=None)),
    ]

    result = services.ingest_events([*bad, good], rejected=1)

    assert result == services.IngestResult(stored=1, duplicates=0, ignored=0, rejected=6)
    assert PresenceEvent.objects.count() == 1


def test_an_unexpected_error_on_one_item_rejects_only_that_item(crew, monkeypatch):
    original = PresenceEventSerializer.to_event

    def fragile(self):
        if self.validated_data["zone_id"] == "boom":
            raise ValueError("boom")
        return original(self)

    monkeypatch.setattr(PresenceEventSerializer, "to_event", fragile)
    who = ("worker", crew["worker"].pk)
    batch = [
        event("entered", T0, site=crew["north"], subject=who, zone="boom"),
        event("entered", T0, site=crew["north"], subject=who, zone="A"),
    ]

    response = tracker().post(EVENTS_URL, {"events": batch}, format="json")

    assert response.status_code == 200
    assert response.json() == {"stored": 1, "duplicates": 0, "ignored": 0, "rejected": 1}


def test_a_database_error_still_fails_the_batch(crew, monkeypatch):
    """A 500: the tracker keeps the batch and sends it again."""

    def locked(self):
        raise OperationalError("database is locked")

    monkeypatch.setattr(PresenceEventSerializer, "to_event", locked)
    batch = [event("entered", T0, site=crew["north"], subject=("worker", crew["worker"].pk))]

    response = tracker().post(EVENTS_URL, {"events": batch}, format="json")

    assert response.status_code == 500
    assert not PresenceEvent.objects.exists()


def test_two_lefts_at_the_same_instant_replayed_add_no_session(crew):
    north, who = crew["north"], ("worker", crew["worker"].pk)
    later = T0 + datetime.timedelta(minutes=30)
    entered = event("entered", T0, site=north, subject=who)
    lefts = [
        event("left", later, site=north, subject=who, reason="timeout"),
        event("left", later, site=north, subject=who, reason="left_geofence"),
    ]
    tracker().post(EVENTS_URL, {"events": [entered, *lefts]}, format="json")
    before = sessions_of(north, crew["worker"])
    assert len(before) == 2  # the visit, then the second left alone at the same instant

    response = tracker().post(EVENTS_URL, {"events": lefts}, format="json")

    assert response.json()["duplicates"] == 2
    assert sessions_of(north, crew["worker"]) == before


# --- sessions are the fold of every event, however the events arrive ---------------------------

# (type, minutes after T0, zone, from_zone, reason): same instants, a left without its
# entered, an entered while a visit is open, a left without a reason, an open visit.
TRICKY_DAY = [
    ("entered", 0, "A", "", ""),
    ("moved_zone", 10, "B", "A", ""),
    ("left", 30, "B", "", "timeout"),
    ("left", 30, "", "", "left_geofence"),
    ("entered", 30, "C", "", ""),
    ("moved_zone", 30, "A", "C", ""),
    ("left", 60, "", "", "timeout"),
    ("entered", 90, "A", "", ""),
    ("entered", 95, "B", "", ""),
    ("left", 120, "", "", ""),
    ("moved_zone", 130, "C", "B", ""),
    ("left", 140, "", "", "timeout"),
    ("left", 140, "", "", "timeout"),
    ("entered", 200, "A", "", ""),
]


def random_day(rng: random.Random) -> list[tuple]:
    """A few events on a coarse clock, so that many share an instant."""
    day = []
    for _ in range(rng.randint(1, 12)):
        kind = rng.choice(["entered", "moved_zone", "left"])
        reason = rng.choice(["timeout", "left_geofence", ""]) if kind == "left" else ""
        day.append((kind, rng.choice(range(0, 90, 15)), rng.choice("AB "), "A", reason))
    return day


def as_events(rng: random.Random, day, site, worker) -> list[services.EventIn]:
    return [
        services.EventIn(
            event_id=uuid.UUID(int=rng.getrandbits(128), version=4),
            site_id=site.pk,
            subject_kind="worker",
            subject_id=worker.pk,
            type=kind,
            at=T0 + datetime.timedelta(minutes=minutes),
            zone=zone.strip(),
            from_zone=from_zone if kind == "moved_zone" else "",
            reason=reason,
        )
        for kind, minutes, zone, from_zone, reason in day
    ]


def stored_sessions(site, worker) -> list[tuple]:
    rows = PresenceSession.objects.filter(
        site=site, subject_kind="worker", subject_id=worker.pk
    ).order_by("started_at", "pk")
    return [
        (s.started_at, s.ended_at, s.end_reason, tuple(s.zones), s.last_event_at, s.event_count)
        for s in rows
    ]


def folded(events) -> list[tuple]:
    return [
        visit.key()
        for visit in engine.fold(
            engine.Event(str(e.event_id), e.type, e.at, e.zone, e.from_zone, e.reason)
            for e in events
        )
    ]


@pytest.mark.parametrize("seed", range(30))
def test_any_order_split_and_replay_gives_the_fold_of_all_events(crew, seed):
    rng = random.Random(seed)
    site, worker = crew["north"], crew["worker"]
    day = TRICKY_DAY if seed % 3 == 0 else random_day(rng)
    events = as_events(rng, day, site, worker)
    pending = rng.sample(events, len(events))
    sent: list[services.EventIn] = []
    while pending:
        size = rng.randint(1, 4)
        batch, pending = pending[:size], pending[size:]
        services.ingest_events(batch)
        sent.extend(batch)
        assert stored_sessions(site, worker) == folded(sent), (seed, len(sent))
        if rng.random() < 0.5:  # the tracker sends part of what it sent again
            services.ingest_events(rng.sample(sent, rng.randint(1, len(sent))))
            assert stored_sessions(site, worker) == folded(sent), (seed, "replay")

    services.ingest_events(events)

    assert stored_sessions(site, worker) == folded(events)


# --- which events count: consent, tracking switched off, an inactive company --------------------


def test_foreman_events_count_only_while_the_notice_is_accepted(crew):
    foreman, north = crew["foreman"], crew["north"]
    hour = datetime.timedelta(hours=1)
    on_record(foreman, at=T0 - hour)
    on_record(foreman, accepted=False, at=T0 + 2 * hour)  # withdrawn
    who = ("foreman", foreman.pk)
    batch = [
        event("entered", T0, site=north, subject=who),
        event("entered", T0 + 3 * hour, site=north, subject=who),
    ]

    response = tracker().post(EVENTS_URL, {"events": batch}, format="json")

    assert response.json() == {"stored": 1, "duplicates": 0, "ignored": 1, "rejected": 0}
    assert PresenceEvent.objects.get(at=T0).applied is True
    assert PresenceEvent.objects.get(at=T0 + 3 * hour).applied is False
    assert list(PresenceSession.objects.values_list("started_at", flat=True)) == [T0]


@pytest.mark.parametrize(
    ("answers", "applied"),
    [
        ([], False),  # never answered
        ([(False, VERSION)], False),  # refused
        ([(True, "2025-01-01")], True),  # an older notice accepted: still consent
        ([(True, "2027-01-01")], False),  # a notice newer than the current one
    ],
)
def test_a_foreman_event_needs_an_acceptance_before_it(crew, answers, applied):
    for accepted, version in answers:
        on_record(crew["foreman"], accepted=accepted, version=version)
    late = T0 + services.CONSENT_CLOCK_SKEW + datetime.timedelta(minutes=1)
    on_record(crew["foreman"], at=late)  # too late for it, even allowing for the clock
    batch = [event("entered", T0, site=crew["north"], subject=("foreman", crew["foreman"].pk))]

    tracker().post(EVENTS_URL, {"events": batch}, format="json")

    assert PresenceEvent.objects.get().applied is applied


@pytest.mark.parametrize(
    "server_after_phone",
    [datetime.timedelta(milliseconds=174), datetime.timedelta(minutes=9)],
)
def test_an_acceptance_counts_for_fixes_the_phone_dated_just_before_it(crew, server_after_phone):
    """The phone's first fix after accepting is dated by its own clock, to the second; the
    acceptance by the server's. A fix from the same second, or from a phone a few minutes
    slow, still counts (found by the live smoke test: the first visit was lost)."""
    on_record(crew["foreman"], at=T0 + server_after_phone)
    batch = [event("entered", T0, site=crew["north"], subject=("foreman", crew["foreman"].pk))]

    tracker().post(EVENTS_URL, {"events": batch}, format="json")

    assert PresenceEvent.objects.get().applied is True


def test_a_refusal_counts_from_the_moment_it_is_recorded(crew):
    hour = datetime.timedelta(hours=1)
    on_record(crew["foreman"], at=T0 - hour)
    on_record(crew["foreman"], accepted=False, at=T0 + datetime.timedelta(seconds=1))
    who = ("foreman", crew["foreman"].pk)
    batch = [
        event("entered", T0, site=crew["north"], subject=who),
        event("entered", T0 + datetime.timedelta(seconds=2), site=crew["south"], subject=who),
    ]

    tracker().post(EVENTS_URL, {"events": batch}, format="json")

    assert PresenceEvent.objects.get(at=T0).applied is True
    assert PresenceEvent.objects.exclude(at=T0).get().applied is False


def test_worker_events_count_only_while_tracking_is_on_for_them(crew):
    north, off = crew["north"], crew["off"]
    switched_off = WorkerFactory(tracking_enabled=False)
    gone = WorkerFactory(is_active=False)
    switched_on = WorkerFactory(tracking_enabled=True)  # on, whatever the site says
    batch = [
        event("entered", T0, site=north, subject=("worker", crew["worker"].pk)),
        event("entered", T0, site=north, subject=("worker", switched_off.pk)),
        event("entered", T0, site=north, subject=("worker", gone.pk)),
        event("entered", T0, site=off, subject=("worker", crew["worker"].pk)),
        event("entered", T0, site=off, subject=("worker", switched_on.pk)),
    ]

    response = tracker().post(EVENTS_URL, {"events": batch}, format="json")

    assert response.json() == {"stored": 2, "duplicates": 0, "ignored": 3, "rejected": 0}
    applied = PresenceEvent.objects.filter(applied=True).values_list("site_id", "subject_id")
    assert sorted(applied) == sorted([(north.pk, crew["worker"].pk), (off.pk, switched_on.pk)])


def test_events_of_an_inactive_company_are_stored_but_not_applied(crew):
    on_record(crew["foreman"])
    company = crew["north"].company
    company.is_active = False
    company.save()
    batch = [
        event("entered", T0, site=crew["north"], subject=("worker", crew["worker"].pk)),
        event("entered", T0, site=crew["north"], subject=("foreman", crew["foreman"].pk)),
    ]

    response = tracker().post(EVENTS_URL, {"events": batch}, format="json")

    assert response.json() == {"stored": 0, "duplicates": 0, "ignored": 2, "rejected": 0}
    assert set(PresenceEvent.objects.values_list("company_id", flat=True)) == {company.pk}
    assert not PresenceSession.objects.exists()


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
    on_record(crew["foreman"])
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
