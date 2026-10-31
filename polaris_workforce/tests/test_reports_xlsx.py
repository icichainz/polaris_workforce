"""Reports: the Excel export (read back with openpyxl)."""

import datetime
import io
import zipfile

import openpyxl
import pytest
from django.urls import reverse

from apps.core.models import CompanySettings
from tests import factories as f
from tests import test_reports_views as base
from tests.test_reports_views import CSV_HEADER

pytestmark = pytest.mark.django_db

XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
seeded = base.seeded  # the same data as the CSV tests: October 2026, two sites


def workbook_of(response):
    assert response.status_code == 200
    assert response["Content-Type"] == XLSX
    return openpyxl.load_workbook(io.BytesIO(response.content))


def values(sheet):
    return [[cell.value for cell in row] for row in sheet.iter_rows()]


def test_one_sheet_per_section_with_the_csv_columns(client, seeded):
    client.force_login(seeded["admin"])

    response = client.get(reverse("dashboard:reports_xlsx"))

    assert response["Content-Disposition"] == (
        'attachment; filename="polaris-report-2026-10-01-2026-10-31.xlsx"'
    )
    workbook = workbook_of(response)
    assert workbook.sheetnames == ["By worker", "By site", "By week", "Employer cost"]
    rows = values(workbook["By worker"])
    assert rows[0] == CSV_HEADER
    body, total = rows[1:-1], rows[-1]
    assert [row[0] for row in body] == ["W-0004", "W-0001", "W-0002", "W-0003"]
    assert body[1] == [
        "W-0001",
        "Akouvi Tete",
        "Mason",
        "Riverside",
        2,
        0,
        1,
        0,
        3,
        1,
        15000,
        0,
        0,
        "XOF",
    ]
    assert body[2][4:11] == [1, 1, 0, 1, 1.5, 0.5, 8000]
    assert total == [None, "Total", None, None, 4, 1, 1, 2, 5.5, 0.69, 29000, 0, 0, "XOF"]


def test_numbers_are_real_numbers_with_formats(client, seeded):
    client.force_login(seeded["admin"])

    sheet = workbook_of(client.get(reverse("dashboard:reports_xlsx")))["By worker"]

    earned, rate, days = sheet["K3"], sheet["J3"], sheet["I4"]
    assert (earned.data_type, earned.number_format) == ("n", "#,##0")
    assert (rate.data_type, rate.number_format) == ("n", "0%")
    assert days.data_type == "n" and days.value == 1.5
    assert all(cell.font.bold for cell in sheet[sheet.max_row] if cell.value is not None)
    assert not sheet["A2"].font.bold
    assert sheet.freeze_panes == "A2"


def test_by_site_and_by_week_sheets(client, seeded):
    client.force_login(seeded["admin"])

    workbook = workbook_of(client.get(reverse("dashboard:reports_xlsx")))

    assert values(workbook["By site"]) == [
        ["Site", "Labour cost", "Labour budget", "Currency"],
        ["Riverside", 23000, 1000000, "XOF"],
        ["Route 4 Bridge", 6000, 25000000, "XOF"],
        ["Total", 29000, None, "XOF"],
    ]
    weeks = values(workbook["By week"])
    assert weeks[0] == ["Year", "Week", "Week starting", "Attendance rate (%)"]
    assert [row[:2] + [row[3]] for row in weeks[1:-1]] == [
        [2026, 40, None],
        [2026, 41, 0.58],
        [2026, 42, 1],
        [2026, 43, None],
        [2026, 44, None],
    ]
    assert weeks[1][2] == datetime.datetime(2026, 9, 28)
    assert workbook["By week"]["C2"].number_format == "yyyy-mm-dd"
    assert weeks[-1] == ["Total", None, None, 0.69]


def test_two_decimal_currency(client, seeded):
    company = CompanySettings.load()
    company.currency, company.currency_decimals = "EUR", 2
    company.save()
    client.force_login(seeded["admin"])

    sheet = workbook_of(client.get(reverse("dashboard:reports_xlsx")))["By worker"]

    total = sheet[sheet.max_row]
    assert (total[10].value, total[13].value) == (290, "EUR")
    assert total[10].number_format == "#,##0.00"


def test_typed_text_is_never_a_formula(client, seeded):
    period = f.PayPeriodFactory(
        site=seeded["riverside"],
        start_date=datetime.date(2026, 10, 20),
        end_date=datetime.date(2026, 10, 20),
    )
    for number, first, last in [("W-0009", '=HYPERLINK("http://x")', "@x"), ("W-0010", "+1", "-1")]:
        worker = f.WorkerFactory(number=number, first_name=first, last_name=last)
        f.PayLineFactory(period=period, worker=worker, gross=1000)
    seeded["riverside"].name = "=cmd|' /C calc'!A0"
    seeded["riverside"].save()
    client.force_login(seeded["admin"])

    response = client.get(reverse("dashboard:reports_xlsx"))

    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        sheets = [name for name in archive.namelist() if name.startswith("xl/worksheets/")]
        assert sheets
        for name in sheets:
            assert b"<f>" not in archive.read(name)  # no formula anywhere
    workbook = workbook_of(response)
    names = [row[1] for row in values(workbook["By worker"])]
    assert "+1 -1" in names
    assert '=HYPERLINK("http://x") @x' in names
    sites = [row[0] for row in values(workbook["By site"])]
    assert "=cmd|' /C calc'!A0" in sites


def test_roles_and_scope(client, seeded):
    client.force_login(f.ForemanFactory())
    assert client.get(reverse("dashboard:reports_xlsx")).status_code == 403

    client.force_login(seeded["manager_a"])
    workbook = workbook_of(client.get(reverse("dashboard:reports_xlsx")))
    sites = [row[0] for row in values(workbook["By site"])[1:-1]]
    workers = {row[3] for row in values(workbook["By worker"])[1:-1]}
    assert sites == ["Riverside"]
    assert workers == {"Riverside"}

    response = client.get(reverse("dashboard:reports_xlsx"), {"site": seeded["bridge"].pk})
    assert "Route 4 Bridge" not in str(values(workbook_of(response)["By site"]))


def test_the_filters_apply(client, seeded):
    client.force_login(seeded["admin"])
    query = {"period": "custom", "start": "2026-10-12", "end": "2026-10-12"}

    rows = values(workbook_of(client.get(reverse("dashboard:reports_xlsx"), query))["By worker"])

    assert [row[0] for row in rows[1:-1]] == ["W-0001", "W-0002"]


def test_reports_page_links_both_exports_with_the_filters(client, seeded):
    client.force_login(seeded["admin"])

    html = client.get(reverse("dashboard:reports"), {"period": "month"}).content.decode()

    assert f'href="{reverse("dashboard:reports_xlsx")}?period=month"' in html
    assert f'href="{reverse("dashboard:reports_csv")}?period=month"' in html
