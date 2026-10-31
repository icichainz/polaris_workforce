"""Workers screens: list (filters, pagination), profile, add / edit, scoping."""

import datetime
from unittest import mock

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse

from apps.attendance.models import AttendanceRecord
from apps.core.choices import PayType
from apps.core.models import AuditLog
from apps.workers.models import Worker
from tests.factories import (
    AdminUserFactory,
    AdvanceFactory,
    AttendanceDayFactory,
    AttendanceRecordFactory,
    ForemanFactory,
    PayLineFactory,
    PaymentFactory,
    PayRateFactory,
    SiteAssignmentFactory,
    SiteFactory,
    SiteManagerFactory,
    TradeFactory,
    WorkerFactory,
)

pytestmark = pytest.mark.django_db

TODAY = datetime.date(2026, 10, 31)
D = datetime.date


@pytest.fixture(autouse=True)
def _fixed_today():
    with mock.patch("apps.core.dates.company_today", return_value=TODAY):
        yield


@pytest.fixture
def admin_client(client):
    client.force_login(AdminUserFactory())
    return client


@pytest.fixture
def yao():
    site = SiteFactory(name="Riverside Apartments")
    # A fixed trade: the factory's trade iterator depends on which tests ran before.
    worker = WorkerFactory(
        first_name="Yao",
        last_name="Mensah",
        phone="+228 90 12 34 56",
        trade=TradeFactory(name="Carpenter"),
    )
    SiteAssignmentFactory(site=site, worker=worker, start_date=D(2026, 3, 3))
    PayRateFactory(worker=worker, amount=5500, effective_from=D(2026, 3, 3))
    PayRateFactory(worker=worker, amount=6000, effective_from=D(2026, 9, 1))
    for day_number, status in [(9, "half_day"), (16, "absent"), (22, "late"), (31, "present")]:
        day = AttendanceDayFactory(site=site, date=D(2026, 10, day_number))
        AttendanceRecordFactory(day=day, worker=worker, status=status)
    AdvanceFactory(worker=worker, amount=10_000, balance=4_000, deduction_per_pay=2_000)
    line = PayLineFactory(worker=worker, gross=6000, advance_deduction=2000, net=4000)
    PaymentFactory(pay_line=line, amount=4000)
    return worker


# --- list -------------------------------------------------------------------------------


def test_workers_list_renders_columns(admin_client, yao):
    response = admin_client.get(reverse("workers:list"))

    assert response.status_code == 200
    html = response.content.decode()
    assert "Yao Mensah" in html
    assert yao.number in html
    assert "Riverside Apartments" in html
    assert "6 000" in html and "/ day" in html
    assert "4 000" in html  # advance owed
    assert "Present" in html  # today's badge
    assert "1 active · 0 inactive" in html


def test_workers_list_search_by_name_phone_and_number(admin_client, yao):
    WorkerFactory(first_name="Kodjo", last_name="Atsu", phone="+228 93 08 77 42")
    url = reverse("workers:list")

    for term in ["mensah", "yao men", "90123456", "90 12", yao.number]:
        html = admin_client.get(url, {"q": term}).content.decode()
        assert "Yao Mensah" in html, term
        assert "Kodjo Atsu" not in html, term


def test_workers_list_filters(admin_client, yao):
    mason = TradeFactory(name="Mason")
    other_site = SiteFactory()
    kodjo = WorkerFactory(first_name="Kodjo", last_name="Atsu", trade=mason)
    SiteAssignmentFactory(site=other_site, worker=kodjo)
    WorkerFactory(first_name="Old", last_name="Timer", is_active=False)
    url = reverse("workers:list")

    by_site = admin_client.get(url, {"site": other_site.pk}).content.decode()
    assert "Kodjo Atsu" in by_site and "Yao Mensah" not in by_site
    by_trade = admin_client.get(url, {"trade": mason.pk}).content.decode()
    assert "Kodjo Atsu" in by_trade and "Yao Mensah" not in by_trade
    inactive = admin_client.get(url, {"status": "inactive"}).content.decode()
    assert "Old Timer" in inactive and "Yao Mensah" not in inactive
    everyone = admin_client.get(url, {"status": "all"}).content.decode()
    assert "Old Timer" in everyone and "Yao Mensah" in everyone


