"""Office views for workers: list, profile, add / edit.

Scoping: ``services.scoped_workers`` (built on ``accounts.permissions.scoped_sites``).
"""

import calendar
import datetime

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.paginator import Paginator
from django.db.models import Q, Value
from django.db.models.functions import Replace
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils.translation import gettext as _

from apps.accounts.permissions import Role, has_role, scoped_sites
from apps.attendance.models import AttendanceRecord
from apps.core import dates, files
from apps.core.choices import LateRule, PayType
from apps.core.models import CompanySettings
from apps.core.settings import resolve_setting
from apps.worksites import services as site_services
from apps.worksites.models import SiteAssignment

from . import display, services
from .forms import WorkerFilterForm, WorkerForm
from .models import Worker

PAGE_SIZE = 25


def _get_worker_or_403(user, pk, today) -> Worker:
    worker = services.scoped_workers(user, today).select_related("trade").filter(pk=pk).first()
    if worker is None:
        raise PermissionDenied
    return worker


# --- list -------------------------------------------------------------------------------


def _filter_workers(workers, data, today):
    status = data.get("status") or "active"
    if status == "active":
        workers = workers.filter(is_active=True)
    elif status == "inactive":
        workers = workers.filter(is_active=False)
    if data.get("trade"):
        workers = workers.filter(trade=data["trade"])
    if data.get("site"):
        on_site = SiteAssignment.objects.filter(site=data["site"]).filter(
            Q(end_date__isnull=True) | Q(end_date__gte=today)
        )
        workers = workers.filter(pk__in=on_site.values("worker"))
    query = data.get("q") or ""
    if query:
        workers = workers.annotate(phone_digits=Replace("phone", Value(" "), Value("")))
        for term in query.split():
            workers = workers.filter(
                Q(first_name__icontains=term)
                | Q(last_name__icontains=term)
                | Q(number__icontains=term)
                | Q(phone__icontains=term)
                | Q(phone_digits__icontains=term)
            )
    return workers


def _site_names(workers, today) -> dict[int, str]:
    """The site each worker is on today, else the one they start on next."""
    found: dict[int, tuple] = {}
    assignments = (
        SiteAssignment.objects.filter(worker__in=workers)
        .filter(Q(end_date__isnull=True) | Q(end_date__gte=today))
        .select_related("site")
        .order_by("start_date")
    )
    for a in assignments:
        rank = 0 if a.start_date <= today else 1
        best = found.get(a.worker_id)
        if best is None or rank < best[0] or (rank == 0 and a.start_date > best[1]):
            found[a.worker_id] = (rank, a.start_date, a.site.name)
    return {worker_id: value[2] for worker_id, value in found.items()}


@login_required
def worker_list(request):
    company = CompanySettings.load()
    today = dates.company_today(company)
    money = display.Money(company)
    visible = services.scoped_workers(request.user, today)
    form = WorkerFilterForm(request.GET or None, sites=scoped_sites(request.user))
    data = form.cleaned_data if form.is_bound and form.is_valid() else {}

    workers = (
        _filter_workers(visible, data, today)
        .select_related("trade")
        .order_by("last_name", "first_name", "number")
    )
    page = Paginator(workers, PAGE_SIZE).get_page(request.GET.get("page"))
    on_page = list(page.object_list)
    rates = services.rates_on(on_page, today)
    statuses = services.statuses_on(on_page, today)
    owed = services.advances_owed(on_page)
    sites = _site_names(on_page, today)

    rows = []
    for worker in on_page:
        rate = rates.get(worker.pk)
        rows.append(
            {
                "worker": worker,
                "initials": display.initials(worker),
                "site": sites.get(worker.pk, "—"),
                "rate": money(rate.amount, currency=False) if rate else "—",
                "unit": display.pay_unit(rate.pay_type) if rate else "",
                "status": statuses.get(worker.pk, "not_recorded"),
                "owes": money(owed[worker.pk], currency=False) if owed.get(worker.pk) else None,
            }
        )

    return render(
        request,
        "workers/list.html",
        {
            "form": form,
            "page": page,
            "rows": rows,
            "active_count": visible.filter(is_active=True).count(),
            "inactive_count": visible.filter(is_active=False).count(),
            "currency": company.currency,
            "can_create": services.can_edit_worker(request.user, None, today),
        },
    )


# --- profile ----------------------------------------------------------------------------


def _month_from(value, today) -> datetime.date:
    try:
        year, month = (int(part) for part in value.split("-"))
        return datetime.date(year, month, 1)
    except (AttributeError, ValueError):
        return today.replace(day=1)


