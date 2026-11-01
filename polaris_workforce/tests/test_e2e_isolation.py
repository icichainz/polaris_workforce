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
import io

import openpyxl
import pytest
from django.core.cache import cache
from django.core.files.base import ContentFile
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import URLPattern, URLResolver, get_resolver, reverse
from rest_framework.test import APIClient

from apps.accounts.models import ForemanDevice
from apps.attendance.models import AttendanceCorrection, AttendanceDay, AttendanceRecord
from apps.checkin.models import Badge, CheckIn, KioskDevice, SiteGeofence
from apps.payroll import pay_rules
from apps.payroll import services as payroll_services
from apps.payroll.models import (
    Advance,
    Bonus,
    CashIssue,
    DeductionRule,
    PayLine,
    Payment,
    PieceWorkEntry,
    StoppageDay,
    TaskType,
)
from apps.portal.models import WorkerAccess
from apps.workers.models import WorkerDocument
from apps.worksites.models import SiteAssignment
from tests.e2e import Office, Phone, admin_office, hire, lines_by_worker, make_site, new_uuid
from tests.e2e import record as mark
from tests.factories import CompanySettingsFactory
from tests.test_checkin_seed import seed_checkin

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
#   "payouts":  bank & mobile-money payouts; site and company scoping checked in
#               test_payouts_isolation.py (which builds its own batches)
#   "own":      acts only on the user's own pending work (the import preview kept in the
#               session); checked in test_workers_import.py. AI assistant tokens: the
#               user's own (an admin's: the company's); checked in test_agents_api.py
OFFICE = {
    "dashboard:home": "list",
    "dashboard:reports": "list",
    "dashboard:reports_csv": "list",
    "dashboard:reports_xlsx": "list",
    "dashboard:components": "debug",
    "core:settings": "admin",
    "core:audit_log": "admin",
    "core:worker_photo": "b_get",
    "core:payment_proof": "b_get",
    "core:media_file": "admin",
    "core:companies": "platform",
    "core:company_switch": "platform",
    "core:set_theme": "public",  # display preference; tests/test_theme.py
    "attendance:approvals": "list",
    "attendance:decide": "b_post",
    "attendance:bulk_approve": "b_post",
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
    "payouts:batches": "payouts",
    "payouts:prepare": "payouts",
    "payouts:batch": "payouts",
    "payouts:batch_export": "payouts",
    "payouts:batch_import": "payouts",
    "payouts:batch_close": "payouts",
    "payouts:batch_cancel": "payouts",
    "payouts:line_confirm": "payouts",
    "payouts:line_fail": "payouts",
    "payouts:line_remove": "payouts",
    "payouts:layouts": "payouts",
    "payouts:layout_create": "payouts",
    "payouts:layout_edit": "payouts",
    "payouts:accounts": "payouts",
    "payouts:account_edit": "payouts",
    "payroll:pay_rules": "admin",
    "payroll:deduction_rule_create": "admin",
    "payroll:deduction_rule_edit": "admin",  # another company's rule: 404
    "payroll:task_type_create": "admin",
    "payroll:task_type_edit": "admin",  # another company's task: 404
    "payroll:site_pay_rules": "admin",  # another company's site: 403
    "payroll:day_extras": "b_get",  # and its POST, below
    "payroll:piece_work_delete": "b_post",
    "payroll:bonus_delete": "b_post",
    "workers:list": "list",
    "workers:create": "list",
    "workers:detail": "b_get",
    "workers:edit": "b_get",  # and its POST, below
    "workers:import": "list",
    "workers:import_template": "list",
    "workers:import_confirm": "own",
    "workers:document_upload": "b_post",
    "workers:document_delete": "b_post",
    "workers:document_file": "b_get",
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
    "checkin:home": "list",
    "checkin:site_day": "b_get",
    "checkin:photo": "b_get",
    "checkin:badges": "list",
    "checkin:badge_sheet": "list",
    "checkin:badges_issue_missing": "b_post",
    "checkin:badge_issue": "b_post",
    "checkin:badge_revoke": "b_post",
    "checkin:kiosks": "list",
    "checkin:kiosk_set": "b_post",
    "checkin:kiosk_end": "b_post",
    "checkin:geofences": "list",
    "checkin:geofence": "b_get",  # and its POST, below
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
    # AI assistant access (agent tokens)
    "agents:access": "own",
    "agents:create": "own",
    "agents:revoke": "own",
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
    "api:checkin:checkins": "b_post",
    "api:checkin:device": "list",  # this phone's own kiosk setting
    "api:checkin:setup": "b_get",
    "api:checkin:site-checkins": "b_get",
    "api:checkin:day-suggestions": "b_get",
    "api:checkin:file-photo": "b_get (404)",
    # Office API for AI assistants: agent tokens only (test_site_a_agent_cannot_reach_site_b)
    "api:office:me": "list",
    "api:office:today": "list",
    "api:office:approvals": "list",
    "api:office:workers": "list",
    "api:office:day": "b_get",
    "api:office:cash-sheet": "b_get",
    "api:office:worker": "b_get",
    "api:office:decision-preview": "b_post",
    "api:office:decisions": "b_post (confirmation of a site-B preview)",
    # Presence tracking (test_presence_api: scoping, consent, the tracker's service token)
    "api:presence:tracking-token": "list",  # your own sites only
    "api:presence:tracking-consent": "own",
    "api:presence:events": "tracker service token only",
    "api:presence:site-sessions": "b_get",
}
JPEG = b"\xff\xd8\xff\xe0" + b"0" * 64
PDF = b"%PDF-1.4\n" + b"0" * 64
XLSX_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def page_text(response) -> str:
    """The text of a page, a CSV or an Excel export (every cell of every sheet)."""
    if response.get("Content-Type", "").startswith(XLSX_TYPE):
        workbook = openpyxl.load_workbook(io.BytesIO(response.content), read_only=True)
        cells = [
            str(value)
            for sheet in workbook.worksheets
            for row in sheet.iter_rows(values_only=True)
            for value in row
            if value is not None
        ]
        workbook.close()
        return "\n".join(cells)
    return response.content.decode("utf-8-sig")


