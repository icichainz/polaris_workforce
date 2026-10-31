"""Evaluate the notification rules of every active company, then send the outbox.

Cron-friendly and idempotent per day (each person gets one text per rule, subject and
day), e.g. every 15 minutes::

    */15 * * * * cd /srv/polaris_workforce && .venv/bin/python manage.py send_notifications
"""

from django.core.management.base import BaseCommand, CommandError

from apps.core.models import Company
from apps.core.tenancy import use_company
from apps.messaging.notifications import run_notifications
from apps.messaging.services import send_pending


class Command(BaseCommand):
    help = "Queue the notification texts of every active company, then send due messages."

    def add_arguments(self, parser):
        parser.add_argument(
            "--company", action="append", default=[], help="Only this company (its short name)."
        )
        parser.add_argument(
            "--no-send", action="store_true", help="Queue only; send with send_messages later."
        )

    def handle(self, *args, **options):
        companies = Company.objects.filter(is_active=True).order_by("pk")
        if options["company"]:
            companies = companies.filter(slug__in=options["company"])
            if not companies.exists():
                raise CommandError("No active company with that short name.")
        for company in companies:
            with use_company(company):
                queued = run_notifications(company)
                line = ", ".join(f"{rule} {n}" for rule, n in queued.items()) or "off"
                if not options["no_send"]:
                    summary = send_pending(company)
                    line += (
                        f" · sent {summary.sent}, retrying {summary.retrying}, "
                        f"failed {summary.failed}, skipped {summary.skipped}, "
                        f"quiet hours {summary.waiting}"
                    )
            self.stdout.write(f"{company.slug}: {line}")
