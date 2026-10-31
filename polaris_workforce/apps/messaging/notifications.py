"""Notification rules, evaluated per company by ``manage.py send_notifications``.

Each rule reuses the screens' own logic (the dashboard's "Needs attention" items, the
Approvals list, payroll's advance check) for the people it is about, so a text never says
something the office screens don't. Each person gets at most one text per rule, subject
and day (the message ``dedupe_key``), so the command can run as often as cron likes.

- Missing roll call: once a site's ``roll_call_due`` has passed on a working day with no
  submitted roll call → its foremen and its site manager (one text per site).
- Days waiting for approval: submitted for more than ``approval_reminder_hours`` →
  everyone who may approve them (site managers on their sites, admins).
- Cash sheets not closed after pay day: pay periods ended before today and not paid out →
  everyone who may close them (site managers on their sites, accountants, admins).
- Advance near its limit (off by default): a worker owes ``advance_limit_warn_percent``
  of their advance limit or more → admins and the manager of the worker's site.
"""

from __future__ import annotations

import datetime

from apps.accounts.models import User
from apps.accounts.permissions import (
    APPROVE_DAYS,
    GIVE_ADVANCES,
    LOCK_PERIODS,
    can,
    is_admin,
)
from apps.attendance.selectors import days_waiting_for_approval
from apps.core.dates import company_now
from apps.core.display import format_amount
from apps.core.models import CompanySettings
from apps.dashboard.services import today_overview
from apps.payroll import services as payroll_services
from apps.workers.models import Worker

from .models import Kind, MessagingSettings
from .services import company_id_for, queue_message

Role = User.Role
SHEETS_IN_TEXT = 3


class _Run:
    """One evaluation for one company at one moment."""

    def __init__(self, company_id: int, now: datetime.datetime):
        self.company_id = company_id
        self.company = CompanySettings.load(company_id)
        self.messaging = MessagingSettings.load(company_id)
        self.now = now
        self.today = now.date()
        # The company's people with a phone (outside a request there is no signed-in
        # user to scope from: the company itself is the scope).
        self.users = list(
            User.objects.filter(company_id=company_id, is_active=True)
            .exclude(phone="")
            .order_by("pk")
        )
        self._overviews: dict[int, object] = {}

    def overview(self, user):
        """The user's "Today" dashboard (cached for the run)."""
        if user.pk not in self._overviews:
            self._overviews[user.pk] = today_overview(user, company=self.company, now=self.now)
        return self._overviews[user.pk]

    def queue(self, kind, key, user, context) -> int:
        message = queue_message(
            kind=kind,
            dedupe_key=f"{kind}:{key}:u{user.pk}:{self.today.isoformat()}",
            context=context,
            user=user,
            company=self.company_id,
        )
        return 1 if message is not None else 0

    # --- rules -------------------------------------------------------------------------

    def missing_roll_calls(self) -> int:
        queued = 0
        for user in self.users:
            if user.is_superuser or user.role not in (Role.FOREMAN, Role.SITE_MANAGER):
                continue
            for alert in self.overview(user).alerts:
                if alert.kind != "missing_roll_call":
                    continue
                queued += self.queue(
                    Kind.MISSING_ROLL_CALL,
                    f"site{alert.site_id}",
                    user,
                    {"site": alert.site_name, "due": alert.due, "date": self.today},
                )
        return queued

    def awaiting_approval(self) -> int:
        cutoff = self.now - datetime.timedelta(hours=self.messaging.approval_reminder_hours)
        queued = 0
        for user in self.users:
            if not can(user, APPROVE_DAYS):
                continue
            days = list(days_waiting_for_approval(user).filter(submitted_at__lte=cutoff))
            if not days:
                continue
            oldest = days[0]
            queued += self.queue(
                Kind.AWAITING_APPROVAL,
                "days",
                user,
                {"count": len(days), "oldest_date": oldest.date, "oldest_site": oldest.site.name},
            )
        return queued

    def unclosed_cash_sheets(self) -> int:
        queued = 0
        for user in self.users:
            if not can(user, LOCK_PERIODS):
                continue
            sheets = [
                {"site": alert.site_name, "date": alert.date}
                for alert in self.overview(user).alerts
                if alert.kind == "unclosed_period"
            ]
            if not sheets:
                continue
            queued += self.queue(
                Kind.UNCLOSED_CASH_SHEET,
                "sheets",
                user,
                {
                    "count": len(sheets),
                    "sheets": sheets[:SHEETS_IN_TEXT],
                    "more": max(0, len(sheets) - SHEETS_IN_TEXT),
                },
            )
        return queued

    def advances_near_limit(self) -> int:
        warn = self.messaging.advance_limit_warn_percent
        workers = (
            Worker.objects.filter(
                company_id=self.company_id, is_active=True, advances__balance__gt=0
            )
            .distinct()
            .order_by("pk")
        )
        managers = [user for user in self.users if can(user, GIVE_ADVANCES)]
        queued = 0
        for worker in workers:
            check = payroll_services.advance_check(
                worker, 0, 0, on=self.today, company=self.company
            )
            if check.limit_amount <= 0 or check.owed * 100 < check.limit_amount * warn:
                continue
            site = payroll_services.current_site(worker, self.today)
            context = {
                "worker": worker.full_name,
                "owed": format_amount(check.owed, self.company),
                "limit": format_amount(check.limit_amount, self.company),
                "percent": check.owed * 100 // check.limit_amount,
            }
            for user in managers:
                # Without a current site only admins may give the worker an advance.
                allowed = is_admin(user) if site is None else can(user, GIVE_ADVANCES, site)
                if not allowed:
                    continue
                queued += self.queue(Kind.ADVANCE_LIMIT, f"w{worker.pk}", user, context)
        return queued


RULES = {
    Kind.MISSING_ROLL_CALL: _Run.missing_roll_calls,
    Kind.AWAITING_APPROVAL: _Run.awaiting_approval,
    Kind.UNCLOSED_CASH_SHEET: _Run.unclosed_cash_sheets,
    Kind.ADVANCE_LIMIT: _Run.advances_near_limit,
}


def run_notifications(company=None, *, now: datetime.datetime | None = None) -> dict[str, int]:
    """Queue the texts every rule asks for in ``company`` (else the current company).

    Returns ``{rule: messages queued}`` for the rules that are on. Idempotent per day.
    """
    company_id = company_id_for(company)
    messaging = MessagingSettings.load(company_id)
    if not messaging.enabled:
        return {}
    now = now or company_now(CompanySettings.load(company_id))
    run = _Run(company_id, now)
    return {kind: rule(run) for kind, rule in RULES.items() if messaging.is_on(kind)}