def add_document(worker, title):
    """A contract for ``worker`` (the documents URLs need one)."""
    document = WorkerDocument(worker=worker, type=WorkerDocument.Type.CONTRACT, title=title)
    document.file.save("contract.pdf", ContentFile(PDF), save=True)
    return document


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
    contract = add_document(zeb, f"{SECRET} contract")

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
    extras = add_pay_extras(b, zeb, today)

    ids = {
        **extras,
        "site": b.site.pk,
        "yesterday": yesterday,
        "today": today,
        "pending_day": AttendanceDay.objects.get(site=b.site, date=today).pk,
        "record": record_id,
        "line": PayLine.objects.get(worker=zeb, period__end_date=yesterday).pk,
        "payment": Payment.objects.get().pk,
        "worker": zeb.pk,
        "document": contract.pk,
        "assignment": SiteAssignment.objects.get(worker=zeb).pk,
        "manager_b": b.manager.pk,
        "foreman_b": b.foreman.pk,
        "device_b": ForemanDevice.objects.get(user=b.foreman).pk,
    }
    ids.update(seed_checkin(b, zeb))
    return a, b, ids


def add_pay_extras(crew, worker, day, label=SECRET, company_label="") -> dict:
    """Pay rules data of a site (Wave 4): a deduction rule, a piece-work task and entry,
    a bonus and a stoppage day; their ids for the URL checks."""
    company = crew.site.company
    rule = DeductionRule.objects.create(
        name=f"{company_label}levy", employee_percent=1, active_from=day, company=company
    )
    task = TaskType.objects.create(
        name=f"{company_label}plaster", unit="m", unit_rate=10, company=company
    )
    entry = pay_rules.add_piece_work(crew.manager, crew.site, worker, day, task, 2)
    bonus = pay_rules.add_bonus(crew.manager, crew.site, worker, day, 500, f"{label}bonus")
    pay_rules.set_stoppage(crew.manager, crew.site, day, f"{label}rain")
    return {"deduction_rule": rule.pk, "task_type": task.pk, "piece": entry.pk, "bonus": bonus.pk}


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
        "badges": sorted(Badge.objects.values_list("pk", "worker_id", "revoked_at")),
        "kiosks": sorted(KioskDevice.objects.values_list("device_id", "site_id")),
        "geofences": sorted(
            SiteGeofence.objects.values_list("site_id", "latitude", "longitude", "radius_m")
        ),
        "checkins": CheckIn.objects.count(),
        "documents": sorted(WorkerDocument.objects.values_list("pk", "worker_id", "title")),
        "pay_extras": (
            sorted(PieceWorkEntry.objects.filter(site=b.site).values_list("pk", "quantity")),
            sorted(Bonus.objects.filter(site=b.site).values_list("pk", "amount")),
            sorted(StoppageDay.objects.filter(site=b.site).values_list("date", "pay", "percent")),
            sorted(DeductionRule.objects.values_list("pk", "employee_percent")),
            sorted(TaskType.objects.values_list("pk", "unit_rate")),
        ),
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
        "checkin:site_day": reverse("checkin:site_day", args=[site, day]),
        "checkin:photo": reverse("checkin:photo", args=[ids["checkin"]]),
        "checkin:geofence": reverse("checkin:geofence", args=[site]),
        "workers:document_file": reverse("workers:document_file", args=[ids["document"]]),
        "payroll:day_extras": reverse("payroll:day_extras", args=[site, ids["today"].isoformat()]),
    }


