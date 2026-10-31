"""Payout export layouts (CSV / XLSX content and formats), reading confirmation reports,
and payout account validation."""

import datetime
import io

import pytest
from django.core.exceptions import ValidationError
from openpyxl import Workbook, load_workbook

from apps.core.models import Company
from apps.payouts import confirmations, layouts
from apps.payouts.models import (
    ExportLayout,
    PayoutAccount,
    PayoutBatch,
    PayoutLine,
    iban_is_valid,
)
from tests.factories import (
    CompanyFactory,
    PayLineFactory,
    PayPeriodFactory,
    SiteFactory,
    WorkerFactory,
)

pytestmark = pytest.mark.django_db

PAY_DAY = datetime.date(2025, 9, 1)


def make_batch(method="mobile_money", currency="XOF", decimals=0, lines=()):
    """A batch with one PayoutLine per ``(worker kwargs, amount, account kwargs)``."""
    site = SiteFactory(name="Riverside")
    period = PayPeriodFactory(
        site=site,
        start_date=PAY_DAY,
        end_date=PAY_DAY,
        currency=currency,
        currency_decimals=decimals,
    )
    batch = PayoutBatch.objects.create(
        number=7,
        method=method,
        pay_date=PAY_DAY,
        site=site,
        currency=currency,
        currency_decimals=decimals,
    )
    for index, (worker_kwargs, amount, account) in enumerate(lines, start=1):
        worker = WorkerFactory(**worker_kwargs)
        pay_line = PayLineFactory(period=period, worker=worker, gross=amount, net=amount)
        PayoutLine.objects.create(
            batch=batch,
            pay_line=pay_line,
            amount=amount,
            reference=f"{batch.reference}-{index:03d}",
            method=method,
            **account,
        )
    return batch


def layout(**kwargs):
    fields = kwargs.pop("fields", ["reference", "amount"])
    kwargs.setdefault("name", "Test layout")
    kwargs.setdefault("method", "mobile_money")
    return ExportLayout.objects.create(columns=[layouts.column(f) for f in fields], **kwargs)


def export(batch, the_layout, company_name="Polaris Construction"):
    lines = list(batch.lines.select_related("pay_line__worker", "pay_line__period__site"))
    return layouts.build_file(batch, lines, the_layout, company_name)


MOMO = {"mobile_operator": "Flooz", "mobile_number": "+228 90 11 22 33", "account_holder": "Yao M"}
BANK = {
    "bank_name": "Ecobank",
    "bank_code": "TG055",
    "account_number": "00123456789",
    "account_holder": "Afi K",
}


# --- built-in layouts ------------------------------------------------------------------------


def test_each_company_gets_the_two_built_in_layouts_once(default_company):
    layouts.ensure_default_layouts(default_company.pk)
    layouts.ensure_default_layouts(default_company.pk)
    other = CompanyFactory()
    layouts.ensure_default_layouts(other.pk)

    mine = ExportLayout.objects.filter(company=default_company)
    assert sorted(mine.values_list("name", "method", "file_type", "builtin")) == [
        ("Generic bank CSV", "bank_transfer", "csv", True),
        ("Generic mobile money CSV", "mobile_money", "csv", True),
    ]
    assert ExportLayout.objects.filter(company=other).count() == 2
    assert Company.objects.count() == 2


def test_generic_mobile_money_csv(default_company):
    layouts.ensure_default_layouts(default_company.pk)
    generic = ExportLayout.objects.get(name="Generic mobile money CSV")
    batch = make_batch(lines=[({"number": "W-0001"}, 6000, MOMO)])

    exported = export(batch, generic)

    assert exported.filename == "PAY-00007-2025-09-01-mobile_money.csv"
    assert exported.content_type == "text/csv; charset=utf-8"
    assert exported.rows == 1
    assert exported.content.decode().splitlines() == [
        "Reference,Mobile number,Account holder,Operator,Amount,Currency,Narrative",
        "PAY-00007-001,22890112233,Yao M,Flooz,6000,XOF,PAY-00007-001 W-0001",
    ]


