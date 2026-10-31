"""Office screens for bank-transfer and mobile-money payouts.

Views stay thin: rules and scoping come from ``services``. A batch, layout or worker
outside the user's company or sites answers 404 (like an id that does not exist); an
action the user may not take answers 403.
"""

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.paginator import Paginator
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils.http import url_has_allowed_host_and_scheme
from django.utils.translation import gettext as _
from django.views.decorators.http import require_GET, require_POST

from apps.api.exceptions import PolarisAPIException
from apps.core import dates
from apps.core.models import CompanySettings

from . import layouts, services
from .confirmations import MAX_BYTES
from .forms import (
    ConfirmLineForm,
    CreateBatchForm,
    ExportForm,
    ExportLayoutForm,
    ImportForm,
    PayoutAccountForm,
    PrepareForm,
    ReasonForm,
)
from .models import ExportLayout, PayoutAccount, PayoutBatch, PayoutLine, TransferMethod

PAGE_SIZE = 50
NAV = "payouts:batches"


def _errors(exc) -> list[str]:
    if isinstance(exc, ValidationError):
        return exc.messages
    return [str(exc.detail)]


def _flash(request, exc) -> None:
    for error in _errors(exc):
        messages.error(request, error)


def _require_view(user) -> None:
    if not services.can_view_payouts(user):
        raise PermissionDenied


def _batch_or_404(user, batch_id) -> PayoutBatch:
    batch = services.batches_for(user).select_related("site", "layout").filter(pk=batch_id)
    batch = batch.first()
    if batch is None:
        raise Http404
    return batch


def _line_or_404(user, line_id) -> PayoutLine:
    line = (
        PayoutLine.objects.filter(batch__in=services.batches_for(user), pk=line_id)
        .select_related("batch__site")
        .first()
    )
    if line is None:
        raise Http404
    return line


def _batch_url(batch) -> str:
    return reverse("payouts:batch", args=[batch.pk])


# --- batches -----------------------------------------------------------------------------


def _prepare_form(request, data=None, form_class=PrepareForm, **kwargs):
    everywhere = services.pays_out_everywhere(request.user)
    sites = services.payout_sites(request.user).order_by("name")
    return form_class(data, sites=sites, all_sites_allowed=everywhere, **kwargs)


@login_required
@require_GET
def batches(request):
    _require_view(request.user)
    company = CompanySettings.load()
    today = dates.company_today(company)
    rows = services.batches_for(request.user).select_related("site", "layout")
    status = request.GET.get("status", "open")
    if status == "open":
        rows = rows.exclude(status__in=[PayoutBatch.Status.CLOSED, PayoutBatch.Status.CANCELLED])
    page = Paginator(rows.order_by("-pay_date", "-number"), PAGE_SIZE).get_page(
        request.GET.get("page")
    )
    for batch in page:
        batch.totals = services.batch_totals(batch)
    can_prepare = services.payout_sites(request.user).exists()
    form = (
        _prepare_form(request, initial={"pay_date": today, "method": TransferMethod.MOBILE_MONEY})
        if can_prepare
        else None
    )
    return render(
        request,
        "payouts/batches.html",
        {
            "page": page,
            "status": status,
            "form": form,
            "company": company,
            "can_manage_layouts": services.can_manage_layouts(request.user),
        },
    )


@login_required
def prepare(request):
    """GET: who would be in the batch (and who lacks an account). POST: create it."""
    _require_view(request.user)
    if request.method not in ("GET", "POST"):
        return HttpResponse(status=405)
    company = CompanySettings.load()
    data = request.POST if request.method == "POST" else (request.GET or None)
    form = _prepare_form(
        request,
        data,
        form_class=CreateBatchForm,
        layouts_qs=services.layouts_for(request.user, active_only=True),
    )
    if not form.is_valid():
        if request.method == "POST":
            for errors in form.errors.values():
                for error in errors:
                    messages.error(request, error)
            return redirect("payouts:batches")
        return render(
            request,
            "payouts/prepare.html",
            {"form": form, "company": company, "found": None, "nav_active": NAV},
        )
    data = form.cleaned_data
    if request.method == "POST":
        try:
            batch = services.create_batch(
                request.user, data["pay_date"], data["method"], data["site"], data["layout"]
            )
        except (ValidationError, PolarisAPIException) as exc:
            _flash(request, exc)
            return redirect(
                reverse("payouts:prepare")
                + f"?pay_date={data['pay_date'].isoformat()}&method={data['method']}"
                + (f"&site={data['site'].pk}" if data["site"] else "")
            )
        messages.success(request, _("Batch %(ref)s prepared.") % {"ref": batch.reference})
        return redirect(_batch_url(batch))
    found = services.candidates(request.user, data["pay_date"], data["method"], data["site"])
    ready = [c for c in found if c.ready]
    return render(
        request,
        "payouts/prepare.html",
        {
            "form": form,
            "company": company,
            "found": found,
            "ready": ready,
            "missing": [c for c in found if not c.ready],
            "total": sum(c.remaining for c in ready),
            "layouts": services.layouts_for(request.user, data["method"], active_only=True),
            "default_layout": services.default_layout(company.company_id, data["method"]),
            "nav_active": NAV,
        },
    )


