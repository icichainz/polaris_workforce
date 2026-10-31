"""Payroll services: periods from attendance, status flow, payments, locking, advances.

Dates are in September 2025 (1 Sep 2025 is a Monday), safely in the past, so a period's
pay day has always come.
"""

import datetime
import random
import uuid

import pytest
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile

from apps.api.exceptions import Conflict, Locked
from apps.attendance.models import AttendanceDay, AttendanceRecord
from apps.core.models import AuditLog
from apps.payroll import services
from apps.payroll.models import Advance, AdvanceRepayment, PayLine, Payment, PayPeriod
from tests.factories import (
    AdvanceFactory,
    AttendanceDayFactory,
    AttendanceRecordFactory,
    CompanySettingsFactory,
    ForemanFactory,
    PayRateFactory,
    SiteAssignmentFactory,
    SiteFactory,
    UserFactory,
    WorkerFactory,
)

MON = datetime.date(2025, 9, 1)
TUE, WED, THU, FRI, SAT = (MON + datetime.timedelta(days=i) for i in range(1, 6))
APPROVED = AttendanceDay.Status.APPROVED
SUBMITTED = AttendanceDay.Status.SUBMITTED


# --- helpers (also used by the API and view tests) ----------------------------------------


def photo(name="proof.jpg", size=1024):
    return SimpleUploadedFile(name, b"\xff\xd8\xff" + b"0" * size, content_type="image/jpeg")


def setup_site(**site_kwargs):
    """A site with a manager and a foreman."""
    foreman = ForemanFactory()
    site = SiteFactory(started_on=datetime.date(2025, 1, 1), **site_kwargs)
    site.foremen.add(foreman)
    return site, site.manager, foreman


def hire(site, rate=6000, pay_type="daily", **worker_kwargs):
    worker = WorkerFactory(**worker_kwargs)
    PayRateFactory(
        worker=worker, amount=rate, pay_type=pay_type, effective_from=datetime.date(2025, 1, 1)
    )
    SiteAssignmentFactory(worker=worker, site=site, start_date=datetime.date(2025, 1, 1))
    return worker


def roll_call(site, date, marks, status=APPROVED, foreman=None):
    """``marks``: {worker: status} or {worker: (status, arrival, departure)}."""
    day = AttendanceDayFactory(
        site=site, date=date, status=status, foreman=foreman or site.foremen.first()
    )
    for worker, mark in marks.items():
        if isinstance(mark, str):
            mark = (mark, None, None)
        AttendanceRecordFactory(
            day=day, worker=worker, status=mark[0], arrival=mark[1], departure=mark[2]
        )
    return day


def sheet_for(site, date):
    return services.cash_sheet(site, date)


def line_of(site, date, worker):
    return PayLine.objects.get(period__site=site, period__end_date=date, worker=worker)


@pytest.fixture
def company(db):
    return CompanySettingsFactory()


# --- periods and lines --------------------------------------------------------------------


def test_daily_sheet_from_an_approved_roll_call(company):
    site, _manager, _foreman = setup_site()
    yao, afi, kodjo = hire(site, 6000), hire(site, 3500), hire(site, 5000)
    roll_call(site, MON, {yao: "present", afi: "half_day", kodjo: "absent"})

    sheet = sheet_for(site, MON)

    assert [p.start_date for p in sheet.periods] == [MON]
    assert sheet.periods[0].status == PayPeriod.Status.READY
    assert sheet.status == services.SHEET_OPEN
    nets = {sl.worker: sl.line.net for sl in sheet.lines}
    assert nets == {yao: 6000, afi: 1750}  # the absent worker is not on the sheet
    assert sheet.absent == [kodjo]
    assert sheet.reconciliation.total_to_pay == 7750
    assert line_of(site, MON, afi).days[0]["status"] == "half_day"


def test_submitted_roll_call_is_a_preview(company):
    site, _m, _f = setup_site()
    yao = hire(site)
    roll_call(site, MON, {yao: "present"}, status=SUBMITTED)

    sheet = sheet_for(site, MON)

    assert sheet.periods[0].status == PayPeriod.Status.OPEN
    assert sheet.status == services.SHEET_NOT_OPEN
    assert sheet.reconciliation.total_to_pay == 6000  # preview figures


