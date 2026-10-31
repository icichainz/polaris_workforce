"""Office views for workers: list, profile, add / edit, documents, Excel / CSV import.

Scoping: ``services.scoped_workers`` (built on ``accounts.permissions.scoped_sites``).
"""

import calendar
import datetime
import posixpath
import secrets

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.paginator import Paginator
from django.db.models import Q, Value
from django.db.models.functions import Replace
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils.translation import gettext as _
from django.utils.translation import ngettext
from django.views.decorators.http import require_POST, require_safe

from apps.accounts.permissions import Role, has_role, scoped_sites
from apps.attendance.models import AttendanceRecord
from apps.core import dates, files
from apps.core.choices import WEEKDAY_ABBRS, LateRule, PayType
from apps.core.models import CompanySettings
from apps.core.settings import resolve_setting
from apps.worksites import services as site_services
from apps.worksites.models import SiteAssignment

from . import display, importer, services
from .forms import WorkerDocumentForm, WorkerFilterForm, WorkerForm, WorkerImportForm
from .models import Worker, WorkerDocument

PAGE_SIZE = 25


def _get_worker_or_403(user, pk, today) -> Worker:
    worker = services.scoped_workers(user, today).select_related("trade").filter(pk=pk).first()
    if worker is None:
        raise PermissionDenied
    return worker


# --- list -------------------------------------------------------------------------------


def _filter_workers(workers, data, today):
    status = data.get("status") or "active"
    if status == "active":
        workers = workers.filter(is_active=True)
    elif status == "inactive":
        workers = workers.filter(is_active=False)
    if data.get("trade"):
        workers = workers.filter(trade=data["trade"])
    if data.get("site"):
        on_site = SiteAssignment.objects.filter(site=data["site"]).filter(
            Q(end_date__isnull=True) | Q(end_date__gte=today)
        )
        workers = workers.filter(pk__in=on_site.values("worker"))
    if data.get("documents") == "attention":
        workers = workers.filter(services.needs_document_attention(today))
    elif data.get("documents") == "expired":
        workers = workers.filter(services.has_expired_document(today))
    query = data.get("q") or ""
    if query:
        workers = workers.annotate(phone_digits=Replace("phone", Value(" "), Value("")))
        for term in query.split():
            workers = workers.filter(
                Q(first_name__icontains=term)
                | Q(last_name__icontains=term)
                | Q(number__icontains=term)
                | Q(phone__icontains=term)
                | Q(phone_digits__icontains=term)
            )
    return workers


def _site_names(workers, today) -> dict[int, str]:
    """The site each worker is on today, else the one they start on next."""
    found: dict[int, tuple] = {}
    assignments = (
        SiteAssignment.objects.filter(worker__in=workers)
        .filter(Q(end_date__isnull=True) | Q(end_date__gte=today))
        .select_related("site")
        .order_by("start_date")
    )
    for a in assignments:
        rank = 0 if a.start_date <= today else 1
        best = found.get(a.worker_id)
        if best is None or rank < best[0] or (rank == 0 and a.start_date > best[1]):
            found[a.worker_id] = (rank, a.start_date, a.site.name)
    return {worker_id: value[2] for worker_id, value in found.items()}


@login_required
def worker_list(request):
    company = CompanySettings.load()
    today = dates.company_today(company)
    money = display.Money(company)
    visible = services.scoped_workers(request.user, today)
    show_documents = services.can_view_documents(request.user)
    form = WorkerFilterForm(
        request.GET or None, sites=scoped_sites(request.user), show_documents=show_documents
    )
    data = form.cleaned_data if form.is_bound and form.is_valid() else {}

    workers = (
        _filter_workers(visible, data, today)
        .select_related("trade")
        .order_by("last_name", "first_name", "number")
    )
    page = Paginator(workers, PAGE_SIZE).get_page(request.GET.get("page"))
    on_page = list(page.object_list)
    rates = services.rates_on(on_page, today)
    statuses = services.statuses_on(on_page, today)
    owed = services.advances_owed(on_page)
    sites = _site_names(on_page, today)
    expiry = services.document_expiry_by_worker(on_page, today) if show_documents else {}

    rows = []
    for worker in on_page:
        rate = rates.get(worker.pk)
        rows.append(
            {
                "worker": worker,
                "initials": display.initials(worker),
                "site": sites.get(worker.pk, "—"),
                "rate": money(rate.amount, currency=False) if rate else "—",
                "unit": display.pay_unit(rate.pay_type) if rate else "",
                "status": statuses.get(worker.pk, "not_recorded"),
                "owes": money(owed[worker.pk], currency=False) if owed.get(worker.pk) else None,
                "documents": expiry.get(worker.pk),
            }
        )

    return render(
        request,
        "workers/list.html",
        {
            "form": form,
            "page": page,
            "rows": rows,
            "active_count": visible.filter(is_active=True).count(),
            "inactive_count": visible.filter(is_active=False).count(),
            "currency": company.currency,
            "can_create": services.can_edit_worker(request.user, None, today),
        },
    )