def _office_b_posts(ids):
    site, day = ids["site"], ids["yesterday"].isoformat()
    return {
        "attendance:decide": (
            reverse("attendance:decide", args=[ids["pending_day"]]),
            {"action": "approve"},
        ),
        "attendance:bulk_approve": (
            reverse("attendance:bulk_approve"),
            {"days": [ids["pending_day"]]},
        ),
        "workers:document_upload": (
            reverse("workers:document_upload", args=[ids["worker"]]),
            {
                "type": "contract",
                "title": "Hacked",
                "file": SimpleUploadedFile("x.pdf", PDF, content_type="application/pdf"),
            },
        ),
        "workers:document_delete": (
            reverse("workers:document_delete", args=[ids["worker"], ids["document"]]),
            {},
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
        "checkin:badges_issue_missing": (
            reverse("checkin:badges_issue_missing", args=[site]),
            {},
        ),
        "checkin:badge_issue": (reverse("checkin:badge_issue", args=[ids["worker"]]), {}),
        "checkin:badge_revoke": (
            reverse("checkin:badge_revoke", args=[ids["worker"]]),
            {"reason": "x"},
        ),
        "checkin:kiosk_set": (
            reverse("checkin:kiosk_set", args=[ids["device_b"]]),
            {f"d{ids['device_b']}-site": site},
        ),
        "checkin:kiosk_end": (reverse("checkin:kiosk_end", args=[ids["kiosk_device"]]), {}),
        "checkin:geofence": (
            reverse("checkin:geofence", args=[site]),
            {"latitude": "1", "longitude": "1", "radius_m": "100"},
        ),
        "payroll:day_extras": (
            reverse("payroll:day_extras", args=[site, ids["today"].isoformat()]),
            {"action": "bonus", "bonus-worker": ids["worker"], "bonus-amount": "100"},
        ),
        "payroll:piece_work_delete": (
            reverse("payroll:piece_work_delete", args=[ids["piece"]]),
            {"reason": "x"},
        ),
        "payroll:bonus_delete": (reverse("payroll:bonus_delete", args=[ids["bonus"]]), {}),
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
        "dashboard:reports_xlsx": (reverse("dashboard:reports_xlsx"), span),
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
        "workers:import": (reverse("workers:import"), {}),
        "workers:import_template": (reverse("workers:import_template"), {}),
        "worksites:list": (reverse("worksites:list"), {}),
        "portal:office_access": (reverse("portal:office_access"), {}),
        "checkin:home": (reverse("checkin:home"), {}),
        "checkin:badges": (reverse("checkin:badges"), {"site": ids["site"]}),
        "checkin:badge_sheet": (reverse("checkin:badge_sheet"), {"site": ids["site"]}),
        "checkin:kiosks": (reverse("checkin:kiosks"), {}),
        "checkin:geofences": (reverse("checkin:geofences"), {}),
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
        "payroll:pay_rules": reverse("payroll:pay_rules"),
        "payroll:deduction_rule_create": reverse("payroll:deduction_rule_create"),
        "payroll:deduction_rule_edit": reverse(
            "payroll:deduction_rule_edit", args=[ids["deduction_rule"]]
        ),
        "payroll:task_type_create": reverse("payroll:task_type_create"),
        "payroll:task_type_edit": reverse("payroll:task_type_edit", args=[ids["task_type"]]),
        "payroll:site_pay_rules": reverse("payroll:site_pay_rules", args=[ids["site"]]),
    }


def test_checks_cover_every_office_url(world):
    _a, _b, ids = world
    covered = (
        {k.split("?")[0] for k in _office_b_get_urls(ids)}
        | set(_office_b_posts(ids))
        | set(_list_pages(ids))
        | set(_admin_urls(ids))
    )
    own_tests = ("public", "debug", "platform", "portal", "payouts", "own")
    wanted = {name for name, kind in OFFICE.items() if kind not in own_tests}
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
        elif name not in ("workers:create", "workers:import", "dashboard:home"):
            assert SECRET in page_text(response), name
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
        assert SECRET not in page_text(response), name
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
        (
            "post",
            "/api/v1/checkins/",
            {
                **body,
                "site_id": site,
                "direction": "in",
                "at": f"{pending}T07:00:00Z",
                "worker_id": ids["worker"],
            },
        ),
        ("get", f"/api/v1/sites/{site}/checkin-setup/", {}),
        ("get", f"/api/v1/sites/{site}/checkins/{pending}/", {}),
        ("get", f"/api/v1/sites/{site}/days/{pending}/suggestions/", {}),
        ("get", f"/api/v1/sites/{site}/presence-sessions/?date={pending}", {}),
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
        reverse("api:checkin:file-photo", args=[ids["checkin"]]),
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


def _agent(user, can_approve=True):
    from apps.agents.services import create_token

    _token, raw = create_token(user, name="Claude", can_approve=can_approve)
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {raw}")
    return client


def test_site_a_agent_cannot_reach_site_b(world):
    """The office API (AI assistants) with site A manager's token: every site-B object is
    403, the lists hold nothing of site B, and a confirmation made by site B's manager
    is useless to it. Site B's own manager reaches the same URLs (positive control)."""
    a, b, ids = world
    site, day, pending = ids["site"], ids["yesterday"].isoformat(), ids["pending_day"]
    b_urls = [
        ("get", f"/api/v1/office/sites/{site}/days/{day}/", {}),
        ("get", f"/api/v1/office/sites/{site}/cash-sheets/{day}/", {}),
        ("get", f"/api/v1/office/workers/{ids['worker']}/", {}),
        ("get", "/api/v1/office/workers/", {"site": site}),
        ("post", f"/api/v1/office/days/{pending}/decision-preview/", {"decision": "approve"}),
    ]
    own = _agent(b.manager)
    for method, url, data in b_urls:
        response = getattr(own, method)(url, data, format="json")
        assert response.status_code == 200, (url, response.content)
    b_confirmation = own.post(
        f"/api/v1/office/days/{pending}/decision-preview/", {"decision": "approve"}, format="json"
    ).json()["confirmation"]

    client = _agent(a.manager)
    before = _b_state(b)
    for method, url, data in b_urls:
        response = getattr(client, method)(url, data, format="json")
        assert response.status_code == 403, (method, url, response.status_code)
        assert response.json()["code"] == "permission_denied"
    response = client.post("/api/v1/office/decisions/", {"confirmation": b_confirmation})
    assert response.status_code == 400
    lists = "".join(
        str(client.get(f"/api/v1/office/{name}/").json())
        for name in ("me", "today", "approvals", "workers")
    )
    assert SECRET not in lists
    assert _b_state(b) == before


def test_site_b_presence_never_reaches_site_a(world):
    """Presence (1.5) in the roll-call suggestions and the office screens: site B's worker
    seen on site B — and even seen on site A, where he is not on the crew — never shows
    in site A's suggestions, approvals or check-in day; site B sees it (positive control)."""
    from django.utils import timezone

    from apps.core.models import CompanySettings
    from apps.presence.models import PresenceSession

    a, b, ids = world
    settings = CompanySettings.load()
    settings.tracking_enabled = True
    settings.save()
    today = ids["today"]
    start = timezone.now() - datetime.timedelta(minutes=20)
    for site in (b.site, a.site):
        PresenceSession.objects.create(
            site=site,
            subject_kind="worker",
            subject_id=ids["worker"],
            local_date=today,
            started_at=start,
            zones=[SECRET],
            last_event_at=start,
            event_count=1,
        )

    url = "/api/v1/sites/{}/days/{}/suggestions/"
    own = Phone(b.phone_number, PIN).client.get(url.format(b.site.pk, today)).json()
    assert [s["presence"]["zones"] for s in own["suggestions"] if s["presence"]] == [[SECRET]]
    seen_on_b = Office(b.manager).get(reverse("checkin:site_day", args=[b.site.pk, today]))
    assert SECRET in page_text(seen_on_b)

    phone = Phone(a.phone_number, PIN)
    assert phone.client.get(url.format(b.site.pk, today)).status_code == 403
    mine = phone.client.get(url.format(a.site.pk, today)).json()
    assert ids["worker"] not in [s["worker_id"] for s in mine["suggestions"]]
    assert SECRET not in str(mine)

    office = Office(a.manager)
    for page in (
        reverse("checkin:site_day", args=[a.site.pk, today]),
        reverse("attendance:approvals"),
    ):
        response = office.get(page)
        assert response.status_code == 200
        assert SECRET not in page_text(response), page
    assert office.get(reverse("checkin:site_day", args=[b.site.pk, today])).status_code == 403
