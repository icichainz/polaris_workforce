import pytest
from django.core.management import CommandError, call_command
from django.test import Client

from apps.accounts.models import User
from apps.attendance.models import AttendanceDay
from apps.core.models import Company
from apps.payroll.models import Advance, PayAdjustment, Payment, PayPeriod
from apps.workers.models import Worker

pytestmark = pytest.mark.django_db


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
