"""Office views for attendance: the Approvals screen.

Site managers see the submitted days of their own sites; admins see every site. The
list, the detail panel and the decision form are one page; htmx swaps ``#approvals-board``
when a day is picked or a decision fails validation.
"""

from django.contrib import messages
from django.core.exceptions import PermissionDenied
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils.translation import gettext as _
from django.views.decorators.http import require_POST
from rest_framework.exceptions import APIException, ValidationError

from apps.accounts.permissions import Role, can_manage_site, role_required, scoped_sites

from . import selectors, services
from .forms import DecisionForm
from .models import AttendanceDay

MANAGERS = (Role.ADMIN, Role.SITE_MANAGER)


def _scoped_day(user, day_id) -> AttendanceDay:
    days = AttendanceDay.objects.filter(site__in=scoped_sites(user)).select_related(
        "site", "submitted_by", "foreman", "approved_by"
    )
    return get_object_or_404(days, pk=day_id)


def _render_board(request, selected_id=None, form=None, status=200):
    waiting = list(selectors.days_waiting_for_approval(request.user))
    tz = selectors.company_tz()
    selected = None
    if selected_id is not None:
        selected = _scoped_day(request.user, selected_id)
    elif waiting:
        selected = waiting[0]
    detail = None
    if selected is not None:
        detail = selectors.approval_detail(selected, tz)
        detail["can_decide"] = selected.status == AttendanceDay.Status.SUBMITTED and (
            can_manage_site(request.user, selected.site)
        )
        detail["form"] = form or DecisionForm()
    context = {
        "cards": [selectors.approval_card(day, tz) for day in waiting],
        "selected_id": selected.pk if selected else None,
        "detail": detail,
        "nav_badges": {"approvals": len(waiting) or None},
    }
    return render(request, "attendance/approvals.html", context, status=status)


@role_required(*MANAGERS)
def approvals(request):
    day_id = request.GET.get("day")
    return _render_board(request, int(day_id) if day_id and day_id.isdigit() else None)


def _done(request) -> HttpResponse:
    url = reverse("attendance:approvals")
    if request.headers.get("HX-Request"):
        response = HttpResponse(status=204)
        response["HX-Redirect"] = url
        return response
    return redirect(url)


@role_required(*MANAGERS)
@require_POST
def decide(request, day_id):
    day = _scoped_day(request.user, day_id)
    if not can_manage_site(request.user, day.site):
        raise PermissionDenied
    form = DecisionForm(request.POST)
    if not form.is_valid():
        return _render_board(request, day.pk, form=form, status=400)
    try:
        if form.cleaned_data["action"] == DecisionForm.APPROVE:
            services.approve_day(day, request.user)
            message = _("%(site)s · %(date)s approved.") % _label(day)
            messages.success(request, message)
        else:
            services.reject_day(day, request.user, form.cleaned_data["note"])
            message = _("%(site)s · %(date)s sent back to the foreman.") % _label(day)
            messages.success(request, message)
    except ValidationError as exc:
        for message in exc.detail.get("note", []):
            form.add_error("note", str(message))
        # 200 so htmx swaps the board in and shows the error under the note field.
        return _render_board(request, day.pk, form=form)
    except APIException as exc:
        messages.error(request, str(exc.detail))
    return _done(request)


def _label(day):
    return {"site": day.site.name, "date": day.date.isoformat()}
