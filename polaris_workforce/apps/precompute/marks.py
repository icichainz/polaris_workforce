"""Which figures an audit entry makes out of date.

Every change to attendance or pay writes an ``AuditLog`` entry (project rule 6), so the
audit log is the one place that sees them all. For each entry this module answers: which
sites, from which date to which date, must be rebuilt?

- ``IGNORED``: actions that change no figure (sign-ins, messages, tokens, documents…),
  and the entries a recomputation writes about its own results (``payroll.period.status``,
  ``payroll.adjustment.*``…): marking them would make every rebuild ask for another.
- ``COMPANY``: actions that change the rules of every site (company settings, pay rules,
  deduction rules, task rates, an import): every site is out of date, every open period.
- Every other action is placed by its *target*: a day by its site and date, a pay period
  by its dates, a worker by the sites they work on, from the date in the entry if there is
  one. A target of a model this module does not know marks the whole company (safe).

``tests/test_precompute_marks.py`` fails when an action in the code is in none of these.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass

from django.db.models import Q

from apps.payroll.models import PayLine, PayPeriod
from apps.worksites.models import SiteAssignment

# --- what each action means -------------------------------------------------------------

IGNORED = frozenset(
    {
        # Results of a recomputation (written by the refresh itself).
        "payroll.period.refresh",
        "payroll.period.status",
        "payroll.adjustment.apply",
        "payroll.adjustment.create",
        "payroll.adjustment.update",
        "payroll.adjustment.cancel",
        "payroll.bonus.apply",
        "payroll.line.closed_unpaid",
        # Nothing that a figure is computed from.
        "core.company_create",
        "accounts.active_change",
        "accounts.device_revoke",
        "accounts.login_locked",
        "accounts.pin_locked",
        "accounts.pin_reset",
        "accounts.pin_set",
        "accounts.role_change",
        "accounts.sites_change",
        "accounts.user_invite",
        "agents.token.create",
        "agents.token.revoke",
        "checkin.badge.issue",
        "checkin.badge.revoke",
        "checkin.geofence.clear",
        "checkin.geofence.set",
        "checkin.kiosk.disable",
        "checkin.kiosk.enable",
        "messaging.message.resend",
        "messaging.preference.update",
        "messaging.settings.update",
        "payouts.account.create",
        "payouts.account.update",
        "payouts.layout.create",
        "payouts.layout.update",
        "portal.access_given",
        "portal.access_revoked",
        "portal.pin_changed",
        "portal.pin_locked",
        "portal.pin_reset",
        "workers.create",  # a new worker has no attendance or pay yet
        "workers.document_add",
        "workers.document_delete",
        "worksites.site_create",
    }
)

# The system's own recomputation (``payroll.auto.…``): never an input. Presence tracking
# (``presence.…``: badges, zones, gateways, consent) never changes attendance or pay; an
# accepted roll-call suggestion is audited as an attendance change.
IGNORED_PREFIXES = ("payroll.auto.", "presence.")

COMPANY = frozenset(
    {
        "settings.update",
        "payroll.rules.update",
        "payroll.deduction_rule.create",
        "payroll.deduction_rule.update",
        "payroll.task_type.create",
        "payroll.task_type.update",
        "workers.import",
    }
)

# Placed by their target (see ``_ranges_of``). Listed so that the test can tell a new
# action from a forgotten one.
BY_TARGET = frozenset(
    {
        "attendance.day.approve",
        "attendance.day.create",
        "attendance.day.lock",
        "attendance.day.reject",
        "attendance.day.submit",
        "attendance.record.correct",
        "attendance.records.upsert",
        "payroll.advance.create",
        "payroll.bonus.create",
        "payroll.bonus.delete",
        "payroll.cash.issue",
        "payroll.payment.create",
        "payroll.payment.undo",
        "payroll.period.lock",
        "payroll.piece.create",
        "payroll.piece.delete",
        "payroll.site_rules.update",
        "payroll.stoppage.clear",
        "payroll.stoppage.set",
        "payouts.batch.create",
        "payouts.batch.export",
        "payouts.batch.import",
        "payouts.batch.status",
        "payouts.line.confirm",
        "payouts.line.fail",
        "payouts.line.remove",
        "workers.rate_change",
        "workers.update",
        "worksites.assignment_close",
        "worksites.assignment_create",
        "worksites.assignment_remove",
        "worksites.settings_update",
        "worksites.site_update",
    }
)

# Edits made in the Django admin (``apps.core.admin.AuditedAdmin``): by their target.
ADMIN_PREFIX = "admin."


@dataclass(frozen=True)
class Range:
    """``site_id`` is out of date from ``start`` (``None``: every open period) to ``end``."""

    site_id: int
    start: datetime.date | None
    end: datetime.date | None  # None: up to today


@dataclass(frozen=True)
class Company:
    """Every site of the company is out of date (every open period)."""

    company_id: int


def is_ignored(action: str) -> bool:
    return action in IGNORED or action.startswith(IGNORED_PREFIXES)


def ranges_for(entry) -> list[Range] | Company | None:
    """What ``entry`` (an ``AuditLog``) makes out of date; ``None`` for nothing."""
    action = entry.action
    if is_ignored(action):
        return None
    if action in COMPANY or entry.company_id is None:
        return Company(entry.company_id) if entry.company_id else None
    target = _target(entry)
    if target is None:
        return Company(entry.company_id)
    ranges = _ranges_of(target, entry)
    return Company(entry.company_id) if ranges is None else ranges


def _target(entry):
    if entry.content_type_id is None or not entry.object_id:
        return None
    model = entry.content_type.model_class()
    if model is None:
        return None
    return model._default_manager.filter(pk=entry.object_id).first()


def _date(value) -> datetime.date | None:
    if isinstance(value, datetime.date):
        return value
    try:
        return datetime.date.fromisoformat(str(value)[:10]) if value else None
    except ValueError:
        return None


def _ranges_of(target, entry) -> list[Range] | None:
    """The ranges of a known target; ``None`` for a model this module does not know."""
    label = target._meta.label_lower
    placer = _PLACERS.get(label)
    return placer(target, entry) if placer else None


# --- one placer per target model --------------------------------------------------------


def _day(day, _entry):
    return [Range(day.site_id, day.date, day.date)]


def _record(record, entry):
    return _day(record.day, entry)


def _correction(correction, entry):
    return _day(correction.record.day, entry)


def _period(period, _entry):
    return [Range(period.site_id, period.start_date, period.end_date)]


def _line(line, entry):
    return _period(line.period, entry)


def _payment(payment, entry):
    return _period(payment.pay_line.period, entry)


def _site_day(obj, _entry):  # bonus, piece work, stoppage day, cash issue
    return [Range(obj.site_id, obj.date, obj.date)]


def _site(site, _entry):
    return [Range(site.pk, None, None)]


def _site_rules(rules, _entry):
    return [Range(rules.site_id, None, None)]


def _assignment(assignment, _entry):
    return [Range(assignment.site_id, assignment.start_date, None)]


def worker_sites(worker_id: int, since: datetime.date | None) -> set[int]:
    """The sites a worker's pay may change on from ``since``: where they are assigned
    then or later, and where they have a pay line in an open period."""
    assignments = SiteAssignment.objects.filter(worker_id=worker_id)
    if since is not None:
        assignments = assignments.filter(Q(end_date__isnull=True) | Q(end_date__gte=since))
    sites = set(assignments.values_list("site_id", flat=True))
    sites |= set(
        PayLine.objects.filter(worker_id=worker_id)
        .exclude(period__status=PayPeriod.Status.LOCKED)
        .values_list("period__site_id", flat=True)
    )
    return sites


def _worker(worker, entry):
    # A rate change says from when (``effective_from``); a profile change (pay frequency,
    # payment method, active) applies to every open period.
    since = _date((entry.after or {}).get("effective_from")) if entry.after else None
    return [Range(site_id, since, None) for site_id in sorted(worker_sites(worker.pk, since))]


def _rate(rate, _entry):
    since = rate.effective_from
    return [Range(site_id, since, None) for site_id in sorted(worker_sites(rate.worker_id, since))]


def _advance(advance, _entry):
    since = advance.given_on
    sites = worker_sites(advance.worker_id, since)
    return [Range(site_id, since, None) for site_id in sorted(sites)]


def _payout_batch(batch, _entry):
    return [Range(batch.site_id, batch.pay_date, batch.pay_date)] if batch.site_id else None


def _payout_line(line, entry):
    return _line(line.pay_line, entry)


_PLACERS = {
    "attendance.attendanceday": _day,
    "attendance.attendancerecord": _record,
    "attendance.attendancecorrection": _correction,
    "payroll.payperiod": _period,
    "payroll.payline": _line,
    "payroll.payment": _payment,
    "payroll.bonus": _site_day,
    "payroll.pieceworkentry": _site_day,
    "payroll.stoppageday": _site_day,
    "payroll.cashissue": _site_day,
    "payroll.advance": _advance,
    "payroll.sitepayrules": _site_rules,
    "payouts.payoutbatch": _payout_batch,
    "payouts.payoutline": _payout_line,
    "worksites.site": _site,
    "worksites.siteassignment": _assignment,
    "workers.worker": _worker,
    "workers.payrate": _rate,
}
