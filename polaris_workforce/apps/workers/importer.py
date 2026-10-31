"""Import workers from an Excel (XLSX) or CSV file.

The flow, driven by the views ``workers:import*``:

1. **Template** — ``template_workbook()``: a ``Workers`` sheet with one column per field
   (``COLUMNS``), a ``Lists`` sheet with the allowed values (the user's sites, the
   company's trades, pay types, ID document types, payment methods) used as drop-downs, and
   an ``Instructions`` sheet.
2. **Read** — ``read_upload()``: XLSX (first sheet) or CSV (comma, semicolon or tab;
   UTF-8 or Windows-1252) → the header and the rows as text. Columns are found by their
   header (English or French label, or the field key), in any order.
3. **Preview** — ``run_import(..., commit=False)``: every row is checked (required values,
   formats, choices, the site must be one the user may put workers on, duplicates by
   phone or ID number within the file and against the company's workers, formula
   injection), then the valid rows are created for real inside a transaction that is
   rolled back, so the rules of the services (assignment, rates) are checked too. Nothing
   is saved.
4. **Confirm** — ``run_import(..., commit=True)``: the same checks, then every worker is
   created through ``services.create_worker`` in **one** transaction: either all rows are
   imported or none. Each worker gets its own ``workers.create`` audit entry (reason:
   "Imported from <file>"), and the import one ``workers.import`` summary entry.

Decisions:
- A trade that does not exist yet is created on confirm (the preview flags it as "new").
- Blank start date → the company's today; blank pay type → daily; blank payment method →
  inherited from the site / company; blank ID document type → "None" (but an ID number
  needs a type).
- A text value that starts with ``=``, ``+``, ``-``, ``@``, a tab or a carriage return is
  refused (spreadsheet formula injection); formula cells arrive as their formula text and
  are refused the same way. Phones may start with ``+`` (only digits and separators).
- Limits: ``MAX_ROWS`` rows, ``MAX_FILE_SIZE`` bytes, ``MAX_UNCOMPRESSED_SIZE`` bytes
  inside an XLSX (zip bombs), ``MAX_COLUMNS`` columns read.
"""

from __future__ import annotations

import csv
import datetime
import io
import re
import unicodedata
import zipfile
from dataclasses import dataclass, field
from decimal import Decimal

from django import forms
from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import translation
from django.utils.translation import gettext as _
from django.utils.translation import gettext_lazy

from apps.accounts.permissions import company_workers
from apps.core import dates
from apps.core.choices import PaymentMethod, PayType
from apps.core.models import AuditLog, CompanySettings

from . import services
from .fields import MoneyField
from .models import Trade, Worker

MAX_ROWS = 500
MAX_FILE_SIZE = 2 * 1024 * 1024  # 2 MB
MAX_UNCOMPRESSED_SIZE = 25 * 1024 * 1024  # what an XLSX may unpack to
MAX_COLUMNS = 40
MAX_SCANNED_ROWS = 20_000  # blank rows included; stops a sheet of a million empty rows
FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")
LANGUAGES = ("en", "fr")
_PHONE = re.compile(r"^\+?[0-9][0-9 ().-]*$")
_ID_SEPARATORS = re.compile(r"[\s./-]")


@dataclass(frozen=True)
class Column:
    key: str
    label: str  # lazy
    required: bool = False
    help: str = ""  # lazy
    width: int = 18


