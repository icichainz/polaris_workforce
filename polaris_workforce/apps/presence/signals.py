"""Changes outside this app that alter a site's tracker config queue a push of it.

Every such change writes an audit entry (tracking settings of the company, a site or a
worker, a site's geofence, a new site, admin edits), so the audit log is the one place
that sees them all, as for ``apps.precompute``. The push runs once the change commits
(``services.queue_site_push``): a change that rolls back pushes nothing.
"""

from django.db.models.signals import post_save
from django.dispatch import receiver

from apps.core.models import AuditLog

from . import services


@receiver(post_save, sender=AuditLog, dispatch_uid="presence.push_from_audit")
def push_from_audit(sender, instance: AuditLog, created: bool, raw: bool = False, **kwargs):
    if created and not raw:
        services.queue_site_push(services.sites_for_audit_entry(instance))