def test_refreshing_twice_changes_nothing(company):
    site, _m, _f = setup_site()
    roll_call(site, MON, {hire(site): "present", hire(site): "late"})
    sheet_for(site, MON)
    entries = AuditLog.objects.count()
    updated = list(PayLine.objects.values_list("updated_at", flat=True))

    sheet_for(site, MON)
    services.sync_site(site, MON, SAT)

    assert AuditLog.objects.count() == entries
    assert list(PayLine.objects.values_list("updated_at", flat=True)) == updated


def test_refresh_and_status_changes_are_audited(company):
    site, _m, _f = setup_site()
    roll_call(site, MON, {hire(site): "present"})

    sheet_for(site, MON)

    actions = set(AuditLog.objects.values_list("action", flat=True))
    assert {"payroll.period.refresh", "payroll.period.status"} <= actions


def test_correction_updates_an_unpaid_line(company):
    site, _m, _f = setup_site()
    yao = hire(site, 6000)
    day = roll_call(site, MON, {yao: "present"})
    sheet_for(site, MON)

    AttendanceRecord.objects.filter(day=day, worker=yao).update(status="half_day")
    sheet_for(site, MON)

    assert line_of(site, MON, yao).gross == 3000


def test_weekly_site_pays_on_saturday_for_the_whole_week(company):
    site, _m, _f = setup_site(pay_frequency="weekly")
    yao = hire(site, 6000)
    for date in (MON, TUE, WED, THU, FRI):
        roll_call(site, date, {yao: "present"})
    roll_call(site, SAT, {yao: "half_day"})

    sheet = sheet_for(site, SAT)

    period = sheet.periods[0]
    assert (period.start_date, period.end_date) == (MON - datetime.timedelta(days=1), SAT)
    assert period.frequency == "weekly"
    assert sheet.lines[0].line.gross == 33000
    assert str(sheet.lines[0].line.units) == "5.50"
    assert len(sheet.lines[0].line.days) == 6
    assert period.status == PayPeriod.Status.READY


def test_weekly_period_waits_for_every_day_to_be_approved(company):
    site, _m, _f = setup_site(pay_frequency="weekly")
    yao = hire(site)
    for date in (MON, TUE, WED, THU, FRI):
        roll_call(site, date, {yao: "present"})
    roll_call(site, SAT, {yao: "present"}, status=SUBMITTED)

    sheet = sheet_for(site, SAT)

    assert sheet.periods[0].status == PayPeriod.Status.OPEN
    assert sheet.status == services.SHEET_NOT_OPEN


def test_a_missing_roll_call_on_a_working_day_blocks_the_period(company):
    site, _m, _f = setup_site(pay_frequency="weekly")
    yao = hire(site)
    for date in (MON, TUE, THU, FRI, SAT):  # no roll call on Wednesday
        roll_call(site, date, {yao: "present"})

    sheet = sheet_for(site, SAT)

    assert sheet.periods[0].status == PayPeriod.Status.OPEN


def test_the_pay_day_must_have_come(company):
    site, _m, _f = setup_site(pay_frequency="weekly")
    yao = hire(site)
    roll_call(site, MON, {yao: "present"})

    periods = services.sync_site(site, MON, MON, today=MON)

    assert periods[0].end_date == SAT
    assert periods[0].status == PayPeriod.Status.OPEN


def test_worker_override_gives_a_second_period_on_the_same_site(company):
    site, _m, _f = setup_site()
    daily = hire(site)
    weekly = hire(site, pay_frequency="weekly")
    for date in (MON, TUE, WED, THU, FRI, SAT):
        roll_call(site, date, {daily: "present", weekly: "present"})

    sheet = sheet_for(site, SAT)

    assert sorted((p.end_date - p.start_date).days + 1 for p in sheet.periods) == [1, 7]
    by_worker = {sl.worker: sl.line for sl in sheet.lines}
    assert by_worker[daily].gross == 6000
    assert by_worker[weekly].gross == 36000
    assert not PayLine.objects.filter(period__end_date=MON, worker=weekly).exists()


