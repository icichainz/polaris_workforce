"""Attendance service layer: idempotent upserts, the day workflow, corrections."""

import datetime
import uuid
from types import SimpleNamespace

import pytest
from django.core.exceptions import PermissionDenied
from rest_framework.exceptions import ValidationError

from apps.api.exceptions import Conflict, Locked
from apps.attendance import selectors, services
from apps.attendance.models import AttendanceCorrection, AttendanceDay, AttendanceRecord
from apps.core.models import AuditLog
from tests.factories import (
    AttendanceDayFactory,
    AttendanceRecordFactory,
    CompanySettingsFactory,
    ForemanFactory,
    SiteAssignmentFactory,
    SiteFactory,
    SiteManagerFactory,
    UserFactory,
)

Status = AttendanceDay.Status
P = AttendanceRecord.Status.PRESENT
A = AttendanceRecord.Status.ABSENT
H = AttendanceRecord.Status.HALF_DAY


@pytest.fixture(autouse=True)
def _fast_passwords(settings):
    # Factories hash a password per user; the default hasher is slow on purpose.
    settings.PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]


@pytest.fixture
def crew(db):
    CompanySettingsFactory(time_zone="Africa/Lome")
    today = selectors.company_today()
    foreman = ForemanFactory()
    manager = SiteManagerFactory()
    site = SiteFactory(manager=manager, foremen=[foreman])
    workers = [
        SiteAssignmentFactory(site=site, start_date=today - datetime.timedelta(days=30)).worker
        for _ in range(3)
    ]
    return SimpleNamespace(
        site=site, foreman=foreman, manager=manager, workers=workers, today=today
    )


def marks(workers, status=P, **extra):
    return [
        {
            "client_uuid": uuid.uuid4(),
            "worker_id": worker.pk,
            "status": status,
            "arrival": datetime.time(7, 0),
            "departure": None,
            "note": "",
            **extra,
        }
        for worker in workers
    ]


def draft_day(crew, status=P):
    day = services.get_or_create_day(crew.site, crew.today, crew.foreman)
    services.upsert_records(day, marks(crew.workers, status), crew.foreman)
    return day


def submitted_day(crew):
    day = draft_day(crew)
    return services.submit_day(day, crew.foreman, uuid.uuid4())


def approved_day(crew):
    return services.approve_day(submitted_day(crew), crew.manager)


def rejected_day(crew):
    return services.reject_day(submitted_day(crew), crew.manager, "Check Kodjo")


def locked_day(crew):
    return services.lock_day(approved_day(crew), crew.manager)


MAKE = {
    Status.DRAFT: draft_day,
    Status.SUBMITTED: submitted_day,
    Status.APPROVED: approved_day,
    Status.REJECTED: rejected_day,
    Status.LOCKED: locked_day,
}


# --- The transition table --------------------------------------------------------------

ALLOWED = {
    ("not_started", "draft"),
    ("draft", "submitted"),
    ("rejected", "draft"),
    ("rejected", "submitted"),
    ("submitted", "approved"),
    ("submitted", "rejected"),
    ("approved", "submitted"),
    ("approved", "locked"),
}
STATES = ["not_started", *Status.values]


@pytest.mark.parametrize("current", STATES)
@pytest.mark.parametrize("target", Status.values)
def test_transition_table(current, target):
    assert services.can_transition(current, target) is ((current, target) in ALLOWED)


# --- Allowed transitions, through the services -----------------------------------------


def test_not_started_to_draft_creates_the_day(crew):
    day = services.get_or_create_day(crew.site, crew.today, crew.foreman)

    assert day.status == Status.DRAFT
    assert day.foreman == crew.foreman
    assert AuditLog.objects.filter(action="attendance.day.create", object_id=str(day.pk)).exists()
    assert services.get_or_create_day(crew.site, crew.today, crew.foreman) == day


def test_draft_to_submitted(crew):
    day = draft_day(crew)

    services.submit_day(day, crew.foreman, uuid.uuid4(), "Concrete late")

    day.refresh_from_db()
    assert day.status == Status.SUBMITTED
    assert day.submitted_by == crew.foreman
    assert day.submitted_at is not None
    assert day.foreman_note == "Concrete late"
    assert AuditLog.objects.filter(action="attendance.day.submit").count() == 1


def test_submitted_to_approved(crew):
    day = services.approve_day(submitted_day(crew), crew.manager)

    day.refresh_from_db()
    assert day.status == Status.APPROVED
    assert day.approved_by == crew.manager
    assert AuditLog.objects.filter(action="attendance.day.approve").count() == 1


