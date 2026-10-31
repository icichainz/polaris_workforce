"""factory_boy factories for every model, with realistic defaults (XOF, daily pay).

Records belong to the current company (``apps.core.tenancy``; the autouse fixture in
``conftest.py`` makes a default one current). Build another company's data inside
``with use_company(CompanyFactory()):``, or pass ``company=``.
"""

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
from apps.core.models import AuditLog, Company, CompanySettings
from apps.core.tenancy import current_company
from apps.payroll.models import (
    Advance,
    AdvanceRepayment,
    CashIssue,
    PayAdjustment,
    PayAdjustmentApplication,
    PayLine,
    Payment,
    PayPeriod,
)
from apps.workers.models import PayRate, Trade, Worker, WorkerDocument
from apps.worksites.models import Site, SiteAssignment

User = get_user_model()

# Daily rates seen on West African sites, in XOF (0 decimals).
DAILY_RATES = [3500, 4000, 4500, 5000, 5500, 6000, 7000, 8000]
TRADES = ["Mason", "Carpenter", "Steel fixer", "Labourer", "Electrician", "Plumber", "Painter"]


# --- core: companies ---------------------------------------------------------------------


class CompanyFactory(factory.django.DjangoModelFactory):
    """A company (without its settings row: ``CompanySettings.load(company)`` makes it)."""

    class Meta:
        model = Company

    name = factory.Sequence(lambda n: f"Company {n + 1}")
    slug = factory.Sequence(lambda n: f"company-{n + 1}")


# --- accounts ---------------------------------------------------------------------------


class UserFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = User
        django_get_or_create = ("username",)
        skip_postgeneration_save = True

    company = factory.LazyFunction(current_company)
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
    """An admin of the current company (a superuser who belongs to it)."""

    role = User.Role.ADMIN
    is_staff = True
    is_superuser = True


class PlatformAdminFactory(UserFactory):
    """A platform admin: a superuser without a company (switches between companies)."""

    company = None
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


# --- core -------------------------------------------------------------------------------


class CompanySettingsFactory(factory.django.DjangoModelFactory):
    """The current company's settings (an existing row is returned unchanged)."""

    class Meta:
        model = CompanySettings
        django_get_or_create = ("company",)

    company = factory.LazyFunction(current_company)
    company_name = "Polaris Construction"
    country = "Togo"
    currency = "XOF"
    currency_decimals = 0
    time_zone = "Africa/Lome"


class AuditLogFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = AuditLog

    company = factory.LazyFunction(current_company)
    actor = factory.SubFactory(SiteManagerFactory)
    action = "attendance.approve"
    reason = ""


# --- workers ----------------------------------------------------------------------------


class TradeFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = Trade
        django_get_or_create = ("company", "name")

    company = factory.LazyFunction(current_company)
    name = factory.Iterator(TRADES)


class WorkerFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = Worker

    company = factory.LazyFunction(current_company)
    number = factory.Sequence(lambda n: f"W-{n + 1:04d}")
    first_name = factory.Faker("first_name", locale="fr_FR")
    last_name = factory.Faker("last_name", locale="fr_FR")
    phone = factory.Sequence(lambda n: f"+228 91 {n // 100 % 100:02d} {n % 100:02d} 00")
    id_document_type = Worker.IdDocumentType.NATIONAL_ID
    id_document_number = factory.Sequence(lambda n: f"TG{n:08d}")
    trade = factory.SubFactory(TradeFactory, company=factory.SelfAttribute("..company"))
    is_active = True


class PayRateFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = PayRate

    worker = factory.SubFactory(WorkerFactory)
    pay_type = PayType.DAILY
    amount = fuzzy.FuzzyChoice(DAILY_RATES)
    effective_from = datetime.date(2026, 1, 1)


class WorkerDocumentFactory(factory.django.DjangoModelFactory):
    """A document row; ``file`` is only a stored name (no file is written)."""

    class Meta:
        model = WorkerDocument

    worker = factory.SubFactory(WorkerFactory)
    type = WorkerDocument.Type.CONTRACT
    title = "Employment contract"
    file = factory.Sequence(lambda n: f"worker-documents/document-{n}.pdf")
    issued_on = datetime.date(2026, 3, 1)
    expires_on = None


# --- worksites --------------------------------------------------------------------------


class SiteFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = Site
        skip_postgeneration_save = True

    company = factory.LazyFunction(current_company)
    name = factory.Sequence(lambda n: f"Site {n + 1} · Résidence Agoè")
    address = "Lomé, Togo"
    status = Site.Status.ACTIVE
    started_on = datetime.date(2026, 3, 1)
    manager = factory.SubFactory(SiteManagerFactory, company=factory.SelfAttribute("..company"))
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


class PayAdjustmentFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = PayAdjustment

    source_line = factory.SubFactory(PayLineFactory)
    worker = factory.SelfAttribute("source_line.worker")
    site = factory.SelfAttribute("source_line.period.site")
    amount = -6_000
    currency = "XOF"
    currency_decimals = 0
    reason = "Went home sick."
    dates = factory.LazyAttribute(lambda o: [o.source_line.period.end_date.isoformat()])


class PayAdjustmentApplicationFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = PayAdjustmentApplication

    adjustment = factory.SubFactory(PayAdjustmentFactory)
    pay_line = factory.SubFactory(
        PayLineFactory, worker=factory.SelfAttribute("..adjustment.worker")
    )
    amount = factory.LazyAttribute(lambda o: o.adjustment.amount)