@pytest.mark.parametrize(
    ("frequency", "n", "expected"),
    [
        ("biweekly", None, 14),
        ("monthly", None, 30),
        ("every_n_days", 10, 10),
    ],
)
def test_other_frequencies(company, frequency, n, expected):
    site, _m, _f = setup_site(pay_frequency=frequency, pay_every_n_days=n)
    roll_call(site, WED, {hire(site): "present"})

    period = services.sync_site(site, WED, WED)[0]

    assert (period.end_date - period.start_date).days + 1 == expected
    assert period.start_date <= WED <= period.end_date
    assert period.frequency == frequency


def test_each_day_uses_the_rate_in_effect(company):
    site, _m, _f = setup_site(pay_frequency="weekly")
    yao = hire(site, 5000)
    PayRateFactory(worker=yao, amount=6000, effective_from=THU)
    for date in (MON, TUE, WED, THU, FRI, SAT):
        roll_call(site, date, {yao: "present"})

    assert sheet_for(site, SAT).lines[0].line.gross == 3 * 5000 + 3 * 6000


def test_hourly_worker_with_overtime_and_settings_from_the_site(company):
    site, _m, _f = setup_site(overtime_after_hours=8, overtime_multiplier=2)
    kafui = hire(site, 900, pay_type="hourly")
    roll_call(site, MON, {kafui: ("present", datetime.time(7, 0), datetime.time(18, 0))})

    line = sheet_for(site, MON).lines[0].line

    # 11 h − 1 h break = 10 h: 8 h × 900 + 2 h × 900 × 2.
    assert line.gross == 7200 + 3600
    assert str(line.hours) == "10.00"
    assert str(line.overtime_hours) == "2.00"


def test_late_rule_comes_from_the_settings(company):
    company.late_rule = "deduct_minutes"
    company.save()
    site, _m, _f = setup_site()
    delali = hire(site, 5400)
    roll_call(site, MON, {delali: ("late", datetime.time(7, 30), None)})

    assert sheet_for(site, MON).lines[0].line.gross == 5100


def test_worker_without_a_rate_is_listed_at_zero(company):
    site, _m, _f = setup_site()
    worker = WorkerFactory()
    roll_call(site, MON, {worker: "present"})

    line = sheet_for(site, MON).lines[0].line

    assert (line.units, line.gross, line.net) == (1, 0, 0)


# --- advances through pay lines -----------------------------------------------------------


def test_advance_is_deducted_once_and_repaid_to_exactly_zero(company):
    site, _m, _f = setup_site()
    yao = hire(site, 6000)
    advance = AdvanceFactory(
        worker=yao, amount=5000, balance=5000, deduction_per_pay=2000, given_on=MON
    )
    dates = (TUE, WED, THU, FRI)
    for date in dates:
        roll_call(site, date, {yao: "present"})

    for date in dates:
        sheet_for(site, date)
        sheet_for(site, date)  # a second refresh must not deduct again

    assert [line_of(site, d, yao).advance_deduction for d in dates] == [2000, 2000, 1000, 0]
    assert [line_of(site, d, yao).net for d in dates] == [4000, 4000, 5000, 6000]
    advance.refresh_from_db()
    assert advance.balance == 0
    assert advance.status == Advance.Status.REPAID
    assert sum(advance.repayments.values_list("amount", flat=True)) == 5000


def test_advance_given_on_pay_day_starts_with_the_next_pay(company):
    site, _m, _f = setup_site()
    yao = hire(site)
    AdvanceFactory(worker=yao, amount=4000, balance=4000, deduction_per_pay=2000, given_on=MON)
    roll_call(site, MON, {yao: "present"})

    assert sheet_for(site, MON).lines[0].line.advance_deduction == 0


def test_line_recomputed_smaller_gives_back_part_of_the_deduction(company):
    site, _m, _f = setup_site()
    afi = hire(site, 3000)
    advance = AdvanceFactory(
        worker=afi, amount=9000, balance=9000, deduction_per_pay=2000, given_on=MON
    )
    day = roll_call(site, TUE, {afi: "present"})
    sheet_for(site, TUE)

    AttendanceRecord.objects.filter(day=day).update(status="half_day")
    sheet_for(site, TUE)

    line = line_of(site, TUE, afi)
    assert (line.gross, line.advance_deduction, line.net) == (1500, 1500, 0)
    advance.refresh_from_db()
    assert advance.balance == 7500
    assert list(AdvanceRepayment.objects.values_list("amount", flat=True)) == [1500]