def test_submitted_to_rejected_keeps_the_note(crew):
    day = services.reject_day(submitted_day(crew), crew.manager, "  Kodjo was on site  ")

    day.refresh_from_db()
    assert day.status == Status.REJECTED
    assert day.rejection_note == "Kodjo was on site"
    assert AuditLog.objects.get(action="attendance.day.reject").reason == "Kodjo was on site"


def test_rejected_to_draft_on_edit(crew):
    day = rejected_day(crew)

    services.upsert_records(day, marks(crew.workers[:1], A), crew.foreman)

    day.refresh_from_db()
    assert day.status == Status.DRAFT


def test_rejected_to_submitted(crew):
    day = rejected_day(crew)

    services.submit_day(day, crew.foreman, uuid.uuid4())

    day.refresh_from_db()
    assert day.status == Status.SUBMITTED
    assert day.rejection_note == ""


def test_approved_to_submitted_when_a_foreman_corrects(crew):
    day = approved_day(crew)
    record = day.records.first()

    services.correct_record(record, "status", H, "Left at noon", crew.foreman, uuid.uuid4())

    day.refresh_from_db()
    assert day.status == Status.SUBMITTED
    assert day.approved_at is None and day.approved_by is None


def test_approved_stays_approved_when_the_manager_corrects(crew):
    day = approved_day(crew)
    record = day.records.first()

    services.correct_record(record, "status", H, "Left at noon", crew.manager, uuid.uuid4())

    day.refresh_from_db()
    assert day.status == Status.APPROVED


def test_approved_to_locked(crew):
    day = services.lock_day(approved_day(crew))

    day.refresh_from_db()
    assert day.status == Status.LOCKED
    assert AuditLog.objects.filter(action="attendance.day.lock").count() == 1
    # Locking again does nothing.
    services.lock_day(day)
    assert AuditLog.objects.filter(action="attendance.day.lock").count() == 1


# --- Forbidden transitions -------------------------------------------------------------


def _expect(status):
    return Locked if status == Status.LOCKED else Conflict


@pytest.mark.parametrize("status", [Status.DRAFT, Status.APPROVED, Status.REJECTED, Status.LOCKED])
def test_approve_only_from_submitted(crew, status):
    day = MAKE[status](crew)
    with pytest.raises(_expect(status)):
        services.approve_day(day, crew.manager)
    day.refresh_from_db()
    assert day.status == status


@pytest.mark.parametrize("status", [Status.DRAFT, Status.APPROVED, Status.REJECTED, Status.LOCKED])
def test_reject_only_from_submitted(crew, status):
    day = MAKE[status](crew)
    with pytest.raises(_expect(status)):
        services.reject_day(day, crew.manager, "Check again")
    day.refresh_from_db()
    assert day.status == status


@pytest.mark.parametrize("status", [Status.DRAFT, Status.SUBMITTED, Status.REJECTED])
def test_lock_only_from_approved(crew, status):
    day = MAKE[status](crew)
    with pytest.raises(Conflict):
        services.lock_day(day)
    day.refresh_from_db()
    assert day.status == status


@pytest.mark.parametrize("status", [Status.SUBMITTED, Status.APPROVED, Status.LOCKED])
def test_submit_again_with_another_client_uuid(crew, status):
    day = MAKE[status](crew)
    with pytest.raises(_expect(status)):
        services.submit_day(day, crew.foreman, uuid.uuid4())
    day.refresh_from_db()
    assert day.status == status


@pytest.mark.parametrize("status", [Status.SUBMITTED, Status.APPROVED, Status.LOCKED])
def test_record_edits_are_locked_after_submission(crew, status):
    day = MAKE[status](crew)

    with pytest.raises(Locked) as exc:
        services.upsert_records(day, marks(crew.workers[:1], A), crew.foreman)

    assert exc.value.details == {"status": status}
    assert not day.records.filter(status=A).exists()


@pytest.mark.parametrize("status", [Status.DRAFT, Status.REJECTED, Status.LOCKED])
def test_corrections_only_after_submission_and_before_lock(crew, status):
    day = MAKE[status](crew)
    record = day.records.first()

    with pytest.raises(_expect(status)):
        services.correct_record(record, "status", A, "Left early", crew.foreman, uuid.uuid4())

    assert not AttendanceCorrection.objects.exists()


# --- upsert_records --------------------------------------------------------------------