def test_workers_list_paginates(admin_client):
    for i in range(30):
        WorkerFactory(last_name=f"Name{i:02d}")
    url = reverse("workers:list")

    first = admin_client.get(url).content.decode()
    assert "Showing 1–25 of 30" in first
    second = admin_client.get(url, {"page": 2}).content.decode()
    assert "Showing 26–30 of 30" in second


def test_workers_list_is_scoped_for_site_managers(client, yao):
    manager = SiteManagerFactory()
    mine = SiteFactory(manager=manager)
    kodjo = WorkerFactory(first_name="Kodjo", last_name="Atsu")
    SiteAssignmentFactory(site=mine, worker=kodjo)
    client.force_login(manager)

    html = client.get(reverse("workers:list")).content.decode()

    assert "Kodjo Atsu" in html and "Yao Mensah" not in html


# --- profile ----------------------------------------------------------------------------


def test_profile_renders_for_admin(admin_client, yao):
    response = admin_client.get(reverse("workers:detail", args=[yao.pk]))

    assert response.status_code == 200
    html = response.content.decode()
    assert "Yao Mensah" in html
    assert "6 000 XOF" in html
    assert "(was 5 500)" in html
    assert "Attendance · October 2026" in html
    assert "Present 1" in html and "Half 1" in html and "Late 1" in html and "Absent 1" in html
    assert "4 000 XOF" in html  # advance owed
    assert "2 000 deducted per pay" in html
    assert "3 Mar 2026 – 31 Aug 2026" in html
    assert "Recent payments" in html
    assert 'href="/workers/" aria-current="page"' in html


def test_profile_month_navigation(admin_client, yao):
    url = reverse("workers:detail", args=[yao.pk])

    html = admin_client.get(url, {"month": "2026-09"}).content.decode()
    assert "Attendance · September 2026" in html
    assert "?month=2026-10" in html
    assert "Attendance · October 2026" in admin_client.get(url, {"month": "junk"}).content.decode()


def test_profile_days_tile_counts_half_days(admin_client, yao):
    response = admin_client.get(reverse("workers:detail", args=[yao.pk]))

    # present 1 + late 1 + half 0.5 = 2.5 days
    assert response.context["units"] == "2.5"


def test_profile_is_scoped(client, yao):
    manager = SiteManagerFactory()
    SiteFactory(manager=manager)
    client.force_login(manager)

    assert client.get(reverse("workers:detail", args=[yao.pk])).status_code == 403
    assert client.get(reverse("workers:edit", args=[yao.pk])).status_code == 403


# --- add / edit -------------------------------------------------------------------------


def _form_data(target_site, **extra):
    data = {
        "first_name": "Kafui",
        "last_name": "Amedome",
        "phone": "+228 99 34 25 71",
        "trade": "",
        "new_trade": "Electrician",
        "id_document_type": "national_id",
        "id_document_number": "",
        "site": target_site.pk,
        "start_date": "2026-11-02",
        "pay_type": "hourly",
        "rate": "900",
        "effective_from": "2026-11-02",
        "payment_method": "",
        "pay_frequency": "",
    }
    data.update(extra)
    return data


def test_add_worker_form_renders(admin_client):
    SiteFactory(name="Market Hall")
    response = admin_client.get(reverse("workers:create"))

    assert response.status_code == 200
    html = response.content.decode()
    assert "Add a worker" in html and "Market Hall" in html
    assert 'enctype="multipart/form-data"' in html
    assert "Company default" in html


