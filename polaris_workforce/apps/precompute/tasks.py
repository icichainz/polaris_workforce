"""Celery tasks of the precompute service (``config/celery.py``).

- ``rebuild_site``: queued by ``services.mark`` once a change commits; rebuilds one site.
- ``refresh``: the periodic run (beat, every 5 minutes) and the nightly sweep, the same
  work as ``manage.py refresh_precomputed``: the fallback for rebuilds that were never
  queued or were lost.
"""

from __future__ import annotations

from celery import shared_task
from django.core.management import call_command
from django.db import OperationalError
from django.utils import timezone

from apps.core.tenancy import use_company
from apps.worksites.models import Site

from . import services
from .models import Stale


@shared_task(
    name="precompute.rebuild_site",
    autoretry_for=(OperationalError,),  # SQLite busy past its timeout: try again later
    retry_backoff=True,
    max_retries=5,
)
def rebuild_site(site_id: int) -> None:
    """Rebuild what is out of date on one site. A mark made while it ran is kept by the
    rebuild (``refresh_site``): it is queued again."""
    site = Site.objects.select_related("company").filter(pk=site_id).first()
    if site is None:
        return
    with use_company(site.company):
        services.refresh_site(site)
    if Stale.objects.filter(site_id=site_id).update(queued_at=timezone.now()):
        services.queue_rebuild(site_id)


@shared_task(name="precompute.refresh")
def refresh(sweep: bool = False) -> None:
    """``manage.py refresh_precomputed [--sweep]`` for every active company."""
    call_command("refresh_precomputed", *(["--sweep"] if sweep else []), verbosity=0)
