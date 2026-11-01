"""Payouts by bank transfer and mobile money: accounts, batches, export and confirmation.

Flow (owner decision, Wave 4): transfers are paid with **bulk-payment files** first.

1. A worker whose effective payment method (``resolve_setting("payment_method", site,
   worker)``) is bank transfer or mobile money has a ``PayoutAccount``. Their pay lines
   are not handed out in cash: they wait for a transfer.
2. ``create_batch`` gathers, for one pay day, one method and optionally one site, every
   pay line that is ready to pay (the cash sheet is open), still has money left to pay
   and is not in another batch. Each becomes a ``PayoutLine`` with the amount left to
   pay, a snapshot of the account and a reference (``PAY-00012-003``). Lines without a
   usable account are left out and listed.
3. ``export_batch`` writes the file in the batch's ``ExportLayout`` through the batch's
   provider (``providers.FileProvider``); the office uploads it to the bank's or the
   operator's portal. Draft → exported.
4. When the provider reports the transfers, lines are confirmed by hand
   (``confirm_line`` / ``fail_line``) or by importing the report
   (``apply_confirmations``). A confirmed line records the payment on the pay line
   (``payroll.services.record_payment`` with the method and the transfer reference as
   proof), exactly once. A failed line goes back to "to pay": it can go in a new batch
   or be paid in cash. Exported → partly confirmed → confirmed (nothing pending).
5. ``close_batch`` once every line is confirmed or failed. A batch with nothing
   confirmed yet can be cancelled.

Permissions: preparing, exporting and confirming need ``PAY_OUT`` on the batch's site
(a batch for every site needs it everywhere: admins). Accountants may read batches.
Every status change, export and confirmation writes an ``AuditLog`` entry.
"""

from __future__ import annotations

import datetime
import hashlib
import re
import uuid
from collections import Counter
from dataclasses import dataclass, field

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError, transaction
from django.db.models import Max, Sum
from django.utils import timezone
from django.utils.translation import gettext as _

from apps.accounts.permissions import (
    ALL,
    EDIT_WORKERS,
    PAY_OUT,
    PERMISSIONS,
    Role,
    can,
    has_role,
    scoped_sites,
)
from apps.api.exceptions import Conflict, Locked
from apps.core.choices import ProofType
from apps.core.dates import company_today
from apps.core.models import AuditLog, CompanySettings
from apps.core.settings import resolve_setting
from apps.core.tenancy import company_id_of
from apps.payroll import services as payroll
from apps.payroll.models import PayLine, PayPeriod
from apps.workers import services as worker_services
from apps.worksites.models import Site

from . import layouts
from .confirmations import FAILURE, ConfirmationRow
from .models import (
    TRANSFER_METHODS,
    ExportLayout,
    PayoutAccount,
    PayoutBatch,
    PayoutLine,
    TransferMethod,
    compact,
    msisdn,
)
from .providers import get_provider

ACTIVE_LINE_STATUSES = (PayoutLine.Status.PENDING, PayoutLine.Status.CONFIRMED)
# Fixed namespace: the payment of a payout line always gets the same client UUID, so a
# confirmation that runs twice can never record two payments.
PAYMENT_UUID_NAMESPACE = uuid.UUID("4f1c7d2e-8a51-4c43-9d0b-6b7a2f6e9c10")


def _audit(actor, action, target=None, *, before=None, after=None, reason=""):
    return AuditLog.record(
        actor=actor if getattr(actor, "is_authenticated", False) else None,
        action=action,
        target=target,
        before=before,
        after=after,
        reason=reason,
    )


# --- permissions -------------------------------------------------------------------------


def company_id_for(user) -> int | None:
    if not getattr(user, "is_authenticated", False) or not user.is_active:
        return None
    return company_id_of(user)


def pays_out_everywhere(user) -> bool:
    """``PAY_OUT`` on every site of the company (admins)."""
    roles = [role for role, scope in PERMISSIONS[PAY_OUT][1].items() if scope == ALL]
    return company_id_for(user) is not None and has_role(user, *roles)


def can_view_payouts(user) -> bool:
    """The payout screens: whoever may pay out somewhere, and accountants (read only)."""
    return company_id_for(user) is not None and (
        can(user, PAY_OUT) or has_role(user, Role.ACCOUNTANT)
    )