@login_required
@require_GET
def batch(request, batch_id):
    _require_view(request.user)
    batch = _batch_or_404(request.user, batch_id)
    lines = batch.lines.select_related(
        "pay_line__worker", "pay_line__period__site", "payment", "resolved_by"
    ).order_by("reference")
    can_act = services.can_act(request.user, batch)
    return render(
        request,
        "payouts/batch.html",
        {
            "batch": batch,
            "lines": lines,
            "totals": services.batch_totals(batch),
            "stale": services.stale_lines(batch),
            "can_act": can_act,
            "export_form": ExportForm(
                layouts_qs=services.layouts_for(request.user, batch.method, active_only=True),
                initial={"layout": batch.layout_id},
            ),
            "import_form": ImportForm(),
            "nav_active": NAV,
            "statuses": PayoutBatch.Status,
            "line_statuses": PayoutLine.Status,
        },
    )


@login_required
@require_POST
def batch_export(request, batch_id):
    batch = _batch_or_404(request.user, batch_id)
    if not services.can_act(request.user, batch):
        raise PermissionDenied
    form = ExportForm(
        request.POST,
        layouts_qs=services.layouts_for(request.user, batch.method, active_only=True),
    )
    layout = form.cleaned_data["layout"] if form.is_valid() else None
    try:
        exported = services.export_batch(request.user, batch, layout)
    except (ValidationError, PolarisAPIException) as exc:
        _flash(request, exc)
        return redirect(_batch_url(batch))
    response = HttpResponse(exported.content, content_type=exported.content_type)
    response["Content-Disposition"] = f'attachment; filename="{exported.filename}"'
    response["Cache-Control"] = "private, no-store"
    response["X-Content-Type-Options"] = "nosniff"
    return response


@login_required
@require_POST
def batch_import(request, batch_id):
    batch = _batch_or_404(request.user, batch_id)
    if not services.can_act(request.user, batch):
        raise PermissionDenied
    form = ImportForm(request.POST, request.FILES)
    if not form.is_valid():
        for errors in form.errors.values():
            for error in errors:
                messages.error(request, error)
        return redirect(_batch_url(batch))
    upload = form.cleaned_data["file"]
    try:
        result = services.import_confirmations(
            request.user,
            batch,
            upload.name,
            upload.read(MAX_BYTES + 1),
            check_only=form.cleaned_data["check_only"],
        )
    except (ValidationError, PolarisAPIException) as exc:
        _flash(request, exc)
        return redirect(_batch_url(batch))
    batch = _batch_or_404(request.user, batch.pk)
    return render(
        request,
        "payouts/import_result.html",
        {"batch": batch, "result": result, "filename": upload.name, "nav_active": NAV},
    )


def _batch_action(request, batch_id, action, success):
    batch = _batch_or_404(request.user, batch_id)
    if not services.can_act(request.user, batch):
        raise PermissionDenied
    form = ReasonForm(request.POST)
    reason = form.cleaned_data["reason"] if form.is_valid() else ""
    try:
        action(request.user, batch, reason)
        messages.success(request, success)
    except (ValidationError, PolarisAPIException) as exc:
        _flash(request, exc)
    return redirect(_batch_url(batch))


@login_required
@require_POST
def batch_close(request, batch_id):
    return _batch_action(request, batch_id, services.close_batch, _("Batch closed."))


@login_required
@require_POST
def batch_cancel(request, batch_id):
    return _batch_action(request, batch_id, services.cancel_batch, _("Batch cancelled."))


@login_required
@require_POST
def line_confirm(request, line_id):
    line = _line_or_404(request.user, line_id)
    if not services.can_act(request.user, line.batch):
        raise PermissionDenied
    form = ConfirmLineForm(request.POST)
    reference = form.cleaned_data["provider_reference"] if form.is_valid() else ""
    try:
        _line, created = services.confirm_line(request.user, line, reference)
        if created:
            messages.success(request, _("%(ref)s confirmed.") % {"ref": line.reference})
    except (ValidationError, PolarisAPIException) as exc:
        _flash(request, exc)
    return redirect(_batch_url(line.batch))


@login_required
@require_POST
def line_fail(request, line_id):
    line = _line_or_404(request.user, line_id)
    if not services.can_act(request.user, line.batch):
        raise PermissionDenied
    form = ReasonForm(request.POST)
    reason = form.cleaned_data["reason"] if form.is_valid() else ""
    try:
        services.fail_line(request.user, line, reason)
        messages.success(request, _("%(ref)s marked failed.") % {"ref": line.reference})
    except (ValidationError, PolarisAPIException) as exc:
        _flash(request, exc)
    return redirect(_batch_url(line.batch))


