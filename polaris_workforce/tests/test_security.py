"""Security pass (Wave 3, I1): CSRF, roles, PIN brute force, device tokens, uploads,
production settings, redirects, clickjacking, escaping and CSV export.

Site scoping on every URL is in ``test_e2e_isolation.py``. The findings and the
production checklist are in ``docs/security-review.md``.
"""

import csv
import io
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest
from django.conf import settings as django_settings
from django.core.cache import cache
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client
from django.urls import reverse
from rest_framework.test import APIClient

from apps.accounts.models import ForemanDevice
from apps.attendance.models import AttendanceDay
from apps.core.models import AuditLog
from apps.core.validators import validate_file_size, validate_upload_extension
from apps.payroll import services as payroll_services
from apps.payroll.models import Advance, CashIssue, PayLine, Payment, PayPeriod
from apps.workers.models import Worker
from apps.worksites.models import SiteAssignment
from tests.e2e import Office, Phone, admin_office, hire, make_site, new_uuid
from tests.factories import CompanySettingsFactory, UserFactory
from tests.test_payroll_services import MON, roll_call

pytestmark = pytest.mark.django_db

BASE_DIR = Path(django_settings.BASE_DIR)
PIN = "4826"
PHONE = "+228 90 12 34 56"


@pytest.fixture(autouse=True)
def _isolated(settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path
    cache.clear()
    yield
    cache.clear()


@pytest.fixture
def world(db):
    """A site with an approved Monday: Ama paid, Kofi not yet."""
    CompanySettingsFactory()
    crew = make_site("Riverside", phone_number=PHONE, pin=PIN)
    ama, kofi = hire(crew, "Ama", 6000), hire(crew, "Kofi", 5000)
    day = roll_call(crew.site, MON, {ama: "present", kofi: "present"}, foreman=crew.foreman)
    payroll_services.cash_sheet(crew.site, MON)
    paid_line = PayLine.objects.get(worker=ama)
    payment = payroll_services.record_payment(
        crew.manager, paid_line, amount=6000, proof_type="signature"
    )
    crew.day = day
    crew.paid_line, crew.open_line = paid_line, PayLine.objects.get(worker=kofi)
    crew.payment = payment
    return crew


# --- CSRF ---------------------------------------------------------------------------------

# Every office URL that changes something on POST. Each is checked without a token.
# GET-only office pages are listed so that a new URL has to be put in one list or the other.
GET_ONLY = {
    "dashboard:home",
    "dashboard:reports",
    "dashboard:reports_csv",
    "dashboard:components",
    "core:audit_log",
    "core:worker_photo",
    "core:payment_proof",
    "core:media_file",
    "attendance:approvals",
    "payroll:cash_sheets",
    "payroll:cash_sheet",
    "payroll:periods",
    "payroll:payslip",
    "payroll:advance_preview",
    "payouts:batches",
    "payouts:batch",
    "payouts:layouts",
    "payouts:accounts",
    "workers:list",
    "workers:detail",
    "worksites:list",
    "worksites:detail",
    "worksites:crew",
    "worksites:attendance",
    "worksites:pay",
    "accounts:users",
    "password_change_done",
    "password_reset_done",
    "password_reset_complete",
}


def _state_changing_urls(crew):
    site, date = crew.site.pk, MON.isoformat()
    worker = crew.paid_line.worker_id
    assignment = SiteAssignment.objects.filter(site=crew.site).first().pk
    device = ForemanDevice.objects.create(
        user=crew.foreman, device_id="csrf-phone", token_hash="x" * 64
    )
    return {
        **_payout_state_changing_urls(crew),
        "attendance:decide": reverse("attendance:decide", args=[crew.day.pk]),
        "payroll:cash_issue": reverse("payroll:cash_issue", args=[site, date]),
        "payroll:cash_sheet_lock": reverse("payroll:cash_sheet_lock", args=[site, date]),
        "payroll:cash_sheet_pay": reverse("payroll:cash_sheet_pay", args=[crew.open_line.pk]),
        "payroll:payment_undo": reverse("payroll:payment_undo", args=[crew.payment.pk]),
        "payroll:advances": reverse("payroll:advances"),
        "workers:create": reverse("workers:create"),
        "workers:edit": reverse("workers:edit", args=[worker]),
        "worksites:create": reverse("worksites:create"),
        "worksites:edit": reverse("worksites:edit", args=[site]),
        "worksites:assign": reverse("worksites:assign", args=[site]),
        "worksites:end_assignment": reverse("worksites:end_assignment", args=[site, assignment]),
        "worksites:settings": reverse("worksites:settings", args=[site]),
        "core:settings": reverse("core:settings"),
        "core:companies": reverse("core:companies"),
        "core:company_switch": reverse("core:company_switch"),
        "accounts:user_create": reverse("accounts:user_create"),
        "accounts:user_edit": reverse("accounts:user_edit", args=[crew.foreman.pk]),
        "accounts:user_set_pin": reverse("accounts:user_set_pin", args=[crew.foreman.pk]),
        "accounts:device_revoke": reverse(
            "accounts:device_revoke", args=[crew.foreman.pk, device.pk]
        ),
        "logout": reverse("logout"),
        "password_change": reverse("password_change"),
        "set_language": reverse("set_language"),
        "login": reverse("login"),
        "password_reset": reverse("password_reset"),
        "password_reset_confirm": reverse("password_reset_confirm", args=["MQ", "set-password"]),
    }


def _payout_state_changing_urls(crew):
    from apps.payouts.models import ExportLayout, PayoutBatch, PayoutLine

    batch = PayoutBatch.objects.create(
        number=1, method="mobile_money", pay_date=MON, site=crew.site, currency="XOF"
    )
    line = PayoutLine.objects.create(
        batch=batch,
        pay_line=crew.open_line,
        amount=crew.open_line.net,
        reference="PAY-00001-001",
        method="mobile_money",
    )
    layout = ExportLayout.objects.create(
        name="CSRF check", method="mobile_money", columns=[{"field": "reference", "header": "R"}]
    )
    return {
        "payouts:prepare": reverse("payouts:prepare"),
        "payouts:batch_export": reverse("payouts:batch_export", args=[batch.pk]),
        "payouts:batch_import": reverse("payouts:batch_import", args=[batch.pk]),
        "payouts:batch_close": reverse("payouts:batch_close", args=[batch.pk]),
        "payouts:batch_cancel": reverse("payouts:batch_cancel", args=[batch.pk]),
        "payouts:line_confirm": reverse("payouts:line_confirm", args=[line.pk]),
        "payouts:line_fail": reverse("payouts:line_fail", args=[line.pk]),
        "payouts:line_remove": reverse("payouts:line_remove", args=[line.pk]),
        "payouts:layout_create": reverse("payouts:layout_create"),
        "payouts:layout_edit": reverse("payouts:layout_edit", args=[layout.pk]),
        "payouts:account_edit": reverse("payouts:account_edit", args=[crew.open_line.worker_id]),
    }


def test_every_office_url_is_either_get_only_or_csrf_checked(world):
    from tests.test_e2e_isolation import OFFICE

    office_names = set(OFFICE)
    assert office_names - GET_ONLY - set(_state_changing_urls(world)) == set()


def test_every_state_changing_office_form_needs_the_csrf_token(world):
    admin = admin_office()
    before = (AuditLog.objects.count(), Payment.objects.count(), CashIssue.objects.count())
    for name, url in _state_changing_urls(world).items():
        if name == "password_reset_confirm":
            continue  # needs a valid reset link to reach the form; Django's own view
        client = Client(enforce_csrf_checks=True)
        if name not in ("login", "password_reset"):
            client.force_login(admin.user)
        plain = client.post(url, {"action": "approve", "amount": "1000", "reason": "x"})
        assert plain.status_code == 403, name
        assert b"CSRF" in plain.content, name
        # htmx sends the token as a header; a wrong one is refused too.
        htmx = client.post(
            url, {"action": "approve"}, HTTP_HX_REQUEST="true", HTTP_X_CSRFTOKEN="forged" * 6
        )
        assert htmx.status_code == 403, name
    after = (AuditLog.objects.count(), Payment.objects.count(), CashIssue.objects.count())
    assert after == before
    assert AttendanceDay.objects.get(pk=world.day.pk).status == "approved"


def test_htmx_post_with_the_page_token_is_accepted(world):
    office = Office(world.manager)
    response = office.post(
        reverse("payroll:cash_sheet_pay", args=[world.open_line.pk]),
        {"client_uuid": new_uuid(), "proof_type": "signature"},
        htmx=True,
    )
    assert response.status_code == 200
    assert Payment.objects.filter(pay_line=world.open_line).exists()


def test_api_with_a_browser_session_needs_csrf(world):
    client = APIClient(enforce_csrf_checks=True)
    client.force_login(world.manager)
    body = {
        "client_uuid": new_uuid(),
        "pay_line_id": world.open_line.pk,
        "amount": world.open_line.net,
        "proof_type": "signature",
    }
    response = client.post("/api/v1/payments/", body, format="json")
    assert response.status_code == 403
    assert "CSRF" in response.json()["message"]
    assert not Payment.objects.filter(pay_line=world.open_line).exists()


def test_device_token_calls_do_not_need_csrf(world):
    phone = Phone(PHONE, PIN)
    phone.client.enforce_csrf_checks = True
    paid = phone.pay(world.open_line.pk, world.open_line.net, new_uuid())
    assert paid.status_code == 201


# --- roles --------------------------------------------------------------------------------


def test_accountant_cannot_approve_pay_or_issue_cash_but_can_lock(world):
    accountant = Office(UserFactory(role="accountant"))
    pending = roll_call(world.site, MON.replace(day=2), {}, status="submitted")
    site, date = world.site.pk, MON.isoformat()
    denied = {
        "decide": accountant.post(
            reverse("attendance:decide", args=[pending.pk]), {"action": "approve"}
        ),
        "cash": accountant.post(
            reverse("payroll:cash_issue", args=[site, date]), {"amount": "1000"}
        ),
        "pay": accountant.post(
            reverse("payroll:cash_sheet_pay", args=[world.open_line.pk]),
            {"client_uuid": new_uuid(), "proof_type": "signature"},
        ),
        "undo": accountant.post(
            reverse("payroll:payment_undo", args=[world.payment.pk]), {"reason": "x"}
        ),
    }
    assert {k: r.status_code for k, r in denied.items()} == dict.fromkeys(denied, 403)
    api = APIClient()
    api.force_authenticate(accountant.user)
    pay = api.post(
        "/api/v1/payments/",
        {
            "client_uuid": new_uuid(),
            "pay_line_id": world.open_line.pk,
            "amount": world.open_line.net,
            "proof_type": "signature",
        },
        format="json",
    )
    assert pay.status_code == 403
    roll = api.put(f"/api/v1/sites/{site}/days/{date}/records/", {"records": []}, format="json")
    assert roll.status_code == 403
    assert Payment.objects.count() == 1 and not CashIssue.objects.exists()

    # Locking pay periods is the accountant's job (with a reason: Kofi is unpaid).
    locked = accountant.post(
        reverse("payroll:cash_sheet_lock", args=[site, date]), {"reason": "Kofi left"}
    )
    assert locked.status_code == 302
    assert PayPeriod.objects.get(end_date=MON).is_locked


def test_foreman_in_the_office_cannot_approve_issue_cash_lock_or_undo(world):
    foreman = Office(world.foreman)
    pending = roll_call(world.site, MON.replace(day=2), {}, status="submitted")
    site, date = world.site.pk, MON.isoformat()
    responses = [
        foreman.post(reverse("attendance:decide", args=[pending.pk]), {"action": "approve"}),
        foreman.post(reverse("payroll:cash_issue", args=[site, date]), {"amount": "1000"}),
        foreman.post(reverse("payroll:cash_sheet_lock", args=[site, date]), {"reason": "x"}),
        foreman.post(reverse("payroll:payment_undo", args=[world.payment.pk]), {"reason": "x"}),
        foreman.get(reverse("core:settings")),
        foreman.get(reverse("accounts:users")),
    ]
    assert [r.status_code for r in responses] == [403] * len(responses)
    assert not PayPeriod.objects.filter(status="locked").exists()
    pending.refresh_from_db()
    assert pending.status == "submitted"


def test_site_manager_cannot_take_the_roll_call_over_the_api(world):
    api = APIClient()
    api.force_authenticate(world.manager)
    url = f"/api/v1/sites/{world.site.pk}/days/{MON.replace(day=3).isoformat()}/records/"
    assert api.put(url, {"records": []}, format="json").status_code == 403


# --- PIN brute force ----------------------------------------------------------------------


def _try_pin(client, phone=PHONE, pin="0000", **extra):
    body = {"phone": phone, "pin": pin, "device_id": "attacker"}
    return client.post("/api/v1/auth/pin/", body, format="json", **extra)


def test_wrong_pins_lock_the_foreman_then_the_ip_is_throttled(world):
    client = APIClient()
    statuses = [_try_pin(client, pin=f"{n:04d}").status_code for n in (1, 2, 3, 4, 5)]
    assert statuses == [401] * 5
    locked = _try_pin(client, pin=PIN)  # even the right PIN
    assert locked.status_code == 423
    assert locked.json()["details"]["locked_until"]
    assert AuditLog.objects.filter(action="accounts.pin_locked").count() == 1
    for _ in range(4):
        _try_pin(client, phone="+228 99 99 99 99")
    throttled = _try_pin(client, phone="+228 99 99 99 99")  # 11th try this minute
    assert throttled.status_code == 429
    assert throttled.json()["code"] == "throttled"
    assert int(throttled["Retry-After"]) > 0
    for entry in AuditLog.objects.all():
        assert PIN not in json.dumps([entry.before, entry.after, entry.reason])


def test_pin_throttle_ignores_a_spoofed_forwarded_for_header(world):
    """Rotating X-Forwarded-For must not give the attacker a fresh throttle bucket
    (``NUM_PROXIES`` in ``REST_FRAMEWORK``; fixed in Wave 3)."""
    client = APIClient()
    codes = [
        _try_pin(client, phone="+228 99 99 99 99", HTTP_X_FORWARDED_FOR=f"10.0.0.{n}").status_code
        for n in range(11)
    ]
    assert codes[:10] == [401] * 10
    assert codes[10] == 429


# --- device tokens ------------------------------------------------------------------------


def test_device_revoked_on_the_users_screen_gets_401(world):
    phone = Phone(PHONE, PIN, device_id="tecno-1")
    assert phone.me().status_code == 200
    device = ForemanDevice.objects.get(device_id="tecno-1")
    admin = admin_office()
    revoked = admin.post(reverse("accounts:device_revoke", args=[world.foreman.pk, device.pk]))
    assert revoked.status_code == 302
    response = phone.me()
    assert response.status_code == 401
    assert response.json()["code"] == "not_authenticated"
    entry = AuditLog.objects.get(action="accounts.device_revoke")
    assert phone.token not in json.dumps([entry.before, entry.after, entry.reason])

    # Signing in again issues a new token; the old one stays dead.
    again = Phone(PHONE, PIN, device_id="tecno-1")
    assert again.me().status_code == 200
    assert phone.me().status_code == 401


def test_logout_and_disabled_user_get_401(world):
    phone = Phone(PHONE, PIN)
    assert phone.logout().status_code == 204
    assert phone.me().status_code == 401
    other = Phone(PHONE, PIN)
    world.foreman.is_active = False
    world.foreman.save()
    assert other.me().status_code == 401
    assert other.put_records(world.site.pk, MON, []).status_code == 401


# --- uploads ------------------------------------------------------------------------------


def _upload(name, content=b"\xff\xd8\xff" + b"0" * 64, content_type="image/jpeg"):
    return SimpleUploadedFile(name, content, content_type=content_type)


@pytest.mark.parametrize(
    "proof",
    [
        _upload("proof.html", b"<script>alert(1)</script>", "text/html"),
        _upload("proof.svg", b"<svg onload=alert(1)>", "image/svg+xml"),
        _upload("proof.jpg.exe"),
        _upload("big.jpg", b"\xff\xd8\xff" + b"0" * (5 * 1024 * 1024)),
    ],
    ids=["html", "svg", "exe", "over-5MB"],
)
def test_bad_proof_files_are_refused(world, proof):
    phone = Phone(PHONE, PIN)
    response = phone.pay(world.open_line.pk, world.open_line.net, new_uuid(), "photo", proof)
    assert response.status_code == 400
    assert "proof_file" in response.json()["details"]
    assert not Payment.objects.filter(pay_line=world.open_line).exists()


def test_proof_file_name_cannot_escape_the_media_folder(world, tmp_path):
    phone = Phone(PHONE, PIN)
    proof = _upload("../../../../etc/cron.d/evil.jpg")
    response = phone.pay(world.open_line.pk, world.open_line.net, new_uuid(), "photo", proof)
    assert response.status_code == 201
    stored = Payment.objects.get(pay_line=world.open_line).proof_file
    assert stored.name.startswith("proofs/")
    assert ".." not in stored.name
    assert Path(stored.path).resolve().is_relative_to(tmp_path.resolve())


def test_every_upload_field_has_the_upload_validators():
    fields = [
        Worker._meta.get_field("photo"),
        Payment._meta.get_field("proof_file"),
        Advance._meta.get_field("proof_file"),
    ]
    for field in fields:
        assert validate_upload_extension in field.validators
        assert validate_file_size in field.validators


# --- audit trail for money and attendance writes ------------------------------------------


def test_office_money_writes_are_audited(world):
    manager = Office(world.manager)
    site, date = world.site.pk, MON.isoformat()
    manager.post(reverse("payroll:cash_issue", args=[site, date]), {"amount": "12000"})
    manager.post(reverse("payroll:payment_undo", args=[world.payment.pk]), {"reason": "Wrong"})
    actions = set(AuditLog.objects.values_list("action", flat=True))
    assert {"payroll.cash.issue", "payroll.payment.undo", "payroll.payment.create"} <= actions
    undo = AuditLog.objects.get(action="payroll.payment.undo")
    assert undo.actor == world.manager and undo.reason == "Wrong"
    assert undo.before["amount"] == 6000


# --- production settings ------------------------------------------------------------------


def _settings_in_subprocess(env_overrides, code):
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("DJANGO_") and key != "SQLITE_PATH"
    }
    env.update(
        {
            "PYTHONPATH": os.pathsep.join(p for p in sys.path if p),
            "DJANGO_SETTINGS_MODULE": "config.settings",
            **env_overrides,
        }
    )
    return subprocess.run(
        [sys.executable, "-c", code],
        cwd=BASE_DIR,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )


def test_debug_off_requires_a_secret_key():
    result = _settings_in_subprocess({"DJANGO_DEBUG": "false"}, "import config.settings")
    assert result.returncode != 0
    assert "DJANGO_SECRET_KEY must be set" in result.stderr


def test_debug_off_settings_are_hardened():
    code = """
import json, django
django.setup()
from django.conf import settings as s
from django.urls import get_resolver
rf = s.REST_FRAMEWORK
print(json.dumps({
    "debug": s.DEBUG,
    "session_secure": s.SESSION_COOKIE_SECURE,
    "csrf_secure": s.CSRF_COOKIE_SECURE,
    "session_httponly": s.SESSION_COOKIE_HTTPONLY,
    "hosts": s.ALLOWED_HOSTS,
    "frame": s.X_FRAME_OPTIONS,
    "nosniff": s.SECURE_CONTENT_TYPE_NOSNIFF,
    "proxy_ssl": list(s.SECURE_PROXY_SSL_HEADER or []),
    "renderers": rf["DEFAULT_RENDERER_CLASSES"],
    "num_proxies": rf["NUM_PROXIES"],
    "routes": sorted(str(p.pattern) for p in get_resolver().url_patterns),
}))
"""
    result = _settings_in_subprocess(
        {"DJANGO_DEBUG": "false", "DJANGO_SECRET_KEY": "test-" + "k" * 60}, code
    )
    assert result.returncode == 0, result.stderr
    values = json.loads(result.stdout)
    assert values["debug"] is False
    assert values["session_secure"] and values["csrf_secure"] and values["session_httponly"]
    assert values["hosts"] == ["localhost", "127.0.0.1"]  # never "*" by default
    assert values["frame"] == "DENY"
    assert values["nosniff"] is True
    assert values["proxy_ssl"] == ["HTTP_X_FORWARDED_PROTO", "https"]
    assert values["renderers"] == ["rest_framework.renderers.JSONRenderer"]
    assert values["num_proxies"] == 0
    # Uploaded files are not served by Django without DEBUG (see the review: they need an
    # authenticated download view in production).
    assert not any(route.startswith("^media") for route in values["routes"])


