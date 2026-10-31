"""End to end: multi-site isolation.

Site A and site B each have a manager, a foreman and a crew; site B has a full day of data
(roll call, approval, payment, cash issue, a pending day with a correction, an advance).
The manager and the foreman of site A then try every office page and every API endpoint
on site B's objects. Each URL name the project resolves is listed in ``OFFICE`` or
``API`` below (``test_every_url_name_is_classified`` fails when a new one is added), and
each B-object URL is first opened by someone who may see it, so a 403/404 for site A
really comes from the scoping and not from a bad URL.
"""

import datetime

import pytest
from django.core.cache import cache
from django.core.files.base import ContentFile
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import URLPattern, URLResolver, get_resolver, reverse
from rest_framework.test import APIClient

from apps.accounts.models import ForemanDevice
from apps.attendance.models import AttendanceCorrection, AttendanceDay, AttendanceRecord
from apps.payroll import services as payroll_services
from apps.payroll.models import Advance, CashIssue, PayLine, Payment
from apps.portal.models import WorkerAccess
from apps.worksites.models import SiteAssignment
from tests.e2e import Office, Phone, admin_office, hire, lines_by_worker, make_site, new_uuid
from tests.e2e import record as mark
from tests.factories import CompanySettingsFactory

pytestmark = pytest.mark.django_db

PIN = "9173"
SECRET = "Bravo"  # appears in site B's name, its worker's and its manager's names

# Office URL names → how site A users are checked.
#   "b_get":    GET a site-B object → 403/404 (and 200 for site B's manager)
#   "b_post":   POST on a site-B object → 403/404, nothing changes
#   "list":     GET a list/overview page → no site-B data in it (site B data shown to admin)
#   "admin":    admin-only screen → 403 for site A's manager and foreman
#   "public":   not site data (sign-in, language, password screens)
#   "debug":    only served with DEBUG on
#   "platform": platform admins only (companies); checked in test_multi_company.py
#   "portal":   worker portal pages (phone + PIN session, no office user): only the
#               signed-in worker's own data; checked in tests/test_portal_isolation.py
OFFICE = {
    "dashboard:home": "list",
    "dashboard:reports": "list",
    "dashboard:reports_csv": "list",
    "dashboard:components": "debug",
    "core:settings": "admin",
    "core:audit_log": "admin",
    "core:worker_photo": "b_get",
    "core:payment_proof": "b_get",
    "core:media_file": "admin",
    "core:companies": "platform",
    "core:company_switch": "platform",
    "attendance:approvals": "list",
    "attendance:decide": "b_post",
    "payroll:cash_sheets": "list",
    "payroll:cash_sheet": "b_get",
    "payroll:cash_issue": "b_post",
    "payroll:cash_sheet_lock": "b_post",
    "payroll:cash_sheet_pay": "b_post",
    "payroll:payment_undo": "b_post",
    "payroll:periods": "list",
    "payroll:payslip": "b_get",
    "payroll:advances": "list",  # and its POST, below
    "payroll:advance_preview": "list",
    "workers:list": "list",
    "workers:create": "list",
    "workers:detail": "b_get",
    "workers:edit": "b_get",  # and its POST, below
    "worksites:list": "list",
    "worksites:create": "admin",
    "worksites:detail": "b_get",
    "worksites:edit": "b_get",
    "worksites:crew": "b_get",
    "worksites:assign": "b_get",  # and its POST, below
    "worksites:end_assignment": "b_post",
    "worksites:attendance": "b_get",
    "worksites:pay": "b_get",
    "worksites:settings": "b_get",  # and its POST, below
    "accounts:users": "admin",
    "accounts:user_create": "admin",
    "accounts:user_edit": "admin",
    "accounts:user_set_pin": "admin",
    "accounts:device_revoke": "admin",
    "messaging:messages": "admin",
    "messaging:settings": "admin",
    "messaging:recipients": "admin",
    "messaging:worker": "admin",  # site B's worker: admins only
    "login": "public",
    "logout": "public",
    "password_change": "public",
    "password_change_done": "public",
    "password_reset": "public",
    "password_reset_done": "public",
    "password_reset_confirm": "public",
    "password_reset_complete": "public",
    "set_language": "public",
    # Worker portal (Wave 4, WP)
    "portal:sign_in": "public",
    "portal:sign_out": "public",
    "portal:home": "portal",
    "portal:days": "portal",
    "portal:pay": "portal",
    "portal:payslip": "portal",
    "portal:advances": "portal",
    "portal:change_pin": "portal",
    "portal:office_access": "list",
    "portal:office_worker": "b_get",
    "portal:office_set_pin": "b_post",
    "portal:office_revoke": "b_post",
}
API = {
    "api:health": "public",
    "api:schema": "public",
    "api:docs": "debug",
    "api:not-found": "public",  # JSON 404 for any other /api/v1/ path
    "api:accounts:auth-pin": "public",
    "api:accounts:auth-logout": "own device only",
    "api:accounts:me": "list",
    "api:worksites:crew": "b_get",
    "api:attendance:days": "b_get",
    "api:attendance:day": "b_get",
    "api:attendance:day-records": "b_put",
    "api:attendance:day-submit": "b_post",
    "api:attendance:record-corrections": "b_post",
    "api:attendance:sync": "list",
    "api:payroll:payout": "b_get",
    "api:payroll:payments": "b_post",
    "api:file-worker-photo": "b_get (404)",
    "api:file-payment-proof": "b_get (404)",
}
JPEG = b"\xff\xd8\xff\xe0" + b"0" * 64