@login_required
@require_POST
def line_remove(request, line_id):
    line = _line_or_404(request.user, line_id)
    if not services.can_act(request.user, line.batch):
        raise PermissionDenied
    try:
        services.remove_line(request.user, line)
        messages.success(request, _("%(ref)s removed from the batch.") % {"ref": line.reference})
    except (ValidationError, PolarisAPIException) as exc:
        _flash(request, exc)
    return redirect(_batch_url(line.batch))


# --- export layouts --------------------------------------------------------------------------


@login_required
@require_GET
def layout_list(request):
    _require_view(request.user)
    return render(
        request,
        "payouts/layouts.html",
        {
            "layouts": services.layouts_for(request.user).order_by("method", "name"),
            "can_manage": services.can_manage_layouts(request.user),
            "fields": [(key, label) for key, (label, _header) in layouts.FIELDS.items()],
            "nav_active": NAV,
        },
    )


def _layout_form_view(request, layout):
    if not services.can_manage_layouts(request.user):
        raise PermissionDenied
    company_id = services.company_id_for(request.user)
    if request.method not in ("GET", "POST"):
        return HttpResponse(status=405)
    initial = None
    if layout.pk is None:
        initial = {
            "columns_text": layouts.columns_to_text(
                [
                    layouts.column(f)
                    for f in layouts.DEFAULT_LAYOUTS[TransferMethod.BANK_TRANSFER][1]
                ]
            )
        }
    form = ExportLayoutForm(
        request.POST if request.method == "POST" else None,
        instance=layout,
        company_id=company_id,
        initial=initial,
    )
    if request.method == "POST" and form.is_valid():
        try:
            services.save_layout(request.user, form.instance)
        except ValidationError as exc:
            form.add_error(None, exc)
        else:
            messages.success(request, _("Export layout saved."))
            return redirect("payouts:layouts")
    return render(
        request,
        "payouts/layout_form.html",
        {
            "form": form,
            "layout": layout,
            "fields": [(key, label) for key, (label, _header) in layouts.FIELDS.items()],
            "nav_active": NAV,
        },
    )


@login_required
def layout_create(request):
    return _layout_form_view(request, ExportLayout())


@login_required
def layout_edit(request, layout_id):
    if not services.can_manage_layouts(request.user):
        raise PermissionDenied
    layout = get_object_or_404(services.layouts_for(request.user), pk=layout_id)
    return _layout_form_view(request, layout)


# --- payout accounts -----------------------------------------------------------------------


@login_required
@require_GET
def accounts(request):
    workers = services.account_workers(request.user)
    if not (services.can_view_payouts(request.user) or workers.exists()):
        raise PermissionDenied
    company = CompanySettings.load()
    today = dates.company_today(company)
    show = request.GET.get("show", "transfer")
    rows = []
    for worker in (
        workers.filter(is_active=True)
        .select_related("payout_account")
        .order_by("last_name", "first_name", "number")
    ):
        account = services.account_of(worker)
        method = services.worker_method(worker, today, company)
        rows.append({"worker": worker, "account": account, "method": method})
    if show == "transfer":
        rows = [r for r in rows if r["account"] is not None or r["method"] in TransferMethod.values]
    elif show == "missing":
        rows = [
            r
            for r in rows
            if r["method"] in TransferMethod.values
            and not (r["account"] and r["account"].is_complete_for(r["method"]))
        ]
    page = Paginator(rows, PAGE_SIZE).get_page(request.GET.get("page"))
    return render(
        request,
        "payouts/accounts.html",
        {"page": page, "show": show, "nav_active": NAV, "methods": TransferMethod},
    )


@login_required
def account_edit(request, worker_id):
    worker = services.account_workers(request.user).filter(pk=worker_id).first()
    if worker is None:
        raise Http404
    if not services.can_edit_account(request.user, worker):
        raise PermissionDenied
    if request.method not in ("GET", "POST"):
        return HttpResponse(status=405)
    account = services.account_of(worker) or PayoutAccount(worker=worker)
    form = PayoutAccountForm(request.POST if request.method == "POST" else None, instance=account)
    if request.method == "POST" and form.is_valid():
        try:
            services.save_account(request.user, form.instance)
        except ValidationError as exc:
            form.add_error(None, exc)
        else:
            messages.success(request, _("Payout account saved."))
            target = request.POST.get("next", "")
            if url_has_allowed_host_and_scheme(
                target, allowed_hosts={request.get_host()}, require_https=request.is_secure()
            ):
                return redirect(target)
            return redirect("payouts:accounts")
    return render(
        request,
        "payouts/account_form.html",
        {
            "form": form,
            "worker": worker,
            "account": account,
            "method": services.worker_method(worker, dates.company_today()),
            "next": request.GET.get("next", ""),
            "nav_active": NAV,
        },
    )
