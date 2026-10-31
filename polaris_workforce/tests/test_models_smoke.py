"""Create and read back every model through its factory, and load its admin pages."""

import datetime

import pytest
from django.contrib import admin
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import ForemanDevice
from apps.core.models import Company, CompanySettings
from apps.payroll.models import PayPeriod
from tests import factories as f

pytestmark = pytest.mark.django_db

FACTORIES = [
    f.CompanyFactory,
    f.UserFactory,
    f.ForemanDeviceFactory,
    f.CompanySettingsFactory,
    f.AuditLogFactory,
    f.TradeFactory,
    f.WorkerFactory,
    f.PayRateFactory,
    f.WorkerDocumentFactory,
    f.SiteFactory,
    f.SiteAssignmentFactory,
    f.AttendanceDayFactory,
    f.AttendanceRecordFactory,
    f.AttendanceCorrectionFactory,
    f.PayPeriodFactory,
    f.PayLineFactory,
    f.AdvanceFactory,
    f.AdvanceRepaymentFactory,
    f.CashIssueFactory,
    f.PaymentFactory,
    f.PayAdjustmentFactory,
    f.PayAdjustmentApplicationFactory,
    f.PayRuleSettingsFactory,
    f.SitePayRulesFactory,
    f.DeductionRuleFactory,
    f.TaskTypeFactory,
    f.PieceWorkEntryFactory,
    f.BonusFactory,
    f.StoppageDayFactory,
]


@pytest.mark.parametrize("factory_class", FACTORIES, ids=lambda fc: fc.__name__)
def test_create_and_read(factory_class):
    obj = factory_class()
    model = factory_class._meta.model

    fetched = model.objects.get(pk=obj.pk)

    assert fetched == obj
    assert str(fetched)


def test_every_project_model_has_a_factory_and_an_admin():
    from django.apps import apps

    project_models = [
        model
        for model in apps.get_models()
        if model._meta.app_label
        in {"core", "accounts", "worksites", "workers", "attendance", "payroll"}
    ]
    factory_models = {factory_class._meta.model for factory_class in FACTORIES}

    assert set(project_models) <= factory_models
    for model in project_models:
        assert admin.site.is_registered(model), model


def test_site_foremen_and_reverse_relations():
    foreman = f.ForemanFactory()
    site = f.SiteFactory(foremen=[foreman])

    assert list(foreman.foreman_sites.all()) == [site]
    assert list(site.manager.managed_sites.all()) == [site]


def test_attendance_relations():
    record = f.AttendanceRecordFactory()
    correction = f.AttendanceCorrectionFactory(record=record)

    assert list(record.day.records.all()) == [record]
    assert list(record.corrections.all()) == [correction]
    assert not record.day.is_locked


def test_payroll_relations():
    line = f.PayLineFactory(gross=5000, advance_deduction=2000)
    payment = f.PaymentFactory(pay_line=line)
    repayment = f.AdvanceRepaymentFactory(pay_line=line, advance__worker=line.worker)

    assert line.net == 3000
    assert payment.amount == 3000
    assert list(line.period.lines.all()) == [line]
    assert list(line.payments.all()) == [payment]
    assert list(repayment.advance.repayments.all()) == [repayment]


def test_pay_period_snapshots_company_currency():
    company = CompanySettings.load()
    company.currency = "EUR"
    company.currency_decimals = 2
    company.save()

    period = PayPeriod.objects.create(
        site=f.SiteFactory(),
        start_date=datetime.date(2026, 10, 1),
        end_date=datetime.date(2026, 10, 7),
        frequency="weekly",
    )

    assert (period.currency, period.currency_decimals) == ("EUR", 2)


def test_foreman_device_helpers():
    device = f.ForemanDeviceFactory(raw_token="raw-secret")

    assert device.token_hash == ForemanDevice.hash_token("raw-secret")
    assert len(device.token_hash) == 64
    assert device.is_active

    device.revoked_at = timezone.now()
    assert not device.is_active
    assert list(device.user.devices.all()) == [device]


def test_files_are_stored_under_their_folders(settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path
    worker = f.WorkerFactory(photo=SimpleUploadedFile("face.jpg", b"jpeg"))
    payment = f.PaymentFactory(proof_file=SimpleUploadedFile("thumb.png", b"png"))

    assert worker.photo.name.startswith("workers/")
    assert payment.proof_file.name.startswith(f"proofs/{payment.created_at:%Y/%m}/")


class TestAdmin:
    """The Django admin is for platform admins (superusers without a company)."""

    @pytest.fixture
    def admin_client(self, client):
        client.force_login(f.PlatformAdminFactory())
        return client

    def test_company_admins_cannot_open_the_django_admin(self, client):
        client.force_login(f.AdminUserFactory())  # a superuser of a company

        response = client.get(reverse("admin:index"))

        assert response.status_code == 302
        assert response.url.startswith(reverse("admin:login"))

    def test_changelists_and_change_pages_load(self, admin_client):
        for factory_class in FACTORIES:
            factory_class()
        for model in admin.site._registry:
            opts = model._meta
            if opts.app_label not in {
                "core",
                "accounts",
                "worksites",
                "workers",
                "attendance",
                "payroll",
            }:
                continue
            url = reverse(f"admin:{opts.app_label}_{opts.model_name}_changelist")
            response = admin_client.get(url, follow=True)
            assert response.status_code == 200, url
            obj = model.objects.first()
            change = reverse(f"admin:{opts.app_label}_{opts.model_name}_change", args=[obj.pk])
            assert admin_client.get(change).status_code == 200, change

    def test_company_settings_cannot_be_added_or_deleted(self, admin_client):
        row = CompanySettings.load()

        assert admin_client.get(reverse("admin:core_companysettings_add")).status_code == 403
        delete = reverse("admin:core_companysettings_delete", args=[row.pk])
        assert admin_client.get(delete).status_code == 403

    def test_company_settings_changelist_lists_one_row_per_company(self, admin_client):
        CompanySettings.load()
        CompanySettings.load(f.CompanyFactory(name="Other Co"))

        response = admin_client.get(reverse("admin:core_companysettings_changelist"))

        assert response.status_code == 200
        assert response.context["cl"].result_count == 2

    def test_adding_a_company_creates_its_settings(self, admin_client):
        response = admin_client.post(
            reverse("admin:core_company_add"),
            {"name": "Lagoon Builders", "slug": "lagoon", "is_active": "on"},
        )

        assert response.status_code == 302
        company = Company.objects.get(slug="lagoon")
        assert CompanySettings.objects.get(company=company).company_name == "Lagoon Builders"
