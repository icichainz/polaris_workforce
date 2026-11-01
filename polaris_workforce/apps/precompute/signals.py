"""Every audit entry marks what it makes out of date (``services.mark_from_audit``).

The entry is written inside the change's transaction, so the mark is too: a change that
rolls back leaves no mark.
"""

from django.db.models.signals import post_save
from django.dispatch import receiver

from apps.core.models import AuditLog

from . import services


@receiver(post_save, sender=AuditLog, dispatch_uid="precompute.mark_from_audit")
def mark_from_audit(sender, instance: AuditLog, created: bool, raw: bool = False, **kwargs):
    if created and not raw:
        services.mark_from_audit(instance)
