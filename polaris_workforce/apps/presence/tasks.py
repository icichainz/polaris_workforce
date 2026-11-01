"""Celery tasks of presence tracking (``config/celery.py``).

- ``push_site_config``: queued by ``services.queue_site_push`` once a change commits;
  retried with a backoff while the tracker is down.
- ``resync_site_configs``: the periodic run (beat, every 10 minutes): every tracked site
  again, for a tracker that restarted (it keeps configs in memory) or a lost push.
"""

from __future__ import annotations

import logging

from celery import shared_task
from django.db import OperationalError

from apps.core.tenancy import use_company
from apps.worksites.models import Site

from . import services

logger = logging.getLogger(__name__)


@shared_task(
    name="presence.push_site_config",
    autoretry_for=(services.TrackerPushError, OperationalError),
    retry_backoff=True,
    max_retries=5,
)
def push_site_config_task(site_id: int) -> None:
    site = Site.objects.select_related("company").filter(pk=site_id).first()
    if site is None:
        return
    with use_company(site.company):
        services.push_site_config(site)


@shared_task(name="presence.resync_site_configs")
def resync_site_configs() -> int:
    """Push every site to resync; stop at the first unreachable tracker. Returns the
    number of configs the tracker took."""
    pushed = 0
    for site in services.sites_to_resync():
        with use_company(site.company):
            try:
                pushed += services.push_site_config(site)
            except services.TrackerUnreachable:
                logger.warning("Presence: resync stopped at site %s", site.pk, exc_info=True)
                break
            except services.TrackerPushError:
                logger.warning("Presence: resync of site %s refused", site.pk, exc_info=True)
    return pushed
