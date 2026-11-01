"""Foreman phone API: PIN sign-in with lockout, device tokens, logout and /me/."""

import datetime
from datetime import timedelta

import pytest
from django.core.cache import cache
from django.db import IntegrityError
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APIClient

from apps.accounts import services
from apps.accounts.models import ForemanDevice, User
from apps.api.authentication import hash_device_token
from apps.core.models import AuditLog
from apps.core.tenancy import use_company
from apps.worksites.models import Site
from tests.factories import (
    CompanyFactory,
    CompanySettingsFactory,
    ForemanFactory,
    SiteFactory,
    SiteManagerFactory,
    UserFactory,
)

PIN = "4821"
# Paths, not reverse(): resolving URLs at import time would load the URLconf while
# DEBUG is still on (see test_api_schema.test_swagger_ui_only_in_debug).
PIN_URL = "/api/v1/auth/pin/"
ME_URL = "/api/v1/me/"
LOGOUT_URL = "/api/v1/auth/logout/"


@pytest.fixture(autouse=True)
def _clear_throttle_cache():
    cache.clear()
    yield
    cache.clear()


@pytest.fixture
def company(db):
    return CompanySettingsFactory(country="TG", language="fr")


@pytest.fixture
def foreman(db):
    user = ForemanFactory(first_name="Kossi", last_name="Amegah", phone="+228 90 11 22 33")
    user.set_pin(PIN)
    user.save()
    return user


def sign_in(phone="+22890112233", pin=PIN, device_id="phone-1", client=None, **extra):
    body = {"phone": phone, "pin": pin, "device_id": device_id, **extra}
    return (client or APIClient()).post(PIN_URL, body, format="json")


def device_client(token):
    return APIClient(HTTP_AUTHORIZATION=f"Device {token}")


# --- Sign-in ---------------------------------------------------------------------------


def test_paths_match_the_url_names(db):
    assert reverse("api:accounts:auth-pin") == PIN_URL
    assert reverse("api:accounts:me") == ME_URL
    assert reverse("api:accounts:auth-logout") == LOGOUT_URL


def test_sign_in_returns_token_device_user_and_sites(company, foreman):
    site = SiteFactory(name="Riverside", foremen=[foreman], roll_call_due=datetime.time(8, 30))
    SiteFactory(name="Elsewhere")

    response = sign_in(device_name="Tecno Spark 10")

    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"token", "device", "user", "sites"}
    assert body["user"] == {
        "id": foreman.pk,
        "name": "Kossi Amegah",
        "role": "foreman",
        "phone": "+228 90 11 22 33",
        "language": "fr",  # blank on the user → company language
    }
    assert body["device"]["device_id"] == "phone-1"
    assert body["device"]["name"] == "Tecno Spark 10"
    assert [s["id"] for s in body["sites"]] == [site.pk]
    assert body["sites"][0]["work_start"] == "07:00"
    assert body["sites"][0]["roll_call_due"] == "08:30"
    assert body["sites"][0]["manager"]["id"] == site.manager_id


def test_token_is_stored_only_as_hash(company, foreman):
    token = sign_in().json()["token"]

    device = ForemanDevice.objects.get(user=foreman)
    assert device.token_hash == hash_device_token(token)
    assert token not in device.token_hash


def test_pin_is_never_stored_in_clear(company, foreman):
    foreman.refresh_from_db()

    assert PIN not in foreman.pin_hash
    assert foreman.pin_hash.startswith(("pbkdf2_", "argon2", "bcrypt", "scrypt", "md5"))
    assert foreman.check_pin(PIN)
    assert not foreman.check_pin("0000")


def test_phone_matches_whatever_the_spacing(company, foreman):
    assert sign_in(phone="+228 90-11-22-33").status_code == 200


def test_the_phone_is_looked_up_without_reading_every_user(
    company, foreman, django_assert_num_queries
):
    for _ in range(5):
        ForemanFactory()

    with django_assert_num_queries(1):
        found = services.users_with_phone("+228 90 11-22-33")

    assert found == [foreman]
    assert services.users_with_phone("+228 90 11 22 33", exclude_pk=foreman.pk) == []


def test_two_users_cannot_share_a_phone_even_in_two_companies(company, foreman):
    with use_company(CompanyFactory()), pytest.raises(IntegrityError):
        ForemanFactory(phone="+22890112233")


def test_the_normalised_phone_follows_a_changed_phone(company, foreman):
    foreman.phone = "+228 97 00 00 01"
    foreman.save(update_fields=["phone"])

    assert sign_in(phone="+22897000001").status_code == 200
    assert sign_in().status_code != 200


def test_sign_in_again_on_same_device_replaces_the_token(company, foreman):
    old = sign_in().json()["token"]
    new = sign_in().json()["token"]

    assert old != new
    assert ForemanDevice.objects.filter(user=foreman).count() == 1
    assert device_client(old).get(ME_URL).status_code == 401
    assert device_client(new).get(ME_URL).status_code == 200


