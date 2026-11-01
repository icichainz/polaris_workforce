"""Worker portal pages: home, days, pay (with payslip PDFs), advances, in English and
French. Strictly the signed-in worker's own data: another worker's rows never show and
their payslip is a 404."""

import datetime
import html

import pytest
from django.core.cache import cache
from django.urls import reverse

from apps.payroll import services as payroll_services
from tests.factories import AdvanceFactory, CompanySettingsFactory, PayAdjustmentFactory
from tests.test_payroll_services import MON, TUE, hire, line_of, roll_call, setup_site
from tests.test_portal_auth import PIN, give, signed_in

pytestmark = pytest.mark.django_db

SECRET = "Kofisecret"  # in the other worker's name, advance reason and adjustment reason


@pytest.fixture(autouse=True)
def _clear_cache():
    cache.clear()
    yield
    cache.clear()


@pytest.fixture
def scene(db):
    """Ama (signed in) and Kofi on one site: Monday present (Ama paid), Tuesday Ama late
    and Kofi absent; an advance and a correction each."""
    CompanySettingsFactory(working_days=[1, 2, 3, 4, 5, 6, 7])
    site, manager, foreman = setup_site(name="Riverside")
    ama = hire(site, 6000, first_name="Ama", last_name="Mensah")
    kofi = hire(site, 5000, first_name="Kofi", last_name=SECRET)
    roll_call(site, MON, {ama: "present", kofi: "present"}, foreman=foreman)
    roll_call(
        site, TUE, {ama: ("late", datetime.time(7, 40), None), kofi: "absent"}, foreman=foreman
    )
    payroll_services.cash_sheet(site, MON)
    payroll_services.cash_sheet(site, TUE)
    payroll_services.record_payment(
        manager, line_of(site, MON, ama), amount=6000, proof_type="signature"
    )
    AdvanceFactory(worker=ama, amount=5000, balance=4000, deduction_per_pay=1000, reason="Bus")
    AdvanceFactory(worker=kofi, reason=f"{SECRET} rent")
    PayAdjustmentFactory(source_line=line_of(site, MON, ama), amount=-1500, reason="Left early")
    PayAdjustmentFactory(source_line=line_of(site, MON, kofi), amount=-2500, reason=SECRET)
    give(ama)
    give(kofi, pin="5821")
    return {"site": site, "ama": ama, "kofi": kofi, "portal": signed_in(ama)}


def _text(response) -> str:
    return html.unescape(response.content.decode())


def test_home_shows_last_payment_and_advance(scene):
    response = scene["portal"].get(reverse("portal:home"))
    assert response.status_code == 200
    text = _text(response)
    assert "Hello, Ama" in text
    assert "6 000 XOF" in text  # last payment
    assert "4 000 XOF" in text  # advance owed
    assert "1 000 XOF deducted per pay" in text
    assert "Choose my PIN" in text  # still the office's PIN
    assert SECRET not in text
    assert "no-store" in response["Cache-Control"]
    assert "private" in response["Cache-Control"]


def test_days_show_letters_words_and_day_pay(scene):
    response = scene["portal"].get(reverse("portal:days"), {"month": "2025-09"})
    assert response.status_code == 200
    text = _text(response)
    assert "September 2025" in text
    assert "Present" in text and "Late" in text
    assert "Riverside · 07:40" in text
    assert "6 000 XOF" in text
    assert SECRET not in text
    assert response.context["counts"] == {"present": 1, "absent": 0, "half_day": 0, "late": 1}
    letters = [
        c["kind"] for c in response.context["cells"] if c and c["kind"] in ("present", "late")
    ]
    assert letters == ["present", "late"]


def test_days_month_parameter_is_forgiving(scene):
    for month in ("nonsense", "2025-13", "2999-01", ""):
        assert scene["portal"].get(reverse("portal:days"), {"month": month}).status_code == 200


def test_draft_roll_calls_are_not_shown(scene):
    roll_call(
        scene["site"], MON + datetime.timedelta(days=3), {scene["ama"]: "absent"}, status="draft"
    )
    response = scene["portal"].get(reverse("portal:days"), {"month": "2025-09"})
    assert response.context["counts"]["absent"] == 0


