"""End to end: a foreman corrects an approved day from the phone.

The day goes back to the manager (submitted), payments are refused until it is approved
again, and the pay then follows the corrected attendance. A worker already paid for the
corrected day keeps that pay; the difference is settled on their next pay (owner
decision M3).
"""

import datetime
import re
import zlib

import pytest
from django.core.cache import cache
from django.urls import reverse
from django.utils import timezone

from apps.attendance.models import AttendanceCorrection, AttendanceDay
from apps.core.models import AuditLog
from apps.payroll.models import PayAdjustment, PayLine, PayPeriod
from tests.e2e import Office, Phone, hire, lines_by_worker, make_site, new_uuid, record
from tests.factories import CompanySettingsFactory

pytestmark = pytest.mark.django_db

PIN = "2468"
PHONE = "+228 90 10 20 30"
MON = datetime.date(2025, 9, 1)
TUE = MON + datetime.timedelta(days=1)


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


def _pdf_text(data: bytes) -> str:
    streams = re.findall(rb"stream\n(.*?)\nendstream", data, re.S)
    return b"".join(zlib.decompress(s) for s in streams).decode("cp1252")


def _roll_call(phone, manager, site_id, date, marks):
    put = phone.put_records(site_id, date, [record(w, status) for w, status in marks])
    assert put.status_code == 200, put.content
    assert phone.submit(site_id, date, new_uuid()).status_code == 200
    day = AttendanceDay.objects.get(site_id=site_id, date=date)
    manager.approve(day.pk)
    return day, {r["worker_id"]: r for r in put.json()["records"]}


def test_correction_after_payment_is_recovered_on_the_next_pay():
    CompanySettingsFactory()
    crew = make_site("Lakeside", phone_number=PHONE, pin=PIN)
    ama, kofi = hire(crew, "Ama", 6000), hire(crew, "Kofi", 5000)
    site_id = crew.site.pk
    phone = Phone(PHONE, PIN)
    manager = Office(crew.manager)

    # --- Monday: both present, both paid ---------------------------------------------------
    monday, records = _roll_call(
        phone, manager, site_id, MON, [(ama, "present"), (kofi, "present")]
    )
    lines = lines_by_worker(phone.payout(site_id, MON).json())
    for worker in (ama, kofi):
        line = lines[worker.pk]
        assert phone.pay(line["pay_line_id"], line["net"], new_uuid()).status_code == 201
    ama_monday = lines[ama.pk]["pay_line_id"]

    # --- the foreman corrects Ama: she went home before work started ----------------------
    corrected = phone.correct(
        records[ama.pk]["id"], new_uuid(), "status", "absent", "Went home before work"
    )
    assert corrected.status_code == 201, corrected.content
    # Nothing moves before the manager approves the corrected day.
    assert not PayAdjustment.objects.exists()
    board = manager.get(reverse("attendance:approvals"), {"day": monday.pk})
    html = board.content.decode()
    assert (
        "Already paid for this day: the difference of 6 000 XOF will be recovered on the "
        "next pay." in html
    )
    assert "1 worker was paid for this day before the correction" in html
    manager.approve(monday.pk)

    adjustment = PayAdjustment.objects.get()
    assert (adjustment.worker, adjustment.site_id, adjustment.amount) == (ama, site_id, -6000)
    assert (adjustment.currency, adjustment.dates) == ("XOF", ["2025-09-01"])
    assert adjustment.source_line_id == ama_monday
    assert adjustment.record_id == records[ama.pk]["id"]
    assert (adjustment.created_by, adjustment.reason) == (crew.manager, "Went home before work")
    assert adjustment.applied_amount == 0

    # The paid line stays as paid; reading the sheets again changes nothing.
    paid_line = PayLine.objects.get(pk=ama_monday)
    assert (paid_line.gross, paid_line.net, paid_line.adjustment) == (6000, 6000, 0)
    entries = AuditLog.objects.count()
    assert phone.payout(site_id, MON).json()["status"] == "paid"
    sheet = manager.get(reverse("payroll:cash_sheet", args=[site_id, MON.isoformat()]))
    assert (
        "Already paid for this day: the difference of 6 000 XOF will be recovered"
        in sheet.content.decode()
    )
    assert AuditLog.objects.count() == entries
    assert PayAdjustment.objects.get().amount == -6000

    # --- Tuesday: Ama works again; the 6 000 is taken back from this pay ------------------
    _roll_call(phone, manager, site_id, TUE, [(ama, "present"), (kofi, "present")])
    tuesday = phone.payout(site_id, TUE).json()
    lines = lines_by_worker(tuesday)
    assert (lines[ama.pk]["gross"], lines[ama.pk]["advance_deduction"]) == (6000, 0)
    assert lines[ama.pk]["net"] == 0  # the contract's fields: only `net` carries it
    assert lines[ama.pk]["payment"] is None
    assert (tuesday["total_to_pay"], tuesday["line_count"], tuesday["paid_count"]) == (
        5000,
        2,
        1,  # Ama: nothing to hand over
    )
    ama_tuesday = lines[ama.pk]["pay_line_id"]
    adjustment.refresh_from_db()
    assert (adjustment.applied_amount, adjustment.applied_to_id) == (-6000, ama_tuesday)
    assert PayLine.objects.get(pk=ama_tuesday).adjustment == -6000

    office = manager.get(reverse("payroll:cash_sheet", args=[site_id, TUE.isoformat()]))
    html = office.content.decode()
    assert "Adjustment" in html
    assert "Correction of 1 Sep" in html
    assert "− 6 000" in html
    payslip = manager.get(reverse("payroll:payslip", args=[ama_tuesday]))
    text = _pdf_text(payslip.content)
    assert "Correction of 1 Sep" in text
    assert "– 6 000 XOF" in text  # the PDF writes the minus sign as an en dash

    # Kofi is paid; the manager closes the Tuesday sheet (their own site, decision M1).
    kofi_line = lines[kofi.pk]
    assert phone.pay(kofi_line["pay_line_id"], 5000, new_uuid()).status_code == 201
    assert phone.payout(site_id, TUE).json()["status"] == "paid"
    assert manager.lock_sheet(site_id, TUE).status_code == 302
    assert PayPeriod.objects.get(site_id=site_id, end_date=TUE).is_locked
    monday_sheet = manager.get(reverse("payroll:cash_sheet", args=[site_id, MON.isoformat()]))
    assert (
        "Corrected after payment: the difference of − 6 000 XOF is settled on the pay of Tue "
        "2 Sep." in monday_sheet.content.decode()
    )
    monday_slip = _pdf_text(manager.get(reverse("payroll:payslip", args=[ama_monday])).content)
    assert "Corrected after payment" in monday_slip

    # Audit: created once by the manager's approval, applied once; nobody who only looked.
    adjustment_rows = AuditLog.objects.filter(action__contains="adjustment")
    assert [(e.action, e.actor) for e in adjustment_rows.order_by("pk")] == [
        ("payroll.adjustment.create", crew.manager),
        ("payroll.adjustment.apply", crew.manager),
    ]
