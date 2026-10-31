"""Office side of the worker portal: who may give access, show-once PINs, revoke, the
phone rule (one enabled access per phone number, across companies), audit entries."""

import re

import pytest
from django.contrib.auth.hashers import check_password
from django.core.cache import cache
from django.db import IntegrityError, transaction
from django.urls import reverse

from apps.accounts.models import User
from apps.core.models import AuditLog
from apps.core.tenancy import use_company
from apps.portal import services
from apps.portal.models import WorkerAccess
from tests.e2e import Office
from tests.factories import (
    AdminUserFactory,
    CompanyFactory,
    CompanySettingsFactory,
    UserFactory,
    WorkerFactory,
)
from tests.test_payroll_services import hire, setup_site
from tests.test_portal_auth import PortalClient, give

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _clear_cache():
    cache.clear()
    yield
    cache.clear()


@pytest.fixture
def crews(db):
    CompanySettingsFactory()
    site_a, manager_a, foreman_a = setup_site(name="Alpha")
    site_b, manager_b, _foreman_b = setup_site(name="Bravo")
    ama = hire(site_a, first_name="Ama", phone="+228 90 00 00 01")
    zeb = hire(site_b, first_name="Zeb", last_name="Bravoworker", phone="+228 90 00 00 02")
    return {
        "admin": AdminUserFactory(),
        "manager_a": manager_a,
        "foreman_a": foreman_a,
        "manager_b": manager_b,
        "ama": ama,
        "zeb": zeb,
    }


def _pin_from(response) -> str:
    match = re.search(r"data-pin>(\d{4})<", response.content.decode())
    assert match, response.content
    return match.group(1)


def test_admin_gives_access_and_sees_the_pin_once(crews):
    office = Office(crews["admin"])
    response = office.post(reverse("portal:office_set_pin", args=[crews["ama"].pk]), {})
    assert response.status_code == 200
    assert "no-store" in response["Cache-Control"]
    pin = _pin_from(response)
    access = WorkerAccess.objects.get(worker=crews["ama"])
    assert access.phone == "+22890000001" and access.is_enabled
    assert access.company_id == crews["ama"].company_id
    assert check_password(pin, access.pin_hash) and pin not in access.pin_hash
    # Not shown again on the worker's page.
    page = office.get(reverse("portal:office_worker", args=[crews["ama"].pk]))
    assert page.status_code == 200 and "data-pin" not in page.content.decode()
    # The worker signs in with it.
    assert PortalClient().sign_in(crews["ama"].phone, pin).status_code == 302
    entry = AuditLog.objects.get(action=services.AUDIT_ACCESS_GIVEN)
    assert entry.actor == crews["admin"]
    assert pin not in str(entry.before) + str(entry.after)


def test_typed_pin_and_reset(crews):
    office = Office(crews["admin"])
    url = reverse("portal:office_set_pin", args=[crews["ama"].pk])
    response = office.post(url, {"pin": "4826", "pin_confirm": "4826"})
    assert _pin_from(response) == "4826"
    response = office.post(url, {"pin": "7305", "pin_confirm": "7305"})
    assert _pin_from(response) == "7305"
    assert AuditLog.objects.filter(action=services.AUDIT_PIN_RESET).count() == 1
    assert PortalClient().sign_in(crews["ama"].phone, "4826").status_code == 400
    assert PortalClient().sign_in(crews["ama"].phone, "7305").status_code == 302


@pytest.mark.parametrize(
    ("data", "message"),
    [
        ({"pin": "1234", "pin_confirm": "1234"}, "Too easy to guess"),
        ({"pin": "4826", "pin_confirm": "4827"}, "The two PINs are different"),
        ({"pin": "48a6", "pin_confirm": "48a6"}, "Enter exactly 4 digits"),
    ],
)
def test_bad_typed_pins_are_refused(crews, data, message):
    office = Office(crews["admin"])
    response = office.post(reverse("portal:office_set_pin", args=[crews["ama"].pk]), data)
    assert response.status_code == 400
    assert message in response.content.decode()
    assert not WorkerAccess.objects.exists()


def test_a_reset_unlocks_the_access(crews):
    give(crews["ama"])
    for _ in range(5):
        PortalClient().sign_in(crews["ama"].phone, "1357")
    assert WorkerAccess.objects.get(worker=crews["ama"]).is_locked
    office = Office(crews["admin"])
    office.post(
        reverse("portal:office_set_pin", args=[crews["ama"].pk]),
        {"pin": "7305", "pin_confirm": "7305"},
    )
    assert PortalClient().sign_in(crews["ama"].phone, "7305").status_code == 302


def test_revoke(crews):
    give(crews["ama"])
    office = Office(crews["admin"])
    response = office.post(reverse("portal:office_revoke", args=[crews["ama"].pk]))
    assert response.status_code == 302
    assert not WorkerAccess.objects.get(worker=crews["ama"]).is_enabled
    assert AuditLog.objects.filter(action=services.AUDIT_ACCESS_REVOKED).count() == 1
    assert PortalClient().sign_in(crews["ama"].phone, "4826").status_code == 400
    # Revoking again changes nothing.
    office.post(reverse("portal:office_revoke", args=[crews["ama"].pk]))
    assert AuditLog.objects.filter(action=services.AUDIT_ACCESS_REVOKED).count() == 1


