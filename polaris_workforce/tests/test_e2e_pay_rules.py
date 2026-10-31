"""End to end: a public holiday with pay rules, from the admin's Pay rules screen to the
locked cash sheet.

admin sets the rules (holiday × 2, a 5 % / 10 % statutory deduction, a piece-work task)
→ foreman's roll call on the phone → manager enters piece work and a bonus (Day extras)
→ preview leaves the piece work out → manager approves → the phone's payout has the
exact nets → everyone paid over the API → accountant locks → employer cost for reports,
payslip with the deduction.
"""

import pytest
from django.core.cache import cache
from django.urls import reverse

from apps.accounts.models import User
from apps.attendance.models import AttendanceDay
from apps.core.models import AuditLog
from apps.payroll import pay_rules
from apps.payroll import services as payroll_services
from apps.payroll.models import DeductionRule, PayLine, TaskType
from tests.e2e import Office, Phone, admin_office, hire, lines_by_worker, make_site, new_uuid
from tests.e2e import record as mark
from tests.factories import CompanySettingsFactory, UserFactory
from tests.test_payroll_views import _pdf_text

pytestmark = pytest.mark.django_db

PIN = "7395"
PHONE = "+228 90 55 66 78"


@pytest.fixture(autouse=True)
def _isolated(settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path
    cache.clear()
    yield
    cache.clear()


def test_a_public_holiday_with_pay_rules():
    settings = CompanySettingsFactory(working_days=[*range(1, 8)])
    today = payroll_services.company_today(settings)
    settings.public_holidays = [{"date": today.isoformat(), "name": "Fête nationale"}]
    settings.save()

    # --- the admin sets the pay rules -------------------------------------------------------
    admin = admin_office()
    saved = admin.post(
        reverse("payroll:pay_rules"),
        {
            "sunday_multiplier": "1",
            "holiday_multiplier": "2",
            "night_multiplier": "1",
            "stoppage_pay": "percent",
            "stoppage_percent": "50",
        },
    )
    assert saved.status_code == 302
    created = admin.post(
        reverse("payroll:deduction_rule_create"),
        {
            "name": "Social security",
            "employee_percent": "5",
            "employer_percent": "10",
            "active_from": today.isoformat(),
            "order": "1",
        },
    )
    assert created.status_code == 302
    admin.post(
        reverse("payroll:task_type_create"),
        {"name": "Block laying", "unit": "block", "unit_rate": "50", "is_active": "on"},
    )
    task = TaskType.objects.get()

    crew = make_site("Lagoon Bridge", phone_number=PHONE, pin=PIN, office=admin)
    ama = hire(crew, "Ama", 6000)
    kofi = hire(crew, "Kofi", 5000, "piece")  # a piece-rate worker
    site_id = crew.site.pk

    # --- the roll call on the phone ---------------------------------------------------------
    phone = Phone(PHONE, PIN)
    put = phone.put_records(site_id, today, [mark(ama, "present"), mark(kofi, "present")])
    assert put.status_code == 200, put.content
    assert phone.submit(site_id, today, new_uuid()).status_code == 200

    # --- the manager enters the day extras --------------------------------------------------
    manager = Office(crew.manager)
    extras = reverse("payroll:day_extras", args=[site_id, today.isoformat()])
    added = manager.post(
        extras,
        {
            "action": "piece",
            "piece-worker": kofi.pk,
            "piece-task": task.pk,
            "piece-quantity": "100",
        },
    )
    assert added.status_code == 302
    manager.post(
        extras,
        {
            "action": "bonus",
            "bonus-worker": ama.pk,
            "bonus-amount": "1000",
            "bonus-reason": "Safety",
        },
    )

    preview = lines_by_worker(phone.payout(site_id, today).json())
    assert preview[ama.pk]["gross"] == 12000 + 1000
    assert preview[kofi.pk]["gross"] == 0  # piece work waits for the approval

    # --- approval and the payout --------------------------------------------------------------
    manager.approve(AttendanceDay.objects.get(site=crew.site, date=today).pk)
    payout = phone.payout(site_id, today).json()
    assert payout["status"] == "open"
    lines = lines_by_worker(payout)
    # Ama: 6000 × 2 (holiday) + 1000 bonus = 13000; 5 % = 650 → 12350.
    # Kofi: 100 blocks × 50 = 5000 (no multiplier on pieces); 5 % = 250 → 4750.
    assert {w: (lines[w.pk]["gross"], lines[w.pk]["net"]) for w in (ama, kofi)} == {
        ama: (13000, 12350),
        kofi: (5000, 4750),
    }
    assert payout["total_to_pay"] == 17100
    assert PayLine.objects.get(worker=ama).days[0]["day_kind"] == "holiday"

    # --- everyone paid over the API, then the accountant locks --------------------------------
    for worker in (ama, kofi):
        line = lines[worker.pk]
        assert phone.pay(line["pay_line_id"], line["net"], new_uuid()).status_code == 201
    accountant = Office(UserFactory(role=User.Role.ACCOUNTANT))
    assert accountant.lock_sheet(site_id, today).status_code == 302
    assert phone.payout(site_id, today).json()["status"] == "closed"

    # --- the employer cost for the reports and the payslip ------------------------------------
    cost = pay_rules.employer_costs(payroll_services.scoped_sites(crew.manager), today, today)
    assert (cost.gross, cost.employee, cost.employer, cost.total_cost) == (18000, 900, 1800, 19800)
    assert [(r.name, r.employee, r.employer) for r in cost.rules] == [
        ("Social security", 900, 1800)
    ]
    line = PayLine.objects.get(worker=ama)
    pdf = _pdf_text(manager.get(reverse("payroll:payslip", args=[line.pk])).content)
    assert "Social security \\(5 %\\)" in pdf and "Safety" in pdf

    actions = set(AuditLog.objects.values_list("action", flat=True))
    assert {
        "payroll.rules.update",
        "payroll.deduction_rule.create",
        "payroll.task_type.create",
        "payroll.piece.create",
        "payroll.bonus.create",
        "payroll.period.lock",
    } <= actions
    assert DeductionRule.objects.get().company_id == crew.site.company_id
