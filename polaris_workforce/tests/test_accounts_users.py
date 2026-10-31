"""Users & roles screen: admin only, invite/edit, site assignment, PINs and phones."""

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.accounts import services
from apps.accounts.models import User
from apps.core.models import AuditLog
from apps.worksites.models import Site
from tests.factories import (
    AdminUserFactory,
    ForemanDeviceFactory,
    ForemanFactory,
    SiteFactory,
    SiteManagerFactory,
    UserFactory,
)

Role = User.Role


@pytest.fixture
def admin(db):
    return UserFactory(role=Role.ADMIN, first_name="Edem", last_name="Kpodar")


@pytest.fixture
def admin_client(client, admin):
    client.force_login(admin)
    return client


def _user_data(**overrides):
    data = {
        "first_name": "Kossi",
        "last_name": "Amegah",
        "username": "kossi",
        "email": "",
        "phone": "+228 91 05 66 23",
        "role": Role.FOREMAN,
        "language": "fr",
        "is_active": "on",
    }
    data.update(overrides)
    return {key: value for key, value in data.items() if value is not None}


# --- Access ----------------------------------------------------------------------------


@pytest.mark.parametrize("role", [Role.SITE_MANAGER, Role.FOREMAN, Role.ACCOUNTANT])
@pytest.mark.parametrize(
    ("name", "kwargs", "method"),
    [
        ("accounts:users", {}, "get"),
        ("accounts:user_create", {}, "get"),
        ("accounts:user_edit", {"pk": 1}, "get"),
        ("accounts:user_set_pin", {"pk": 1}, "post"),
        ("accounts:device_revoke", {"pk": 1, "device_pk": 1}, "post"),
    ],
)
def test_non_admins_get_403(client, db, role, name, kwargs, method):
    client.force_login(UserFactory(role=role))

    response = getattr(client, method)(reverse(name, kwargs=kwargs))

    assert response.status_code == 403


def test_anonymous_is_sent_to_sign_in(client, db):
    response = client.get(reverse("accounts:users"))

    assert response.status_code == 302
    assert response.url.startswith(reverse("login"))


def test_superuser_without_admin_role_is_allowed(client, db):
    client.force_login(AdminUserFactory(role=Role.FOREMAN))

    assert client.get(reverse("accounts:users")).status_code == 200


# --- List ------------------------------------------------------------------------------


def test_list_shows_users_roles_sites_and_matrix(admin_client, admin):
    manager = SiteManagerFactory(first_name="Ama", last_name="Fiagbe")
    foreman = ForemanFactory(first_name="Kossi", last_name="Amegah")
    foreman.set_pin("4821")
    foreman.save()
    SiteFactory(name="Riverside", manager=manager, foremen=[foreman])
    ForemanDeviceFactory(user=foreman, last_seen_at=timezone.now())

    response = admin_client.get(reverse("accounts:users"))
    html = response.content.decode()

    assert response.status_code == 200
    assert "Ama Fiagbe" in html and "Kossi Amegah" in html
    assert "Riverside" in html
    assert "4-digit PIN" in html
    assert "What each role can do" in html
    assert "Close &amp; lock pay periods" in html
    assert reverse("accounts:user_edit", kwargs={"pk": foreman.pk}) in html
    assert 'href="/users/" aria-current="page"' in html


# --- Invite and edit --------------------------------------------------------------------


def test_invite_foreman_with_sites(admin_client, admin):
    riverside = SiteFactory(name="Riverside")
    other = SiteFactory(name="Market Hall")

    response = admin_client.post(
        reverse("accounts:user_create"),
        _user_data(foreman_sites=[riverside.pk], managed_sites=[other.pk]),
    )

    user = User.objects.get(username="kossi")
    assert response.status_code == 302
    assert response.url == reverse("accounts:user_edit", kwargs={"pk": user.pk})
    assert user.role == Role.FOREMAN
    assert list(user.foreman_sites.all()) == [riverside]
    # Managed sites are only kept for site managers.
    assert Site.objects.get(pk=other.pk).manager != user
    assert not user.has_usable_password()
    entry = AuditLog.objects.get(action=services.AUDIT_USER_INVITE)
    assert entry.actor == admin
    assert entry.after["foreman_sites"] == [riverside.pk]


def test_invite_office_user_with_temporary_password(admin_client):
    admin_client.post(
        reverse("accounts:user_create"),
        _user_data(username="ama", phone="", role=Role.SITE_MANAGER, password="Tempor4ry-pass"),
    )

    assert User.objects.get(username="ama").check_password("Tempor4ry-pass")


def test_foreman_needs_a_phone(admin_client):
    response = admin_client.post(reverse("accounts:user_create"), _user_data(phone=""))

    assert response.status_code == 200
    assert "Foremen need a phone number" in response.content.decode()
    assert not User.objects.filter(username="kossi").exists()


def test_phone_must_be_unique_whatever_the_spacing(admin_client):
    ForemanFactory(phone="+22891056623")

    response = admin_client.post(reverse("accounts:user_create"), _user_data())

    assert "Another user already has this phone number" in response.content.decode()


def test_edit_assigns_managed_sites_through_the_site(admin_client, admin):
    manager = SiteManagerFactory()
    old_site = SiteFactory(manager=manager)
    new_site = SiteFactory()

    response = admin_client.post(
        reverse("accounts:user_edit", kwargs={"pk": manager.pk}),
        _user_data(
            username=manager.username,
            phone="",
            role=Role.SITE_MANAGER,
            managed_sites=[new_site.pk],
        ),
    )

    assert response.status_code == 302
    assert Site.objects.get(pk=new_site.pk).manager == manager
    assert Site.objects.get(pk=old_site.pk).manager is None
    entry = AuditLog.objects.get(action=services.AUDIT_SITES_CHANGE)
    assert entry.before["managed_sites"] == [old_site.pk]
    assert entry.after["managed_sites"] == [new_site.pk]


