import datetime
import io
from decimal import Decimal
from unittest import mock

import pytest
from django.core.management import CommandError, call_command
from django.test import Client

from apps.accounts.models import User
from apps.attendance.models import AttendanceDay
from apps.checkin.models import Badge, CheckIn, KioskDevice, SiteGeofence
from apps.core.models import Company
from apps.messaging.models import Kind, Message, MessagingSettings
from apps.payouts.models import PayoutAccount, PayoutBatch
from apps.payroll.models import (
    Advance,
    Bonus,
    DeductionRule,
    PayAdjustment,
    PayLine,
    Payment,
    PayPeriod,
    PayRuleSettings,
    PieceWorkEntry,
    StoppageDay,
)
from apps.portal.models import WorkerAccess
from apps.workers.models import Worker

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _a_working_day():
    """The demo depends on the date (today's roll calls, check-ins, weekly pay): seed it on
    a Friday morning so the tests don't change with the calendar (Sundays have no work)."""
    friday = datetime.datetime(2026, 10, 30, 10, 0, tzinfo=datetime.UTC)
    real_now = datetime.datetime.now
    started = real_now(datetime.UTC)
    # The clock keeps ticking from that Friday: the seed relies on events happening in order
    # (a correction after a payment becomes a pay adjustment).
    with mock.patch(
        "django.utils.timezone.now", side_effect=lambda: friday + (real_now(datetime.UTC) - started)
    ):
        yield


def test_seed_demo_builds_a_consistent_demo(settings):
    settings.ALLOWED_HOSTS = ["testserver"]
    call_command("seed_demo", password="demo-pass-123", verbosity=0)

    assert Worker.objects.count() == 30
    statuses = set(AttendanceDay.objects.values_list("status", flat=True))
    assert {"submitted", "approved", "locked"} <= statuses
    assert PayPeriod.objects.filter(status="locked").exists()
    assert Payment.objects.exists()
    assert PayAdjustment.objects.exists()
    assert Advance.objects.filter(balance__gt=0).exists()

    phone = Client().post(
        "/api/v1/auth/pin/",
        {"phone": "+22890112233", "pin": "4821", "device_id": "demo"},
        content_type="application/json",
    )
    assert phone.status_code == 200

    office = Client()
    assert office.login(username="admin", password="demo-pass-123")
    for path in ["/", "/approvals/", "/cash-sheets/", "/pay-periods/", "/reports/"]:
        assert office.get(path).status_code == 200, path


def test_seed_demo_refuses_a_database_with_data():
    call_command("seed_demo", password="demo-pass-123", days=3, verbosity=0)

    with pytest.raises(CommandError):
        call_command("seed_demo", days=3, verbosity=0)


def test_seed_demo_adds_a_second_company_with_keep(settings):
    settings.ALLOWED_HOSTS = ["testserver"]
    call_command("seed_demo", password="demo-pass-123", days=3, verbosity=0)
    call_command(
        "seed_demo", password="demo-pass-123", days=3, company="Other Co", keep=True, verbosity=0
    )

    demo = Company.objects.get(name="Polaris Demo Construction")
    other = Company.objects.get(name="Other Co")
    assert Worker.objects.filter(company=demo).count() == 30
    assert Worker.objects.filter(company=other).count() == 30
    # Each company numbers its own workers from W-0001.
    assert Worker.objects.filter(number="W-0001").count() == 2
    assert User.objects.get(username="admin").company == demo
    assert User.objects.get(username="other-co-admin").company == other
    platform = User.objects.get(username="platform")
    assert platform.company is None and platform.is_superuser

    # The second company's foreman signs in with their own (different) phone number.
    foreman = User.objects.get(username="other-co-kossi")
    assert foreman.phone != User.objects.get(username="kossi").phone
    phone = Client().post(
        "/api/v1/auth/pin/",
        {"phone": foreman.phone, "pin": "4821", "device_id": "demo-2"},
        content_type="application/json",
    )
    assert phone.status_code == 200
    assert {site["name"] for site in phone.json()["sites"]} == {"Riverside Apartments"}

    office = Client()
    assert office.login(username="platform", password="demo-pass-123")
    assert office.get("/").status_code == 200
    with pytest.raises(CommandError):  # the same name twice
        call_command("seed_demo", days=3, company="Other Co", keep=True, verbosity=0)
    # Wave 4 demo data (portal phones are unique across companies) is the first company's.
    assert not WorkerAccess.objects.filter(company=other).exists()


