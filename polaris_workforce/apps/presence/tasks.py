"""Celery tasks of presence tracking (``config/celery.py``).

- ``push_site_config``: queued by ``services.queue_site_push`` once a change commits.
  Retried with a backoff while the tracker is down or failing (no answer, 5xx); a refusal
  (4xx) is logged and not retried (the same config would be refused again). One push per
  site at a time (``site_lock``): the config is built when the push runs, so the one
  sent last is the one built last. A push that finds the lock taken is queued again a
  few seconds later.
- ``resync_site_configs``: the periodic run (beat, every 10 minutes): every tracked site
  again, for a tracker that restarted (it keeps configs in memory) or a lost push. One run
  at a time; a site whose push is running is left to that push.
- ``reconcile_sessions``: the nightly run (beat, 03:15 in ``CELERY_TIMEZONE``): past days'
  sessions corrected with the tracker's replay (``reconcile``). Each company's days are
  worked out in its own time zone, so one fixed time serves every company. One run at a
  time.

Every task has a soft and a hard time limit (enforced by the prefork pool). Locks are
cache entries (``cache.add``) that expire with the hard limit, so a killed worker never
leaves one behind for long.
"""

from __future__ import annotations

import contextlib
import logging
import uuid

from celery import shared_task
from celery.exceptions import SoftTimeLimitExceeded
from django.core.cache import cache
from django.db import OperationalError

from apps.core.tenancy import use_company
from apps.worksites.models import Site

from . import reconcile, services

logger = logging.getLogger(__name__)

PUSH_SOFT_TIME_LIMIT = 45
PUSH_TIME_LIMIT = 60
RESYNC_SOFT_TIME_LIMIT = 8 * 60  # under the 10-minute beat interval
RESYNC_TIME_LIMIT = 9 * 60
RECONCILE_SOFT_TIME_LIMIT = 55 * 60
RECONCILE_TIME_LIMIT = 60 * 60
# Seconds before a push that found its site's lock taken runs again.
LOCK_RETRY_SECONDS = 5

RESYNC_LOCK = "presence:resync-lock"
RECONCILE_LOCK = "presence:reconcile-lock"

# Worth trying again later: no answer, a failing tracker, a busy database.
RETRYABLE = (services.TrackerUnreachable, services.TrackerServerError, OperationalError)


@contextlib.contextmanager
def held(key: str, timeout: int):
    """Take the lock ``key`` for at most ``timeout`` seconds; yields whether it was taken.
    Released on exit only by its holder (an expired lock may belong to someone else)."""
    token = uuid.uuid4().hex
    taken = cache.add(key, token, timeout)
    try:
        yield taken
    finally:
        if taken and cache.get(key) == token:
            cache.delete(key)


def site_lock(site_id: int):
    """The lock of one site's config push (task and resync alike)."""
    return held(f"presence:push-lock:{int(site_id)}", PUSH_TIME_LIMIT)


@shared_task(
    name="presence.push_site_config",
    autoretry_for=RETRYABLE,
    retry_backoff=True,
    max_retries=5,
    soft_time_limit=PUSH_SOFT_TIME_LIMIT,
    time_limit=PUSH_TIME_LIMIT,
)
def push_site_config_task(site_id: int) -> None:
    site = Site.objects.select_related("company").filter(pk=site_id).first()
    if site is None:
        return
    with site_lock(site_id) as taken:
        if not taken:
            try:
                push_site_config_task.apply_async((site_id,), countdown=LOCK_RETRY_SECONDS)
            except Exception:  # broker down: the periodic resync pushes the site anyway
                logger.warning("Presence: could not queue site %s again", site_id, exc_info=True)
            return
        with use_company(site.company):
            try:
                services.push_site_config(site)
            except RETRYABLE:
                raise
            except services.TrackerPushError:
                logger.warning(
                    "Presence: the tracker refused the config of site %s; not retried",
                    site_id,
                    exc_info=True,
                )


@shared_task(
    name="presence.resync_site_configs",
    soft_time_limit=RESYNC_SOFT_TIME_LIMIT,
    time_limit=RESYNC_TIME_LIMIT,
)
def resync_site_configs() -> int:
    """Push every site to resync; stop at the first unreachable tracker. Returns the
    number of configs the tracker took."""
    pushed = 0
    with held(RESYNC_LOCK, RESYNC_TIME_LIMIT) as taken:
        if not taken:
            logger.info("Presence: a resync is already running; this one is skipped")
            return 0
        try:
            for site in services.sites_to_resync():
                with site_lock(site.pk) as free, use_company(site.company):
                    if not free:
                        continue  # its push is running and builds the latest config
                    try:
                        pushed += services.push_site_config(site)
                    except services.TrackerUnreachable:
                        logger.warning(
                            "Presence: resync stopped at site %s", site.pk, exc_info=True
                        )
                        break
                    except services.TrackerPushError:
                        logger.warning(
                            "Presence: resync of site %s refused", site.pk, exc_info=True
                        )
        except SoftTimeLimitExceeded:
            logger.warning("Presence: resync stopped at its time limit after %s push(es)", pushed)
    return pushed


@shared_task(
    name="presence.reconcile_sessions",
    soft_time_limit=RECONCILE_SOFT_TIME_LIMIT,
    time_limit=RECONCILE_TIME_LIMIT,
)
def reconcile_sessions() -> int:
    """``manage.py reconcile_presence`` for every tracked site. Never raises for a site or
    the tracker (failures are recorded and tried again the next night). Returns the
    number of site days reconciled."""
    with held(RECONCILE_LOCK, RECONCILE_TIME_LIMIT) as taken:
        if not taken:
            logger.info("Presence: a reconciliation is already running; this one is skipped")
            return 0
        try:
            return reconcile.run().reconciled
        except SoftTimeLimitExceeded:
            # Each site's day commits on its own: what is left is caught up tomorrow.
            logger.warning("Presence: reconciliation stopped at its time limit")
            return 0
