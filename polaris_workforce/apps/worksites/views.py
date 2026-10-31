"""Office views for sites: list, new / edit, detail tabs, site settings, assigning workers.

Every view goes through ``apps.accounts.permissions`` for site scoping. Rules live in
``services.py``; views only gather what the templates show.
"""

import datetime

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.db.models import Count, Q, Sum
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils.translation import gettext as _
from django.views.decorators.http import require_POST

from apps.accounts.permissions import (
    Role,
    can_manage_site,
    company_workers,
    get_site_or_403,
    is_admin,
    role_required,
    scoped_sites,
)
from apps.attendance.models import AttendanceDay, AttendanceRecord
from apps.core import dates
from apps.core.models import CompanySettings
from apps.core.money import format_money
from apps.payroll.models import CashIssue, PayLine, PayPeriod
from apps.workers import display
from apps.workers import services as worker_services

from . import services
from .forms import AssignWorkersForm, SiteForm, SiteSettingsForm
from .models import Site, SiteAssignment

CREW_PREVIEW = 10


# --- list -------------------------------------------------------------------------------


@login_required
def site_list(request):
    company = CompanySettings.load()
    today = dates.company_today(company)
    sites = scoped_sites(request.user)
    counts = services.status_counts(sites)

    status = request.GET.get("status") or Site.Status.ACTIVE
    if status not in Site.Status.values:
        status = Site.Status.ACTIVE
    query = request.GET.get("q", "").strip()

    shown = sites.filter(status=status)
    if query:
        shown = shown.filter(Q(name__icontains=query) | Q(address__icontains=query))
    shown = (
        services.annotate_today(shown, today)
        .select_related("manager")
        .prefetch_related("foremen")
        .order_by("name")
    )

    cards = []
    for site in shown:
        frequency = services.site_setting(site, "pay_frequency", company)
        every = services.site_setting(site, "pay_every_n_days", company)
        assigned = site.assigned_count
        cards.append(
            {
                "site": site,
                "foremen": ", ".join(str(f) for f in site.foremen.all()) or "—",
                "assigned": assigned,
                "present": site.present_count,
                "percent": round(100 * site.present_count / assigned) if assigned else 0,
                "pay": display.pay_frequency_text(frequency.value, every.value),
                "pay_overridden": frequency.overridden,
            }
        )

    tabs = [
        {"status": value, "label": label, "count": counts[value], "current": value == status}
        for value, label in Site.Status.choices
    ]
    return render(
        request,
        "worksites/list.html",
        {
            "cards": cards,
            "tabs": tabs,
            "status": status,
            "query": query,
            "counts": counts,
            "can_create": is_admin(request.user),
        },
    )


# --- new / edit -------------------------------------------------------------------------


def _site_form_view(request, site):
    company = CompanySettings.load()
    form = SiteForm(request.POST or None, instance=site, decimals=company.currency_decimals)
    if request.method == "POST" and form.is_valid():
        site = form.save(commit=False)
        site.labour_budget = form.cleaned_data["labour_budget"]
        services.save_site(site, actor=request.user, foremen=form.cleaned_data["foremen"])
        messages.success(request, _("Site saved."))
        return redirect("worksites:detail", pk=site.pk)
    return render(
        request,
        "worksites/form.html",
        {
            "form": form,
            "site": site if site.pk else None,
            "company_currency": company.currency,
            "nav_active": "worksites:list",
        },
    )


@role_required(Role.ADMIN)
def site_create(request):
    return _site_form_view(request, Site())


@role_required(Role.ADMIN)
def site_edit(request, pk):
    return _site_form_view(request, get_object_or_404(scoped_sites(request.user), pk=pk))


# --- detail -----------------------------------------------------------------------------


def _detail_context(request, site, tab, company, today):
    return {
        "site": site,
        "tab": tab,
        "today": today,
        "crew_count": services.crew_for(site, today).count(),
        "can_manage": can_manage_site(request.user, site),
        "is_admin": is_admin(request.user),
        "nav_active": "worksites:list",
        "company": company,
    }


def _day_counts(day) -> dict:
    counts = dict.fromkeys(AttendanceRecord.Status.values, 0)
    if day is not None:
        for row in day.records.values("status").annotate(n=Count("pk")):
            counts[row["status"]] = row["n"]
    return counts


