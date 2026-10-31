"""Audit log viewer: filters and the before/after diff."""

import datetime

import pytest
from django.contrib.contenttypes.models import ContentType
from django.urls import reverse
from django.utils import timezone

from apps.core.models import AuditLog
from apps.core.services import audit_diff
from apps.worksites.models import Site

from . import factories as f
from .test_settings_view import form_data

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _fast_passwords(settings):
    settings.PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]


@pytest.fixture
def entries():
    alice = f.SiteManagerFactory(username="alice", first_name="Alice", last_name="Mensah")
    bob = f.SiteManagerFactory(username="bob", first_name="Bob", last_name="Kpodar")
    site = f.SiteFactory(name="Riverside")
    record = f.AttendanceRecordFactory()
    first = AuditLog.record(
        actor=alice,
        action="site.update",
        target=site,
        before={"name": "Old name", "status": "active"},
        after={"name": "Riverside", "status": "active"},
    )
    second = AuditLog.record(
        actor=bob,
        action="attendance.correct",
        target=record,
        before={"status": "absent"},
        after={"status": "present"},
        reason="Arrived after the roll call.",
    )
    AuditLog.objects.filter(pk=first.pk).update(
        created_at=timezone.make_aware(datetime.datetime(2026, 10, 1, 9, 0))
    )
    AuditLog.objects.filter(pk=second.pk).update(
        created_at=timezone.make_aware(datetime.datetime(2026, 10, 20, 9, 0))
    )
    return {"alice": alice, "bob": bob, "first": first, "second": second}


@pytest.fixture
def admin_client(client):
    client.force_login(f.AdminUserFactory(username="admin"))
    return client


def listed(response):
    return [entry.pk for entry, _diff in response.context["rows"]]


def test_lists_newest_first_with_diff(admin_client, entries):
    response = admin_client.get(reverse("core:audit_log"))

    html = response.content.decode()
    assert response.status_code == 200
    assert listed(response) == [entries["second"].pk, entries["first"].pk]
    assert "attendance.correct" in html
    assert "Arrived after the roll call." in html
    assert "Old name" in html and "Riverside" in html
    assert "Alice Mensah" in html


def test_filter_by_actor(admin_client, entries):
    response = admin_client.get(reverse("core:audit_log"), {"actor": entries["alice"].pk})

    assert listed(response) == [entries["first"].pk]


def test_filter_by_target_type(admin_client, entries):
    site_type = ContentType.objects.get_for_model(Site)

    response = admin_client.get(reverse("core:audit_log"), {"target_type": site_type.pk})

    assert listed(response) == [entries["first"].pk]


def test_filter_by_date(admin_client, entries):
    response = admin_client.get(
        reverse("core:audit_log"), {"date_from": "2026-10-15", "date_to": "2026-10-20"}
    )

    assert listed(response) == [entries["second"].pk]


def test_bad_date_range_shows_error_and_everything(admin_client, entries):
    response = admin_client.get(
        reverse("core:audit_log"), {"date_from": "2026-10-20", "date_to": "2026-10-01"}
    )

    assert "date_to" in response.context["form"].errors
    assert len(listed(response)) == 2


def test_settings_save_shows_in_the_log(admin_client):
    admin_client.post(reverse("core:settings"), form_data(company_name="Polaris BTP"))

    html = admin_client.get(reverse("core:audit_log")).content.decode()
    assert "settings.update" in html
    assert "Polaris BTP" in html


def test_empty_log(admin_client):
    response = admin_client.get(reverse("core:audit_log"))

    assert "No audit entries" in response.content.decode()


def test_audit_diff_rows():
    rows = audit_diff({"a": 1, "b": [1, 2]}, {"a": 1, "b": [1], "c": "new"})

    assert [(row.key, row.before, row.after, row.changed) for row in rows] == [
        ("a", "1", "1", False),
        ("b", "[1, 2]", "[1]", True),
        ("c", "—", "new", True),
    ]
    assert audit_diff(None, None) == []
    assert audit_diff([1], [2])[0].changed
