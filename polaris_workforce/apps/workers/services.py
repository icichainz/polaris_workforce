"""Worker rules: numbering, pay-rate history, absence streaks, who sees which workers.

Every write to a rate or an assignment writes an ``AuditLog`` entry (assignments go
through ``apps.worksites.services.assign``).
"""

import datetime
import re
from collections import defaultdict

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.db.models import Exists, OuterRef, Q, Sum
from django.db.models.functions import Length
from django.utils.translation import gettext as _

from apps.accounts.permissions import Role, company_workers, has_role, scoped_sites
from apps.attendance.models import AttendanceRecord
from apps.core import dates
from apps.core.choices import PayType, ProofType
from apps.core.models import AuditLog
from apps.core.tenancy import current_company
from apps.payroll.models import Advance, PayLine, Payment, PayPeriod
from apps.worksites import services as site_services
from apps.worksites.models import Site, SiteAssignment

from .models import EXPIRY_WARNING_DAYS, PayRate, Worker, WorkerDocument

NUMBER_PREFIX = "W-"
NUMBER_WIDTH = 4
_NUMBER_RE = re.compile(rf"^{re.escape(NUMBER_PREFIX)}(\d+)$")

# How far back absent_streak looks (in calendar days).
STREAK_LOOKBACK_DAYS = 120

WORKER_FIELDS = (
    "first_name",
    "last_name",
    "phone",
    "trade_id",
    "id_document_type",
    "id_document_number",
    "is_active",
    "payment_method",
    "pay_frequency",
    "emergency_contact_name",
    "emergency_contact_relationship",
    "emergency_contact_phone",
)


# --- numbering --------------------------------------------------------------------------


def next_worker_number(company=None) -> str:
    """The next free number in the company (``W-0001``, ``W-0002``…, wider after
    ``W-9999``); each company has its own sequence. Default: the current company.

    Call it inside the transaction that saves the worker: with SQLite's IMMEDIATE
    transactions the write lock is held from the start, so two creates can't both read
    the same last number, and the unique constraint catches anything else.
    """
    company = company or current_company()
    numbers = (
        Worker.objects.filter(company=company, number__startswith=NUMBER_PREFIX)
        .order_by(Length("number").desc(), "-number")
        .values_list("number", flat=True)
    )
    for number in numbers:
        match = _NUMBER_RE.match(number)
        if match:
            return f"{NUMBER_PREFIX}{int(match.group(1)) + 1:0{NUMBER_WIDTH}d}"
    return f"{NUMBER_PREFIX}{1:0{NUMBER_WIDTH}d}"


def _worker_snapshot(worker: Worker) -> dict:
    return {name: getattr(worker, name) for name in ("number", *WORKER_FIELDS)}


def create_worker(
    *,
    actor=None,
    site: Site | None = None,
    start_date: datetime.date | None = None,
    pay_type: str = PayType.DAILY,
    amount: int | None = None,
    effective_from: datetime.date | None = None,
    reason: str = "",
    **fields,
) -> Worker:
    """Create a worker of the current company (or ``fields["company"]``) with the next
    number, then assign and give a first rate if given. ``reason`` goes on the audit entry
    (the Excel import names the file)."""
    company = fields.pop("company", None) or current_company()
    for attempt in range(3):
        try:
            with transaction.atomic():
                worker = Worker(company=company, number=next_worker_number(company), **fields)
                worker.full_clean(exclude=["photo"])
                worker.save()
                AuditLog.record(
                    actor=actor,
                    action="workers.create",
                    target=worker,
                    after=_worker_snapshot(worker),
                    reason=reason,
                )
                start_date = start_date or dates.company_today()
                if site is not None:
                    site_services.assign(worker, site, start_date, actor=actor)
                if amount is not None:
                    change_rate(worker, amount, pay_type, effective_from or start_date, actor=actor)
                return worker
        except IntegrityError:
            # Someone took the same number between the read and the insert: retry.
            if attempt == 2:
                raise
    raise AssertionError("unreachable")  # pragma: no cover


