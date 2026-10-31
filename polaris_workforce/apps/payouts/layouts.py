"""Export layouts: which columns a bulk-payment file has, and how values are written.

A layout's ``columns`` is a list of ``{"field": <key>, "header": <text>}`` in file order.
The keys are in ``FIELDS``; ``text:<value>`` writes a fixed value on every row (a debit
account, a payment type code…) and ``blank`` an empty column. Headers are written as
typed (bank portals match them exactly), so they are not translated.

Office users type the columns one per line, ``field`` or ``field = Header``
(``columns_to_text`` / ``parse_columns``).
"""

from __future__ import annotations

import csv
import datetime
import io
from dataclasses import dataclass

from django.core.exceptions import ValidationError
from django.utils.translation import gettext as _
from django.utils.translation import gettext_lazy

from apps.core.money import from_minor

from .models import ExportLayout, PayoutBatch, PayoutLine, TransferMethod, msisdn

# key → (description shown in the office, default header written in the file)
FIELDS: dict[str, tuple] = {
    "reference": (gettext_lazy("Payment reference (one per line)"), "Reference"),
    "batch_reference": (gettext_lazy("Batch reference"), "Batch"),
    "worker_number": (gettext_lazy("Worker number"), "Worker number"),
    "worker_name": (gettext_lazy("Worker name"), "Worker"),
    "first_name": (gettext_lazy("First name"), "First name"),
    "last_name": (gettext_lazy("Last name"), "Last name"),
    "account_holder": (gettext_lazy("Account holder"), "Account holder"),
    "bank_name": (gettext_lazy("Bank name"), "Bank"),
    "bank_code": (gettext_lazy("Bank code / BIC"), "Bank code"),
    "account_number": (gettext_lazy("Account number or IBAN"), "Account number"),
    "mobile_operator": (gettext_lazy("Mobile money operator"), "Operator"),
    "mobile_number": (gettext_lazy("Mobile money number (digits only)"), "Mobile number"),
    "amount": (gettext_lazy("Amount (in the layout's amount format)"), "Amount"),
    "currency": (gettext_lazy("Currency code"), "Currency"),
    "pay_date": (gettext_lazy("Pay day (in the layout's date format)"), "Pay date"),
    "site": (gettext_lazy("Site name"), "Site"),
    "narrative": (gettext_lazy("Narrative: reference and worker number"), "Narrative"),
    "company_name": (gettext_lazy("Company name"), "Payer"),
}
TEXT_PREFIX = "text:"
BLANK = "blank"

DEFAULT_LAYOUTS = {
    TransferMethod.BANK_TRANSFER: (
        "Generic bank CSV",
        [
            "reference",
            "account_holder",
            "bank_name",
            "bank_code",
            "account_number",
            "amount",
            "currency",
            "pay_date",
            "narrative",
        ],
    ),
    TransferMethod.MOBILE_MONEY: (
        "Generic mobile money CSV",
        [
            "reference",
            "mobile_number",
            "account_holder",
            "mobile_operator",
            "amount",
            "currency",
            "narrative",
        ],
    ),
}

# Text that a spreadsheet would run as a formula gets a leading apostrophe.
FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


def safe_text(value: str) -> str:
    return f"'{value}" if value.startswith(FORMULA_PREFIXES) else value


def default_header(field: str) -> str:
    if field.startswith(TEXT_PREFIX) or field == BLANK:
        return ""
    return FIELDS[field][1]


def column(field: str, header: str | None = None) -> dict:
    return {"field": field, "header": default_header(field) if header is None else header}


def ensure_default_layouts(company_id: int) -> None:
    """Create the two built-in layouts of a company if they are missing."""
    for method, (name, fields) in DEFAULT_LAYOUTS.items():
        ExportLayout.objects.get_or_create(
            company_id=company_id,
            name=name,
            defaults={
                "method": method,
                "builtin": True,
                "columns": [column(f) for f in fields],
            },
        )


# --- columns typed in the office ---------------------------------------------------------


def columns_to_text(columns: list[dict]) -> str:
    lines = []
    for col in columns:
        field, header = col.get("field", ""), col.get("header", "")
        lines.append(field if header == default_header(field) else f"{field} = {header}")
    return "\n".join(lines)


def parse_columns(text: str) -> list[dict]:
    """``"reference\\namount = Montant"`` → columns. Raises ``ValidationError``."""
    columns, errors = [], []
    for number, raw in enumerate((text or "").splitlines(), start=1):
        line = raw.strip()
        if not line:
            continue
        field, sep, header = line.partition("=")
        field = field.strip()
        if field.lower().startswith(TEXT_PREFIX):
            field = TEXT_PREFIX + field[len(TEXT_PREFIX) :].strip()
        elif field.lower() == BLANK:
            field = BLANK
        else:
            field = field.lower()
            if field not in FIELDS:
                errors.append(
                    _("Line %(line)s: unknown field “%(field)s”.")
                    % {"line": number, "field": field}
                )
                continue
        columns.append(column(field, header.strip() if sep else None))
    if errors:
        raise ValidationError(errors)
    if not columns:
        raise ValidationError(_("Add at least one column."))
    if len(columns) > 40:
        raise ValidationError(_("A layout has at most 40 columns."))
    return columns


