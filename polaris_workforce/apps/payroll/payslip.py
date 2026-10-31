"""The payslip PDF (Payslip artboard, A4 portrait) for one pay line."""

import datetime
from decimal import Decimal

from django.db.models import Sum
from django.utils import formats, timezone
from django.utils.translation import gettext as _

from apps.core.choices import PayFrequency, PaymentMethod, ProofType, status_letter
from apps.core.models import CompanySettings
from apps.core.money import format_money

from . import labels
from .models import Advance, PayLine
from .pdf import Document

INK = "#16191D"
INK_700 = "#3F4650"
STEEL = "#5B626E"
LINE = "#E3E5E9"
LINE_SOFT = "#EEF0F3"
SUBTLE = "#F8F9FA"
ADVANCE = "#8A4A06"

LEFT, RIGHT = 48.0, 547.28


def _status_word(status: str) -> str:
    return {
        "present": _("Present"),
        "absent": _("Absent"),
        "half_day": _("Half day"),
        "late": _("Late"),
    }.get(status, status)


def payslip_number(line: PayLine) -> str:
    return f"PS-{line.period.end_date:%Y}-{line.pk:05d}"


def payslip_filename(line: PayLine) -> str:
    return f"payslip-{line.worker.number}-{line.period.end_date.isoformat()}.pdf"