def update_worker(
    worker: Worker,
    changes: dict,
    *,
    actor=None,
    site: Site | None = None,
    start_date: datetime.date | None = None,
    pay_type: str | None = None,
    amount: int | None = None,
    effective_from: datetime.date | None = None,
) -> Worker:
    """Save identity / override changes, then move site and change the rate if asked.

    All in one transaction: a refused assignment or rate leaves the worker unchanged.
    """
    with transaction.atomic():
        before = _worker_snapshot(Worker.objects.get(pk=worker.pk))
        for name, value in changes.items():
            setattr(worker, name, value)
        worker.full_clean(exclude=["photo"])
        worker.save()
        after = _worker_snapshot(worker)
        if after != before:
            AuditLog.record(
                actor=actor, action="workers.update", target=worker, before=before, after=after
            )
        if site is not None:
            site_services.assign(worker, site, start_date, actor=actor)
        if amount is not None:
            change_rate_if_different(worker, amount, pay_type, effective_from, actor=actor)
    return worker


# --- pay rates --------------------------------------------------------------------------


def _rate_snapshot(rate: PayRate | None) -> dict | None:
    if rate is None:
        return None
    return {
        "pay_type": rate.pay_type,
        "amount": rate.amount,
        "effective_from": rate.effective_from.isoformat(),
    }


def change_rate(
    worker: Worker,
    amount: int,
    pay_type: str,
    effective_from: datetime.date,
    *,
    actor=None,
    reason: str = "",
) -> PayRate:
    """Give ``worker`` a new rate from ``effective_from``; earlier rates stay as history.

    The new rate ends the previous one (``Worker.rate_on`` picks the latest rate that
    started on or before a date). A second change on the same date replaces that day's
    rate. Dates inside a locked pay period the worker was paid in are refused.
    """
    if isinstance(amount, bool) or not isinstance(amount, int):
        raise TypeError("amount must be an int in the currency's smallest unit")
    if amount < 0:
        raise ValidationError({"amount": _("The rate cannot be negative.")})
    if pay_type not in PayType.values:
        raise ValidationError({"pay_type": _("Unknown pay type.")})
    if effective_from is None:
        raise ValidationError({"effective_from": _("Set the date the rate starts.")})

    locked = PayLine.objects.filter(
        worker=worker,
        period__status=PayPeriod.Status.LOCKED,
        period__end_date__gte=effective_from,
    ).exists()
    if locked:
        raise ValidationError(
            {"effective_from": _("A locked pay period covers this date. Choose a date after it.")}
        )

    with transaction.atomic():
        before = _rate_snapshot(worker.rate_on(effective_from))
        rate, created = PayRate.objects.get_or_create(
            worker=worker,
            effective_from=effective_from,
            defaults={"amount": amount, "pay_type": pay_type},
        )
        if not created:
            if rate.amount == amount and rate.pay_type == pay_type:
                return rate
            rate.amount = amount
            rate.pay_type = pay_type
            rate.save(update_fields=["amount", "pay_type", "updated_at"])
        AuditLog.record(
            actor=actor,
            action="workers.rate_change",
            target=worker,
            before=before,
            after=_rate_snapshot(rate),
            reason=reason,
        )
    return rate


def change_rate_if_different(
    worker: Worker, amount: int, pay_type: str, effective_from: datetime.date, *, actor=None
) -> PayRate | None:
    """``change_rate`` unless that rate already applies on ``effective_from`` (edit form)."""
    current = worker.rate_on(effective_from)
    if current is not None and current.amount == amount and current.pay_type == pay_type:
        return None
    return change_rate(worker, amount, pay_type, effective_from, actor=actor)


def rate_history(worker: Worker) -> list[dict]:
    """Rates newest first, each with the last day it applied (``None`` = still current)."""
    rates = list(worker.pay_rates.order_by("-effective_from"))
    history = []
    next_start = None
    for rate in rates:
        until = next_start - datetime.timedelta(days=1) if next_start else None
        history.append({"rate": rate, "until": until})
        next_start = rate.effective_from
    return history


def rates_on(workers, date: datetime.date) -> dict[int, PayRate]:
    """``{worker_id: rate in effect on date}`` in one query."""
    ids = [w.pk for w in workers]
    found: dict[int, PayRate] = {}
    for rate in PayRate.objects.filter(worker_id__in=ids, effective_from__lte=date).order_by(
        "worker_id", "-effective_from"
    ):
        found.setdefault(rate.worker_id, rate)
    return found


