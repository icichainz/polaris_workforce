"""Office screens for the pay rules (Wave 4): the Pay rules page and its forms (admins),
the day extras of a site and date (piece work, bonuses, stoppage), what the cash sheet
and the payslip show, permissions and another company's objects."""

import datetime
from decimal import Decimal

import pytest
from django.urls import reverse

from apps.core.models import AuditLog
from apps.core.tenancy import use_company
from apps.payroll import services
from apps.payroll.models import (
    Bonus,
    DeductionRule,
    PayRuleSettings,
    PieceWorkEntry,
    SitePayRules,
    StoppageDay,
    TaskType,
)
from tests.factories import (
    AdminUserFactory,
    CompanyFactory,
    CompanySettingsFactory,
    DeductionRuleFactory,
    TaskTypeFactory,
    UserFactory,
)
from tests.test_payroll_services import MON, hire, line_of, roll_call, setup_site
from tests.test_payroll_views import _pdf_text

pytestmark = pytest.mark.django_db


@pytest.fixture
def company(db, settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path
    return CompanySettingsFactory()


@pytest.fixture
def crew(company):
    site, manager, foreman = setup_site(name="Harbour Road")
    yao = hire(site, 6000, first_name="Yao", last_name="Mensah")
    roll_call(site, MON, {yao: "present"})
    return site, manager, foreman, yao


def extras_url(site, day=MON):
    return reverse("payroll:day_extras", args=[site.pk, day.isoformat()])


def text(response) -> str:
    return response.content.decode()


# --- pay rules page (admins) ---------------------------------------------------------------


def rules_data(**changes):
    data = {
        "sunday_multiplier": "1",
        "holiday_multiplier": "1",
        "night_start": "",
        "night_end": "",
        "night_multiplier": "1",
        "stoppage_pay": "percent",
        "stoppage_percent": "50",
        "stoppage_amount": "",
    }
    data.update(changes)
    return data


def test_pay_rules_page_and_save(client, crew):
    client.force_login(AdminUserFactory())
    DeductionRuleFactory(name="CNSS")
    TaskTypeFactory(name="Plastering", unit="m²", unit_rate=300)

    page = client.get(reverse("payroll:pay_rules"))
    assert page.status_code == 200
    for expected in (
        "Rate multipliers",
        "Statutory deductions",
        "CNSS",
        "Plastering",
        "Harbour Road",
    ):
        assert expected in text(page)

    response = client.post(
        reverse("payroll:pay_rules"),
        rules_data(
            sunday_multiplier="2",
            night_start="22:00",
            night_end="06:00",
            night_multiplier="1.25",
            stoppage_pay="fixed",
            stoppage_amount="2 500",
        ),
    )
    assert response.status_code == 302
    rules = PayRuleSettings.load()
    assert (rules.sunday_multiplier, rules.night_start, rules.stoppage_amount) == (
        Decimal("2"),
        datetime.time(22),
        2500,
    )
    entry = AuditLog.objects.get(action="payroll.rules.update")
    assert entry.after["sunday_multiplier"] == "2" and "holiday_multiplier" not in entry.after


@pytest.mark.parametrize(
    ("changes", "field"),
    [
        ({"night_start": "22:00"}, "night_end"),
        ({"night_start": "22:00", "night_end": "22:00"}, "night_end"),
        ({"sunday_multiplier": "0.5"}, "sunday_multiplier"),
        ({"stoppage_percent": "150"}, "stoppage_percent"),
    ],
)
def test_pay_rules_errors(client, crew, changes, field):
    client.force_login(AdminUserFactory())
    response = client.post(reverse("payroll:pay_rules"), rules_data(**changes))
    assert response.status_code == 200
    assert field in response.context["form"].errors
    assert not AuditLog.objects.filter(action="payroll.rules.update").exists()


@pytest.mark.parametrize("role", ["site_manager", "accountant", "foreman"])
def test_pay_rules_screens_are_admin_only(client, crew, role):
    site = crew[0]
    client.force_login(UserFactory(role=role))
    rule, task = DeductionRuleFactory(), TaskTypeFactory()
    urls = [
        reverse("payroll:pay_rules"),
        reverse("payroll:deduction_rule_create"),
        reverse("payroll:deduction_rule_edit", args=[rule.pk]),
        reverse("payroll:task_type_create"),
        reverse("payroll:task_type_edit", args=[task.pk]),
        reverse("payroll:site_pay_rules", args=[site.pk]),
    ]
    for url in urls:
        assert client.get(url).status_code == 403, url
        assert client.post(url, rules_data()).status_code == 403, url


def test_pay_periods_page_links_the_pay_rules_for_admins(client, crew):
    client.force_login(AdminUserFactory())
    assert reverse("payroll:pay_rules") in text(client.get(reverse("payroll:periods")))
    client.force_login(crew[1])
    assert reverse("payroll:pay_rules") not in text(client.get(reverse("payroll:periods")))


def test_create_and_edit_a_deduction_rule(client, crew):
    client.force_login(AdminUserFactory())
    data = {
        "name": "Social security",
        "employee_percent": "3.6",
        "employer_percent": "16.5",
        "base_floor": "1 000",
        "base_ceiling": "",
        "pay_types": ["daily", "piece"],
        "active_from": "2025-01-01",
        "active_to": "",
        "order": "1",
    }
    response = client.post(reverse("payroll:deduction_rule_create"), data)
    assert response.status_code == 302
    rule = DeductionRule.objects.get()
    assert (rule.base_floor, rule.base_ceiling, rule.pay_types) == (1000, None, ["daily", "piece"])
    assert AuditLog.objects.get(action="payroll.deduction_rule.create").target == rule

    data.update(employee_percent="4", base_ceiling="500")
    response = client.post(reverse("payroll:deduction_rule_edit", args=[rule.pk]), data)
    assert "base_ceiling" in response.context["form"].errors  # under the floor
    data.update(base_ceiling="200 000")
    client.post(reverse("payroll:deduction_rule_edit", args=[rule.pk]), data)
    rule.refresh_from_db()
    assert (rule.employee_percent, rule.base_ceiling) == (Decimal("4.00"), 200000)
    entry = AuditLog.objects.get(action="payroll.deduction_rule.update")
    assert set(entry.after) == {"employee_percent", "base_ceiling"}

    # The new rule is on the pay at once (pay not paid yet).
    assert services.cash_sheet(crew[0], MON).lines[0].line.statutory_deductions == 240

    duplicate = client.post(
        reverse("payroll:deduction_rule_create"), {**data, "name": "social SECURITY"}
    )
    assert "name" in duplicate.context["form"].errors


def test_create_and_edit_a_task(client, crew):
    client.force_login(AdminUserFactory())
    data = {"name": "Block laying", "unit": "block", "unit_rate": "75", "is_active": "on"}
    assert client.post(reverse("payroll:task_type_create"), data).status_code == 302
    task = TaskType.objects.get()
    assert task.unit_rate == 75 and task.company_id == crew[1].company_id
    client.post(reverse("payroll:task_type_edit", args=[task.pk]), {**data, "unit_rate": "80"})
    task.refresh_from_db()
    assert task.unit_rate == 80
    assert {"payroll.task_type.create", "payroll.task_type.update"} <= set(
        AuditLog.objects.values_list("action", flat=True)
    )


def test_site_pay_rules_form(client, crew):
    site = crew[0]
    client.force_login(AdminUserFactory())
    url = reverse("payroll:site_pay_rules", args=[site.pk])
    assert client.get(url).status_code == 200
    data = {
        "sunday_multiplier": "2",
        "holiday_multiplier": "",
        "night_start": "",
        "night_end": "",
        "night_multiplier": "",
    }
    assert client.post(url, data).status_code == 302
    assert SitePayRules.objects.get(site=site).sunday_multiplier == Decimal("2")
    assert AuditLog.objects.filter(action="payroll.site_rules.update").count() == 1
    bad = client.post(url, {**data, "night_end": "05:00"})
    assert "night_end" in bad.context["form"].errors


def test_another_companys_rules_tasks_and_sites_are_out_of_reach(client, crew):
    with use_company(CompanyFactory()):
        CompanySettingsFactory()
        rule, task = DeductionRuleFactory(name="Theirs"), TaskTypeFactory(name="Their task")
        theirs = setup_site(name="Their site")[0]
    client.force_login(AdminUserFactory())

    page = text(client.get(reverse("payroll:pay_rules")))
    assert "Theirs" not in page and "Their task" not in page and "Their site" not in page
    assert client.get(reverse("payroll:deduction_rule_edit", args=[rule.pk])).status_code == 404
    assert client.post(reverse("payroll:task_type_edit", args=[task.pk]), {}).status_code == 404
    url = reverse("payroll:site_pay_rules", args=[theirs.pk])
    assert client.post(url, {"sunday_multiplier": "9"}).status_code == 403
    assert not SitePayRules.objects.exists()


# --- day extras ----------------------------------------------------------------------------


def test_day_extras_add_piece_work_bonus_and_stoppage(client, crew):
    site, manager, _foreman, yao = crew
    task = TaskTypeFactory(name="Block laying", unit="block", unit_rate=50)
    client.force_login(manager)
    url = extras_url(site)
    assert client.get(url).status_code == 200

    response = client.post(
        url,
        {
            "action": "piece",
            "piece-worker": yao.pk,
            "piece-task": task.pk,
            "piece-quantity": "40",
            "piece-note": "Wall B",
        },
    )
    assert response.status_code == 302
    client.post(
        url,
        {
            "action": "bonus",
            "bonus-worker": yao.pk,
            "bonus-amount": "1 000",
            "bonus-reason": "Tidy",
        },
    )
    assert line_of(site, MON, yao).gross == 6000 + 2000 + 1000

    client.post(
        url,
        {
            "action": "stoppage",
            "stoppage-reason": "Rain from 10:00",
            "stoppage-pay": "percent",
            "stoppage-percent": "50",
        },
    )
    assert StoppageDay.objects.get(site=site, date=MON).reason == "Rain from 10:00"
    assert line_of(site, MON, yao).gross == 3000 + 2000 + 1000

    page = text(client.get(url))
    assert "Wall B" in page and "Tidy" in page and "Rain from 10:00" in page

    client.post(url, {"action": "clear_stoppage", "reason": "Wrong day"})
    assert not StoppageDay.objects.exists()
    entry = PieceWorkEntry.objects.get()
    client.post(reverse("payroll:piece_work_delete", args=[entry.pk]), {"reason": "x"})
    client.post(reverse("payroll:bonus_delete", args=[Bonus.objects.get().pk]))
    assert line_of(site, MON, yao).gross == 6000


def test_day_extras_form_errors_stay_on_the_page(client, crew):
    site, manager, _foreman, _yao = crew
    stranger = hire(setup_site()[0], 5000)
    client.force_login(manager)
    response = client.post(
        extras_url(site),
        {"action": "bonus", "bonus-worker": stranger.pk, "bonus-amount": "-5", "bonus-reason": ""},
    )
    assert response.status_code == 200
    errors = response.context["bonus_form"].errors
    assert {"worker", "amount", "reason"} <= set(errors)
    assert not Bonus.objects.exists()


def test_day_extras_are_read_only_for_foremen_and_paid_days(client, crew):
    site, manager, foreman, yao = crew
    client.force_login(foreman)
    page = client.get(extras_url(site))
    assert page.status_code == 200 and 'name="action"' not in text(page)
    assert client.post(extras_url(site), {"action": "stoppage"}).status_code == 403

    line = services.cash_sheet(site, MON).lines[0].line
    services.record_payment(manager, line, amount=line.net, proof_type="signature")
    client.force_login(manager)
    page = text(client.get(extras_url(site)))
    assert "Read only." in page and 'value="stoppage"' not in page
    response = client.post(
        extras_url(site),
        {"action": "stoppage", "stoppage-reason": "Rain", "stoppage-pay": "percent"},
    )
    assert response.status_code == 302 and not StoppageDay.objects.exists()


def test_day_extras_of_another_site_are_403(client, crew):
    site = crew[0]
    other_manager = setup_site()[1]
    client.force_login(other_manager)
    assert client.get(extras_url(site)).status_code == 403
    assert client.post(extras_url(site), {"action": "stoppage"}).status_code == 403


# --- cash sheet and payslip ----------------------------------------------------------------


def test_cash_sheet_and_payslip_show_the_rules(client, crew):
    site, manager, _foreman, yao = crew
    DeductionRuleFactory(name="CNSS", employee_percent=Decimal("5"), employer_percent=Decimal("10"))
    task = TaskTypeFactory(name="Block laying", unit="block", unit_rate=50)
    client.force_login(manager)
    client.post(
        extras_url(site),
        {"action": "piece", "piece-worker": yao.pk, "piece-task": task.pk, "piece-quantity": "20"},
    )
    client.post(
        extras_url(site),
        {
            "action": "bonus",
            "bonus-worker": yao.pk,
            "bonus-amount": "500",
            "bonus-reason": "Tidy site",
        },
    )

    page = text(client.get(reverse("payroll:cash_sheet", args=[site.pk, MON.isoformat()])))
    line = line_of(site, MON, yao)
    assert (line.gross, line.statutory_deductions, line.net) == (7500, 375, 7125)
    assert "CNSS" in page and extras_url(site) in page
    assert "incl. piece work 1 000" in page and "incl. bonus 500" in page

    pdf = _pdf_text(client.get(reverse("payroll:payslip", args=[line.pk])).content)
    for expected in ("CNSS \\(5 %\\)", "Block laying", "Piece work", "Bonus", "Tidy site"):
        assert expected in pdf, expected