# --- static checks ------------------------------------------------------------------------


def _python_sources():
    return [p for p in (BASE_DIR / "apps").rglob("*.py") if "migrations" not in p.parts]


def test_no_raw_sql_or_csrf_exemptions_in_the_code():
    pattern = re.compile(r"\.raw\(|\.extra\(|RawSQL|connection\.cursor|csrf_exempt")
    offenders = [str(p) for p in _python_sources() if pattern.search(p.read_text())]
    assert offenders == []


def test_templates_never_turn_escaping_off():
    pattern = re.compile(r"\|\s*safe\b|autoescape\s+off|mark_safe")
    offenders = [
        str(p)
        for p in [*(BASE_DIR / "templates").rglob("*.html"), *_python_sources()]
        if pattern.search(p.read_text())
    ]
    assert offenders == []


# --- redirects, framing, escaping, CSV ----------------------------------------------------


@pytest.mark.parametrize("target", ["https://evil.example/", "//evil.example/", "/\\evil.example"])
def test_login_and_language_switch_do_not_redirect_off_site(world, target):
    world.manager.set_password("s3cret-pass-123")
    world.manager.save()
    client = Client()
    login = client.post(
        reverse("login"),
        {"username": world.manager.username, "password": "s3cret-pass-123", "next": target},
    )
    assert login.status_code == 302
    assert "evil.example" not in login["Location"]
    switch = client.post(reverse("set_language"), {"language": "fr", "next": target})
    assert switch.status_code == 302
    assert "evil.example" not in switch["Location"]


