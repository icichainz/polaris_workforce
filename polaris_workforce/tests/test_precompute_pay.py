"""Screens recompute pay only where a change marked it out of date (``apps.precompute``).

Pay periods, Cash sheets, a cash sheet, the phone's payout and the agents' cash sheet read
pay through ``ensure_pay_fresh``: a site is recomputed when a change marked the dates it
shows (from the change's audit entry), or on a new day; otherwise the read recomputes
nothing. ``manage.py refresh_precomputed`` does the same work in the background, and its
nightly sweep catches writes that bypassed the services.
"""

import datetime

import pytest
from django.contrib.admin.sites import site as admin_site
from django.core.management import call_command
from django.test import RequestFactory
from django.urls import reverse

from apps.core.models import AuditLog
from apps.payroll import services
from apps.payroll.models import PayLine
from apps.precompute import services as precompute
from apps.precompute.models import RefreshRun, Stale
from apps.workers import services as worker_services
from apps.workers.models import PayRate
from apps.worksites.models import Site
from tests.factories import UserFactory
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


def read_recent(*sites, today=MON):
    services.sync_recent_on_read(sites_of(*sites), today, 7)


def test_a_read_recomputes_once_then_skips(company, recomputations):  # noqa: F811
    site, _m, _f = setup_site()
    roll_call(site, MON, {hire(site): "present"})

    for _ in range(4):
        read_recent(site)

    # The recomputation's own audit entries (payroll.auto.*) mark nothing: no second pass.
    assert recomputations == [site.pk]
    assert PayLine.objects.get().net == 6000


def test_a_rate_change_reprices_only_that_workers_site(company, recomputations):  # noqa: F811
    site, _m, _f = setup_site()
    other, _m, _f = setup_site()
    yao = hire(site)
    roll_call(site, MON, {yao: "present"})
    roll_call(other, MON, {hire(other): "present"})
    read_recent(site, other)
    recomputations.clear()

    worker_services.change_rate(yao, 8000, "daily", datetime.date(2025, 1, 1))
    read_recent(site, other)

    assert recomputations == [site.pk]
    assert PayLine.objects.get(worker=yao).net == 8000


def test_sign_ins_and_messages_recompute_nothing(company, recomputations):  # noqa: F811
    site, _m, _f = setup_site()
    roll_call(site, MON, {hire(site): "present"})
    read_recent(site)
    recomputations.clear()

    AuditLog.record(actor=None, action="accounts.login_locked")
    AuditLog.record(actor=None, action="messaging.message.resend")
    read_recent(site)

    assert recomputations == []


def test_the_next_day_recomputes(company, recomputations):  # noqa: F811
    site, _m, _f = setup_site()
    roll_call(site, MON, {hire(site): "present"})
    read_recent(site, today=MON)
    recomputations.clear()

    read_recent(site, today=TUE)
    read_recent(site, today=TUE)

    assert recomputations == [site.pk]


def test_a_read_of_other_dates_is_not_held_up(company, recomputations):  # noqa: F811
    site, _m, _f = setup_site()
    roll_call(site, MON, {hire(site): "present"})
    read_recent(site)
    recomputations.clear()

    precompute.mark(site.pk, datetime.date(2025, 6, 2), datetime.date(2025, 6, 2))
    services.read_cash_sheet(site, MON, today=MON)

    assert recomputations == []
    assert Stale.objects.filter(site=site).exists()  # left for the cron run


def test_the_cron_run_leaves_nothing_for_the_screens(company, recomputations):  # noqa: F811
    site, _m, _f = setup_site()
    yao = hire(site)
    roll_call(site, MON, {yao: "present"})

    precompute.run(site.company_id, today=MON)
    recomputations.clear()
    read_recent(site)
    services.read_cash_sheet(site, MON, today=MON)

    assert recomputations == []
    assert PayLine.objects.get().net == 6000


def test_an_admin_edit_reprices_on_the_next_read(company, recomputations):  # noqa: F811
    site, _m, _f = setup_site()
    yao = hire(site)
    roll_call(site, MON, {yao: "present"})
    read_recent(site)
    rate = PayRate.objects.get(worker=yao)
    rate.amount = 7000
    request = RequestFactory().post("/admin/")
    request.user = UserFactory(is_superuser=True, is_staff=True, company=None)

    admin_site._registry[PayRate].save_model(request, rate, form=_Form(["amount"]), change=True)
    read_recent(site)

    assert PayLine.objects.get().net == 7000


class _Form:
    def __init__(self, changed_data):
        self.changed_data = changed_data


def test_the_sweep_fixes_and_reports_an_unmarked_change(company):  # noqa: F811
    # A write that bypassed the services and the audit log (a shell, a script).
    site, _m, _f = setup_site()
    yao = hire(site)
    roll_call(site, MON, {yao: "present"})
    read_recent(site)
    PayRate.objects.filter(worker=yao).update(amount=7000)

    read_recent(site)
    assert PayLine.objects.get().net == 6000  # nothing marked it

    run = precompute.run(site.company_id, sweep=True, today=MON)

    assert PayLine.objects.get().net == 7000
    assert [item["site"] for item in run.mismatches] == [site.pk]


def test_the_command_logs_its_run(company):  # noqa: F811
    site, _m, _f = setup_site()
    roll_call(site, MON, {hire(site): "present"})
    call_command("refresh_precomputed", "--sweep", verbosity=0)
    run = RefreshRun.objects.get()
    assert run.sweep and run.finished_at is not None and run.mismatches == []


# --- the screens ----------------------------------------------------------------------------


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
    assert recomputations == [site.pk]

    worker_services.change_rate(yao, 8000, "daily", datetime.date(2025, 1, 1))
    client.get(reverse("payroll:periods"))

    assert recomputations == [site.pk, site.pk]
    assert PayLine.objects.get().net == 8000


def test_the_cash_sheet_page_and_the_phone_read_without_recomputing(
    client,
    company,  # noqa: F811
    recomputations,
):
    from rest_framework.test import APIClient

    site, manager, foreman = setup_site()
    roll_call(site, MON, {hire(site): "present"})
    client.force_login(manager)
    phone = APIClient()
    phone.force_authenticate(foreman)
    page = reverse("payroll:cash_sheet", args=[site.pk, MON.isoformat()])
    payout = reverse("api:payroll:payout", kwargs={"site_id": site.pk, "date": MON.isoformat()})

    assert client.get(page).status_code == 200
    first = list(recomputations)
    assert set(first) == {site.pk}  # the marked day, and the new day's periods
    assert client.get(page).status_code == 200
    assert phone.get(payout).status_code == 200
    assert recomputations == first
