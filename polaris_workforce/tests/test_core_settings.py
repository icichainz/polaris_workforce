import datetime
from decimal import Decimal

import pytest
from django.core.exceptions import ValidationError
from django.db import IntegrityError

from apps.core.choices import PayFrequency, PaymentMethod
from apps.core.models import AuditLog, CompanySettings
from apps.core.settings import SITE_OVERRIDABLE, WORKER_OVERRIDABLE, resolve_setting, setting_names
from apps.core.tenancy import NoCurrentCompany, current_company, use_company
from apps.worksites.models import Site
from tests.factories import CompanyFactory, SiteFactory, UserFactory, WorkerFactory


@pytest.mark.django_db
class TestCompanySettings:
    def test_load_creates_the_row_with_defaults(self):
        assert not CompanySettings.objects.exists()

        company = CompanySettings.load()

        assert company.company == current_company()
        assert company.company_name == current_company().name
        assert company.currency == "XOF"
        assert company.currency_decimals == 0
        assert company.time_zone == "UTC"
        assert company.language == "en"
        assert company.attendance_mode == "foreman_roll_call"
        assert company.pay_frequency == "daily"
        assert company.payment_method == "cash"
        assert company.default_pay_type == "daily"
        assert company.work_start == datetime.time(7, 0)
        assert company.work_end == datetime.time(17, 0)
        assert company.late_after == datetime.time(7, 15)
        assert company.roll_call_due == datetime.time(8, 0)
        assert company.break_minutes == 60
        assert company.half_day_units == Decimal("0.50")
        assert company.late_rule == "full_day_flagged"
        assert company.overtime_after_hours == Decimal("9")
        assert company.overtime_multiplier == Decimal("1.5")
        assert company.advance_limit_percent == 50
        assert company.default_advance_deduction == 2000
        assert company.working_days == [1, 2, 3, 4, 5, 6]
        assert company.require_correction_reason is True
        assert company.accepted_proofs == ["photo", "signature", "thumb_mark"]
        assert company.pay_every_n_days is None

    def test_load_returns_the_same_row(self):
        first = CompanySettings.load()
        first.currency = "EUR"
        first.currency_decimals = 2
        first.save()

        again = CompanySettings.load()

        assert again.pk == first.pk
        assert again.currency == "EUR"
        assert CompanySettings.objects.count() == 1

    def test_each_company_has_its_own_row(self):
        mine = CompanySettings.load()
        other_company = CompanyFactory(name="Other Co")
        other = CompanySettings.load(other_company)
        other.currency = "EUR"
        other.save()

        assert CompanySettings.objects.count() == 2
        assert CompanySettings.load().pk == mine.pk
        assert CompanySettings.load().currency == "XOF"
        with use_company(other_company):
            assert CompanySettings.load().currency == "EUR"
        assert CompanySettings.load(other_company.pk).pk == other.pk

    def test_database_rejects_a_second_row_for_a_company(self):
        CompanySettings.load()
        with pytest.raises(IntegrityError):
            CompanySettings.objects.bulk_create([CompanySettings(company=current_company())])

    def test_load_without_a_current_company_fails_loudly(self):
        with use_company(None), pytest.raises(NoCurrentCompany):
            CompanySettings.load()

    def test_every_n_days_requires_the_number(self):
        company = CompanySettings(pay_frequency=PayFrequency.EVERY_N_DAYS)
        with pytest.raises(ValidationError) as exc:
            company.full_clean()
        assert "pay_every_n_days" in exc.value.message_dict

    def test_rejects_bad_working_days_and_time_zone(self):
        company = CompanySettings(working_days=[0, 8], time_zone="Mars/Olympus")
        with pytest.raises(ValidationError) as exc:
            company.full_clean()
        assert {"working_days", "time_zone"} <= set(exc.value.message_dict)


@pytest.mark.django_db
class TestResolveSetting:
    def test_company_level(self):
        company = CompanySettings.load()
        company.payment_method = PaymentMethod.MOBILE_MONEY
        company.save()

        assert resolve_setting("payment_method") == PaymentMethod.MOBILE_MONEY
        assert resolve_setting("currency") == "XOF"

    def test_site_overrides_company(self):
        site = SiteFactory(
            payment_method=PaymentMethod.BANK_TRANSFER, work_start=datetime.time(6, 30)
        )

        assert resolve_setting("payment_method", site=site) == PaymentMethod.BANK_TRANSFER
        assert resolve_setting("work_start", site=site) == datetime.time(6, 30)

    def test_site_without_override_inherits(self):
        site = SiteFactory()

        assert site.payment_method is None
        assert resolve_setting("payment_method", site=site) == PaymentMethod.CASH
        assert resolve_setting("overtime_multiplier", site=site) == Decimal("1.5")

    def test_worker_overrides_site_and_company(self):
        site = SiteFactory(payment_method=PaymentMethod.BANK_TRANSFER)
        worker = WorkerFactory(payment_method=PaymentMethod.MOBILE_MONEY)

        assert resolve_setting("payment_method", site=site, worker=worker) == (
            PaymentMethod.MOBILE_MONEY
        )
        assert resolve_setting("payment_method", worker=worker) == PaymentMethod.MOBILE_MONEY

    def test_worker_without_override_falls_back_to_site(self):
        site = SiteFactory(pay_frequency=PayFrequency.WEEKLY)
        worker = WorkerFactory()

        assert resolve_setting("pay_frequency", site=site, worker=worker) == PayFrequency.WEEKLY

    def test_empty_string_override_inherits(self):
        site = SiteFactory(payment_method="")

        assert resolve_setting("payment_method", site=site) == PaymentMethod.CASH

    def test_non_overridable_names_ignore_site_and_worker(self):
        # Site has no "currency" field: the company value always applies.
        site = SiteFactory()
        worker = WorkerFactory()

        assert resolve_setting("currency", site=site, worker=worker) == "XOF"
        # work_start is site-level only; a worker never overrides it.
        assert "work_start" not in WORKER_OVERRIDABLE

    def test_accepts_a_preloaded_company(self, django_assert_num_queries):
        company = CompanySettings.load()

        with django_assert_num_queries(0):
            assert resolve_setting("currency", company=company) == "XOF"

    def test_unknown_name_raises_key_error(self):
        with pytest.raises(KeyError):
            resolve_setting("does_not_exist")
        with pytest.raises(KeyError):
            resolve_setting("id")

    def test_override_maps_match_the_models(self):
        site_fields = {f.name for f in Site._meta.concrete_fields}
        worker_fields = {f.name for f in WorkerFactory._meta.model._meta.concrete_fields}

        assert SITE_OVERRIDABLE <= site_fields
        assert WORKER_OVERRIDABLE <= worker_fields
        assert SITE_OVERRIDABLE <= setting_names()
        assert WORKER_OVERRIDABLE <= setting_names()


@pytest.mark.django_db
def test_audit_log_record_points_at_its_target():
    actor = UserFactory()
    site = SiteFactory()

    entry = AuditLog.record(
        actor=actor,
        action="site.update",
        target=site,
        before={"status": "active"},
        after={"status": "paused"},
        reason="Rainy season",
    )

    entry.refresh_from_db()
    assert entry.target == site
    assert entry.object_id == str(site.pk)
    assert entry.before == {"status": "active"}
    assert entry.after == {"status": "paused"}
    assert entry.actor == actor