def _shift_month(first: datetime.date, months: int) -> datetime.date:
    index = first.year * 12 + first.month - 1 + months
    return datetime.date(index // 12, index % 12 + 1, 1)


def _calendar(first, records, today, working_days):
    by_date = {r.day.date: r for r in records}
    weeks = []
    for week in calendar.Calendar(firstweekday=0).monthdatescalendar(first.year, first.month):
        cells = []
        for date in week:
            if date.month != first.month:
                cells.append(None)
                continue
            record = by_date.get(date)
            if record is not None:
                kind = record.status
                title = record.get_status_display()
            elif date.isoweekday() not in working_days:
                kind, title = "off", _("Day off")
            elif date > today:
                kind, title = "future", ""
            else:
                kind, title = "none", _("Not recorded")
            cells.append({"date": date, "kind": kind, "title": title, "is_today": date == today})
        weeks.append(cells)
    return weeks


def _setting_source(worker, site, name) -> str:
    if getattr(worker, name):
        return _("worker")
    if site is not None and getattr(site, name):
        return _("site")
    return _("default")


@login_required
def worker_detail(request, pk):
    company = CompanySettings.load()
    today = dates.company_today(company)
    tz = dates.company_tz(company)
    money = display.Money(company)
    worker = _get_worker_or_403(request.user, pk, today)

    first = _month_from(request.GET.get("month"), today)
    last = _shift_month(first, 1) - datetime.timedelta(days=1)
    records = list(
        AttendanceRecord.objects.filter(worker=worker, day__date__range=(first, last))
        .select_related("day")
        .order_by("day__date")
    )
    working_days = set(company.working_days or [])
    summary = services.month_summary(records, company.half_day_units)
    expected = services.working_days_between(first, min(last, today), working_days)

    assignment = site_services.current_or_next_assignment(worker, today)
    site = assignment.site if assignment else None
    rate = worker.rate_on(today)
    history = services.rate_history(worker)
    previous = next(
        (h["rate"] for h in history if rate and h["rate"].effective_from < rate.effective_from),
        None,
    )

    month_end = _shift_month(first, 1)
    paid = services.payments_between(
        worker,
        datetime.datetime.combine(first, datetime.time.min, tzinfo=tz),
        datetime.datetime.combine(month_end, datetime.time.min, tzinfo=tz),
        user=request.user,
    )
    advances = services.advance_summary(worker)

    # Only payments made on sites the user may see: a worker who moved between sites keeps
    # the old site's payments, and only that site's people may see their amounts, proofs
    # and payslips.
    payments = []
    recent = list(
        services.visible_payments(request.user, worker)
        .select_related("pay_line__period")
        .order_by("-paid_at")[:10]
    )
    for payment in recent:
        line = payment.pay_line
        paid_at = payment.paid_at.astimezone(tz)
        payments.append(
            {
                "date": paid_at.date(),
                "time": paid_at.strftime("%H:%M"),
                "units": display.decimal_text(line.units),
                "gross": money(line.gross, currency=False),
                "advance": money(line.advance_deduction, currency=False)
                if line.advance_deduction
                else None,
                "net": money(payment.amount, currency=False),
                "proof": payment.get_proof_type_display(),
                "proof_url": files.payment_proof_url(payment),
                "payslip_url": reverse("payroll:payslip", args=[line.pk]),
            }
        )

    method = resolve_setting("payment_method", site=site, worker=worker, company=company)
    frequency = resolve_setting("pay_frequency", site=site, worker=worker, company=company)
    every = resolve_setting("pay_every_n_days", site=site, worker=worker, company=company)
    counts = summary["counts"]

    context = {
        "worker": worker,
        "initials": display.initials(worker),
        "assignment": assignment,
        "site": site,
        "nav_active": "workers:list",
        "can_edit": services.can_edit_worker(request.user, worker, today),
        "can_give_advance": has_role(request.user, Role.ADMIN, Role.SITE_MANAGER),
        "rate": rate,
        "rate_text": money(rate.amount) if rate else "—",
        "previous_rate": money(previous.amount, currency=False) if previous else None,
        "month": first,
        "prev_month": _shift_month(first, -1),
        "next_month": _shift_month(first, 1) if _shift_month(first, 1) <= today else None,
        "weeks": _calendar(first, records, today, working_days),
        "weekday_names": [_("Mon"), _("Tue"), _("Wed"), _("Thu"), _("Fri"), _("Sat"), _("Sun")],
        "units": display.decimal_text(summary["units"]),
        "expected": expected,
        "expected_suffix": f"/ {expected}",
        "today_date": today,
        "attendance_percent": round(100 * summary["units"] / expected) if expected else None,
        "late_full_day": company.late_rule == LateRule.FULL_DAY_FLAGGED,
        "count_present": counts[AttendanceRecord.Status.PRESENT],
        "count_half": counts[AttendanceRecord.Status.HALF_DAY],
        "count_late": counts[AttendanceRecord.Status.LATE],
        "count_absent": counts[AttendanceRecord.Status.ABSENT],
        "days_off": [
            str(name)
            for day, name in enumerate(
                [
                    _("Mondays"),
                    _("Tuesdays"),
                    _("Wednesdays"),
                    _("Thursdays"),
                    _("Fridays"),
                    _("Saturdays"),
                    _("Sundays"),
                ],
                start=1,
            )
            if day not in working_days
        ],
        "paid_total": money(paid["total"]),
        "paid_count": paid["count"],
        "all_with_receipt": paid["all_with_proof"],
        "owed": money(advances["owed"]),
        "deduction": money(advances["deduction"], currency=False) if advances["owed"] else None,
        "advances_url": f"{reverse('payroll:advances')}?worker={worker.pk}",
        "history": [
            {
                "from": h["rate"].effective_from,
                "until": h["until"],
                "amount": money(h["rate"].amount, currency=False),
                "unit": display.pay_unit(h["rate"].pay_type),
            }
            for h in history
        ],
        "payments": payments,
        "payment_method": display.payment_method_text(method),
        "payment_method_source": _setting_source(worker, site, "payment_method"),
        "pay_frequency": display.pay_frequency_text(frequency, every),
        "pay_frequency_source": _setting_source(worker, site, "pay_frequency"),
        "pay_type": PayType(rate.pay_type).label if rate else None,
    }
    return render(request, "workers/detail.html", context)


# --- add / edit -------------------------------------------------------------------------


_SERVICE_FIELDS = {"start": "start_date", "end": "start_date", "amount": "rate"}


def _add_service_errors(form, exc: ValidationError):
    if hasattr(exc, "error_dict"):
        for key, errors in exc.message_dict.items():
            field = _SERVICE_FIELDS.get(key, key)
            form.add_error(field if field in form.fields else None, errors)
    else:
        form.add_error(None, exc.messages)


def _inherited(sites, company) -> dict:
    """What each site gives a worker who doesn't override (for the form's live hints)."""

    def entry(site):
        method = resolve_setting("payment_method", site=site, company=company)
        frequency = resolve_setting("pay_frequency", site=site, company=company)
        every = resolve_setting("pay_every_n_days", site=site, company=company)
        return {
            "payment_method": display.payment_method_text(method),
            "payment_method_source": _("From site · %(site)s") % {"site": site.name}
            if site.payment_method
            else _("Company default"),
            "pay_frequency": display.pay_frequency_text(frequency, every),
            "pay_frequency_source": _("From site · %(site)s") % {"site": site.name}
            if site.pay_frequency
            else _("Company default"),
        }

    data = {str(site.pk): entry(site) for site in sites}
    data[""] = {
        "payment_method": display.payment_method_text(company.payment_method),
        "payment_method_source": _("Company default"),
        "pay_frequency": display.pay_frequency_text(
            company.pay_frequency, company.pay_every_n_days
        ),
        "pay_frequency_source": _("Company default"),
    }
    return data


def _form_fields(form) -> dict:
    data = form.cleaned_data
    fields = {
        "first_name": data["first_name"],
        "last_name": data["last_name"],
        "phone": data["phone"],
        "trade": form.trade_value(),
        "id_document_type": data["id_document_type"],
        "id_document_number": data["id_document_number"],
        "payment_method": data["payment_method"],
        "pay_frequency": data["pay_frequency"],
    }
    if "is_active" in data:
        fields["is_active"] = data["is_active"]
    photo = data.get("photo")
    if photo:
        fields["photo"] = photo
    elif photo is False:
        fields["photo"] = None
    return fields


def _worker_form_view(request, worker):
    company = CompanySettings.load()
    today = dates.company_today(company)
    if not services.can_edit_worker(request.user, worker, today):
        raise PermissionDenied
    sites = services.assignable_sites(request.user).order_by("name")
    form = WorkerForm(
        request.POST or None,
        request.FILES or None,
        # A copy: validation fills the instance, the page header keeps the saved values.
        instance=Worker.objects.get(pk=worker.pk) if worker else Worker(),
        sites=sites,
        decimals=company.currency_decimals,
        today=today,
    )
    if request.method == "POST" and form.is_valid():
        data = form.cleaned_data
        pay = {
            "pay_type": data["pay_type"],
            "amount": data["rate"],
            "effective_from": data["effective_from"],
        }
        try:
            if worker is None:
                saved = services.create_worker(
                    actor=request.user,
                    site=data["site"],
                    start_date=data["start_date"],
                    **pay,
                    **_form_fields(form),
                )
                messages.success(request, _("Worker %(number)s added.") % {"number": saved.number})
            else:
                saved = services.update_worker(
                    Worker.objects.get(pk=worker.pk),
                    _form_fields(form),
                    actor=request.user,
                    site=data["site"],
                    start_date=data["start_date"],
                    **pay,
                )
                messages.success(request, _("Worker saved."))
        except ValidationError as exc:
            _add_service_errors(form, exc)
        else:
            return redirect("workers:detail", pk=saved.pk)

    current = None
    if worker is not None:
        assignment = site_services.current_or_next_assignment(worker, today)
        current = assignment.site if assignment else None
    inherited = _inherited(sites, company)
    selected = form["site"].value() or (current.pk if current else "")
    hint = inherited.get(str(selected), inherited[""])
    return render(
        request,
        "workers/form.html",
        {
            "form": form,
            "worker": worker,
            "current_site": current,
            "currency": company.currency,
            "inherited": inherited,
            "hint": hint,
            "nav_active": "workers:list",
        },
    )


@login_required
def worker_create(request):
    return _worker_form_view(request, None)


@login_required
def worker_edit(request, pk):
    company = CompanySettings.load()
    today = dates.company_today(company)
    return _worker_form_view(request, _get_worker_or_403(request.user, pk, today))
