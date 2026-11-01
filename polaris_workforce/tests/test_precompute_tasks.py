"""Rebuilds in the background (Celery): what a change queues, and what the worker does.

The tests run without a broker; those about queueing set ``CELERY_BROKER_URL`` and catch
what ``rebuild_site.apply_async`` would send.
"""

import datetime
import logging

import pytest
from django.utils import timezone
from kombu.exceptions import OperationalError

from apps.core.models import AuditLog
from apps.precompute import services, tasks
from apps.precompute.models import Freshness, Stale
from config import celery_app
from tests.test_payroll_services import (  # noqa: F401
    MON,
    TUE,
    company,
    hire,
    roll_call,
    setup_site,
)


@pytest.fixture
def broker(settings, monkeypatch):
    settings.CELERY_BROKER_URL = "memory://"
    monkeypatch.setattr(services, "_broker_down_until", 0.0)  # no backoff from another test


@pytest.fixture
def sent(monkeypatch):
    """The site ids ``rebuild_site`` is queued for, in order."""
    calls = []

    def apply_async(args, countdown=None):
        calls.append(args[0])

    monkeypatch.setattr(tasks.rebuild_site, "apply_async", apply_async)
    return calls


def test_one_rebuild_for_many_changes_to_a_site(
    company,  # noqa: F811
    broker,
    sent,
    django_capture_on_commit_callbacks,
):
    site, *_ = setup_site()
    other, *_ = setup_site()
    with django_capture_on_commit_callbacks(execute=True):
        services.mark(site.pk, MON, MON)
        services.mark(site.pk, TUE, TUE)
        services.mark(other.pk, MON, MON)
        assert sent == []  # not before the change commits

    assert sent == [site.pk, other.pk]
    assert Stale.objects.get(site=site).queued_at is not None


def test_a_mark_long_after_a_lost_rebuild_queues_again(
    company,  # noqa: F811
    broker,
    sent,
    django_capture_on_commit_callbacks,
    settings,
):
    site, *_ = setup_site()
    with django_capture_on_commit_callbacks(execute=True):
        services.mark(site.pk, MON, MON)
    long_ago = timezone.now() - datetime.timedelta(
        minutes=settings.PRECOMPUTE_REQUEUE_AFTER_MINUTES + 1
    )
    Stale.objects.filter(site=site).update(queued_at=long_ago)

    with django_capture_on_commit_callbacks(execute=True):
        services.mark(site.pk, TUE, TUE)

    assert sent == [site.pk, site.pk]


def test_without_a_broker_nothing_is_queued(
    company,  # noqa: F811
    sent,
    django_capture_on_commit_callbacks,
):
    site, *_ = setup_site()
    with django_capture_on_commit_callbacks(execute=True):
        services.mark(site.pk, MON, MON)
    assert sent == []
    assert Stale.objects.get(site=site).queued_at is None


def test_a_broker_that_is_down_never_fails_the_change(
    company,  # noqa: F811
    broker,
    monkeypatch,
    django_capture_on_commit_callbacks,
    caplog,
):
    def unreachable(*args, **kwargs):
        raise OperationalError("Error 111 connecting to redis:6379. Connection refused.")

    monkeypatch.setattr(tasks.rebuild_site, "apply_async", unreachable)
    site, *_ = setup_site()
    with caplog.at_level(logging.WARNING, logger="polaris.precompute"):
        with django_capture_on_commit_callbacks(execute=True):
            AuditLog.record(actor=None, action="worksites.settings_update", target=site)

    assert Stale.objects.filter(site=site).exists()  # the periodic run will rebuild it
    assert "could not queue the rebuild" in caplog.text


def test_the_worker_rebuilds_the_site(company, monkeypatch):  # noqa: F811
    site, *_ = setup_site()
    services.mark(site.pk, MON, MON)
    rebuilt = []
    monkeypatch.setattr(
        services, "_STEPS", [(0, lambda site, start, end, **_: rebuilt.append((start, end)))]
    )

    tasks.rebuild_site(site.pk)

    assert rebuilt
    assert not Stale.objects.filter(site=site).exists()
    assert Freshness.objects.filter(site=site).exists()


def test_a_change_during_the_rebuild_is_queued_again(
    company,  # noqa: F811
    broker,
    sent,
    monkeypatch,
    django_capture_on_commit_callbacks,
):
    site, *_ = setup_site()
    Stale.objects.create(site=site, start=MON, end=MON, queued_at=timezone.now())

    def step(site, start, end, **_options):
        services.mark(site.pk, TUE, TUE)  # queued already: no second task from the mark

    monkeypatch.setattr(services, "_STEPS", [(0, step)])
    with django_capture_on_commit_callbacks(execute=True):
        tasks.rebuild_site(site.pk)

    assert Stale.objects.filter(site=site).exists()
    assert sent == [site.pk]  # by the task, for the mark it kept


def test_a_rebuild_of_a_deleted_site_does_nothing(company):  # noqa: F811
    tasks.rebuild_site(999_999)


def test_the_periodic_task_runs_the_command(company, monkeypatch):  # noqa: F811
    calls = []
    monkeypatch.setattr(tasks, "call_command", lambda *args, **kwargs: calls.append(args))
    tasks.refresh()
    tasks.refresh(sweep=True)
    assert calls == [("refresh_precomputed",), ("refresh_precomputed", "--sweep")]


def test_beat_runs_every_periodic_job():
    from apps.messaging import tasks as messaging_tasks  # noqa: F401 (registers its tasks)
    from apps.presence import tasks as presence_tasks  # noqa: F401 (registers its tasks)

    schedule = celery_app.conf.beat_schedule
    assert {entry["task"] for entry in schedule.values()} == {
        "messaging.send_messages",
        "messaging.send_notifications",
        "precompute.refresh",
        "presence.resync_site_configs",
        "presence.reconcile_sessions",
    }
    assert all(entry["task"] in celery_app.tasks for entry in schedule.values())
    assert schedule["sweep-precomputed"]["kwargs"] == {"sweep": True}