def payout_sites(user):
    """The sites where ``user`` may pay out."""
    if company_id_for(user) is None or not can(user, PAY_OUT):
        return Site.objects.none()
    return scoped_sites(user)


def batches_for(user):
    """The batches ``user`` may see: their company's, and for site roles only the batches
    of their own sites."""
    company_id = company_id_for(user)
    if company_id is None or not can_view_payouts(user):
        return PayoutBatch.objects.none()
    batches = PayoutBatch.objects.filter(company_id=company_id)
    if pays_out_everywhere(user) or has_role(user, Role.ACCOUNTANT):
        return batches
    return batches.filter(site__in=scoped_sites(user))


def can_act(user, batch: PayoutBatch) -> bool:
    """Prepare, export, confirm or close this batch."""
    if batch.company_id != company_id_for(user):
        return False
    if batch.site_id is None:
        return pays_out_everywhere(user)
    return can(user, PAY_OUT, batch.site)


def _check_act(user, batch: PayoutBatch) -> None:
    if not can_act(user, batch):
        raise PermissionDenied


def can_manage_layouts(user) -> bool:
    return company_id_for(user) is not None and has_role(user, Role.ADMIN)


# --- layouts -----------------------------------------------------------------------------


def layouts_for(user, method: str | None = None, *, active_only=False):
    company_id = company_id_for(user)
    if company_id is None:
        return ExportLayout.objects.none()
    layouts.ensure_default_layouts(company_id)
    result = ExportLayout.objects.filter(company_id=company_id)
    if method:
        result = result.filter(method=method)
    if active_only:
        result = result.filter(is_active=True)
    return result


def default_layout(company_id: int, method: str) -> ExportLayout | None:
    layouts.ensure_default_layouts(company_id)
    return (
        ExportLayout.objects.filter(company_id=company_id, method=method, is_active=True)
        .order_by("-builtin", "name")
        .first()
    )


def _layout_snapshot(layout: ExportLayout) -> dict:
    return {
        "name": layout.name,
        "method": layout.method,
        "file_type": layout.file_type,
        "columns": layout.columns,
        "header_row": layout.header_row,
        "separator": layout.separator,
        "date_format": layout.date_format,
        "amount_format": layout.amount_format,
        "encoding": layout.encoding,
        "is_active": layout.is_active,
    }


def save_layout(user, layout: ExportLayout) -> ExportLayout:
    """Create or change a company's export layout (admins)."""
    if not can_manage_layouts(user):
        raise PermissionDenied
    company_id = company_id_for(user)
    if layout.pk is not None and layout.company_id != company_id:
        raise PermissionDenied
    layout.company_id = company_id
    layouts.validate_columns(layout.columns)
    before = None
    if layout.pk is not None:
        before = _layout_snapshot(ExportLayout.objects.get(pk=layout.pk))
    layout.full_clean()
    with transaction.atomic():
        layout.save()
        _audit(
            user,
            "payouts.layout.update" if before else "payouts.layout.create",
            layout,
            before=before,
            after=_layout_snapshot(layout),
        )
    return layout


# --- payout accounts ---------------------------------------------------------------------


def account_workers(user, today: datetime.date | None = None):
    """Workers whose payout account ``user`` may see."""
    if not (can_view_payouts(user) or can(user, EDIT_WORKERS)):
        return worker_services.scoped_workers(user, today or company_today()).none()
    return worker_services.scoped_workers(user, today or company_today())


def can_edit_account(user, worker, today: datetime.date | None = None) -> bool:
    if worker.company_id != company_id_for(user):
        return False
    return worker_services.can_edit_worker(user, worker, today or company_today())


def _account_snapshot(account: PayoutAccount | None) -> dict | None:
    if account is None or account.pk is None:
        return None
    return {
        "method": account.method,
        "account_holder": account.account_holder,
        "bank_name": account.bank_name,
        "bank_code": account.bank_code,
        "account_number": account.account_number,
        "mobile_operator": account.mobile_operator,
        "mobile_number": account.mobile_number,
    }


def save_account(user, account: PayoutAccount) -> PayoutAccount:
    """Create or change a worker's payout account (who may edit the worker)."""
    if not can_edit_account(user, account.worker):
        raise PermissionDenied
    before = None
    if account.pk is not None:
        before = _account_snapshot(PayoutAccount.objects.get(pk=account.pk))
    account.full_clean()
    with transaction.atomic():
        account.save()
        _audit(
            user,
            "payouts.account.update" if before else "payouts.account.create",
            account.worker,
            before=before,
            after=_account_snapshot(account),
        )
    return account