def render_payslip(line: PayLine, *, printed_at: datetime.datetime | None = None) -> bytes:
    company = CompanySettings.load(line.period.site.company_id)
    period, worker = line.period, line.worker
    site = period.site
    code, decimals = period.currency, period.currency_decimals

    def money(amount, currency=True):
        return format_money(int(amount), code if currency else "", decimals)

    payments = list(line.payments.select_related("paid_by").order_by("paid_at"))
    payment = payments[0] if payments else None
    still_owed = (
        Advance.objects.filter(worker=worker, balance__gt=0).aggregate(total=Sum("balance"))[
            "total"
        ]
        or 0
    )
    days = line.days or []
    last_day = days[-1] if days else {}
    applications = list(
        line.adjustment_applications.select_related("adjustment__source_line__period")
    )
    raised = list(line.raised_adjustments.select_related("applied_to__period"))
    bonuses = list(line.bonuses.order_by("date", "pk"))

    doc = Document(
        title=_("Pay statement") + f" {payslip_number(line)}", author=company.company_name
    )

    # Header: company on the left, statement on the right.
    y = 64.0
    doc.text(LEFT, y, company.company_name or _("Company"), size=16, bold=True)
    doc.text(RIGHT, y - 2, _("Pay statement"), size=14, bold=True, align="right")
    y += 16
    doc.text(LEFT, y, company.country or "", size=9, color=STEEL)
    doc.text(RIGHT, y, labels.period_label(period), size=10, color=INK_700, align="right")
    y += 14
    number = _("No. %(number)s") % {"number": payslip_number(line)}
    doc.text(RIGHT, y, number, size=9, color=STEEL, align="right")
    y += 14
    doc.line(LEFT, y, RIGHT, y, width=1.5, color=INK)

    # Worker block: 3 columns × 2 rows.
    if last_day and last_day.get("pay_type") == "piece":
        rate = _("Piece work")
    elif last_day:
        unit = _("hour") if last_day.get("pay_type") == "hourly" else _("day")
        rate = f"{money(last_day.get('rate', 0))} / {unit}"
    else:
        rate = "—"
    frequency = dict(PayFrequency.choices).get(period.frequency, period.frequency)
    method = dict(PaymentMethod.choices).get(
        payment.method if payment else "cash", PaymentMethod.CASH.label
    )
    fields = [
        (_("Worker"), worker.full_name),
        (_("Worker ID"), worker.number),
        (_("Trade"), worker.trade.name if worker.trade else "—"),
        (_("Site"), site.name),
        (_("Rate"), rate),
        (_("Paid"), _("%(frequency)s, %(method)s") % {"frequency": frequency, "method": method}),
    ]
    column = (RIGHT - LEFT) / 3
    y += 26
    for index, (label, value) in enumerate(fields):
        x = LEFT + column * (index % 3)
        row_y = y + 34 * (index // 3)
        doc.text(x, row_y, label.upper(), size=7.5, bold=True, color=STEEL)
        doc.text(x, row_y + 13, value, size=10.5, bold=True)
    y += 34 * 2 + 10

    # Days table.
    columns = [
        (LEFT + 8, _("Day"), "left"),
        (LEFT + 110, _("Status"), "left"),
        (LEFT + 290, _("Worked"), "right"),
        (LEFT + 380, _("Overtime"), "right"),
        (RIGHT - 8, _("Gross"), "right"),
    ]
    doc.rect(LEFT, y, RIGHT - LEFT, 22, fill=SUBTLE)
    for x, title, align in columns:
        doc.text(x, y + 14.5, title.upper(), size=7.5, bold=True, color=STEEL, align=align)
    y += 22
    for day in days:
        if y > 700:
            doc.add_page()
            y = 64.0
        date = datetime.date.fromisoformat(day["date"])
        if day.get("pay_type") == "hourly" and day.get("hours") is not None:
            worked = _("%(hours)s h") % {"hours": labels.number(day["hours"])}
        else:
            worked = labels.day_units_label(day.get("units", "0"))
        overtime = Decimal(day.get("overtime_hours") or "0")
        doc.text(columns[0][0], y + 15, labels.short_date(date), size=9.5)
        status = day.get("status", "")
        letter_width = doc.text(columns[1][0], y + 15, status_letter(status), size=9.5, bold=True)
        status_text = _status_word(status)
        if status == "late" and day.get("late_minutes"):
            status_text += " · " + _("%(minutes)s min") % {"minutes": day["late_minutes"]}
        rules_text = labels.day_rules_label(day)
        if rules_text:
            status_text += " · " + rules_text
        doc.text(columns[1][0] + letter_width + 5, y + 15, status_text, size=9.5)
        doc.text(columns[2][0], y + 15, worked, size=9.5, align="right")
        doc.text(
            columns[3][0],
            y + 15,
            _("%(hours)s h") % {"hours": labels.number(overtime)} if overtime else "—",
            size=9.5,
            align="right",
        )
        doc.text(columns[4][0], y + 15, money(day.get("gross", 0), False), size=9.5, align="right")
        y += 22
        doc.line(LEFT, y, RIGHT, y, color=LINE_SOFT)
    if not days:
        doc.text(LEFT + 8, y + 15, _("No attendance in this period."), size=9.5, color=STEEL)
        y += 22

    # Piece work paid on this line.
    for piece in line.pieces or []:
        if y > 700:
            doc.add_page()
            y = 64.0
        date = datetime.date.fromisoformat(piece["date"])
        doc.text(columns[0][0], y + 15, labels.short_date(date), size=9.5)
        doc.text(columns[1][0], y + 15, labels.piece_label(piece, decimals), size=9.5)
        doc.text(
            columns[4][0], y + 15, money(piece.get("amount", 0), False), size=9.5, align="right"
        )
        y += 22
        doc.line(LEFT, y, RIGHT, y, color=LINE_SOFT)

    # Totals, right-aligned box.
    y += 18
    box_x = RIGHT - 250
    totals = [(_("Days worked"), labels.number(line.units), False, INK)]
    if line.hours is not None:
        totals.append((_("Hours"), labels.number(line.hours), False, INK))
    if line.overtime_hours:
        totals.append((_("Overtime hours"), labels.number(line.overtime_hours), False, INK))
    if line.piece_amount:
        totals.append((_("Piece work"), money(line.piece_amount), False, INK))
    for bonus in bonuses:
        totals.append(
            (_("Bonus · %(reason)s") % {"reason": bonus.reason}, money(bonus.amount), False, INK)
        )
    totals.append((_("Gross"), money(line.gross), False, INK))
    if line.other_deductions:
        totals.append((_("Other deductions"), "− " + money(line.other_deductions), False, INK))
    # Statutory deductions, employee share, one row per rule.
    for item in line.deductions or []:
        if item.get("employee"):
            label = _("%(name)s (%(percent)s %%)") % {
                "name": item.get("name", ""),
                "percent": labels.number(item.get("employee_percent", "0")),
            }
            totals.append((label, "− " + money(item["employee"]), False, INK))
    totals.append(
        (
            _("Advance repaid"),
            ("− " + money(line.advance_deduction)) if line.advance_deduction else "—",
            False,
            ADVANCE,
        )
    )
    # Corrections of days paid earlier, settled on this pay: "Correction of 30 Oct".
    for application in applications:
        totals.append(
            (
                labels.adjustment_label(application.adjustment),
                labels.signed_money(application.amount, code, decimals),
                False,
                ADVANCE if application.amount < 0 else INK,
            )
        )
    totals.append(((_("Total paid") if payment else _("Net to pay")), money(line.net), True, INK))
    totals.append((_("Advance still owed"), money(still_owed), False, STEEL))
    height = 20 * len(totals) + 12
    doc.rect(box_x, y, RIGHT - box_x, height, stroke=LINE, width=0.8)
    row_y = y + 20
    for label, value, strong, color in totals:
        size = 11.5 if strong else 9.5
        if strong:
            doc.line(box_x + 12, row_y - 14, RIGHT - 12, row_y - 14, color=LINE)
        doc.text(box_x + 12, row_y, label, size=size, bold=strong, color=INK_700)
        doc.text(RIGHT - 12, row_y, value, size=size, bold=strong, color=color, align="right")
        row_y += 20

    # Receipt.
    if payment:
        proof = dict(ProofType.choices).get(payment.proof_type, payment.proof_type)
        paid_at = timezone.localtime(payment.paid_at)
        receipt = _("Paid %(when)s · receipt: %(proof)s") % {
            "when": formats.date_format(paid_at, _("j M Y H:i")),
            "proof": proof,
        }
    else:
        receipt = _("Not paid yet.")
    doc.text(LEFT, y + 20, _("Receipt").upper(), size=7.5, bold=True, color=STEEL)
    doc.text(LEFT, y + 34, receipt, size=9.5)
    # This pay was corrected after it was paid: say where the difference went (under
    # the totals box, full width).
    note_y = y + max(height, 50) + 18
    for adjustment in raised:
        doc.text(LEFT, note_y, labels.raised_note(adjustment), size=8.5, color=ADVANCE)
        note_y += 14
    y = note_y + 52

    # Signatures.
    half = (RIGHT - LEFT - 40) / 2
    paymaster = payment.paid_by.get_full_name() if payment and payment.paid_by else ""
    for index, label in enumerate(
        [
            _("Worker signature or thumb mark"),
            _("Paymaster · %(name)s") % {"name": paymaster} if paymaster else _("Paymaster"),
        ]
    ):
        x = LEFT + index * (half + 40)
        doc.line(x, y, x + half, y, width=0.8, color=INK_700)
        doc.text(x, y + 14, label, size=8.5, color=STEEL)

    # Footer.
    printed_at = timezone.localtime(printed_at or timezone.now())
    footer_y = 800.0
    doc.line(LEFT, footer_y - 14, RIGHT, footer_y - 14, color=LINE)
    doc.text(
        LEFT,
        footer_y,
        " · ".join(
            f"{status_letter(status)} {_status_word(status).lower()}"
            for status in ("present", "half_day", "late", "absent")
        ),
        size=8,
        color=STEEL,
    )
    doc.text(
        RIGHT,
        footer_y,
        _("Printed %(when)s · Polaris Workforce")
        % {"when": formats.date_format(printed_at, _("j M Y H:i"))},
        size=8,
        color=STEEL,
        align="right",
    )
    return doc.to_bytes()
