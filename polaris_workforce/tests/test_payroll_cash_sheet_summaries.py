"""The Cash sheets list reads totals, not lines (``services.cash_sheet_summaries``).

A summary must show exactly what the full sheet (``services.cash_sheets``) shows for the
list's columns: the reconciliation, the status and the currency.
"""

import datetime
from unittest import mock

import pytest
from django.core.management import call_command
from django.db import connection
from django.test.utils import CaptureQueriesContext

from apps.payroll import services
from apps.payroll.models import PayPeriod
from apps.worksites.models import Site
from tests.test_payroll_services import (  # noqa: F401
    MON,
    SUBMITTED,
    TUE,
    company,
    hire,
    line_of,
    media,
    pay,
    roll_call,
    setup_site,
)


def listed(sheet) -> dict:
    """What the Cash sheets list shows of a sheet."""
    return {
        "site": sheet.site.pk,
        "date": sheet.date,
        "periods": [p.pk for p in sheet.periods],
        "reconciliation": sheet.reconciliation,
        "status": sheet.status,
        "currency": (sheet.currency, sheet.currency_decimals),
    }


def assert_same_as_full_sheets(keys, today):
    full = services.cash_sheets(keys, today=today)
    summaries = services.cash_sheet_summaries(keys, today=today)
    assert [listed(s) for s in summaries] == [listed(s) for s in full]


def test_summaries_match_the_full_sheets(company, media):  # noqa: F811
    site, _manager, foreman = setup_site()
    yao, afi, kodjo = hire(site), hire(site, 3500), hire(site, 5000)
    roll_call(site, MON, {yao: "present", afi: "half_day", kodjo: "absent"})
    services.cash_sheet(site, MON)
    services.issue_cash(site.manager, site, MON, 20000)
    pay(foreman, line_of(site, MON, yao))  # Afi is still to pay: a sheet paying out
    roll_call(site, TUE, {yao: "present", afi: "present"}, status=SUBMITTED)  # not ready
    services.cash_sheet(site, TUE, today=TUE)
    empty = Site.objects.exclude(pk=site.pk).first() or setup_site()[0]  # no period

    assert_same_as_full_sheets([(site, MON), (site, TUE), (empty, MON)], today=TUE)


def test_a_fully_paid_sheet_and_a_closed_one(company, media):  # noqa: F811
    site, manager, foreman = setup_site()
    yao = hire(site)
    roll_call(site, MON, {yao: "present"})
    services.cash_sheet(site, MON)
    pay(foreman, line_of(site, MON, yao))
    assert_same_as_full_sheets([(site, MON)], today=MON)
    assert services.cash_sheet_summaries([(site, MON)], today=MON)[0].status == "paid"

    PayPeriod.objects.filter(site=site).update(status=PayPeriod.Status.LOCKED)
    assert_same_as_full_sheets([(site, MON)], today=MON)
    assert services.cash_sheet_summaries([(site, MON)], today=MON)[0].status == "closed"


def test_queries_do_not_grow_with_the_crew(company):  # noqa: F811
    def queries_for(crew_size):
        site, _m, _f = setup_site()
        roll_call(site, MON, {hire(site): "present" for _ in range(crew_size)})
        services.cash_sheet(site, MON)
        with CaptureQueriesContext(connection) as queries:
            services.cash_sheet_summaries([(site, MON)], today=MON)
        return len(queries)

    assert queries_for(12) == queries_for(2)


def test_no_sheets_no_queries(company, django_assert_num_queries):  # noqa: F811
    with django_assert_num_queries(0):
        assert services.cash_sheet_summaries([]) == []


@pytest.mark.django_db
def test_summaries_match_the_full_sheets_on_the_demo(settings, tmp_path):
    # The demo holds every kind of sheet: locked, paid, partly paid, adjusted, waiting.
    settings.MEDIA_ROOT = tmp_path
    friday = datetime.datetime(2026, 10, 30, 10, 0, tzinfo=datetime.UTC)
    real_now = datetime.datetime.now
    started = real_now(datetime.UTC)
    with mock.patch(
        "django.utils.timezone.now", side_effect=lambda: friday + (real_now(datetime.UTC) - started)
    ):
        call_command("seed_demo", password="demo-pass-123", verbosity=0)
        today = friday.date()
        keys = [
            (period.site, period.end_date)
            for period in PayPeriod.objects.select_related("site")
            .order_by("-end_date", "site_id")
            .distinct()
        ]
        keys = list(dict.fromkeys(keys))
        assert len(keys) > 10
        statuses = {s.status for s in services.cash_sheets(keys, today=today)}
        assert {"closed", "open"} <= statuses
        assert_same_as_full_sheets(keys, today)