def worker_method(worker, today: datetime.date, company: CompanySettings | None = None) -> str:
    """How the worker is paid today: their own method, else their current site's, else
    the company's."""
    site = payroll.current_site(worker, today)
    return resolve_setting("payment_method", site, worker, company=company)


def worker_methods(workers, today: datetime.date, company: CompanySettings) -> dict[int, str]:
    """``worker_method`` for many workers, with their current sites read in one query."""
    workers = list(workers)
    sites = payroll.current_sites([worker.pk for worker in workers], today)
    return {
        worker.pk: resolve_setting("payment_method", sites.get(worker.pk), worker, company=company)
        for worker in workers
    }


def effective_method(line: PayLine, company: CompanySettings | None = None) -> str:
    """How this pay line is paid: the worker's, else the site's, else the company's."""
    return resolve_setting("payment_method", line.period.site, line.worker, company=company)


# --- preparing a batch -------------------------------------------------------------------


@dataclass
class Candidate:
    """A pay line to pay by transfer on the pay day."""

    pay_line: PayLine
    remaining: int
    method: str
    account: PayoutAccount | None

    @property
    def worker(self):
        return self.pay_line.worker

    @property
    def ready(self) -> bool:
        return self.account is not None and self.account.is_complete_for(self.method)

    @property
    def problem(self) -> str:
        if self.account is None:
            return _("No payout account.")
        if self.account.method != self.method:
            return _("The payout account is for %(method)s.") % {
                "method": self.account.get_method_display()
            }
        if not self.ready:
            return _("The payout account is incomplete.")
        return ""


def account_of(worker) -> PayoutAccount | None:
    try:
        return worker.payout_account
    except PayoutAccount.DoesNotExist:
        return None


def candidates(
    user,
    pay_date: datetime.date,
    method: str,
    site: Site | None = None,
    *,
    today: datetime.date | None = None,
) -> list[Candidate]:
    """Pay lines of ``pay_date`` to pay by ``method``, on the sites where ``user`` pays out.

    Only open cash sheets count (every day of the period approved, not locked); lines
    already in a batch (pending or confirmed) and lines fully paid are left out. The pay is
    brought up to date first, like opening the cash sheet.
    """
    company = CompanySettings.load()
    today = today or company_today(company)
    sites = payout_sites(user)
    if site is not None:
        sites = sites.filter(pk=site.pk)
    for active in sites.filter(status=Site.Status.ACTIVE):
        payroll.sync_sheet(active, pay_date, today=today, company=company)
    periods = [
        p
        for p in PayPeriod.objects.filter(site__in=sites, end_date=pay_date)
        .exclude(status=PayPeriod.Status.LOCKED)
        .select_related("site")
        if payroll.period_is_ready(p, today, company)
    ]
    lines = (
        PayLine.objects.filter(period__in=periods)
        .exclude(payout_lines__status__in=ACTIVE_LINE_STATUSES)
        .select_related("worker__payout_account", "worker__trade", "period__site")
        .annotate(paid=Sum("payments__amount"))
        .order_by("period__site__name", "worker__last_name", "worker__first_name", "pk")
    )
    found = []
    for line in lines:
        remaining = line.net - (line.paid or 0)
        if remaining <= 0:
            continue
        line_method = effective_method(line, company)
        if line_method != method:
            continue
        found.append(Candidate(line, remaining, line_method, account_of(line.worker)))
    return found


def _next_number(company_id: int) -> int:
    top = PayoutBatch.objects.filter(company_id=company_id).aggregate(n=Max("number"))["n"]
    return (top or 0) + 1


def _batch_snapshot(batch: PayoutBatch) -> dict:
    return {
        "reference": batch.reference,
        "method": batch.method,
        "pay_date": batch.pay_date.isoformat(),
        "site_id": batch.site_id,
        "status": batch.status,
        "layout_id": batch.layout_id,
    }


def _check_layout(user, layout: ExportLayout | None, method: str) -> ExportLayout | None:
    if layout is None:
        return default_layout(company_id_for(user), method)
    if layout.company_id != company_id_for(user):
        raise PermissionDenied
    if layout.method != method:
        raise ValidationError({"layout": [_("This layout is for another payment method.")]})
    return layout


