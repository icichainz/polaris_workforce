"""The cash sheet and transfers (Wave 4 integration): cash workers get the "Mark paid"
form, bank-transfer and mobile-money workers their transfer status from ``apps.payouts``."""

import re

import pytest
from django.urls import reverse

from apps.payouts import services as payouts
from tests.e2e import Office
from tests.test_payouts_services import MOMO, World
from tests.test_payroll_services import MON

pytestmark = pytest.mark.django_db


@pytest.fixture
def world(db):
    from tests.factories import CompanySettingsFactory

    CompanySettingsFactory()
    return World()


def _rows(world, user=None) -> dict:
    """The rendered ``<tr>`` of each worker's line, by worker."""
    office = Office(user or world.manager)
    response = office.get(reverse("payroll:cash_sheet", args=[world.site.pk, MON.isoformat()]))
    assert response.status_code == 200
    html = response.content.decode()
    rows = {}
    for worker in (world.yao, world.afi, world.kodjo, world.kofi):
        line = world.line(worker)
        match = re.search(rf'<tr id="line-{line.pk}".*?</tr>', html, re.S)
        assert match, worker
        rows[worker] = match.group(0)
    return rows


def test_cash_rows_keep_the_mark_paid_form_and_transfer_rows_show_their_status(world):
    rows = _rows(world)

    kofi = rows[world.kofi]
    assert reverse("payroll:cash_sheet_pay", args=[world.line(world.kofi).pk]) in kofi
    assert "Mark paid" in kofi and "Prepare a transfer batch" not in kofi

    for worker, method in ((world.yao, "mobile money"), (world.kodjo, "bank transfer")):
        row = rows[worker]
        assert reverse("payroll:cash_sheet_pay", args=[world.line(worker).pk]) not in row
        assert "Mark paid" not in row
        assert f"By {method}" in row
        assert "Prepare a transfer batch" in row
        assert f"pay_date={MON.isoformat()}" in row and f"site={world.site.pk}" in row


def test_a_transfer_row_follows_its_batch(world):
    batch = payouts.create_batch(world.manager, MON, MOMO, world.site)
    payouts.export_batch(world.manager, batch)
    batch.refresh_from_db()

    yao = _rows(world)[world.yao]
    assert reverse("payouts:batch", args=[batch.pk]) in yao
    assert batch.reference in yao
    assert "Prepare a transfer batch" not in yao

    payouts.confirm_line(world.manager, batch.lines.get(), "MP2509.1234")
    rows = _rows(world)
    assert "Paid" in rows[world.yao] and "Mobile money" in rows[world.yao]
    assert "Mark paid" not in rows[world.yao]
    # The cash worker is unaffected.
    assert "Mark paid" in rows[world.kofi]


def test_a_failed_transfer_shows_the_reason_and_can_be_prepared_again(world):
    batch = payouts.create_batch(world.manager, MON, MOMO, world.site)
    payouts.export_batch(world.manager, batch)
    payouts.fail_line(world.manager, batch.lines.get(), "Wrong number")

    yao = _rows(world)[world.yao]
    assert "failed" in yao and "Wrong number" in yao
    assert "Prepare a transfer batch" in yao