# --- profile ----------------------------------------------------------------------------


def _month_from(value, today) -> datetime.date:
    try:
        year, month = (int(part) for part in value.split("-"))
        return datetime.date(year, month, 1)
    except (AttributeError, ValueError):
        return today.replace(day=1)


def _shift_month(first: datetime.date, months: int) -> datetime.date:
    index = first.year * 12 + first.month - 1 + months
    return datetime.date(index // 12, index % 12 + 1, 1)


def _calendar(first, records, today, working_days):
    by_date = {r.day.date: r for r in records}
    weeks = []
    for week in calendar.Calendar(firstweekday=0).monthdatescalendar(first.year, first.month):
        cells = []
        for date in week:
            if date.month != first.month:
                cells.append(None)
                continue
            record = by_date.get(date)
            if record is not None:
                kind = record.status
                title = record.get_status_display()
            elif date.isoweekday() not in working_days:
                kind, title = "off", _("Day off")
            elif date > today:
                kind, title = "future", ""
            else:
                kind, title = "none", _("Not recorded")
            cells.append({"date": date, "kind": kind, "title": title, "is_today": date == today})
        weeks.append(cells)
    return weeks


def _setting_source(worker, site, name) -> str:
    if getattr(worker, name):
        return _("worker")
    if site is not None and getattr(site, name):
        return _("site")
    return _("default")


@login_required
def worker_detail(request, pk):
    company = CompanySettings.load()
    today = dates.company_today(company)
    worker = _get_worker_or_403(request.user, pk, today)
    return _render_detail(request, worker, company, today)


def _documents(user, worker, today) -> list[dict]:
    if not services.can_view_documents(user):
        return []
    return [
        {
            "document": document,
            "expiry": document.expiry(today),
            "url": files.worker_document_url(document),
        }
        for document in worker.documents.select_related("uploaded_by").order_by(
            "-uploaded_at", "-pk"
        )
    ]


def _render_detail(request, worker, company, today, *, document_form=None, status=200):
    tz = dates.company_tz(company)
    money = display.Money(company)

    first = _month_from(request.GET.get("month"), today)
    last = _shift_month(first, 1) - datetime.timedelta(days=1)
    records = list(
        AttendanceRecord.objects.filter(worker=worker, day__date__range=(first, last))
        .select_related("day")
        .order_by("day__date")
    )
    working_days = set(company.working_days or [])
    summary = services.month_summary(records, company.half_day_units)
    expected = services.working_days_between(first, min(last, today), working_days)

    assignment = site_services.current_or_next_assignment(worker, today)
    site = assignment.site if assignment else None
    rate = worker.rate_on(today)
    history = services.rate_history(worker)
    previous = next(
        (h["rate"] for h in history if rate and h["rate"].effective_from < rate.effective_from),
        None,
    )

    month_end = _shift_month(first, 1)
    paid = services.payments_between(
        worker,
        datetime.datetime.combine(first, datetime.time.min, tzinfo=tz),
        datetime.datetime.combine(month_end, datetime.time.min, tzinfo=tz),
        user=request.user,
    )
    advances = services.advance_summary(worker)

    # Only payments made on sites the user may see: a worker who moved between sites keeps
    # the old site's payments, and only that site's people may see their amounts, proofs
    # and payslips.
    payments = []
    recent = list(
        services.visible_payments(request.user, worker)
        .select_related("pay_line__period")
        .order_by("-paid_at")[:10]
    )
    for payment in recent:
        line = payment.pay_line
        paid_at = payment.paid_at.astimezone(tz)
        payments.append(
            {
                "date": paid_at.date(),
                "time": paid_at.strftime("%H:%M"),
                "units": display.decimal_text(line.units),
                "gross": money(line.gross, currency=False),
                "advance": money(line.advance_deduction, currency=False)
                if line.advance_deduction
                else None,
                "net": money(payment.amount, currency=False),
                "proof": payment.get_proof_type_display(),
                "proof_url": files.payment_proof_url(payment),
                "payslip_url": reverse("payroll:payslip", args=[line.pk]),
            }
        )

    method = resolve_setting("payment_method", site=site, worker=worker, company=company)
    frequency = resolve_setting("pay_frequency", site=site, worker=worker, company=company)
    every = resolve_setting("pay_every_n_days", site=site, worker=worker, company=company)
    counts = summary["counts"]

    context = {
        "worker": worker,
        "initials": display.initials(worker),
        "assignment": assignment,
        "site": site,
        "nav_active": "workers:list",
        "can_edit": services.can_edit_worker(request.user, worker, today),
        "can_give_advance": has_role(request.user, Role.ADMIN, Role.SITE_MANAGER),
        "rate": rate,
        "rate_text": money(rate.amount) if rate else "—",
        "previous_rate": money(previous.amount, currency=False) if previous else None,
        "month": first,
        "prev_month": _shift_month(first, -1),
        "next_month": _shift_month(first, 1) if _shift_month(first, 1) <= today else None,
        "weeks": _calendar(first, records, today, working_days),
        "weekday_names": WEEKDAY_ABBRS,
        "units": display.decimal_text(summary["units"]),
        "expected": expected,
        "expected_suffix": f"/ {expected}",
        "today_date": today,
        "attendance_percent": round(100 * summary["units"] / expected) if expected else None,
        "late_full_day": company.late_rule == LateRule.FULL_DAY_FLAGGED,
        "count_present": counts[AttendanceRecord.Status.PRESENT],
        "count_half": counts[AttendanceRecord.Status.HALF_DAY],
        "count_late": counts[AttendanceRecord.Status.LATE],
        "count_absent": counts[AttendanceRecord.Status.ABSENT],
        "days_off": [
            str(name)
            for day, name in enumerate(
                [
                    _("Mondays"),
                    _("Tuesdays"),
                    _("Wednesdays"),
                    _("Thursdays"),
                    _("Fridays"),
                    _("Saturdays"),
                    _("Sundays"),
                ],
                start=1,
            )
            if day not in working_days
        ],
        "paid_total": money(paid["total"]),
        "paid_count": paid["count"],
        "all_with_receipt": paid["all_with_proof"],
        "owed": money(advances["owed"]),
        "deduction": money(advances["deduction"], currency=False) if advances["owed"] else None,
        "advances_url": f"{reverse('payroll:advances')}?worker={worker.pk}",
        "history": [
            {
                "from": h["rate"].effective_from,
                "until": h["until"],
                "amount": money(h["rate"].amount, currency=False),
                "unit": display.pay_unit(h["rate"].pay_type),
            }
            for h in history
        ],
        "payments": payments,
        "payment_method": display.payment_method_text(method),
        "payment_method_source": _setting_source(worker, site, "payment_method"),
        "pay_frequency": display.pay_frequency_text(frequency, every),
        "pay_frequency_source": _setting_source(worker, site, "pay_frequency"),
        "pay_type": PayType(rate.pay_type).label if rate else None,
    }
    can_edit = context["can_edit"]
    documents = _documents(request.user, worker, today)
    context.update(
        {
            "show_documents": services.can_view_documents(request.user),
            "documents": documents,
            "expired_count": sum(1 for d in documents if d["expiry"] == "expired"),
            "expiring_count": sum(1 for d in documents if d["expiry"] == "expiring"),
            "document_form": (document_form or WorkerDocumentForm()) if can_edit else None,
            "show_document_form": document_form is not None,
        }
    )
    return render(request, "workers/detail.html", context, status=status)


# --- add / edit -------------------------------------------------------------------------


_SERVICE_FIELDS = {"start": "start_date", "end": "start_date", "amount": "rate"}


def _add_service_errors(form, exc: ValidationError):
    if hasattr(exc, "error_dict"):
        for key, errors in exc.message_dict.items():
            field = _SERVICE_FIELDS.get(key, key)
            form.add_error(field if field in form.fields else None, errors)
    else:
        form.add_error(None, exc.messages)


def _inherited(sites, company) -> dict:
    """What each site gives a worker who doesn't override (for the form's live hints)."""

    def entry(site):
        method = resolve_setting("payment_method", site=site, company=company)
        frequency = resolve_setting("pay_frequency", site=site, company=company)
        every = resolve_setting("pay_every_n_days", site=site, company=company)
        return {
            "payment_method": display.payment_method_text(method),
            "payment_method_source": _("From site · %(site)s") % {"site": site.name}
            if site.payment_method
            else _("Company default"),
            "pay_frequency": display.pay_frequency_text(frequency, every),
            "pay_frequency_source": _("From site · %(site)s") % {"site": site.name}
            if site.pay_frequency
            else _("Company default"),
        }

    data = {str(site.pk): entry(site) for site in sites}
    data[""] = {
        "payment_method": display.payment_method_text(company.payment_method),
        "payment_method_source": _("Company default"),
        "pay_frequency": display.pay_frequency_text(
            company.pay_frequency, company.pay_every_n_days
        ),
        "pay_frequency_source": _("Company default"),
    }
    return data


def _form_fields(form) -> dict:
    data = form.cleaned_data
    fields = {
        "first_name": data["first_name"],
        "last_name": data["last_name"],
        "phone": data["phone"],
        "trade": form.trade_value(),
        "id_document_type": data["id_document_type"],
        "id_document_number": data["id_document_number"],
        "payment_method": data["payment_method"],
        "pay_frequency": data["pay_frequency"],
        "emergency_contact_name": data["emergency_contact_name"],
        "emergency_contact_relationship": data["emergency_contact_relationship"],
        "emergency_contact_phone": data["emergency_contact_phone"],
    }
    if "is_active" in data:
        fields["is_active"] = data["is_active"]
    photo = data.get("photo")
    if photo:
        fields["photo"] = photo
    elif photo is False:
        fields["photo"] = None
    return fields


def _worker_form_view(request, worker):
    company = CompanySettings.load()
    today = dates.company_today(company)
    if not services.can_edit_worker(request.user, worker, today):
        raise PermissionDenied
    sites = services.assignable_sites(request.user).order_by("name")
    form = WorkerForm(
        request.POST or None,
        request.FILES or None,
        # A copy: validation fills the instance, the page header keeps the saved values.
        instance=Worker.objects.get(pk=worker.pk) if worker else Worker(),
        sites=sites,
        decimals=company.currency_decimals,
        today=today,
    )
    if request.method == "POST" and form.is_valid():
        data = form.cleaned_data
        pay = {
            "pay_type": data["pay_type"],
            "amount": data["rate"],
            "effective_from": data["effective_from"],
        }
        try:
            if worker is None:
                saved = services.create_worker(
                    actor=request.user,
                    site=data["site"],
                    start_date=data["start_date"],
                    **pay,
                    **_form_fields(form),
                )
                messages.success(request, _("Worker %(number)s added.") % {"number": saved.number})
            else:
                saved = services.update_worker(
                    Worker.objects.get(pk=worker.pk),
                    _form_fields(form),
                    actor=request.user,
                    site=data["site"],
                    start_date=data["start_date"],
                    **pay,
                )
                messages.success(request, _("Worker saved."))
        except ValidationError as exc:
            _add_service_errors(form, exc)
        else:
            return redirect("workers:detail", pk=saved.pk)

    current = None
    if worker is not None:
        assignment = site_services.current_or_next_assignment(worker, today)
        current = assignment.site if assignment else None
    inherited = _inherited(sites, company)
    selected = form["site"].value() or (current.pk if current else "")
    hint = inherited.get(str(selected), inherited[""])
    return render(
        request,
        "workers/form.html",
        {
            "form": form,
            "worker": worker,
            "current_site": current,
            "currency": company.currency,
            "inherited": inherited,
            "hint": hint,
            "nav_active": "workers:list",
        },
    )


@login_required
def worker_create(request):
    return _worker_form_view(request, None)


@login_required
def worker_edit(request, pk):
    company = CompanySettings.load()
    today = dates.company_today(company)
    return _worker_form_view(request, _get_worker_or_403(request.user, pk, today))


# --- documents --------------------------------------------------------------------------


def _editable_worker(request, pk):
    company = CompanySettings.load()
    today = dates.company_today(company)
    worker = _get_worker_or_403(request.user, pk, today)
    if not services.can_edit_worker(request.user, worker, today):
        raise PermissionDenied
    return worker, company, today


@login_required
@require_POST
def document_upload(request, pk):
    worker, company, today = _editable_worker(request, pk)
    form = WorkerDocumentForm(request.POST, request.FILES)
    if form.is_valid():
        data = form.cleaned_data
        try:
            services.add_document(
                worker,
                actor=request.user,
                file=data["file"],
                type=data["type"],
                title=data["title"],
                issued_on=data["issued_on"],
                expires_on=data["expires_on"],
            )
        except ValidationError as exc:
            _add_service_errors(form, exc)
        else:
            messages.success(request, _("Document added."))
            return redirect(reverse("workers:detail", args=[worker.pk]) + "#documents")
    return _render_detail(request, worker, company, today, document_form=form, status=400)


@login_required
@require_POST
def document_delete(request, pk, document_id):
    worker, _company, _today = _editable_worker(request, pk)
    document = get_object_or_404(WorkerDocument, pk=document_id, worker=worker)
    services.delete_document(document, actor=request.user)
    messages.success(request, _("Document “%(title)s” deleted.") % {"title": document.title})
    return redirect(reverse("workers:detail", args=[worker.pk]) + "#documents")


@login_required
@require_safe
def document_file(request, document_id):
    """The document's file (rules in ``apps.core.files.worker_document_or_404``)."""
    return files.file_response(request, files.worker_document_or_404(request.user, document_id))


# --- Excel / CSV import -----------------------------------------------------------------

IMPORT_SESSION_KEY = "workers_import"
XLSX_CONTENT_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def _check_can_import(user):
    company = CompanySettings.load()
    if not services.can_edit_worker(user, None, dates.company_today(company)):
        raise PermissionDenied
    return company


def _display_name(name: str) -> str:
    return posixpath.basename(str(name).replace("\\", "/"))[:120]


def _import_page(request, form, *, preview=None, summary=None, status=200):
    return render(
        request,
        "workers/import.html",
        {
            "form": form,
            "preview": preview,
            "summary": summary,
            "max_rows": importer.MAX_ROWS,
            "max_size_mb": importer.MAX_FILE_SIZE // (1024 * 1024),
            "columns": importer.COLUMNS,
            "nav_active": "workers:list",
        },
        status=status,
    )


def _preview(request, result, rows, file_name) -> dict:
    """Keep the rows in the session for the confirm step and describe the preview."""
    token = secrets.token_urlsafe(16)
    request.session[IMPORT_SESSION_KEY] = {"token": token, "file": file_name, "rows": rows}
    return {
        "token": token,
        "file": file_name,
        "rows": [importer.cleaned_preview(row) for row in result.rows],
        "total": len(result.rows),
        "error_count": result.error_count,
        "ok": result.ok,
    }


@login_required
def worker_import(request):
    """Upload the file → preview with the errors of each row (nothing saved yet)."""
    _check_can_import(request.user)
    form = WorkerImportForm(request.POST or None, request.FILES or None)
    preview = None
    if request.method == "POST" and form.is_valid():
        upload = form.cleaned_data["file"]
        try:
            rows = importer.rows_as_dicts(importer.read_upload(upload))
        except importer.ImportFileError as exc:
            form.add_error("file", exc.message)
        else:
            name = _display_name(upload.name)
            result = importer.run_import(rows, request.user, commit=False, file_name=name)
            preview = _preview(request, result, rows, name)
    status = 400 if form.is_bound and form.errors else 200
    return _import_page(request, form, preview=preview, status=status)


@login_required
@require_POST
def worker_import_confirm(request):
    """Create every worker of the previewed file in one transaction (all or nothing)."""
    _check_can_import(request.user)
    stored = request.session.get(IMPORT_SESSION_KEY)
    if not stored or request.POST.get("token") != stored.get("token"):
        messages.error(request, _("This preview has expired. Upload the file again."))
        return redirect("workers:import")
    result = importer.run_import(
        stored["rows"], request.user, commit=True, file_name=stored["file"]
    )
    if not result.ok:
        # Something changed since the preview (a phone number taken meanwhile…).
        messages.error(request, _("Nothing was imported: some rows have errors now."))
        preview = _preview(request, result, stored["rows"], stored["file"])
        return _import_page(request, WorkerImportForm(), preview=preview)
    del request.session[IMPORT_SESSION_KEY]
    messages.success(
        request,
        ngettext("%(n)s worker imported.", "%(n)s workers imported.", len(result.created))
        % {"n": len(result.created)},
    )
    summary = {
        "file": stored["file"],
        "workers": [
            {"worker": row.worker, "site": row.cleaned["site"].name}
            for row in result.rows
            if row.worker is not None
        ],
        "new_trades": result.new_trades,
    }
    return _import_page(request, WorkerImportForm(), summary=summary)


@login_required
@require_safe
def worker_import_template(request):
    """The XLSX template with the user's sites and the company's trades as drop-downs."""
    _check_can_import(request.user)
    response = HttpResponse(
        importer.template_workbook(request.user), content_type=XLSX_CONTENT_TYPE
    )
    response["Content-Disposition"] = 'attachment; filename="polaris-workers-template.xlsx"'
    response["Cache-Control"] = "private, no-store"
    return response