# --- attendance -------------------------------------------------------------------------


def absent_streaks(workers, date: datetime.date) -> dict[int, int]:
    """``{worker_id: consecutive absences right before date}`` in one query.

    Only recorded days count: a day with no record for the worker (a day off, a site
    with no roll call) neither extends nor breaks the streak. Looks back
    ``STREAK_LOOKBACK_DAYS`` days at most.
    """
    ids = [getattr(w, "pk", w) for w in workers]
    streaks = dict.fromkeys(ids, 0)
    broken: set[int] = set()
    records = (
        AttendanceRecord.objects.filter(
            worker_id__in=ids,
            day__date__lt=date,
            day__date__gte=date - datetime.timedelta(days=STREAK_LOOKBACK_DAYS),
        )
        .order_by("worker_id", "-day__date")
        .values_list("worker_id", "status")
    )
    for worker_id, status in records:
        if worker_id in broken:
            continue
        if status == AttendanceRecord.Status.ABSENT:
            streaks[worker_id] += 1
        else:
            broken.add(worker_id)
    return streaks


def absent_streak(worker: Worker, date: datetime.date) -> int:
    """Consecutive recorded working days the worker was absent right before ``date``."""
    return absent_streaks([worker], date)[worker.pk]


def statuses_on(workers, date: datetime.date) -> dict[int, str]:
    """``{worker_id: attendance status on date}`` (missing = not recorded)."""
    ids = [w.pk for w in workers]
    return dict(
        AttendanceRecord.objects.filter(worker_id__in=ids, day__date=date).values_list(
            "worker_id", "status"
        )
    )


# --- payroll (read-only) ----------------------------------------------------------------


def advances_owed(workers) -> dict[int, int]:
    """``{worker_id: open advance balance}`` (minor units)."""
    ids = [w.pk for w in workers]
    rows = (
        Advance.objects.filter(worker_id__in=ids, status=Advance.Status.OPEN)
        .values("worker_id")
        .annotate(total=Sum("balance"))
    )
    return {row["worker_id"]: row["total"] or 0 for row in rows}


# --- scoping ----------------------------------------------------------------------------


def scoped_workers(user, today: datetime.date):
    """The workers this user may see.

    Admins and accountants see everyone in their company. Site managers and foremen see
    workers with an assignment on one of their sites that has not ended before ``today``.
    """
    workers = company_workers(user)
    if has_role(user, Role.ADMIN, Role.ACCOUNTANT):
        return workers
    sites = scoped_sites(user)
    on_my_sites = SiteAssignment.objects.filter(worker=OuterRef("pk"), site__in=sites).filter(
        Q(end_date__isnull=True) | Q(end_date__gte=today)
    )
    return workers.filter(Exists(on_my_sites))


def can_edit_worker(user, worker: Worker | None, today: datetime.date) -> bool:
    """Admins edit anyone; site managers edit the workers they can see (and add new ones)."""
    if has_role(user, Role.ADMIN):
        return True
    if not has_role(user, Role.SITE_MANAGER):
        return False
    if worker is None:
        return True
    return scoped_workers(user, today).filter(pk=worker.pk).exists()


def assignable_sites(user):
    """Sites this user may put workers on."""
    if has_role(user, Role.ADMIN, Role.SITE_MANAGER):
        return scoped_sites(user).exclude(status=Site.Status.COMPLETED)
    return Site.objects.none()


def month_summary(records, half_day_units) -> dict:
    """Counts per status and days worked (½ = ``half_day_units``) for a set of records."""
    counts = defaultdict(int)
    for record in records:
        counts[record.status] += 1
    units = (
        counts[AttendanceRecord.Status.PRESENT]
        + counts[AttendanceRecord.Status.LATE]
        + counts[AttendanceRecord.Status.HALF_DAY] * half_day_units
    )
    return {"counts": counts, "units": units}


def working_days_between(first: datetime.date, last: datetime.date, working_days) -> int:
    """Number of working days (ISO weekdays in ``working_days``) from first to last."""
    if last < first:
        return 0
    return sum(
        1
        for n in range((last - first).days + 1)
        if (first + datetime.timedelta(days=n)).isoweekday() in working_days
    )


