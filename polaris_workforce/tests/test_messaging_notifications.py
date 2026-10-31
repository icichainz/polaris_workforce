"""Messaging: the notification rules and the ``send_notifications`` command (two companies)."""

import datetime
import io
import zoneinfo
from unittest import mock

import pytest
from django.core.management import call_command

from apps.accounts.models import User
from apps.attendance.models import AttendanceDay
from apps.core.models import CompanySettings
from apps.core.tenancy import use_company
from apps.messaging.models import Message, MessagingSettings
from apps.messaging.notifications import run_notifications
from apps.payroll import services as payroll_services
from apps.payroll.models import PayPeriod
from tests.factories import (
    AdminUserFactory,
    AdvanceFactory,
    AttendanceDayFactory,
    CompanyFactory,
    CompanySettingsFactory,
    ForemanFactory,
    PayPeriodFactory,
    PayRateFactory,
    SiteAssignmentFactory,
    SiteFactory,
    UserFactory,
    WorkerFactory,
)

pytestmark = pytest.mark.django_db

LOME = zoneinfo.ZoneInfo("Africa/Lome")
FRI = datetime.date(2026, 10, 30)
NINE = datetime.datetime(2026, 10, 30, 9, 0, tzinfo=LOME)  # roll call due at 08:00


def enable(**fields) -> MessagingSettings:
    messaging = MessagingSettings.load()
    for name, value in {"enabled": True, **fields}.items():
        setattr(messaging, name, value)
    messaging.save()
    return messaging


def build(name="Riverside"):
    """A company setup in the current company: site with manager, foreman and a crew."""
    CompanySettingsFactory(
        company_name=f"{name} Builders", time_zone="Africa/Lome", working_days=[1, 2, 3, 4, 5, 6, 7]
    )
    site = SiteFactory(name=name)
    foreman = ForemanFactory()
    site.foremen.add(foreman)
    worker = WorkerFactory()
    PayRateFactory(worker=worker, amount=5000)
    SiteAssignmentFactory(worker=worker, site=site)
    admin = AdminUserFactory()
    accountant = UserFactory(role=User.Role.ACCOUNTANT)
    return {
        "site": site,
        "manager": site.manager,
        "foreman": foreman,
        "worker": worker,
        "admin": admin,
        "accountant": accountant,
    }


def recipients(kind):
    return set(Message.objects.filter(kind=kind).values_list("user_id", flat=True))


# --- missing roll call -----------------------------------------------------------------------


def test_missing_roll_call_goes_to_the_foreman_and_the_manager():
    w = build()
    enable()
    assert run_notifications(now=NINE)["missing_roll_call"] == 2
    assert recipients("missing_roll_call") == {w["foreman"].pk, w["manager"].pk}
    text = Message.objects.filter(kind="missing_roll_call").first().text
    assert text == (
        "Riverside Builders: The roll call for Riverside is not in yet (due 08:00 today). "
        "Please send it."
    )
    # Run again (cron): one message per person, site and day.
    assert run_notifications(now=NINE + datetime.timedelta(hours=1))["missing_roll_call"] == 0
    assert Message.objects.filter(kind="missing_roll_call").count() == 2
    # Next day: again.
    assert run_notifications(now=NINE + datetime.timedelta(days=1))["missing_roll_call"] == 2


def test_no_missing_roll_call_before_it_is_due_or_once_submitted():
    w = build()
    enable()
    assert run_notifications(now=NINE.replace(hour=7, minute=30))["missing_roll_call"] == 0
    AttendanceDayFactory(site=w["site"], date=FRI, status=AttendanceDay.Status.SUBMITTED)
    assert run_notifications(now=NINE)["missing_roll_call"] == 0


def test_no_missing_roll_call_on_a_day_off():
    build()
    CompanySettings.objects.update(working_days=[1, 2, 3, 4])  # Friday is off
    enable()
    assert run_notifications(now=NINE)["missing_roll_call"] == 0


def test_rules_that_are_off_send_nothing():
    build()
    enable(notify_missing_roll_call=False)
    assert "missing_roll_call" not in run_notifications(now=NINE)
    enable(enabled=False)
    assert run_notifications(now=NINE) == {}
    assert Message.objects.count() == 0


# --- approvals -------------------------------------------------------------------------------


def test_days_waiting_for_approval_go_to_who_may_approve_them():
    w = build()
    enable(notify_missing_roll_call=False)
    AttendanceDayFactory(
        site=w["site"],
        date=FRI - datetime.timedelta(days=1),
        status=AttendanceDay.Status.SUBMITTED,
        submitted_at=NINE - datetime.timedelta(hours=3),
    )
    other_site = SiteFactory(name="Hilltop")  # another manager: not theirs
    assert run_notifications(now=NINE)["awaiting_approval"] == 2
    assert recipients("awaiting_approval") == {w["manager"].pk, w["admin"].pk}
    assert other_site.manager.pk not in recipients("awaiting_approval")
    text = Message.objects.filter(kind="awaiting_approval", user=w["manager"]).get().text
    assert text == (
        "Riverside Builders: 1 roll call waiting for your approval, the oldest Thu 29 Oct · "
        "Riverside."
    )
    assert run_notifications(now=NINE)["awaiting_approval"] == 0  # once a day


