"""End to end: one day of daily pay, from the admin's settings to the locked cash sheet.

admin sets the company → foreman signs in on the phone → roll call (replayed) → submit →
manager approves in the Approvals screen (htmx POST) → payouts → cash issued in the
office → every worker paid over the API (one payment replayed) → reconciliation →
accountant closes and locks the sheet → attendance locked → payslip PDF → dashboard,
reports and CSV agree.

Run once in XOF (0 decimals) and once in EUR (2 decimals). Every amount is an integer in
the currency's smallest unit.
"""

import csv
import io
import json
import logging
import re
from decimal import Decimal

import pytest
from django.core.cache import cache
from django.urls import reverse

from apps.accounts.models import ForemanDevice
from apps.attendance.models import AttendanceDay
from apps.core.models import AuditLog
from apps.payroll import services as payroll_services
from apps.payroll.models import CashIssue, Payment, PayPeriod
from tests.e2e import Office, Phone, admin_office, hire, lines_by_worker, make_site, new_uuid
from tests.factories import CompanySettingsFactory, UserFactory
from tests.test_payroll_services import photo

pytestmark = pytest.mark.django_db

PIN = "7395"
PHONE = "+228 90 55 66 77"

# Marks for the day: (status, arrival, departure). Defaults: 07:00–17:00, 60 min break,
# overtime after 9 h at ×1.5, half day = 0.5, late = full day (flagged).
MARKS = {
    "Ama": ("present", "07:00", "17:00"),  # daily, 9 h: no overtime
    "Kofi": ("half_day", "07:00", "12:00"),  # daily, half
    "Yao": ("late", "07:40", "17:00"),  # daily, full day flagged late
    "Esi": ("present", "07:00", "18:00"),  # hourly, 10 h: 9 + 1 overtime
    "Kwame": ("half_day", "13:00", "17:00"),  # hourly, 4 h (no break on a half day)
}

CURRENCIES = {
    "XOF": {
        "decimals": 0,
        "rates": {
            "Ama": (6000, "daily"),
            "Kofi": (5000, "daily"),
            "Yao": (4500, "daily"),
            "Esi": (750, "hourly"),
            "Kwame": (625, "hourly"),
        },
        # 750 × 9 + 750 × 1 × 1.5 = 7875
        "nets": {"Ama": 6000, "Kofi": 2500, "Yao": 4500, "Esi": 7875, "Kwame": 2500},
        "cash_text": "25 000",
        "cash": 25000,
        "total_text": "23375",
    },
    "EUR": {
        "decimals": 2,
        "rates": {
            "Ama": (4550, "daily"),
            "Kofi": (3999, "daily"),  # half: 1999.5 → 2000 (half up, once per line)
            "Yao": (4100, "daily"),
            "Esi": (1233, "hourly"),  # 11097 + 1849.5 = 12946.5 → 12947
            "Kwame": (1111, "hourly"),
        },
        "nets": {"Ama": 4550, "Kofi": 2000, "Yao": 4100, "Esi": 12947, "Kwame": 4444},
        "cash_text": "300,00",
        "cash": 30000,
        "total_text": "280.41",
    },
}


