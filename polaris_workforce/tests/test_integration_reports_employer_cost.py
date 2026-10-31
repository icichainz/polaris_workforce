"""Reports show the employer cost and the statutory deductions (Wave 4 integration): the
tiles and the per-rule table, the per-worker deductions, the CSV and the Excel export —
scoped to the viewer's sites and company."""

import csv
import datetime
import io
from decimal import Decimal

import openpyxl
import pytest
from django.urls import reverse

from apps.core.tenancy import use_company
from apps.payroll import services
from apps.payroll.models import DeductionRule
from tests.factories import (
    AdminUserFactory,
    CompanyFactory,
    CompanySettingsFactory,
    SiteManagerFactory,
)
from tests.test_payroll_services import MON, hire, roll_call, setup_site

pytestmark = pytest.mark.django_db

SPAN = {"period": "custom", "start": MON.isoformat(), "end": MON.isoformat()}


def rule(name, employee, employer, order=1):
    return DeductionRule.objects.create(
        name=name,
        employee_percent=Decimal(employee),
        employer_percent=Decimal(employer),
        active_from=datetime.date(2025, 1, 1),
        order=order,
    )


@pytest.fixture
def world():
    CompanySettingsFactory()
    rule("Social security", "4", "16", order=1)
    rule("Health insurance", "1", "2", order=2)
    site, manager, _foreman = setup_site(name="Market Hall")
    yao = hire(site, 10000, first_name="Yao", last_name="Mensah")
    afi = hire(site, 5000, first_name="Afi", last_name="Dosseh")
    roll_call(site, MON, {yao: "present", afi: "present"})
    services.cash_sheet(site, MON)
    # Another company with the same rule and pay: never counted.
    with use_company(CompanyFactory()):
        CompanySettingsFactory()
        rule("Social security", "4", "16")
        other, _m, _f = setup_site(name="Elsewhere")
        roll_call(other, MON, {hire(other, 90000): "present"})
        services.cash_sheet(other, MON)
    return {"site": site, "manager": manager, "admin": AdminUserFactory()}


def test_the_report_has_the_employer_cost_per_rule(client, world):
    client.force_login(world["admin"])
    response = client.get(reverse("dashboard:reports"), SPAN)

    report = response.context["report"]
    cost = report.employer_cost
    assert (report.labour_cost, cost.gross) == (15000, 15000)
    assert (cost.employee, cost.employer) == (750, 2700)  # 5 % and 18 % of 15 000
    assert [(r.name, r.employee, r.employer) for r in cost.rules] == [
        ("Social security", 600, 2400),
        ("Health insurance", 150, 300),
    ]
    assert report.employer_total == 17700
    by_name = {row.name: row for row in report.workers}
    assert (by_name["Yao Mensah"].statutory, by_name["Yao Mensah"].employer) == (500, 1800)
    html = response.content.decode()
    assert 'id="employer-cost"' in html
    assert "Employer cost" in html and "Social security" in html and "Health insurance" in html
    assert "17 700" in html and "Statutory deductions" in html


def test_the_csv_and_excel_exports_carry_the_deductions(client, world):
    client.force_login(world["admin"])

    rows = list(
        csv.reader(
            io.StringIO(
                client.get(reverse("dashboard:reports_csv"), SPAN).content.decode("utf-8-sig")
            )
        )
    )
    header, total = rows[0], rows[-1]
    assert total[header.index("Statutory deductions")] == "750"
    assert total[header.index("Employer contributions")] == "2700"

    book = openpyxl.load_workbook(
        io.BytesIO(client.get(reverse("dashboard:reports_xlsx"), SPAN).content)
    )
    sheet = [[cell.value for cell in row] for row in book["Employer cost"].iter_rows()]
    assert sheet[0] == ["Deduction rule", "Deducted from pay", "Employer contributions", "Currency"]
    assert sheet[1] == ["Social security", 600, 2400, "XOF"]
    assert sheet[2] == ["Health insurance", 150, 300, "XOF"]
    assert sheet[3] == ["Total", 750, 2700, "XOF"]
    assert sheet[-1][:2] == ["Employer cost (labour cost + employer contributions)", 17700]


def test_a_manager_of_another_site_sees_no_deductions(client, world):
    client.force_login(SiteManagerFactory())
    report = client.get(reverse("dashboard:reports"), SPAN).context["report"]

    assert (report.employer_cost.employee, report.employer_cost.employer) == (0, 0)
    assert report.employer_cost.rules == []


def test_the_site_manager_sees_their_site(client, world):
    client.force_login(world["manager"])
    report = client.get(reverse("dashboard:reports"), SPAN).context["report"]

    assert report.employer_cost.employer == 2700