def create_batch(
    user,
    pay_date: datetime.date,
    method: str,
    site: Site | None = None,
    layout: ExportLayout | None = None,
    *,
    today: datetime.date | None = None,
) -> PayoutBatch:
    """A draft batch with every ready transfer of the pay day (see ``candidates``)."""
    if method not in TransferMethod.values:
        raise ValidationError({"method": [_("Choose bank transfer or mobile money.")]})
    company_id = company_id_for(user)
    if company_id is None:
        raise PermissionDenied
    if site is None:
        if not pays_out_everywhere(user):
            raise PermissionDenied
    elif site.company_id != company_id or not can(user, PAY_OUT, site):
        raise PermissionDenied
    layout = _check_layout(user, layout, method)
    ready = [c for c in candidates(user, pay_date, method, site, today=today) if c.ready]
    if not ready:
        raise ValidationError(
            _("Nobody is ready to be paid by %(method)s on this pay day.")
            % {"method": TransferMethod(method).label.lower()}
        )
    currency = ready[0].pay_line.period.currency
    decimals = ready[0].pay_line.period.currency_decimals
    ready = [c for c in ready if c.pay_line.period.currency == currency]
    try:
        with transaction.atomic():
            batch = PayoutBatch.objects.create(
                company_id=company_id,
                number=_next_number(company_id),
                method=method,
                pay_date=pay_date,
                site=site,
                layout=layout,
                currency=currency,
                currency_decimals=decimals,
                created_by=user,
            )
            for index, candidate in enumerate(ready, start=1):
                account = candidate.account
                PayoutLine.objects.create(
                    batch=batch,
                    pay_line=candidate.pay_line,
                    amount=candidate.remaining,
                    reference=f"{batch.reference}-{index:03d}",
                    method=method,
                    account_holder=account.holder,
                    bank_name=account.bank_name,
                    bank_code=account.bank_code,
                    account_number=account.account_number,
                    mobile_operator=account.mobile_operator,
                    mobile_number=account.mobile_number,
                )
            _audit(
                user,
                "payouts.batch.create",
                batch,
                after={
                    **_batch_snapshot(batch),
                    "lines": len(ready),
                    "total": sum(c.remaining for c in ready),
                    "pay_line_ids": [c.pay_line.pk for c in ready],
                },
            )
    except IntegrityError as exc:
        # Another batch took one of these lines at the same moment.
        raise Conflict(
            _("Some of these pay lines were just put in another batch. Try again."),
            details={},
        ) from exc
    return batch


# --- batch status --------------------------------------------------------------------------


def _set_status(user, batch: PayoutBatch, status: str, *, reason: str = "", **extra) -> None:
    old = batch.status
    if old == status:
        return
    batch.status = status
    fields = ["status", "updated_at"]
    for name, value in extra.items():
        setattr(batch, name, value)
        fields.append(name)
    batch.save(update_fields=fields)
    _audit(
        user,
        "payouts.batch.status",
        batch,
        before={"status": old},
        after={"status": status, "reference": batch.reference},
        reason=reason,
    )


def _sync_status(user, batch: PayoutBatch) -> None:
    """Exported / partly confirmed / confirmed, from the lines."""
    if batch.status in (
        PayoutBatch.Status.DRAFT,
        PayoutBatch.Status.CLOSED,
        PayoutBatch.Status.CANCELLED,
    ):
        return
    counts = Counter(
        batch.lines.exclude(status=PayoutLine.Status.CANCELLED).values_list("status", flat=True)
    )
    pending = counts[PayoutLine.Status.PENDING]
    resolved = counts[PayoutLine.Status.CONFIRMED] + counts[PayoutLine.Status.FAILED]
    if pending == 0 and resolved:
        status = PayoutBatch.Status.CONFIRMED
    elif resolved:
        status = PayoutBatch.Status.PARTLY_CONFIRMED
    else:
        status = PayoutBatch.Status.EXPORTED
    _set_status(user, batch, status)


def _reload(batch: PayoutBatch) -> PayoutBatch:
    return PayoutBatch.objects.select_related("site", "layout").get(pk=batch.pk)


