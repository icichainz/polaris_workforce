"""The presence-tracking settings: company → site → worker, and where they are edited.

``tracking_enabled`` has all three levels; the thresholds, the GPS interval and the
tracking hours have company and site; the retention is company-wide. Each is edited on
the existing settings screens (company settings, a site's settings tab, the worker form)
and audited there.
"""

import datetime

import pytest
from django.core.exceptions import ValidationError
from django.urls import reverse

from apps.core.models import AuditLog, CompanySettings
from apps.core.settings import (
    SITE_OVERRIDABLE,
    TRACKING_SETTINGS,
    WORKER_OVERRIDABLE,
    resolve_setting,
)
from apps.worksites.forms import SiteSettingsForm
from tests.factories import AdminUserFactory, SiteFactory, WorkerFactory
from tests.test_settings_view import form_data

pytestmark = pytest.mark.django_db

DEFAULTS = {
    "tracking_enabled": False,
    "presence_sightings_to_enter": 2,
    "presence_arrival_window_seconds": 300,
    "presence_absence_to_leave_seconds": 900,
    "foreman_gps_interval_seconds": 60,
    "tracking_start": datetime.time(6, 0),
    "tracking_end": datetime.time(19, 0),
    "track_retention_days": 30,
}


def test_defaults_are_the_design_ones():
    company = CompanySettings.load()
    assert {name: getattr(company, name) for name in DEFAULTS} == DEFAULTS
    assert set(DEFAULTS) == TRACKING_SETTINGS


def test_levels_of_each_setting():
    assert TRACKING_SETTINGS - {"track_retention_days"} <= SITE_OVERRIDABLE
    assert "track_retention_days" not in SITE_OVERRIDABLE
    assert TRACKING_SETTINGS & WORKER_OVERRIDABLE == {"tracking_enabled"}


@pytest.mark.parametrize(
    ("company_value", "site_value", "worker_value", "expected"),
    [
        (False, None, None, False),
        (True, None, None, True),
        (True, False, None, False),  # off on the site beats on for the company
        (False, True, None, True),
        (True, True, False, False),  # the worker's own switch wins
        (False, False, True, True),
        (True, False, True, True),
    ],
)
def test_tracking_enabled_resolves_company_site_worker(
    company_value, site_value, worker_value, expected
):
    company = CompanySettings.load()
    company.tracking_enabled = company_value
    company.save()
    site = SiteFactory(tracking_enabled=site_value)
    worker = WorkerFactory(tracking_enabled=worker_value)

    assert resolve_setting("tracking_enabled", site=site, worker=worker) is expected


def test_site_overrides_the_thresholds_and_hours_but_a_worker_cannot():
    site = SiteFactory(
        presence_absence_to_leave_seconds=1200,
        tracking_start=datetime.time(5, 30),
        foreman_gps_interval_seconds=30,
    )
    worker = WorkerFactory()

    assert resolve_setting("presence_absence_to_leave_seconds", site=site) == 1200
    assert resolve_setting("presence_sightings_to_enter", site=site) == 2
    assert resolve_setting("tracking_start", site=site, worker=worker) == datetime.time(5, 30)
    assert resolve_setting("foreman_gps_interval_seconds", site=site, worker=worker) == 30
    assert resolve_setting("track_retention_days", site=site) == 30


def test_tracking_must_end_after_it_starts():
    company = CompanySettings.load()
    company.tracking_start = datetime.time(19, 0)
    company.tracking_end = datetime.time(6, 0)
    with pytest.raises(ValidationError) as raised:
        company.full_clean()
    assert "tracking_end" in raised.value.message_dict


# --- the company settings screen -----------------------------------------------------------------


@pytest.fixture
def admin_client(client):
    client.force_login(AdminUserFactory())
    return client


