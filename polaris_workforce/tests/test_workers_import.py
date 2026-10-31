"""Excel / CSV import of workers: template, preview, confirm, limits, safety, scoping."""

import datetime
import io
import zipfile
from unittest import mock

import openpyxl
import pytest
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse

from apps.core.models import AuditLog
from apps.core.tenancy import use_company
from apps.workers import importer
from apps.workers.models import PayRate, Trade, Worker
from apps.worksites.models import SiteAssignment
from tests.e2e import Office
from tests.factories import (
    AdminUserFactory,
    CompanyFactory,
    CompanySettingsFactory,
    ForemanFactory,
    SiteFactory,
    SiteManagerFactory,
    TradeFactory,
    UserFactory,
    WorkerFactory,
)

pytestmark = pytest.mark.django_db

XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
HEADER = [
    "First name",
    "Last name",
    "Phone",
    "Trade",
    "Site",
    "Start date",
    "Pay type",
    "Rate",
    "ID document type",
    "ID document number",
    "Payment method",
]


def row(**values):
    defaults = {
        "First name": "Kofi",
        "Last name": "Mensah",
        "Phone": "+228 90 11 11 11",
        "Trade": "Mason",
        "Site": "Riverside",
        "Start date": "2026-10-01",
        "Pay type": "Daily",
        "Rate": "6000",
        "ID document type": "National ID card",
        "ID document number": "TG-0001",
        "Payment method": "",
    }
    defaults.update(values)
    return [defaults[name] for name in HEADER]


def xlsx_file(rows, header=HEADER, name="workers.xlsx"):
    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet.append(header)
    for values in rows:
        sheet.append(values)
    buffer = io.BytesIO()
    workbook.save(buffer)
    return SimpleUploadedFile(name, buffer.getvalue(), content_type=XLSX)


def csv_file(rows, header=HEADER, delimiter=",", name="workers.csv", encoding="utf-8-sig"):
    lines = [delimiter.join(header)] + [delimiter.join(values) for values in rows]
    return SimpleUploadedFile(name, "\n".join(lines).encode(encoding), content_type="text/csv")


@pytest.fixture
def world(db):
    CompanySettingsFactory(currency_decimals=0)
    manager = SiteManagerFactory()
    riverside = SiteFactory(name="Riverside", manager=manager)
    other = SiteFactory(name="Route 4 Bridge")
    TradeFactory(name="Mason")
    return {"manager": manager, "riverside": riverside, "other": other, "admin": AdminUserFactory()}


def upload(office, file):
    return office.post(reverse("workers:import"), {"file": file})


def confirm(office, response):
    token = response.context["preview"]["token"]
    return office.post(reverse("workers:import_confirm"), {"token": token})


def preview_errors(response) -> dict[int, list[str]]:
    return {r["line"]: r["errors"] for r in response.context["preview"]["rows"] if r["errors"]}


# --- template ----------------------------------------------------------------------------


def test_template_has_the_columns_lists_and_drop_downs(world):
    office = Office(world["admin"])
    response = office.get(reverse("workers:import_template"))

    assert response.status_code == 200
    assert response["Content-Type"] == XLSX
    assert "attachment" in response["Content-Disposition"]
    workbook = openpyxl.load_workbook(io.BytesIO(response.content))
    sheet = workbook.worksheets[0]
    assert [cell.value for cell in sheet[1]] == HEADER
    lists = workbook["Lists"]
    values = {cell.value for column in lists.iter_cols() for cell in column}
    assert {"Riverside", "Route 4 Bridge", "Mason", "Daily rate", "National ID card"} <= values
    ranges = {str(v.sqref) for v in sheet.data_validations.dataValidation}
    assert "E2:E501" in ranges  # site drop-down
    assert "Instructions" in workbook.sheetnames


