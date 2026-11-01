"""Wave 3 hardening (H-sec), see docs/security-review.md:

- H2: uploads get random names and are only downloaded through scoped views;
- M4: DEBUG is off unless asked for; production settings pass ``check --deploy``;
- M5: office and admin sign-in lock a (username, IP) out after too many wrong passwords;
- M6: production uses a cache shared by every process;
- L3: device tokens expire after DEVICE_TOKEN_IDLE_DAYS without use.

L4 (magic bytes) is in test_core_validators.py, L6 (worker profile) in test_cross_area.py,
and site-B isolation of the file URLs in test_e2e_isolation.py.
"""

import datetime
import json
import re
from unittest import mock

import pytest
from django.core.cache import cache
from django.core.files.storage import default_storage
from django.http import Http404
from django.test import Client
from django.urls import Resolver404, resolve, reverse
from django.utils import timezone
from rest_framework.test import APIClient

from apps.accounts import services as account_services
from apps.core import files
from apps.core.models import AuditLog
from apps.core.storage import RandomNameStorage
from apps.payroll import services as payroll_services
from tests import factories as f
from tests.test_payroll_services import MON, hire, line_of, pay, photo, roll_call, setup_site
from tests.test_security import _settings_in_subprocess

pytestmark = pytest.mark.django_db

PASSWORD = "s3cret-pass"  # tests.factories.UserFactory
TOKEN = re.compile(r"[A-Za-z0-9_-]{32}")


