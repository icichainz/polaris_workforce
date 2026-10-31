"""Emergency contact on the worker: form, profile, audit."""

import datetime
from unittest import mock

import pytest
from django.urls import reverse

from apps.core.models import AuditLog
from apps.workers.models import Worker
from tests.factories import (
    AdminUserFactory,
    PayRateFactory,
    SiteAssignmentFactory,
    SiteFactory,
    WorkerFactory,
)

pytestmark = pytest.mark.django_db

TODAY = datetime.date(2026, 10, 31)


@pytest.fixture(autouse=True)
def _fixed_today():
    with mock.patch("apps.core.dates.company_today", return_value=TODAY):
        yield


@pytest.fixture
def admin_client(client):
    client.force_login(AdminUserFactory())
    return client


def form_data(site, **extra):
    data = {
        "first_name": "Kafui",
        "last_name": "Amedome",
        "phone": "+228 99 34 25 71",
        "trade": "",
        "new_trade": "",
        "id_document_type": "national_id",
        "id_document_number": "",
        "site": site.pk,
        "start_date": "2026-11-02",
        "pay_type": "daily",
        "rate": "5000",
        "effective_from": "2026-11-02",
        "payment_method": "",
        "pay_frequency": "",
        "emergency_contact_name": "Afi Amedome",
        "emergency_contact_relationship": "Sister",
        "emergency_contact_phone": "+228 90 00 11 22",
    }
    data.update(extra)
    return data


def test_add_worker_with_an_emergency_contact(admin_client):
    site = SiteFactory()

    response = admin_client.post(reverse("workers:create"), form_data(site))

    assert response.status_code == 302
    worker = Worker.objects.get()
    assert worker.emergency_contact_name == "Afi Amedome"
    assert worker.emergency_contact_relationship == "Sister"
    assert worker.emergency_contact_phone == "+228 90 00 11 22"
    entry = AuditLog.objects.get(action="workers.create")
    assert entry.after["emergency_contact_name"] == "Afi Amedome"


def test_the_emergency_contact_is_optional(admin_client):
    site = SiteFactory()
    data = form_data(
        site,
        emergency_contact_name="",
        emergency_contact_relationship="",
        emergency_contact_phone="",
    )

    assert admin_client.post(reverse("workers:create"), data).status_code == 302
    assert Worker.objects.get().emergency_contact_name == ""


def test_profile_shows_the_emergency_contact(admin_client):
    worker = WorkerFactory(
        emergency_contact_name="Komla Mensah",
        emergency_contact_relationship="Brother",
        emergency_contact_phone="+228 91 22 33 44",
    )

    html = admin_client.get(reverse("workers:detail", args=[worker.pk])).content.decode()

    assert "Emergency contact" in html
    assert "Komla Mensah" in html and "Brother" in html
    assert 'href="tel:+22891223344"' in html


def test_profile_without_emergency_contact_invites_to_add_one(admin_client):
    worker = WorkerFactory()

    html = admin_client.get(reverse("workers:detail", args=[worker.pk])).content.decode()

    assert "No emergency contact yet." in html


def test_editing_the_emergency_contact_is_audited(admin_client):
    site = SiteFactory()
    worker = WorkerFactory(emergency_contact_name="Old name")
    SiteAssignmentFactory(site=site, worker=worker, start_date=datetime.date(2026, 3, 1))
    PayRateFactory(worker=worker, amount=5000, effective_from=datetime.date(2026, 3, 1))
    data = form_data(site)
    data.update(
        first_name=worker.first_name,
        last_name=worker.last_name,
        phone=worker.phone,
        site="",
        start_date="",
        rate="5000",
        effective_from="2026-10-31",
        is_active="on",
        emergency_contact_name="New name",
    )

    response = admin_client.post(reverse("workers:edit", args=[worker.pk]), data)

    assert response.status_code == 302
    worker.refresh_from_db()
    assert worker.emergency_contact_name == "New name"
    entry = AuditLog.objects.get(action="workers.update")
    assert entry.before["emergency_contact_name"] == "Old name"
    assert entry.after["emergency_contact_name"] == "New name"


def test_the_form_shows_the_emergency_fields(admin_client):
    SiteFactory()
    html = admin_client.get(reverse("workers:create")).content.decode()

    for name in (
        "emergency_contact_name",
        "emergency_contact_relationship",
        "emergency_contact_phone",
    ):
        assert f'name="{name}"' in html
