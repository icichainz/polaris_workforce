"""The worker portal pay card shows the Wave 4 pay rules like the payslip does (Wave 4
integration): piece work and bonuses in the gross, one line per statutory deduction, and
a net that agrees with them."""

import datetime
import html
from decimal import Decimal

import pytest
from django.core.cache import cache
from django.urls import reverse

from apps.payroll import pay_rules, services
from apps.payroll.models import DeductionRule, TaskType
from tests.factories import CompanySettingsFactory
from tests.test_payroll_services import MON, hire, line_of, roll_call, setup_site
from tests.test_portal_auth import give, signed_in

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _clear_cache():
    cache.clear()
    yield
    cache.clear()


@pytest.fixture
def scene(db):
    CompanySettingsFactory(working_days=[1, 2, 3, 4, 5, 6, 7])
    DeductionRule.objects.create(
        name="Social security",
        employee_percent=Decimal("4"),
        employer_percent=Decimal("16"),
        active_from=datetime.date(2025, 1, 1),
    )
    site, manager, _foreman = setup_site(name="Riverside")
    ama = hire(site, 6000, first_name="Ama", last_name="Mensah")
    kofi = hire(site, 6000, first_name="Kofi", last_name="Secretname")
    roll_call(site, MON, {ama: "present", kofi: "present"})
    blocks = TaskType.objects.create(name="Block laying", unit="block", unit_rate=50)
    pay_rules.add_piece_work(manager, site, ama, MON, blocks, Decimal("40"))
    pay_rules.add_bonus(manager, site, ama, MON, 1000, "Finished the slab")
    pay_rules.add_bonus(manager, site, kofi, MON, 700, "Kofi only")
    services.cash_sheet(site, MON)
    give(ama)
    return {"site": site, "ama": ama, "portal": signed_in(ama)}


def test_the_pay_card_shows_piece_work_bonuses_and_deductions(scene):
    line = line_of(scene["site"], MON, scene["ama"])
    # 6 000 day + 2 000 piece work + 1 000 bonus; 4 % social security.
    assert (line.gross, line.piece_amount, line.bonus_amount) == (9000, 2000, 1000)
    assert (line.statutory_deductions, line.net) == (360, 8640)

    response = scene["portal"].get(reverse("portal:pay"))
    text = html.unescape(response.content.decode())

    assert response.status_code == 200
    assert "Piece work · Block laying · 40 block × 50" in text and "2 000 XOF" in text
    assert "Bonus · Finished the slab" in text and "1 000 XOF" in text
    assert "Social security (4 %)" in text and "360 XOF" in text
    assert "9 000 XOF" in text and "8 640 XOF" in text
    assert "Kofi only" not in text
    row = response.context["rows"][0]
    assert row.deductions == 360