def visible_payments(user, worker: Worker):
    """The worker's payments made on sites ``user`` may see.

    A worker who moved between sites keeps payments from the old site; the manager of the
    new site must not see their amounts or proofs (and the reverse).
    """
    return Payment.objects.filter(
        pay_line__worker=worker, pay_line__period__site__in=scoped_sites(user)
    )


def payments_between(
    worker: Worker, start: datetime.datetime, end: datetime.datetime, *, user=None
) -> dict:
    """Total, count and receipt coverage of payments made to the worker in [start, end).

    With ``user``, only payments on the sites that user may see count.
    """
    payments = Payment.objects.filter(pay_line__worker=worker)
    if user is not None:
        payments = visible_payments(user, worker)
    payments = payments.filter(paid_at__gte=start, paid_at__lt=end)
    count = payments.count()
    return {
        "total": payments.aggregate(total=Sum("amount"))["total"] or 0,
        "count": count,
        "all_with_proof": count > 0 and not payments.filter(proof_type=ProofType.NONE).exists(),
    }


def advance_summary(worker: Worker) -> dict:
    """Open advance balance and the deduction taken per pay (minor units)."""
    totals = Advance.objects.filter(worker=worker, status=Advance.Status.OPEN).aggregate(
        owed=Sum("balance"), deduction=Sum("deduction_per_pay")
    )
    return {"owed": totals["owed"] or 0, "deduction": totals["deduction"] or 0}


# --- documents --------------------------------------------------------------------------


def can_view_documents(user) -> bool:
    """Contracts and ID scans are for the office: admins, accountants and site managers
    (on the workers they may see). Foremen don't see them."""
    return has_role(user, Role.ADMIN, Role.ACCOUNTANT, Role.SITE_MANAGER)


def _document_snapshot(document: WorkerDocument) -> dict:
    return {
        "type": document.type,
        "title": document.title,
        "file": document.file.name,
        "issued_on": document.issued_on.isoformat() if document.issued_on else None,
        "expires_on": document.expires_on.isoformat() if document.expires_on else None,
    }


def add_document(worker: Worker, *, actor, file, **fields) -> WorkerDocument:
    """Store a document for ``worker`` (fields: type, title, issued_on, expires_on)."""
    document = WorkerDocument(worker=worker, uploaded_by=actor, file=file, **fields)
    document.full_clean()
    with transaction.atomic():
        document.save()
        AuditLog.record(
            actor=actor,
            action="workers.document_add",
            target=worker,
            after=_document_snapshot(document),
        )
    return document


def delete_document(document: WorkerDocument, *, actor) -> None:
    """Remove the document and, once the transaction commits, its file."""
    name = document.file.name
    storage = document.file.storage
    with transaction.atomic():
        AuditLog.record(
            actor=actor,
            action="workers.document_delete",
            target=document.worker,
            before=_document_snapshot(document),
        )
        document.delete()
        if name:
            transaction.on_commit(lambda: storage.delete(name))


def needs_document_attention(today: datetime.date) -> Q:
    """Workers with a document expired, or expiring within ``EXPIRY_WARNING_DAYS``."""
    limit = today + datetime.timedelta(days=EXPIRY_WARNING_DAYS)
    return Q(Exists(WorkerDocument.objects.filter(worker=OuterRef("pk"), expires_on__lte=limit)))


def has_expired_document(today: datetime.date) -> Q:
    return Q(Exists(WorkerDocument.objects.filter(worker=OuterRef("pk"), expires_on__lt=today)))


def document_expiry_by_worker(workers, today: datetime.date) -> dict[int, str]:
    """``{worker_id: "expired" | "expiring"}`` for workers with such a document, in one
    query; expired wins over expiring."""
    ids = [getattr(w, "pk", w) for w in workers]
    limit = today + datetime.timedelta(days=EXPIRY_WARNING_DAYS)
    found: dict[int, str] = {}
    rows = WorkerDocument.objects.filter(worker_id__in=ids, expires_on__lte=limit).values_list(
        "worker_id", "expires_on"
    )
    for worker_id, expires_on in rows:
        if expires_on < today:
            found[worker_id] = WorkerDocument.Expiry.EXPIRED
        else:
            found.setdefault(worker_id, WorkerDocument.Expiry.EXPIRING)
    return found