def test_generic_bank_csv(default_company):
    layouts.ensure_default_layouts(default_company.pk)
    generic = ExportLayout.objects.get(name="Generic bank CSV")
    batch = make_batch(method="bank_transfer", lines=[({"number": "W-0002"}, 3500, BANK)])

    rows = export(batch, generic).content.decode().splitlines()

    assert rows[0] == (
        "Reference,Account holder,Bank,Bank code,Account number,Amount,Currency,Pay date,Narrative"
    )
    assert rows[1] == (
        "PAY-00007-001,Afi K,Ecobank,TG055,00123456789,3500,XOF,2025-09-01,PAY-00007-001 W-0002"
    )


# --- formats -----------------------------------------------------------------------------


def test_separator_date_amount_format_and_custom_headers():
    custom = layout(
        fields=["reference", "pay_date", "amount", "currency"],
        separator=";",
        date_format="%d/%m/%Y",
        amount_format="major_comma",
    )
    custom.columns[2]["header"] = "Montant"
    custom.save()
    batch = make_batch(currency="EUR", decimals=2, lines=[({}, 123450, MOMO)])

    rows = export(batch, custom).content.decode().splitlines()

    assert rows == ["Reference;Pay date;Montant;Currency", "PAY-00007-001;01/09/2025;1234,50;EUR"]


@pytest.mark.parametrize(
    ("amount_format", "decimals", "amount", "text"),
    [
        ("major_dot", 0, 3500, "3500"),
        ("major_dot", 2, 123450, "1234.50"),
        ("major_comma", 2, 5, "0,05"),
        ("minor", 2, 123450, "123450"),
        ("minor", 0, 3500, "3500"),
    ],
)
def test_amount_formats(amount_format, decimals, amount, text):
    assert layouts.format_amount(amount, decimals, amount_format) == text


@pytest.mark.parametrize(
    ("date_format", "text"),
    [
        ("%Y-%m-%d", "2025-09-01"),
        ("%d/%m/%Y", "01/09/2025"),
        ("%d.%m.%Y", "01.09.2025"),
        ("%m/%d/%Y", "09/01/2025"),
        ("%Y%m%d", "20250901"),
    ],
)
def test_date_formats(date_format, text):
    assert layouts.format_date(PAY_DAY, date_format) == text


def test_no_header_row_fixed_text_and_blank_columns_tab_separated():
    custom = layout(fields=["reference", "blank", "amount"], header_row=False, separator="\t")
    custom.columns.insert(0, layouts.column("text:SAL"))
    custom.save()
    batch = make_batch(lines=[({}, 6000, MOMO), ({}, 4000, MOMO)])

    rows = export(batch, custom).content.decode().splitlines()

    assert rows == ["SAL\tPAY-00007-001\t\t6000", "SAL\tPAY-00007-002\t\t4000"]


def test_windows_encoding_keeps_accents_and_bom_option():
    latin = layout(fields=["worker_name"], encoding="cp1252", header_row=False)
    batch = make_batch(lines=[({"first_name": "Akossiwa", "last_name": "Agbéko"}, 6000, MOMO)])
    assert export(batch, latin).content == "Akossiwa Agbéko\r\n".encode("cp1252")

    bom = layout(name="BOM", fields=["worker_name"], encoding="utf-8-sig", header_row=False)
    assert export(batch, bom).content.startswith(b"\xef\xbb\xbf")


def test_spreadsheet_formulas_are_neutralised_in_csv_and_xlsx():
    batch = make_batch(
        lines=[({"first_name": '=HYPERLINK("http://x")', "last_name": "A"}, 6000, MOMO)]
    )
    csv_layout = layout(fields=["worker_name", "amount"])
    rows = export(batch, csv_layout).content.decode().splitlines()
    assert rows[1].startswith("\"'=HYPERLINK(")

    xlsx_layout = layout(name="X", fields=["worker_name", "amount"], file_type="xlsx")
    sheet = load_workbook(io.BytesIO(export(batch, xlsx_layout).content)).active
    assert sheet["A2"].value.startswith("'=HYPERLINK")
    assert sheet["A2"].data_type == "s"