def test_add_worker_creates_number_assignment_rate_and_photo(admin_client, settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path
    site = SiteFactory()
    WorkerFactory(number="W-0200")
    photo = SimpleUploadedFile("kafui.jpg", b"\xff\xd8\xff\xe0jpeg", content_type="image/jpeg")

    response = admin_client.post(reverse("workers:create"), _form_data(site, photo=photo))

    worker = Worker.objects.get(last_name="Amedome")
    assert response.status_code == 302
    assert response.url == reverse("workers:detail", args=[worker.pk])
    assert worker.number == "W-0201"
    assert worker.trade.name == "Electrician"
    assert worker.photo.name.startswith("workers/")
    assert worker.assignments.get().site == site
    rate = worker.rate_on(D(2026, 11, 2))
    assert (rate.amount, rate.pay_type) == (900, PayType.HOURLY)
    assert AuditLog.objects.filter(action="workers.rate_change").exists()


def test_add_worker_rejects_bad_photo_and_bad_rate(admin_client):
    site = SiteFactory()
    pdf = SimpleUploadedFile("id.pdf", b"%PDF-1.4", content_type="application/pdf")

    response = admin_client.post(reverse("workers:create"), _form_data(site, photo=pdf, rate="9.5"))

    assert response.status_code == 200
    form = response.context["form"]
    assert "photo" in form.errors
    assert "rate" in form.errors
    assert not Worker.objects.exists()


def test_add_worker_needs_a_site_and_start_date(admin_client):
    site = SiteFactory()

    response = admin_client.post(
        reverse("workers:create"), _form_data(site, site="", start_date="")
    )

    assert response.status_code == 200
    assert {"site", "start_date"} <= set(response.context["form"].errors)


def test_edit_worker_changes_rate_with_history_and_moves_site(admin_client, yao):
    new_site = SiteFactory(name="Route 4 Bridge")
    url = reverse("workers:edit", args=[yao.pk])
    assert admin_client.get(url).status_code == 200

    response = admin_client.post(
        url,
        {
            "first_name": "Yao",
            "last_name": "Mensah",
            "phone": yao.phone,
            "trade": yao.trade_id,
            "id_document_type": "national_id",
            "id_document_number": "TG1",
            "is_active": "on",
            "site": new_site.pk,
            "start_date": "2026-11-02",
            "pay_type": "daily",
            "rate": "6500",
            "effective_from": "2026-11-01",
            "payment_method": "mobile_money",
            "pay_frequency": "",
        },
    )

    assert response.status_code == 302
    yao.refresh_from_db()
    assert yao.payment_method == "mobile_money"
    assert yao.pay_rates.count() == 3
    assert yao.rate_on(D(2026, 10, 31)).amount == 6000
    assert yao.rate_on(D(2026, 11, 1)).amount == 6500
    assert yao.assignments.get(end_date__isnull=True).site == new_site
    assert AuditLog.objects.filter(action="workers.update").exists()


def test_edit_without_changes_adds_no_rate(admin_client, yao):
    url = reverse("workers:edit", args=[yao.pk])
    data = {
        "first_name": yao.first_name,
        "last_name": yao.last_name,
        "phone": yao.phone,
        "trade": yao.trade_id,
        "id_document_type": yao.id_document_type,
        "id_document_number": yao.id_document_number,
        "is_active": "on",
        "site": "",
        "start_date": "",
        "pay_type": "daily",
        "rate": "6000",
        "effective_from": "2026-10-31",
    }

    assert admin_client.post(url, data).status_code == 302
    assert yao.pay_rates.count() == 2
    assert not AuditLog.objects.filter(action="workers.update").exists()


def test_foreman_cannot_add_workers(client):
    client.force_login(ForemanFactory())
    assert client.get(reverse("workers:create")).status_code == 403


def test_records_status_values_match_badges():
    # The list shows today's status through status_badge.html, which knows these codes.
    assert set(AttendanceRecord.Status.values) == {"present", "absent", "half_day", "late"}