# --- payments -----------------------------------------------------------------------------


def pay(user, line, **kwargs):
    kwargs.setdefault("amount", line.net)
    kwargs.setdefault("proof_type", "photo")
    kwargs.setdefault("proof_file", photo())
    return services.record_payment(user, line, **kwargs)


@pytest.fixture
def media(settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path


def test_payment_moves_the_period_to_paying_then_paid(company, media):
    site, _manager, foreman = setup_site()
    yao, afi = hire(site), hire(site, 3500)
    roll_call(site, MON, {yao: "present", afi: "present"})
    sheet_for(site, MON)

    payment = pay(foreman, line_of(site, MON, yao))
    period = PayPeriod.objects.get()
    assert period.status == PayPeriod.Status.PAYING
    assert payment.method == "cash"
    assert payment.paid_by == foreman
    assert payment.proof_file.name.startswith("proofs/")

    pay(foreman, line_of(site, MON, afi))
    period.refresh_from_db()
    assert period.status == PayPeriod.Status.PAID
    assert sheet_for(site, MON).status == services.SHEET_PAID
    assert AuditLog.objects.filter(action="payroll.payment.create").count() == 2


@pytest.mark.parametrize("amount", [5999, 6001, 0])
def test_payment_amount_must_equal_the_net_left(company, media, amount):
    site, _m, foreman = setup_site()
    yao = hire(site)
    roll_call(site, MON, {yao: "present"})
    sheet_for(site, MON)

    with pytest.raises(ValidationError) as exc:
        pay(foreman, line_of(site, MON, yao), amount=amount)

    assert "amount" in exc.value.message_dict
    assert not Payment.objects.exists()


def test_a_paid_line_cannot_be_paid_again(company, media):
    site, _m, foreman = setup_site()
    yao = hire(site)
    roll_call(site, MON, {yao: "present"})
    sheet_for(site, MON)
    pay(foreman, line_of(site, MON, yao))

    with pytest.raises(Conflict):
        pay(foreman, line_of(site, MON, yao))


def test_payment_refused_before_approval(company, media):
    site, _m, foreman = setup_site()
    yao = hire(site)
    roll_call(site, MON, {yao: "present"}, status=SUBMITTED)
    sheet_for(site, MON)

    with pytest.raises(Conflict):
        pay(foreman, line_of(site, MON, yao))


def test_payment_refused_when_a_correction_reopened_the_day(company, media):
    site, _m, foreman = setup_site()
    yao, afi = hire(site), hire(site)
    day = roll_call(site, MON, {yao: "present", afi: "present"})
    sheet_for(site, MON)
    pay(foreman, line_of(site, MON, yao))
    AttendanceDay.objects.filter(pk=day.pk).update(status=SUBMITTED)  # sent back

    with pytest.raises(Conflict):
        pay(foreman, line_of(site, MON, afi))
    assert sheet_for(site, MON).status == services.SHEET_NOT_OPEN


@pytest.mark.parametrize(
    ("accepted", "proof_type", "file", "field"),
    [
        (["photo", "signature", "thumb_mark"], "none", None, "proof_type"),
        (["photo"], "signature", "file", "proof_type"),
        (["photo", "signature"], "photo", None, "proof_file"),
        (["photo"], "photo", "big", "proof_file"),
        (["photo"], "photo", "exe", "proof_file"),
        (["photo"], "cheque", "file", "proof_type"),
    ],
)
def test_proof_rules(company, media, accepted, proof_type, file, field):
    company.accepted_proofs = accepted
    company.save()
    site, _m, foreman = setup_site()
    yao = hire(site)
    roll_call(site, MON, {yao: "present"})
    sheet_for(site, MON)
    upload = {
        None: None,
        "file": photo(),
        "big": photo(size=5 * 1024 * 1024 + 1),
        "exe": SimpleUploadedFile("proof.exe", b"MZ"),
    }[file]

    with pytest.raises(ValidationError) as exc:
        pay(foreman, line_of(site, MON, yao), proof_type=proof_type, proof_file=upload)

    assert field in exc.value.message_dict


@pytest.mark.parametrize("accepted", [["none", "photo"], []])
def test_no_proof_when_the_settings_allow_it(company, accepted):
    company.accepted_proofs = accepted
    company.save()
    site, _m, foreman = setup_site()
    yao = hire(site)
    roll_call(site, MON, {yao: "present"})
    sheet_for(site, MON)

    payment = pay(foreman, line_of(site, MON, yao), proof_type="none", proof_file=None)

    assert payment.proof_type == "none"


def test_only_people_of_the_site_may_pay(company, media):
    site, _m, _f = setup_site()
    other_site, other_manager, other_foreman = setup_site()
    accountant = UserFactory(role="accountant")
    yao = hire(site)
    roll_call(site, MON, {yao: "present"})
    sheet_for(site, MON)

    for user in (other_manager, other_foreman, accountant):
        with pytest.raises(PermissionDenied):
            pay(user, line_of(site, MON, yao))


def test_undo_payment(company, media):
    site, manager, foreman = setup_site()
    yao = hire(site)
    roll_call(site, MON, {yao: "present"})
    sheet_for(site, MON)
    payment = pay(foreman, line_of(site, MON, yao))

    services.undo_payment(manager, payment, "Wrong worker")

    assert not Payment.objects.exists()
    assert PayPeriod.objects.get().status == PayPeriod.Status.READY
    assert AuditLog.objects.get(action="payroll.payment.undo").reason == "Wrong worker"
    with pytest.raises(PermissionDenied):
        services.undo_payment(foreman, pay(foreman, line_of(site, MON, yao)))


# --- cash issues and reconciliation -------------------------------------------------------


def test_cash_reconciliation_follows_issues_and_payments(company, media):
    site, manager, foreman = setup_site()
    workers = [hire(site, rate) for rate in (6000, 5500, 3500)]
    roll_call(site, MON, dict.fromkeys(workers, "present"))
    services.issue_cash(manager, site, MON, 10000, received_by=foreman)
    services.issue_cash(manager, site, MON, 10000)
    sheet_for(site, MON)
    pay(foreman, line_of(site, MON, workers[0]))

    rec = sheet_for(site, MON).reconciliation

    assert rec.cash_issued == 20000
    assert rec.total_to_pay == 15000
    assert rec.paid_total == 6000
    assert rec.left_to_pay == 9000
    assert rec.cash_in_hand == 14000
    assert rec.return_amount == 5000
    assert (rec.line_count, rec.paid_count) == (3, 1)


@pytest.mark.parametrize("seed", range(5))
def test_property_cash_sheet_totals_always_add_up(company, media, seed):
    rng = random.Random(seed)
    site, manager, foreman = setup_site()
    workers = [hire(site, rng.choice([3500, 4000, 6000, 7500])) for _ in range(rng.randint(1, 6))]
    initial = {}
    for worker in workers:
        if rng.random() < 0.5:
            advance = AdvanceFactory(
                worker=worker,
                amount=10000,
                balance=rng.choice([1000, 10000]),
                deduction_per_pay=2000,
                given_on=MON - datetime.timedelta(days=3),
            )
            initial[advance.pk] = advance.balance
    statuses = ["present", "half_day", "late", "absent"]
    roll_call(site, MON, {w: rng.choice(statuses) for w in workers})
    for _ in range(rng.randint(0, 3)):
        services.issue_cash(manager, site, MON, rng.randint(1, 50000))
    for sheet_line in sheet_for(site, MON).lines:
        if sheet_line.line.net and rng.random() < 0.5:
            pay(foreman, sheet_line.line)

    sheet = sheet_for(site, MON)
    rec = sheet.reconciliation

    assert rec.total_to_pay == sum(sl.line.net for sl in sheet.lines)
    assert rec.paid_total == sum(Payment.objects.values_list("amount", flat=True))
    assert rec.left_to_pay == rec.total_to_pay - rec.paid_total
    assert rec.cash_in_hand == rec.cash_issued - rec.paid_total
    assert rec.return_amount == rec.cash_issued - rec.total_to_pay
    for sl in sheet.lines:
        line = sl.line
        assert line.net == line.gross - line.advance_deduction - line.other_deductions >= 0
        repaid = sum(line.advance_repayments.values_list("amount", flat=True))
        assert repaid == line.advance_deduction
    for advance in Advance.objects.all():
        repaid = sum(advance.repayments.values_list("amount", flat=True))
        assert advance.balance == initial[advance.pk] - repaid >= 0


def test_cash_issue_rules(company):
    site, manager, foreman = setup_site()

    with pytest.raises(PermissionDenied):
        services.issue_cash(foreman, site, MON, 1000)
    with pytest.raises(ValidationError):
        services.issue_cash(manager, site, MON, 0)
    issue = services.issue_cash(manager, site, MON, 1000, note="Safe")
    assert AuditLog.objects.get(action="payroll.cash.issue").object_id == str(issue.pk)


# --- locking ------------------------------------------------------------------------------


def test_lock_needs_a_reason_when_someone_is_unpaid(company, media):
    site, manager, foreman = setup_site()
    yao, afi = hire(site), hire(site)
    AdvanceFactory(worker=afi, amount=4000, balance=4000, deduction_per_pay=2000, given_on=MON)
    day = roll_call(site, TUE, {yao: "present", afi: "present"})
    sheet_for(site, TUE)
    pay(foreman, line_of(site, TUE, yao))

    with pytest.raises(ValidationError):
        services.lock_sheet(manager, site, TUE, "")

    services.lock_sheet(manager, site, TUE, "Afi left before pay-out")

    period = PayPeriod.objects.get()
    assert period.status == PayPeriod.Status.LOCKED
    assert period.locked_at is not None
    day.refresh_from_db()
    assert day.status == AttendanceDay.Status.LOCKED
    # Afi was not paid, so nothing was repaid from her pay.
    afi_line = line_of(site, TUE, afi)
    assert (afi_line.advance_deduction, afi_line.net) == (0, 6000)
    assert Advance.objects.get(worker=afi).balance == 4000
    entry = AuditLog.objects.get(action="payroll.period.lock")
    assert entry.reason == "Afi left before pay-out"
    assert entry.after["unpaid_worker_ids"] == [afi.pk]


def test_locked_period_rejects_changes(company, media):
    site, manager, foreman = setup_site()
    yao, afi = hire(site), hire(site)
    day = roll_call(site, MON, {yao: "present", afi: "present"})
    sheet_for(site, MON)
    pay(foreman, line_of(site, MON, yao))
    services.lock_sheet(manager, site, MON, "Closed by the office")
    before = list(PayLine.objects.values("gross", "net", "updated_at"))

    AttendanceRecord.objects.filter(day=day).update(status="absent")
    sheet = sheet_for(site, MON)

    assert sheet.status == services.SHEET_CLOSED
    assert list(PayLine.objects.values("gross", "net", "updated_at")) == before
    with pytest.raises(Locked):
        pay(foreman, line_of(site, MON, afi))
    with pytest.raises(Locked):
        services.issue_cash(manager, site, MON, 1000)
    with pytest.raises(Locked):
        services.undo_payment(manager, Payment.objects.get())
    with pytest.raises(Locked):
        services.lock_period(manager, PayPeriod.objects.get())
    with pytest.raises(Locked):
        services.lock_sheet(manager, site, MON)


def test_lock_requires_an_approved_period(company):
    site, manager, _f = setup_site()
    roll_call(site, MON, {hire(site): "present"}, status=SUBMITTED)

    with pytest.raises(Conflict):
        services.lock_sheet(manager, site, MON, "x")


def test_who_may_lock(company, media):
    site, manager, foreman = setup_site()
    yao = hire(site)
    roll_call(site, MON, {yao: "present"})
    sheet_for(site, MON)
    pay(foreman, line_of(site, MON, yao))

    with pytest.raises(PermissionDenied):
        services.lock_sheet(foreman, site, MON)
    services.lock_sheet(UserFactory(role="accountant"), site, MON)

    assert PayPeriod.objects.get().is_locked


# --- advances -----------------------------------------------------------------------------


def test_advance_within_the_limit(company):
    site, manager, _f = setup_site()
    yao = hire(site, 6000)
    AdvanceFactory(worker=yao, amount=4000, balance=4000, deduction_per_pay=2000)

    advance = services.create_advance(
        manager,
        yao,
        amount=10000,
        deduction_per_pay=2000,
        reason="School fees",
        proof_type="signature",
        proof_file=photo(),
    )

    assert (advance.balance, advance.status, advance.given_by) == (10000, "open", manager)
    entry = AuditLog.objects.get(action="payroll.advance.create")
    assert entry.after["new_total_owed"] == 14000
    check = services.advance_check(yao, 1000, 2000)
    assert check.owed == 14000
    assert check.expected_weekly_pay == 36000  # 6 000 × 6 working days


@pytest.mark.parametrize(("limit", "amount"), [(50, 18001), (0, 1), (10, 3601)])
def test_advance_over_the_limit_is_rejected(company, limit, amount):
    company.advance_limit_percent = limit
    company.save()
    site, manager, _f = setup_site()
    yao = hire(site, 6000)

    with pytest.raises(ValidationError) as exc:
        services.create_advance(
            manager, yao, amount=amount, deduction_per_pay=1, proof_type="photo", proof_file=photo()
        )

    assert "amount" in exc.value.message_dict
    assert not Advance.objects.exists()


def test_advance_limit_counts_what_is_already_owed(company):
    site, manager, _f = setup_site()
    yao = hire(site, 6000)
    AdvanceFactory(worker=yao, amount=10000, balance=10000)

    with pytest.raises(ValidationError):
        services.create_advance(
            manager,
            yao,
            amount=8001,
            deduction_per_pay=2000,
            proof_type="photo",
            proof_file=photo(),
        )
    services.create_advance(
        manager, yao, amount=8000, deduction_per_pay=2000, proof_type="photo", proof_file=photo()
    )


@pytest.mark.parametrize(
    ("amount", "per_pay", "field"),
    [(0, 1000, "amount"), (5000, 0, "deduction_per_pay"), (5000, 6000, "deduction_per_pay")],
)
def test_advance_input_rules(company, amount, per_pay, field):
    site, manager, _f = setup_site()
    yao = hire(site)

    with pytest.raises(ValidationError) as exc:
        services.create_advance(manager, yao, amount=amount, deduction_per_pay=per_pay)

    assert field in exc.value.message_dict


def test_advance_needs_a_rate_and_a_manager_of_the_site(company):
    site, manager, foreman = setup_site()
    _other, other_manager, _of = setup_site()
    no_rate = WorkerFactory()
    SiteAssignmentFactory(worker=no_rate, site=site, start_date=datetime.date(2025, 1, 1))
    yao = hire(site)
    kwargs = {"amount": 1000, "deduction_per_pay": 500, "proof_type": "photo"}

    with pytest.raises(ValidationError):
        services.create_advance(manager, no_rate, proof_file=photo(), **kwargs)
    for user in (foreman, other_manager):
        with pytest.raises(PermissionDenied):
            services.create_advance(user, yao, proof_file=photo(), **kwargs)


def test_default_deduction_per_pay_comes_from_the_settings(company):
    site, manager, _f = setup_site()
    yao = hire(site)

    advance = services.create_advance(
        manager, yao, amount=6000, proof_type="photo", proof_file=photo()
    )

    assert advance.deduction_per_pay == company.default_advance_deduction


def test_scoped_workers(company):
    site, manager, _f = setup_site()
    other, _om, _of = setup_site()
    mine, theirs = hire(site), hire(other)

    assert list(services.scoped_workers(manager)) == [mine]
    assert set(services.scoped_workers(UserFactory(role="accountant"))) == {mine, theirs}


def test_client_uuid_is_kept(company, media):
    site, _m, foreman = setup_site()
    yao = hire(site)
    roll_call(site, MON, {yao: "present"})
    sheet_for(site, MON)
    key = uuid.uuid4()

    payment = pay(foreman, line_of(site, MON, yao), client_uuid=key)

    assert payment.client_uuid == key