def test_replaying_the_same_upsert_changes_nothing(crew):
    day = services.get_or_create_day(crew.site, crew.today, crew.foreman)
    batch = marks(crew.workers)

    first = services.upsert_records(day, batch, crew.foreman)
    stamps = dict(AttendanceRecord.objects.values_list("pk", "updated_at"))
    logs = AuditLog.objects.count()
    second = services.upsert_records(day, batch, crew.foreman)

    assert [(r.pk, r.client_uuid) for r in first] == [(r.pk, r.client_uuid) for r in second]
    assert AttendanceRecord.objects.count() == 3
    assert dict(AttendanceRecord.objects.values_list("pk", "updated_at")) == stamps
    assert AuditLog.objects.count() == logs


def test_replay_on_a_submitted_day_is_a_no_op(crew):
    day = services.get_or_create_day(crew.site, crew.today, crew.foreman)
    batch = marks(crew.workers)
    services.upsert_records(day, batch, crew.foreman)
    services.submit_day(day, crew.foreman, uuid.uuid4())

    records = services.upsert_records(day, batch, crew.foreman)

    assert len(records) == 3


def test_upsert_writes_only_changed_fields_and_audits(crew):
    day = services.get_or_create_day(crew.site, crew.today, crew.foreman)
    batch = marks(crew.workers)
    services.upsert_records(day, batch, crew.foreman)

    batch[0] = {**batch[0], "status": H, "departure": datetime.time(12, 0)}
    services.upsert_records(day, batch, crew.foreman)

    record = AttendanceRecord.objects.get(worker=crew.workers[0])
    assert record.status == H and record.departure == datetime.time(12, 0)
    entry = AuditLog.objects.filter(action="attendance.records.upsert").first()
    worker_key = str(crew.workers[0].pk)
    assert entry.before["records"] == {worker_key: {"status": P, "departure": ""}}
    assert entry.after["records"][worker_key]["status"] == H


def test_the_servers_client_uuid_wins_for_an_existing_worker(crew):
    day = draft_day(crew)
    original = AttendanceRecord.objects.get(worker=crew.workers[0])

    records = services.upsert_records(day, marks(crew.workers[:1], A), crew.foreman)

    assert records[0].pk == original.pk
    assert records[0].client_uuid == original.client_uuid
    assert AttendanceRecord.objects.get(pk=original.pk).status == A
    assert AttendanceRecord.objects.count() == 3


def test_a_client_uuid_of_another_record_is_a_conflict(crew):
    day = draft_day(crew)
    other = AttendanceRecordFactory()  # another site and day

    batch = marks(crew.workers[:1], A)
    batch[0]["client_uuid"] = other.client_uuid
    with pytest.raises(Conflict):
        services.upsert_records(day, batch, crew.foreman)


def test_a_worker_off_the_crew_is_rejected(crew):
    day = services.get_or_create_day(crew.site, crew.today, crew.foreman)
    stranger = SiteAssignmentFactory().worker  # assigned to another site

    with pytest.raises(ValidationError) as exc:
        services.upsert_records(day, marks([crew.workers[0], stranger]), crew.foreman)

    assert exc.value.detail["records"][0] == {}
    assert "worker_id" in exc.value.detail["records"][1]
    assert not AttendanceRecord.objects.exists()


def test_an_ended_assignment_is_not_on_the_crew(crew):
    worker = crew.workers[0]
    worker.assignments.update(end_date=crew.today - datetime.timedelta(days=1))

    assert worker.pk not in selectors.expected_worker_ids(crew.site, crew.today)


def test_no_roll_call_for_a_future_day(crew):
    with pytest.raises(ValidationError):
        services.get_or_create_day(crew.site, crew.today + datetime.timedelta(days=1), crew.foreman)


def test_foreman_of_another_site_cannot_write(crew):
    day = services.get_or_create_day(crew.site, crew.today, crew.foreman)

    with pytest.raises(PermissionDenied):
        services.upsert_records(day, marks(crew.workers), ForemanFactory())


# --- submit_day ------------------------------------------------------------------------


def test_submit_with_unmarked_workers_is_a_conflict(crew):
    day = services.get_or_create_day(crew.site, crew.today, crew.foreman)
    services.upsert_records(day, marks(crew.workers[:1]), crew.foreman)

    with pytest.raises(Conflict) as exc:
        services.submit_day(day, crew.foreman, uuid.uuid4())

    assert exc.value.details["unmarked_worker_ids"] == sorted(w.pk for w in crew.workers[1:])
    day.refresh_from_db()
    assert day.status == Status.DRAFT


