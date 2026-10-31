"""factory_boy factories for every model, with realistic defaults (XOF, daily pay)."""

import datetime
import uuid
from decimal import Decimal

import factory
from django.contrib.auth import get_user_model
from django.utils import timezone
from factory import fuzzy

from apps.accounts.models import ForemanDevice
from apps.attendance.models import AttendanceCorrection, AttendanceDay, AttendanceRecord
from apps.core.choices import PaymentMethod, PayType, ProofType
from apps.core.models import AuditLog, CompanySettings
from apps.payroll.models import (
    Advance,
    AdvanceRepayment,
    CashIssue,
    PayLine,
    Payment,
    PayPeriod,
)
from apps.workers.models import PayRate, Trade, Worker
from apps.worksites.models import Site, SiteAssignment

User = get_user_model()

# Daily rates seen on West African sites, in XOF (0 decimals).
DAILY_RATES = [3500, 4000, 4500, 5000, 5500, 6000, 7000, 8000]
TRADES = ["Mason", "Carpenter", "Steel fixer", "Labourer", "Electrician", "Plumber", "Painter"]


# --- accounts ---------------------------------------------------------------------------


class UserFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = User
        django_get_or_create = ("username",)
        skip_postgeneration_save = True

    username = factory.Sequence(lambda n: f"user{n}")
    first_name = factory.Faker("first_name", locale="fr_FR")
    last_name = factory.Faker("last_name", locale="fr_FR")
    email = factory.LazyAttribute(lambda o: f"{o.username}@example.com")
    phone = factory.Sequence(lambda n: f"+228 90 {n // 100 % 100:02d} {n % 100:02d} 00")
    role = User.Role.FOREMAN
    password = factory.django.Password("s3cret-pass")


class ForemanFactory(UserFactory):
    role = User.Role.FOREMAN


class SiteManagerFactory(UserFactory):
    role = User.Role.SITE_MANAGER


class AdminUserFactory(UserFactory):
    role = User.Role.ADMIN
    is_staff = True
    is_superuser = True


class ForemanDeviceFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = ForemanDevice

    class Params:
        raw_token = factory.Sequence(lambda n: f"device-token-{n}-{uuid.uuid4().hex}")

    user = factory.SubFactory(ForemanFactory)
    device_id = factory.Sequence(lambda n: f"android-{n:06d}")
    token_hash = factory.LazyAttribute(lambda o: ForemanDevice.hash_token(o.raw_token))
    pin_hash = ""


# --- core -------------------------------------------------------------------------------


class CompanySettingsFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = CompanySettings
        django_get_or_create = ("id",)

    id = CompanySettings.SINGLETON_PK
    company_name = "Polaris Construction"
    country = "Togo"
    currency = "XOF"
    currency_decimals = 0
    time_zone = "Africa/Lome"


class AuditLogFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = AuditLog

    actor = factory.SubFactory(SiteManagerFactory)
    action = "attendance.approve"
    reason = ""


# --- workers ----------------------------------------------------------------------------


class TradeFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = Trade
        django_get_or_create = ("name",)

    name = factory.Iterator(TRADES)


class WorkerFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = Worker

    number = factory.Sequence(lambda n: f"W-{n + 1:04d}")
    first_name = factory.Faker("first_name", locale="fr_FR")
    last_name = factory.Faker("last_name", locale="fr_FR")
    phone = factory.Sequence(lambda n: f"+228 91 {n // 100 % 100:02d} {n % 100:02d} 00")
    id_document_type = Worker.IdDocumentType.NATIONAL_ID
    id_document_number = factory.Sequence(lambda n: f"TG{n:08d}")
    trade = factory.SubFactory(TradeFactory)
    is_active = True


class PayRateFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = PayRate

    worker = factory.SubFactory(WorkerFactory)
    pay_type = PayType.DAILY
    amount = fuzzy.FuzzyChoice(DAILY_RATES)
    effective_from = datetime.date(2026, 1, 1)


# --- worksites --------------------------------------------------------------------------


class SiteFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = Site
        skip_postgeneration_save = True

    name = factory.Sequence(lambda n: f"Site {n + 1} · Résidence Agoè")
    address = "Lomé, Togo"
    status = Site.Status.ACTIVE
    started_on = datetime.date(2026, 3, 1)
    manager = factory.SubFactory(SiteManagerFactory)
    labour_budget = 25_000_000

    @factory.post_generation
    def foremen(self, create, extracted, **kwargs):
        if create and extracted:
            self.foremen.set(extracted)


class SiteAssignmentFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = SiteAssignment

    worker = factory.SubFactory(WorkerFactory)
    site = factory.SubFactory(SiteFactory)
    start_date = datetime.date(2026, 3, 1)
    end_date = None


# --- attendance -------------------------------------------------------------------------


class AttendanceDayFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = AttendanceDay

    site = factory.SubFactory(SiteFactory)
    date = factory.Sequence(lambda n: datetime.date(2026, 10, 1) + datetime.timedelta(days=n))
    foreman = factory.SubFactory(ForemanFactory)
    status = AttendanceDay.Status.DRAFT
    client_uuid = factory.LazyFunction(uuid.uuid4)


class AttendanceRecordFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = AttendanceRecord

    day = factory.SubFactory(AttendanceDayFactory)
    worker = factory.SubFactory(WorkerFactory)
    status = AttendanceRecord.Status.PRESENT
    arrival = datetime.time(7, 2)
    departure = datetime.time(17, 0)
    client_uuid = factory.LazyFunction(uuid.uuid4)
    recorded_at = factory.LazyFunction(timezone.now)


class AttendanceCorrectionFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = AttendanceCorrection

    record = factory.SubFactory(AttendanceRecordFactory)
    field = "status"
    old_value = AttendanceRecord.Status.ABSENT
    new_value = AttendanceRecord.Status.PRESENT
    reason = "Arrived after the roll call; confirmed by the site manager."
    corrected_by = factory.SubFactory(SiteManagerFactory)


# --- payroll ----------------------------------------------------------------------------


class PayPeriodFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = PayPeriod

    site = factory.SubFactory(SiteFactory)
    start_date = factory.Sequence(lambda n: datetime.date(2026, 10, 1) + datetime.timedelta(days=n))
    end_date = factory.LazyAttribute(lambda o: o.start_date)
    frequency = "daily"
    status = PayPeriod.Status.OPEN
    currency = "XOF"
    currency_decimals = 0


class PayLineFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = PayLine

    period = factory.SubFactory(PayPeriodFactory)
    worker = factory.SubFactory(WorkerFactory)
    units = Decimal("1.00")
    overtime_hours = Decimal("0.00")
    gross = fuzzy.FuzzyChoice(DAILY_RATES)
    advance_deduction = 0
    other_deductions = 0
    net = factory.LazyAttribute(lambda o: o.gross - o.advance_deduction - o.other_deductions)


class AdvanceFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = Advance

    worker = factory.SubFactory(WorkerFactory)
    amount = 10_000
    deduction_per_pay = 2_000
    balance = factory.LazyAttribute(lambda o: o.amount)
    reason = "Transport and food until the first pay day."
    given_on = datetime.date(2026, 10, 1)
    given_by = factory.SubFactory(SiteManagerFactory)
    status = Advance.Status.OPEN
    proof_type = ProofType.SIGNATURE


class AdvanceRepaymentFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = AdvanceRepayment

    advance = factory.SubFactory(AdvanceFactory)
    pay_line = factory.SubFactory(PayLineFactory, worker=factory.SelfAttribute("..advance.worker"))
    amount = 2_000


class CashIssueFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = CashIssue

    site = factory.SubFactory(SiteFactory)
    date = datetime.date(2026, 10, 1)
    amount = 150_000
    issued_by = factory.SubFactory(SiteManagerFactory)
    received_by = factory.SubFactory(ForemanFactory)
    note = ""


class PaymentFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = Payment

    pay_line = factory.SubFactory(PayLineFactory)
    method = PaymentMethod.CASH
    amount = factory.LazyAttribute(lambda o: o.pay_line.net)
    paid_by = factory.SubFactory(ForemanFactory)
    paid_at = factory.LazyFunction(timezone.now)
    proof_type = ProofType.THUMB_MARK
    client_uuid = factory.LazyFunction(uuid.uuid4)
