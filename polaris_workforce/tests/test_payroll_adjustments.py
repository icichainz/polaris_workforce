"""Pay adjustments (security review M3), who locks pay (M1) and refresh on read (L7).

A day corrected after the worker was paid for it: the paid line stays as paid, and the
difference becomes a ``PayAdjustment`` settled on the worker's next pay at the site.
"""

import datetime
import uuid

import pytest
from django.core.exceptions import PermissionDenied
from django.urls import reverse

from apps.api.exceptions import Conflict
from apps.attendance import services as attendance
from apps.attendance.models import AttendanceRecord
from apps.attendance.selectors import approval_detail, paid_notes
from apps.core.dates import company_tz
from apps.core.models import AuditLog
from apps.payroll import services
from apps.payroll.models import PayAdjustment, PayAdjustmentApplication, PayLine, PayPeriod
from tests.factories import (
    AdvanceFactory,
    CompanySettingsFactory,
    SiteManagerFactory,
    UserFactory,
)
from tests.test_payroll_services import (
    FRI,
    MON,
    SAT,
    THU,
    TUE,
    WED,
    hire,
    line_of,
    pay,
    roll_call,
    setup_site,
)


@pytest.fixture
def company(db, settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path
    return CompanySettingsFactory()


def record_of(day, worker):
    return AttendanceRecord.objects.get(day=day, worker=worker)


def correct(day, worker, value, user, reason="Went home sick", field="status"):
    """A correction through the attendance service (which refreshes pay)."""
    record = record_of(day, worker)
    return attendance.correct_record(record, field, value, reason, user, uuid.uuid4())


def paid_monday(rate=6000, mark="present", **site_kwargs):
    """Yao present on Monday (approved) and paid; Afi present too, unpaid."""
    site, manager, foreman = setup_site(**site_kwargs)
    yao, afi = hire(site, rate, first_name="Yao"), hire(site, 5000, first_name="Afi")
    day = roll_call(site, MON, {yao: mark, afi: "present"})
    services.cash_sheet(site, MON)
    pay(foreman, line_of(site, MON, yao))
    return site, manager, foreman, yao, afi, day


def actions(prefix="payroll."):
    return list(
        AuditLog.objects.filter(action__startswith=prefix)
        .order_by("pk")
        .values_list("action", flat=True)
    )


# --- raising an adjustment ----------------------------------------------------------------


def test_a_manager_correction_after_payment_raises_an_adjustment(company):
    site, manager, _foreman, yao, _afi, monday = paid_monday()
    paid_line = line_of(site, MON, yao)

    correct(monday, yao, "absent", manager)

    adjustment = PayAdjustment.objects.get()
    assert (adjustment.worker, adjustment.site, adjustment.amount) == (yao, site, -6000)
    assert adjustment.source_line == paid_line
    assert (adjustment.day, adjustment.record) == (monday, record_of(monday, yao))
    assert (adjustment.dates, adjustment.reason) == (["2025-09-01"], "Went home sick")
    assert (adjustment.currency, adjustment.currency_decimals) == ("XOF", 0)
    assert adjustment.created_by == manager
    assert (adjustment.applied_amount, adjustment.applied_to) == (0, None)
    # The paid line is what was paid.
    paid_line.refresh_from_db()
    assert (paid_line.gross, paid_line.net, paid_line.adjustment) == (6000, 6000, 0)
    entry = AuditLog.objects.get(action="payroll.adjustment.create")
    assert entry.actor == manager
    assert (entry.after["amount"], entry.after["paid"], entry.after["worth"]) == (-6000, 6000, 0)


def test_refreshing_or_approving_again_does_not_double_it(company):
    site, manager, _foreman, yao, _afi, monday = paid_monday()
    correct(monday, yao, "absent", manager)
    entries = AuditLog.objects.count()

    services.cash_sheet(site, MON)
    services.sync_site(site, MON, SAT)
    services.on_attendance_changed(monday, actor=manager)

    assert list(PayAdjustment.objects.values_list("amount", flat=True)) == [-6000]
    assert AuditLog.objects.count() == entries


def test_a_foreman_correction_waits_for_the_approval(company):
    site, manager, foreman, yao, _afi, monday = paid_monday()

    correct(monday, yao, "half_day", foreman)

    monday.refresh_from_db()
    assert monday.status == "submitted"
    assert not PayAdjustment.objects.exists()
    # The Approvals screen warns before the manager approves.
    assert paid_notes(monday) == {
        yao.pk: "Already paid for this day: the difference of 3 000 XOF will be recovered "
        "on the next pay."
    }
    detail = approval_detail(monday, company_tz())
    assert detail["paid_count"] == 1
    assert [row["name"] for row in detail["rows"] if row["paid_note"]] == [yao.full_name]

    attendance.approve_day(monday, manager)

    assert PayAdjustment.objects.get().amount == -3000


def test_a_further_correction_updates_the_open_adjustment(company):
    site, manager, _foreman, yao, _afi, monday = paid_monday()
    correct(monday, yao, "absent", manager)
    adjustment = PayAdjustment.objects.get()

    correct(monday, yao, "half_day", manager, reason="Came at noon")

    adjustment.refresh_from_db()
    assert adjustment.amount == -3000
    assert adjustment.reason == "Went home sick; Came at noon"
    entry = AuditLog.objects.get(action="payroll.adjustment.update")
    assert (entry.before["amount"], entry.after["amount"]) == (-6000, -3000)

    # Back to what was paid: nothing left to settle.
    correct(monday, yao, "present", manager, reason="Was there after all")

    assert not PayAdjustment.objects.exists()
    assert actions("payroll.adjustment.") == [
        "payroll.adjustment.create",
        "payroll.adjustment.update",
        "payroll.adjustment.cancel",
    ]


def test_corrections_that_change_nothing_raise_nothing(company):
    _site, manager, _foreman, yao, _afi, monday = paid_monday()

    correct(monday, yao, "Holds the keys", manager, field="note")

    assert not PayAdjustment.objects.exists()


def test_an_underpaid_day_is_added_to_the_next_pay(company):
    site, manager, foreman, yao, _afi, monday = paid_monday(mark="half_day")
    correct(monday, yao, "present", manager, reason="Stayed for the slab")
    assert PayAdjustment.objects.get().amount == 3000

    roll_call(site, TUE, {yao: "present"})
    services.cash_sheet(site, TUE)

    line = line_of(site, TUE, yao)
    assert (line.gross, line.adjustment, line.net) == (6000, 3000, 9000)


def test_an_underpaid_worker_is_paid_even_on_a_day_off(company):
    site, manager, _foreman, yao, afi, monday = paid_monday(mark="half_day")
    correct(monday, yao, "present", manager)

    roll_call(site, TUE, {yao: "absent", afi: "present"})
    sheet = services.cash_sheet(site, TUE)

    line = line_of(site, TUE, yao)
    assert (line.gross, line.adjustment, line.net) == (0, 3000, 3000)
    assert yao not in sheet.absent


def test_a_weekly_pay_corrected_after_payment(company):
    site, manager, foreman = setup_site(pay_frequency="weekly")
    yao = hire(site, 6000)
    days = {
        date: roll_call(site, date, {yao: "present"}) for date in (MON, TUE, WED, THU, FRI, SAT)
    }
    services.cash_sheet(site, SAT)
    pay(foreman, line_of(site, SAT, yao))  # 36 000

    correct(days[WED], yao, "half_day", manager)
    correct(days[THU], yao, "absent", manager, reason="Funeral")

    adjustment = PayAdjustment.objects.get()
    assert adjustment.amount == -9000
    assert adjustment.dates == ["2025-09-03", "2025-09-04"]
    assert adjustment.reason == "Went home sick; Funeral"
    next_monday = MON + datetime.timedelta(days=7)
    roll_call(site, next_monday, {yao: "present"})
    next_saturday = SAT + datetime.timedelta(days=7)
    services.cash_sheet(site, next_saturday)
    line = line_of(site, next_saturday, yao)
    assert (line.gross, line.adjustment, line.net) == (6000, -6000, 0)
    assert PayAdjustment.objects.get().remaining == -3000


# --- settling it on the next pays ---------------------------------------------------------


def test_the_next_pay_recovers_it_and_carries_the_rest(company):
    site, manager, foreman, yao, _afi, monday = paid_monday()
    correct(monday, yao, "absent", manager)
    adjustment = PayAdjustment.objects.get()

    roll_call(site, TUE, {yao: "half_day"})
    services.cash_sheet(site, TUE)
    tuesday = line_of(site, TUE, yao)
    assert (tuesday.gross, tuesday.adjustment, tuesday.net) == (3000, -3000, 0)
    adjustment.refresh_from_db()
    assert (adjustment.applied_amount, adjustment.applied_to) == (-3000, tuesday)

    roll_call(site, WED, {yao: "present"})
    services.cash_sheet(site, WED)
    wednesday = line_of(site, WED, yao)
    assert (wednesday.gross, wednesday.adjustment, wednesday.net) == (6000, -3000, 3000)
    adjustment.refresh_from_db()
    assert (adjustment.applied_amount, adjustment.remaining) == (-6000, 0)
    assert adjustment.applied_to == wednesday
    assert sorted(
        PayAdjustmentApplication.objects.values_list("pay_line__period__end_date", "amount")
    ) == [(TUE, -3000), (WED, -3000)]

    # Thursday owes nothing more.
    roll_call(site, THU, {yao: "present"})
    services.cash_sheet(site, THU)
    assert line_of(site, THU, yao).net == 6000


def test_it_is_recovered_after_the_advance(company):
    site, manager, _foreman, yao, _afi, monday = paid_monday()
    AdvanceFactory(worker=yao, amount=6000, balance=6000, deduction_per_pay=2000, given_on=MON)
    # Monday was already paid before the advance existed (given on Monday, repaid from
    # the pays after it).
    correct(monday, yao, "absent", manager)

    roll_call(site, TUE, {yao: "present"})
    services.cash_sheet(site, TUE)

    line = line_of(site, TUE, yao)
    assert (line.gross, line.advance_deduction, line.adjustment, line.net) == (
        6000,
        2000,
        -4000,
        0,
    )
    assert PayAdjustment.objects.get().remaining == -2000


def test_an_adjustment_already_on_the_next_pay_follows_a_further_correction(company):
    site, manager, _foreman, yao, _afi, monday = paid_monday()
    roll_call(site, TUE, {yao: "present"})
    services.cash_sheet(site, TUE)  # Tuesday's line exists (not paid yet)

    correct(monday, yao, "absent", manager)
    # The correction refreshed Tuesday's pay at once (no stale figure on the phone).
    assert line_of(site, TUE, yao).net == 0

    correct(monday, yao, "half_day", manager, reason="Came at noon")
    assert (line_of(site, TUE, yao).adjustment, line_of(site, TUE, yao).net) == (-3000, 3000)

    correct(monday, yao, "present", manager, reason="All day")
    line = line_of(site, TUE, yao)
    assert (line.adjustment, line.net) == (0, 6000)
    assert not PayAdjustmentApplication.objects.exists()


def test_once_settled_on_a_paid_pay_a_further_correction_raises_a_new_one(company):
    site, manager, foreman, yao, _afi, monday = paid_monday()
    correct(monday, yao, "absent", manager)
    roll_call(site, TUE, {yao: "present", _afi: "present"})
    services.cash_sheet(site, TUE)
    tuesday = line_of(site, TUE, yao)
    assert tuesday.net == 0
    pay(foreman, line_of(site, TUE, _afi))
    services.lock_sheet(manager, site, TUE)  # Tuesday settled: -6000 is final

    correct(monday, yao, "present", manager, reason="Was on site after all")

    amounts = list(PayAdjustment.objects.order_by("pk").values_list("amount", "applied_amount"))
    assert amounts == [(-6000, -6000), (6000, 0)]
    assert PayAdjustment.objects.order_by("pk").last().reason == "Was on site after all"
    roll_call(site, WED, {yao: "present"})
    services.cash_sheet(site, WED)
    assert line_of(site, WED, yao).net == 12000


def test_a_pay_closed_unpaid_gives_the_adjustment_back(company):
    site, manager, foreman, yao, afi, monday = paid_monday()
    correct(monday, yao, "absent", manager)
    roll_call(site, TUE, {yao: "present", afi: "present"})
    services.cash_sheet(site, TUE)
    assert line_of(site, TUE, yao).adjustment == -6000
    # Yao's net is 0 (nothing to hand over); Afi is left unpaid and the sheet closed.
    services.lock_sheet(manager, site, TUE, "Afi was away")
    assert PayAdjustment.objects.get().applied_amount == -6000  # net 0 counts as paid

    # Another case: the pay holding part of an adjustment is closed unpaid.
    site2, manager2, foreman2, kofi, _x, monday2 = paid_monday()
    correct(monday2, kofi, "half_day", manager2)
    roll_call(site2, TUE, {kofi: "present"})
    services.cash_sheet(site2, TUE)
    assert line_of(site2, TUE, kofi).net == 3000
    services.lock_sheet(manager2, site2, TUE, "Kofi did not come for his pay")

    kofi_line = line_of(site2, TUE, kofi)
    assert (kofi_line.adjustment, kofi_line.net) == (0, 6000)
    adjustment = PayAdjustment.objects.get(worker=kofi)
    assert (adjustment.applied_amount, adjustment.remaining) == (0, -3000)
    roll_call(site2, WED, {kofi: "present"})
    services.cash_sheet(site2, WED)
    assert line_of(site2, WED, kofi).net == 3000


def test_other_sites_and_other_workers_are_not_touched(company):
    site, manager, _foreman, yao, afi, monday = paid_monday()
    other, _m, _f = setup_site()
    hire_other = hire(other, 6000)
    correct(monday, yao, "absent", manager)

    roll_call(site, TUE, {afi: "present"})
    roll_call(other, TUE, {hire_other: "present"})
    services.cash_sheet(site, TUE)
    services.cash_sheet(other, TUE)

    assert line_of(site, TUE, afi).net == 5000
    assert line_of(other, TUE, hire_other).net == 6000
    assert PayAdjustment.objects.get().applied_amount == 0


# --- undoing the payment ------------------------------------------------------------------


def test_undoing_the_payment_cancels_an_open_adjustment(company):
    site, manager, _foreman, yao, _afi, monday = paid_monday()
    roll_call(site, TUE, {yao: "present"})
    services.cash_sheet(site, TUE)
    correct(monday, yao, "absent", manager)
    assert line_of(site, TUE, yao).net == 0

    services.undo_payment(manager, line_of(site, MON, yao).payments.get(), "Wrong worker")

    assert not PayAdjustment.objects.exists()
    assert line_of(site, TUE, yao).net == 6000
    services.cash_sheet(site, MON)
    assert not PayLine.objects.filter(period__end_date=MON, worker=yao).exists()  # absent


def test_a_settled_correction_blocks_undoing_the_payment(company):
    site, manager, foreman, yao, _afi, monday = paid_monday(mark="half_day")
    correct(monday, yao, "present", manager)
    roll_call(site, TUE, {yao: "present"})
    services.cash_sheet(site, TUE)
    pay(foreman, line_of(site, TUE, yao))  # 9 000 handed over

    with pytest.raises(Conflict):
        services.undo_payment(manager, line_of(site, MON, yao).payments.get(), "Oops")


# --- display ------------------------------------------------------------------------------


def test_cash_sheet_and_payslip_show_the_adjustment(company, client):
    site, manager, _foreman, yao, _afi, monday = paid_monday()
    correct(monday, yao, "absent", manager)
    roll_call(site, TUE, {yao: "half_day"})

    sheet = services.cash_sheet(site, TUE)
    sheet_line = next(sl for sl in sheet.lines if sl.worker == yao)
    assert [(a.amount, applied) for a, applied in sheet_line.adjustments] == [(-6000, -3000)]
    client.force_login(manager)
    html = client.get(reverse("payroll:cash_sheet", args=[site.pk, TUE.isoformat()]))
    html = html.content.decode()
    assert "Correction of 1 Sep" in html
    assert "− 3 000" in html
    monday_html = client.get(reverse("payroll:cash_sheet", args=[site.pk, MON.isoformat()]))
    assert (
        "Already paid for this day: the difference of 3 000 XOF will be recovered on the "
        "next pay." in monday_html.content.decode()  # what is left after Tuesday
    )
    periods = client.get(reverse("payroll:periods"), {"status": "all"})
    tuesday_row = next(r for r in periods.context["rows"] if r["period"].end_date == TUE)
    assert (tuesday_row["period"].gross, tuesday_row["deductions"]) == (3000, -3000)
    assert tuesday_row["period"].net == 0  # gross + deductions = net


# --- who locks pay (M1) -------------------------------------------------------------------


@pytest.mark.parametrize(
    ("who", "allowed"),
    [("admin", True), ("accountant", True), ("manager", True), ("other_manager", False)],
)
def test_who_may_lock_a_site(company, who, allowed):
    site, manager, foreman = setup_site()
    other_site, other_manager, _f = setup_site()
    users = {
        "admin": UserFactory(role="admin"),
        "accountant": UserFactory(role="accountant"),
        "manager": manager,
        "other_manager": other_manager,
    }
    yao = hire(site)
    roll_call(site, MON, {yao: "present"})
    services.cash_sheet(site, MON)
    pay(foreman, line_of(site, MON, yao))
    user = users[who]

    assert services.can_lock(user, site) is allowed
    if allowed:
        services.lock_sheet(user, site, MON)
        assert PayPeriod.objects.get(site=site).is_locked
    else:
        with pytest.raises(PermissionDenied):
            services.lock_sheet(user, site, MON)
    assert services.can_lock(manager, other_site) is False


def test_a_foreman_cannot_lock_and_nothing_is_refreshed_first(company):
    site, _manager, foreman = setup_site()
    roll_call(site, MON, {hire(site): "present"})
    entries = AuditLog.objects.count()

    with pytest.raises(PermissionDenied):
        services.lock_sheet(foreman, site, MON)
    with pytest.raises(PermissionDenied):
        services.lock_period(
            foreman, PayPeriod.objects.create(site=site, start_date=MON, end_date=MON)
        )

    assert AuditLog.objects.count() == entries
    assert not PayLine.objects.exists()


def test_the_lock_button_follows_the_permission(company, client):
    site, manager, foreman = setup_site()
    roll_call(site, MON, {hire(site): "present"})
    url = reverse("payroll:cash_sheet", args=[site.pk, MON.isoformat()])

    for user, shown in [
        (manager, True),
        (UserFactory(role="accountant"), True),
        (foreman, False),
    ]:
        client.force_login(user)
        assert client.get(url).context["can_lock"] is shown
    client.force_login(SiteManagerFactory())
    assert client.get(url).status_code == 403


# --- refresh on read (L7) -----------------------------------------------------------------


def test_reading_pay_writes_nothing_as_the_viewer(company, client):
    site, manager, foreman = setup_site()
    roll_call(site, MON, {hire(site): "present"})
    accountant = UserFactory(role="accountant")
    client.force_login(accountant)

    for url in (
        reverse("payroll:cash_sheet", args=[site.pk, MON.isoformat()]),
        reverse("payroll:periods"),
        reverse("payroll:cash_sheets"),
    ):
        assert client.get(url).status_code == 200

    entries = AuditLog.objects.filter(action__startswith="payroll.")
    assert entries.exists()  # the first read computed the pay…
    assert all(e.actor is None and e.action.startswith("payroll.auto.") for e in entries)
    assert not AuditLog.objects.filter(actor=accountant).exists()
    count = AuditLog.objects.count()

    # …and reading again, nothing changed: no rows at all.
    client.get(reverse("payroll:cash_sheet", args=[site.pk, MON.isoformat()]))
    client.force_login(foreman)
    api = client.get(f"/api/v1/sites/{site.pk}/payouts/{MON.isoformat()}/")
    assert api.status_code == 200
    assert AuditLog.objects.count() == count


def test_a_users_write_is_still_attributed_to_them(company):
    site, manager, foreman = setup_site()
    yao = hire(site)
    day = roll_call(site, MON, {yao: "present"}, status="submitted")

    attendance.approve_day(day, manager)

    rows = AuditLog.objects.filter(action__startswith="payroll.period.")
    assert rows.exists()
    assert {e.actor for e in rows} == {manager}
    assert not AuditLog.objects.filter(action__startswith="payroll.auto.").exists()
