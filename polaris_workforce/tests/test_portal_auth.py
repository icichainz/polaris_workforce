"""Worker portal sign-in: phone + PIN, wrong-PIN countdown, lockout, per-IP limit, weak
PINs, disabled access, and a session kept apart from the office sign-in.

The helpers here (``PortalClient``, ``give``) are reused by the other portal tests.
"""

import datetime

import pytest
from django.core.cache import cache
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from apps.core.models import AuditLog, Company
from apps.portal import services
from apps.portal.models import WorkerAccess
from tests.factories import AdminUserFactory, CompanySettingsFactory, WorkerFactory

pytestmark = pytest.mark.django_db

PIN = "4826"


@pytest.fixture(autouse=True)
def _clear_cache():
    cache.clear()
    yield
    cache.clear()


class PortalClient:
    """A worker's phone browser on the portal, with CSRF checks on."""

    def __init__(self, client=None):
        self.client = client or Client(enforce_csrf_checks=True)

    @property
    def csrf(self) -> str:
        if "csrftoken" not in self.client.cookies:
            self.client.get(reverse("portal:sign_in"))
        return self.client.cookies["csrftoken"].value

    def get(self, url, data=None, **extra):
        return self.client.get(url, data or {}, **extra)

    def post(self, url, data=None, **extra):
        data = dict(data or {})
        data.setdefault("csrfmiddlewaretoken", self.csrf)
        return self.client.post(url, data, **extra)

    def sign_in(self, phone, pin, **extra):
        return self.post(reverse("portal:sign_in"), {"phone": phone, "pin": pin}, **extra)


def give(worker, pin=PIN, actor=None):
    """Give ``worker`` portal access with ``pin`` (as the office would)."""
    actor = actor or AdminUserFactory(company=worker.company)
    access, _pin = services.give_access(worker, actor=actor, pin=pin)
    return access


def signed_in(worker, pin=PIN) -> PortalClient:
    portal = PortalClient()
    response = portal.sign_in(worker.phone, pin)
    assert response.status_code == 302, response.content
    assert response["Location"] == reverse("portal:home")
    return portal


@pytest.fixture
def worker(db):
    CompanySettingsFactory()
    worker = WorkerFactory(phone="+228 90 11 22 33")
    give(worker)
    return worker


# --- sign-in ------------------------------------------------------------------------------


def test_good_pin_signs_in_with_any_phone_spelling(worker):
    portal = PortalClient()
    response = portal.sign_in("+228 90-11-22-33", PIN)
    assert response.status_code == 302
    home = portal.get(reverse("portal:home"))
    assert home.status_code == 200
    assert worker.full_name in home.content.decode()
    access = WorkerAccess.objects.get(worker=worker)
    assert access.last_sign_in_at is not None
    assert access.failed_attempts == 0


def test_sign_in_page_renders_with_the_language_switch(db):
    response = PortalClient().get(reverse("portal:sign_in"))
    assert response.status_code == 200
    page = response.content.decode()
    assert 'name="language" value="fr"' in page
    assert "<script" not in page  # light page for slow networks
    assert "no-store" in response["Cache-Control"]


def test_wrong_pin_counts_down_then_locks(worker):
    portal = PortalClient()
    for left in (4, 3, 2, 1):
        response = portal.sign_in(worker.phone, "1357")
        assert response.status_code == 400
        assert f"{left} tr" in response.content.decode()
    response = portal.sign_in(worker.phone, "1357")
    assert response.status_code == 423
    assert "Too many wrong PINs" in response.content.decode()
    access = WorkerAccess.objects.get(worker=worker)
    assert access.is_locked
    entry = AuditLog.objects.get(action=services.AUDIT_PIN_LOCKED)
    assert entry.company_id == worker.company_id
    assert PIN not in str(entry.after) and access.pin_hash not in str(entry.after)

    # Locked: even the right PIN is refused.
    response = portal.sign_in(worker.phone, PIN)
    assert response.status_code == 423
    assert portal.get(reverse("portal:home")).status_code == 302


def test_the_lockout_ends_after_15_minutes(worker):
    WorkerAccess.objects.filter(worker=worker).update(
        failed_attempts=5, locked_until=timezone.now() - datetime.timedelta(seconds=1)
    )
    assert PortalClient().sign_in(worker.phone, PIN).status_code == 302
    assert WorkerAccess.objects.get(worker=worker).failed_attempts == 0


def test_a_good_pin_resets_the_count(worker):
    portal = PortalClient()
    portal.sign_in(worker.phone, "1357")
    portal.sign_in(worker.phone, "1357")
    assert portal.sign_in(worker.phone, PIN).status_code == 302
    assert WorkerAccess.objects.get(worker=worker).failed_attempts == 0


@pytest.mark.parametrize("phone", ["+228 99 99 99 99", "", "abc"])
def test_unknown_phone_is_refused_with_one_message(worker, phone):
    response = PortalClient().sign_in(phone, PIN)
    assert response.status_code == 400
    assert not AuditLog.objects.filter(action=services.AUDIT_PIN_LOCKED).exists()


def test_disabled_access_is_refused(worker):
    services.revoke_access(worker, actor=None)
    response = PortalClient().sign_in(worker.phone, PIN)
    assert response.status_code == 400
    assert "Wrong phone number or PIN" in response.content.decode()


def test_inactive_worker_or_company_is_refused(worker):
    worker.is_active = False
    worker.save()
    assert PortalClient().sign_in(worker.phone, PIN).status_code == 400
    worker.is_active = True
    worker.save()
    Company.objects.filter(pk=worker.company_id).update(is_active=False)
    assert PortalClient().sign_in(worker.phone, PIN).status_code == 400


