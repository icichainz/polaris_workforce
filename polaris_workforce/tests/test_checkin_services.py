"""Check-in services: recording, flags, suggestion rules, self check-in drafts, kiosks."""

import datetime
import uuid
import zoneinfo
from decimal import Decimal

import pytest
from rest_framework.exceptions import PermissionDenied as ApiPermissionDenied
from rest_framework.exceptions import ValidationError

from apps.accounts.models import ForemanDevice
from apps.api.exceptions import Conflict
from apps.attendance import services as attendance_services
from apps.attendance.models import AttendanceDay, AttendanceRecord
from apps.checkin import selectors, services, tokens
from apps.checkin.models import CheckIn, KioskDevice
from apps.core.dates import company_today
from apps.core.models import AuditLog
from tests.e2e import hire, make_site
from tests.factories import CompanySettingsFactory, SiteFactory, WorkerFactory

pytestmark = pytest.mark.django_db
LOME = zoneinfo.ZoneInfo("Africa/Lome")


@pytest.fixture
def crew():
    CompanySettingsFactory(working_days=[1, 2, 3, 4, 5, 6, 7])
    crew = make_site("Riverside", phone_number="+228 90 11 22 33")
    crew.ama = hire(crew, "Ama", 5000)
    crew.kofi = hire(crew, "Kofi", 5000)
    crew.day = company_today() - datetime.timedelta(days=1)
    return crew


def at(crew, hhmm: str) -> datetime.datetime:
    hour, minute = map(int, hhmm.split(":"))
    return datetime.datetime.combine(crew.day, datetime.time(hour, minute), tzinfo=LOME)


def check(crew, worker=None, direction="in", when="07:00", *, badge=None, **kwargs):
    kwargs.setdefault("user", crew.foreman)
    kwargs.setdefault("device", None)
    return services.record_checkin(
        site=crew.site,
        direction=direction,
        at=at(crew, when),
        client_uuid=kwargs.pop("client_uuid", uuid.uuid4()),
        badge=badge,
        worker=worker if badge is None else None,
        **kwargs,
    )


def mode(value):
    from apps.core.models import CompanySettings

    settings = CompanySettings.load()
    settings.attendance_mode = value
    settings.save()


# --- recording ---------------------------------------------------------------------------


def test_badge_scan_is_counted_with_its_worker(crew):
    badge = services.issue_badge(crew.ama, crew.manager)

    result = check(crew, badge=badge.payload)

    checkin = result.instance
    assert result.created
    assert checkin.worker == crew.ama and checkin.badge == badge
    assert checkin.source == CheckIn.Source.FOREMAN_SCAN
    assert checkin.counted and checkin.on_crew and checkin.date == crew.day
    assert checkin.scanned_text == "" and selectors.flags_for(checkin) == []


def test_manual_check_in(crew):
    checkin = check(crew, crew.kofi).instance
    assert checkin.source == CheckIn.Source.MANUAL and checkin.badge is None and checkin.counted


def test_unknown_and_revoked_badges_are_recorded_but_do_not_count(crew):
    badge = services.issue_badge(crew.ama, crew.manager)
    services.revoke_badge(badge, crew.manager, "Lost")

    revoked = check(crew, badge=badge.payload).instance
    unknown = check(crew, badge="https://example.com/not-a-badge").instance
    forged = check(crew, badge=tokens.make_payload(tokens.new_public_id())).instance

    assert revoked.worker == crew.ama and not revoked.counted
    assert selectors.flags_for(revoked) == [selectors.REVOKED_BADGE]
    assert unknown.worker is None and unknown.scanned_text == "https://example.com/not-a-badge"
    assert selectors.flags_for(unknown) == [selectors.UNKNOWN_BADGE]
    assert forged.worker is None and not forged.counted
    assert selectors.day_suggestions(crew.site, crew.day).suggestions == []


def test_another_companys_badge_is_unknown(crew):
    from apps.core.models import Company
    from apps.core.tenancy import use_company

    other = Company.objects.create(name="Other Co")
    with use_company(other):
        stranger = WorkerFactory()
        badge = services.issue_badge(stranger, None)

    checkin = check(crew, badge=badge.payload).instance
    assert checkin.worker is None and checkin.badge_problem == "unknown"


