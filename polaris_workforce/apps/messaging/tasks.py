"""Celery tasks for the periodic messaging jobs (beat, ``CELERY_BEAT_SCHEDULE``).

They run the management commands, which stay the one place these jobs are written, and
which can still be run by hand.
"""

from celery import shared_task
from django.core.management import call_command


@shared_task(name="messaging.send_messages")
def send_messages() -> None:
    """Flush the outbox: payslip texts, portal PINs and retries (every 2 minutes)."""
    call_command("send_messages", verbosity=0)


@shared_task(name="messaging.send_notifications")
def send_notifications() -> None:
    """Evaluate the notification rules of every company, then send (every 15 minutes)."""
    call_command("send_notifications", verbosity=0)
