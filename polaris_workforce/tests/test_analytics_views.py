"""Analytics screens: the reports tabs, the budget warning, the Today alert and the site
card, scoped to the viewer's role, sites and company, at a fixed number of queries."""

import datetime

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from apps.accounts.models import User
from apps.analytics.models import AnalyticsSettings
from apps.core.dates import company_today
from apps.core.models import AuditLog
from apps.core.tenancy import use_company
from apps.payouts.models import PayoutAccount, TransferMethod
from apps.payroll.models import PayPeriod
from apps.worksites.forms import SiteForm

from . import factories as f

pytestmark = pytest.mark.django_db

PAGES = ("dashboard:reports", "dashboard:reports_cash", "dashboard:reports_checks")


def last_30_days(world):
    start = world["today"] - datetime.timedelta(days=30)
    return {"period": "custom", "start": start.isoformat(), "end": world["today"].isoformat()}


def pay(site, date, gross, worker=None):
    period = PayPeriod.objects.filter(site=site, start_date=date, end_date=date).first()
    period = period or f.PayPeriodFactory(
        site=site, start_date=date, end_date=date, status=PayPeriod.Status.PAYING
    )
    return f.PayLineFactory(period=period, worker=worker or f.WorkerFactory(), gross=gross)


def mobile(worker, number):
    return PayoutAccount.objects.create(
        worker=worker,
        account_holder=worker.first_name,
        method=TransferMethod.MOBILE_MONEY,
        mobile_operator="Flooz",
        mobile_number=number,
    )


@pytest.fixture
def world():
    f.CompanySettingsFactory()
    today = company_today()
    manager = f.SiteManagerFactory()
    mine = f.SiteFactory(name="Market Hall", manager=manager, labour_budget=100_000)
    theirs = f.SiteFactory(name="Bridge Works", labour_budget=1_000_000)
    worker = f.WorkerFactory(first_name="Yao")
    f.SiteAssignmentFactory(
        worker=worker, site=mine, start_date=today - datetime.timedelta(days=60)
    )
    line = pay(mine, today - datetime.timedelta(days=2), 95_000, worker=worker)
    f.CashIssueFactory(site=mine, date=line.period.end_date, amount=120_000)
    pay(theirs, today - datetime.timedelta(days=2), 33_000)
    f.AdvanceFactory(worker=worker, balance=4_000, given_on=today - datetime.timedelta(days=5))
    mobile(worker, "+228 90 11 22 33")
    mobile(f.WorkerFactory(first_name="Afi"), "+22890112233")
    with use_company(f.CompanyFactory()):
        f.CompanySettingsFactory()
        elsewhere = f.SiteFactory(name="Elsewhere Site", labour_budget=10)
        pay(elsewhere, today - datetime.timedelta(days=2), 77_000)
        f.CashIssueFactory(site=elsewhere, date=today - datetime.timedelta(days=2))
    return {
        "today": today,
        "mine": mine,
        "theirs": theirs,
        "manager": manager,
        "admin": f.AdminUserFactory(),
        "accountant": f.UserFactory(role=User.Role.ACCOUNTANT),
    }


@pytest.mark.parametrize("page", PAGES)
def test_foremen_may_not_open_the_reports(client, world, page):
    client.force_login(f.ForemanFactory())

    assert client.get(reverse(page)).status_code == 403


@pytest.mark.parametrize("page", PAGES)
def test_admins_see_every_site_of_their_company_only(client, world, page):
    client.force_login(world["admin"])

    html = client.get(reverse(page)).content.decode()

    assert "Elsewhere Site" not in html
    if page != "dashboard:reports_checks":
        assert "Market Hall" in html and "Bridge Works" in html


@pytest.mark.parametrize("page", PAGES)
def test_site_managers_see_their_sites_only(client, world, page):
    client.force_login(world["manager"])

    response = client.get(reverse(page))

    html = response.content.decode()
    assert response.status_code == 200
    assert "Bridge Works" not in html and "Elsewhere Site" not in html


def test_the_cost_tab_shows_budgets_breakdown_and_changes(client, world):
    client.force_login(world["accountant"])

    response = client.get(reverse("dashboard:reports"), last_30_days(world))

    budgets = {item.name: item for item in response.context["budgets"]}
    assert budgets["Market Hall"].status == "at_risk"  # 95 % of the budget used
    assert budgets["Market Hall"].used_percent == 95
    assert response.context["costs"].total.labour_cost == 128_000
    assert "labour_cost" in response.context["changes"]
    html = response.content.decode()
    assert 'id="budgets"' in html and 'id="cost-breakdown"' in html
    assert "At risk" in html and "Nothing to compare with in the previous period" in html
    assert 'action="/reports/budget-warning/"' not in html  # admins only


def test_the_cash_tab(client, world):
    client.force_login(world["manager"])

    response = client.get(reverse("dashboard:reports_cash"), last_30_days(world))

    (sheet,) = response.context["cash"].days
    assert (sheet.to_pay, sheet.cash_issued, sheet.unpaid_workers) == (95_000, 120_000, 1)
    assert response.context["unpaid_total"].total == 95_000
    assert response.context["advance_total"].total == 4_000
    assert 'id="cash-sheets"' in response.content.decode()