def _crew_rows(site, date, company, limit=None):
    money = display.Money(company)
    crew = list(services.crew_for(site, date)[:limit] if limit else services.crew_for(site, date))
    rates = worker_services.rates_on(crew, date)
    records = {
        r.worker_id: r
        for r in AttendanceRecord.objects.filter(day__site=site, day__date=date, worker__in=crew)
    }
    rows = []
    for worker in crew:
        rate = rates.get(worker.pk)
        record = records.get(worker.pk)
        rows.append(
            {
                "worker": worker,
                "initials": display.initials(worker),
                "record": record,
                "status": record.status if record else "not_recorded",
                "arrival": display.time_text(record.arrival if record else None),
                "departure": display.time_text(record.departure if record else None),
                "rate": money(rate.amount) if rate else "—",
                "unit": display.pay_unit(rate.pay_type) if rate else "",
            }
        )
    return rows


def _pay_settings(site, company):
    setting = services.site_setting

    def source(*names):
        overridden = any(setting(site, n, company).overridden for n in names)
        return _("Site override") if overridden else _("Company default")

    frequency = setting(site, "pay_frequency", company).value
    every = setting(site, "pay_every_n_days", company).value
    method = setting(site, "payment_method", company).value
    start = setting(site, "work_start", company).value
    end = setting(site, "work_end", company).value
    after = setting(site, "overtime_after_hours", company).value
    multiplier = setting(site, "overtime_multiplier", company).value
    return [
        {
            "label": _("Pay frequency"),
            "value": display.pay_frequency_text(frequency, every),
            "source": source("pay_frequency", "pay_every_n_days"),
        },
        {
            "label": _("Payment method"),
            "value": display.payment_method_text(method),
            "source": source("payment_method"),
        },
        {
            "label": _("Work hours"),
            "value": f"{display.time_text(start)} – {display.time_text(end)}",
            "source": source("work_start", "work_end"),
        },
        {
            "label": _("Overtime"),
            "value": _("After %(hours)s h × %(multiplier)s")
            % {
                "hours": display.decimal_text(after),
                "multiplier": display.decimal_text(multiplier),
            },
            "source": source("overtime_after_hours", "overtime_multiplier"),
        },
    ]


@login_required
def site_detail(request, pk):
    site = get_site_or_403(request.user, pk)
    company = CompanySettings.load()
    today = dates.company_today(company)
    money = display.Money(company)

    week = services.week_strip(site, today, company)
    day = AttendanceDay.objects.filter(site=site, date=today).select_related("foreman").first()
    counts = _day_counts(day)
    submitted_at = (
        day.submitted_at.astimezone(dates.company_tz(company)).strftime("%H:%M")
        if day and day.submitted_at
        else None
    )
    foremen = list(site.foremen.order_by("first_name", "last_name"))

    month_start = today.replace(day=1)
    labour_cost = (
        PayLine.objects.filter(
            period__site=site, period__start_date__gte=month_start, period__start_date__lte=today
        ).aggregate(total=Sum("gross"))["total"]
        or 0
    )

    context = _detail_context(request, site, "overview", company, today)
    context.update(
        {
            "week": week,
            "day": day,
            "counts": counts,
            "submitted_at": submitted_at,
            "crew_rows": _crew_rows(site, today, company, limit=CREW_PREVIEW),
            "foremen": foremen,
            "pay_settings": _pay_settings(site, company),
            "labour_cost": money(labour_cost, currency=False),
            "currency": company.currency,
            "budget": money(site.labour_budget) if site.labour_budget is not None else None,
        }
    )
    return render(request, "worksites/detail.html", context)


def _parse_date(value, default):
    try:
        return datetime.date.fromisoformat(value) if value else default
    except ValueError:
        return default


@login_required
def site_crew(request, pk):
    site = get_site_or_403(request.user, pk)
    company = CompanySettings.load()
    today = dates.company_today(company)
    date = _parse_date(request.GET.get("date"), today)
    rows = _crew_rows(site, date, company)
    assignments = {
        a.worker_id: a
        for a in SiteAssignment.objects.active_on(date).filter(
            site=site, worker__in=[r["worker"] for r in rows]
        )
    }
    for row in rows:
        row["assignment"] = assignments.get(row["worker"].pk)
    context = _detail_context(request, site, "crew", company, today)
    context.update({"rows": rows, "date": date})
    return render(request, "worksites/crew.html", context)


@login_required
def site_attendance(request, pk):
    site = get_site_or_403(request.user, pk)
    company = CompanySettings.load()
    today = dates.company_today(company)
    days = (
        AttendanceDay.objects.filter(site=site)
        .select_related("foreman")
        .annotate(
            present=Count("records", filter=Q(records__status__in=services.PRESENT_STATUSES)),
            absent=Count("records", filter=Q(records__status=AttendanceRecord.Status.ABSENT)),
            total=Count("records"),
        )
        .order_by("-date")[:60]
    )
    context = _detail_context(request, site, "attendance", company, today)
    context["days"] = days
    return render(request, "worksites/attendance.html", context)


