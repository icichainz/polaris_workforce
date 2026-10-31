"""Site services: crew on a date, assignment rules, site settings, week strip."""

import datetime

import pytest
from django.core.exceptions import ValidationError

from apps.attendance.models import AttendanceDay, AttendanceRecord
from apps.core.choices import PayFrequency
from apps.core.models import AuditLog, CompanySettings
from apps.worksites import services
from apps.worksites.models import Site, SiteAssignment
from tests.factories import (
    AdminUserFactory,
    AttendanceDayFactory,
    AttendanceRecordFactory,
    SiteAssignmentFactory,
    SiteFactory,
    WorkerFactory,
)

pytestmark = pytest.mark.django_db

D = datetime.date


# --- crew -------------------------------------------------------------------------------


def test_crew_respects_assignment_dates_and_inactive_workers():
    site = SiteFactory()
    always = SiteAssignmentFactory(site=site, start_date=D(2026, 3, 1)).worker
    starts_later = SiteAssignmentFactory(site=site, start_date=D(2026, 11, 2)).worker
    left = SiteAssignmentFactory(
        site=site, start_date=D(2026, 3, 1), end_date=D(2026, 10, 30)
    ).worker
    last_day = SiteAssignmentFactory(
        site=site, start_date=D(2026, 3, 1), end_date=D(2026, 10, 31)
    ).worker
    inactive = WorkerFactory(is_active=False)
    SiteAssignmentFactory(site=site, worker=inactive, start_date=D(2026, 3, 1))
    elsewhere = SiteAssignmentFactory(start_date=D(2026, 3, 1)).worker

    crew = set(services.crew_for(site, D(2026, 10, 31)))

    assert crew == {always, last_day}
    assert starts_later in set(services.crew_for(site, D(2026, 11, 2)))
    assert left in set(services.crew_for(site, D(2026, 10, 30)))
    assert inactive not in crew and elsewhere not in crew


def test_crew_is_sorted_by_last_name():
    site = SiteFactory()
    for last in ["Mensah", "Agbeko", "Atsu"]:
        SiteAssignmentFactory(site=site, worker=WorkerFactory(last_name=last))

    names = [w.last_name for w in services.crew_for(site, D(2026, 10, 31))]
    assert names == ["Agbeko", "Atsu", "Mensah"]


# --- assignments ------------------------------------------------------------------------


def test_assign_closes_the_previous_open_assignment():
    actor = AdminUserFactory()
    first, second = SiteFactory(), SiteFactory()
    worker = WorkerFactory()
    old = services.assign(worker, first, D(2026, 3, 1), actor=actor)

    new = services.assign(worker, second, D(2026, 11, 2), actor=actor)

    old.refresh_from_db()
    assert old.end_date == D(2026, 11, 1)
    assert (new.site, new.start_date, new.end_date) == (second, D(2026, 11, 2), None)
    assert worker in services.crew_for(first, D(2026, 11, 1))
    assert worker not in services.crew_for(first, D(2026, 11, 2))
    assert worker in services.crew_for(second, D(2026, 11, 2))
    actions = list(AuditLog.objects.order_by("id").values_list("action", flat=True))
    assert actions == [
        "worksites.assignment_create",
        "worksites.assignment_close",
        "worksites.assignment_create",
    ]
    assert AuditLog.objects.filter(actor=actor).count() == 3


def test_assign_to_the_same_site_keeps_the_open_assignment():
    site = SiteFactory()
    worker = WorkerFactory()
    first = services.assign(worker, site, D(2026, 3, 1))

    again = services.assign(worker, site, D(2026, 6, 1))

    assert again.pk == first.pk
    assert SiteAssignment.objects.filter(worker=worker).count() == 1


def test_assign_replaces_an_open_assignment_that_had_not_started():
    a, b = SiteFactory(), SiteFactory()
    worker = WorkerFactory()
    services.assign(worker, a, D(2026, 11, 5))

    services.assign(worker, b, D(2026, 11, 2))

    assert list(worker.assignments.values_list("site", flat=True)) == [b.pk]
    assert AuditLog.objects.filter(action="worksites.assignment_remove").exists()


