"""End to end: an advance is taken from the next pays until it is repaid exactly, and the
limit (``advance_limit_percent`` of a normal week's pay) is enforced in the office.

Company defaults: Monday–Saturday (6 working days), limit 50 %. Yao earns 5000 a day, so
a normal week is 30000 and he may owe at most 15000.
"""

import datetime

import pytest
from django.core.cache import cache
from django.urls import reverse

from apps.attendance.models import AttendanceDay
from apps.core.models import AuditLog
from apps.payroll import services as payroll_services
from apps.payroll.models import Advance, AdvanceRepayment
from tests.e2e import Office, Phone, hire, lines_by_worker, make_site, new_uuid, record
from tests.factories import CompanySettingsFactory, UserFactory

pytestmark = pytest.mark.django_db

PIN = "1357"
PHONE = "+228 90 33 44 55"
MON = datetime.date(2025, 9, 1)
TUE, WED, THU, FRI = (MON + datetime.timedelta(days=i) for i in range(1, 5))


@pytest.fixture(autouse=True)
def _isolated(settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path
    cache.clear()
    yield
    cache.clear()


def _advance_form(worker, amount, per_pay):
    return {
        "worker": worker.pk,
        "amount": amount,
        "deduction_per_pay": per_pay,
        "reason": "School fees",
        "proof_type": "signature",
    }


def test_advance_is_repaid_from_the_next_pays_and_the_limit_holds():
    CompanySettingsFactory()
    crew = make_site("Hilltop", phone_number=PHONE, pin=PIN)
    yao = hire(crew, "Yao", 5000)
    site_id = crew.site.pk
    manager = Office(crew.manager)
    phone = Phone(PHONE, PIN)

    # Given on Monday (the office screen dates an advance today; the service takes a date).
    advance = payroll_services.create_advance(
        crew.manager,
        yao,
        amount=7000,
        deduction_per_pay=3000,
        reason="Rent",
        given_on=MON,
        proof_type="signature",
    )

    # --- the limit, through the Advances screen -------------------------------------------
    advances_url = reverse("payroll:advances")
    over = manager.post(advances_url, _advance_form(yao, "9 000", "3 000"))  # 16000 > 15000
    assert over.status_code == 200
    assert over.context["form"].errors["amount"]
    assert Advance.objects.count() == 1
    preview = manager.get(
        reverse("payroll:advance_preview"),
        {"worker": yao.pk, "amount": "9000", "deduction_per_pay": "3000"},
    )
    check = preview.context["preview"]["check"]
    assert (check.limit_amount, check.new_total, check.allowed) == (15000, 16000, False)

    # --- pays from Tuesday on repay it: 3000, 3000, then the last 1000 --------------------
    expected = {TUE: (3000, 2000), WED: (3000, 2000), THU: (1000, 4000), FRI: (0, 5000)}
    for day, (deduction, net) in expected.items():
        assert phone.put_records(site_id, day, [record(yao, "present")]).status_code == 200
        assert phone.submit(site_id, day, new_uuid()).status_code == 200
        manager.approve(AttendanceDay.objects.get(site=crew.site, date=day).pk)
        line = lines_by_worker(phone.payout(site_id, day).json())[yao.pk]
        assert (line["gross"], line["advance_deduction"], line["net"]) == (5000, deduction, net)
        assert phone.pay(line["pay_line_id"], net, new_uuid()).status_code == 201

    advance.refresh_from_db()
    assert advance.balance == 0
    assert advance.status == Advance.Status.REPAID
    repaid = AdvanceRepayment.objects.filter(advance=advance).values_list("amount", flat=True)
    assert sorted(repaid) == [1000, 3000, 3000]
    assert sum(repaid) == advance.amount

    # Monday's pay (the day it was given) deducted nothing.
    assert phone.put_records(site_id, MON, [record(yao, "present")]).status_code == 200
    assert phone.submit(site_id, MON, new_uuid()).status_code == 200
    manager.approve(AttendanceDay.objects.get(site=crew.site, date=MON).pk)
    assert lines_by_worker(phone.payout(site_id, MON).json())[yao.pk]["advance_deduction"] == 0

    # --- once repaid, a new advance up to the limit is allowed, one unit over is not ------
    over_by_one = manager.post(advances_url, _advance_form(yao, "15 001", "3 000"))
    assert over_by_one.status_code == 200
    assert over_by_one.context["form"].errors["amount"]
    at_limit = manager.post(advances_url, _advance_form(yao, "15 000", "3 000"))
    assert at_limit.status_code == 302
    assert Advance.objects.filter(amount=15000).exists()
    assert AuditLog.objects.filter(action="payroll.advance.create").count() == 2


def test_only_managers_and_admins_give_advances():
    CompanySettingsFactory()
    crew = make_site("Hilltop", phone_number=PHONE)
    yao = hire(crew, "Yao", 5000)
    for user in (crew.foreman, UserFactory(role="accountant")):
        office = Office(user)
        response = office.post(reverse("payroll:advances"), _advance_form(yao, "1 000", "500"))
        assert response.status_code == 200
        assert response.context["form"].errors
    assert not Advance.objects.exists()
