"""Office screens for the worker portal: who has access, give or reset a PIN, revoke.

Admins manage every worker of their company; site managers the workers on their sites
(``apps.workers.services.scoped_workers``). Any other worker — another site, another
company — is a 404. The PIN is shown once, on the response to the POST that set it,
and never stored in clear or in the session.
"""

from django.contrib import messages
from django.core.paginator import Paginator
from django.db.models import Q
from django.http import Http404
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils.cache import add_never_cache_headers
from django.utils.translation import gettext as _
from django.views.decorators.http import require_GET, require_POST

from apps.accounts.permissions import Role, role_required
from apps.accounts.services import normalize_phone
from apps.core import dates
from apps.workers.services import scoped_workers

from . import services
from .forms import OfficePinForm
from .models import WorkerAccess

PAGE_SIZE = 50
MANAGERS = (Role.ADMIN, Role.SITE_MANAGER)
NAV = "portal:office_access"


def _workers(user):
    return scoped_workers(user, dates.company_today()).select_related("portal_access", "trade")


def _worker_or_404(user, worker_id):
    worker = _workers(user).filter(pk=worker_id).first()
    if worker is None:
        raise Http404
    return worker


def _access(worker):
    try:
        return worker.portal_access
    except WorkerAccess.DoesNotExist:
        return None


def _status(access) -> tuple[str, str]:
    """(status_badge status, label) for an access (or no access)."""
    return services.access_status(access)


@role_required(*MANAGERS)
@require_GET
def access_list(request):
    workers = _workers(request.user).filter(is_active=True)
    query = request.GET.get("q", "").strip()
    if query:
        workers = workers.filter(
            Q(first_name__icontains=query)
            | Q(last_name__icontains=query)
            | Q(number__icontains=query)
            | Q(phone__icontains=query)
        )
    show = request.GET.get("show", "all")
    if show == "with":
        workers = workers.filter(portal_access__is_enabled=True)
    elif show == "without":
        workers = workers.exclude(portal_access__is_enabled=True)
    page = Paginator(workers.order_by("last_name", "first_name", "number"), PAGE_SIZE).get_page(
        request.GET.get("page")
    )
    rows = []
    for worker in page.object_list:
        access = _access(worker)
        status, label = _status(access)
        rows.append({"worker": worker, "access": access, "status": status, "label": label})
    context = {
        "rows": rows,
        "page": page,
        "query": query,
        "show": show,
        "with_access": _workers(request.user).filter(portal_access__is_enabled=True).count(),
        "nav_active": NAV,
    }
    return render(request, "portal/office/access_list.html", context)


def _worker_page(request, worker, *, form=None, error=None, status=200):
    access = _access(worker)
    badge, label = _status(access)
    phone = normalize_phone(worker.phone)
    context = {
        "worker": worker,
        "access": access,
        "status": badge,
        "status_label": label,
        "form": form or OfficePinForm(),
        "error": error,
        "phone": phone,
        "phone_changed": bool(access and access.is_enabled and phone != access.phone),
        "phone_taken": bool(phone and services.phone_taken(phone, worker=worker)),
        "nav_active": NAV,
    }
    return render(request, "portal/office/worker.html", context, status=status)


@role_required(*MANAGERS)
@require_GET
def worker_access(request, worker_id):
    return _worker_page(request, _worker_or_404(request.user, worker_id))


@role_required(*MANAGERS)
@require_POST
def set_pin(request, worker_id):
    worker = _worker_or_404(request.user, worker_id)
    form = OfficePinForm(request.POST)
    if not form.is_valid():
        return _worker_page(request, worker, form=form, status=400)
    try:
        access, pin = services.give_access(
            worker, actor=request.user, pin=form.cleaned_data.get("pin") or ""
        )
    except services.AccessError as exc:
        return _worker_page(request, worker, form=form, error=str(exc), status=400)
    texted = services.text_pin(worker, pin, request.build_absolute_uri(reverse("portal:sign_in")))
    # Shown once: this response only, never cached, never in the session.
    response = render(
        request,
        "portal/office/pin_shown.html",
        {"worker": worker, "access": access, "pin": pin, "texted": texted, "nav_active": NAV},
    )
    add_never_cache_headers(response)
    return response


@role_required(*MANAGERS)
@require_POST
def revoke(request, worker_id):
    worker = _worker_or_404(request.user, worker_id)
    if services.revoke_access(worker, actor=request.user, reason="Turned off in the office."):
        messages.success(
            request,
            _("%(name)s can no longer sign in to the worker portal.") % {"name": worker.full_name},
        )
    return redirect("portal:office_worker", worker_id=worker.pk)