def test_assign_refuses_overlap_with_a_closed_assignment_and_bad_dates():
    a, b = SiteFactory(), SiteFactory()
    worker = WorkerFactory()
    SiteAssignmentFactory(worker=worker, site=a, start_date=D(2026, 3, 1), end_date=D(2026, 12, 31))

    with pytest.raises(ValidationError):
        services.assign(worker, b, D(2026, 11, 2))
    with pytest.raises(ValidationError):
        services.assign(worker, b, D(2027, 2, 1), D(2027, 1, 1))
    services.assign(worker, b, D(2027, 1, 1))  # after the end: fine


def test_end_assignment_sets_the_end_date_and_audits():
    assignment = SiteAssignmentFactory(start_date=D(2026, 3, 1))

    services.end_assignment(assignment, D(2026, 10, 31))

    assignment.refresh_from_db()
    assert assignment.end_date == D(2026, 10, 31)
    entry = AuditLog.objects.get(action="worksites.assignment_close")
    assert entry.before["end_date"] is None and entry.after["end_date"] == "2026-10-31"
    with pytest.raises(ValidationError):
        services.end_assignment(assignment, D(2026, 1, 1))


# --- settings ---------------------------------------------------------------------------


def test_update_site_settings_overrides_and_inherits_with_audit():
    company = CompanySettings.load()
    site = SiteFactory()
    assert services.site_setting(site, "pay_frequency", company).overridden is False

    services.update_site_settings(
        site, {"pay_frequency": PayFrequency.WEEKLY, "work_start": datetime.time(6, 30)}
    )
    site.refresh_from_db()
    resolved = services.site_setting(site, "pay_frequency", company)
    assert (resolved.value, resolved.overridden) == (PayFrequency.WEEKLY, True)
    entry = AuditLog.objects.get(action="worksites.settings_update")
    assert entry.before["pay_frequency"] is None
    assert entry.after["pay_frequency"] == "weekly"
    assert entry.after["work_start"] == "06:30:00"

    services.update_site_settings(site, {"pay_frequency": None})
    site.refresh_from_db()
    assert services.site_setting(site, "pay_frequency", company).value == company.pay_frequency

    with pytest.raises(KeyError):
        services.update_site_settings(site, {"currency": "EUR"})


# --- site list figures ------------------------------------------------------------------


def test_annotate_today_counts_assigned_and_present():
    site = SiteFactory()
    today = D(2026, 10, 31)
    workers = [SiteAssignmentFactory(site=site).worker for _ in range(4)]
    SiteAssignmentFactory(site=site, worker=WorkerFactory(is_active=False))
    day = AttendanceDayFactory(site=site, date=today)
    statuses = ["present", "late", "half_day", "absent"]
    for worker, status in zip(workers, statuses, strict=True):
        AttendanceRecordFactory(day=day, worker=worker, status=status)

    annotated = services.annotate_today(Site.objects.filter(pk=site.pk), today).get()

    assert annotated.assigned_count == 4
    assert annotated.present_count == 3


def test_week_strip_uses_working_days_and_day_status():
    company = CompanySettings.load()
    site = SiteFactory()
    worker = SiteAssignmentFactory(site=site).worker
    friday = D(2026, 10, 30)
    day = AttendanceDayFactory(site=site, date=D(2026, 10, 26), status=AttendanceDay.Status.LOCKED)
    AttendanceRecordFactory(day=day, worker=worker, status=AttendanceRecord.Status.PRESENT)

    week = services.week_strip(site, friday, company)

    assert [d.date.isoweekday() for d in week] == company.working_days
    assert (week[0].status, week[0].present, week[0].crew) == ("locked", 1, 1)
    assert week[1].status == "not_started"
    assert week[4].is_today
    assert week[5].status == "upcoming"
