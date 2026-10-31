"""Wiring between areas: the company clock, the crew rule, deep links, proofs, cash tonight."""

import datetime
from unittest import mock

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.attendance import selectors
from apps.core import dates
from apps.core.forms import CompanySettingsForm
from apps.core.models import CompanySettings
from apps.dashboard import services as dashboard_services
from apps.payroll import services as payroll_services
from apps.worksites import services as site_services
from tests import factories as f
from tests.test_payroll_services import MON, TUE, hire, line_of, pay, roll_call, setup_site
from tests.test_settings_view import form_data

pytestmark = pytest.mark.django_db

D = datetime.date


@pytest.fixture(autouse=True)
def _fast_passwords(settings, tmp_path):
    settings.PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]
    settings.MEDIA_ROOT = tmp_path


# --- apps.core.dates ---------------------------------------------------------------------


def test_company_clock_uses_the_company_time_zone():
    company = f.CompanySettingsFactory(time_zone="Pacific/Kiritimati")  # UTC+14
    late_evening_utc = datetime.datetime(2026, 10, 30, 22, 0, tzinfo=datetime.UTC)
    with mock.patch.object(timezone, "now", return_value=late_evening_utc):
        assert dates.company_now(company).utcoffset() == datetime.timedelta(hours=14)
        assert dates.company_today(company) == D(2026, 10, 31)
        assert dates.company_today() == D(2026, 10, 31)  # loads the settings itself
    assert str(dates.company_tz(company)) == "Pacific/Kiritimati"


def test_unknown_or_empty_time_zone_falls_back_to_utc():
    for value in ("", "Not/AZone"):
        company = CompanySettings(time_zone=value)
        assert dates.company_tz(company).utcoffset(None) == datetime.timedelta(0)


def test_old_names_are_the_shared_helpers():
    # The API packages still import the helpers from these modules.
    assert site_services.company_today is dates.company_today
    assert selectors.company_today is dates.company_today
    assert payroll_services.company_today is dates.company_today
    assert dashboard_services.company_now is dates.company_now


# --- one crew rule -----------------------------------------------------------------------


def test_attendance_expected_crew_is_the_worksites_crew():
    site = f.SiteFactory()
    always = f.SiteAssignmentFactory(site=site, start_date=D(2026, 10, 1)).worker
    left = f.SiteAssignmentFactory(
        site=site, start_date=D(2026, 10, 1), end_date=D(2026, 10, 30)
    ).worker
    later = f.SiteAssignmentFactory(site=site, start_date=D(2026, 11, 2)).worker
    inactive = f.SiteAssignmentFactory(
        site=site, start_date=D(2026, 10, 1), worker=f.WorkerFactory(is_active=False)
    ).worker
    f.SiteAssignmentFactory(start_date=D(2026, 10, 1))  # another site

    start, end = D(2026, 10, 29), D(2026, 11, 3)
    by_date = selectors.expected_by_date(site, start, end)
    assert list(by_date) == [start + datetime.timedelta(days=i) for i in range(6)]
    for date, expected in by_date.items():
        crew = {w.pk for w in site_services.crew_for(site, date)}
        assert expected == crew == selectors.expected_worker_ids(site, date)
    assert by_date[D(2026, 10, 30)] == {always.pk, left.pk}  # end date inclusive
    assert by_date[D(2026, 10, 31)] == {always.pk}
    assert by_date[D(2026, 11, 2)] == {always.pk, later.pk}  # start date inclusive
    assert inactive.pk not in set().union(*by_date.values())


# --- dashboard cash tonight = payroll reconciliation -------------------------------------


def test_reconciliations_match_the_cash_sheet():
    f.CompanySettingsFactory()
    site, manager, foreman = setup_site()
    yao, afi = hire(site), hire(site, 3500)
    roll_call(site, MON, {yao: "present", afi: "present"})
    payroll_services.cash_sheet(site, MON)
    payroll_services.issue_cash(manager, site, MON, 12000, received_by=foreman)
    pay(foreman, line_of(site, MON, yao))
    other, *_ = setup_site()

    batched = payroll_services.reconciliations([site.pk, other.pk], MON)

    assert batched == {
        site.pk: payroll_services.cash_sheet(site, MON, refresh=False).reconciliation
    }
    assert batched[site.pk].left_to_pay == 3500
    assert dashboard_services._cash_tonight([site.pk, other.pk], MON) == {site.pk: 3500}


def test_reconciliations_leave_out_locked_periods():
    f.CompanySettingsFactory()
    site, *_ = setup_site()
    roll_call(site, MON, {hire(site): "present"})
    payroll_services.cash_sheet(site, MON)
    payroll_services.PayPeriod.objects.update(status=payroll_services.PayPeriod.Status.LOCKED)

    assert payroll_services.reconciliations([site.pk], MON) == {}


# --- settings: "no proof needed" ---------------------------------------------------------


def test_settings_accept_no_proof(client):
    client.force_login(f.AdminUserFactory())
    company = f.CompanySettingsFactory(accepted_proofs=["photo"])

    page = client.get(reverse("core:settings")).content.decode()
    assert 'value="none"' in page
    assert "No proof needed" in page
    assert "without any proof of receipt" in page

    response = client.post(
        reverse("core:settings"), form_data(company, accepted_proofs=["photo", "none"])
    )

    assert response.status_code == 302
    company = CompanySettings.load()
    assert company.accepted_proofs == ["photo", "none"]
    payroll_services.validate_proof("none", None, company)  # accepted now


