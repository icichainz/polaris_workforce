"""Reading a provider's confirmation file (CSV or XLSX) into ``ConfirmationRow``s.

Banks and mobile-money portals return a report of the bulk payment: one row per
transfer with (usually) the reference we sent, the account, the amount and a status.
Columns are found by their header, in English or French (``HEADER_NAMES``), or by the
headers of the batch's own export layout, so the exported file with a "Status" column
added is accepted too. Without a status column every row counts as paid.

Amounts are read in the layout's amount format (major units by default, smallest unit
for a "minor" layout) and converted with ``apps.core.money`` — never through a float
(an XLSX number is read through its decimal text).
"""

from __future__ import annotations

import csv
import io
import re
import unicodedata
from dataclasses import dataclass
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.utils.translation import gettext as _

from apps.core.money import to_minor

from .models import ExportLayout

MAX_BYTES = 2 * 1024 * 1024
MAX_ROWS = 5000

SUCCESS = "success"
FAILURE = "failure"


def _norm(text) -> str:
    text = unicodedata.normalize("NFKD", str(text or "")).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


HEADER_NAMES = {
    "reference": {
        "reference",
        "ref",
        "payment reference",
        "customer reference",
        "client reference",
        "end to end id",
        "your reference",
        "notre reference",
        "votre reference",
        "reference paiement",
        "reference client",
    },
    "account": {
        "account",
        "account number",
        "beneficiary account",
        "iban",
        "msisdn",
        "phone",
        "phone number",
        "mobile number",
        "mobile",
        "wallet",
        "numero",
        "numero de compte",
        "compte",
        "telephone",
        "numero de telephone",
        "beneficiaire",
    },
    "amount": {"amount", "montant", "value", "valeur", "sum", "somme", "amount paid"},
    "status": {"status", "statut", "state", "etat", "result", "resultat", "outcome"},
    "provider_reference": {
        "transaction id",
        "transaction",
        "transaction reference",
        "bank reference",
        "provider reference",
        "id transaction",
        "reference transaction",
        "reference banque",
        "txn id",
    },
    "message": {"message", "reason", "motif", "comment", "commentaire", "error", "erreur"},
}

SUCCESS_WORDS = {
    "",
    "ok",
    "success",
    "successful",
    "succeeded",
    "paid",
    "completed",
    "complete",
    "done",
    "confirmed",
    "accepted",
    "executed",
    "processed",
    "settled",
    "succes",
    "reussi",
    "paye",
    "effectue",
    "execute",
    "valide",
    "traite",
}
FAILURE_WORDS = {
    "failed",
    "fail",
    "failure",
    "rejected",
    "error",
    "returned",
    "cancelled",
    "canceled",
    "declined",
    "refused",
    "echec",
    "echoue",
    "rejete",
    "refuse",
    "erreur",
    "annule",
    "retourne",
}


@dataclass
class ConfirmationRow:
    row: int  # line number in the file (1 = first line)
    reference: str = ""
    account: str = ""
    amount: int | None = None
    status: str = SUCCESS  # SUCCESS | FAILURE | "" (unreadable)
    provider_reference: str = ""
    message: str = ""
    error: str = ""  # the row could not be read


def _rows_from_csv(content: bytes) -> list[list[str]]:
    for encoding in ("utf-8-sig", "cp1252", "latin-1"):
        try:
            text = content.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    sample = text[:4096]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
        delimiter = dialect.delimiter
    except csv.Error:
        delimiter = ","
    return [row for row in csv.reader(io.StringIO(text), delimiter=delimiter)]


def _cell_text(value) -> str:
    if value is None:
        return ""
    if isinstance(value, float):
        # openpyxl gives floats for numbers: read them through their shortest decimal text.
        value = Decimal(repr(value))
        if value == value.to_integral_value():
            value = value.quantize(Decimal(1))
        return format(value, "f")
    return str(value).strip()


def _rows_from_xlsx(content: bytes) -> list[list[str]]:
    from openpyxl import load_workbook

    try:
        workbook = load_workbook(io.BytesIO(content), read_only=True, data_only=True)
    except Exception as exc:  # noqa: BLE001 (any unreadable workbook)
        raise ValidationError(_("This Excel file cannot be read.")) from exc
    sheet = workbook.worksheets[0]
    rows = []
    for row in sheet.iter_rows(values_only=True):
        rows.append([_cell_text(v) for v in row])
        if len(rows) > MAX_ROWS + 1:
            break
    workbook.close()
    return rows


