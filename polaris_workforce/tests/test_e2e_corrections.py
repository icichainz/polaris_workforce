"""End to end: a foreman corrects an approved day from the phone.

The day goes back to the manager (submitted), payments are refused until it is approved
again, and the pay then follows the corrected attendance.
"""

import datetime

import pytest
from django.core.cache import cache
from django.urls import reverse
from django.utils import timezone

from apps.attendance.models import AttendanceCorrection, AttendanceDay
from apps.core.models import AuditLog
from tests.e2e import Office, Phone, hire, lines_by_worker, make_site, new_uuid, record
from tests.factories import CompanySettingsFactory

pytestmark = pytest.mark.django_db

PIN = "2468"
PHONE = "+228 90 10 20 30"
MON = datetime.date(2025, 9, 1)


@pytest.fixture(autouse=True)
def _isolated(settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path
    cache.clear()
    yield
    cache.clear()


def test_correction_after_approval_goes_back_to_the_manager():
    started = timezone.now()
    CompanySettingsFactory()
    crew = make_site("Lakeside", phone_number=PHONE, pin=PIN)
    ama, kofi = hire(crew, "Ama", 6000), hire(crew, "Kofi", 5000)
    site_id = crew.site.pk
    phone = Phone(PHONE, PIN)
    manager = Office(crew.manager)

    put = phone.put_records(site_id, MON, [record(ama, "present"), record(kofi, "half_day")])
    assert put.status_code == 200
    kofi_record = next(r for r in put.json()["records"] if r["worker_id"] == kofi.pk)
    assert phone.submit(site_id, MON, new_uuid()).status_code == 200
    day = AttendanceDay.objects.get(site=crew.site, date=MON)
    manager.approve(day.pk)

    payout = phone.payout(site_id, MON).json()
    assert payout["status"] == "open"
    lines = lines_by_worker(payout)
    assert (lines[ama.pk]["net"], lines[kofi.pk]["net"]) == (6000, 2500)
    # Ama is paid before the correction comes in.
    assert phone.pay(lines[ama.pk]["pay_line_id"], 6000, new_uuid()).status_code == 201

    # --- the foreman corrects Kofi: he worked the whole day -------------------------------
    correction_uuid = new_uuid()
    corrected = phone.correct(
        kofi_record["id"], correction_uuid, "status", "present", "Stayed for the slab pour"
    )
    assert corrected.status_code == 201, corrected.content
    replay = phone.correct(
        kofi_record["id"], correction_uuid, "status", "present", "Stayed for the slab pour"
    )
    assert replay.status_code == 200
    assert replay["Idempotent-Replayed"] == "true"
    assert AttendanceCorrection.objects.count() == 1
    assert AuditLog.objects.filter(action="attendance.record.correct").count() == 1

    day.refresh_from_db()
    assert day.status == AttendanceDay.Status.SUBMITTED
    assert phone.day(site_id, MON).json()["status"] == "submitted"

    # Payments are refused until the manager approves again.
    waiting = phone.payout(site_id, MON).json()
    assert waiting["status"] == "not_open"
    kofi_line = lines_by_worker(waiting)[kofi.pk]
    refused = phone.pay(kofi_line["pay_line_id"], kofi_line["net"], new_uuid())
    assert refused.status_code == 409
    assert refused.json()["details"] == {"status": "not_open"}

    # The Approvals screen shows the corrected row.
    board = manager.get(reverse("attendance:approvals"), {"day": day.pk})
    assert board.context["detail"]["can_decide"] is True
    assert "Stayed for the slab pour" in board.content.decode()
    manager.approve(day.pk)

    # --- the pay follows the corrected value ----------------------------------------------
    reopened = phone.payout(site_id, MON).json()
    assert reopened["status"] == "open"
    lines = lines_by_worker(reopened)
    assert lines[kofi.pk]["net"] == 5000
    assert lines[ama.pk]["net"] == 6000  # paid: unchanged
    assert reopened["total_to_pay"] == 11000
    stale = phone.pay(lines[kofi.pk]["pay_line_id"], 2500, new_uuid())
    assert stale.status_code == 400  # the old figure is refused
    paid = phone.pay(lines[kofi.pk]["pay_line_id"], 5000, new_uuid())
    assert paid.status_code == 201
    final = phone.payout(site_id, MON).json()
    assert (final["status"], final["paid_total"], final["left_to_pay"]) == ("paid", 11000, 0)

    # The phone learns about it through sync (since the start of the test).
    sync = phone.sync(started.strftime("%Y-%m-%dT%H:%M:%S.%fZ")).json()
    assert [c["record_id"] for c in sync["corrections"]] == [kofi_record["id"]]
    synced = next(r for r in sync["records"] if r["id"] == kofi_record["id"])
    assert (synced["status"], synced["corrected"]) == ("present", True)


def test_correction_reason_is_required_and_status_checked():
    CompanySettingsFactory()
    crew = make_site("Lakeside", phone_number=PHONE, pin=PIN)
    ama = hire(crew, "Ama", 6000)
    phone = Phone(PHONE, PIN)
    put = phone.put_records(crew.site.pk, MON, [record(ama, "present")])
    record_id = put.json()["records"][0]["id"]

    # Still a draft: edit the record directly, not with a correction.
    draft = phone.correct(record_id, new_uuid(), "status", "absent", "Mistake")
    assert draft.status_code == 409
    assert phone.submit(crew.site.pk, MON, new_uuid()).status_code == 200
    no_reason = phone.correct(record_id, new_uuid(), "status", "absent", "")
    assert no_reason.status_code == 400
    # A submitted day refuses a plain edit.
    edit = phone.put_records(crew.site.pk, MON, [record(ama, "absent")])
    assert edit.status_code == 423