@pytest.fixture(autouse=True)
def _isolated(settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path
    cache.clear()
    yield
    cache.clear()


@pytest.fixture
def paid():
    """A paid Monday on one site (proof photo, worker photo), and a second site."""
    f.CompanySettingsFactory()
    site, manager, foreman = setup_site(name="Market Hall")
    yao = hire(site, first_name="Yao")
    yao.photo = photo("Yao Mensah.JPG")
    yao.save()
    roll_call(site, MON, {yao: "present"})
    payroll_services.cash_sheet(site, MON)
    payment = pay(foreman, line_of(site, MON, yao), proof_file=photo("proof.jpg"))
    _other_site, other_manager, other_foreman = setup_site(name="Elsewhere")
    f.ForemanDeviceFactory(user=foreman, raw_token="token-own")
    f.ForemanDeviceFactory(user=other_foreman, raw_token="token-other")
    return {
        "site": site,
        "manager": manager,
        "foreman": foreman,
        "worker": yao,
        "payment": payment,
        "other_manager": other_manager,
    }


def _office(user=None):
    client = Client()
    if user is not None:
        client.force_login(user)
    return client


def _phone(token):
    return APIClient(HTTP_AUTHORIZATION=f"Device {token}")


def _body(response):
    return b"".join(response.streaming_content)


def _urls(paid, api=False):
    if api:
        return [
            reverse("api:file-worker-photo", args=[paid["worker"].pk]),
            reverse("api:file-payment-proof", args=[paid["payment"].pk]),
        ]
    return [
        reverse("core:worker_photo", args=[paid["worker"].pk]),
        reverse("core:payment_proof", args=[paid["payment"].pk]),
    ]


# --- H2: random names ----------------------------------------------------------------------


def test_uploads_get_random_names_keeping_the_folder_and_extension(paid):
    proof = paid["payment"].proof_file.name
    picture = paid["worker"].photo.name
    folder, name = proof.rsplit("/", 1)
    assert re.fullmatch(r"proofs/\d{4}/\d{2}", folder)
    assert TOKEN.fullmatch(name.removesuffix(".jpg"))
    assert picture.startswith("workers/") and picture.endswith(".jpg")  # .JPG lower-cased
    assert "Yao" not in picture and "proof" not in name
    assert default_storage.exists(proof) and default_storage.exists(picture)


def test_the_same_upload_name_twice_gives_two_random_names(tmp_path):
    storage = RandomNameStorage(location=tmp_path)
    first = storage.generate_filename("proofs/2026/10/proof.jpg")
    second = storage.generate_filename("proofs/2026/10/proof.jpg")
    assert first != second
    assert first.startswith("proofs/2026/10/") and first.endswith(".jpg")
    assert storage.generate_filename("workers/no-extension").startswith("workers/")
    assert "." not in storage.generate_filename("workers/x.p h p").rsplit("/", 1)[1]


def test_a_clashing_name_draws_a_new_token(tmp_path):
    storage = RandomNameStorage(location=tmp_path)
    taken = storage.save("workers/abc.jpg", photo())
    again = storage.get_available_name(taken)
    assert again != taken and again.startswith("workers/") and again.endswith(".jpg")


# --- H2: no public route --------------------------------------------------------------------


def test_media_root_has_no_public_route(paid):
    name = paid["payment"].proof_file.name
    for path in (f"/media/{name}", "/media/"):
        with pytest.raises(Resolver404):
            resolve(path)
    assert _office(paid["manager"]).get(f"/media/{name}").status_code == 404


def test_no_media_route_even_with_debug_on():
    code = """
import django, json
django.setup()
from django.urls import Resolver404, resolve
from django.conf import settings
try:
    resolve("/media/proofs/2026/10/proof.jpg")
    found = True
except Resolver404:
    found = False
print(json.dumps({"debug": settings.DEBUG, "found": found}))
"""
    result = _settings_in_subprocess({"DJANGO_DEBUG": "true"}, code)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {"debug": True, "found": False}


# --- H2: office downloads ---------------------------------------------------------------------


def test_office_downloads_for_the_sites_own_people(paid):
    for user in (paid["manager"], f.AdminUserFactory(), f.UserFactory(role="accountant")):
        client = _office(user)
        for url in _urls(paid):
            response = client.get(url)
            assert response.status_code == 200, (user.role, url)
            assert response["Content-Type"] == "image/jpeg"
            assert response["Content-Disposition"].startswith("inline")
            assert response["Cache-Control"] == "private, no-cache"
            assert response["X-Content-Type-Options"] == "nosniff"
            assert _body(response).startswith(b"\xff\xd8\xff")


def test_office_downloads_are_404_on_other_sites_and_need_a_sign_in(paid):
    outsider = _office(paid["other_manager"])
    for url in _urls(paid):
        assert outsider.get(url).status_code == 404
        anonymous = _office().get(url)
        assert anonymous.status_code == 302
        assert anonymous["Location"].startswith(reverse("login"))


def test_an_unchanged_file_answers_304(paid):
    client = _office(paid["manager"])
    url = _urls(paid)[1]
    etag = client.get(url)["ETag"]
    again = client.get(url, HTTP_IF_NONE_MATCH=etag)
    assert again.status_code == 304
    assert again.content == b""
    assert client.get(url, HTTP_IF_NONE_MATCH='"other"').status_code == 200


def test_missing_files_are_404(paid):
    client = _office(paid["manager"])
    default_storage.delete(paid["payment"].proof_file.name)
    assert client.get(_urls(paid)[1]).status_code == 404
    assert _phone("token-own").get(_urls(paid, api=True)[1]).status_code == 404
    other = f.WorkerFactory()  # no photo
    f.SiteAssignmentFactory(worker=other, site=paid["site"], start_date=MON)
    assert client.get(reverse("core:worker_photo", args=[other.pk])).status_code == 404
    assert client.get(reverse("core:payment_proof", args=[999999])).status_code == 404


def test_pdf_proofs_download_as_attachments(paid):
    payment = paid["payment"]
    payment.proof_file.save("receipt.pdf", photo(), save=True)
    response = _office(paid["manager"]).get(_urls(paid)[1])
    assert response["Content-Type"] == "application/pdf"
    assert response["Content-Disposition"].startswith("attachment")


def test_field_file_url_is_the_admin_only_route(paid):
    url = paid["payment"].proof_file.url
    assert url == f"/files/media/{paid['payment'].proof_file.name}"
    assert _office(f.AdminUserFactory()).get(url).status_code == 200
    assert _office(paid["manager"]).get(url).status_code == 403
    assert _office().get(url).status_code == 302


def test_admin_route_cannot_leave_media_root(paid):
    admin = f.AdminUserFactory()
    for name in ("../settings.py", "proofs/../../manage.py", "/etc/passwd", ".."):
        with pytest.raises(Http404):
            files.file_response(None, files.media_name_or_404(admin, name))
    response = _office(admin).get("/files/media/proofs/../../manage.py")
    assert response.status_code == 404


# --- H2: API downloads ------------------------------------------------------------------------


def test_api_downloads_with_the_device_token_or_a_session(paid):
    session = APIClient()
    session.force_login(paid["manager"])
    for client in (_phone("token-own"), session):
        for url in _urls(paid, api=True):
            response = client.get(url, HTTP_ACCEPT="image/webp,image/*,*/*;q=0.8")
            assert response.status_code == 200, url
            assert _body(response).startswith(b"\xff\xd8\xff")
    # An image loader may ask for images only: still the file, not 406.
    only_images = _phone("token-own").get(_urls(paid, api=True)[0], HTTP_ACCEPT="image/*")
    assert only_images.status_code == 200


def test_api_downloads_are_404_on_other_sites_and_401_without_a_token(paid):
    for url in _urls(paid, api=True):
        outsider = _phone("token-other").get(url)
        assert outsider.status_code == 404
        assert outsider.json()["code"] == "not_found"
        anonymous = APIClient().get(url)
        assert anonymous.status_code == 401
        assert anonymous.json()["code"] == "not_authenticated"


def test_api_responses_link_to_the_protected_urls(paid):
    site = paid["site"]
    payout = _phone("token-own").get(reverse("api:payroll:payout", args=[site.pk, MON.isoformat()]))
    worker = payout.json()["lines"][0]["worker"]
    assert worker["photo_url"] == "http://testserver" + _urls(paid, api=True)[0]
    assert "/media/" not in json.dumps(payout.json())


@pytest.mark.parametrize("mode", ["nginx", "apache"])
def test_the_web_server_can_send_the_file(paid, settings, mode):
    settings.PROTECTED_MEDIA_MODE = mode
    name = paid["payment"].proof_file.name
    response = _office(paid["manager"]).get(_urls(paid)[1])
    assert response.status_code == 200
    assert response.content == b""
    if mode == "nginx":
        assert response["X-Accel-Redirect"] == f"/protected-media/{name}"
    else:
        assert response["X-Sendfile"] == default_storage.path(name)
    assert response["Content-Type"] == "image/jpeg"
    # Still checked first.
    assert _office(paid["other_manager"]).get(_urls(paid)[1]).status_code == 404


def test_unknown_protected_media_mode_refuses_to_start():
    result = _settings_in_subprocess(
        {"DJANGO_DEBUG": "true", "PROTECTED_MEDIA_MODE": "s3"}, "import config.settings"
    )
    assert result.returncode != 0
    assert "PROTECTED_MEDIA_MODE" in result.stderr


# --- M4 and M6: production settings ------------------------------------------------------------


def test_debug_is_off_unless_asked_for():
    code = "from django.conf import settings; print(settings.DEBUG)"
    unset = _settings_in_subprocess({"DJANGO_SECRET_KEY": "k" * 60}, code)
    assert unset.returncode == 0, unset.stderr
    assert unset.stdout.strip() == "False"
    no_key = _settings_in_subprocess({}, "import config.settings")
    assert "DJANGO_SECRET_KEY must be set" in no_key.stderr
    local = _settings_in_subprocess({"DJANGO_DEBUG": "true"}, code)
    assert local.stdout.strip() == "True"


def test_production_cache_is_shared_between_processes(tmp_path):
    code = """
import json
from django.conf import settings
print(json.dumps(settings.CACHES["default"]))
"""
    env = {"DJANGO_SECRET_KEY": "k" * 60, "DJANGO_CACHE_DIR": str(tmp_path / "cache")}
    result = _settings_in_subprocess(env, code)
    assert result.returncode == 0, result.stderr
    cache_settings = json.loads(result.stdout)
    assert cache_settings["BACKEND"] == "django.core.cache.backends.filebased.FileBasedCache"
    assert cache_settings["LOCATION"] == str(tmp_path / "cache")


def test_check_deploy_is_clean_with_production_settings(tmp_path):
    code = """
from django.core.management import call_command
call_command("check", deploy=True, fail_level="WARNING")
"""
    env = {
        "DJANGO_SECRET_KEY": "production-" + "x9Q" * 20,
        "DJANGO_ALLOWED_HOSTS": "polaris.example.com",
        "DJANGO_CSRF_TRUSTED_ORIGINS": "https://polaris.example.com",
        "DJANGO_HSTS_SECONDS": "31536000",
        "DJANGO_HSTS_INCLUDE_SUBDOMAINS": "true",
        "DJANGO_HSTS_PRELOAD": "true",
        "DJANGO_NUM_PROXIES": "1",
        "DJANGO_CACHE_DIR": str(tmp_path / "cache"),
        "PROTECTED_MEDIA_MODE": "nginx",
    }
    result = _settings_in_subprocess(env, "import django; django.setup()\n" + code)
    assert result.returncode == 0, result.stderr


def _cookie_flags(env):
    code = (
        "from django.conf import settings\n"
        "print(settings.SESSION_COOKIE_SECURE, settings.CSRF_COOKIE_SECURE)\n"
    )
    base = {"DJANGO_SECRET_KEY": "production-" + "x9Q" * 20, "DJANGO_ALLOWED_HOSTS": "x"}
    result = _settings_in_subprocess({**base, **env}, code)
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def test_cookies_are_secure_in_production_by_default():
    assert _cookie_flags({}) == "True True"


def test_secure_cookies_can_be_turned_off_for_local_plain_http():
    # docker compose on http://localhost sets DJANGO_SECURE_COOKIES=false.
    assert _cookie_flags({"DJANGO_SECURE_COOKIES": "false"}) == "False False"


# --- M5: office sign-in lockout ------------------------------------------------------------------


def _sign_in(client, url, username, password, ip="203.0.113.7"):
    return client.post(url, {"username": username, "password": password}, REMOTE_ADDR=ip)


def _locked(response) -> bool:
    return "Too many failed sign-in attempts" in response.content.decode()


@pytest.mark.parametrize("url_name", ["login", "admin:login"])
def test_wrong_passwords_lock_the_username_and_ip(settings, url_name):
    settings.LOGIN_MAX_ATTEMPTS = 3
    user = f.AdminUserFactory(username="amina")
    url = reverse(url_name)
    client = Client()
    for _ in range(2):
        response = _sign_in(client, url, "amina", "wrong")
        assert response.status_code == 200 and not _locked(response)
    third = _sign_in(client, url, "amina", "wrong")
    assert _locked(third)
    assert "Try again in 15 minutes" in third.content.decode()

    # Even the right password is refused while locked …
    right = _sign_in(client, url, "amina", PASSWORD)
    assert right.status_code == 200 and _locked(right)
    assert "_auth_user_id" not in client.session
    # … from that address only.
    elsewhere = _sign_in(Client(), url, "amina", PASSWORD, ip="198.51.100.4")
    assert elsewhere.status_code == 302

    entry = AuditLog.objects.get(action=account_services.AUDIT_LOGIN_LOCKED)
    assert entry.target == user and entry.actor is None
    assert entry.after["username"] == "amina" and entry.after["ip"] == "203.0.113.7"
    assert PASSWORD not in json.dumps(entry.after) and "wrong" not in json.dumps(entry.after)


def test_the_lockout_ends_after_the_window(settings):
    settings.LOGIN_MAX_ATTEMPTS = 2
    f.SiteManagerFactory(username="kofi")
    client = Client()
    for _ in range(2):
        _sign_in(client, reverse("login"), "kofi", "wrong")
    later = timezone.now() + datetime.timedelta(minutes=16)
    with mock.patch("django.utils.timezone.now", return_value=later):
        response = _sign_in(client, reverse("login"), "kofi", PASSWORD)
    assert response.status_code == 302


def test_a_successful_sign_in_resets_the_count(settings):
    settings.LOGIN_MAX_ATTEMPTS = 3
    f.SiteManagerFactory(username="afi")
    url = reverse("login")
    for _ in range(2):
        _sign_in(Client(), url, "afi", "wrong")
    assert _sign_in(Client(), url, "afi", PASSWORD).status_code == 302
    for _ in range(2):
        assert not _locked(_sign_in(Client(), url, "afi", "wrong"))


def test_unknown_usernames_are_locked_too_without_logging_the_name(settings):
    settings.LOGIN_MAX_ATTEMPTS = 2
    client = Client()
    for _ in range(2):
        response = _sign_in(client, reverse("login"), "my-password-by-mistake", "wrong")
    assert _locked(response)
    entry = AuditLog.objects.get(action=account_services.AUDIT_LOGIN_LOCKED)
    assert entry.target is None
    assert "username" not in entry.after


def test_lockout_ignores_a_spoofed_forwarded_for_header(settings):
    settings.LOGIN_MAX_ATTEMPTS = 2
    f.SiteManagerFactory(username="ama")
    client = Client()
    for n in range(2):
        response = client.post(
            reverse("login"),
            {"username": "ama", "password": "wrong"},
            REMOTE_ADDR="203.0.113.7",
            HTTP_X_FORWARDED_FOR=f"10.0.0.{n}",
        )
    assert _locked(response)


# --- L3: device token idle expiry --------------------------------------------------------------


def _me(token):
    return _phone(token).get(reverse("api:accounts:me"))


def test_device_token_expires_after_the_idle_days(settings):
    f.CompanySettingsFactory()
    settings.DEVICE_TOKEN_IDLE_DAYS = 30
    now = timezone.now()
    fresh = f.ForemanDeviceFactory(raw_token="fresh", last_seen_at=now - datetime.timedelta(29))
    stale = f.ForemanDeviceFactory(raw_token="stale", last_seen_at=now - datetime.timedelta(31))

    assert _me("fresh").status_code == 200
    response = _me("stale")
    assert response.status_code == 401
    assert response.json()["code"] == "not_authenticated"
    assert "expired" in response.json()["message"]
    # Using a token keeps it alive; a refused one is not refreshed.
    fresh.refresh_from_db()
    stale.refresh_from_db()
    assert fresh.last_seen_at > now - datetime.timedelta(minutes=1)
    assert stale.last_seen_at < now - datetime.timedelta(days=30)


def test_a_device_never_seen_expires_from_its_creation(settings):
    f.CompanySettingsFactory()
    settings.DEVICE_TOKEN_IDLE_DAYS = 2
    device = f.ForemanDeviceFactory(raw_token="never-used", last_seen_at=None)
    type(device).objects.filter(pk=device.pk).update(
        created_at=timezone.now() - datetime.timedelta(days=3)
    )
    assert _me("never-used").status_code == 401


def test_signing_in_again_gives_a_working_token(settings):
    f.CompanySettingsFactory()
    foreman = f.ForemanFactory(phone="+228 90 55 55 55")
    foreman.set_pin("4826")
    foreman.save()
    sign_in = account_services.pin_sign_in(
        phone="+228 90 55 55 55", pin="4826", device_id="phone-1"
    )
    sign_in.device.__class__.objects.filter(pk=sign_in.device.pk).update(
        last_seen_at=timezone.now() - datetime.timedelta(days=60)
    )
    assert _me(sign_in.token).status_code == 401
    again = account_services.pin_sign_in(phone="+228 90 55 55 55", pin="4826", device_id="phone-1")
    assert _me(again.token).status_code == 200