def test_shared_payout_accounts_are_for_admins_only(client, world):
    client.force_login(world["admin"])
    admin_page = client.get(reverse("dashboard:reports_checks"))
    client.force_login(world["accountant"])
    accountant_page = client.get(reverse("dashboard:reports_checks"))

    assert len(admin_page.context["shared_accounts"]) == 1
    assert 'id="shared-accounts"' in admin_page.content.decode()
    assert accountant_page.context["shared_accounts"] is None
    assert 'id="shared-accounts"' not in accountant_page.content.decode()


def test_tabs_carry_the_filters_and_only_them(client, world):
    client.force_login(world["admin"])

    html = client.get(
        reverse("dashboard:reports_cash"), {"period": "last_month", "page": "2"}
    ).content.decode()

    assert f'href="{reverse("dashboard:reports_checks")}?period=last_month"' in html


@pytest.mark.parametrize("page", PAGES)
def test_query_count_does_not_grow_with_sites(client, world, page, django_assert_max_num_queries):
    client.force_login(world["admin"])
    client.get(reverse(page))  # warm the per-process caches (content types)
    with CaptureQueriesContext(connection) as before:
        client.get(reverse(page))
    for index in range(5):
        site = f.SiteFactory(name=f"Extra {index}", labour_budget=50_000)
        pay(site, world["today"] - datetime.timedelta(days=1), 1_000)
        f.CashIssueFactory(site=site, date=world["today"] - datetime.timedelta(days=1))

    with django_assert_max_num_queries(len(before)):
        assert client.get(reverse(page)).status_code == 200


def test_admins_set_the_budget_warning(client, world):
    client.force_login(world["admin"])

    response = client.post(
        reverse("dashboard:budget_warning"),
        {"budget_warn_percent": "96", "query": "period=last_month&evil=1"},
    )

    assert response.status_code == 302
    assert response["Location"] == reverse("dashboard:reports") + "?period=last_month#budgets"
    assert AnalyticsSettings.load().budget_warn_percent == 96
    assert AuditLog.objects.filter(action="analytics.settings.update").count() == 1
    html = client.get(reverse("dashboard:reports")).content.decode()
    assert 'action="/reports/budget-warning/"' in html
    budgets = client.get(reverse("dashboard:reports")).context["budgets"]
    # 95 % used, under the new 96 %; three days of pay are too few to project.
    assert {item.name: item.status for item in budgets}["Market Hall"] == "not_enough_data"


def test_budget_warning_is_validated_and_admin_only(client, world):
    client.force_login(world["admin"])
    client.post(reverse("dashboard:budget_warning"), {"budget_warn_percent": "150"})
    assert AnalyticsSettings.load().pk is None

    client.force_login(world["manager"])
    response = client.post(reverse("dashboard:budget_warning"), {"budget_warn_percent": "50"})
    assert response.status_code == 403
    assert client.get(reverse("dashboard:budget_warning")).status_code == 403


def test_budget_warning_changes_the_admins_own_company_only(client, world, default_company):
    other = f.CompanyFactory()
    with use_company(other):
        AnalyticsSettings.objects.create(budget_warn_percent=50)
    client.force_login(world["admin"])

    client.post(reverse("dashboard:budget_warning"), {"budget_warn_percent": "70"})

    assert AnalyticsSettings.load(other).budget_warn_percent == 50
    assert AnalyticsSettings.load(default_company).budget_warn_percent == 70


def test_today_lists_sites_over_or_at_risk_of_their_budget(client, world):
    over = f.SiteFactory(name="Over Site", labour_budget=1_000)
    pay(over, world["today"] - datetime.timedelta(days=1), 2_000)
    client.force_login(world["admin"])

    response = client.get(reverse("dashboard:home"))

    alerts = {a.site_name: a for a in response.context["overview"].alerts if a.kind == "budget"}
    assert (alerts["Over Site"].severity, alerts["Over Site"].count) == ("danger", 200)
    assert alerts["Market Hall"].severity == "warning"
    assert "Bridge Works" not in alerts
    html = response.content.decode()
    assert "Over Site — over its labour budget" in html
    assert "Market Hall — labour budget at risk" in html


def test_foremen_get_no_budget_alerts(client, world):
    foreman = f.ForemanFactory()
    world["mine"].foremen.add(foreman)
    client.force_login(foreman)

    response = client.get(reverse("dashboard:home"))

    assert not [a for a in response.context["overview"].alerts if a.kind == "budget"]


def test_the_site_card_shows_cost_to_date_against_the_budget(client, world):
    client.force_login(world["manager"])

    response = client.get(reverse("worksites:detail", args=[world["mine"].pk]))

    assert response.context["burn"].spent == 95_000
    html = response.content.decode()
    assert "Labour cost to date" in html and "95% of the budget of" in html
    assert "At risk" in html


def test_the_site_form_takes_a_planned_end_date(world):
    site = world["mine"]
    data = {
        "name": site.name,
        "address": site.address,
        "status": site.status,
        "started_on": "2026-03-01",
        "planned_end_date": "2026-02-01",
        "manager": site.manager_id,
    }

    form = SiteForm(data, instance=site, decimals=0)
    assert not form.is_valid()
    assert "planned_end_date" in form.errors

    data["planned_end_date"] = "2027-03-31"
    form = SiteForm(data, instance=site, decimals=0)
    assert form.is_valid(), form.errors
    assert form.save().planned_end_date == datetime.date(2027, 3, 31)
