"""Pay lists skip the refresh on read while nothing changed (``services.sync_*_on_read``).

Pay periods and Cash sheets recompute every active site's pay on read. The recomputation
is skipped while the company's change marker (``apps.core.changes``, moved by every
``AuditLog`` entry) and the day are those of the site's last recomputation, for at most
``READ_REFRESH_MAX_AGE`` seconds. A recomputation that writes moves the marker itself,
so pay is settled after two reads: the first creates the lines, the second finds nothing
to change (``settle``).
"""

import datetime
import time

import pytest
from django.urls import reverse

from apps.core import changes
from apps.core.models import AuditLog
from apps.core.tenancy import use_company
from apps.payroll import services
from apps.payroll.models import PayLine
from apps.workers import services as worker_services
from apps.worksites.models import Site
from tests.factories import CompanyFactory
from tests.test_payroll_services import (  # noqa: F401
    MON,
    TUE,
    company,
    hire,
    roll_call,
    setup_site,
)


@pytest.fixture
def recomputations(monkeypatch):
    """The sites ``services.sync_site`` recomputes, in order."""
    seen = []
    real = services.sync_site

    def spy(site, *args, **kwargs):
        seen.append(site.pk)
        return real(site, *args, **kwargs)

    monkeypatch.setattr(services, "sync_site", spy)
    return seen


def sites_of(*sites):
    return Site.objects.filter(pk__in=[s.pk for s in sites])


def read_recent(site, today=MON):
    services.sync_recent_on_read(sites_of(site), today, 7)


def settle(recomputations, *sites, today=MON):
    """Read until nothing is left to change, then forget the recomputations so far."""
    for _ in range(2):
        services.sync_recent_on_read(sites_of(*sites), today, 7)
    recomputations.clear()


def test_reads_skip_the_recomputation_once_pay_is_settled(company, recomputations):  # noqa: F811
    site, _m, _f = setup_site()
    roll_call(site, MON, {hire(site): "present"})

    for _ in range(4):
        read_recent(site)

    # The first read creates the line (and its audit entries), the second finds nothing
    # to change, the others skip.
    assert recomputations == [site.pk, site.pk]
    assert PayLine.objects.get().net == 6000


def test_an_audited_change_is_recomputed_on_the_next_read(company, recomputations):  # noqa: F811
    # A rate change has no pay hook of its own: the read refresh is what reprices the day.
    site, _m, _f = setup_site()
    yao = hire(site)
    roll_call(site, MON, {yao: "present"})
    settle(recomputations, site)

    worker_services.change_rate(yao, 8000, "daily", datetime.date(2025, 1, 1))
    read_recent(site)

    assert recomputations == [site.pk]
    assert PayLine.objects.get().net == 8000


def test_the_next_day_recomputes(company, recomputations):  # noqa: F811
    site, _m, _f = setup_site()
    roll_call(site, MON, {hire(site): "present"})
    settle(recomputations, site, today=MON)

    read_recent(site, TUE)

    assert recomputations == [site.pk]


def test_another_companys_change_does_not_recompute(company, recomputations):  # noqa: F811
    site, _m, _f = setup_site()
    roll_call(site, MON, {hire(site): "present"})
    settle(recomputations, site)

    other = CompanyFactory()
    with use_company(other):
        AuditLog.record(actor=None, action="workers.rate_change")
    read_recent(site)

    assert recomputations == []


def test_a_change_recomputes_every_site_of_the_company(company, recomputations):  # noqa: F811
    # A worker's advances and adjustments span sites: any change may move any site's pay.
    first, _m, _f = setup_site()
    second, _m, _f = setup_site()
    roll_call(first, MON, {hire(first): "present"})
    roll_call(second, MON, {hire(second): "present"})
    settle(recomputations, first, second)

    AuditLog.record(actor=None, action="workers.rate_change")
    services.sync_recent_on_read(sites_of(first, second), MON, 7)

    assert sorted(recomputations) == sorted([first.pk, second.pk])


def test_an_unaudited_change_shows_once_the_skip_expires(company, monkeypatch):  # noqa: F811
    # A write that bypasses the services (Django admin, a shell) leaves no audit entry.
    site, _m, _f = setup_site()
    yao = hire(site)
    roll_call(site, MON, {yao: "present"})
    read_recent(site)
    read_recent(site)
    yao.pay_rates.update(amount=7000)

    read_recent(site)
    assert PayLine.objects.get().net == 6000

    later = time.time() + services.READ_REFRESH_MAX_AGE + 1
    monkeypatch.setattr("django.core.cache.backends.locmem.time.time", lambda: later)
    read_recent(site)
    assert PayLine.objects.get().net == 7000


def test_cash_sheets_skip_their_refresh_too(company, monkeypatch):  # noqa: F811
    site, _m, _f = setup_site()
    roll_call(site, MON, {hire(site): "present"})
    seen = []
    real = services.sync_sheet
    monkeypatch.setattr(
        services, "sync_sheet", lambda s, *a, **k: seen.append(s.pk) or real(s, *a, **k)
    )

    for _ in range(3):
        services.sync_sheets_on_read(sites_of(site), MON)
    assert seen == [site.pk, site.pk]

    AuditLog.record(actor=None, action="attendance.approve")
    services.sync_sheets_on_read(sites_of(site), MON)
    assert seen == [site.pk, site.pk, site.pk]


def test_the_two_lists_keep_their_own_markers(company, recomputations):  # noqa: F811
    # The Cash sheets refresh covers today's sheet only, not the Pay periods' last week.
    site, _m, _f = setup_site()
    roll_call(site, MON, {hire(site): "present"})
    for _ in range(2):
        services.sync_sheets_on_read(sites_of(site), MON)
    recomputations.clear()

    read_recent(site)

    assert recomputations == [site.pk]


def test_pay_periods_page_recomputes_once_until_something_changes(
    client,
    company,  # noqa: F811
    recomputations,
    monkeypatch,
):
    site, manager, _f = setup_site()
    yao = hire(site)
    roll_call(site, MON, {yao: "present"})
    monkeypatch.setattr("apps.core.dates.company_today", lambda company=None: MON)
    client.force_login(manager)

    for _ in range(4):
        client.get(reverse("payroll:periods"))
    assert recomputations == [site.pk, site.pk]

    worker_services.change_rate(yao, 8000, "daily", datetime.date(2025, 1, 1))
    client.get(reverse("payroll:periods"))

    assert recomputations == [site.pk, site.pk, site.pk]
    assert PayLine.objects.get().net == 8000


# --- the change marker --------------------------------------------------------------------


def test_an_audit_entry_moves_the_marker(company):  # noqa: F811
    before = changes.version(company.company_id)

    AuditLog.record(actor=None, action="workers.rate_change")

    assert changes.version(company.company_id) != before


def test_the_marker_moves_again_when_the_transaction_commits(
    company,  # noqa: F811
    django_capture_on_commit_callbacks,
):
    # A reader that recomputed between the write and the commit read data not yet
    # committed: the second move makes the next read recompute again.
    with django_capture_on_commit_callbacks(execute=True):
        changes.mark_changed(company.company_id)
        during = changes.version(company.company_id)

    assert changes.version(company.company_id) != during


def test_a_missing_marker_counts_as_a_change(company):  # noqa: F811
    from django.core.cache import cache

    first = changes.version(company.company_id)
    cache.clear()

    assert changes.version(company.company_id) != first
