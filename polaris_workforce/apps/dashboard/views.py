"""Office views: the "Today" dashboard, reports and the CSV export."""

import io
from urllib.parse import urlencode

from django import forms
from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.http import Http404, HttpResponse
from django.shortcuts import render
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from apps.accounts.permissions import Role, has_role, role_required, scoped_sites
from apps.core.dates import company_now, company_tz
from apps.core.models import CompanySettings

from .forms import ReportFilterForm
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


def _report_context(request):
    company = CompanySettings.load()
    sites = scoped_sites(request.user)
    form = ReportFilterForm(
        request.GET or None, sites=sites.order_by("name"), today=company_now(company).date()
    )
    start, end = form.date_range()
    site = form.selected("site")
    report = build_report(
        [site] if site else sites, start, end, trade=form.selected("trade"), company=company
    )
    return company, form, report


@role_required(*REPORT_ROLES)
def reports(request):
    company, form, report = _report_context(request)
    return render(
        request,
        "dashboard/reports.html",
        {
            "form": form,
            "report": report,
            "company_settings": company,
            "query": request.GET.urlencode(),
            "period": form.selected("period") or "month",
        },
    )


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