def test_worker_without_phone_or_inactive_gets_a_clear_error(crews):
    office = Office(crews["admin"])
    crews["ama"].phone = ""
    crews["ama"].save()
    response = office.post(reverse("portal:office_set_pin", args=[crews["ama"].pk]), {})
    assert response.status_code == 400
    assert "phone number on their profile first" in response.content.decode()
    crews["ama"].phone = "+228 90 00 00 01"
    crews["ama"].is_active = False
    crews["ama"].save()
    response = office.post(reverse("portal:office_set_pin", args=[crews["ama"].pk]), {})
    assert response.status_code in (400, 404)
    assert not WorkerAccess.objects.exists()


# --- permissions --------------------------------------------------------------------------


def test_site_manager_manages_only_their_sites_workers(crews):
    office = Office(crews["manager_a"])
    listing = office.get(reverse("portal:office_access"))
    assert listing.status_code == 200
    text = listing.content.decode()
    assert "Ama" in text and "Bravoworker" not in text
    assert office.get(reverse("portal:office_worker", args=[crews["ama"].pk])).status_code == 200
    assert office.get(reverse("portal:office_worker", args=[crews["zeb"].pk])).status_code == 404
    for name in ("portal:office_set_pin", "portal:office_revoke"):
        assert office.post(reverse(name, args=[crews["zeb"].pk])).status_code == 404
    assert not WorkerAccess.objects.filter(worker=crews["zeb"]).exists()
    response = office.post(reverse("portal:office_set_pin", args=[crews["ama"].pk]))
    assert response.status_code == 200


@pytest.mark.parametrize("role", [User.Role.FOREMAN, User.Role.ACCOUNTANT])
def test_foremen_and_accountants_cannot_manage_access(crews, role):
    office = Office(UserFactory(role=role))
    assert office.get(reverse("portal:office_access")).status_code == 403
    assert office.get(reverse("portal:office_worker", args=[crews["ama"].pk])).status_code == 403
    assert office.post(reverse("portal:office_set_pin", args=[crews["ama"].pk])).status_code == 403
    assert office.post(reverse("portal:office_revoke", args=[crews["ama"].pk])).status_code == 403
    assert not WorkerAccess.objects.exists()


def test_office_pages_need_an_office_sign_in(crews):
    from django.test import Client

    response = Client().get(reverse("portal:office_access"))
    assert response.status_code == 302 and response["Location"].startswith(reverse("login"))


def test_sidebar_entry_for_admins_and_site_managers(crews):
    href = f'href="{reverse("portal:office_access")}"'
    assert href in Office(crews["admin"]).get(reverse("dashboard:home")).content.decode()
    assert href in Office(crews["manager_a"]).get(reverse("dashboard:home")).content.decode()
    foreman = Office(crews["foreman_a"]).get(reverse("dashboard:home")).content.decode()
    assert href not in foreman


def test_list_filters(crews):
    give(crews["ama"])
    office = Office(crews["admin"])
    with_access = office.get(reverse("portal:office_access"), {"show": "with"}).content.decode()
    assert "Ama" in with_access and "Bravoworker" not in with_access
    without = office.get(reverse("portal:office_access"), {"show": "without"}).content.decode()
    assert "Bravoworker" in without
    found = office.get(reverse("portal:office_access"), {"q": "bravo"}).content.decode()
    assert "Bravoworker" in found


# --- the phone rule -----------------------------------------------------------------------


def test_one_enabled_access_per_phone_in_the_company(crews):
    give(crews["ama"])
    twin = WorkerFactory(phone="+228-90-00-00-01")  # same number, other spelling
    office = Office(crews["admin"])
    response = office.post(reverse("portal:office_set_pin", args=[twin.pk]), {})
    assert response.status_code == 400
    assert "already signs in to the worker portal with this phone number" in (
        response.content.decode().replace("\n", " ")
    )
    assert not WorkerAccess.objects.filter(worker=twin).exists()
    # Once the first access is off, the number is free again.
    services.revoke_access(crews["ama"], actor=None)
    assert office.post(reverse("portal:office_set_pin", args=[twin.pk]), {}).status_code == 200


def test_phone_is_unique_across_companies(crews):
    give(crews["ama"])
    other = CompanyFactory(name="Other Co", slug="other")
    with use_company(other):
        CompanySettingsFactory(company_name="Other Co")
        admin = AdminUserFactory()
        twin = WorkerFactory(phone="+228 90 00 00 01")
    office = Office(admin)
    response = office.post(reverse("portal:office_set_pin", args=[twin.pk]), {})
    assert response.status_code == 400
    assert not WorkerAccess.objects.filter(worker=twin).exists()


def test_the_database_refuses_two_enabled_accesses_with_one_phone(crews):
    give(crews["ama"])
    with pytest.raises(IntegrityError), transaction.atomic():
        WorkerAccess.objects.create(
            company=crews["zeb"].company, worker=crews["zeb"], phone="+22890000001"
        )
    WorkerAccess.objects.create(
        company=crews["zeb"].company, worker=crews["zeb"], phone="+22890000001", is_enabled=False
    )


def test_profile_phone_change_is_flagged_until_the_pin_is_reset(crews):
    give(crews["ama"])
    crews["ama"].phone = "+228 90 00 00 09"
    crews["ama"].save()
    office = Office(crews["admin"])
    page = office.get(reverse("portal:office_worker", args=[crews["ama"].pk])).content.decode()
    assert "phone number on the profile changed" in page
    office.post(reverse("portal:office_set_pin", args=[crews["ama"].pk]), {})
    assert WorkerAccess.objects.get(worker=crews["ama"]).phone == "+22890000009"
