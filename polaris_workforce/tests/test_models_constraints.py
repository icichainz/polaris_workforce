"""Every database constraint rejects bad rows, even when ``save()`` validation is skipped."""

import datetime
import uuid

import pytest
from django.db import IntegrityError, transaction

from apps.attendance.models import AttendanceCorrection
from apps.payroll.models import Advance, Payment
from tests.factories import (
    AdvanceFactory,
    AttendanceCorrectionFactory,
    AttendanceDayFactory,
    AttendanceRecordFactory,
    ForemanDeviceFactory,
    PayLineFactory,
    PaymentFactory,
    PayPeriodFactory,
    PayRateFactory,
    SiteAssignmentFactory,
    SiteFactory,
    TradeFactory,
    UserFactory,
    WorkerFactory,
)

pytestmark = pytest.mark.django_db


def assert_integrity_error(create):
    with pytest.raises(IntegrityError), transaction.atomic():
        create()


class TestClientUuid:
    def test_duplicate_record_client_uuid(self):
        record = AttendanceRecordFactory()
        assert_integrity_error(lambda: AttendanceRecordFactory(client_uuid=record.client_uuid))

    def test_duplicate_payment_client_uuid(self):
        payment = PaymentFactory()
        assert_integrity_error(lambda: PaymentFactory(client_uuid=payment.client_uuid))

    def test_duplicate_day_client_uuid(self):
        day = AttendanceDayFactory()
        assert_integrity_error(lambda: AttendanceDayFactory(client_uuid=day.client_uuid))

    def test_days_without_client_uuid_are_allowed(self):
        AttendanceDayFactory(client_uuid=None)
        AttendanceDayFactory(client_uuid=None)

    def test_records_and_payments_get_a_uuid_by_default(self):
        record = AttendanceRecordFactory.build()
        assert isinstance(Payment().client_uuid, uuid.UUID)
        assert isinstance(record.client_uuid, uuid.UUID)


class TestAttendance:
    def test_one_record_per_worker_per_day(self):
        record = AttendanceRecordFactory()
        assert_integrity_error(
            lambda: AttendanceRecordFactory(day=record.day, worker=record.worker)
        )

    def test_same_worker_on_another_day_is_fine(self):
        record = AttendanceRecordFactory()
        AttendanceRecordFactory(worker=record.worker)

    def test_one_attendance_day_per_site_per_date(self):
        day = AttendanceDayFactory()
        assert_integrity_error(lambda: AttendanceDayFactory(site=day.site, date=day.date))

    @pytest.mark.parametrize("reason", ["", "   "])
    def test_correction_requires_a_reason(self, reason):
        assert_integrity_error(lambda: AttendanceCorrectionFactory(reason=reason))

    def test_correction_reason_is_stripped(self):
        correction = AttendanceCorrectionFactory(reason="  Wrong worker ticked  ")
        assert AttendanceCorrection.objects.get(pk=correction.pk).reason == "Wrong worker ticked"


class TestAssignments:
    def test_end_before_start_is_rejected(self):
        assert_integrity_error(
            lambda: SiteAssignmentFactory(
                start_date=datetime.date(2026, 5, 10), end_date=datetime.date(2026, 5, 9)
            )
        )

    @pytest.mark.parametrize("end_date", [None, datetime.date(2026, 5, 10)])
    def test_open_or_same_day_assignment_is_fine(self, end_date):
        SiteAssignmentFactory(start_date=datetime.date(2026, 5, 10), end_date=end_date)

    def test_active_on(self):
        site = SiteFactory()
        current = SiteAssignmentFactory(
            site=site, start_date=datetime.date(2026, 5, 1), end_date=datetime.date(2026, 5, 31)
        )
        open_ended = SiteAssignmentFactory(site=site, start_date=datetime.date(2026, 5, 15))
        SiteAssignmentFactory(site=site, start_date=datetime.date(2026, 6, 1))  # future

        active = site.assignments.active_on(datetime.date(2026, 5, 31))

        assert set(active) == {current, open_ended}


class TestPayroll:
    def test_period_end_before_start_is_rejected(self):
        assert_integrity_error(
            lambda: PayPeriodFactory(
                start_date=datetime.date(2026, 10, 7), end_date=datetime.date(2026, 10, 6)
            )
        )

    def test_one_period_per_site_and_dates(self):
        period = PayPeriodFactory()
        assert_integrity_error(
            lambda: PayPeriodFactory(
                site=period.site, start_date=period.start_date, end_date=period.end_date
            )
        )

    def test_one_pay_line_per_worker_per_period(self):
        line = PayLineFactory()
        assert_integrity_error(lambda: PayLineFactory(period=line.period, worker=line.worker))

    def test_advance_balance_cannot_exceed_amount(self):
        assert_integrity_error(lambda: AdvanceFactory(amount=10_000, balance=12_000))

    def test_advance_balance_cannot_be_negative(self):
        assert_integrity_error(lambda: AdvanceFactory(balance=-1))

    def test_advance_amount_must_be_positive(self):
        assert_integrity_error(lambda: AdvanceFactory(amount=0, balance=0))

    def test_payment_amount_must_be_positive(self):
        assert_integrity_error(lambda: PaymentFactory(amount=0))

    def test_repaid_advance(self):
        advance = AdvanceFactory(balance=0, status=Advance.Status.REPAID)
        assert advance.status == "repaid"


class TestUniqueness:
    def test_worker_number_is_unique(self):
        worker = WorkerFactory()
        assert_integrity_error(lambda: WorkerFactory(number=worker.number))

    def test_trade_name_is_unique(self):
        trade = TradeFactory(name="Mason")
        assert_integrity_error(lambda: TradeFactory._meta.model.objects.create(name=trade.name))

    def test_one_rate_per_worker_per_date(self):
        rate = PayRateFactory()
        assert_integrity_error(
            lambda: PayRateFactory(worker=rate.worker, effective_from=rate.effective_from)
        )

    def test_pay_rate_cannot_be_negative(self):
        assert_integrity_error(lambda: PayRateFactory(amount=-1))

    def test_device_token_hash_is_unique(self):
        device = ForemanDeviceFactory()
        assert_integrity_error(lambda: ForemanDeviceFactory(token_hash=device.token_hash))

    def test_device_id_unique_per_user(self):
        device = ForemanDeviceFactory()
        assert_integrity_error(
            lambda: ForemanDeviceFactory(user=device.user, device_id=device.device_id)
        )

    def test_same_device_id_for_another_user_is_fine(self):
        device = ForemanDeviceFactory()
        ForemanDeviceFactory(user=UserFactory(), device_id=device.device_id)
