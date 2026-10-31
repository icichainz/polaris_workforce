"""Office screens for the pay rules (Wave 4).

* **Pay rules** (admins): the company's multipliers, night window and stoppage pay, its
  statutory deduction rules and piece-work tasks, and each site's own multipliers.
* **Day extras** (a site and date; its managers and admins change it, everyone who sees
  the site reads it): piece work, bonuses and the stoppage day.

Views stay thin: the rules and the audit are in ``apps.payroll.pay_rules``.
"""

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.http import HttpResponseNotAllowed
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils.translation import gettext as _
from django.views.decorators.http import require_POST

from apps.accounts.permissions import Role, can_manage_site, get_site_or_403, role_required
from apps.api.exceptions import PolarisAPIException
from apps.core.models import CompanySettings

from . import pay_rules, services
from .forms import (
    BonusForm,
    DeductionRuleForm,
    MoneyField,
    PayRulesForm,
    PieceWorkForm,
    ReasonForm,
    SitePayRulesForm,
    StoppageForm,
    TaskTypeForm,
)
from .models import (
    Bonus,
    PayRuleSettings,
    PieceWorkEntry,
    SitePayRules,
    StoppageDay,
    StoppagePay,
)

NAV = "payroll:periods"


def _reason(request) -> str:
    form = ReasonForm(request.POST)
    return form.cleaned_data["reason"] if form.is_valid() else ""


def _errors(exc) -> list[str]:
    if isinstance(exc, ValidationError):
        return exc.messages
    return [str(exc.detail)]


def _add_errors(form, exc: ValidationError) -> None:
    for name, errors in exc.message_dict.items():
        form.add_error(name if name in form.fields else None, errors)


# --- pay rules (admins) --------------------------------------------------------------------


@role_required(Role.ADMIN)
def rules(request):
    company = CompanySettings.load()
    settings = PayRuleSettings.load()
    before = pay_rules.snapshot(settings, pay_rules.RULE_FIELDS)
    form = PayRulesForm(request.POST or None, instance=settings, company=company)
    if request.method == "POST":
        if form.is_valid():
            if pay_rules.save_rules(request.user, form.instance, before):
                messages.success(request, _("Pay rules saved."))
            else:
                messages.info(request, _("Nothing changed."))
            return redirect("payroll:pay_rules")
        messages.error(request, _("Some rules need fixing — see the highlighted fields."))
    elif request.method != "GET":
        return HttpResponseNotAllowed(["GET", "POST"])
    deductions, tasks = pay_rules.company_rules(request.user)
    sites = services.scoped_sites(request.user).order_by("name")
    overrides = {o.site_id: o for o in SitePayRules.objects.filter(site__in=sites)}
    return render(
        request,
        "payroll/pay_rules.html",
        {
            "form": form,
            "company": company,
            "deductions": deductions,
            "tasks": tasks,
            "sites": [(site, overrides.get(site.pk)) for site in sites],
            "today": services.company_today(company),
            "nav_active": NAV,
        },
    )


def _rule_form_page(request, form, *, title, back, instance_before, save, saved_message):
    if request.method == "POST":
        if form.is_valid():
            try:
                changed = save(request.user, form.instance, instance_before)
            except ValidationError as exc:
                _add_errors(form, exc)
            else:
                if changed:
                    messages.success(request, saved_message)
                else:
                    messages.info(request, _("Nothing changed."))
                return redirect(back)
    elif request.method != "GET":
        return HttpResponseNotAllowed(["GET", "POST"])
    company = CompanySettings.load()
    return render(
        request,
        "payroll/rule_form.html",
        {
            "form": form,
            "title": title,
            "back": back,
            "company": company,
            "money_fields": [n for n, f in form.fields.items() if isinstance(f, MoneyField)],
            "nav_active": NAV,
        },
    )


@role_required(Role.ADMIN)
def deduction_rule(request, pk=None):
    company = CompanySettings.load()
    rules, _tasks = pay_rules.company_rules(request.user)
    instance = get_object_or_404(rules, pk=pk) if pk else None
    before = pay_rules.snapshot(instance, pay_rules.DEDUCTION_FIELDS) if instance else None
    initial = {"active_from": services.company_today(company)} if instance is None else None
    form = DeductionRuleForm(
        request.POST or None, instance=instance, initial=initial, company=company, rules=rules
    )
    return _rule_form_page(
        request,
        form,
        title=_("Deduction rule") if instance else _("New deduction rule"),
        back=reverse("payroll:pay_rules") + "#deductions",
        instance_before=before,
        save=pay_rules.save_deduction_rule,
        saved_message=_("Deduction rule saved."),
    )


@role_required(Role.ADMIN)
def task_type(request, pk=None):
    company = CompanySettings.load()
    _rules, tasks = pay_rules.company_rules(request.user)
    instance = get_object_or_404(tasks, pk=pk) if pk else None
    before = pay_rules.snapshot(instance, pay_rules.TASK_FIELDS) if instance else None
    form = TaskTypeForm(request.POST or None, instance=instance, company=company, tasks=tasks)
    return _rule_form_page(
        request,
        form,
        title=_("Piece work task") if instance else _("New piece work task"),
        back=reverse("payroll:pay_rules") + "#tasks",
        instance_before=before,
        save=pay_rules.save_task_type,
        saved_message=_("Task saved."),
    )