def test_pay_lists_periods_payments_and_corrections(scene):
    response = scene["portal"].get(reverse("portal:pay"))
    assert response.status_code == 200
    text = _text(response)
    rows = response.context["rows"]
    assert [row.line.period.end_date for row in rows] == [TUE, MON]
    assert all(row.line.worker_id == scene["ama"].pk for row in rows)
    assert rows[1].state == "paid"
    assert "Payments received" in text
    assert "Left early" in text and "− 1 500 XOF" in text
    assert SECRET not in text
    for row in rows:
        assert reverse("portal:payslip", args=[row.line.pk]) in text


def test_advances_page(scene):
    response = scene["portal"].get(reverse("portal:advances"))
    assert response.status_code == 200
    text = _text(response)
    assert "Bus" in text and "4 000 XOF" in text and "5 000 XOF" in text
    assert SECRET not in text


def test_own_payslip_downloads(scene):
    line = line_of(scene["site"], MON, scene["ama"])
    response = scene["portal"].get(reverse("portal:payslip", args=[line.pk]))
    assert response.status_code == 200
    assert response["Content-Type"] == "application/pdf"
    assert response["Content-Disposition"].startswith("attachment;")
    assert response.content.startswith(b"%PDF")
    assert "no-store" in response["Cache-Control"]


def test_another_workers_payslip_is_404(scene):
    kofi_line = line_of(scene["site"], MON, scene["kofi"])
    assert scene["portal"].get(reverse("portal:payslip", args=[kofi_line.pk])).status_code == 404
    assert scene["portal"].get(reverse("portal:payslip", args=[999_999])).status_code == 404


def test_signed_out_pages_go_to_the_sign_in(scene):
    from django.test import Client

    client = Client()
    line = line_of(scene["site"], MON, scene["ama"])
    for url in (
        reverse("portal:home"),
        reverse("portal:days"),
        reverse("portal:pay"),
        reverse("portal:advances"),
        reverse("portal:change_pin"),
        reverse("portal:payslip", args=[line.pk]),
    ):
        response = client.get(url)
        assert response.status_code == 302, url
        assert response["Location"] == reverse("portal:sign_in")


def test_a_worker_with_nothing_yet_sees_empty_pages(scene):
    newcomer = hire(scene["site"], 4000, first_name="Yao")
    give(newcomer, pin="6042")
    portal = signed_in(newcomer, "6042")
    for name in ("portal:home", "portal:days", "portal:pay", "portal:advances"):
        response = portal.get(reverse(name))
        assert response.status_code == 200, name
        assert SECRET not in _text(response)
    assert "No pay yet" in _text(portal.get(reverse("portal:pay")))


# --- French -------------------------------------------------------------------------------


def test_days_page_in_french_with_the_switch(scene):
    portal = scene["portal"]
    response = portal.post(reverse("set_language"), {"language": "fr", "next": "/portal/days/"})
    assert response.status_code == 302
    response = portal.get(reverse("portal:days"), {"month": "2025-09"})
    page = response.content.decode()
    assert '<html lang="fr" ' in page
    text = _text(response)
    assert "septembre 2025" in text
    assert "Présent" in text and "Retard" in text
    assert ">R<" in page.replace("\n", "")  # late is R (retard) in French
    assert "Mois précédent" in text
    assert response["Content-Language"] == "fr"


def test_portal_follows_the_company_language(scene):
    from apps.core.models import CompanySettings

    CompanySettings.objects.filter(company=scene["ama"].company).update(language="fr")
    response = scene["portal"].get(reverse("portal:pay"))
    assert '<html lang="fr" ' in response.content.decode()
    text = _text(response)
    assert "Périodes de paie" in text and "Net à payer" in text and "Brut" in text


def test_sign_in_page_in_french(db):
    from django.test import Client

    response = Client().get(reverse("portal:sign_in"), headers={"accept-language": "fr"})
    page = response.content.decode()
    assert '<html lang="fr" ' in page
    assert "Se connecter" in _text(response)


def test_wrong_pin_answer_keeps_the_pin_out_of_the_page(scene):
    from tests.test_portal_auth import PortalClient

    response = PortalClient().sign_in(scene["ama"].phone, "1357")
    assert "1357" not in response.content.decode()
    assert PIN not in response.content.decode()