def test_a_day_just_submitted_is_not_reminded_yet():
    w = build()
    enable(approval_reminder_hours=2)
    AttendanceDayFactory(
        site=w["site"],
        date=FRI,
        status=AttendanceDay.Status.SUBMITTED,
        submitted_at=NINE - datetime.timedelta(minutes=30),
    )
    assert run_notifications(now=NINE)["awaiting_approval"] == 0


# --- cash sheets -----------------------------------------------------------------------------


def test_unclosed_cash_sheets_go_to_managers_accountants_and_admins():
    w = build()
    enable(notify_missing_roll_call=False)
    yesterday = FRI - datetime.timedelta(days=1)
    PayPeriodFactory(site=w["site"], start_date=yesterday, end_date=yesterday)
    PayPeriodFactory(site=w["site"], start_date=FRI, end_date=FRI)  # today: not late yet
    paid = FRI - datetime.timedelta(days=2)
    PayPeriodFactory(site=w["site"], start_date=paid, end_date=paid, status=PayPeriod.Status.PAID)
    assert run_notifications(now=NINE)["unclosed_cash_sheet"] == 3
    assert recipients("unclosed_cash_sheet") == {
        w["manager"].pk,
        w["accountant"].pk,
        w["admin"].pk,
    }
    text = Message.objects.filter(kind="unclosed_cash_sheet").first().text
    assert text == (
        "Riverside Builders: 1 cash sheet not closed after pay day: Riverside (Thu 29 Oct)."
    )
    assert w["foreman"].pk not in recipients("unclosed_cash_sheet")


# --- advances --------------------------------------------------------------------------------


def test_advance_near_its_limit_warns_the_manager_and_admins_when_on():
    w = build()
    enable(notify_missing_roll_call=False, notify_advance_limit=True, advance_limit_warn_percent=80)
    limit = payroll_services.advance_check(w["worker"], 0, 0, on=FRI).limit_amount
    assert limit > 0
    owed = limit * 9 // 10
    AdvanceFactory(worker=w["worker"], amount=owed, balance=owed)
    below = WorkerFactory()
    PayRateFactory(worker=below, amount=5000)
    SiteAssignmentFactory(worker=below, site=w["site"])
    AdvanceFactory(worker=below, amount=limit // 2, balance=limit // 2)

    assert run_notifications(now=NINE)["advance_limit"] == 2
    assert recipients("advance_limit") == {w["manager"].pk, w["admin"].pk}

    def money(amount):
        return f"{amount:,} XOF".replace(",", " ")

    assert set(Message.objects.filter(kind="advance_limit").values_list("text", flat=True)) == {
        f"Riverside Builders: {w['worker'].full_name} owes {money(owed)} on advances, "
        f"90% of the limit ({money(limit)})."
    }


def test_advance_rule_is_off_by_default():
    w = build()
    enable()
    AdvanceFactory(worker=w["worker"], amount=100_000, balance=100_000)
    assert "advance_limit" not in run_notifications(now=NINE)


# --- the command, across companies -----------------------------------------------------------


def test_the_command_runs_every_company_without_mixing_them(default_company):
    a = build("Alpha")
    enable()
    other = CompanyFactory(name="Bravo Co")
    with use_company(other):
        b = build("Bravo")
        enable()
    off = CompanyFactory(name="Charlie Co")
    with use_company(off):
        build("Charlie")  # messaging off
    inactive = CompanyFactory(name="Delta Co", is_active=False)
    with use_company(inactive):
        build("Delta")
        enable()

    out = io.StringIO()
    with mock.patch("apps.messaging.notifications.company_now", return_value=NINE):
        call_command("send_notifications", stdout=out)
        call_command("send_notifications", stdout=io.StringIO())  # idempotent

    mine = Message.objects.filter(company=default_company)
    theirs = Message.objects.filter(company=other)
    assert set(mine.values_list("user_id", flat=True)) == {a["foreman"].pk, a["manager"].pk}
    assert set(theirs.values_list("user_id", flat=True)) == {b["foreman"].pk, b["manager"].pk}
    assert all("Alpha" in m.text and "Bravo" not in m.text for m in mine)
    assert all("Bravo" in m.text and "Alpha" not in m.text for m in theirs)
    assert Message.objects.filter(company__in=[off, inactive]).count() == 0
    assert Message.objects.count() == 4
    assert set(Message.objects.values_list("status", flat=True)) == {"sent"}
    assert "missing_roll_call 2" in out.getvalue()
    assert "sent 2" in out.getvalue()


def test_the_command_can_queue_only_and_send_messages_sends(default_company):
    build()
    enable()
    with mock.patch("apps.messaging.notifications.company_now", return_value=NINE):
        call_command("send_notifications", "--no-send", stdout=io.StringIO())
    assert set(Message.objects.values_list("status", flat=True)) == {"queued"}
    out = io.StringIO()
    call_command("send_messages", stdout=out)
    assert set(Message.objects.values_list("status", flat=True)) == {"sent"}
    assert f"{default_company.slug}: sent 2" in out.getvalue()


def test_the_command_refuses_an_unknown_company():
    from django.core.management.base import CommandError

    with pytest.raises(CommandError):
        call_command("send_notifications", "--company", "nope", stdout=io.StringIO())