def validate_columns(columns) -> None:
    if not isinstance(columns, list) or not columns:
        raise ValidationError(_("Add at least one column."))
    for col in columns:
        field = col.get("field") if isinstance(col, dict) else None
        if not isinstance(field, str) or not (
            field in FIELDS or field == BLANK or field.startswith(TEXT_PREFIX)
        ):
            raise ValidationError(_("Unknown column: %(field)s.") % {"field": field})


# --- values ------------------------------------------------------------------------------


def format_amount(amount: int, decimals: int, amount_format: str) -> str:
    if amount_format == ExportLayout.AmountFormat.MINOR:
        return str(amount)
    text = f"{from_minor(amount, decimals):f}"
    if amount_format == ExportLayout.AmountFormat.MAJOR_COMMA:
        text = text.replace(".", ",")
    return text


def format_date(day: datetime.date, date_format: str) -> str:
    return day.strftime(date_format)


def _value(field: str, line: PayoutLine, batch: PayoutBatch, layout: ExportLayout, company_name):
    """The text of one cell (amounts as text; the XLSX writer turns them into numbers)."""
    worker = line.pay_line.worker
    if field.startswith(TEXT_PREFIX):
        return field[len(TEXT_PREFIX) :]
    values = {
        BLANK: "",
        "reference": line.reference,
        "batch_reference": batch.reference,
        "worker_number": worker.number,
        "worker_name": worker.full_name,
        "first_name": worker.first_name,
        "last_name": worker.last_name,
        "account_holder": line.account_holder,
        "bank_name": line.bank_name,
        "bank_code": line.bank_code,
        "account_number": line.account_number,
        "mobile_operator": line.mobile_operator,
        "mobile_number": msisdn(line.mobile_number),
        "currency": batch.currency,
        "pay_date": format_date(batch.pay_date, layout.date_format),
        "site": line.pay_line.period.site.name,
        "narrative": f"{line.reference} {worker.number}",
        "company_name": company_name,
    }
    return values[field]


@dataclass
class ExportFile:
    filename: str
    content: bytes
    content_type: str
    rows: int


CONTENT_TYPES = {
    ExportLayout.FileType.CSV: "text/csv",
    ExportLayout.FileType.XLSX: (
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    ),
}


def build_file(batch: PayoutBatch, lines: list[PayoutLine], layout: ExportLayout, company_name):
    """The bulk-payment file of ``lines`` in ``layout``."""
    headers = [col.get("header", "") for col in layout.columns]
    fields = [col["field"] for col in layout.columns]
    if layout.file_type == ExportLayout.FileType.XLSX:
        content = _xlsx(batch, lines, layout, company_name, headers, fields)
        extension = "xlsx"
        content_type = CONTENT_TYPES[ExportLayout.FileType.XLSX]
    else:
        content = _csv(batch, lines, layout, company_name, headers, fields)
        extension = "csv"
        content_type = f"{CONTENT_TYPES[ExportLayout.FileType.CSV]}; charset={layout.encoding}"
    filename = f"{batch.reference}-{batch.pay_date:%Y-%m-%d}-{batch.method}.{extension}"
    return ExportFile(filename, content, content_type, len(lines))


def _csv(batch, lines, layout, company_name, headers, fields) -> bytes:
    buffer = io.StringIO()
    writer = csv.writer(buffer, delimiter=layout.separator, lineterminator="\r\n")
    if layout.header_row:
        writer.writerow([safe_text(h) for h in headers])
    for line in lines:
        row = []
        for field in fields:
            if field == "amount":
                row.append(
                    format_amount(line.amount, batch.currency_decimals, layout.amount_format)
                )
            else:
                row.append(safe_text(str(_value(field, line, batch, layout, company_name))))
        writer.writerow(row)
    return buffer.getvalue().encode(layout.encoding, errors="replace")


def _xlsx(batch, lines, layout, company_name, headers, fields) -> bytes:
    from openpyxl import Workbook

    workbook = Workbook()
    sheet = workbook.active
    sheet.title = batch.reference
    if layout.header_row:
        sheet.append([safe_text(h) for h in headers])
    decimals = batch.currency_decimals
    for line in lines:
        row = []
        for field in fields:
            if field == "amount":
                if layout.amount_format == ExportLayout.AmountFormat.MINOR:
                    row.append(line.amount)
                else:
                    row.append(from_minor(line.amount, decimals))
            else:
                row.append(safe_text(str(_value(field, line, batch, layout, company_name))))
        sheet.append(row)
        # Numbers stay text (account numbers with leading zeros, long IBANs).
        for cell, field in zip(sheet[sheet.max_row], fields, strict=True):
            if field == "amount":
                cell.number_format = "0" if decimals == 0 else "0." + "0" * decimals
            else:
                cell.data_type = "s"
    out = io.BytesIO()
    workbook.save(out)
    return out.getvalue()
