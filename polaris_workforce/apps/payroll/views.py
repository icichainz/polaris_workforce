"""Office screens for payroll: pay periods, cash sheets, advances, payslips.

Views stay thin: figures and rules come from ``services`` (and its ``engine``), site
scoping from ``apps.accounts.permissions``.
"""

import datetime

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.paginator import Paginator
from django.db.models import Count, Sum
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils.translation import gettext as _
from django.views.decorators.http import require_GET, require_POST

from apps.accounts.permissions import can_manage_site, get_site_or_403, scoped_sites
from apps.api.exceptions import PolarisAPIException
from apps.api.idempotency import run_once
from apps.core.choices import PayFrequency
from apps.core.models import CompanySettings
from apps.worksites.models import Site

from . import services
from .forms import AdvanceForm, CashIssueForm, LockForm, MarkPaidForm, UndoPaymentForm
from .models import Advance, PayLine, Payment, PayPeriod
from .payslip import payslip_filename, render_payslip

PAGE_SIZE = 50
RECENT_DAYS = 7


def _errors(exc) -> list[str]:
    if isinstance(exc, ValidationError):
        return exc.messages
    return [str(exc.detail)]


def _site_filter(request, sites):
    site_id = request.GET.get("site", "")
    if site_id.isdigit():
        return sites.filter(pk=int(site_id)).first()
    return None


def _sync_recent(sites, today, actor):
    """Bring the last week's pay up to date (the attendance services also trigger this)."""
    company = CompanySettings.load()
    for site in sites.filter(status=Site.Status.ACTIVE):
        services.sync_site(
            site,
            today - datetime.timedelta(days=RECENT_DAYS - 1),
            today,
            actor=actor,
            today=today,
            company=company,
        )


def _period_badge(period: PayPeriod, today: datetime.date):
    """(status_badge status, label) for the Pay periods table."""
    status = period.status
    if status == PayPeriod.Status.OPEN:
        if today < period.end_date:
            return "draft", _("Open · in progress")
        return "submitted", _("Waiting approval")
    if status == PayPeriod.Status.READY:
        return "to_pay", _("Ready to pay")
    if status == PayPeriod.Status.PAYING:
        return "to_pay", _("Paying")
    if status == PayPeriod.Status.PAID:
        return "paid", _("Paid")
    return "locked", _("Locked")


# --- pay periods -------------------------------------------------------------------------


@login_required
@require_GET
def periods(request):
    company = CompanySettings.load()
    today = services.company_today(company)
    sites = scoped_sites(request.user).order_by("name")
    site = _site_filter(request, sites)
    shown_sites = sites.filter(pk=site.pk) if site else sites
    _sync_recent(shown_sites, today, request.user)

    tab = request.GET.get("status", "open")
    if tab not in ("open", "locked", "all"):
        tab = "open"
    base = PayPeriod.objects.filter(site__in=shown_sites)
    open_count = base.exclude(status=PayPeriod.Status.LOCKED).count()
    queryset = base
    if tab == "open":
        queryset = queryset.exclude(status=PayPeriod.Status.LOCKED)
    elif tab == "locked":
        queryset = queryset.filter(status=PayPeriod.Status.LOCKED)
    queryset = (
        queryset.select_related("site")
        .annotate(
            workers=Count("lines", distinct=True),
            gross=Sum("lines__gross"),
            deductions=Sum("lines__advance_deduction") + Sum("lines__other_deductions"),
            net=Sum("lines__net"),
        )
        .order_by("-end_date", "site__name")
    )
    page = Paginator(queryset, PAGE_SIZE).get_page(request.GET.get("page"))
    paid_counts = dict(
        PayLine.objects.filter(period__in=[p.pk for p in page], payments__isnull=False)
        .values_list("period_id")
        .annotate(n=Count("id", distinct=True))
    )
    rows = []
    for period in page:
        badge, badge_label = _period_badge(period, today)
        workers = period.workers or 0
        paid = paid_counts.get(period.pk, 0)
        rows.append(
            {
                "period": period,
                "badge": badge,
                "badge_label": badge_label,
                "workers": workers,
                "paid": paid,
                "progress": round(100 * paid / workers) if workers else 0,
                "has_figures": period.status != PayPeriod.Status.OPEN or bool(period.gross),
                "sheet_url": reverse(
                    "payroll:cash_sheet", args=[period.site_id, period.end_date.isoformat()]
                ),
            }
        )

    week_start = today - datetime.timedelta(days=today.isoweekday() - 1)
    week = PayLine.objects.filter(
        period__site__in=shown_sites,
        period__end_date__gte=week_start,
        period__end_date__lte=week_start + datetime.timedelta(days=6),
    )
    week_totals = week.aggregate(
        gross=Sum("gross"), advances=Sum("advance_deduction"), net=Sum("net")
    )
    paid_out = (
        Payment.objects.filter(pay_line__in=week).aggregate(total=Sum("amount"))["total"] or 0
    )
    context = {
        "company": company,
        "rows": rows,
        "page": page,
        "tab": tab,
        "open_count": open_count,
        "sites": sites,
        "site": site,
        "week": {
            "gross": week_totals["gross"] or 0,
            "advances": week_totals["advances"] or 0,
            "paid": paid_out,
            "left": (week_totals["net"] or 0) - paid_out,
        },
        "frequency_note": _frequency_note(company, sites),
    }
    return render(request, "payroll/periods.html", context)


