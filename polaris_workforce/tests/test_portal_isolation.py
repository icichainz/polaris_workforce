"""Worker portal isolation across companies: a worker of company A signed in to the
portal opens every portal URL and sees only their own data; company B's payslips are
404; the office screens of the portal follow the usual company scoping (they are also in
``test_e2e_isolation``'s tables, which ``test_multi_company`` reuses)."""

import pytest
from django.core.cache import cache
from django.urls import reverse

from apps.core.tenancy import use_company
from apps.payroll import services as payroll_services
from apps.portal.models import WorkerAccess
from tests.e2e import Office
from tests.factories import (
    AdminUserFactory,
    AdvanceFactory,
    CompanyFactory,
    CompanySettingsFactory,
    PayAdjustmentFactory,
)
from tests.test_e2e_isolation import OFFICE
from tests.test_payroll_services import MON, hire, line_of, roll_call, setup_site
from tests.test_portal_auth import give, signed_in

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _clear_cache():
    cache.clear()
    yield
    cache.clear()


def _company(label: str, phone: str, currency: str):
    company = CompanyFactory(name=f"{label} Construction", slug=label.lower())
    with use_company(company):
        CompanySettingsFactory(
            company_name=f"{label} Construction", currency=currency, working_days=[*range(1, 8)]
        )
        site, manager, foreman = setup_site(name=f"{label} Tower")
        worker = hire(site, 6000, first_name="Zeb", last_name=f"{label}worker", phone=phone)
        roll_call(site, MON, {worker: "present"}, foreman=foreman)
        payroll_services.cash_sheet(site, MON)
        line = line_of(site, MON, worker)
        payroll_services.record_payment(manager, line, amount=6000, proof_type="signature")
        AdvanceFactory(worker=worker, given_by=manager, reason=f"{label} advance")
        PayAdjustmentFactory(
            source_line=line, amount=-1000, currency=currency, reason=f"{label} correction"
        )
        admin = AdminUserFactory()
        give(worker, actor=admin)
    return {"company": company, "worker": worker, "line": line, "admin": admin}


@pytest.fixture
def companies(db):
    return _company("Alpha", "+228 90 01 01 01", "XOF"), _company(
        "Bravo", "+33 6 12 34 56 78", "EUR"
    )


def _portal_urls(line_id):
    return {
        "portal:home": reverse("portal:home"),
        "portal:days": reverse("portal:days") + "?month=2025-09",
        "portal:pay": reverse("portal:pay"),
        "portal:advances": reverse("portal:advances"),
        "portal:change_pin": reverse("portal:change_pin"),
        "portal:payslip": reverse("portal:payslip", args=[line_id]),
    }


def test_every_portal_page_is_covered_here():
    portal_names = {name for name, kind in OFFICE.items() if kind == "portal"}
    assert portal_names == set(_portal_urls(1))
    assert {name for name in OFFICE if name.startswith("portal:")} >= portal_names


def test_each_worker_sees_only_their_company_and_own_data(companies):
    a, b = companies
    for me, other in ((a, b), (b, a)):
        portal = signed_in(me["worker"])
        label = me["company"].name.split()[0]
        other_label = other["company"].name.split()[0]
        for name, url in _portal_urls(me["line"].pk).items():
            response = portal.get(url)
            assert response.status_code == 200, name
            if name == "portal:payslip":
                continue
            text = response.content.decode()
            assert other_label not in text, name
            assert label in text, name  # the header names the worker's own company
        # Currency follows the worker's company.
        pay = portal.get(reverse("portal:pay")).content.decode()
        mine, theirs = ("EUR", "XOF") if label == "Bravo" else ("XOF", "EUR")
        assert mine in pay, pay[pay.find("Pay periods") :][:1500]
        assert theirs not in pay


def test_another_companys_payslip_is_404(companies):
    a, b = companies
    portal = signed_in(a["worker"])
    assert portal.get(reverse("portal:payslip", args=[b["line"].pk])).status_code == 404
    assert portal.get(reverse("portal:payslip", args=[a["line"].pk])).status_code == 200


def test_signing_in_works_inside_the_workers_company(companies):
    a, b = companies
    WorkerAccess.objects.filter(worker=b["worker"]).update(failed_attempts=0)
    from tests.test_portal_auth import PortalClient

    for _ in range(5):
        PortalClient().sign_in(b["worker"].phone, "1357")
    from apps.core.models import AuditLog
    from apps.portal.services import AUDIT_PIN_LOCKED

    entry = AuditLog.objects.get(action=AUDIT_PIN_LOCKED)
    assert entry.company_id == b["company"].pk
    assert not WorkerAccess.objects.get(worker=a["worker"]).is_locked


def test_company_admins_manage_only_their_own_workers(companies):
    a, b = companies
    office = Office(a["admin"])
    listing = office.get(reverse("portal:office_access")).content.decode()
    assert "Alphaworker" in listing and "Bravoworker" not in listing
    worker_b = b["worker"].pk
    assert office.get(reverse("portal:office_worker", args=[worker_b])).status_code == 404
    before = list(WorkerAccess.objects.values_list("worker_id", "is_enabled", "pin_hash"))
    assert office.post(reverse("portal:office_set_pin", args=[worker_b])).status_code == 404
    assert office.post(reverse("portal:office_revoke", args=[worker_b])).status_code == 404
    assert list(WorkerAccess.objects.values_list("worker_id", "is_enabled", "pin_hash")) == before


def test_accesses_belong_to_their_workers_company(companies):
    for world in companies:
        access = WorkerAccess.objects.get(worker=world["worker"])
        assert access.company_id == world["company"].pk == world["worker"].company_id


def test_a_portal_session_ignores_an_office_user_of_another_company(companies):
    a, b = companies
    portal = signed_in(a["worker"])
    # An office user of company B signs in on the same browser afterwards.
    portal.client.force_login(b["admin"])
    response = portal.get(reverse("portal:pay"))
    assert response.status_code == 200
    text = response.content.decode()
    assert "Alphaworker" in text and "Bravo" not in text and "XOF" in text