@role_required(Role.ADMIN)
def site_rules(request, site_id):
    site = get_site_or_403(request.user, site_id)
    instance = SitePayRules.objects.filter(site=site).first() or SitePayRules(site=site)
    before = (
        pay_rules.snapshot(instance, SitePayRules.OVERRIDES) if instance.pk is not None else None
    )
    form = SitePayRulesForm(request.POST or None, instance=instance)
    return _rule_form_page(
        request,
        form,
        title=_("Pay rules · %(site)s") % {"site": site.name},
        back=reverse("payroll:pay_rules") + "#sites",
        instance_before=before,
        save=pay_rules.save_site_rules,
        saved_message=_("Site pay rules saved."),
    )


# --- day extras: piece work, bonuses, stoppage ---------------------------------------------


def _extras_forms(request, site, date, company, data=None, action=None):
    workers = pay_rules.crew_on(request.user, site, date)
    _rules, tasks = pay_rules.company_rules(request.user)
    rules = PayRuleSettings.load()
    stoppage = StoppageDay.objects.filter(site=site, date=date).first()
    stoppage_initial = {
        "pay": stoppage.pay if stoppage else rules.stoppage_pay,
        "percent": (stoppage.percent if stoppage else rules.stoppage_percent),
        "amount": (stoppage.amount if stoppage else rules.stoppage_amount),
        "reason": stoppage.reason if stoppage else "",
    }

    def bound(name):
        return data if action == name else None

    return {
        "piece_form": PieceWorkForm(
            bound("piece"), workers=workers, tasks=tasks.filter(is_active=True), prefix="piece"
        ),
        "bonus_form": BonusForm(bound("bonus"), workers=workers, company=company, prefix="bonus"),
        "stoppage_form": StoppageForm(
            bound("stoppage"), company=company, initial=stoppage_initial, prefix="stoppage"
        ),
    }


def _extras_url(site_id, date) -> str:
    return reverse("payroll:day_extras", args=[site_id, date.isoformat()])


@login_required
def day_extras(request, site_id, date):
    site = get_site_or_403(request.user, site_id)
    company = CompanySettings.load()
    can_edit = can_manage_site(request.user, site)
    action = request.POST.get("action") if request.method == "POST" else None
    if request.method == "POST":
        if not can_edit:
            raise PermissionDenied
        forms = _extras_forms(request, site, date, company, request.POST, action)
        done = _extras_post(request, site, date, action, forms)
        if done:
            return redirect(_extras_url(site.pk, date))
    elif request.method == "GET":
        forms = _extras_forms(request, site, date, company)
    else:
        return HttpResponseNotAllowed(["GET", "POST"])
    extras = pay_rules.day_extras(site, date)
    return render(
        request,
        "payroll/day_extras.html",
        {
            **forms,
            "extras": extras,
            "site": site,
            "date": date,
            "company": company,
            "can_edit": can_edit and extras.is_open,
            "can_manage": can_edit,
            "sheet_url": reverse("payroll:cash_sheet", args=[site.pk, date.isoformat()]),
            "nav_active": "payroll:cash_sheets",
        },
    )


def _extras_post(request, site, date, action, forms) -> bool:
    """Handle one form of the day extras page; True when done (redirect)."""
    user = request.user
    try:
        if action == "piece":
            form = forms["piece_form"]
            if not form.is_valid():
                return False
            data = form.cleaned_data
            pay_rules.add_piece_work(
                user, site, data["worker"], date, data["task"], data["quantity"], data["note"]
            )
            messages.success(request, _("Piece work recorded."))
        elif action == "bonus":
            form = forms["bonus_form"]
            if not form.is_valid():
                return False
            data = form.cleaned_data
            pay_rules.add_bonus(user, site, data["worker"], date, data["amount"], data["reason"])
            messages.success(request, _("Bonus recorded: it is added to the next pay."))
        elif action == "stoppage":
            form = forms["stoppage_form"]
            if not form.is_valid():
                return False
            data = form.cleaned_data
            pay_rules.set_stoppage(
                user,
                site,
                date,
                data["reason"],
                pay=data["pay"],
                percent=data["percent"],
                amount=data["amount"] if data["pay"] == StoppagePay.FIXED else None,
            )
            messages.success(request, _("Stoppage day saved."))
        elif action == "clear_stoppage":
            reason = _reason(request)
            if pay_rules.clear_stoppage(user, site, date, reason):
                messages.success(request, _("Stoppage day removed."))
        else:
            messages.error(request, _("Unknown action."))
    except ValidationError as exc:
        form = forms.get(f"{action}_form")
        if form is None:
            for error in _errors(exc):
                messages.error(request, error)
            return True
        _add_errors(form, exc)
        return False
    except PolarisAPIException as exc:
        for error in _errors(exc):
            messages.error(request, error)
    return True


@login_required
@require_POST
def piece_work_delete(request, pk):
    entry = get_object_or_404(
        PieceWorkEntry.objects.filter(site__in=services.scoped_sites(request.user)), pk=pk
    )
    reason = _reason(request)
    try:
        pay_rules.delete_piece_work(request.user, entry, reason)
        messages.success(request, _("Piece work removed."))
    except PolarisAPIException as exc:
        for error in _errors(exc):
            messages.error(request, error)
    return redirect(_extras_url(entry.site_id, entry.date))


@login_required
@require_POST
def bonus_delete(request, pk):
    bonus = get_object_or_404(
        Bonus.objects.filter(site__in=services.scoped_sites(request.user)), pk=pk
    )
    reason = _reason(request)
    try:
        pay_rules.delete_bonus(request.user, bonus, reason)
        messages.success(request, _("Bonus removed."))
    except PolarisAPIException as exc:
        for error in _errors(exc):
            messages.error(request, error)
    return redirect(_extras_url(bonus.site_id, bonus.date))