def test_xlsx_file():
    xlsx = layout(
        fields=["reference", "account_number", "amount", "pay_date"],
        method="bank_transfer",
        file_type="xlsx",
        date_format="%d.%m.%Y",
    )
    batch = make_batch(
        method="bank_transfer", currency="EUR", decimals=2, lines=[({}, 123450, BANK)]
    )

    exported = export(batch, xlsx)

    assert exported.filename.endswith(".xlsx")
    assert exported.content_type.startswith("application/vnd.openxmlformats")
    sheet = load_workbook(io.BytesIO(exported.content)).active
    rows = list(sheet.iter_rows(values_only=True))
    assert rows[0] == ("Reference", "Account number", "Amount", "Pay date")
    assert rows[1][0] == "PAY-00007-001"
    assert rows[1][1] == "00123456789"  # text: the leading zeros stay
    assert str(rows[1][2]) == "1234.5"  # a number
    assert sheet["C2"].number_format == "0.00"
    assert rows[1][3] == "01.09.2025"


def test_xlsx_minor_amounts_are_integers():
    xlsx = layout(fields=["amount"], file_type="xlsx", amount_format="minor", header_row=False)
    batch = make_batch(currency="EUR", decimals=2, lines=[({}, 123450, MOMO)])
    sheet = load_workbook(io.BytesIO(export(batch, xlsx).content)).active
    assert sheet["A1"].value == 123450


# --- typing the columns ------------------------------------------------------------------------


def test_columns_are_typed_one_per_line():
    columns = layouts.parse_columns(
        "reference\n  amount = Montant  \n\ntext:VIR = Type\nBLANK\nmobile_number"
    )
    assert columns == [
        {"field": "reference", "header": "Reference"},
        {"field": "amount", "header": "Montant"},
        {"field": "text:VIR", "header": "Type"},
        {"field": "blank", "header": ""},
        {"field": "mobile_number", "header": "Mobile number"},
    ]
    assert layouts.parse_columns(layouts.columns_to_text(columns)) == columns


def test_unknown_fields_and_empty_columns_are_refused():
    with pytest.raises(ValidationError) as excinfo:
        layouts.parse_columns("reference\nsalary\niban")
    assert len(excinfo.value.messages) == 2
    with pytest.raises(ValidationError):
        layouts.parse_columns("  \n ")
    with pytest.raises(ValidationError):
        layouts.validate_columns([{"field": "password"}])


# --- confirmation reports --------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "decimals", "amount_format", "minor"),
    [
        ("6000", 0, "major_dot", 6000),
        ("6 000", 0, "major_dot", 6000),
        ("6,000", 0, "major_dot", 6000),
        ("6000 XOF", 0, "major_dot", 6000),
        ("1234.50", 2, "major_dot", 123450),
        ("1 234,50", 2, "major_comma", 123450),
        ("1.234,50", 2, "major_dot", 123450),
        ("1,234.50", 2, "major_dot", 123450),
        ("12,5", 2, "major_dot", 1250),
        ("123450", 2, "minor", 123450),
    ],
)
def test_report_amounts(text, decimals, amount_format, minor):
    assert confirmations.parse_amount(text, decimals, amount_format) == minor


@pytest.mark.parametrize("text", ["12.345", "abc", "1.5"])
def test_unreadable_report_amounts(text):
    with pytest.raises(ValueError):
        confirmations.parse_amount(text, 0 if text != "12.345" else 2, "major_dot")


@pytest.mark.parametrize(
    ("word", "status"),
    [
        ("OK", "success"),
        ("Successful", "success"),
        ("Payé", "success"),
        ("Effectué", "success"),
        ("", "success"),
        ("FAILED", "failure"),
        ("Rejeté", "failure"),
        ("Échec", "failure"),
        ("pending review", ""),
    ],
)
def test_report_status_words(word, status):
    assert confirmations.status_of(word) == status


def test_french_semicolon_report_is_read():
    content = (
        "Référence;Numéro;Montant;Statut;ID transaction;Motif\r\n"
        "PAY-00007-001;22890112233;6 000;Succès;TX-1;\r\n"
        "PAY-00007-002;22890999999;4000;Échec;;Compte inconnu\r\n"
        ";;;;;\r\n"
    ).encode("cp1252")

    rows = confirmations.parse("report.csv", content, decimals=0, layout=None)

    assert [(r.row, r.reference, r.account, r.amount, r.status) for r in rows] == [
        (2, "PAY-00007-001", "22890112233", 6000, "success"),
        (3, "PAY-00007-002", "22890999999", 4000, "failure"),
    ]
    assert rows[0].provider_reference == "TX-1"
    assert rows[1].message == "Compte inconnu"