def test_template_round_trip_imports_every_row(world):
    office = Office(world["admin"])
    template = office.get(reverse("workers:import_template"))
    workbook = openpyxl.load_workbook(io.BytesIO(template.content))
    sheet = workbook.worksheets[0]
    filled = [
        row(),
        row(
            **{
                "First name": "Ama",
                "Phone": "",
                "Trade": "Tiler",
                "Start date": datetime.date(2026, 10, 5),
                "Pay type": "Hourly",
                "Rate": 750,
                "ID document type": "",
                "ID document number": "",
                "Payment method": "Mobile money",
            }
        ),
    ]
    for line, values in enumerate(filled, 2):  # typed under the headers, as a person would
        for column, value in enumerate(values, 1):
            sheet.cell(row=line, column=column, value=value or None)
    buffer = io.BytesIO()
    workbook.save(buffer)
    file = SimpleUploadedFile("template.xlsx", buffer.getvalue(), content_type=XLSX)

    preview = upload(office, file)

    assert preview.status_code == 200
    assert preview.context["preview"]["ok"], preview_errors(preview)
    assert preview.context["preview"]["total"] == 2
    assert Worker.objects.count() == 0  # the preview saves nothing
    assert not Trade.objects.filter(name="Tiler").exists()
    notes = [r["notes"] for r in preview.context["preview"]["rows"]]
    assert notes[1] == ["New trade “Tiler” (created on import)."]

    done = confirm(office, preview)

    assert done.status_code == 200
    kofi, ama = Worker.objects.order_by("number")
    assert (kofi.number, kofi.first_name, kofi.phone) == ("W-0001", "Kofi", "+228 90 11 11 11")
    assert kofi.trade.name == "Mason"
    assert kofi.id_document_type == Worker.IdDocumentType.NATIONAL_ID
    assert kofi.id_document_number == "TG-0001"
    assert kofi.payment_method is None  # inherited
    assert (ama.number, ama.trade.name, ama.payment_method) == ("W-0002", "Tiler", "mobile_money")
    rate = PayRate.objects.get(worker=ama)
    assert (rate.amount, rate.pay_type, rate.effective_from) == (
        750,
        "hourly",
        datetime.date(2026, 10, 5),
    )
    assignment = SiteAssignment.objects.get(worker=kofi)
    assert (assignment.site, assignment.start_date) == (
        world["riverside"],
        datetime.date(2026, 10, 1),
    )

    creates = AuditLog.objects.filter(action="workers.create")
    assert creates.count() == 2
    assert {entry.reason for entry in creates} == {"Imported from template.xlsx"}
    summary = AuditLog.objects.get(action="workers.import")
    assert summary.after["count"] == 2
    assert summary.after["numbers"] == ["W-0001", "W-0002"]
    assert summary.after["new_trades"] == ["Tiler"]
    assert summary.actor == world["admin"]
    assert [item["worker"] for item in done.context["summary"]["workers"]] == [kofi, ama]
    assert "workers_import" not in office.client.session


def test_csv_with_semicolons_keys_and_any_column_order(world):
    office = Office(world["admin"])
    header = ["site", "rate", "last_name", "first_name"]
    file = csv_file([["riverside", "5 500", "Doe", "Jane"]], header=header, delimiter=";")

    preview = upload(office, file)

    assert preview.context["preview"]["ok"], preview_errors(preview)
    confirm(office, preview)
    jane = Worker.objects.get()
    assert (jane.full_name, jane.trade, jane.phone) == ("Jane Doe", None, "")
    assert PayRate.objects.get(worker=jane).amount == 5500
    assert SiteAssignment.objects.get(worker=jane).site == world["riverside"]


def test_windows_1252_csv_is_read(world):
    office = Office(world["admin"])
    file = csv_file([row(**{"First name": "Agbéko"})], encoding="cp1252")

    preview = upload(office, file)

    assert preview.context["preview"]["rows"][0]["name"] == "Agbéko Mensah"


# --- validation --------------------------------------------------------------------------