def test_pages_api_and_pdf_cannot_be_framed(world):
    office = Office(world.manager)
    responses = [
        office.get(reverse("dashboard:home")),
        office.get(reverse("payroll:payslip", args=[world.paid_line.pk])),
        office.get(reverse("login")),
        APIClient().get("/api/v1/health/"),
    ]
    assert [r["X-Frame-Options"] for r in responses] == ["DENY"] * len(responses)
    assert all(r["X-Content-Type-Options"] == "nosniff" for r in responses)


def test_worker_names_are_escaped_on_office_pages(world):
    worker = world.open_line.worker
    worker.first_name = "<script>alert(1)</script>"
    worker.save()
    office = Office(world.manager)
    pages = [
        reverse("workers:list"),
        reverse("workers:detail", args=[worker.pk]),
        reverse("payroll:cash_sheet", args=[world.site.pk, MON.isoformat()]),
        reverse("worksites:crew", args=[world.site.pk]),
    ]
    for url in pages:
        content = office.get(url).content.decode()
        assert "<script>alert(1)</script>" not in content, url
        assert "&lt;script&gt;alert(1)&lt;/script&gt;" in content, url


def test_csv_export_neutralises_formulas(world):
    worker = world.open_line.worker
    worker.first_name = '=HYPERLINK("http://evil.example","Click")'
    worker.last_name = "+cmd"
    worker.save()
    office = Office(world.manager)
    span = {"period": "custom", "start": MON.isoformat(), "end": MON.isoformat()}
    response = office.get(reverse("dashboard:reports_csv"), span)
    rows = list(csv.reader(io.StringIO(response.content.decode("utf-8-sig"))))
    names = [row[1] for row in rows[1:-1]]
    assert '\'=HYPERLINK("http://evil.example","Click") +cmd' in names
    assert not any(cell.startswith(("=", "+", "@")) for row in rows for cell in row)