def test_worker_not_on_crew_is_flagged(crew):
    outsider = WorkerFactory()
    checkin = check(crew, outsider).instance
    assert not checkin.on_crew and selectors.flags_for(checkin) == [selectors.NOT_ON_CREW]


def test_geofence_flag(crew):
    services.set_geofence(
        crew.site,
        latitude=Decimal("6.131944"),
        longitude=Decimal("1.222778"),
        radius_m=100,
        actor=crew.manager,
    )
    inside = check(
        crew, crew.ama, latitude=Decimal("6.132300"), longitude=Decimal("1.222778"), accuracy_m=8
    ).instance
    outside = check(
        crew, crew.kofi, latitude=Decimal("6.150000"), longitude=Decimal("1.222778")
    ).instance
    no_gps = check(crew, crew.ama, direction="out", when="17:00").instance

    assert inside.inside_geofence is True and inside.distance_m == 40
    assert outside.inside_geofence is False and outside.distance_m > 1900
    assert selectors.flags_for(outside) == [selectors.OUTSIDE_GEOFENCE]
    assert no_gps.inside_geofence is None and no_gps.distance_m is None


def test_duplicate_within_five_minutes(crew):
    first = check(crew, crew.ama, when="07:00").instance
    second = check(crew, crew.ama, when="07:04").instance
    later = check(crew, crew.ama, when="07:10").instance
    out = check(crew, crew.ama, direction="out", when="07:11").instance

    duplicates = selectors.duplicate_ids(selectors.day_checkins(crew.site, crew.day))
    assert duplicates == {second.pk}
    assert selectors.flags_for(second) == [selectors.DUPLICATE]
    assert first.pk not in duplicates and later.pk not in duplicates and out.pk not in duplicates


def test_clock_checks(crew):
    from django.utils import timezone

    with pytest.raises(ValidationError) as future:
        services.record_checkin(
            user=crew.foreman,
            device=None,
            site=crew.site,
            direction="in",
            at=timezone.now() + datetime.timedelta(minutes=11),
            client_uuid=uuid.uuid4(),
            worker=crew.ama,
        )
    assert "at" in future.value.detail
    with pytest.raises(ValidationError):
        services.record_checkin(
            user=crew.foreman,
            device=None,
            site=crew.site,
            direction="in",
            at=timezone.now() - datetime.timedelta(days=32),
            client_uuid=uuid.uuid4(),
            worker=crew.ama,
        )
    assert CheckIn.objects.count() == 0


# --- idempotency ----------------------------------------------------------------------------


def test_replay_returns_the_original(crew):
    key = uuid.uuid4()
    first = check(crew, crew.ama, client_uuid=key)
    again = check(crew, crew.ama, client_uuid=key)

    assert first.created and not again.created
    assert again.instance.pk == first.instance.pk
    assert CheckIn.objects.count() == 1


def test_same_uuid_for_another_check_in_is_a_conflict(crew):
    key = uuid.uuid4()
    check(crew, crew.ama, client_uuid=key)
    with pytest.raises(Conflict):
        check(crew, crew.kofi, client_uuid=key)
    with pytest.raises(Conflict):
        check(crew, crew.ama, direction="out", client_uuid=key)
    with pytest.raises(Conflict):
        check(crew, crew.ama, when="07:01", client_uuid=key)


# --- suggestions ---------------------------------------------------------------------------


def _suggestion(crew, worker):
    found = [
        s
        for s in selectors.day_suggestions(crew.site, crew.day).suggestions
        if s.worker_id == worker.pk
    ]
    return found[0] if found else None