def _frequency_note(company, sites) -> str:
    """ "Daily by default · Route 4 Bridge pays weekly"."""
    names = dict(PayFrequency.choices)
    note = _("%(frequency)s by default") % {"frequency": names.get(company.pay_frequency, "")}
    others = [
        _("%(site)s pays %(frequency)s")
        % {"site": s.name, "frequency": str(names.get(s.pay_frequency, "")).lower()}
        for s in sites
        if s.pay_frequency and s.pay_frequency != company.pay_frequency
    ][:2]
    return " · ".join([note, *others])


# --- cash sheets -------------------------------------------------------------------------


@login_required
@require_GET
def cash_sheets(request):
    company = CompanySettings.load()
    today = services.company_today(company)
    sites = scoped_sites(request.user).order_by("name")
    site = _site_filter(request, sites)
    shown_sites = sites.filter(pk=site.pk) if site else sites
    for active in shown_sites.filter(status=Site.Status.ACTIVE):
        services.sync_sheet(active, today, actor=request.user, today=today, company=company)

    groups = (
        PayPeriod.objects.filter(site__in=shown_sites)
        .values("site_id", "end_date")
        .annotate(n=Count("id"))
        .order_by("-end_date", "site_id")
    )
    page = Paginator(groups, PAGE_SIZE).get_page(request.GET.get("page"))
    site_by_id = {s.pk: s for s in sites}
    sheets = [
        services.cash_sheet(site_by_id[g["site_id"]], g["end_date"], refresh=False) for g in page
    ]
    return render(
        request,
        "payroll/cash_sheets.html",
        {"sheets": sheets, "page": page, "sites": sites, "site": site, "today": today},
    )


def _sheet_context(request, sheet, **extra):
    company = CompanySettings.load()
    can_manage = can_manage_site(request.user, sheet.site)
    context = {
        "sheet": sheet,
        "nav_active": "payroll:cash_sheets",
        "company": company,
        "can_pay": services.can_pay(request.user, sheet.site) and sheet.can_pay,
        "can_manage": can_manage,
        "can_lock": services.can_lock(request.user, sheet.site) and not sheet.is_closed,
        "proof_choices": MarkPaidForm(company=company).fields["proof_type"].choices,
        "issue_form": CashIssueForm(company=company),
        "lock_form": LockForm(),
        "rows": [_row(request, sheet, sl, company) for sl in sheet.lines],
    }
    context.update(extra)
    return context