@login_required
def site_pay(request, pk):
    site = get_site_or_403(request.user, pk)
    company = CompanySettings.load()
    today = dates.company_today(company)
    money = display.Money(company)
    periods = (
        PayPeriod.objects.filter(site=site)
        .annotate(net=Sum("lines__net"), workers=Count("lines"))
        .order_by("-start_date")[:30]
    )
    period_rows = [
        {
            "period": p,
            "net": format_period_money(p, p.net or 0),
            "workers": p.workers,
        }
        for p in periods
    ]
    issues = CashIssue.objects.filter(site=site).select_related("received_by")[:30]
    issue_rows = [{"issue": i, "amount": money(i.amount)} for i in issues]
    context = _detail_context(request, site, "pay", company, today)
    context.update({"periods": period_rows, "issues": issue_rows})
    return render(request, "worksites/pay.html", context)


def format_period_money(period, amount):
    """Periods keep their own currency snapshot."""
    return format_money(int(amount), period.currency, period.currency_decimals)


@login_required
def site_settings(request, pk):
    site = get_site_or_403(request.user, pk)
    company = CompanySettings.load()
    today = dates.company_today(company)
    can_manage = can_manage_site(request.user, site)
    if request.method == "POST" and not can_manage:
        raise PermissionDenied
    # The form edits its own copy, so the page keeps showing the saved values on errors.
    form = SiteSettingsForm(
        request.POST or None, instance=Site.objects.get(pk=site.pk), company=company
    )
    if not can_manage:
        for field in form.fields.values():
            field.disabled = True
    if request.method == "POST" and form.is_valid():
        try:
            services.update_site_settings(
                Site.objects.get(pk=site.pk), form.changes(), actor=request.user
            )
        except ValidationError as exc:
            form.add_error(None, exc)
        else:
            messages.success(request, _("Site settings saved."))
            return redirect("worksites:settings", pk=site.pk)
    context = _detail_context(request, site, "settings", company, today)
    context.update({"form": form, "pay_settings": _pay_settings(site, company)})
    return render(request, "worksites/settings.html", context)


# --- crew changes -----------------------------------------------------------------------


@login_required
def site_assign(request, pk):
    site = get_site_or_403(request.user, pk)
    if not can_manage_site(request.user, site):
        raise PermissionDenied
    company = CompanySettings.load()
    today = dates.company_today(company)
    on_site = services.crew_for(site, today).values("pk")
    candidates = (
        company_workers(request.user)
        .filter(is_active=True)
        .exclude(pk__in=on_site)
        .select_related("trade")
        .order_by("last_name", "first_name")
    )
    if not is_admin(request.user):
        # Site managers move their own workers and anyone not on a site yet.
        visible = worker_services.scoped_workers(request.user, today).values("pk")
        unassigned = SiteAssignment.objects.filter(
            Q(end_date__isnull=True) | Q(end_date__gte=today)
        ).values("worker")
        candidates = candidates.filter(Q(pk__in=visible) | ~Q(pk__in=unassigned))
    form = AssignWorkersForm(request.POST or None, workers=candidates, today=today)
    if request.method == "POST" and form.is_valid():
        start = form.cleaned_data["start_date"]
        try:
            for worker in form.cleaned_data["workers"]:
                services.assign(worker, site, start, actor=request.user)
        except ValidationError as exc:
            form.add_error(None, exc.messages)
        else:
            messages.success(request, _("Crew updated."))
            return redirect("worksites:crew", pk=site.pk)
    context = _detail_context(request, site, "crew", company, today)
    context["form"] = form
    return render(request, "worksites/assign.html", context)


@require_POST
@login_required
def end_assignment(request, pk, assignment_id):
    site = get_site_or_403(request.user, pk)
    if not can_manage_site(request.user, site):
        raise PermissionDenied
    assignment = get_object_or_404(SiteAssignment, pk=assignment_id, site=site)
    company = CompanySettings.load()
    today = dates.company_today(company)
    end = _parse_date(request.POST.get("end_date"), today)
    try:
        services.end_assignment(assignment, max(end, assignment.start_date), actor=request.user)
    except ValidationError as exc:
        messages.error(request, " ".join(exc.messages))
    else:
        messages.success(
            request,
            _("%(name)s leaves the crew after %(date)s.")
            % {"name": assignment.worker.full_name, "date": assignment.end_date.isoformat()},
        )
    return redirect(reverse("worksites:crew", kwargs={"pk": site.pk}))