COLUMNS = (
    Column("first_name", gettext_lazy("First name"), required=True),
    Column("last_name", gettext_lazy("Last name"), required=True),
    Column("phone", gettext_lazy("Phone"), help=gettext_lazy("For example +228 90 12 34 56.")),
    Column(
        "trade",
        gettext_lazy("Trade"),
        help=gettext_lazy("Pick one from the list or type a new one (it is created)."),
    ),
    Column(
        "site",
        gettext_lazy("Site"),
        required=True,
        help=gettext_lazy("One of your sites, as named in Polaris."),
        width=26,
    ),
    Column(
        "start_date",
        gettext_lazy("Start date"),
        help=gettext_lazy("YYYY-MM-DD or DD/MM/YYYY. Empty: today."),
        width=14,
    ),
    Column("pay_type", gettext_lazy("Pay type"), help=gettext_lazy("Empty: daily."), width=12),
    Column(
        "rate",
        gettext_lazy("Rate"),
        required=True,
        help=gettext_lazy(
            "Per day or per hour, in the company's currency. Piece work: a reference day value."
        ),
        width=12,
    ),
    Column("id_document_type", gettext_lazy("ID document type"), width=20),
    Column("id_document_number", gettext_lazy("ID document number"), width=20),
    Column(
        "payment_method",
        gettext_lazy("Payment method"),
        help=gettext_lazy("Empty: the site's or the company's setting."),
        width=18,
    ),
)
COLUMN_KEYS = tuple(column.key for column in COLUMNS)
TEXT_FIELDS = ("first_name", "last_name", "trade", "id_document_number")


class ImportFileError(Exception):
    """The file as a whole cannot be imported (format, size, headers)."""

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


# --- helpers ------------------------------------------------------------------------------


def _fold(text: str) -> str:
    """Case-, accent- and space-insensitive form of a header or a choice label."""
    text = unicodedata.normalize("NFKD", str(text)).encode("ascii", "ignore").decode()
    return " ".join(text.replace("*", " ").replace("_", " ").casefold().split())


def _labels(value_labels) -> dict[str, str]:
    """``{folded label or value: value}`` for choices, in every office language."""
    found: dict[str, str] = {}
    for value, label in value_labels:
        found[_fold(value)] = value
        for language in LANGUAGES:
            with translation.override(language):
                found[_fold(str(label))] = value
        found[_fold(str(label))] = value  # the current language
    return found


def _header_aliases() -> dict[str, str]:
    aliases: dict[str, str] = {}
    for column in COLUMNS:
        aliases[_fold(column.key)] = column.key
        for language in LANGUAGES:
            with translation.override(language):
                aliases[_fold(str(column.label))] = column.key
        aliases[_fold(str(column.label))] = column.key
    return aliases


def _cell_text(value) -> str:
    """A cell as text: dates as ISO, whole numbers without ``.0``, never via float maths."""
    if value is None:
        return ""
    if isinstance(value, datetime.datetime):
        return value.date().isoformat()
    if isinstance(value, datetime.date):
        return value.isoformat()
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, float):
        # str() of a float is its shortest exact-looking form ("1234.5"); Decimal parses it.
        number = Decimal(str(value))
        return format(
            number.quantize(Decimal(1)) if number == number.to_integral() else number, "f"
        )
    text = str(value).replace("\x00", "")
    return text.strip()


def phone_key(phone: str) -> str:
    """Digits only: "+228 90-12 34 56" and "22890123456" are the same phone."""
    return "".join(ch for ch in phone if ch.isdigit())


def id_key(number: str) -> str:
    return _ID_SEPARATORS.sub("", number).casefold()


# --- reading the file ---------------------------------------------------------------------


@dataclass
class Upload:
    name: str
    header: list[str]
    rows: list[tuple[int, list[str]]]  # (line number in the file, cells as text)