def pending_lines(batch: PayoutBatch):
    return (
        batch.lines.filter(status=PayoutLine.Status.PENDING)
        .select_related("pay_line__worker", "pay_line__period__site")
        .order_by("reference")
    )


def export_batch(user, batch: PayoutBatch, layout: ExportLayout | None = None):
    """The bulk-payment file of the batch's pending lines (draft → exported).

    Exporting again gives the same lines (a lost file, another layout); each export is
    in the audit log with the file's SHA-256.
    """
    _check_act(user, batch)
    batch = _reload(batch)
    if batch.status not in (
        PayoutBatch.Status.DRAFT,
        PayoutBatch.Status.EXPORTED,
        PayoutBatch.Status.PARTLY_CONFIRMED,
    ):
        raise Conflict(_("This batch can no longer be exported."), details={"status": batch.status})
    layout = _check_layout(user, layout or batch.layout, batch.method)
    if layout is None:
        raise ValidationError({"layout": [_("Choose an export layout.")]})
    lines = list(pending_lines(batch))
    if not lines:
        raise Conflict(_("Nothing is left to pay in this batch."), details={})
    company = CompanySettings.load()
    exported = get_provider(batch.provider).submit(
        batch, lines, layout, company.company_name or str(company.company)
    )
    with transaction.atomic():
        if batch.layout_id != layout.pk:
            batch.layout = layout
            batch.save(update_fields=["layout", "updated_at"])
        batch.export_count += 1
        batch.exported_at = timezone.now()
        batch.save(update_fields=["export_count", "exported_at", "updated_at"])
        if batch.status == PayoutBatch.Status.DRAFT:
            _set_status(user, batch, PayoutBatch.Status.EXPORTED)
        _audit(
            user,
            "payouts.batch.export",
            batch,
            after={
                "reference": batch.reference,
                "layout_id": layout.pk,
                "layout": layout.name,
                "provider": batch.provider,
                "filename": exported.filename if exported else "",
                "rows": len(lines),
                "total": sum(line.amount for line in lines),
                "sha256": hashlib.sha256(exported.content).hexdigest() if exported else "",
            },
        )
    return exported


def remove_line(user, line: PayoutLine, reason: str = "") -> PayoutLine:
    """Take a line out of a draft batch (it goes back to "to pay")."""
    batch = line.batch
    _check_act(user, batch)
    if batch.status != PayoutBatch.Status.DRAFT:
        raise Conflict(
            _("Only a draft batch can change. Mark the line failed instead."),
            details={"status": batch.status},
        )
    if line.status == PayoutLine.Status.CANCELLED:
        return line
    with transaction.atomic():
        line.status = PayoutLine.Status.CANCELLED
        line.resolved_at, line.resolved_by = timezone.now(), user
        line.save(update_fields=["status", "resolved_at", "resolved_by", "updated_at"])
        _audit(
            user,
            "payouts.line.remove",
            line,
            after={"reference": line.reference, "pay_line_id": line.pay_line_id},
            reason=reason,
        )
        if not batch.lines.filter(status=PayoutLine.Status.PENDING).exists():
            _set_status(user, batch, PayoutBatch.Status.CANCELLED, reason=reason)
    return line


def cancel_batch(user, batch: PayoutBatch, reason: str = "") -> PayoutBatch:
    """Give up a batch nothing of which is confirmed or failed yet. An exported batch
    needs a reason (the file may already be in the provider's portal)."""
    _check_act(user, batch)
    batch = _reload(batch)
    if batch.status not in (PayoutBatch.Status.DRAFT, PayoutBatch.Status.EXPORTED):
        raise Conflict(
            _("Lines of this batch are already confirmed or failed: close it instead."),
            details={"status": batch.status},
        )
    if batch.status == PayoutBatch.Status.EXPORTED and not reason.strip():
        raise ValidationError(
            {"reason": [_("The file was exported: say why the batch is cancelled.")]}
        )
    with transaction.atomic():
        batch.lines.filter(status=PayoutLine.Status.PENDING).update(
            status=PayoutLine.Status.CANCELLED,
            resolved_at=timezone.now(),
            resolved_by=user,
            updated_at=timezone.now(),
        )
        _set_status(user, batch, PayoutBatch.Status.CANCELLED, reason=reason)
    return batch


