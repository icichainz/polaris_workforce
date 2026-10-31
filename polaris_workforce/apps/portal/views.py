"""Worker portal pages (mobile web): sign-in, home, days, pay, payslips, advances, PIN.

Anonymous entry (sign-in) finds the worker by phone first, then works inside
``use_company(worker.company)``. Every signed-in page goes through ``worker_required``:
it reads the portal session (never ``request.user``), runs the view inside the worker's
company and language, and marks the response private and uncacheable. Views pass only
the signed-in worker to the selectors, so any other worker's object is a 404.
"""

from __future__ import annotations

from functools import wraps

from django.conf import settings
from django.core.paginator import Paginator
from django.http import Http404, HttpResponse
from django.shortcuts import redirect, render
from django.utils import translation
from django.utils.cache import add_never_cache_headers
from django.utils.translation import gettext as _
from django.utils.translation import ngettext
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from apps.accounts.services import login_client_ip
from apps.core import dates
from apps.core.models import CompanySettings
from apps.core.templatetags.polaris import _CACHE_ATTR
from apps.core.tenancy import use_company
from apps.payroll.payslip import payslip_filename, render_payslip

from . import selectors, services
from .forms import ChangePinForm, SignInForm

PAY_PAGE_SIZE = 20


def portal_language(request, company: CompanySettings) -> str:
    """The worker's pick on the EN/FR switch (the language cookie), else the company's
    language, else the site default."""
    languages = dict(settings.LANGUAGES)
    chosen = request.COOKIES.get(settings.LANGUAGE_COOKIE_NAME)
    if chosen in languages:
        return chosen
    if company.language in languages:
        return company.language
    return settings.LANGUAGE_CODE


def _private(response):
    """Worker data: never kept by a shared phone's browser cache or a proxy."""
    add_never_cache_headers(response)
    return response


def worker_required(view):
    """Signed-in worker only (else to the portal sign-in). ``request.portal_access`` is
    their access; the view runs in their company, with their company's settings for
    ``{% money %}`` and their portal language."""

    @wraps(view)
    def wrapper(request, *args, **kwargs):
        access = services.session_access(request)
        if access is None:
            return redirect("portal:sign_in")
        request.portal_access = access
        with use_company(access.company):
            company = CompanySettings.load(access.company)
            # The {% money %} tag reads this; it may hold an office user's company.
            setattr(request, _CACHE_ATTR, company)
            language = portal_language(request, company)
            with translation.override(language):
                request.LANGUAGE_CODE = language
                response = view(request, access, company, *args, **kwargs)
        response.headers.setdefault("Content-Language", language)
        return _private(response)

    return wrapper


def _context(access, active: str, **extra) -> dict:
    return {
        "access": access,
        "worker": access.worker,
        "portal_active": active,
        **extra,
    }


# --- Sign-in and sign-out ----------------------------------------------------------------


def _wrong_pin_message(exc: services.WrongPin) -> str:
    if exc.attempts_left == 0:
        return _("Too many wrong PINs. Try again in %(minutes)s minutes.") % {
            "minutes": services.minutes_left(exc.locked_until)
        }
    return ngettext(
        "Wrong PIN. %(count)s try left.", "Wrong PIN. %(count)s tries left.", exc.attempts_left
    ) % {"count": exc.attempts_left}


@require_http_methods(["GET", "POST"])
def sign_in(request):
    if services.session_access(request) is not None:
        return redirect("portal:home")
    form = SignInForm(request.POST or None)
    error, status = None, 200
    if request.method == "POST":
        if not services.sign_in_allowed(login_client_ip(request)):
            error, status = _("Too many tries from this phone. Wait a minute and try again."), 429
        elif form.is_valid():
            try:
                access = services.sign_in(
                    phone=form.cleaned_data["phone"], pin=form.cleaned_data["pin"]
                )
            except services.WrongPin as exc:
                error, status = _wrong_pin_message(exc), 423 if exc.attempts_left == 0 else 400
            except services.PinLocked as exc:
                error = _("Too many wrong PINs. Try again in %(minutes)s minutes.") % {
                    "minutes": services.minutes_left(exc.locked_until)
                }
                status = 423
            except services.SignInRefused:
                error, status = _("Wrong phone number or PIN."), 400
            else:
                services.start_session(request, access)
                return redirect("portal:home")
        else:
            status = 400
    response = render(request, "portal/sign_in.html", {"form": form, "error": error}, status=status)
    if status == 429:
        response["Retry-After"] = str(services.SIGN_IN_WINDOW)
    return _private(response)


@require_POST
def sign_out(request):
    services.end_session(request)
    return redirect("portal:sign_in")


# --- Pages ------------------------------------------------------------------------------


@require_GET
@worker_required
def home(request, access, company):
    data = selectors.home(access.worker, company)
    return render(request, "portal/home.html", _context(access, "home", **data))


@require_GET
@worker_required
def days(request, access, company):
    today = dates.company_today(company)
    first = selectors.month_from(request.GET.get("month"), today)
    data = selectors.month_days(access.worker, company, first)
    return render(request, "portal/days.html", _context(access, "days", **data))


@require_GET
@worker_required
def pay(request, access, company):
    page = Paginator(selectors.pay_lines(access.worker), PAY_PAGE_SIZE).get_page(
        request.GET.get("page")
    )
    context = _context(
        access,
        "pay",
        page=page,
        rows=selectors.pay_rows(page.object_list),
        payments=selectors.payments(access.worker, company) if page.number == 1 else [],
        adjustments=selectors.adjustments(access.worker) if page.number == 1 else [],
    )
    return render(request, "portal/pay.html", context)


@require_GET
@worker_required
def payslip(request, access, company, line_id):
    line = selectors.own_line(access.worker, line_id)
    if line is None:
        raise Http404
    response = HttpResponse(render_payslip(line), content_type="application/pdf")
    response["Content-Disposition"] = f'attachment; filename="{payslip_filename(line)}"'
    return response


@require_GET
@worker_required
def advances(request, access, company):
    data = selectors.advances(access.worker)
    return render(request, "portal/advances.html", _context(access, "advances", **data))


@require_http_methods(["GET", "POST"])
@worker_required
def change_pin(request, access, company):
    form = ChangePinForm(request.POST or None)
    status = 200
    if request.method == "POST":
        status = 400
        if form.is_valid():
            try:
                services.change_own_pin(
                    access,
                    current_pin=form.cleaned_data["current_pin"],
                    new_pin=form.cleaned_data["pin"],
                )
            except services.WrongPin as exc:
                if exc.attempts_left == 0:
                    services.end_session(request)
                    return redirect("portal:sign_in")
                form.add_error("current_pin", _wrong_pin_message(exc))
            except services.PinLocked:
                services.end_session(request)
                return redirect("portal:sign_in")
            except services.AccessError as exc:
                form.add_error("pin", str(exc))
            else:
                services.refresh_session(request, access)
                return render(
                    request,
                    "portal/change_pin.html",
                    _context(access, "pin", form=None, done=True),
                )
    return render(
        request, "portal/change_pin.html", _context(access, "pin", form=form), status=status
    )