def read_upload(file) -> Upload:
    """Read an uploaded XLSX or CSV file (raises ``ImportFileError``)."""
    name = getattr(file, "name", "") or ""
    size = getattr(file, "size", None)
    if size is not None and size > MAX_FILE_SIZE:
        raise ImportFileError(
            _("The file is too large. The maximum is %(size)s MB.")
            % {"size": MAX_FILE_SIZE // (1024 * 1024)}
        )
    data = file.read(MAX_FILE_SIZE + 1)
    if len(data) > MAX_FILE_SIZE:
        raise ImportFileError(
            _("The file is too large. The maximum is %(size)s MB.")
            % {"size": MAX_FILE_SIZE // (1024 * 1024)}
        )
    if not data:
        raise ImportFileError(_("The file is empty."))
    extension = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    if extension == "xlsx":
        if not data.startswith(b"PK\x03\x04"):
            raise ImportFileError(_("This file is not an Excel workbook (.xlsx)."))
        lines = _xlsx_lines(data)
    elif extension == "csv":
        lines = _csv_lines(data)
    else:
        raise ImportFileError(_("Upload an Excel workbook (.xlsx) or a CSV file (.csv)."))
    return _split(name, lines)


def _xlsx_lines(data: bytes):
    # Imported here: only the import and the exports need openpyxl.
    import openpyxl
    from openpyxl.utils.exceptions import InvalidFileException

    unreadable = _("This Excel file could not be read. Save it again as .xlsx and retry.")
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            unpacked = sum(info.file_size for info in archive.infolist())
    except (zipfile.BadZipFile, OSError, ValueError) as exc:
        raise ImportFileError(unreadable) from exc
    if unpacked > MAX_UNCOMPRESSED_SIZE:
        raise ImportFileError(_("This Excel file holds too much data to import."))
    try:
        # data_only=False: a formula cell comes as its "=…" text and is refused below.
        workbook = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=False)
    except (InvalidFileException, zipfile.BadZipFile, KeyError, OSError, ValueError) as exc:
        raise ImportFileError(unreadable) from exc
    try:
        if not workbook.worksheets:
            raise ImportFileError(unreadable)
        sheet = workbook.worksheets[0]
        lines = []
        for number, row in enumerate(sheet.iter_rows(max_col=MAX_COLUMNS, values_only=True), 1):
            if number > MAX_SCANNED_ROWS:
                raise ImportFileError(_too_many_rows())
            lines.append((number, [_cell_text(value) for value in row]))
        return lines
    except (KeyError, OSError, ValueError, TypeError) as exc:
        raise ImportFileError(unreadable) from exc
    finally:
        workbook.close()


def _csv_lines(data: bytes):
    for encoding in ("utf-8-sig", "cp1252"):
        try:
            text = data.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    else:  # pragma: no cover - cp1252 decodes almost anything
        raise ImportFileError(_("The CSV file must be saved as UTF-8."))
    if "\x00" in text:
        raise ImportFileError(_("This file is not a CSV text file."))
    first_line = text.split("\n", 1)[0]
    delimiter = max((";", ",", "\t"), key=first_line.count)
    lines = []
    try:
        for number, row in enumerate(csv.reader(io.StringIO(text), delimiter=delimiter), 1):
            if number > MAX_SCANNED_ROWS:
                raise ImportFileError(_too_many_rows())
            lines.append((number, [_cell_text(value) for value in row[:MAX_COLUMNS]]))
    except csv.Error as exc:
        raise ImportFileError(_("This CSV file could not be read.")) from exc
    return lines


def _too_many_rows() -> str:
    return _("The file has more than %(max)s workers. Split it into several files.") % {
        "max": MAX_ROWS
    }


def _split(name: str, lines) -> Upload:
    lines = [(number, cells) for number, cells in lines if any(cells)]
    if not lines:
        raise ImportFileError(_("The file is empty."))
    (_number, header), rows = lines[0], lines[1:]
    if not rows:
        raise ImportFileError(_("The file has headers but no worker rows."))
    if len(rows) > MAX_ROWS:
        raise ImportFileError(_too_many_rows())
    return Upload(name=name, header=header, rows=rows)


def map_columns(header: list[str]) -> dict[str, int]:
    """``{column key: index}`` from the header row (raises ``ImportFileError``)."""
    aliases = _header_aliases()
    found: dict[str, int] = {}
    unknown = []
    for index, title in enumerate(header):
        if not title:
            continue
        key = aliases.get(_fold(title))
        if key is None:
            unknown.append(title)
        elif key in found:
            raise ImportFileError(_("The column “%(name)s” appears twice.") % {"name": title})
        else:
            found[key] = index
    missing = [
        str(column.label) for column in COLUMNS if column.required and column.key not in found
    ]
    if missing:
        raise ImportFileError(
            _("Required columns are missing: %(columns)s. Start from the template.")
            % {"columns": ", ".join(missing)}
        )
    if unknown:
        raise ImportFileError(
            _("Unknown columns: %(columns)s. Use the template's headers.")
            % {"columns": ", ".join(unknown[:5])}
        )
    return found


def rows_as_dicts(upload: Upload) -> list[dict]:
    """The rows as ``{"line": n, "values": {key: text}}`` (what the preview keeps)."""
    columns = map_columns(upload.header)
    result = []
    for number, cells in upload.rows:
        values = {
            key: (cells[index] if index < len(cells) else "") for key, index in columns.items()
        }
        result.append({"line": number, "values": {key: values.get(key, "") for key in COLUMN_KEYS}})
    return result


# --- checking the rows --------------------------------------------------------------------


@dataclass
class RowResult:
    line: int
    values: dict
    errors: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    cleaned: dict = field(default_factory=dict)
    worker: Worker | None = None

    @property
    def ok(self) -> bool:
        return not self.errors


@dataclass
class ImportResult:
    rows: list[RowResult]
    created: list[Worker] = field(default_factory=list)
    new_trades: list[str] = field(default_factory=list)

    @property
    def error_count(self) -> int:
        return sum(1 for row in self.rows if row.errors)

    @property
    def ok(self) -> bool:
        return self.error_count == 0


class _Context:
    """What every row is checked against, loaded once per import."""

    def __init__(self, user, company: CompanySettings, today: datetime.date):
        self.user = user
        self.company = company
        self.today = today
        self.sites: dict[str, list] = {}
        for site in services.assignable_sites(user):
            self.sites.setdefault(_fold(site.name), []).append(site)
        self.trades = {
            _fold(trade.name): trade for trade in Trade.objects.filter(company=company.company)
        }
        self.pay_types = _labels(PayType.choices)
        self.id_types = _labels(Worker.IdDocumentType.choices)
        self.methods = _labels(PaymentMethod.choices)
        workers = company_workers(user)
        visible = set(services.scoped_workers(user, today).values_list("pk", flat=True))
        self.phones: dict[str, str] = {}
        self.ids: dict[str, str] = {}
        for pk, number, phone, id_number in workers.values_list(
            "pk", "number", "phone", "id_document_number"
        ):
            shown = number if pk in visible else ""
            if phone_key(phone):
                self.phones.setdefault(phone_key(phone), shown)
            if id_key(id_number):
                self.ids.setdefault(id_key(id_number), shown)
        self.money = MoneyField(decimals=company.currency_decimals)
        self.date_field = forms.DateField(
            input_formats=["%Y-%m-%d", "%d/%m/%Y", "%d.%m.%Y", "%d-%m-%Y"]
        )


def _check_row(row: dict, ctx: _Context) -> RowResult:
    values = row["values"]
    result = RowResult(line=row["line"], values=values)
    errors, cleaned = result.errors, result.cleaned

    refused = set()
    for key in TEXT_FIELDS:
        raw = values.get(key, "")
        if raw.startswith(FORMULA_PREFIXES):
            errors.append(
                _("%(column)s starts with “%(char)s”, which spreadsheets run as a formula.")
                % {"column": _column_label(key), "char": raw[:1]}
            )
            refused.add(key)
        cleaned[key] = "" if key in refused else " ".join(raw.split())

    for key in ("first_name", "last_name"):
        if not cleaned[key] and key not in refused:
            errors.append(_("%(column)s is required.") % {"column": _column_label(key)})

    phone = values.get("phone", "")
    if phone and (not _PHONE.match(phone) or not 6 <= len(phone_key(phone)) <= 20):
        errors.append(_("The phone number “%(phone)s” is not valid.") % {"phone": phone})
    cleaned["phone"] = " ".join(phone.split())

    trade_name = cleaned.pop("trade")
    cleaned["trade_name"] = trade_name
    if trade_name and _fold(trade_name) not in ctx.trades:
        result.notes.append(_("New trade “%(trade)s” (created on import).") % {"trade": trade_name})

    site_name = values.get("site", "")
    matches = ctx.sites.get(_fold(site_name), []) if site_name else []
    if not site_name:
        errors.append(_("%(column)s is required.") % {"column": _column_label("site")})
    elif not matches:
        errors.append(
            _("Unknown site “%(site)s”, or one you cannot add workers to.") % {"site": site_name}
        )
    elif len(matches) > 1:
        errors.append(_("Several sites are named “%(site)s”.") % {"site": site_name})
    else:
        cleaned["site"] = matches[0]

    start = values.get("start_date", "")
    cleaned["start_date"] = ctx.today
    if start:
        try:
            cleaned["start_date"] = ctx.date_field.clean(start)
        except ValidationError:
            errors.append(
                _("The start date “%(date)s” is not a date (use YYYY-MM-DD or DD/MM/YYYY).")
                % {"date": start}
            )

    pay_type = values.get("pay_type", "")
    cleaned["pay_type"] = PayType.DAILY
    if pay_type:
        if _fold(pay_type) in ctx.pay_types:
            cleaned["pay_type"] = ctx.pay_types[_fold(pay_type)]
        else:
            errors.append(_("Unknown pay type “%(value)s”.") % {"value": pay_type})

    rate = values.get("rate", "")
    if not rate:
        errors.append(_("%(column)s is required.") % {"column": _column_label("rate")})
    else:
        try:
            cleaned["amount"] = ctx.money.clean(rate)
        except ValidationError as exc:
            errors.append(
                _("Rate “%(value)s”: %(error)s") % {"value": rate, "error": " ".join(exc.messages)}
            )

    id_type = values.get("id_document_type", "")
    cleaned["id_document_type"] = Worker.IdDocumentType.NONE
    if id_type:
        if _fold(id_type) in ctx.id_types:
            cleaned["id_document_type"] = ctx.id_types[_fold(id_type)]
        else:
            errors.append(_("Unknown ID document type “%(value)s”.") % {"value": id_type})
    if cleaned["id_document_number"] and cleaned["id_document_type"] == Worker.IdDocumentType.NONE:
        if not id_type:
            errors.append(_("Give the ID document type for the ID number."))

    method = values.get("payment_method", "")
    cleaned["payment_method"] = None
    if method:
        if _fold(method) in ctx.methods:
            cleaned["payment_method"] = ctx.methods[_fold(method)]
        else:
            errors.append(_("Unknown payment method “%(value)s”.") % {"value": method})

    # Lengths and the rest of the model's own rules.
    probe = Worker(
        company=ctx.company.company,
        number="W-0000",
        first_name=cleaned["first_name"],
        last_name=cleaned["last_name"],
        phone=cleaned["phone"],
        id_document_type=cleaned["id_document_type"],
        id_document_number=cleaned["id_document_number"],
        payment_method=cleaned["payment_method"],
    )
    try:
        probe.full_clean(exclude=["photo", "number", "company", "trade"], validate_unique=False)
    except ValidationError as exc:
        for key, messages in exc.message_dict.items():
            if key in ("first_name", "last_name") and not cleaned[key]:
                continue  # already "is required" or refused
            errors.extend(f"{_column_label(key)}: {message}" for message in messages)
    if len(trade_name) > Trade._meta.get_field("name").max_length:
        errors.append(_("%(column)s is too long.") % {"column": _column_label("trade")})
    return result


def _column_label(key: str) -> str:
    for column in COLUMNS:
        if column.key == key:
            return str(column.label)
    return key


def _check_duplicates(results: list[RowResult], ctx: _Context) -> None:
    seen_phones: dict[str, int] = {}
    seen_ids: dict[str, int] = {}
    for result in results:
        phone = phone_key(result.cleaned.get("phone", ""))
        if phone:
            if phone in ctx.phones:
                result.errors.append(_existing(_("phone number"), ctx.phones[phone]))
            elif phone in seen_phones:
                result.errors.append(
                    _("Same phone number as row %(line)s.") % {"line": seen_phones[phone]}
                )
            else:
                seen_phones[phone] = result.line
        number = id_key(result.cleaned.get("id_document_number", ""))
        if number:
            if number in ctx.ids:
                result.errors.append(_existing(_("ID number"), ctx.ids[number]))
            elif number in seen_ids:
                result.errors.append(
                    _("Same ID number as row %(line)s.") % {"line": seen_ids[number]}
                )
            else:
                seen_ids[number] = result.line


def _existing(what: str, number: str) -> str:
    if number:
        return _("A worker of the company already has this %(what)s (%(number)s).") % {
            "what": what,
            "number": number,
        }
    return _("A worker of the company already has this %(what)s.") % {"what": what}


# --- creating -----------------------------------------------------------------------------


class _Abort(Exception):
    pass


def _create(result: RowResult, ctx: _Context, reason: str, new_trades: dict) -> Worker:
    cleaned = result.cleaned
    trade = None
    if cleaned["trade_name"]:
        key = _fold(cleaned["trade_name"])
        trade = ctx.trades.get(key) or new_trades.get(key)
        if trade is None:
            trade = Trade.objects.create(company=ctx.company.company, name=cleaned["trade_name"])
            new_trades[key] = trade
    return services.create_worker(
        actor=ctx.user,
        company=ctx.company.company,
        site=cleaned["site"],
        start_date=cleaned["start_date"],
        pay_type=cleaned["pay_type"],
        amount=cleaned["amount"],
        effective_from=cleaned["start_date"],
        reason=reason,
        first_name=cleaned["first_name"],
        last_name=cleaned["last_name"],
        phone=cleaned["phone"],
        trade=trade,
        id_document_type=cleaned["id_document_type"],
        id_document_number=cleaned["id_document_number"],
        payment_method=cleaned["payment_method"],
    )


def _create_rows(results, ctx, reason, *, stop_on_error: bool) -> tuple[list[Worker], dict]:
    created, new_trades = [], {}
    for result in results:
        if result.errors:
            continue
        try:
            with transaction.atomic():
                result.worker = _create(result, ctx, reason, new_trades)
        except ValidationError as exc:
            result.errors.extend(exc.messages)
            if stop_on_error:
                raise _Abort from exc
        else:
            created.append(result.worker)
    return created, new_trades


def run_import(rows: list[dict], user, *, commit: bool, file_name: str = "") -> ImportResult:
    """Check ``rows`` (from ``rows_as_dicts``); with ``commit``, create every worker in one
    transaction if no row has an error (else nothing is created)."""
    company = CompanySettings.load()
    ctx = _Context(user, company, dates.company_today(company))
    results = [_check_row(row, ctx) for row in rows]
    _check_duplicates(results, ctx)
    reason = _("Imported from %(file)s") % {"file": file_name or _("a file")}

    if not commit:
        # Dry run: the services' own rules (assignment, rates) are checked, then undone.
        with transaction.atomic():
            _create_rows(results, ctx, reason, stop_on_error=False)
            transaction.set_rollback(True)
        for result in results:
            result.worker = None
        return ImportResult(rows=results)

    if any(result.errors for result in results):
        return ImportResult(rows=results)
    try:
        with transaction.atomic():
            created, new_trades = _create_rows(results, ctx, reason, stop_on_error=True)
            AuditLog.record(
                actor=user,
                action="workers.import",
                after={
                    "file": file_name,
                    "count": len(created),
                    "numbers": [worker.number for worker in created],
                    "new_trades": sorted(trade.name for trade in new_trades.values()),
                },
                reason=reason,
            )
    except _Abort:
        for result in results:
            result.worker = None
        return ImportResult(rows=results)
    return ImportResult(
        rows=results,
        created=created,
        new_trades=sorted(trade.name for trade in new_trades.values()),
    )


# --- the template -------------------------------------------------------------------------


def _string_cell(cell, value) -> None:
    """Put typed text in a cell as text, never as a formula (site names, trades…)."""
    cell.value = value
    if isinstance(value, str):
        cell.data_type = "s"


def template_workbook(user) -> bytes:
    """The XLSX template, with the user's sites and the company's trades in drop-downs."""
    import openpyxl
    from openpyxl.styles import Font
    from openpyxl.utils import get_column_letter, quote_sheetname
    from openpyxl.worksheet.datavalidation import DataValidation

    company = CompanySettings.load()
    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet.title = _("Workers")[:31]
    bold = Font(bold=True)
    for index, column in enumerate(COLUMNS, 1):
        cell = sheet.cell(row=1, column=index)
        _string_cell(cell, str(column.label))
        cell.font = bold
        letter = get_column_letter(index)
        sheet.column_dimensions[letter].width = column.width
        if column.key in ("phone", "id_document_number", "first_name", "last_name"):
            # Text: Excel must keep "+228…" and leading zeros as typed.
            for row in range(2, MAX_ROWS + 2):
                sheet.cell(row=row, column=index).number_format = "@"
        elif column.key == "start_date":
            for row in range(2, MAX_ROWS + 2):
                sheet.cell(row=row, column=index).number_format = "yyyy-mm-dd"
    sheet.freeze_panes = "A2"

    lists = workbook.create_sheet(_("Lists")[:31])
    sites = sorted({site.name for site in services.assignable_sites(user)}, key=str.casefold)
    trades = list(
        Trade.objects.filter(company=company.company)
        .order_by("name")
        .values_list("name", flat=True)
    )
    choices = {
        "site": sites,
        "trade": trades,
        "pay_type": [str(label) for _value, label in PayType.choices],
        "id_document_type": [str(label) for _value, label in Worker.IdDocumentType.choices],
        "payment_method": [str(label) for _value, label in PaymentMethod.choices],
    }
    for list_index, (key, values) in enumerate(choices.items(), 1):
        header = lists.cell(row=1, column=list_index)
        _string_cell(header, _column_label(key))
        header.font = bold
        lists.column_dimensions[get_column_letter(list_index)].width = 26
        for row, value in enumerate(values, 2):
            _string_cell(lists.cell(row=row, column=list_index), value)
        if not values:
            continue
        letter = get_column_letter(list_index)
        source = f"{quote_sheetname(lists.title)}!${letter}$2:${letter}${len(values) + 1}"
        # Trades: a drop-down, but a new trade may be typed in.
        validation = DataValidation(
            type="list", formula1=source, allow_blank=True, showErrorMessage=key != "trade"
        )
        target = get_column_letter(COLUMN_KEYS.index(key) + 1)
        validation.add(f"{target}2:{target}{MAX_ROWS + 1}")
        sheet.add_data_validation(validation)

    notes = workbook.create_sheet(_("Instructions")[:31])
    notes.column_dimensions["A"].width = 24
    notes.column_dimensions["B"].width = 90
    lines = [
        (_("Import workers"), ""),
        ("", _("Fill one row per worker on the first sheet, from row 2. Keep the headers.")),
        (
            "",
            _("At most %(rows)s workers per file. You can also save the sheet as CSV.")
            % {"rows": MAX_ROWS},
        ),
        ("", _("Nothing is saved until you confirm the preview; then all rows are imported.")),
        ("", ""),
    ]
    for column in COLUMNS:
        required = _("Required") if column.required else _("Optional")
        lines.append((str(column.label), f"{required}. {column.help}".strip()))
    lines.append(("", ""))
    lines.append(
        (
            _("Currency"),
            _("Rates are in %(currency)s, with up to %(decimals)s decimal place(s).")
            % {"currency": company.currency, "decimals": company.currency_decimals},
        )
    )
    for row, (title, text) in enumerate(lines, 1):
        first = notes.cell(row=row, column=1)
        _string_cell(first, title)
        first.font = bold
        _string_cell(notes.cell(row=row, column=2), text)

    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


def cleaned_preview(result: RowResult) -> dict:
    """What the preview table shows for a row (the values as they will be saved)."""
    values = result.values
    return {
        "line": result.line,
        "name": " ".join(
            part for part in (values.get("first_name"), values.get("last_name")) if part
        ),
        "phone": values.get("phone", ""),
        "trade": values.get("trade", ""),
        "site": values.get("site", ""),
        "start_date": result.cleaned.get("start_date"),
        "pay_type": values.get("pay_type", ""),
        "rate": values.get("rate", ""),
        "errors": result.errors,
        "notes": result.notes,
    }
