"""Payouts across companies: every payout URL of company B (classified "payouts" in
``test_e2e_isolation.OFFICE``) answers 403/404 to company A's admin, accountant, site
manager and foreman, nothing of B changes, and B's batches never show in A's lists."""

import datetime

import pytest
from django.urls import reverse

from apps.core.models import AuditLog
from apps.core.tenancy import use_company
from apps.payouts import layouts
from apps.payouts.models import ExportLayout, PayoutAccount, PayoutBatch, PayoutLine
from apps.payroll.models import PayLine, Payment
from tests.e2e import Office
from tests.test_e2e_isolation import OFFICE
from tests.test_multi_company import build_company

pytestmark = pytest.mark.django_db


@pytest.fixture
def worlds(db):
    a = build_company("Alpha", "+228 90 01 01 01")
    b = build_company("Bravo", "+228 90 02 02 02")
    with use_company(b.company):
        layouts.ensure_default_layouts(b.company.pk)
        layout = ExportLayout.objects.create(
            name="Bravo bank layout",
            method="mobile_money",
            columns=[{"field": "reference", "header": "Bravo ref"}],
        )
        PayoutAccount.objects.create(
            worker=b.worker,
            method="mobile_money",
            mobile_operator="Flooz",
            mobile_number="+228 90 02 02 99",
            account_holder="Bravoholder",
        )
        batch = PayoutBatch.objects.create(
            number=1,
            method="mobile_money",
            pay_date=b.ids["yesterday"],
            site=b.crew.site,
            layout=layout,
            currency="XOF",
            status=PayoutBatch.Status.EXPORTED,
        )
        line = PayoutLine.objects.create(
            batch=batch,
            pay_line=PayLine.objects.get(pk=b.ids["line"]),
            amount=6000,
            reference="PAY-00001-001",
            method="mobile_money",
            account_holder="Bravoholder",
            mobile_number="+228 90 02 02 99",
        )
    b.ids.update({"batch": batch.pk, "payout_line": line.pk, "layout": layout.pk})
    return a, b


def _gets(ids):
    return {
        "payouts:batch": reverse("payouts:batch", args=[ids["batch"]]),
        "payouts:layout_edit": reverse("payouts:layout_edit", args=[ids["layout"]]),
        "payouts:account_edit": reverse("payouts:account_edit", args=[ids["worker"]]),
    }


def _posts(ids):
    batch, line = ids["batch"], ids["payout_line"]
    return {
        "payouts:batch_export": (reverse("payouts:batch_export", args=[batch]), {}),
        "payouts:batch_import": (reverse("payouts:batch_import", args=[batch]), {}),
        "payouts:batch_close": (reverse("payouts:batch_close", args=[batch]), {}),
        "payouts:batch_cancel": (reverse("payouts:batch_cancel", args=[batch]), {"reason": "x"}),
        "payouts:line_confirm": (reverse("payouts:line_confirm", args=[line]), {}),
        "payouts:line_fail": (reverse("payouts:line_fail", args=[line]), {"reason": "x"}),
        "payouts:line_remove": (reverse("payouts:line_remove", args=[line]), {}),
        "payouts:layout_edit": (
            reverse("payouts:layout_edit", args=[ids["layout"]]),
            {"name": "Hacked", "columns_text": "reference", "method": "mobile_money"},
        ),
        "payouts:account_edit": (
            reverse("payouts:account_edit", args=[ids["worker"]]),
            {"method": "mobile_money", "mobile_operator": "X", "mobile_number": "+22899999999"},
        ),
        "payouts:prepare": (
            reverse("payouts:prepare"),
            {
                "pay_date": ids["yesterday"].isoformat(),
                "method": "mobile_money",
                "site": ids["site"],
            },
        ),
    }


def _lists(ids):
    return {
        "payouts:batches": (reverse("payouts:batches"), {"status": "all"}),
        "payouts:layouts": (reverse("payouts:layouts"), {}),
        "payouts:accounts": (reverse("payouts:accounts"), {"show": "all"}),
        "payouts:layout_create": (reverse("payouts:layout_create"), {}),
        "payouts:prepare": (
            reverse("payouts:prepare"),
            {
                "pay_date": ids["yesterday"].isoformat(),
                "method": "mobile_money",
                "site": ids["site"],
            },
        ),
    }


def _b_state(b):
    company = b.company
    return {
        "batches": sorted(
            PayoutBatch.objects.filter(company=company).values_list("pk", "status", "layout_id")
        ),
        "lines": sorted(
            PayoutLine.objects.filter(batch__company=company).values_list(
                "pk", "status", "provider_reference"
            )
        ),
        "layouts": sorted(
            ExportLayout.objects.filter(company=company).values_list("pk", "name", "columns")
        ),
        "accounts": sorted(
            PayoutAccount.objects.filter(worker__company=company).values_list("pk", "mobile_number")
        ),
        "payments": Payment.objects.filter(pay_line__worker__company=company).count(),
        "payout_audit": AuditLog.objects.filter(action__startswith="payouts.").count(),
    }


def test_every_payout_url_is_covered():
    ids = dict.fromkeys(("batch", "payout_line", "layout", "worker", "site"), 1)
    ids["yesterday"] = datetime.date(2025, 1, 1)
    covered = set(_gets(ids)) | set(_posts(ids)) | set(_lists(ids))
    assert {name for name, kind in OFFICE.items() if kind == "payouts"} == covered


def test_company_b_admin_opens_company_b_payouts(worlds):
    _a, b = worlds
    office = Office(b.admin)
    for name, url in _gets(b.ids).items():
        assert office.get(url).status_code == 200, name
    listing = office.get(reverse("payouts:batches"), {"status": "all"}).content.decode()
    assert "PAY-00001" in listing
    assert "Bravo bank layout" in office.get(reverse("payouts:layouts")).content.decode()


@pytest.mark.parametrize("who", ["admin", "accountant", "manager", "foreman"])
def test_company_a_cannot_reach_company_b_payouts(worlds, who):
    a, b = worlds
    user = {"manager": a.crew.manager, "foreman": a.crew.foreman}.get(who) or getattr(a, who)
    office = Office(user)
    before = _b_state(b)

    for name, url in _gets(b.ids).items():
        response = office.get(url)
        assert response.status_code in (403, 404), (name, response.status_code)

    for name, (url, data) in _posts(b.ids).items():
        response = office.post(url, data)
        if name == "payouts:prepare":
            # A's own screen: B's site is not A's, so nothing is created.
            assert response.status_code in (302, 403), (name, response.status_code)
        else:
            assert response.status_code in (403, 404), (name, response.status_code)

    for name, (url, query) in _lists(b.ids).items():
        response = office.get(url, query)
        assert response.status_code in (200, 403), (name, response.status_code)
        text = response.content.decode()
        assert "Bravo" not in text, name
        assert "PAY-00001" not in text, name

    assert _b_state(b) == before
