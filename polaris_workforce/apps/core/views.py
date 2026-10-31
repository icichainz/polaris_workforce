"""Office views: company settings and the audit log viewer (admins only), and the
protected downloads of uploaded files (signed-in users, scoped by site)."""

import datetime

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.shortcuts import redirect, render
from django.utils import timezone
from django.utils.translation import gettext as _
from django.views.decorators.http import require_safe

from apps.accounts.permissions import Role, role_required

from . import files, services
from .forms import CURRENCY_SUGGESTIONS, AuditLogFilterForm, CompanySettingsForm
from .models import AuditLog, CompanySettings

AUDIT_PAGE_SIZE = 50


@role_required(Role.ADMIN)
def settings_view(request):
    company = CompanySettings.load()
    before = services.settings_snapshot(company)
    if request.method == "POST":
        form = CompanySettingsForm(request.POST, instance=company)
        if form.is_valid():
            change = services.save_company_settings(form, actor=request.user, before=before)
            if not change.changed:
                messages.info(request, _("Nothing changed."))
            else:
                messages.success(request, _("Settings saved."))
            if change.currency_changed:
                messages.warning(
                    request,
                    _(
                        "Amounts now show in %(currency)s with %(decimals)d decimal place(s). "
                        "Existing and locked pay periods keep the currency they were "
                        "created with."
                    )
                    % {"currency": company.currency, "decimals": company.currency_decimals},
                )
            return redirect("core:settings")
        messages.error(request, _("Some settings need fixing — see the highlighted fields."))
    else:
        form = CompanySettingsForm(instance=company)
    return render(
        request,
        "core/settings.html",
        {
            "form": form,
            "company_settings": CompanySettings.load() if request.method == "POST" else company,
            "currency_suggestions": CURRENCY_SUGGESTIONS,
            "saved_currency": before["currency"],
            "saved_decimals": before["currency_decimals"],
        },
    )


@role_required(Role.ADMIN)
def audit_log_view(request):
    form = AuditLogFilterForm(request.GET or None)
    entries = AuditLog.objects.select_related("actor", "content_type")
    if form.is_bound and form.is_valid():
        data = form.cleaned_data
        if data["actor"]:
            entries = entries.filter(actor=data["actor"])
        if data["target_type"]:
            entries = entries.filter(content_type=data["target_type"])
        if data["date_from"]:
            entries = entries.filter(created_at__gte=_day_start(data["date_from"]))
        if data["date_to"]:
            next_day = data["date_to"] + datetime.timedelta(days=1)
            entries = entries.filter(created_at__lt=_day_start(next_day))
    page = Paginator(entries, AUDIT_PAGE_SIZE).get_page(request.GET.get("page"))
    rows = [(entry, services.audit_diff(entry.before, entry.after)) for entry in page]
    query = request.GET.copy()
    query.pop("page", None)
    return render(
        request,
        "core/audit_log.html",
        {
            "form": form,
            "page": page,
            "rows": rows,
            "query": query.urlencode(),
            "nav_active": "core:settings",
        },
    )


def _day_start(date: datetime.date) -> datetime.datetime:
    return timezone.make_aware(datetime.datetime.combine(date, datetime.time.min))


# --- protected file downloads (rules in apps.core.files) -----------------------------------


@login_required
@require_safe
def worker_photo(request, pk):
    return files.file_response(request, files.worker_photo_or_404(request.user, pk))


@login_required
@require_safe
def payment_proof(request, pk):
    return files.file_response(request, files.payment_proof_or_404(request.user, pk))


@role_required(Role.ADMIN)
@require_safe
def media_file(request, name):
    """``FieldFile.url`` (Django admin's file links): admins only, like the admin."""
    return files.file_response(request, files.media_name_or_404(request.user, name))