def test_report_headers_from_the_batch_layout_are_recognised():
    custom = layout(fields=["reference", "amount"])
    custom.columns = [
        {"field": "reference", "header": "Ref client"},
        {"field": "amount", "header": "Somme payée"},
    ]
    content = "Ref client,Somme payée\nPAY-00007-001,6000\n".encode()

    rows = confirmations.parse("r.csv", content, decimals=0, layout=custom)

    assert rows[0].reference == "PAY-00007-001"
    assert rows[0].amount == 6000
    assert rows[0].status == "success"  # no status column: paid


def test_report_rows_with_unknown_status_or_amount_are_flagged():
    content = b"Reference,Amount,Status\nPAY-1,abc,OK\nPAY-2,10,On hold\n"
    rows = confirmations.parse("r.csv", content, decimals=0, layout=None)
    assert "Unreadable amount" in rows[0].error
    assert "Unknown status" in rows[1].error


def test_xlsx_report_is_read_without_floats_drifting():
    workbook = Workbook()
    sheet = workbook.active
    sheet.append(["Reference", "Amount", "Status"])
    sheet.append(["PAY-00007-001", 1234.5, "Paid"])
    sheet.append(["PAY-00007-002", 6000, "Failed"])
    out = io.BytesIO()
    workbook.save(out)

    rows = confirmations.parse("report.xlsx", out.getvalue(), decimals=2, layout=None)

    assert [(r.reference, r.amount, r.status) for r in rows] == [
        ("PAY-00007-001", 123450, "success"),
        ("PAY-00007-002", 600000, "failure"),
    ]


def test_reports_that_cannot_be_used_are_refused():
    with pytest.raises(ValidationError):
        confirmations.parse("r.pdf", b"%PDF", decimals=0, layout=None)
    with pytest.raises(ValidationError):
        confirmations.parse("r.csv", b"Name,Comment\nYao,ok\n", decimals=0, layout=None)
    with pytest.raises(ValidationError):
        confirmations.parse("r.csv", b"", decimals=0, layout=None)
    with pytest.raises(ValidationError):
        confirmations.parse("r.xlsx", b"PK not a workbook", decimals=0, layout=None)
    with pytest.raises(ValidationError):
        confirmations.parse("r.csv", b"x" * (confirmations.MAX_BYTES + 1), decimals=0, layout=None)


# --- payout accounts -------------------------------------------------------------------------


def test_iban_check_digits():
    assert iban_is_valid("FR14 2004 1010 0505 0001 3M02 606")
    assert iban_is_valid("GB82WEST12345698765432")
    assert not iban_is_valid("GB82WEST12345698765433")


def _account(**kwargs):
    return PayoutAccount(worker=WorkerFactory(), **kwargs)


def test_a_bank_account_needs_a_bank_and_a_valid_number():
    account = _account(method="bank_transfer")
    with pytest.raises(ValidationError) as excinfo:
        account.full_clean()
    assert set(excinfo.value.message_dict) == {"bank_name", "account_number"}

    bad_iban = _account(
        method="bank_transfer", bank_name="Ecobank", account_number="GB82 WEST 1234 5698 7654 33"
    )
    with pytest.raises(ValidationError) as excinfo:
        bad_iban.full_clean()
    assert "IBAN" in excinfo.value.message_dict["account_number"][0]

    good = _account(
        method="bank_transfer",
        bank_name="Ecobank",
        account_number="fr14 2004-1010 0505 0001 3m02 606",
    )
    good.full_clean()
    assert good.account_number == "FR1420041010050500013M02606"
    assert good.is_complete_for("bank_transfer")
    assert not good.is_complete_for("mobile_money")
    assert good.masked == "•••• 2606"


def test_a_mobile_money_account_needs_an_operator_and_a_phone():
    account = _account(method="mobile_money", mobile_number="12")
    with pytest.raises(ValidationError) as excinfo:
        account.full_clean()
    assert set(excinfo.value.message_dict) == {"mobile_operator", "mobile_number"}

    good = _account(
        method="mobile_money", mobile_operator="T-Money", mobile_number="+228 90 11 22 33"
    )
    good.full_clean()
    assert good.holder == good.worker.full_name
    assert good.masked == "•••• 2233"