@pytest.mark.parametrize(
    ("scans", "status", "arrival", "departure"),
    [
        ([("in", "07:15")], "present", "07:15", None),  # on the dot: not late
        ([("in", "06:50"), ("out", "17:05")], "present", "06:50", "17:05"),
        ([("in", "07:16")], "late", "07:16", None),
        ([("in", "07:05"), ("out", "11:30")], "half_day", "07:05", "11:30"),
        ([("in", "13:00"), ("out", "17:00")], "half_day", "13:00", "17:00"),
        ([("out", "17:00")], None, None, "17:00"),
        # First in and last out win, whatever the order they arrive in.
        (
            [("in", "07:30"), ("in", "07:02"), ("out", "12:30"), ("out", "16:00")],
            "present",
            "07:02",
            "16:00",
        ),
    ],
)
def test_suggestion_rules(crew, scans, status, arrival, departure):
    for direction, when in scans:
        check(crew, crew.ama, direction=direction, when=when)

    suggestion = _suggestion(crew, crew.ama)

    assert suggestion.status == status
    assert (suggestion.arrival.strftime("%H:%M") if suggestion.arrival else None) == arrival
    assert (suggestion.departure.strftime("%H:%M") if suggestion.departure else None) == departure
    assert suggestion.checkin_count == len(scans)


def test_no_check_in_no_suggestion(crew):
    check(crew, crew.ama)
    assert _suggestion(crew, crew.kofi) is None


def test_site_late_after_override_is_used(crew):
    crew.site.late_after = datetime.time(8, 0)
    crew.site.save()
    check(crew, crew.ama, when="07:45")
    assert _suggestion(crew, crew.ama).status == "present"


def test_suggestions_ignore_workers_not_on_the_crew(crew):
    check(crew, WorkerFactory())
    assert selectors.day_suggestions(crew.site, crew.day).suggestions == []


# --- attendance modes ---------------------------------------------------------------------


def _record(crew, worker):
    return AttendanceRecord.objects.filter(
        day__site=crew.site, day__date=crew.day, worker=worker
    ).first()


@pytest.mark.parametrize("value", ["foreman_roll_call", "both"])
def test_roll_call_and_both_modes_never_write_records(crew, value):
    mode(value)
    checkin = check(crew, crew.ama).instance
    assert not checkin.draft_updated
    assert not AttendanceDay.objects.exists()


def test_self_check_in_writes_the_draft_as_the_phone_user(crew):
    mode("self_check_in")

    first = check(crew, crew.ama, when="07:20").instance
    check(crew, crew.ama, direction="out", when="16:45")

    assert first.draft_updated
    day = AttendanceDay.objects.get(site=crew.site, date=crew.day)
    assert day.status == "draft" and day.foreman == crew.foreman
    record = _record(crew, crew.ama)
    assert (record.status, record.arrival, record.departure) == (
        "late",
        datetime.time(7, 20),
        datetime.time(16, 45),
    )
    entry = AuditLog.objects.filter(action="attendance.records.upsert").latest("pk")
    assert entry.actor == crew.foreman


def test_self_check_in_keeps_the_foremans_own_marks(crew):
    mode("self_check_in")
    check(crew, crew.ama, when="07:20")  # late
    record = _record(crew, crew.ama)
    # The foreman excuses Ama: present.
    attendance_services.upsert_records(
        record.day,
        [
            {
                "client_uuid": record.client_uuid,
                "worker_id": crew.ama.pk,
                "status": "present",
                "arrival": record.arrival,
                "departure": None,
                "note": "Bus breakdown",
            }
        ],
        crew.foreman,
    )

    check(crew, crew.ama, direction="out", when="16:00")

    record.refresh_from_db()
    assert record.status == "present" and record.note == "Bus breakdown"
    assert record.departure == datetime.time(16, 0)  # empty field filled


def test_self_check_in_only_out_creates_nothing(crew):
    mode("self_check_in")
    checkin = check(crew, crew.ama, direction="out", when="17:00").instance
    assert not checkin.draft_updated and _record(crew, crew.ama) is None


def test_self_check_in_never_writes_after_submit(crew):
    mode("self_check_in")
    check(crew, crew.ama, when="07:00")
    check(crew, crew.kofi, when="07:00")
    day = AttendanceDay.objects.get(site=crew.site, date=crew.day)
    attendance_services.submit_day(day, crew.foreman, uuid.uuid4())
    before = list(AttendanceRecord.objects.values_list("pk", "status", "arrival", "departure"))

    late_out = check(crew, crew.ama, direction="out", when="11:00").instance

    assert not late_out.draft_updated
    assert list(AttendanceRecord.objects.values_list("pk", "status", "arrival", "departure")) == (
        before
    )
    day.refresh_from_db()
    assert day.status == "submitted"