def _row(request, sheet, sheet_line, company, form=None, errors=None):
    return {
        "sl": sheet_line,
        "form": form or MarkPaidForm(company=company),
        "errors": errors or [],
        "payslip_url": reverse("payroll:payslip", args=[sheet_line.line.pk]),
    }


@login_required
@require_GET
def cash_sheet(request, site_id, date):
    site = get_site_or_403(request.user, site_id)
    sheet = services.cash_sheet(site, date, actor=request.user)
    return render(request, "payroll/cash_sheet.html", _sheet_context(request, sheet))


def _sheet_url(site_id, date) -> str:
    return reverse("payroll:cash_sheet", args=[site_id, date.isoformat()])


@login_required
@require_POST
def cash_sheet_pay(request, line_id):
    line = get_object_or_404(PayLine.objects.select_related("period"), pk=line_id)
    site = get_site_or_403(request.user, line.period.site_id)
    company = CompanySettings.load()
    form = MarkPaidForm(request.POST, request.FILES, company=company)
    errors = []
    if form.is_valid():
        data = form.cleaned_data
        try:
            run_once(
                Payment.objects,
                data["client_uuid"],
                create=lambda: services.record_payment(
                    request.user,
                    line,
                    amount=line.net - (line.payments.aggregate(t=Sum("amount"))["t"] or 0),
                    proof_type=data["proof_type"],
                    proof_file=data.get("proof_file"),
                    client_uuid=data["client_uuid"],
                ),
                matches=lambda p: p.pay_line_id == line.pk,
            )
        except (ValidationError, PolarisAPIException) as exc:
            errors = _errors(exc)
    else:
        errors = [e for field_errors in form.errors.values() for e in field_errors]
    if not request.headers.get("HX-Request"):
        for error in errors:
            messages.error(request, error)
        if not errors:
            messages.success(request, _("Payment recorded."))
        return redirect(_sheet_url(site.pk, line.period.end_date))
    return _sheet_partial(request, site, line, form if errors else None, errors)


def _sheet_partial(request, site, line, form=None, errors=None):
    """htmx: the updated worker row plus the reconciliation strip (out of band)."""
    sheet = services.cash_sheet(site, line.period.end_date, refresh=False)
    company = CompanySettings.load()
    sheet_line = next(sl for sl in sheet.lines if sl.line.pk == line.pk)
    context = _sheet_context(request, sheet)
    context["row"] = _row(request, sheet, sheet_line, company, form=form, errors=errors)
    context["oob"] = True
    return render(request, "payroll/_cash_sheet_row.html", context)


@login_required
@require_POST
def payment_undo(request, payment_id):
    payment = get_object_or_404(Payment.objects.select_related("pay_line__period"), pk=payment_id)
    line = payment.pay_line
    site = get_site_or_403(request.user, line.period.site_id)
    form = UndoPaymentForm(request.POST)
    reason = form.cleaned_data["reason"] if form.is_valid() else ""
    errors = []
    try:
        services.undo_payment(request.user, payment, reason)
    except (ValidationError, PolarisAPIException) as exc:
        errors = _errors(exc)
    if request.headers.get("HX-Request"):
        return _sheet_partial(request, site, line, errors=errors)
    for error in errors:
        messages.error(request, error)
    return redirect(_sheet_url(site.pk, line.period.end_date))


@login_required
@require_POST
def cash_issue(request, site_id, date):
    site = get_site_or_403(request.user, site_id)
    form = CashIssueForm(request.POST, company=CompanySettings.load())
    if form.is_valid():
        try:
            services.issue_cash(
                request.user,
                site,
                date,
                form.cleaned_data["amount"],
                note=form.cleaned_data["note"],
            )
            messages.success(request, _("Cash issue recorded."))
        except (ValidationError, PolarisAPIException) as exc:
            for error in _errors(exc):
                messages.error(request, error)
    else:
        for field_errors in form.errors.values():
            for error in field_errors:
                messages.error(request, error)
    return redirect(_sheet_url(site.pk, date))