def test_seed_demo_has_wave4_data_for_every_new_screen(settings):
    """Pay rules, payouts, check-in, worker portal and messaging (the first company)."""
    settings.ALLOWED_HOSTS = ["testserver"]
    out = io.StringIO()
    call_command("seed_demo", password="demo-pass-123", stdout=out)
    report = out.getvalue()
    company = Company.objects.get(name="Polaris Demo Construction")

    rules = PayRuleSettings.load(company)
    assert (rules.sunday_multiplier, rules.holiday_multiplier) == (Decimal("1.5"), Decimal("2"))
    rule = DeductionRule.objects.get(company=company)
    assert (rule.name, rule.employee_percent, rule.employer_percent) == ("Social security", 4, 16)
    assert rule.base_ceiling
    assert PayLine.objects.exclude(employer_contributions=0).exists()
    assert PieceWorkEntry.objects.exists() and PayLine.objects.exclude(piece_amount=0).exists()
    assert Bonus.objects.filter(applied_to__isnull=False).exists()
    assert StoppageDay.objects.exists()
    assert any(
        day.get("day_kind") == "holiday" for line in PayLine.objects.all() for day in line.days
    )

    assert PayoutAccount.objects.filter(method="mobile_money").count() >= 3
    assert PayoutAccount.objects.filter(method="bank_transfer").exists()
    assert set(PayoutBatch.objects.values_list("status", flat=True)) == {"exported", "confirmed"}

    assert Badge.objects.filter(revoked_at__isnull=True).count() == Worker.objects.count()
    assert SiteGeofence.objects.count() == 3
    assert KioskDevice.objects.select_related("site").get().site.name == "Route 4 Bridge"
    assert CheckIn.objects.exists()

    assert WorkerAccess.objects.filter(is_enabled=True).count() == 3
    assert MessagingSettings.load(company).enabled
    assert Message.objects.filter(kind=Kind.PORTAL_PIN, status="sent").count() == 3
    assert Message.objects.filter(kind=Kind.PAYSLIP).exists()
    assert not Message.objects.filter(text__contains=": 4071").exists()  # masked once sent

    # The report gives the portal sign-ins and the kiosk, and they work.
    assert "Worker portal" in report and "4071" in report and "demo-kiosk-bridge" in report
    first = WorkerAccess.objects.order_by("pk").first()
    portal = Client()
    signed = portal.post("/portal/sign-in/", {"phone": first.phone, "pin": "4071"})
    assert signed.status_code == 302, signed.content
    assert portal.get("/portal/pay/").status_code == 200

    kiosk_phone = Client()
    signin = kiosk_phone.post(
        "/api/v1/auth/pin/",
        {"phone": "+22898115230", "pin": "7264", "device_id": "demo-kiosk-bridge"},
        content_type="application/json",
    )
    assert signin.status_code == 200
    token = {"HTTP_AUTHORIZATION": f"Device {signin.json()['token']}"}
    assert kiosk_phone.get("/api/v1/checkin/device/", **token).json()["kiosk"] is not None
    assert kiosk_phone.get("/api/v1/sync/", **token).status_code == 403

    office = Client()
    assert office.login(username="admin", password="demo-pass-123")
    for path in [
        "/payouts/",
        "/payouts/accounts/",
        "/checkin/",
        "/checkin/badges/",
        "/checkin/kiosks/",
        "/checkin/geofences/",
        "/portal/office/",
        "/messages/",
        "/pay-rules/",
        "/reports/",
    ]:
        assert office.get(path).status_code == 200, path
