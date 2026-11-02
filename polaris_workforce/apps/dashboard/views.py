"""Office views: the "Today" dashboard, reports (cost, cash and advances, checks) and
the exports."""

import io
from urllib.parse import parse_qsl, urlencode

from django import forms
from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import Http404, HttpResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from django.views.decorators.http import require_POST

from apps.accounts.permissions import (
    Role,
    company_workers,
    has_role,
    is_admin,
    role_required,
    scoped_sites,
)
from apps.analytics import services as analytics
from apps.analytics.models import AnalyticsSettings
from apps.core.dates import company_now, company_tz
from apps.core.models import CompanySettings
from apps.core.tenancy import current_company
from apps.workers.services import scoped_workers

from .forms import FILTER_FIELDS, BudgetWarningForm, ReportFilterForm
from .reports import XLSX_CONTENT_TYPE, build_report, write_csv, write_xlsx
from .services import today_overview

REPORT_ROLES = (Role.ADMIN, Role.ACCOUNTANT, Role.SITE_MANAGER)


@login_required
def home(request):
    company = CompanySettings.load()
    overview = today_overview(request.user, company=company)
    today = overview.today.isoformat()
    context = {
        "overview": overview,
        "company_settings": company,
        "can_export": has_role(request.user, *REPORT_ROLES),
        "export_query": "?" + urlencode({"period": "custom", "start": today, "end": today}),
    }
    # Dates and times on the page are shown in the company time zone.
    with timezone.override(company_tz(company)):
        return render(request, "dashboard/home.html", context)


def _filters(request):
    """The company, the filter form and the sites it selects (the user's sites, or the
    one site picked among them)."""
    company = CompanySettings.load()
    sites = scoped_sites(request.user)
    form = ReportFilterForm(
        request.GET or None, sites=sites.order_by("name"), today=company_now(company).date()
    )
    site = form.selected("site")
    return company, form, [site] if site else sites


def _report_context(request):
    company, form, sites = _filters(request)
    start, end = form.date_range()
    report = build_report(sites, start, end, trade=form.selected("trade"), company=company)
    return company, form, report


def _filter_query(query: str) -> str:
    """``query`` with the report filters only."""
    return urlencode([(key, value) for key, value in parse_qsl(query) if key in FILTER_FIELDS])


def _page_context(request, company, form, tab: str) -> dict:
    """What every reports tab shows: the filters and the links between the tabs."""
    return {
        "form": form,
        "company_settings": company,
        "query": _filter_query(request.GET.urlencode()),
        "period": form.selected("period") or "month",
        "tab": tab,
    }


@role_required(*REPORT_ROLES)
def reports(request):
    company, form, sites = _filters(request)
    start, end = form.date_range()
    trade = form.selected("trade")
    report = build_report(sites, start, end, trade=trade, company=company)
    today = company_now(company).date()
    previous_start, previous_end = analytics.previous_range(
        start, end, period=form.selected("period") or "month", today=today
    )
    previous = build_report(sites, previous_start, previous_end, trade=trade, company=company)
    warning = AnalyticsSettings.load()
    context = _page_context(request, company, form, "cost")
    context.update(
        {
            "report": report,
            "previous_start": previous_start,
            "previous_end": previous_end,
            "changes": analytics.report_changes(report, previous),
            "budgets": analytics.site_budgets(
                sites, today=today, settings=warning, with_spend_only=True
            ),
            "costs": analytics.cost_breakdown(sites, start, end, trade=trade),
            "warning": warning,
            "warning_form": BudgetWarningForm(instance=warning) if is_admin(request.user) else None,
        }
    )
    return render(request, "dashboard/reports.html", context)


@role_required(Role.ADMIN)
@require_POST
def budget_warning(request):
    """Set the company's budget warning (admins; the form sits on the Reports page)."""
    # Created here, not in the form's save: two first saves at once must not collide.
    warning, _created = AnalyticsSettings.objects.get_or_create(company=current_company())
    form = BudgetWarningForm(request.POST, instance=warning)
    if form.is_valid():
        analytics.save_settings(form.instance, actor=request.user)
        messages.success(request, _("Budget warning saved."))
    else:
        for error in form.errors.get("budget_warn_percent", []):
            messages.error(request, error)
    query = _filter_query(request.POST.get("query", ""))
    return redirect(reverse("dashboard:reports") + (f"?{query}" if query else "") + "#budgets")


