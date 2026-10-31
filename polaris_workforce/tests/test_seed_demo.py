import pytest
from django.core.management import CommandError, call_command
from django.test import Client

from apps.attendance.models import AttendanceDay
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