def test_row_errors_are_shown_and_nothing_is_saved(world):
    office = Office(world["admin"])
    rows = [
        row(),
        row(**{"First name": "", "Phone": "+228 90 22 22 22", "ID document number": "A2"}),
        row(**{"Site": "Nowhere", "Phone": "", "ID document number": ""}),
        row(**{"Start date": "31/02/2026", "Phone": "", "ID document number": ""}),
        row(**{"Rate": "12.5", "Phone": "", "ID document number": ""}),
        row(**{"Pay type": "Weekly", "Phone": "", "ID document number": ""}),
        row(**{"ID document type": "", "Phone": "", "ID document number": "X9"}),
        row(**{"Phone": "call me", "ID document number": ""}),
        row(**{"Payment method": "Cheque", "Phone": "", "ID document number": ""}),
    ]

    response = upload(office, xlsx_file(rows))

    errors = preview_errors(response)
    assert set(errors) == {3, 4, 5, 6, 7, 8, 9, 10}  # row 2 (the first worker) is fine
    assert errors[3] == ["First name is required."]
    assert "Unknown site “Nowhere”" in errors[4][0]
    assert "is not a date" in errors[5][0]
    assert errors[6] == ["Rate “12.5”: Use at most 0 decimal place(s)."]
    assert errors[7] == ["Unknown pay type “Weekly”."]
    assert errors[8] == ["Give the ID document type for the ID number."]
    assert errors[9] == ["The phone number “call me” is not valid."]
    assert errors[10] == ["Unknown payment method “Cheque”."]
    assert response.context["preview"]["ok"] is False
    assert reverse("workers:import_confirm") not in response.content.decode()
    assert Worker.objects.count() == 0
    assert not AuditLog.objects.exists()


def test_duplicates_within_the_file_and_the_company(world):
    WorkerFactory(number="W-0042", phone="+228 90 33 33 33", id_document_number="TG 777")
    office = Office(world["admin"])
    rows = [
        row(**{"Phone": "22890333333", "ID document number": "A1"}),  # taken in the company
        row(**{"Phone": "+228 90 44 44 44", "ID document number": "tg-777"}),  # taken
        row(**{"Phone": "+228 90 55 55 55", "ID document number": "B1"}),
        row(**{"Phone": "+228 90-55-55-55", "ID document number": "B 1"}),  # same as row 4
    ]

    errors = preview_errors(upload(office, xlsx_file(rows)))

    assert errors[2] == ["A worker of the company already has this phone number (W-0042)."]
    assert errors[3] == ["A worker of the company already has this ID number (W-0042)."]
    assert 4 not in errors
    assert errors[5] == ["Same phone number as row 4.", "Same ID number as row 4."]


def test_a_site_manager_is_not_told_the_number_of_a_worker_they_cannot_see(world):
    WorkerFactory(number="W-0042", phone="+228 90 33 33 33")
    office = Office(world["manager"])

    errors = preview_errors(upload(office, xlsx_file([row(**{"Phone": "+228 90 33 33 33"})])))

    assert errors[2] == ["A worker of the company already has this phone number."]


def test_another_companys_workers_are_not_duplicates(world):
    with use_company(CompanyFactory()):
        WorkerFactory(phone="+228 90 11 11 11", id_document_number="TG-0001")
    office = Office(world["admin"])

    response = upload(office, xlsx_file([row()]))

    assert response.context["preview"]["ok"], preview_errors(response)


def test_existing_trade_is_matched_without_case(world):
    office = Office(world["admin"])

    preview = upload(office, xlsx_file([row(**{"Trade": "  mason "})]))
    confirm(office, preview)

    assert Worker.objects.get().trade == Trade.objects.get(name="Mason")
    assert Trade.objects.count() == 1


# --- limits and file checks ----------------------------------------------------------------


def test_too_many_rows_is_refused(world, monkeypatch):
    monkeypatch.setattr(importer, "MAX_ROWS", 2)
    office = Office(world["admin"])
    rows = [
        row(**{"Phone": f"+228 90 00 00 0{n}", "ID document number": f"N{n}"}) for n in range(3)
    ]

    response = upload(office, xlsx_file(rows))

    assert response.status_code == 400
    assert "more than 2 workers" in response.context["form"].errors["file"][0]
    assert "workers_import" not in office.client.session