def test_settings_form_orders_proofs():
    form = CompanySettingsForm(
        data=form_data(f.CompanySettingsFactory(), accepted_proofs=["none", "signature"]),
        instance=CompanySettings.load(),
    )
    assert form.is_valid(), form.errors
    assert form.cleaned_data["accepted_proofs"] == ["signature", "none"]


# --- deep links --------------------------------------------------------------------------


@pytest.fixture
def paid_monday():
    f.CompanySettingsFactory()
    site, manager, foreman = setup_site(name="Market Hall")
    yao = hire(site, first_name="Yao", last_name="Mensah")
    f.AdvanceFactory(worker=yao, amount=10000, balance=10000, deduction_per_pay=2000)
    roll_call(site, MON, {yao: "present"})
    payroll_services.cash_sheet(site, MON)
    payroll_services.issue_cash(manager, site, MON, 6000, received_by=foreman)
    pay(foreman, line_of(site, MON, yao))
    return site, manager, yao


def test_worker_profile_links_payslips_and_advances(client, paid_monday):
    site, manager, yao = paid_monday
    client.force_login(manager)

    html = client.get(reverse("workers:detail", args=[yao.pk])).content.decode()

    payslip = reverse("payroll:payslip", args=[line_of(site, MON, yao).pk])
    assert f'href="{payslip}"' in html
    advances = f"{reverse('payroll:advances')}?worker={yao.pk}"
    assert html.count(f'href="{advances}"') == 2  # the tile and the "Give advance" button
    assert "See advances" in html


def test_worker_profile_hides_payments_of_sites_out_of_scope(client, paid_monday):
    # Security review L6: a worker who moved sites keeps the old site's payments; the new
    # site's manager sees none of them (rows, amounts, proof links, payslips).
    site, manager, yao = paid_monday
    payment = line_of(site, MON, yao).payments.get()
    elsewhere, other_manager, _foreman = setup_site()
    f.SiteAssignmentFactory(worker=yao, site=elsewhere, start_date=TUE)

    client.force_login(manager)
    own = client.get(reverse("workers:detail", args=[yao.pk])).content.decode()
    assert reverse("core:payment_proof", args=[payment.pk]) in own
    assert "No payments yet." not in own

    client.force_login(other_manager)
    response = client.get(reverse("workers:detail", args=[yao.pk]))
    html = response.content.decode()

    assert response.status_code == 200
    assert "No payments yet." in html
    assert reverse("payroll:payslip", args=[line_of(site, MON, yao).pk]) not in html
    assert reverse("core:payment_proof", args=[payment.pk]) not in html
    assert "/files/proofs/" not in html and "/media/" not in html
    assert client.get(reverse("core:payment_proof", args=[payment.pk])).status_code == 404


def test_advances_page_filters_by_worker(client, paid_monday):
    _site, manager, yao = paid_monday
    other = f.AdvanceFactory(worker=hire(_site, first_name="Afi", last_name="Dosseh"))
    client.force_login(manager)

    html = client.get(reverse("payroll:advances"), {"worker": yao.pk}).content.decode()
    table = html.split("<tbody>", 1)[1].split("</tbody>", 1)[0]

    assert "Yao Mensah" in table
    assert other.worker.full_name not in table
    assert "Showing Yao Mensah only." in html
    assert f'<option value="{yao.pk}" selected>' in html


def test_advances_worker_filter_is_scoped(client, paid_monday):
    _site, _manager, yao = paid_monday
    _elsewhere, stranger, _foreman = setup_site()
    client.force_login(stranger)

    html = client.get(reverse("payroll:advances"), {"worker": yao.pk}).content.decode()

    assert "Yao Mensah" not in html


def test_site_pay_tab_links_to_its_cash_sheets(client, paid_monday):
    site, manager, _yao = paid_monday
    client.force_login(manager)

    html = client.get(reverse("worksites:pay", args=[site.pk])).content.decode()

    assert f'href="{reverse("payroll:cash_sheets")}?site={site.pk}"' in html
    assert f'href="{reverse("payroll:periods")}?site={site.pk}"' in html
    assert f'href="{reverse("payroll:cash_sheet", args=[site.pk, MON.isoformat()])}"' in html


def test_dashboard_alerts_link_to_details(client, monkeypatch):
    today = D(2026, 10, 30)  # a Friday
    now = datetime.datetime(2026, 10, 30, 11, 0, tzinfo=datetime.UTC)
    monkeypatch.setattr(dashboard_services, "company_now", lambda company: now)
    f.CompanySettingsFactory()
    admin = f.AdminUserFactory()
    site = f.SiteFactory(name="Riverside")
    worker = f.WorkerFactory(first_name="Kodjo", last_name="Atsu")
    f.SiteAssignmentFactory(site=site, worker=worker)
    for back in (3, 2, 1):
        day = f.AttendanceDayFactory(
            site=site, date=today - datetime.timedelta(days=back), status="approved"
        )
        f.AttendanceRecordFactory(day=day, worker=worker, status="absent")
    submitted = f.AttendanceDayFactory(site=site, date=D(2026, 10, 26), status="submitted")
    f.AttendanceCorrectionFactory(record=f.AttendanceRecordFactory(day=submitted))
    f.PayPeriodFactory(site=site, start_date=D(2026, 10, 26), end_date=D(2026, 10, 26))
    client.force_login(admin)

    html = client.get(reverse("dashboard:home")).content.decode()

    assert f'href="{reverse("worksites:detail", args=[site.pk])}"' in html  # no roll call
    assert f'href="{reverse("workers:detail", args=[worker.pk])}"' in html  # absent streak
    assert f'href="{reverse("attendance:approvals")}?day={submitted.pk}"' in html
    assert f'href="{reverse("payroll:cash_sheet", args=[site.pk, "2026-10-26"])}"' in html