def test_sign_in_reactivates_a_revoked_device(company, foreman):
    token = sign_in().json()["token"]
    device_client(token).post(LOGOUT_URL)

    response = sign_in()

    assert response.status_code == 200
    assert device_client(response.json()["token"]).get(ME_URL).status_code == 200


def test_another_user_on_the_same_phone_revokes_the_first_token(company, foreman):
    other = ForemanFactory(phone="+22891000000")
    other.set_pin("7350")
    other.save()
    first = sign_in().json()["token"]

    assert sign_in(phone="+22891000000", pin="7350").status_code == 200
    assert device_client(first).get(ME_URL).status_code == 401


def test_bad_device_token_header_is_ignored_on_sign_in(company, foreman):
    client = APIClient(HTTP_AUTHORIZATION="Device not-a-real-token")

    assert sign_in(client=client).status_code == 200


@pytest.mark.parametrize(
    "setup",
    ["unknown_phone", "no_pin", "inactive", "site_manager"],
)
def test_sign_in_refused(company, foreman, setup):
    phone = "+22890112233"
    if setup == "unknown_phone":
        phone = "+22899999999"
    elif setup == "no_pin":
        foreman.pin_hash = ""
        foreman.save()
    elif setup == "inactive":
        foreman.is_active = False
        foreman.save()
    elif setup == "site_manager":
        foreman.role = User.Role.SITE_MANAGER
        foreman.save()

    response = sign_in(phone=phone)

    assert response.status_code == 401
    assert response.json()["code"] == "not_authenticated"
    assert response.json()["details"] == {}
    assert not ForemanDevice.objects.exists()


def test_admin_with_a_pin_may_sign_in(company):
    admin = UserFactory(role=User.Role.ADMIN, phone="+22890000001")
    admin.set_pin(PIN)
    admin.save()
    SiteFactory()

    response = sign_in(phone="+22890000001")

    assert response.status_code == 200
    assert len(response.json()["sites"]) == 1


def test_sign_in_validates_the_body(company, foreman):
    response = sign_in(pin="12")

    assert response.status_code == 400
    assert "pin" in response.json()["details"]


# --- Lockout ---------------------------------------------------------------------------


def test_wrong_pin_counts_down_then_locks_on_the_sixth_try(company, foreman):
    for attempts_left in (4, 3, 2, 1, 0):
        response = sign_in(pin="0000")
        assert response.status_code == 401
        assert response.json()["code"] == "not_authenticated"
        assert response.json()["details"]["attempts_left"] == attempts_left

    response = sign_in()  # the right PIN, but too late

    assert response.status_code == 423
    body = response.json()
    assert body["code"] == "locked"
    foreman.refresh_from_db()
    assert foreman.is_pin_locked
    locked_until = datetime.datetime.fromisoformat(body["details"]["locked_until"])
    assert locked_until == foreman.pin_locked_until
    assert locked_until - timezone.now() > timedelta(minutes=14)
    assert AuditLog.objects.filter(action=services.AUDIT_PIN_LOCKED).count() == 1
    assert not ForemanDevice.objects.exists()


def test_locked_user_cannot_sign_in_until_lock_expires(company, foreman):
    foreman.pin_failed_attempts = services.PIN_MAX_ATTEMPTS
    foreman.pin_locked_until = timezone.now() + timedelta(minutes=5)
    foreman.save()

    assert sign_in().status_code == 423

    User.objects.filter(pk=foreman.pk).update(
        pin_locked_until=timezone.now() - timedelta(seconds=1)
    )
    response = sign_in()

    assert response.status_code == 200
    foreman.refresh_from_db()
    assert foreman.pin_failed_attempts == 0
    assert foreman.pin_locked_until is None


def test_wrong_pin_after_lock_expires_starts_a_fresh_count(company, foreman):
    foreman.pin_failed_attempts = services.PIN_MAX_ATTEMPTS
    foreman.pin_locked_until = timezone.now() - timedelta(seconds=1)
    foreman.save()

    response = sign_in(pin="0000")

    assert response.status_code == 401
    assert response.json()["details"]["attempts_left"] == services.PIN_MAX_ATTEMPTS - 1


def test_successful_sign_in_resets_the_count(company, foreman):
    sign_in(pin="0000")
    sign_in(pin="0000")

    assert sign_in().status_code == 200
    foreman.refresh_from_db()
    assert foreman.pin_failed_attempts == 0


# --- Logout and revoked devices ---------------------------------------------------------


def test_logout_revokes_this_device_only(company, foreman):
    first = sign_in(device_id="phone-1").json()["token"]
    second = sign_in(device_id="phone-2").json()["token"]

    response = device_client(first).post(LOGOUT_URL)

    assert response.status_code == 204
    assert device_client(first).get(ME_URL).status_code == 401
    assert device_client(second).get(ME_URL).status_code == 200
    entry = AuditLog.objects.get(action=services.AUDIT_DEVICE_REVOKE)
    assert entry.actor == foreman