@pytest.fixture(autouse=True)
def _isolated(settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path
    cache.clear()  # the PIN throttle
    yield
    cache.clear()


def _walk(value):
    """Every key and scalar in a JSON-like value."""
    if isinstance(value, dict):
        for key, item in value.items():
            yield key
            yield from _walk(item)
    elif isinstance(value, list):
        for item in value:
            yield from _walk(item)
    else:
        yield value


@pytest.mark.parametrize("currency", ["XOF", "EUR"])
def test_a_full_day_of_daily_pay(currency, caplog):
    caplog.set_level(logging.DEBUG)
    plan = CURRENCIES[currency]
    CompanySettingsFactory()

    # --- the admin sets the company up (Settings screen, CSRF on) -------------------------
    admin = admin_office()
    company = admin.save_settings(
        company_name="Polaris BTP",
        currency=currency,
        currency_decimals=str(plan["decimals"]),
        working_days=[str(d) for d in range(1, 8)],  # so "today" is always a working day
        pay_frequency="daily",
    )
    assert (company.currency, company.currency_decimals) == (currency, plan["decimals"])
    today = payroll_services.company_today(company)

    crew = make_site("Riverside Apartments", phone_number=PHONE, pin=PIN, office=admin)
    for key, (rate, pay_type) in plan["rates"].items():
        hire(crew, key, rate, pay_type)
    workers = crew.workers
    site_id = crew.site.pk

    # --- the foreman signs in on the phone ------------------------------------------------
    phone = Phone(PHONE, PIN)
    me = phone.me().json()
    assert [s["id"] for s in me["sites"]] == [site_id]
    assert me["company"]["currency"] == {"code": currency, "decimals": plan["decimals"]}

    crew_json = phone.crew(site_id, today).json()
    assert {w["id"] for w in crew_json["workers"]} == {w.pk for w in workers.values()}
    assert crew_json["count"] == 5

    # --- roll call, sent three times (timeouts) -------------------------------------------
    records = [
        {
            "client_uuid": new_uuid(),
            "worker_id": workers[key].pk,
            "status": status,
            "arrival": arrival,
            "departure": departure,
            "note": "",
        }
        for key, (status, arrival, departure) in MARKS.items()
    ]
    first = phone.put_records(site_id, today, records)
    assert first.status_code == 200, first.content
    for _ in range(2):
        again = phone.put_records(site_id, today, records)
        assert again.status_code == 200
        assert again.json()["records"] == first.json()["records"]
    assert AuditLog.objects.filter(action="attendance.records.upsert").count() == 1

    submit_uuid = new_uuid()
    submitted = phone.submit(site_id, today, submit_uuid, "Concrete came late")
    assert submitted.status_code == 200, submitted.content
    assert submitted.json()["status"] == "submitted"
    assert phone.submit(site_id, today, submit_uuid).status_code == 200  # replay

    # Before approval the payout is a preview and payments are refused.
    preview = phone.payout(site_id, today).json()
    assert preview["status"] == "not_open"
    refused = phone.pay(preview["lines"][0]["pay_line_id"], preview["lines"][0]["net"], new_uuid())
    assert refused.status_code == 409
    assert refused.json()["code"] == "conflict"

    # --- the manager approves in the Approvals screen -------------------------------------
    manager = Office(crew.manager)
    board = manager.get(reverse("attendance:approvals"))
    day = AttendanceDay.objects.get(site=crew.site, date=today)
    assert [card["day"].pk for card in board.context["cards"]] == [day.pk]
    manager.approve(day.pk)
    day.refresh_from_db()
    assert day.status == AttendanceDay.Status.APPROVED

    # --- the payout shows the right net per worker ----------------------------------------
    payout = phone.payout(site_id, today).json()
    assert payout["status"] == "open"
    assert payout["currency"] == {"code": currency, "decimals": plan["decimals"]}
    by_worker = lines_by_worker(payout)
    nets = {key: by_worker[w.pk]["net"] for key, w in workers.items()}
    assert nets == plan["nets"]
    assert all(isinstance(n, int) for n in nets.values())
    assert all(line["advance_deduction"] == 0 for line in payout["lines"])
    assert by_worker[workers["Esi"].pk]["units"] == "10.00"  # hours for an hourly worker
    total = sum(plan["nets"].values())
    assert payout["total_to_pay"] == total

    # The dashboard shows what has to go out tonight.
    home = manager.get(reverse("dashboard:home")).context["overview"]
    assert home.totals["cash_tonight"] == total
    assert {k: home.totals[k] for k in ("expected", "present", "half", "late", "absent")} == {
        "expected": 5,
        "present": 2,
        "half": 2,
        "late": 1,
        "absent": 0,
    }

    # --- cash issued in the office --------------------------------------------------------
    manager.issue_cash(site_id, today, plan["cash_text"])
    assert CashIssue.objects.get().amount == plan["cash"]
    assert phone.payout(site_id, today).json()["cash_issued"] == plan["cash"]

    # --- every worker paid over the API, one payment replayed -----------------------------
    replayed = None
    for index, (key, worker) in enumerate(workers.items()):
        line = by_worker[worker.pk]
        client_uuid = new_uuid()
        kwargs = {"proof_type": "photo", "proof": photo()} if index == 0 else {}
        paid = phone.pay(line["pay_line_id"], line["net"], client_uuid, **kwargs)
        assert paid.status_code == 201, (key, paid.content)
        assert paid.json()["amount"] == line["net"]
        if index == 1:
            replayed = (line, client_uuid, paid.json())
    line, client_uuid, original = replayed
    replay = phone.pay(line["pay_line_id"], line["net"], client_uuid)
    assert replay.status_code == 200
    assert replay["Idempotent-Replayed"] == "true"
    assert replay.json() == original
    assert Payment.objects.count() == 5
    assert Payment.objects.filter(proof_type="photo").get().proof_file.name.startswith("proofs/")

    # --- the reconciliation adds up -------------------------------------------------------
    paid_sheet = phone.payout(site_id, today).json()
    assert paid_sheet["status"] == "paid"
    assert paid_sheet["paid_total"] == total
    assert paid_sheet["left_to_pay"] == 0
    assert paid_sheet["cash_in_hand"] == plan["cash"] - total
    assert paid_sheet["paid_count"] == paid_sheet["line_count"] == 5
    office_sheet = manager.get(reverse("payroll:cash_sheet", args=[site_id, today.isoformat()]))
    rec = office_sheet.context["sheet"].reconciliation
    assert (rec.cash_issued, rec.total_to_pay, rec.paid_total, rec.cash_in_hand) == (
        plan["cash"],
        total,
        total,
        plan["cash"] - total,
    )
    assert manager.get(reverse("dashboard:home")).context["overview"].totals["cash_tonight"] == 0

    # --- the accountant closes and locks the sheet ----------------------------------------
    accountant = Office(UserFactory(role="accountant"))
    locked = accountant.lock_sheet(site_id, today)
    assert locked.status_code == 302
    period = PayPeriod.objects.get(site=crew.site)
    assert period.status == PayPeriod.Status.LOCKED
    day.refresh_from_db()
    assert day.status == AttendanceDay.Status.LOCKED
    assert phone.payout(site_id, today).json()["status"] == "closed"
    assert phone.day(site_id, today).json()["status"] == "locked"

    # Locked: the phone can change nothing any more.
    changed = [{**records[0], "status": "absent", "arrival": None, "departure": None}]
    assert phone.put_records(site_id, today, changed).status_code == 423
    record_id = phone.day(site_id, today).json()["records"][0]["id"]
    late_fix = phone.correct(record_id, new_uuid(), "note", "late fix", "Forgot")
    assert late_fix.status_code == 423
    # …but replaying what was already sent still answers 200 (offline outbox).
    assert phone.put_records(site_id, today, records).status_code == 200
    assert phone.pay(line["pay_line_id"], line["net"], client_uuid).status_code == 200

    # --- payslip PDF ----------------------------------------------------------------------
    esi_line = by_worker[workers["Esi"].pk]["pay_line_id"]
    payslip = manager.get(reverse("payroll:payslip", args=[esi_line]))
    assert payslip.status_code == 200
    assert payslip["Content-Type"] == "application/pdf"
    assert payslip.content.startswith(b"%PDF")

    # --- reports and CSV agree with the cash sheet ----------------------------------------
    query = {"period": "custom", "start": today.isoformat(), "end": today.isoformat()}
    report = manager.get(reverse("dashboard:reports"), query).context["report"]
    assert report.labour_cost == total
    assert report.currency == currency
    csv_response = manager.get(reverse("dashboard:reports_csv"), query)
    assert csv_response.status_code == 200
    rows = list(csv.reader(io.StringIO(csv_response.content.decode("utf-8-sig"))))
    header, body, total_row = rows[0], rows[1:-1], rows[-1]
    earned = header.index("Earned")
    by_name = {row[1].split()[0]: Decimal(row[earned]) for row in body}
    scale = Decimal(10) ** plan["decimals"]
    assert {k: int(v * scale) for k, v in by_name.items()} == plan["nets"]
    assert total_row[1] == "Total"
    assert total_row[earned] == plan["total_text"]
    assert int(Decimal(total_row[earned]) * scale) == total
    assert total_row[header.index("Currency")] == currency

    # --- audit trail: every write once, and no secrets ------------------------------------
    actions = list(AuditLog.objects.values_list("action", flat=True))
    for action, count in {
        "settings.update": 1,
        "accounts.pin_set": 1,
        "attendance.day.create": 1,
        "attendance.records.upsert": 1,
        "attendance.day.submit": 1,
        "attendance.day.approve": 1,
        "payroll.cash.issue": 1,
        "payroll.payment.create": 5,
        "payroll.period.lock": 1,
        "attendance.day.lock": 1,
    }.items():
        assert actions.count(action) == count, action
    device = ForemanDevice.objects.get(user=crew.foreman)
    crew.foreman.refresh_from_db()
    secrets = {phone.token, device.token_hash, crew.foreman.pin_hash}
    for entry in AuditLog.objects.all():
        values = [*_walk(entry.before), *_walk(entry.after), entry.reason]
        for value in values:
            assert value != PIN
            assert not any(isinstance(value, str) and s in value for s in secrets)
        assert "pin_hash" not in json.dumps([entry.before, entry.after])
    log_text = caplog.text
    assert not any(s in log_text for s in secrets)
    assert not re.search(rf"(?<![0-9a-f-]){PIN}(?![0-9a-f-])", log_text)