def test_role_change_is_audited_and_signs_out_phones(admin_client, admin):
    foreman = ForemanFactory()
    device = ForemanDeviceFactory(user=foreman)

    admin_client.post(
        reverse("accounts:user_edit", kwargs={"pk": foreman.pk}),
        _user_data(username=foreman.username, phone=foreman.phone, role=Role.ACCOUNTANT),
    )

    entry = AuditLog.objects.get(action=services.AUDIT_ROLE_CHANGE)
    assert entry.actor == admin
    assert entry.before == {"role": "foreman"} and entry.after == {"role": "accountant"}
    device.refresh_from_db()
    assert device.revoked_at is not None


def test_disabling_a_user_is_audited_and_signs_out_phones(admin_client):
    foreman = ForemanFactory()
    device = ForemanDeviceFactory(user=foreman)

    admin_client.post(
        reverse("accounts:user_edit", kwargs={"pk": foreman.pk}),
        _user_data(username=foreman.username, phone=foreman.phone, is_active=None),
    )

    foreman.refresh_from_db()
    device.refresh_from_db()
    assert not foreman.is_active
    assert device.revoked_at is not None
    assert AuditLog.objects.filter(action=services.AUDIT_ACTIVE_CHANGE).exists()


def test_admin_cannot_demote_or_disable_themselves(admin_client, admin):
    response = admin_client.post(
        reverse("accounts:user_edit", kwargs={"pk": admin.pk}),
        _user_data(username=admin.username, phone="", role=Role.FOREMAN, is_active=None),
    )

    html = response.content.decode()
    assert response.status_code == 200
    assert "You cannot change your own role" in html
    assert "You cannot disable your own account" in html
    admin.refresh_from_db()
    assert admin.role == Role.ADMIN and admin.is_active


# --- PIN -------------------------------------------------------------------------------


def test_set_pin_hashes_it_unlocks_and_audits(admin_client, admin):
    foreman = ForemanFactory(pin_failed_attempts=5, pin_locked_until=timezone.now())
    url = reverse("accounts:user_set_pin", kwargs={"pk": foreman.pk})

    response = admin_client.post(url, {"pin": "4821", "pin_confirm": "4821"})

    assert response.status_code == 302
    foreman.refresh_from_db()
    assert foreman.check_pin("4821")
    assert "4821" not in foreman.pin_hash
    assert foreman.pin_failed_attempts == 0 and foreman.pin_locked_until is None
    entry = AuditLog.objects.get(action=services.AUDIT_PIN_SET)
    assert entry.actor == admin
    assert "4821" not in str(entry.before) + str(entry.after) + entry.reason

    admin_client.post(url, {"pin": "7350", "pin_confirm": "7350"})

    assert AuditLog.objects.filter(action=services.AUDIT_PIN_RESET).count() == 1


@pytest.mark.parametrize(
    ("pin", "confirm", "message"),
    [
        ("12", "12", "Enter exactly 4 digits."),
        ("abcd", "abcd", "Enter exactly 4 digits."),
        ("1111", "1111", "Too easy to guess"),
        ("1234", "1234", "Too easy to guess"),
        ("4821", "4812", "The two PINs are different."),
    ],
)
def test_bad_pins_are_refused(admin_client, pin, confirm, message):
    foreman = ForemanFactory()

    response = admin_client.post(
        reverse("accounts:user_set_pin", kwargs={"pk": foreman.pk}),
        {"pin": pin, "pin_confirm": confirm},
    )

    assert response.status_code == 400
    assert message in response.content.decode()
    foreman.refresh_from_db()
    assert not foreman.has_pin


def test_office_roles_cannot_get_a_pin(admin_client):
    manager = SiteManagerFactory()

    admin_client.post(
        reverse("accounts:user_set_pin", kwargs={"pk": manager.pk}),
        {"pin": "4821", "pin_confirm": "4821"},
    )

    manager.refresh_from_db()
    assert not manager.has_pin


# --- Phones ----------------------------------------------------------------------------


def test_edit_page_lists_devices(admin_client):
    foreman = ForemanFactory()
    ForemanDeviceFactory(user=foreman, name="Tecno Spark 10")

    html = admin_client.get(reverse("accounts:user_edit", kwargs={"pk": foreman.pk})).content
    html = html.decode()

    assert "Tecno Spark 10" in html
    assert "Set phone PIN" in html


def test_revoke_device_from_the_users_screen(admin_client, admin):
    device = ForemanDeviceFactory(raw_token="raw-token-1")

    response = admin_client.post(
        reverse("accounts:device_revoke", kwargs={"pk": device.user_id, "device_pk": device.pk})
    )

    assert response.status_code == 302
    device.refresh_from_db()
    assert device.revoked_at is not None
    entry = AuditLog.objects.get(action=services.AUDIT_DEVICE_REVOKE)
    assert entry.actor == admin and entry.target == device
    api = admin_client.__class__()
    assert api.get("/api/v1/me/", HTTP_AUTHORIZATION="Device raw-token-1").status_code == 401


def test_revoke_checks_the_device_belongs_to_the_user(admin_client):
    device = ForemanDeviceFactory()
    other = ForemanFactory()

    response = admin_client.post(
        reverse("accounts:device_revoke", kwargs={"pk": other.pk, "device_pk": device.pk})
    )

    assert response.status_code == 404
    device.refresh_from_db()
    assert device.revoked_at is None