@role_required(*REPORT_ROLES)
def reports_cash(request):
    """Cash and advances: every cash sheet of the period, unpaid pay and advances by age."""
    company, form, sites = _filters(request)
    start, end = form.date_range()
    today = company_now(company).date()
    site = form.selected("site")
    unpaid_sites, unpaid_total = analytics.unpaid_by_age(sites, today=today)
    workers = scoped_workers(request.user, today)
    advance_sites, advance_total = analytics.advances_by_age(
        workers, sites=scoped_sites(request.user), today=today, site=site
    )
    context = _page_context(request, company, form, "cash")
    context.update(
        {
            "start": start,
            "end": end,
            "today": today,
            "cash": analytics.cash_by_day(sites, start, end),
            "unpaid_sites": unpaid_sites,
            "unpaid_total": unpaid_total,
            "advance_sites": advance_sites,
            "advance_total": advance_total,
        }
    )
    return render(request, "dashboard/reports_cash.html", context)


@role_required(*REPORT_ROLES)
def reports_checks(request):
    """Integrity checks: the roll call against check-ins, corrections per foreman and
    check-in flags. Shared payout accounts for admins only: a site manager could be part
    of what that check looks for."""
    company, form, sites = _filters(request)
    start, end = form.date_range()
    context = _page_context(request, company, form, "checks")
    context.update(
        {
            "start": start,
            "end": end,
            "mismatches": analytics.check_in_mismatches(sites, start, end),
            "foremen": analytics.corrections_by_foreman(sites, start, end),
            "check_ins": analytics.check_in_flags(sites, start, end),
            "shared_accounts": (
                analytics.shared_payout_accounts(company_workers(request.user))
                if is_admin(request.user)
                else None
            ),
        }
    )
    return render(request, "dashboard/reports_checks.html", context)


@role_required(*REPORT_ROLES)
def reports_csv(request):
    _company, _form, report = _report_context(request)
    buffer = io.StringIO()
    buffer.write("﻿")  # UTF-8 byte-order mark: Excel then reads the accents correctly.
    write_csv(report, buffer)
    response = HttpResponse(buffer.getvalue(), content_type="text/csv; charset=utf-8")
    filename = f"polaris-attendance-{report.start:%Y-%m-%d}-{report.end:%Y-%m-%d}.csv"
    response["Content-Disposition"] = f'attachment; filename="{filename}"'
    return response


@role_required(*REPORT_ROLES)
def reports_xlsx(request):
    """The same report as an Excel workbook (one sheet per section)."""
    _company, _form, report = _report_context(request)
    response = HttpResponse(write_xlsx(report), content_type=XLSX_CONTENT_TYPE)
    filename = f"polaris-report-{report.start:%Y-%m-%d}-{report.end:%Y-%m-%d}.xlsx"
    response["Content-Disposition"] = f'attachment; filename="{filename}"'
    return response


class _GalleryForm(forms.Form):
    """Demo form for the component gallery only."""

    full_name = forms.CharField(label=_("Full name"), initial="Yao Mensah")
    trade = forms.ChoiceField(
        label=_("Trade"),
        choices=[("mason", _("Mason")), ("carpenter", _("Carpenter")), ("labourer", _("Labourer"))],
    )
    daily_rate = forms.IntegerField(label=_("Daily rate"), initial=6000)
    reason = forms.CharField(
        label=_("Correction reason"),
        widget=forms.TextInput(attrs={"placeholder": _("e.g. Bus breakdown, arrived 07:20")}),
    )
    note = forms.CharField(
        label=_("Note"),
        required=False,
        help_text=_("Shown to the foreman on the phone."),
        widget=forms.Textarea(attrs={"rows": 3}),
    )
    keep_signed_in = forms.BooleanField(
        label=_("Keep me signed in on this computer"), required=False, initial=True
    )


def components(request):
    """Component gallery for design review. Only available with DEBUG on."""
    if not settings.DEBUG:
        raise Http404
    form = _GalleryForm(data={"full_name": "Yao Mensah", "trade": "mason", "daily_rate": "6000"})
    form.is_valid()  # populate errors so the error state shows on "reason"
    return render(request, "dashboard/components.html", {"form": form, "unbound": _GalleryForm()})