def close_batch(user, batch: PayoutBatch, reason: str = "") -> PayoutBatch:
    """Every line is confirmed or failed: the batch is done."""
    _check_act(user, batch)
    batch = _reload(batch)
    if batch.status == PayoutBatch.Status.CLOSED:
        return batch
    if batch.status != PayoutBatch.Status.CONFIRMED:
        raise Conflict(
            _("Mark every line confirmed or failed before closing the batch."),
            details={"status": batch.status},
        )
    with transaction.atomic():
        _set_status(user, batch, PayoutBatch.Status.CLOSED, reason=reason, closed_at=timezone.now())
    return batch


# --- confirmation --------------------------------------------------------------------------


def _check_confirmable(batch: PayoutBatch) -> None:
    if batch.status == PayoutBatch.Status.DRAFT:
        raise Conflict(
            _("Export the batch before confirming transfers."), details={"status": batch.status}
        )
    if not batch.is_open:
        raise Locked(_("This batch is closed."), details={"status": batch.status})


def payment_uuid(line: PayoutLine) -> uuid.UUID:
    return uuid.uuid5(PAYMENT_UUID_NAMESPACE, f"payout-line:{line.pk}")


def confirm_line(
    user,
    line: PayoutLine,
    provider_reference: str = "",
    *,
    paid_at: datetime.datetime | None = None,
    source: str = "manual",
) -> tuple[PayoutLine, bool]:
    """The transfer reached the worker: record the payment on the pay line.

    Returns ``(line, created)``. Confirming a confirmed line changes nothing
    (``created`` is False). Raises ``PermissionDenied``, ``Conflict`` (draft batch, line
    failed or cancelled, worker already paid another way), ``Locked`` (batch closed, pay
    period locked) or ``ValidationError`` (the pay changed since the batch was prepared).
    """
    _check_act(user, line.batch)
    with transaction.atomic():
        line = PayoutLine.objects.select_related("batch", "pay_line").get(pk=line.pk)
        if line.status == PayoutLine.Status.CONFIRMED:
            return line, False
        _check_confirmable(line.batch)
        if line.status != PayoutLine.Status.PENDING:
            raise Conflict(
                _("This line is %(status)s: it cannot be confirmed.")
                % {"status": line.get_status_display().lower()},
                details={"status": line.status},
            )
        reference = provider_reference.strip()[:100] or line.reference
        try:
            with payroll.transfer_proof(reference):
                payment = payroll.record_payment(
                    user,
                    line.pay_line,
                    amount=line.amount,
                    proof_type=ProofType.NONE,
                    paid_at=paid_at,
                    client_uuid=payment_uuid(line),
                    method=line.method,
                )
        except ValidationError as exc:
            if "amount" in getattr(exc, "message_dict", {}):
                raise ValidationError(
                    _(
                        "The pay of this worker changed after the batch was prepared: "
                        "mark the line failed and pay them again."
                    )
                ) from exc
            raise
        line.status = PayoutLine.Status.CONFIRMED
        line.payment = payment
        line.provider_reference = provider_reference.strip()[:100]
        line.failure_reason = ""
        line.resolved_at, line.resolved_by = timezone.now(), user
        line.save()
        _audit(
            user,
            "payouts.line.confirm",
            line,
            after={
                "reference": line.reference,
                "provider_reference": line.provider_reference,
                "amount": line.amount,
                "pay_line_id": line.pay_line_id,
                "payment_id": payment.pk,
                "source": source,
            },
        )
        _sync_status(user, line.batch)
    return line, True


def fail_line(user, line: PayoutLine, reason: str, *, source: str = "manual") -> PayoutLine:
    """The transfer did not go through: the pay line goes back to "to pay"."""
    _check_act(user, line.batch)
    if not reason.strip():
        raise ValidationError({"reason": [_("Say why the transfer failed.")]})
    with transaction.atomic():
        line = PayoutLine.objects.select_related("batch").get(pk=line.pk)
        if line.status == PayoutLine.Status.FAILED:
            return line
        _check_confirmable(line.batch)
        if line.status != PayoutLine.Status.PENDING:
            raise Conflict(
                _("This line is %(status)s: it cannot be marked failed.")
                % {"status": line.get_status_display().lower()},
                details={"status": line.status},
            )
        line.status = PayoutLine.Status.FAILED
        line.failure_reason = reason.strip()[:300]
        line.resolved_at, line.resolved_by = timezone.now(), user
        line.save()
        _audit(
            user,
            "payouts.line.fail",
            line,
            after={
                "reference": line.reference,
                "amount": line.amount,
                "pay_line_id": line.pay_line_id,
                "source": source,
            },
            reason=line.failure_reason,
        )
        _sync_status(user, line.batch)
    return line