def test_submit_is_idempotent(crew):
    day = draft_day(crew)
    client_uuid = uuid.uuid4()

    services.submit_day(day, crew.foreman, client_uuid)
    services.submit_day(day, crew.foreman, client_uuid)
    services.approve_day(day, crew.manager)
    again = services.submit_day(day, crew.foreman, client_uuid)

    assert again.status == Status.APPROVED
    assert AuditLog.objects.filter(action="attendance.day.submit").count() == 1


def test_submit_client_uuid_of_another_day_is_a_conflict(crew):
    other = AttendanceDayFactory()
    day = draft_day(crew)

    with pytest.raises(Conflict):
        services.submit_day(day, crew.foreman, other.client_uuid)


# --- correct_record --------------------------------------------------------------------


def test_correction_changes_the_record_and_keeps_the_history(crew):
    day = submitted_day(crew)
    record = day.records.get(worker=crew.workers[0])

    result = services.correct_record(
        record, "arrival", "07:20", "Gate log says 07:20", crew.foreman, uuid.uuid4()
    )

    assert result.created
    correction = result.instance
    assert (correction.old_value, correction.new_value) == ("07:00", "07:20")
    record.refresh_from_db()
    assert record.arrival == datetime.time(7, 20)
    assert record.corrected
    entry = AuditLog.objects.get(action="attendance.record.correct")
    assert entry.reason == "Gate log says 07:20"
    assert entry.before["arrival"] == "07:00" and entry.after["arrival"] == "07:20"


@pytest.mark.parametrize("reason", ["", "   ", None])
def test_correction_without_reason_is_rejected(crew, reason):
    record = submitted_day(crew).records.first()

    with pytest.raises(ValidationError) as exc:
        services.correct_record(record, "status", A, reason, crew.foreman, uuid.uuid4())

    assert "reason" in exc.value.detail
    record.refresh_from_db()
    assert record.status == P and not record.corrected


def test_correction_after_lock_is_rejected(crew):
    record = locked_day(crew).records.first()

    with pytest.raises(Locked):
        services.correct_record(record, "status", A, "Left early", crew.manager, uuid.uuid4())


def test_correction_replay_returns_the_original(crew):
    day = submitted_day(crew)
    record = day.records.first()
    client_uuid = uuid.uuid4()

    first = services.correct_record(record, "status", A, "Went home", crew.foreman, client_uuid)
    second = services.correct_record(record, "status", A, "Went home", crew.foreman, client_uuid)

    assert not second.created
    assert second.instance.pk == first.instance.pk
    assert AttendanceCorrection.objects.count() == 1
    other = day.records.exclude(pk=record.pk).first()
    with pytest.raises(Conflict):
        services.correct_record(other, "status", A, "Went home", crew.foreman, client_uuid)


@pytest.mark.parametrize(
    ("field", "value"), [("status", "sick"), ("arrival", "7h20"), ("status", P)]
)
def test_correction_with_a_bad_or_same_value(crew, field, value):
    record = submitted_day(crew).records.first()

    with pytest.raises(ValidationError):
        services.correct_record(record, field, value, "Typo", crew.foreman, uuid.uuid4())


# --- approve / reject permissions ------------------------------------------------------


@pytest.mark.parametrize("who", ["foreman", "other_manager", "accountant"])
def test_only_managers_of_the_site_approve_or_reject(crew, who):
    day = submitted_day(crew)
    user = {
        "foreman": crew.foreman,
        "other_manager": SiteManagerFactory(),
        "accountant": UserFactory(role="accountant"),
    }[who]

    with pytest.raises(PermissionDenied):
        services.approve_day(day, user)
    with pytest.raises(PermissionDenied):
        services.reject_day(day, user, "No")


def test_reject_needs_a_note(crew):
    day = submitted_day(crew)

    with pytest.raises(ValidationError):
        services.reject_day(day, crew.manager, "  ")


# --- payout_status ---------------------------------------------------------------------


def test_payout_status_follows_the_cash_sheet(crew):
    from apps.payroll import services as payroll_services
    from apps.payroll.models import PayPeriod

    day = submitted_day(crew)
    assert selectors.payout_status(day) == "not_open"

    # Approving refreshes pay: payroll opens the day's period and the phone sees its sheet.
    services.approve_day(day, crew.manager)
    assert PayPeriod.objects.filter(site=crew.site, end_date=day.date).exists()
    status = selectors.payout_status(day)
    assert status in ("open", "paid")
    assert status == payroll_services.sheet_status(crew.site, day.date)

    # Back to submitted (e.g. a foreman's correction): the payout closes again.
    day.status = AttendanceDay.Status.SUBMITTED
    day.save(update_fields=["status"])
    assert selectors.payout_status(day) == "not_open"
