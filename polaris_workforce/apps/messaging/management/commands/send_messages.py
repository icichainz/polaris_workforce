"""Send the due messages of every active company (queued texts and retries).

Run it often so payslip texts go out soon after the payment, e.g. every 2 minutes::

    */2 * * * * cd /srv/polaris_workforce && .venv/bin/python manage.py send_messages
"""

from django.core.management.base import BaseCommand

from apps.core.models import Company
from apps.core.tenancy import use_company
from apps.messaging.services import send_pending


class Command(BaseCommand):
    help = "Send the queued messages of every active company (with retries)."

    def handle(self, *args, **options):
        for company in Company.objects.filter(is_active=True).order_by("pk"):
            with use_company(company):
                summary = send_pending(company)
            self.stdout.write(
                f"{company.slug}: sent {summary.sent}, retrying {summary.retrying}, "
                f"failed {summary.failed}, skipped {summary.skipped}, "
                f"quiet hours {summary.waiting}"
            )