def read_table(name: str, content: bytes) -> list[list[str]]:
    if len(content) > MAX_BYTES:
        raise ValidationError(_("The file is too large (2 MB at most)."))
    if name.lower().endswith(".xlsx") or content[:2] == b"PK":
        rows = _rows_from_xlsx(content)
    elif name.lower().endswith((".csv", ".txt")):
        rows = _rows_from_csv(content)
    else:
        raise ValidationError(_("Upload a CSV or XLSX file."))
    rows = [[str(c).strip() for c in row] for row in rows]
    while rows and not any(rows[-1]):
        rows.pop()
    if len(rows) > MAX_ROWS + 1:
        raise ValidationError(_("The file has too many rows (5000 at most)."))
    return rows


def _header_map(header: list[str], layout: ExportLayout | None) -> dict[str, int]:
    names = {key: set(values) for key, values in HEADER_NAMES.items()}
    if layout is not None:
        for col in layout.columns:
            target = {
                "reference": "reference",
                "account_number": "account",
                "mobile_number": "account",
                "amount": "amount",
            }.get(col.get("field"))
            if target and col.get("header"):
                names[target].add(_norm(col["header"]))
    found: dict[str, int] = {}
    for index, cell in enumerate(header):
        key = _norm(cell)
        for target, options in names.items():
            if key in options and target not in found:
                found[target] = index
    return found


def parse_amount(text: str, decimals: int, amount_format: str) -> int:
    cleaned = re.sub(r"[\s  ']", "", text)
    cleaned = re.sub(r"[A-Za-z]{3}$|^[A-Za-z]{3}", "", cleaned)  # "3500 XOF"
    if amount_format == ExportLayout.AmountFormat.MINOR:
        if not re.fullmatch(r"-?\d+", cleaned):
            raise ValueError(cleaned)
        return int(cleaned)
    if "," in cleaned and "." in cleaned:
        # The last separator is the decimal one.
        if cleaned.rfind(",") > cleaned.rfind("."):
            cleaned = cleaned.replace(".", "").replace(",", ".")
        else:
            cleaned = cleaned.replace(",", "")
    elif "," in cleaned:
        head, _sep, tail = cleaned.rpartition(",")
        if amount_format == ExportLayout.AmountFormat.MAJOR_COMMA or len(tail) != 3:
            cleaned = f"{head.replace(',', '')}.{tail}"
        else:
            cleaned = cleaned.replace(",", "")
    return to_minor(cleaned, decimals)


def status_of(text: str) -> str:
    word = _norm(text)
    if word in SUCCESS_WORDS:
        return SUCCESS
    if word in FAILURE_WORDS:
        return FAILURE
    return ""


def parse(name: str, content: bytes, *, decimals: int, layout: ExportLayout | None):
    """The rows of a confirmation file. Raises ``ValidationError`` when the file itself
    cannot be used (no header with a reference or account column…)."""
    table = read_table(name, content)
    if not table:
        raise ValidationError(_("The file is empty."))
    columns = _header_map(table[0], layout)
    if "reference" not in columns and "account" not in columns:
        raise ValidationError(
            _("No “Reference” or “Account” column found in the first row of the file.")
        )
    amount_format = layout.amount_format if layout else ExportLayout.AmountFormat.MAJOR_DOT
    rows = []
    for number, cells in enumerate(table[1:], start=2):
        if not any(cells):
            continue

        def cell(key, cells=cells):
            index = columns.get(key)
            return cells[index] if index is not None and index < len(cells) else ""

        row = ConfirmationRow(
            row=number,
            reference=cell("reference"),
            account=cell("account"),
            provider_reference=cell("provider_reference")[:100],
            message=cell("message")[:300],
        )
        if "status" in columns:
            row.status = status_of(cell("status"))
            if not row.status:
                row.error = _("Unknown status “%(status)s”.") % {"status": cell("status")}
        amount_text = cell("amount")
        if amount_text:
            try:
                row.amount = parse_amount(amount_text, decimals, amount_format)
            except (ValueError, TypeError):
                row.error = _("Unreadable amount “%(amount)s”.") % {"amount": amount_text}
        rows.append(row)
    return rows