def _url_names(patterns=None, namespace=""):
    for pattern in patterns if patterns is not None else get_resolver().url_patterns:
        if isinstance(pattern, URLResolver):
            if pattern.namespace == "admin":
                continue  # Django admin: staff only, not site-scoped
            ns = f"{namespace}{pattern.namespace}:" if pattern.namespace else namespace
            yield from _url_names(pattern.url_patterns, ns)
        elif isinstance(pattern, URLPattern) and pattern.name:
            yield namespace + pattern.name


def test_every_url_name_is_classified():
    names = set(_url_names())
    assert names - set(OFFICE) - set(API) == set(), "classify the new URL in this test"


@pytest.fixture(autouse=True)
def _isolated(settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path
    cache.clear()
    yield
    cache.clear()


@pytest.fixture
def world(db):
    CompanySettingsFactory(working_days=[1, 2, 3, 4, 5, 6, 7])
    today = payroll_services.company_today()
    yesterday = today - datetime.timedelta(days=1)

    a = make_site("Alpha Court", phone_number="+228 90 01 01 01", pin=PIN)
    hire(a, "Akosua", 5000)
    b = make_site(f"{SECRET} Tower", phone_number="+228 90 02 02 02", pin=PIN)
    b.manager.last_name = f"{SECRET}manager"
    b.manager.save()
    zeb = hire(b, "Zebulon", 6000, last_name=f"{SECRET}worker")
    zeb.photo.save("zeb.jpg", ContentFile(JPEG))

    # Site B: yesterday approved, paid, with cash issued; today submitted and corrected.
    phone_b = Phone(b.phone_number, PIN)
    office_b = Office(b.manager)
    assert phone_b.put_records(b.site.pk, yesterday, [mark(zeb, "present")]).status_code == 200
    assert phone_b.submit(b.site.pk, yesterday, new_uuid()).status_code == 200
    office_b.approve(AttendanceDay.objects.get(site=b.site, date=yesterday).pk)
    office_b.issue_cash(b.site.pk, yesterday, "10000")
    line = lines_by_worker(phone_b.payout(b.site.pk, yesterday).json())[zeb.pk]
    proof = SimpleUploadedFile("receipt.jpg", JPEG, content_type="image/jpeg")
    paid = phone_b.pay(line["pay_line_id"], line["net"], new_uuid(), "photo", proof)
    assert paid.status_code == 201
    put = phone_b.put_records(b.site.pk, today, [mark(zeb, "late", "07:40")])
    assert phone_b.submit(b.site.pk, today, new_uuid()).status_code == 200
    record_id = put.json()["records"][0]["id"]
    fix = phone_b.correct(record_id, new_uuid(), "arrival", "07:20", "Gate log says 07:20")
    assert fix.status_code == 201
    payroll_services.create_advance(
        b.manager, zeb, amount=5000, deduction_per_pay=1000, proof_type="signature"
    )

    ids = {
        "site": b.site.pk,
        "yesterday": yesterday,
        "today": today,
        "pending_day": AttendanceDay.objects.get(site=b.site, date=today).pk,
        "record": record_id,
        "line": PayLine.objects.get(worker=zeb, period__end_date=yesterday).pk,
        "payment": Payment.objects.get().pk,
        "worker": zeb.pk,
        "assignment": SiteAssignment.objects.get(worker=zeb).pk,
        "manager_b": b.manager.pk,
        "foreman_b": b.foreman.pk,
        "device_b": ForemanDevice.objects.get(user=b.foreman).pk,
    }
    return a, b, ids


def _b_state(b):
    """Everything site A must not be able to change on site B."""
    return {
        "days": sorted(
            AttendanceDay.objects.filter(site=b.site).values_list("date", "status", "foreman_note")
        ),
        "records": sorted(
            AttendanceRecord.objects.filter(day__site=b.site).values_list(
                "pk", "status", "arrival", "note"
            )
        ),
        "corrections": AttendanceCorrection.objects.count(),
        "payments": sorted(Payment.objects.values_list("pk", "amount")),
        "cash": sorted(CashIssue.objects.values_list("site_id", "amount")),
        "advances": sorted(Advance.objects.values_list("worker_id", "amount", "balance")),
        "assignments": sorted(
            SiteAssignment.objects.filter(site=b.site).values_list("pk", "end_date")
        ),
        "site": (b.site.name, b.site.pay_frequency, b.site.status),
        "devices": sorted(ForemanDevice.objects.values_list("pk", "revoked_at")),
        "portal": sorted(WorkerAccess.objects.values_list("worker_id", "is_enabled", "pin_hash")),
    }


def _office_b_get_urls(ids):
    site, day = ids["site"], ids["yesterday"].isoformat()
    return {
        "payroll:cash_sheet": reverse("payroll:cash_sheet", args=[site, day]),
        "payroll:payslip": reverse("payroll:payslip", args=[ids["line"]]),
        "workers:detail": reverse("workers:detail", args=[ids["worker"]]),
        "workers:edit": reverse("workers:edit", args=[ids["worker"]]),
        "worksites:detail": reverse("worksites:detail", args=[site]),
        "worksites:edit": reverse("worksites:edit", args=[site]),
        "worksites:crew": reverse("worksites:crew", args=[site]),
        "worksites:assign": reverse("worksites:assign", args=[site]),
        "worksites:attendance": reverse("worksites:attendance", args=[site]),
        "worksites:pay": reverse("worksites:pay", args=[site]),
        "worksites:settings": reverse("worksites:settings", args=[site]),
        "attendance:approvals?day": reverse("attendance:approvals") + f"?day={ids['pending_day']}",
        "core:worker_photo": reverse("core:worker_photo", args=[ids["worker"]]),
        "core:payment_proof": reverse("core:payment_proof", args=[ids["payment"]]),
        "portal:office_worker": reverse("portal:office_worker", args=[ids["worker"]]),
    }


def _office_b_posts(ids):
    site, day = ids["site"], ids["yesterday"].isoformat()
    return {
        "attendance:decide": (
            reverse("attendance:decide", args=[ids["pending_day"]]),
            {"action": "approve"},
        ),
        "payroll:cash_issue": (
            reverse("payroll:cash_issue", args=[site, day]),
            {"amount": "1000", "note": "x"},
        ),
        "payroll:cash_sheet_lock": (
            reverse("payroll:cash_sheet_lock", args=[site, day]),
            {"reason": "x"},
        ),
        "payroll:cash_sheet_pay": (
            reverse("payroll:cash_sheet_pay", args=[ids["line"]]),
            {"client_uuid": new_uuid(), "proof_type": "signature"},
        ),
        "payroll:payment_undo": (
            reverse("payroll:payment_undo", args=[ids["payment"]]),
            {"reason": "x"},
        ),
        "workers:edit": (
            reverse("workers:edit", args=[ids["worker"]]),
            {"first_name": "Hacked", "last_name": "Hacked"},
        ),
        "worksites:assign": (reverse("worksites:assign", args=[site]), {}),
        "worksites:end_assignment": (
            reverse("worksites:end_assignment", args=[site, ids["assignment"]]),
            {"end_date": ids["today"].isoformat()},
        ),
        "worksites:settings": (
            reverse("worksites:settings", args=[site]),
            {"pay_frequency": "monthly"},
        ),
        "portal:office_set_pin": (
            reverse("portal:office_set_pin", args=[ids["worker"]]),
            {"pin": "4827", "pin_confirm": "4827"},
        ),
        "portal:office_revoke": (reverse("portal:office_revoke", args=[ids["worker"]]), {}),
    }


def _list_pages(ids):
    span = {
        "period": "custom",
        "start": ids["yesterday"].isoformat(),
        "end": ids["today"].isoformat(),
    }
    return {
        "dashboard:home": (reverse("dashboard:home"), {}),
        "dashboard:reports": (reverse("dashboard:reports"), {**span, "site": ids["site"]}),
        "dashboard:reports_csv": (reverse("dashboard:reports_csv"), span),
        "attendance:approvals": (reverse("attendance:approvals"), {}),
        "payroll:cash_sheets": (reverse("payroll:cash_sheets"), {"site": ids["site"]}),
        "payroll:periods": (reverse("payroll:periods"), {"site": ids["site"], "status": "all"}),
        "payroll:advances": (reverse("payroll:advances"), {"show": "all"}),
        "payroll:advance_preview": (
            reverse("payroll:advance_preview"),
            {"worker": ids["worker"], "amount": "1000", "deduction_per_pay": "500"},
        ),
        "workers:list": (reverse("workers:list"), {"site": ids["site"]}),
        "workers:create": (reverse("workers:create"), {}),
        "worksites:list": (reverse("worksites:list"), {}),
        "portal:office_access": (reverse("portal:office_access"), {}),
    }


def _admin_urls(ids):
    return {
        "core:settings": reverse("core:settings"),
        "core:audit_log": reverse("core:audit_log"),
        "core:media_file": Payment.objects.get(pk=ids["payment"]).proof_file.url,
        "worksites:create": reverse("worksites:create"),
        "accounts:users": reverse("accounts:users"),
        "accounts:user_create": reverse("accounts:user_create"),
        "accounts:user_edit": reverse("accounts:user_edit", args=[ids["manager_b"]]),
        "accounts:user_set_pin": reverse("accounts:user_set_pin", args=[ids["foreman_b"]]),
        "accounts:device_revoke": reverse(
            "accounts:device_revoke", args=[ids["foreman_b"], ids["device_b"]]
        ),
        "messaging:messages": reverse("messaging:messages"),
        "messaging:settings": reverse("messaging:settings"),
        "messaging:recipients": reverse("messaging:recipients"),
        "messaging:worker": reverse("messaging:worker", args=[ids["worker"]]),
    }


def test_checks_cover_every_office_url(world):
    _a, _b, ids = world
    covered = (
        {k.split("?")[0] for k in _office_b_get_urls(ids)}
        | set(_office_b_posts(ids))
        | set(_list_pages(ids))
        | set(_admin_urls(ids))
    )
    wanted = {
        name
        for name, kind in OFFICE.items()
        if kind not in ("public", "debug", "platform", "portal")
    }
    assert wanted - covered == set()


def test_site_b_objects_open_for_site_b_manager(world):
    """Positive control: the URLs used below are valid and show site B to its manager."""
    _a, b, ids = world
    office = Office(b.manager)
    for name, url in _office_b_get_urls(ids).items():
        if name == "worksites:edit":
            continue  # admin only
        response = office.get(url)
        assert response.status_code == 200, (name, response.status_code)
    admin = admin_office()
    for name, (url, query) in _list_pages(ids).items():
        response = admin.get(url, query)
        assert response.status_code == 200, name
        if name == "payroll:advance_preview":
            assert response.context["preview"]["worker"].pk == ids["worker"]
        elif name not in ("workers:create", "dashboard:home"):
            assert SECRET in response.content.decode("utf-8-sig"), name
    for name, url in _admin_urls(ids).items():
        if name not in ("accounts:user_set_pin", "accounts:device_revoke"):  # POST only
            assert admin.get(url).status_code == 200, name


@pytest.mark.parametrize("who", ["manager", "foreman"])
def test_site_a_users_cannot_reach_site_b_in_the_office(world, who):
    a, b, ids = world
    office = Office(a.manager if who == "manager" else a.foreman)
    before = _b_state(b)

    for name, url in _office_b_get_urls(ids).items():
        response = office.get(url)
        assert response.status_code in (403, 404), (name, response.status_code)

    for name, (url, data) in _office_b_posts(ids).items():
        for htmx in (False, True):
            response = office.post(url, data, htmx=htmx)
            assert response.status_code in (403, 404), (name, htmx, response.status_code)

    for name, url in _admin_urls(ids).items():
        response = office.post(url, {}) if "pin" in name or "revoke" in name else office.get(url)
        assert response.status_code == 403, name

    for name, (url, query) in _list_pages(ids).items():
        response = office.get(url, query)
        assert response.status_code in (200, 403), name
        assert SECRET not in response.content.decode("utf-8-sig"), name
        if name == "payroll:advance_preview":
            assert response.context["preview"] is None

    # Giving site B's worker an advance from site A's screen is refused.
    response = office.post(
        reverse("payroll:advances"),
        {
            "worker": ids["worker"],
            "amount": "1000",
            "deduction_per_pay": "500",
            "proof_type": "signature",
        },
    )
    assert response.status_code in (200, 403)
    assert _b_state(b) == before


def _api_b_calls(ids):
    site, day, pending = ids["site"], ids["yesterday"].isoformat(), ids["today"].isoformat()
    body = {"client_uuid": new_uuid()}
    return [
        ("get", f"/api/v1/sites/{site}/crew/", {}),
        ("get", f"/api/v1/sites/{site}/days/", {}),
        ("get", f"/api/v1/sites/{site}/days/{day}/", {}),
        ("get", f"/api/v1/sites/{site}/payouts/{day}/", {}),
        (
            "put",
            f"/api/v1/sites/{site}/days/{pending}/records/",
            {"records": [{**body, "worker_id": ids["worker"], "status": "absent"}]},
        ),
        ("post", f"/api/v1/sites/{site}/days/{pending}/submit/", body),
        (
            "post",
            f"/api/v1/records/{ids['record']}/corrections/",
            {**body, "field": "status", "new_value": "absent", "reason": "x"},
        ),
        (
            "post",
            "/api/v1/payments/",
            {**body, "pay_line_id": ids["line"], "amount": 1, "proof_type": "signature"},
        ),
    ]


def test_site_a_foreman_phone_cannot_reach_site_b(world):
    a, b, ids = world
    phone = Phone(a.phone_number, PIN)
    before = _b_state(b)
    for method, url, data in _api_b_calls(ids):
        response = getattr(phone.client, method)(url, data, format="json")
        assert response.status_code == 403, (method, url, response.status_code)
        assert response.json()["code"] == "permission_denied"
    me = phone.me().json()
    assert [s["id"] for s in me["sites"]] == [a.site.pk]
    sync = phone.sync().json()
    assert sync["days"] == sync["records"] == sync["corrections"] == []
    assert SECRET not in str(me) + str(sync)
    assert _b_state(b) == before


def test_site_a_manager_session_on_the_api_cannot_reach_site_b(world):
    a, b, ids = world
    client = APIClient()
    client.force_login(a.manager)
    before = _b_state(b)
    for method, url, data in _api_b_calls(ids):
        response = getattr(client, method)(url, data, format="json")
        assert response.status_code == 403, (method, url, response.status_code)
    assert _b_state(b) == before


def test_site_b_files_are_404_for_site_a_on_the_api(world):
    a, b, ids = world
    urls = [
        reverse("api:file-worker-photo", args=[ids["worker"]]),
        reverse("api:file-payment-proof", args=[ids["payment"]]),
    ]
    own = Phone(b.phone_number, PIN)
    for url in urls:
        assert own.client.get(url).status_code == 200, url  # positive control
    session = APIClient()
    session.force_login(a.manager)
    for client in (Phone(a.phone_number, PIN).client, session):
        for url in urls:
            response = client.get(url)
            assert response.status_code == 404, (url, response.status_code)
            assert response.json()["code"] == "not_found"


def test_site_b_phone_is_not_signed_out_by_site_a(world):
    a, _b, ids = world
    phone = Phone(a.phone_number, PIN)
    assert phone.logout().status_code == 204
    assert ForemanDevice.objects.get(pk=ids["device_b"]).revoked_at is None