def test_self_check_in_skips_unknown_and_off_crew(crew):
    mode("self_check_in")
    unknown = check(crew, badge="PW1:NOPE").instance
    outsider = check(crew, WorkerFactory()).instance
    assert not unknown.draft_updated and not outsider.draft_updated
    assert not AttendanceDay.objects.exists()


# --- kiosks ---------------------------------------------------------------------------------


@pytest.fixture
def kiosk(crew):
    device = ForemanDevice.objects.create(
        user=crew.foreman, device_id="gate", token_hash=ForemanDevice.hash_token("gate-token")
    )
    services.enable_kiosk(device, crew.site, crew.manager)
    return device


def test_enable_kiosk_is_audited_and_movable(crew, kiosk):
    other = SiteFactory(manager=crew.manager)
    other.foremen.add(crew.foreman)

    services.enable_kiosk(kiosk, other, crew.manager)

    assert KioskDevice.objects.get(device=kiosk).site == other
    entries = AuditLog.objects.filter(action="checkin.kiosk.enable").order_by("pk")
    actions = list(entries.values_list("after"))
    assert actions == [({"site_id": crew.site.pk},), ({"site_id": other.pk},)]


def test_kiosk_rules(crew, kiosk):
    other = SiteFactory(manager=crew.manager)
    other.foremen.add(crew.foreman)
    badge = services.issue_badge(crew.ama, crew.manager)

    scan = services.record_checkin(
        user=crew.foreman,
        device=kiosk,
        site=crew.site,
        direction="in",
        at=at(crew, "07:00"),
        client_uuid=uuid.uuid4(),
        badge=badge.payload,
    )
    assert scan.instance.source == CheckIn.Source.KIOSK and scan.instance.device == kiosk

    with pytest.raises(ApiPermissionDenied):
        services.record_checkin(
            user=crew.foreman,
            device=kiosk,
            site=other,
            direction="in",
            at=at(crew, "07:00"),
            client_uuid=uuid.uuid4(),
            badge=badge.payload,
        )
    with pytest.raises(ValidationError):
        services.record_checkin(
            user=crew.foreman,
            device=kiosk,
            site=crew.site,
            direction="in",
            at=at(crew, "07:00"),
            client_uuid=uuid.uuid4(),
            worker=crew.ama,
        )


def test_kiosk_needs_a_foreman_of_the_site(crew):
    stranger_site = SiteFactory(manager=crew.manager)
    device = ForemanDevice.objects.create(
        user=crew.foreman, device_id="x", token_hash=ForemanDevice.hash_token("x")
    )
    with pytest.raises(ValidationError):
        services.enable_kiosk(device, stranger_site, crew.manager)
    device.revoked_at = device.created_at
    device.save()
    with pytest.raises(ValidationError):
        services.enable_kiosk(device, crew.site, crew.manager)


def test_disable_kiosk(crew, kiosk):
    services.disable_kiosk(KioskDevice.objects.get(device=kiosk), crew.manager)
    assert not KioskDevice.objects.exists()
    assert AuditLog.objects.filter(action="checkin.kiosk.disable").count() == 1


def test_geofence_set_and_clear_are_audited(crew):
    services.set_geofence(
        crew.site,
        latitude=Decimal("6.1"),
        longitude=Decimal("1.2"),
        radius_m=200,
        actor=crew.manager,
    )
    services.set_geofence(
        crew.site,
        latitude=Decimal("6.2"),
        longitude=Decimal("1.2"),
        radius_m=200,
        actor=crew.manager,
    )
    services.clear_geofence(crew.site, crew.manager)

    entries = AuditLog.objects.filter(action__startswith="checkin.geofence").order_by("pk")
    assert [e.action for e in entries] == [
        "checkin.geofence.set",
        "checkin.geofence.set",
        "checkin.geofence.clear",
    ]
    assert entries[1].before["latitude"] == "6.100000"