def test_company_settings_screen_shows_and_saves_tracking(admin_client):
    page = admin_client.get(reverse("core:settings")).content.decode()
    assert 'id="tracking"' in page
    assert 'name="tracking_enabled"' in page
    assert 'name="track_retention_days"' in page

    response = admin_client.post(
        reverse("core:settings"),
        form_data(
            tracking_enabled="on",
            tracking_start="05:30",
            presence_sightings_to_enter="3",
            track_retention_days="14",
        ),
    )

    assert response.status_code == 302
    company = CompanySettings.load()
    assert company.tracking_enabled is True
    assert company.tracking_start == datetime.time(5, 30)
    assert company.presence_sightings_to_enter == 3
    assert company.track_retention_days == 14
    entry = AuditLog.objects.get(action="settings.update")
    assert entry.after["tracking_enabled"] is True
    assert entry.after["tracking_start"] == "05:30"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("tracking_end", "05:00"),
        ("presence_sightings_to_enter", "0"),
        ("foreman_gps_interval_seconds", "5"),
        ("track_retention_days", "1000"),
    ],
)
def test_company_settings_screen_refuses_bad_tracking_values(admin_client, field, value):
    response = admin_client.post(reverse("core:settings"), form_data(**{field: value}))

    assert response.status_code == 200
    assert field in response.context["form"].errors
    assert not AuditLog.objects.filter(action="settings.update").exists()


# --- a site's settings tab ----------------------------------------------------------------------


def test_site_settings_tab_saves_tracking_overrides(admin_client):
    site = SiteFactory()
    url = reverse("worksites:settings", args=[site.pk])
    assert 'name="tracking_enabled"' in admin_client.get(url).content.decode()

    response = admin_client.post(
        url,
        {"tracking_enabled": "True", "tracking_end": "18:00", "presence_sightings_to_enter": "4"},
    )

    assert response.status_code == 302
    site.refresh_from_db()
    assert site.tracking_enabled is True
    assert site.tracking_end == datetime.time(18, 0)
    assert site.presence_sightings_to_enter == 4
    entry = AuditLog.objects.get(action="worksites.settings_update")
    assert entry.after["tracking_enabled"] is True

    admin_client.post(url, {"tracking_enabled": "False"})
    site.refresh_from_db()
    assert site.tracking_enabled is False
    assert site.tracking_end is None  # blank inherits again

    admin_client.post(url, {"tracking_enabled": ""})
    site.refresh_from_db()
    assert site.tracking_enabled is None


def test_site_settings_form_shows_the_saved_switch_and_checks_the_hours():
    company = CompanySettings.load()
    site = SiteFactory(tracking_enabled=False)
    form = SiteSettingsForm(instance=site, company=company)
    assert '<option value="False" selected>' in str(form["tracking_enabled"])

    bad = SiteSettingsForm({"tracking_start": "20:00"}, instance=site, company=company)
    assert not bad.is_valid()
    assert "tracking_end" in bad.errors


# --- the worker form ----------------------------------------------------------------------------


def test_worker_form_saves_the_tracking_override(admin_client):
    worker = WorkerFactory(first_name="Yao", last_name="Mensah")
    url = reverse("workers:edit", args=[worker.pk])
    assert 'name="tracking_enabled"' in admin_client.get(url).content.decode()
    data = {
        "first_name": "Yao",
        "last_name": "Mensah",
        "phone": worker.phone,
        "trade": "",
        "id_document_type": worker.id_document_type,
        "id_document_number": "",
        "is_active": "on",
        "site": "",
        "start_date": "",
        "pay_type": "daily",
        "rate": "5000",
        "effective_from": "2026-11-01",
        "payment_method": "",
        "pay_frequency": "",
        "tracking_enabled": "False",
    }

    response = admin_client.post(url, data)

    assert response.status_code == 302, response.context["form"].errors
    worker.refresh_from_db()
    assert worker.tracking_enabled is False
    entry = AuditLog.objects.filter(action="workers.update").latest("pk")
    assert entry.before["tracking_enabled"] is None
    assert entry.after["tracking_enabled"] is False