def test_revoked_device_gets_401(company, foreman):
    token = sign_in().json()["token"]
    services.revoke_device(ForemanDevice.objects.get(), actor=None)

    response = device_client(token).get(ME_URL)

    assert response.status_code == 401
    assert response.json()["code"] == "not_authenticated"


def test_disabled_user_gets_401(company, foreman):
    token = sign_in().json()["token"]
    User.objects.filter(pk=foreman.pk).update(is_active=False)

    assert device_client(token).get(ME_URL).status_code == 401


def test_logout_with_a_session_is_a_no_op(company, foreman):
    client = APIClient()
    client.force_login(foreman)

    assert client.post(LOGOUT_URL).status_code == 204


# --- /me/ and site scope ---------------------------------------------------------------


def test_me_returns_profile_company_and_only_own_sites(company, foreman):
    mine = SiteFactory(name="Riverside", foremen=[foreman])
    SiteFactory(name="Market Hall", foremen=[ForemanFactory()])
    SiteFactory(name="Finished", foremen=[foreman], status=Site.Status.COMPLETED)
    foreman.language = "en"
    foreman.save()
    token = sign_in().json()["token"]

    response = device_client(token).get(ME_URL)

    assert response.status_code == 200
    body = response.json()
    assert body["id"] == foreman.pk
    assert body["language"] == "en"
    assert body["company"] == {
        "country": "TG",
        "time_zone": "Africa/Lome",
        "currency": {"code": "XOF", "decimals": 0},
        "payments": {
            "accepted_proofs": ["photo", "signature", "thumb_mark"],
            "file_required_for": ["photo"],
        },
    }
    assert [s["id"] for s in body["sites"]] == [mine.pk]


@pytest.mark.parametrize(
    ("setting", "accepted"),
    [
        (["signature", "photo"], ["photo", "signature"]),
        (["none", "thumb_mark"], ["thumb_mark", "none"]),
        ([], ["photo", "signature", "thumb_mark", "none"]),  # empty: everything goes
    ],
)
def test_me_lists_the_proof_rules(company, foreman, setting, accepted):
    company.accepted_proofs = setting
    company.save()
    token = sign_in().json()["token"]

    payments = device_client(token).get(ME_URL).json()["company"]["payments"]

    assert payments == {"accepted_proofs": accepted, "file_required_for": ["photo"]}


@pytest.mark.parametrize("proof_type", ["photo", "signature", "thumb_mark", "none"])
def test_proof_rules_match_the_payment_service(company, proof_type):
    """`accepted_proofs`/`file_required_for` must say what validate_proof enforces."""
    from django.core.exceptions import ValidationError

    from apps.accounts.api.views import payment_rules
    from apps.payroll.services import validate_proof

    for setting in ([], ["photo", "signature", "thumb_mark"], ["signature", "none"]):
        company.accepted_proofs = setting
        rules = payment_rules(company)
        try:
            validate_proof(proof_type, None, company)
            errors = {}
        except ValidationError as exc:
            errors = exc.message_dict
        assert ("proof_type" not in errors) is (proof_type in rules["accepted_proofs"]), setting
        if proof_type in rules["accepted_proofs"]:
            assert ("proof_file" in errors) is (proof_type in rules["file_required_for"])


def test_unknown_phone_no_pin_and_wrong_role_get_the_same_answer(company, foreman):
    manager = SiteManagerFactory(phone="+22890000002")
    manager.set_pin(PIN)
    manager.save()
    UserFactory(role=User.Role.FOREMAN, phone="+22890000003")  # no PIN

    answers = [
        sign_in(phone=phone).json() for phone in ("+22899999999", "+22890000002", "+22890000003")
    ]

    assert answers[0] == answers[1] == answers[2]
    assert answers[0]["code"] == "not_authenticated"
    assert answers[0]["details"] == {}


def test_foreman_token_cannot_see_another_sites_data(company, foreman):
    other_foreman = ForemanFactory()
    other_site = SiteFactory(name="Market Hall", foremen=[other_foreman])
    token = sign_in().json()["token"]

    body = device_client(token).get(ME_URL).json()

    assert other_site.pk not in [s["id"] for s in body["sites"]]
    assert body["sites"] == []


def test_me_for_a_site_manager_session_lists_managed_sites(company):
    manager = SiteManagerFactory()
    managed = SiteFactory(manager=manager)
    SiteFactory()
    client = APIClient()
    client.force_login(manager)

    body = client.get(ME_URL).json()

    assert [s["id"] for s in body["sites"]] == [managed.pk]
    assert body["sites"][0]["manager"]["id"] == manager.pk