# --- importing a confirmation report ---------------------------------------------------------

CONFIRMED = "confirmed"
ALREADY_CONFIRMED = "already_confirmed"
FAILED = "failed"
UNKNOWN = "unknown"
AMOUNT_MISMATCH = "amount_mismatch"
ERROR = "error"

_REFERENCE_RE = re.compile(r"PAY-\d{5,}-\d{3,}", re.IGNORECASE)


@dataclass
class RowResult:
    row: ConfirmationRow
    outcome: str
    line: PayoutLine | None = None
    message: str = ""


@dataclass
class ImportResult:
    rows: list[RowResult] = field(default_factory=list)
    check_only: bool = False

    @property
    def counts(self) -> dict[str, int]:
        return dict(Counter(r.outcome for r in self.rows))

    @property
    def not_applied(self) -> int:
        """Rows that changed nothing: unknown, amount differs, unreadable or refused."""
        return sum(r.outcome in (UNKNOWN, AMOUNT_MISMATCH, ERROR) for r in self.rows)


def _account_key(value: str) -> str:
    text = compact(value)
    return msisdn(text) if re.fullmatch(r"\+?[0-9]+", text) else text


def _same_account(a: str, b: str) -> bool:
    a, b = _account_key(a), _account_key(b)
    if not a or not b:
        return False
    if a == b:
        return True
    # A phone written with and without its country code.
    if a.isdigit() and b.isdigit() and min(len(a), len(b)) >= 8:
        return a.endswith(b) or b.endswith(a)
    return False


def _line_account(line: PayoutLine) -> str:
    if line.method == TransferMethod.BANK_TRANSFER:
        return line.account_number
    return line.mobile_number


def match_row(row: ConfirmationRow, lines: list[PayoutLine]) -> tuple[PayoutLine | None, str]:
    """The batch line a report row is about: by reference, else by account (and amount)."""
    by_reference = {line.reference.upper(): line for line in lines}
    reference = row.reference.strip().upper()
    if reference:
        if reference in by_reference:
            return by_reference[reference], ""
        for found in _REFERENCE_RE.findall(reference):
            if found.upper() in by_reference:
                return by_reference[found.upper()], ""
    if row.account:
        same = [line for line in lines if _same_account(row.account, _line_account(line))]
        if row.amount is not None:
            same = [line for line in same if line.amount == row.amount] or same
        if len(same) > 1:
            pending = [line for line in same if line.status == PayoutLine.Status.PENDING]
            same = pending if len(pending) == 1 else same
        if len(same) == 1:
            return same[0], ""
        if len(same) > 1:
            return None, _("Several lines of this batch have this account.")
    return None, _("No line of this batch has this reference or account.")


def apply_confirmations(
    user, batch: PayoutBatch, rows: list[ConfirmationRow], *, check_only=False, filename=""
) -> ImportResult:
    """Match a provider's report to the batch and confirm or fail the lines.

    Rows that match nothing, rows whose amount differs from the line's, and unreadable
    rows change nothing and are reported. Running the same report twice changes nothing
    the second time. With ``check_only``, nothing is written: the result says what would
    happen.
    """
    _check_act(user, batch)
    batch = _reload(batch)
    _check_confirmable(batch)
    lines = list(
        batch.lines.exclude(status=PayoutLine.Status.CANCELLED).select_related("pay_line__worker")
    )
    result = ImportResult(check_only=check_only)
    for row in rows:
        if row.error:
            result.rows.append(RowResult(row, ERROR, message=row.error))
            continue
        line, why = match_row(row, lines)
        if line is None:
            result.rows.append(RowResult(row, UNKNOWN, message=why))
            continue
        if row.amount is not None and row.amount != line.amount:
            result.rows.append(
                RowResult(row, AMOUNT_MISMATCH, line, _("The amount differs from the batch."))
            )
            continue
        result.rows.append(_apply_row(user, row, line, check_only))
        line.refresh_from_db(fields=["status"])
    if not check_only:
        _audit(
            user,
            "payouts.batch.import",
            batch,
            after={
                "reference": batch.reference,
                "filename": filename,
                "rows": len(rows),
                "counts": result.counts,
            },
        )
    return result