def test_too_large_file_is_refused(world, monkeypatch):
    monkeypatch.setattr(importer, "MAX_FILE_SIZE", 100)
    office = Office(world["admin"])

    response = upload(office, xlsx_file([row()]))

    assert response.status_code == 400
    assert "too large" in response.context["form"].errors["file"][0]


def test_a_zip_bomb_is_refused_before_it_is_opened(world, monkeypatch):
    monkeypatch.setattr(importer, "MAX_UNCOMPRESSED_SIZE", 1000)
    office = Office(world["admin"])

    response = upload(office, xlsx_file([row()]))

    assert "too much data" in response.context["form"].errors["file"][0]


@pytest.mark.parametrize(
    ("name", "content", "message"),
    [
        ("workers.txt", b"First name\nKofi", "Allowed extensions"),
        ("workers.xlsx", b"<html>not a workbook</html>", "not an Excel workbook"),
        ("workers.xlsx", b"PK\x03\x04garbage", "could not be read"),
        ("workers.csv", b"", "empty"),
        ("workers.csv", b"First name,Last name,Site,Rate\n", "no worker rows"),
        ("workers.csv", b"First name,Last name\nKofi,Mensah", "Required columns are missing"),
        ("workers.csv", b"First name,Last name,Site,Rate,Salary\nA,B,Riverside,1,2", "Unknown"),
        ("workers.csv", b"Site,Rate,Site,First name,Last name\nA,1,A,B,C", "appears twice"),
    ],
)
def test_bad_files_are_refused(world, name, content, message):
    office = Office(world["admin"])

    response = upload(office, SimpleUploadedFile(name, content))

    assert response.status_code == 400
    assert message in " ".join(response.context["form"].errors["file"])
    assert Worker.objects.count() == 0


def test_a_real_zip_that_is_not_a_workbook_is_refused(world):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("hello.txt", "hi")
    office = Office(world["admin"])

    response = upload(office, SimpleUploadedFile("workers.xlsx", buffer.getvalue()))

    assert "could not be read" in response.context["form"].errors["file"][0]


# --- formula injection ---------------------------------------------------------------------


@pytest.mark.parametrize("value", ['=HYPERLINK("http://x")', "+Kofi", "-1+2", "@SUM(A1)"])
def test_text_that_spreadsheets_would_run_is_refused(world, value):
    office = Office(world["admin"])

    errors = preview_errors(upload(office, csv_file([row(**{"Last name": value})])))

    assert errors[2] == [
        f"Last name starts with “{value[0]}”, which spreadsheets run as a formula."
    ]


def test_formula_cells_in_a_workbook_are_refused(world):
    office = Office(world["admin"])
    rows = [row(**{"First name": '=CONCAT("Ko","fi")', "Trade": "=1+1"})]

    errors = preview_errors(upload(office, xlsx_file(rows)))

    assert len(errors[2]) == 2
    assert all("formula" in error for error in errors[2])


def test_a_phone_may_start_with_plus(world):
    office = Office(world["admin"])

    response = upload(office, csv_file([row(**{"Phone": "+228 90 12 34 56"})]))

    assert response.context["preview"]["ok"]


# --- confirm: all or nothing ---------------------------------------------------------------


def test_confirm_creates_nothing_if_one_worker_fails(world):
    office = Office(world["admin"])
    rows = [
        row(),
        row(**{"First name": "Ama", "Phone": "+228 90 99 99 99", "ID document number": "Z"}),
    ]
    preview = upload(office, xlsx_file(rows))
    assert preview.context["preview"]["ok"]
    real = importer.services.create_worker
    calls = []

    def flaky(**kwargs):
        calls.append(kwargs["first_name"])
        if len(calls) == 2:
            raise ValidationError("The disk is full.")
        return real(**kwargs)

    with mock.patch.object(importer.services, "create_worker", side_effect=flaky):
        response = confirm(office, preview)

    assert calls == ["Kofi", "Ama"]
    assert Worker.objects.count() == 0  # Kofi was rolled back too
    assert not SiteAssignment.objects.exists()
    assert not AuditLog.objects.exists()
    assert response.context["preview"]["ok"] is False
    assert preview_errors(response) == {3: ["The disk is full."]}


