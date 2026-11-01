"""Presence tasks: one config push per site at a time, time limits, no retry of a refusal.

What must hold:
- Two pushes of one site never run at once: the config sent last is built last. A push
  that finds its site's lock taken is queued again a few seconds later; the resync leaves
  a site whose push is running to that push. Locks are released afterwards.
- Every task has a soft and a hard time limit; one resync (and one reconciliation) at a
  time; the resync stops at its soft limit and releases its lock.
- A config the tracker refuses (4xx) is not retried; no answer and 5xx are.
"""

import io
import urllib.error

import pytest
from celery.exceptions import SoftTimeLimitExceeded

from apps.presence import services, tasks
from tests.factories import SiteFactory
from tests.test_presence_config import TRACKER, fake_tracker, tracked  # noqa: F401

pytestmark = pytest.mark.django_db


@pytest.fixture
def requeued(monkeypatch):
    """What ``push_site_config_task`` queues again: ``(site_id, countdown)``."""
    calls = []
    monkeypatch.setattr(
        tasks.push_site_config_task,
        "apply_async",
        lambda args, countdown=None: calls.append((args[0], countdown)),
    )
    return calls


def http_error(status: int) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(TRACKER, status, "Error", {}, io.BytesIO(b"{}"))


# --- (a) one push per site at a time -------------------------------------------------------------


def test_a_push_waits_for_the_running_push_of_its_site(tracked, fake_tracker, requeued):  # noqa: F811
    site = tracked["site"]
    with tasks.site_lock(site.pk) as taken:
        assert taken
        tasks.push_site_config_task(site.pk)

    assert fake_tracker.requests == []
    assert requeued == [(site.pk, tasks.LOCK_RETRY_SECONDS)]


def test_the_lock_is_per_site_and_released_after_the_push(tracked, fake_tracker, requeued):  # noqa: F811
    site = tracked["site"]
    other = SiteFactory(name="South")
    with tasks.site_lock(other.pk):
        tasks.push_site_config_task(site.pk)  # another site's push does not block it

    assert fake_tracker.sites == [site.pk]
    assert requeued == []
    with tasks.site_lock(site.pk) as taken:
        assert taken


def test_the_lock_is_released_when_the_push_fails(tracked, fake_tracker):  # noqa: F811
    fake_tracker.error = urllib.error.URLError("down")

    with pytest.raises(services.TrackerUnreachable):
        tasks.push_site_config_task(tracked["site"].pk)

    with tasks.site_lock(tracked["site"].pk) as taken:
        assert taken


def test_a_lock_is_released_only_by_its_holder(tracked):  # noqa: F811
    with tasks.held("presence:test-lock", 60) as first:
        with tasks.held("presence:test-lock", 60) as second:
            assert first and not second
        # the second holder's exit did not release the first one's lock
        with tasks.held("presence:test-lock", 60) as third:
            assert not third


def test_the_resync_leaves_a_site_to_its_running_push(tracked, fake_tracker):  # noqa: F811
    site = tracked["site"]
    south = SiteFactory(name="South")
    with tasks.site_lock(site.pk):
        assert tasks.resync_site_configs() == 1

    assert fake_tracker.sites == [south.pk]


# --- (b) time limits and overlap -----------------------------------------------------------------


@pytest.mark.parametrize(
    ("task", "soft", "hard"),
    [
        (tasks.push_site_config_task, tasks.PUSH_SOFT_TIME_LIMIT, tasks.PUSH_TIME_LIMIT),
        (tasks.resync_site_configs, tasks.RESYNC_SOFT_TIME_LIMIT, tasks.RESYNC_TIME_LIMIT),
        (tasks.reconcile_sessions, tasks.RECONCILE_SOFT_TIME_LIMIT, tasks.RECONCILE_TIME_LIMIT),
    ],
)
def test_every_task_has_time_limits(task, soft, hard):
    assert task.soft_time_limit == soft
    assert task.time_limit == hard
    assert 0 < soft < hard


def test_the_resync_finishes_before_the_next_one_is_due():
    assert tasks.RESYNC_TIME_LIMIT < 10 * 60  # beat: every 10 minutes


def test_two_resyncs_never_run_at_once(tracked, fake_tracker):  # noqa: F811
    with tasks.held(tasks.RESYNC_LOCK, 60) as taken:
        assert taken
        assert tasks.resync_site_configs() == 0

    assert fake_tracker.requests == []
    assert tasks.resync_site_configs() == 1  # released: the next one runs


def test_the_resync_stops_at_its_time_limit(tracked, fake_tracker, monkeypatch):  # noqa: F811
    SiteFactory(name="South")
    calls = []

    def push(site):
        calls.append(site.pk)
        if len(calls) == 2:
            raise SoftTimeLimitExceeded()
        return True

    monkeypatch.setattr(services, "push_site_config", push)

    assert tasks.resync_site_configs() == 1

    assert len(calls) == 2
    with tasks.held(tasks.RESYNC_LOCK, 60) as taken:
        assert taken
    with tasks.site_lock(calls[-1]) as taken:
        assert taken


# --- (c) retries ---------------------------------------------------------------------------------


@pytest.mark.parametrize("status", [400, 401, 403, 404, 422])
def test_a_refused_config_is_not_retried(tracked, fake_tracker, status, caplog):  # noqa: F811
    fake_tracker.error = http_error(status)

    tasks.push_site_config_task(tracked["site"].pk)  # no exception: Celery does not retry

    assert len(fake_tracker.requests) == 1
    assert "not retried" in caplog.text


@pytest.mark.parametrize("status", [500, 502, 503])
def test_a_failing_tracker_is_retried(tracked, fake_tracker, status):  # noqa: F811
    fake_tracker.error = http_error(status)

    with pytest.raises(services.TrackerServerError):
        tasks.push_site_config_task(tracked["site"].pk)


def test_an_unreachable_tracker_is_retried(tracked, fake_tracker):  # noqa: F811
    fake_tracker.error = urllib.error.URLError("down")

    with pytest.raises(services.TrackerUnreachable):
        tasks.push_site_config_task(tracked["site"].pk)


def test_only_unreachable_and_failing_trackers_retry():
    retried = tasks.push_site_config_task.autoretry_for
    assert services.TrackerUnreachable in retried
    assert services.TrackerServerError in retried
    assert services.TrackerPushError not in retried


def test_a_server_error_is_still_a_push_error(tracked, fake_tracker):  # noqa: F811
    fake_tracker.error = http_error(503)

    with pytest.raises(services.TrackerPushError) as raised:
        services.push_site_config(tracked["site"])

    assert isinstance(raised.value, services.TrackerServerError)
    assert not isinstance(raised.value, services.TrackerUnreachable)