@login_required
@require_POST
def cash_sheet_lock(request, site_id, date):
    site = get_site_or_403(request.user, site_id)
    form = LockForm(request.POST)
    reason = form.cleaned_data["reason"] if form.is_valid() else ""
    try:
        services.lock_sheet(request.user, site, date, reason)
        messages.success(request, _("The cash sheet is closed and locked."))
    except (ValidationError, PolarisAPIException) as exc:
        for error in _errors(exc):
            messages.error(request, error)
    return redirect(_sheet_url(site.pk, date))


# --- advances ----------------------------------------------------------------------------


def _advance_rows(user):
    return (
        Advance.objects.filter(worker__in=services.scoped_workers(user))
        .select_related("worker")
        .order_by("-given_on", "-pk")
    )


@login_required
def advances(request):
    company = CompanySettings.load()
    today = services.company_today(company)
    workers = (
        services.scoped_workers(request.user)
        .filter(is_active=True)
        .order_by("last_name", "first_name")
    )
    form = AdvanceForm(company=company, workers=workers)
    if request.method == "POST":
        form = AdvanceForm(request.POST, request.FILES, company=company, workers=workers)
        if form.is_valid():
            data = form.cleaned_data
            try:
                services.create_advance(
                    request.user,
                    data["worker"],
                    amount=data["amount"],
                    deduction_per_pay=data["deduction_per_pay"],
                    reason=data["reason"],
                    proof_type=data["proof_type"],
                    proof_file=data.get("proof_file"),
                )
            except PermissionDenied:
                form.add_error("worker", _("You cannot give an advance to this worker."))
            except ValidationError as exc:
                for field, field_errors in exc.message_dict.items():
                    form.add_error(field if field in form.fields else None, field_errors)
            else:
                messages.success(request, _("Advance recorded."))
                return redirect("payroll:advances")
    elif request.method != "GET":
        return HttpResponse(status=405)

    show = request.GET.get("show", "open")
    rows = _advance_rows(request.user)
    owed = rows.filter(balance__gt=0)
    if show != "all":
        rows = owed
    page = Paginator(rows, PAGE_SIZE).get_page(request.GET.get("page"))
    for advance in page:
        site = services.current_site(advance.worker, today)
        advance.site_name = site.name if site else ""
        advance.repaid_percent = round(100 * (advance.amount - advance.balance) / advance.amount)
    context = {
        "company": company,
        "form": form,
        "page": page,
        "show": show,
        "owed_workers": owed.values("worker").distinct().count(),
        "owed_total": owed.aggregate(total=Sum("balance"))["total"] or 0,
        "preview": _preview_context(form, company) if form.is_bound else None,
    }
    return render(request, "payroll/advances.html", context)


def _preview_context(form, company):
    """The live explanation under the advance form, from whatever is typed so far."""
    worker = None
    amount = per_pay = 0
    field = form.fields["worker"]
    try:
        worker = field.clean(form.data.get("worker"))
    except ValidationError:
        return None
    for name in ("amount", "deduction_per_pay"):
        try:
            value = form.fields[name].clean(form.data.get(name)) or 0
        except ValidationError:
            value = 0
        if name == "amount":
            amount = value
        else:
            per_pay = value
    check = services.advance_check(worker, amount, per_pay, company=company)
    return {"worker": worker, "check": check}


@login_required
@require_GET
def advance_preview(request):
    company = CompanySettings.load()
    workers = services.scoped_workers(request.user)
    form = AdvanceForm(request.GET, company=company, workers=workers)
    return render(
        request,
        "payroll/_advance_preview.html",
        {"preview": _preview_context(form, company), "company": company},
    )


# --- payslips ----------------------------------------------------------------------------


@login_required
@require_GET
def payslip(request, line_id):
    line = (
        PayLine.objects.select_related("period__site", "worker__trade").filter(pk=line_id).first()
    )
    if line is None:
        raise Http404
    get_site_or_403(request.user, line.period.site_id)
    response = HttpResponse(render_payslip(line), content_type="application/pdf")
    response["Content-Disposition"] = f'inline; filename="{payslip_filename(line)}"'
    return response