def test_confirm_checks_again_and_refuses_a_phone_taken_since_the_preview(world):
    office = Office(world["admin"])
    preview = upload(office, xlsx_file([row()]))
    WorkerFactory(phone="+228 90 11 11 11")

    response = confirm(office, preview)

    assert response.status_code == 200
    assert Worker.objects.count() == 1
    assert "already has this phone number" in preview_errors(response)[2][0]
    assert not AuditLog.objects.filter(action="workers.import").exists()


def test_confirm_needs_the_preview_of_this_session(world):
    office = Office(world["admin"])
    upload(office, xlsx_file([row()]))

    stale = office.post(reverse("workers:import_confirm"), {"token": "forged"})
    other = Office(AdminUserFactory()).post(reverse("workers:import_confirm"), {"token": "x"})

    assert stale.status_code == other.status_code == 302
    assert stale["Location"] == reverse("workers:import")
    assert Worker.objects.count() == 0


def test_a_second_confirm_does_not_import_twice(world):
    office = Office(world["admin"])
    preview = upload(office, xlsx_file([row()]))

    confirm(office, preview)
    again = confirm(office, preview)

    assert again.status_code == 302
    assert Worker.objects.count() == 1


# --- who may import ------------------------------------------------------------------------


@pytest.mark.parametrize("role", ["foreman", "accountant"])
def test_only_admins_and_site_managers_may_import(world, role):
    user = ForemanFactory() if role == "foreman" else UserFactory(role="accountant")
    office = Office(user)

    assert office.get(reverse("workers:import")).status_code == 403
    assert office.get(reverse("workers:import_template")).status_code == 403
    assert upload(office, xlsx_file([row()])).status_code == 403
    assert office.post(reverse("workers:import_confirm"), {"token": "x"}).status_code == 403


def test_a_site_manager_imports_only_onto_their_sites(world):
    office = Office(world["manager"])
    template = office.get(reverse("workers:import_template"))
    lists = openpyxl.load_workbook(io.BytesIO(template.content))["Lists"]
    sites = [cell.value for cell in lists["A"][1:] if cell.value]
    assert sites == ["Riverside"]

    rows = [row(), row(**{"Site": "Route 4 Bridge", "Phone": "", "ID document number": ""})]
    response = upload(office, xlsx_file(rows))

    assert "Unknown site “Route 4 Bridge”" in preview_errors(response)[3][0]


def test_another_companys_sites_and_trades_are_unknown(world):
    with use_company(CompanyFactory()):
        CompanySettingsFactory()
        SiteFactory(name="Bravo Tower")
        TradeFactory(name="Bravotrade")
    office = Office(world["admin"])

    template = office.get(reverse("workers:import_template"))
    values = {
        str(cell.value)
        for column in openpyxl.load_workbook(io.BytesIO(template.content))["Lists"].iter_cols()
        for cell in column
    }
    response = upload(office, xlsx_file([row(**{"Site": "Bravo Tower", "Trade": "Bravotrade"})]))

    assert "Bravo Tower" not in values and "Bravotrade" not in values
    assert "Unknown site “Bravo Tower”" in preview_errors(response)[2][0]
    assert response.context["preview"]["rows"][0]["notes"] == [
        "New trade “Bravotrade” (created on import)."
    ]


def test_the_import_page_links_from_the_workers_list(world):
    page = Office(world["admin"]).get(reverse("workers:list")).content.decode()
    assert reverse("workers:import") in page
    foreman_page = Office(ForemanFactory()).get(reverse("workers:list")).content.decode()
    assert reverse("workers:import") not in foreman_page
