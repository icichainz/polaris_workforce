"""Company settings screen: access, rendering, validation and the audit trail."""

import datetime
from decimal import Decimal

import pytest
from django.urls import reverse

from apps.core.forms import CompanySettingsForm, text_to_holidays
from apps.core.models import AuditLog, CompanySettings
from apps.core.services import settings_snapshot

from . import factories as f

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _fast_passwords(settings):
    # The factories hash a password for every user; the default hasher is slow on purpose.
    settings.PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]


def form_data(company=None, **changes):
    """POST data for the settings form with the current values, plus ``changes``."""
    company = company or CompanySettings.load()
    data = {}
    form = CompanySettingsForm(instance=company)
    for name, field in form.fields.items():
        value = form.initial.get(name, field.initial)
        if isinstance(value, list):
            data[name] = [str(v) for v in value]
        elif isinstance(value, bool):
            if value:
                data[name] = "on"
        elif isinstance(value, datetime.time):
            data[name] = value.strftime("%H:%M")
        elif value is None:
            data[name] = ""
        else:
            data[name] = str(value)
    for name, value in changes.items():
        if value is None:
            data.pop(name, None)
        else:
            data[name] = value
    return data


@pytest.fixture
def admin_client(client):
    admin = f.AdminUserFactory(username="admin")
    client.force_login(admin)
    client.user = admin
    return client


@pytest.mark.parametrize(
    "user_factory",
    [f.SiteManagerFactory, f.ForemanFactory],
)
@pytest.mark.parametrize("url_name", ["core:settings", "core:audit_log"])
def test_non_admins_get_403(client, user_factory, url_name):
    client.force_login(user_factory())

    assert client.get(reverse(url_name)).status_code == 403
    assert client.post(reverse(url_name), {}).status_code == 403


def test_accountant_gets_403(client):
    client.force_login(f.UserFactory(role="accountant"))

    assert client.get(reverse("core:settings")).status_code == 403


def test_role_admin_without_superuser_flag_can_open(client):
    client.force_login(f.UserFactory(role="admin"))

    assert client.get(reverse("core:settings")).status_code == 200


def test_settings_page_renders_every_section(admin_client):
    html = admin_client.get(reverse("core:settings")).content.decode()

    for anchor in ("company", "attendance", "pay", "payments", "holidays"):
        assert f'id="{anchor}"' in html
        assert f'href="#{anchor}"' in html
    assert "Save settings" in html
    assert "coming later" in html
    assert "keep their original currency" in html
    assert 'name="number_format"' in html
    assert 'name="default_advance_deduction" value="2000"' in html


def test_unchanged_form_saves_nothing(admin_client):
    response = admin_client.post(reverse("core:settings"), form_data())

    assert response.status_code == 302
    assert not AuditLog.objects.exists()


def test_save_writes_audit_log_with_before_and_after(admin_client):
    data = form_data(
        company_name="Polaris BTP",
        overtime_multiplier="2",
        late_after="07:30",
        working_days=["1", "2", "3", "4", "5"],
    )

    response = admin_client.post(reverse("core:settings"), data)

    assert response.status_code == 302
    company = CompanySettings.load()
    assert company.company_name == "Polaris BTP"
    assert company.overtime_multiplier == Decimal("2")
    assert company.late_after == datetime.time(7, 30)
    assert company.working_days == [1, 2, 3, 4, 5]
    entry = AuditLog.objects.get()
    assert entry.action == "settings.update"
    assert entry.actor == admin_client.user
    assert entry.target == company
    assert entry.company == company.company
    assert company.company.name == "Polaris BTP"  # the switcher name follows
    assert entry.before == {
        "company_name": "Polaris Construction",  # a new row starts with the company name
        "overtime_multiplier": "1.5",
        "late_after": "07:15",
        "working_days": [1, 2, 3, 4, 5, 6],
    }
    assert entry.after == {
        "company_name": "Polaris BTP",
        "overtime_multiplier": "2",
        "late_after": "07:30",
        "working_days": [1, 2, 3, 4, 5],
    }