def _apply_row(user, row: ConfirmationRow, line: PayoutLine, check_only: bool) -> RowResult:
    if row.status == FAILURE:
        if line.status == PayoutLine.Status.CONFIRMED:
            return RowResult(
                row,
                ERROR,
                line,
                _("Reported failed, but this line is already confirmed: check the payment."),
            )
        if line.status == PayoutLine.Status.FAILED or check_only:
            return RowResult(row, FAILED, line, row.message)
        try:
            fail_line(
                user,
                line,
                row.message or _("Failed in the provider's report."),
                source="import",
            )
        except (ValidationError, PermissionDenied, Conflict, Locked) as exc:
            return RowResult(row, ERROR, line, _error_text(exc))
        return RowResult(row, FAILED, line, row.message)
    if line.status == PayoutLine.Status.CONFIRMED:
        return RowResult(row, ALREADY_CONFIRMED, line)
    if line.status != PayoutLine.Status.PENDING:
        return RowResult(
            row,
            ERROR,
            line,
            _("This line is %(status)s.") % {"status": line.get_status_display().lower()},
        )
    if check_only:
        return RowResult(row, CONFIRMED, line)
    try:
        confirm_line(user, line, row.provider_reference, source="import")
    except (ValidationError, PermissionDenied, Conflict, Locked) as exc:
        return RowResult(row, ERROR, line, _error_text(exc))
    return RowResult(row, CONFIRMED, line)


def _error_text(exc) -> str:
    if isinstance(exc, ValidationError):
        return " ".join(exc.messages)
    if isinstance(exc, PermissionDenied):
        return _("You may not pay this worker.")
    return str(exc.detail)


def import_confirmations(user, batch: PayoutBatch, name: str, content: bytes, **kwargs):
    """Read an uploaded report with the batch's provider and apply it."""
    _check_act(user, batch)
    batch = _reload(batch)
    rows = get_provider(batch.provider).confirmations(batch, (name, content))
    return apply_confirmations(user, batch, rows, filename=name, **kwargs)


# --- the cash sheet ------------------------------------------------------------------------


@dataclass
class TransferState:
    """How a pay line paid by transfer stands (for the cash sheet)."""

    method: str
    line: PayoutLine | None  # the pending or confirmed payout line
    failed: PayoutLine | None  # the last failed one, when no batch holds it now

    @property
    def method_label(self) -> str:
        return TransferMethod(self.method).label


def transfer_state(pay_line: PayLine, company: CompanySettings | None = None):
    """``None`` for a line paid in cash, else its ``TransferState``."""
    method = effective_method(pay_line, company)
    if method not in TRANSFER_METHODS:
        return None
    payout_lines = list(
        pay_line.payout_lines.select_related("batch").order_by("-created_at", "-pk")
    )
    active = next((p for p in payout_lines if p.status in ACTIVE_LINE_STATUSES), None)
    failed = None
    if active is None:
        failed = next((p for p in payout_lines if p.status == PayoutLine.Status.FAILED), None)
    return TransferState(method, active, failed)


def batch_totals(batch: PayoutBatch) -> dict[str, int]:
    """Amounts by line status, and the number of lines."""
    rows = batch.lines.values("status").annotate(total=Sum("amount"))
    totals = {row["status"]: row["total"] or 0 for row in rows}
    totals["all"] = sum(v for k, v in totals.items() if k != PayoutLine.Status.CANCELLED)
    totals["count"] = batch.lines.exclude(status=PayoutLine.Status.CANCELLED).count()
    return totals


def stale_lines(batch: PayoutBatch) -> set[int]:
    """Pending lines whose pay changed since the batch was prepared (amount ≠ left to pay)."""
    pending = batch.lines.filter(status=PayoutLine.Status.PENDING).select_related("pay_line")
    paid = dict(
        PayLine.objects.filter(pk__in=[p.pay_line_id for p in pending])
        .annotate(paid=Sum("payments__amount"))
        .values_list("pk", "paid")
    )
    return {p.pk for p in pending if p.amount != p.pay_line.net - (paid.get(p.pay_line_id) or 0)}
