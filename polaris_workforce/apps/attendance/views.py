"""Office views for attendance: the Approvals screen.

Site managers see the submitted days of their own sites; admins see every site. The
list, the detail panel and the decision form are one page; htmx swaps ``#approvals-board``
when a day is picked or a decision fails validation.
"""

from django.contrib import messages
from django.core.exceptions import PermissionDenied
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils.translation import gettext as _
from django.utils.translation import ngettext
from django.views.decorators.http import require_POST
from rest_framework.exceptions import APIException, ValidationError

from apps.accounts.permissions import Role, can_manage_site, role_required, scoped_sites
from apps.core import dates
from apps.core.htmx import wants_fragment

from . import selectors, services
from .forms import BulkApproveForm, DecisionForm
from .models import AttendanceDay

MANAGERS = (Role.ADMIN, Role.SITE_MANAGER)


def _scoped_day(user, day_id) -> AttendanceDay:
    days = AttendanceDay.objects.filter(site__in=scoped_sites(user)).select_related(
        "site", "submitted_by", "foreman", "approved_by"
    )
    return get_object_or_404(days, pk=day_id)


def _render_board(request, selected_id=None, form=None, status=200):
    waiting = list(selectors.days_waiting_for_approval(request.user))
    tz = dates.company_tz()
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
        "card_ids": [str(day.pk) for day in waiting],
        "cards": [selectors.approval_card(day, tz) for day in waiting],
        "selected_id": selected.pk if selected else None,
        "detail": detail,
    }
    return render(request, "attendance/approvals.html", context, status=status)


@role_required(*MANAGERS)
def approvals(request):
    day_id = request.GET.get("day")
    return _render_board(request, int(day_id) if day_id and day_id.isdigit() else None)


def _done(request) -> HttpResponse:
    url = reverse("attendance:approvals")
    if wants_fragment(request):
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


# --- bulk approve ----------------------------------------------------------------------


def bulk_problems(day: AttendanceDay) -> list[str]:
    """Why a day should be opened before it is approved, so a bulk approve skips it:
    the flags of the detail panel (records changed after submission, a late sync), workers
    expected but not marked, and workers already paid whose pay the day would change."""
    corrected = day.records.filter(corrected=True).count()
    problems = selectors.approval_flags(day, corrected)
    unmarked = len(selectors.unmarked_worker_ids(day))
    if unmarked:
        problems.append(
            ngettext("%(n)s worker not marked", "%(n)s workers not marked", unmarked)
            % {"n": unmarked}
        )
    paid = len(selectors.paid_notes(day))
    if paid:
        problems.append(
            ngettext(
                "%(n)s worker already paid for this day",
                "%(n)s workers already paid for this day",
                paid,
            )
            % {"n": paid}
        )
    return problems


@role_required(*MANAGERS)
@require_POST
def bulk_approve(request):
    """Approve the ticked days through ``services.approve_day``, one by one. A day with
    something to check (``bulk_problems``), no longer waiting, or refused by the service
    is skipped, and the page says which and why. An id outside the user's sites → 404."""
    days = AttendanceDay.objects.filter(site__in=scoped_sites(request.user)).select_related("site")
    form = BulkApproveForm(request.POST, days=days)
    if not form.is_valid():
        if form.out_of_scope:
            raise Http404
        for error in form.errors.get("days", []):
            messages.error(request, error)
        return _done(request)

    approved, skipped = [], []
    for day in sorted(form.cleaned_data["days"], key=lambda d: (d.date, d.site.name, d.pk)):
        if not can_manage_site(request.user, day.site):
            skipped.append((day, [_("you cannot approve days of this site")]))
            continue
        if day.status != AttendanceDay.Status.SUBMITTED:
            status = day.get_status_display()
            skipped.append((day, [_("not waiting for approval (%(status)s)") % {"status": status}]))
            continue
        problems = bulk_problems(day)
        if problems:
            skipped.append((day, problems))
            continue
        try:
            services.approve_day(day, request.user)
        except (APIException, PermissionDenied) as exc:
            detail = getattr(exc, "detail", None) or _("refused")
            skipped.append((day, [str(detail)]))
        else:
            approved.append(day)

    if approved:
        messages.success(
            request,
            ngettext("%(n)s day approved.", "%(n)s days approved.", len(approved))
            % {"n": len(approved)},
        )
    for day, reasons in skipped:
        messages.warning(
            request,
            _("%(site)s · %(date)s not approved: %(reasons)s. Open it to check.")
            % {**_label(day), "reasons": "; ".join(reasons)},
        )
    return _done(request)
