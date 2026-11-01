"""Office views: company settings and the audit log viewer (admins only), the protected
downloads of uploaded files (signed-in users, scoped by site), and the Companies page
and company switcher (platform admins only), and the colour theme switch (everyone)."""

import datetime
from functools import wraps

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.core.paginator import Paginator
from django.http import HttpResponse, HttpResponseBadRequest, HttpResponseRedirect
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme
from django.utils.translation import gettext as _
from django.views.decorators.http import require_POST, require_safe

from apps.accounts.permissions import Role, role_required

from . import files, services, tenancy, theme
from .forms import CURRENCY_SUGGESTIONS, AuditLogFilterForm, CompanyForm, CompanySettingsForm
from .htmx import wants_fragment
from .models import AuditLog, Company, CompanySettings

AUDIT_PAGE_SIZE = 50


@role_required(Role.ADMIN)
def settings_view(request):
    company = CompanySettings.load(fresh=True)  # the form edits it
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
    company_entries = AuditLog.objects.filter(company=tenancy.current_company())
    form = AuditLogFilterForm(request.GET or None, entries=company_entries)
    entries = company_entries.select_related("actor", "content_type")
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


# --- companies (platform admins) -------------------------------------------------------------


def platform_admin_required(view):
    """Signed in as a platform admin (a superuser without a company), else 403."""

    @login_required
    @wraps(view)
    def wrapper(request, *args, **kwargs):
        if not tenancy.is_platform_admin(request.user):
            raise PermissionDenied
        return view(request, *args, **kwargs)

    return wrapper


@platform_admin_required
def companies(request):
    """Every company, and a form to add one (its settings start with the defaults)."""
    form = CompanyForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        company = services.create_company(form.cleaned_data["name"], actor=request.user)
        tenancy.switch_company(request, company)
        messages.success(
            request,
            _("%(name)s was added. You are now viewing it: set it up in Settings.")
            % {"name": company.name},
        )
        return redirect("core:settings")
    return render(
        request,
        "core/companies.html",
        {
            "form": form,
            "companies": Company.objects.order_by("name", "pk"),
            "current": tenancy.current_company_or_none(),
        },
    )


@platform_admin_required
@require_POST
def company_switch(request):
    """The sidebar's company switcher: view another company's data."""
    value = request.POST.get("company", "")
    company = get_object_or_404(Company, pk=int(value) if value.isdigit() else 0)
    tenancy.switch_company(request, company)
    messages.success(request, _("Now viewing %(name)s.") % {"name": company.name})
    return redirect("dashboard:home")


def _safe_next(request) -> str:
    """Where the no-JS switch goes back to: ``next``, else the referring page, else the
    dashboard; only ever on this site (as Django's ``set_language`` does)."""
    for candidate in (request.POST.get("next"), request.headers.get("referer")):
        if candidate and url_has_allowed_host_and_scheme(
            candidate, allowed_hosts={request.get_host()}, require_https=request.is_secure()
        ):
            return candidate
    return "/"


@require_POST
def set_theme(request):
    """The theme switch (components/theme_switch.html): light, dark or system.

    Open to everyone (signed out on the sign-in pages, workers on the portal). Saves
    ``User.theme`` for a signed-in office user, unless the switch is the portal's
    (``device=1``: there the choice belongs to the browser); always sets the cookie. An
    htmx post gets 204 (the page has already switched); a plain form post is sent back to
    a safe ``next``. No audit row: a display preference, not attendance or pay data.
    """
    value = request.POST.get("theme", "")
    if not theme.is_valid(value):
        return HttpResponseBadRequest(_("Choose light, dark or system."))
    user = request.user
    if user.is_authenticated and request.POST.get("device") != "1" and user.theme != value:
        user.theme = value
        user.save(update_fields=["theme"])
    if wants_fragment(request):
        response = HttpResponse(status=204)
    else:
        response = HttpResponseRedirect(_safe_next(request))
    theme.set_cookie(response, value)
    return response
