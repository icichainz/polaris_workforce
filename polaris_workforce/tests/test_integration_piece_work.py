"""Piece-rate workers end to end (Wave 4 integration): the worker form, the services, the
Excel import, the phone's crew list and the pay (piece work only, no time pay)."""

import datetime
from decimal import Decimal
from unittest import mock

import pytest
from django.core.exceptions import ValidationError
from django.urls import reverse
from rest_framework.test import APIClient

from apps.attendance.models import AttendanceDay
from apps.core.choices import PayType
from apps.payroll import pay_rules, services
from apps.payroll.models import TaskType
from apps.workers import services as worker_services
from apps.workers.models import Worker
from tests.e2e import Office
from tests.factories import (
    AdminUserFactory,
    CompanySettingsFactory,
    SiteFactory,
    SiteManagerFactory,
    TradeFactory,
)
from tests.test_payroll_services import MON, line_of, roll_call, setup_site
from tests.test_workers_import import confirm, csv_file, row, upload
from tests.test_workers_views import _form_data

pytestmark = pytest.mark.django_db


@pytest.fixture
def admin_client(client):
    client.force_login(AdminUserFactory())
    return client


def test_piece_work_is_a_pay_type():
    assert PayType.PIECE == "piece"
    assert str(PayType.PIECE.label) == "Piece work"


def test_the_worker_form_offers_piece_work(admin_client):
    setup_site()
    html = admin_client.get(reverse("workers:create")).content.decode()

    assert 'value="piece"' in html
    assert "Coming later" not in html
    assert "Pay comes from piece-work entries" in html


def test_the_worker_form_creates_a_piece_rate_worker(admin_client):
    site, _manager, _foreman = setup_site()
    with mock.patch("apps.core.dates.company_today", return_value=datetime.date(2026, 10, 31)):
        response = admin_client.post(
            reverse("workers:create"), _form_data(site, pay_type="piece", rate="5000")
        )

    assert response.status_code == 302, response.content
    worker = Worker.objects.get(last_name="Amedome")
    rate = worker.rate_on(datetime.date(2026, 11, 2))
    assert (rate.pay_type, rate.amount) == ("piece", 5000)


def test_services_accept_piece_and_refuse_unknown_pay_types():
    site, manager, _foreman = setup_site()
    worker = worker_services.create_worker(
        actor=manager,
        site=site,
        start_date=MON,
        pay_type=PayType.PIECE,
        amount=4000,
        first_name="Kossi",
        last_name="Ayivi",
    )
    assert worker.rate_on(MON).pay_type == "piece"
    with pytest.raises(ValidationError):
        worker_services.change_rate(worker, 4000, "per_ton", MON)


def test_the_import_reads_piece_work_in_both_languages():
    CompanySettingsFactory(currency_decimals=0)
    SiteFactory(name="Riverside", manager=SiteManagerFactory())
    TradeFactory(name="Mason")
    office = Office(AdminUserFactory())
    rows = [
        row(**{"Pay type": "Piece work"}),
        row(**{"Pay type": "piece", "Phone": "+228 90 22 22 22", "ID document number": "X2"}),
    ]
    response = upload(office, csv_file(rows))
    assert response.context["preview"]["ok"], response.context["preview"]["rows"]
    confirm(office, response)

    assert sorted(
        Worker.objects.filter(last_name="Mensah").values_list("pay_rates__pay_type", flat=True)
    ) == ["piece", "piece"]


def test_a_piece_rate_worker_is_paid_by_the_piece_from_the_screens_to_the_sheet():
    CompanySettingsFactory()
    site, manager, foreman = setup_site()
    kossi = worker_services.create_worker(
        actor=manager,
        site=site,
        start_date=datetime.date(2025, 1, 1),
        pay_type=PayType.PIECE,
        amount=4000,
        first_name="Kossi",
        last_name="Ayivi",
    )
    roll_call(site, MON, {kossi: "present"}, status=AttendanceDay.Status.SUBMITTED)
    blocks = TaskType.objects.create(name="Block laying", unit="block", unit_rate=60)
    pay_rules.add_piece_work(manager, site, kossi, MON, blocks, Decimal("150"), "Wall B")
    AttendanceDay.objects.filter(site=site, date=MON).update(status=AttendanceDay.Status.APPROVED)

    services.cash_sheet(site, MON)
    line = line_of(site, MON, kossi)
    assert (line.gross, line.piece_amount, line.net) == (9000, 9000, 9000)
    assert line.days[0]["pay_type"] == "piece"

    # The phone marks a piece-rate worker like a daily one (contract 1.2: daily | hourly).
    phone = APIClient()
    phone.force_authenticate(foreman)
    crew = phone.get(
        reverse("api:worksites:crew", kwargs={"site_id": site.pk}), {"date": MON.isoformat()}
    )
    assert crew.status_code == 200
    assert [w["pay_type"] for w in crew.json()["workers"]] == ["daily"]