@pytest.mark.parametrize(
    ("changes", "field"),
    [
        ({"overtime_multiplier": "0.9"}, "overtime_multiplier"),
        ({"advance_limit_percent": "101"}, "advance_limit_percent"),
        ({"advance_limit_percent": "-1"}, "advance_limit_percent"),
        ({"half_day_units": "1.5"}, "half_day_units"),
        ({"currency": "francs"}, "currency"),
        ({"work_end": "06:00"}, "work_end"),
        ({"working_days": None}, "working_days"),
        ({"pay_frequency": "every_n_days", "pay_every_n_days": ""}, "pay_every_n_days"),
        ({"public_holidays": "Christmas 2026-12-25"}, "public_holidays"),
        ({"default_advance_deduction": "12.5"}, "default_advance_deduction"),
    ],
)
def test_invalid_values_are_rejected(admin_client, changes, field):
    before = settings_snapshot(CompanySettings.load())

    response = admin_client.post(reverse("core:settings"), form_data(**changes))

    assert response.status_code == 200
    assert field in response.context["form"].errors
    assert settings_snapshot(CompanySettings.load()) == before
    assert not AuditLog.objects.exists()


def test_boundary_values_are_accepted(admin_client):
    data = form_data(overtime_multiplier="1", advance_limit_percent="0")

    assert admin_client.post(reverse("core:settings"), data).status_code == 302
    company = CompanySettings.load()
    assert company.overtime_multiplier == Decimal("1")
    assert company.advance_limit_percent == 0


def test_currency_change_converts_deduction_and_warns(admin_client):
    data = form_data(currency="EUR", currency_decimals="2", default_advance_deduction="12.50")

    response = admin_client.post(reverse("core:settings"), data, follow=True)

    company = CompanySettings.load()
    assert (company.currency, company.currency_decimals) == ("EUR", 2)
    assert company.default_advance_deduction == 1250
    messages = [str(m) for m in response.context["messages"]]
    assert any("keep the currency they were created with" in m for m in messages)
    entry = AuditLog.objects.get()
    assert entry.before["currency"] == "XOF"
    assert entry.after["currency_decimals"] == 2


def test_locked_period_keeps_its_snapshot_after_currency_change(admin_client):
    period = f.PayPeriodFactory(currency="XOF", currency_decimals=0, status="locked")
    admin_client.post(reverse("core:settings"), form_data(currency="EUR", currency_decimals="2"))

    period.refresh_from_db()
    assert (period.currency, period.currency_decimals) == ("XOF", 0)


def test_public_holidays_round_trip(admin_client):
    data = form_data(public_holidays="2026-12-25 Christmas Day\n\n2026-11-01 All Saints")

    admin_client.post(reverse("core:settings"), data)

    company = CompanySettings.load()
    assert company.public_holidays == [
        {"date": "2026-11-01", "name": "All Saints"},
        {"date": "2026-12-25", "name": "Christmas Day"},
    ]
    assert not company.is_working_day(datetime.date(2026, 12, 25))
    html = admin_client.get(reverse("core:settings")).content.decode()
    assert "2026-11-01 All Saints\n2026-12-25 Christmas Day" in html


def test_text_to_holidays_deduplicates_dates():
    assert text_to_holidays("2026-01-01 New year\n2026-01-01 Jour de l'an") == [
        {"date": "2026-01-01", "name": "Jour de l'an"}
    ]


def test_an_invalid_form_does_not_leak_into_the_page_settings(admin_client):
    # The form edits its instance even when invalid; the page (money, language) must
    # keep reading the saved settings.
    saved = CompanySettings.load().currency

    response = admin_client.post(
        reverse("core:settings"), form_data(currency="francs", advance_limit_percent="101")
    )

    assert response.status_code == 200
    assert response.context["company_settings"].currency == saved


def test_a_page_reads_the_settings_once(admin_client):
    from django.db import connection
    from django.test.utils import CaptureQueriesContext

    with CaptureQueriesContext(connection) as queries:
        assert admin_client.get(reverse("dashboard:home")).status_code == 200
    reads = [q["sql"] for q in queries if 'FROM "core_companysettings"' in q["sql"]]
    assert len(reads) == 1