def test_sign_in_is_limited_per_ip(worker):
    portal = PortalClient()
    for _ in range(services.SIGN_IN_RATE):
        assert portal.sign_in("+228 99 99 99 99", PIN).status_code == 400
    response = portal.sign_in(worker.phone, PIN)
    assert response.status_code == 429
    assert response["Retry-After"] == str(services.SIGN_IN_WINDOW)
    # Another address is not affected; a spoofed X-Forwarded-For does not help.
    spoofed = PortalClient().sign_in(worker.phone, PIN, HTTP_X_FORWARDED_FOR="10.9.9.9")
    assert spoofed.status_code == 429
    other = PortalClient().sign_in(worker.phone, PIN, REMOTE_ADDR="10.0.0.2")
    assert other.status_code == 302


def test_sign_in_and_sign_out_need_the_csrf_token(worker):
    client = Client(enforce_csrf_checks=True)
    response = client.post(reverse("portal:sign_in"), {"phone": worker.phone, "pin": PIN})
    assert response.status_code == 403
    portal = signed_in(worker)
    bare = Client(enforce_csrf_checks=True)
    bare.cookies = portal.client.cookies
    assert bare.post(reverse("portal:sign_out")).status_code == 403
    assert portal.get(reverse("portal:home")).status_code == 200


def test_sign_out_ends_the_session(worker):
    portal = signed_in(worker)
    response = portal.post(reverse("portal:sign_out"))
    assert response.status_code == 302
    assert portal.get(reverse("portal:home"))["Location"] == reverse("portal:sign_in")


# --- weak PINs ----------------------------------------------------------------------------


@pytest.mark.parametrize("pin", ["1111", "1234", "9876", "0000"])
def test_weak_pins_are_refused(worker, pin):
    with pytest.raises(services.AccessError):
        services.give_access(worker, actor=None, pin=pin)
    portal = signed_in(worker)
    response = portal.post(
        reverse("portal:change_pin"), {"current_pin": PIN, "pin": pin, "pin_confirm": pin}
    )
    assert response.status_code == 400
    assert "Too easy to guess" in response.content.decode()
    assert PortalClient().sign_in(worker.phone, PIN).status_code == 302


def test_generated_pins_are_never_weak():
    from apps.accounts.services import is_weak_pin

    pins = {services.generate_pin() for _ in range(300)}
    assert all(len(p) == 4 and p.isdigit() and not is_weak_pin(p) for p in pins)


def test_worker_chooses_their_own_pin(worker):
    portal = signed_in(worker)
    response = portal.post(
        reverse("portal:change_pin"), {"current_pin": PIN, "pin": "7305", "pin_confirm": "7305"}
    )
    assert response.status_code == 200
    assert portal.get(reverse("portal:home")).status_code == 200  # still signed in
    access = WorkerAccess.objects.get(worker=worker)
    assert access.pin_chosen_by_worker
    assert PortalClient().sign_in(worker.phone, PIN).status_code == 400
    assert PortalClient().sign_in(worker.phone, "7305").status_code == 302
    entry = AuditLog.objects.get(action=services.AUDIT_PIN_CHANGED)
    assert "7305" not in str(entry.after) + entry.reason


def test_wrong_current_pin_counts_as_a_wrong_pin(worker):
    portal = signed_in(worker)
    for _ in range(4):
        response = portal.post(
            reverse("portal:change_pin"),
            {"current_pin": "1357", "pin": "7305", "pin_confirm": "7305"},
        )
        assert response.status_code == 400
    response = portal.post(
        reverse("portal:change_pin"), {"current_pin": "1357", "pin": "7305", "pin_confirm": "7305"}
    )
    assert response["Location"] == reverse("portal:sign_in")
    assert WorkerAccess.objects.get(worker=worker).is_locked
    assert portal.get(reverse("portal:home")).status_code == 302


# --- session separation -------------------------------------------------------------------


def test_office_sign_in_does_not_open_the_portal(worker):
    client = Client()
    client.force_login(AdminUserFactory())
    response = client.get(reverse("portal:home"))
    assert response.status_code == 302
    assert response["Location"] == reverse("portal:sign_in")


def test_portal_sign_in_does_not_open_the_office(worker):
    portal = signed_in(worker)
    response = portal.get(reverse("dashboard:home"))
    assert response.status_code == 302
    assert response["Location"].startswith(reverse("login"))
    assert portal.get(reverse("portal:office_access")).status_code == 302


def test_portal_sign_in_signs_the_office_user_out_of_this_browser(worker):
    portal = PortalClient()
    portal.client.force_login(AdminUserFactory())
    assert portal.get(reverse("dashboard:home")).status_code == 200
    assert portal.sign_in(worker.phone, PIN).status_code == 302
    assert portal.get(reverse("dashboard:home")).status_code == 302
    assert portal.get(reverse("portal:home")).status_code == 200


def test_pin_reset_or_revoke_ends_open_sessions(worker):
    portal = signed_in(worker)
    services.give_access(worker, actor=None, pin="7305")
    assert portal.get(reverse("portal:home")).status_code == 302

    portal = signed_in(worker, "7305")
    services.revoke_access(worker, actor=None)
    assert portal.get(reverse("portal:pay")).status_code == 302


def test_a_tampered_session_worker_id_is_refused(worker):
    other = WorkerFactory()
    give(other, pin="5821")
    portal = signed_in(worker)
    session = portal.client.session
    session[services.SESSION_WORKER_KEY] = other.pk  # hash still the first worker's
    session.save()
    assert portal.get(reverse("portal:home")).status_code == 302
