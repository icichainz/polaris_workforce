"""Payout services: which lines go by transfer, the batch flow, idempotent confirmation,
importing confirmation reports, permissions, site scoping and the audit trail.

Dates are in September 2025 (in the past): 1 Sep 2025 is a Monday.
"""

import io

import pytest
from django.core.exceptions import PermissionDenied, ValidationError
from django.template import Context, Template
from openpyxl import Workbook

from apps.api.exceptions import Conflict, Locked
from apps.core.models import AuditLog
from apps.payouts import services
from apps.payouts.confirmations import ConfirmationRow
from apps.payouts.models import PayoutAccount, PayoutBatch, PayoutLine
from apps.payroll import services as payroll
from apps.payroll.models import PayLine, Payment
from tests.factories import AdminUserFactory, CompanySettingsFactory, UserFactory
from tests.test_payroll_services import MON, TUE, hire, line_of, roll_call, setup_site

pytestmark = pytest.mark.django_db

MOMO = "mobile_money"
BANK = "bank_transfer"


@pytest.fixture
def company(db):
    return CompanySettingsFactory()


def momo_account(worker, number="+228 90 11 22 33", **kwargs):
    return PayoutAccount.objects.create(
        worker=worker,
        method=MOMO,
        mobile_operator="Flooz",
        mobile_number=number,
        **kwargs,
    )


def bank_account(worker, number="00123456789"):
    return PayoutAccount.objects.create(
        worker=worker, method=BANK, bank_name="Ecobank", bank_code="TG055", account_number=number
    )


class World:
    """A site with an approved Monday: Yao and Afi paid by mobile money (Afi without an
    account), Kodjo by bank transfer, Kofi in cash."""

    def __init__(self):
        self.site, self.manager, self.foreman = setup_site()
        self.yao = hire(self.site, 6000, payment_method=MOMO)
        self.afi = hire(self.site, 3500, payment_method=MOMO)
        self.kodjo = hire(self.site, 5000, payment_method=BANK)
        self.kofi = hire(self.site, 4000)
        momo_account(self.yao)
        bank_account(self.kodjo)
        roll_call(
            self.site,
            MON,
            {w: "present" for w in (self.yao, self.afi, self.kodjo, self.kofi)},
        )
        payroll.cash_sheet(self.site, MON)

    def line(self, worker):
        return line_of(self.site, MON, worker)


@pytest.fixture
def world(company):
    return World()


def exported_batch(world, method=MOMO, user=None):
    user = user or world.manager
    batch = services.create_batch(user, MON, method, world.site)
    services.export_batch(user, batch)
    batch.refresh_from_db()
    return batch


# --- which lines go by transfer --------------------------------------------------------------


def test_transfer_lines_are_the_ones_with_a_transfer_method(world):
    found = services.candidates(world.manager, MON, MOMO, world.site)
    assert {c.worker for c in found} == {world.yao, world.afi}
    by_worker = {c.worker: c for c in found}
    assert by_worker[world.yao].ready and by_worker[world.yao].remaining == 6000
    assert not by_worker[world.afi].ready
    assert by_worker[world.afi].problem == "No payout account."

    bank = services.candidates(world.manager, MON, BANK, world.site)
    assert [c.worker for c in bank] == [world.kodjo]


def test_the_site_payment_method_counts_unless_the_worker_overrides_it(world):
    world.site.payment_method = MOMO
    world.site.save()
    world.kofi.payment_method = None
    world.kofi.save()
    momo_account(world.kofi, number="+228 90 44 55 66")

    found = services.candidates(world.manager, MON, MOMO, world.site)

    assert {c.worker for c in found} == {world.yao, world.afi, world.kofi}


def test_an_account_for_another_method_is_not_used(world):
    PayoutAccount.objects.filter(worker=world.yao).update(method=BANK)
    found = services.candidates(world.manager, MON, MOMO, world.site)
    yao = next(c for c in found if c.worker == world.yao)
    assert not yao.ready
    assert "Bank transfer" in yao.problem


def test_a_sheet_that_is_not_open_has_no_transfers(world):
    roll_call(world.site, TUE, {world.yao: "present"}, status="submitted")
    assert services.candidates(world.manager, TUE, MOMO, world.site) == []


# --- the batch flow --------------------------------------------------------------------------


def test_prepare_a_batch(world):
    batch = services.create_batch(world.manager, MON, MOMO, world.site)

    assert batch.reference == "PAY-00001"
    assert batch.status == PayoutBatch.Status.DRAFT
    assert batch.layout.name == "Generic mobile money CSV"
    assert (batch.currency, batch.currency_decimals) == ("XOF", 0)
    line = batch.lines.get()
    assert line.pay_line == world.line(world.yao)
    assert (line.amount, line.reference, line.status) == (6000, "PAY-00001-001", "pending")
    assert (line.account_holder, line.mobile_operator, line.mobile_number) == (
        world.yao.full_name,
        "Flooz",
        "+228 90 11 22 33",
    )
    entry = AuditLog.objects.get(action="payouts.batch.create")
    assert entry.actor == world.manager
    assert entry.after["lines"] == 1 and entry.after["total"] == 6000

    # The account snapshot does not follow later changes.
    PayoutAccount.objects.filter(worker=world.yao).update(mobile_number="+228 99 99 99 99")
    line.refresh_from_db()
    assert line.mobile_number == "+228 90 11 22 33"


def test_a_line_is_in_one_batch_at_a_time(world):
    services.create_batch(world.manager, MON, MOMO, world.site)
    assert all(not c.ready for c in services.candidates(world.manager, MON, MOMO, world.site))
    with pytest.raises(ValidationError):
        services.create_batch(world.manager, MON, MOMO, world.site)
    second = services.create_batch(world.manager, MON, BANK, world.site)
    assert second.number == 2


def test_export_marks_the_batch_exported_and_is_audited(world):
    batch = services.create_batch(world.manager, MON, MOMO, world.site)

    exported = services.export_batch(world.manager, batch)

    assert exported.content.decode().splitlines()[1].startswith("PAY-00001-001,22890112233,")
    batch.refresh_from_db()
    assert batch.status == PayoutBatch.Status.EXPORTED
    assert batch.export_count == 1 and batch.exported_at is not None
    status = AuditLog.objects.get(action="payouts.batch.status")
    assert (status.before, status.after["status"]) == ({"status": "draft"}, "exported")
    entry = AuditLog.objects.get(action="payouts.batch.export")
    assert entry.after["rows"] == 1 and len(entry.after["sha256"]) == 64

    services.export_batch(world.manager, batch)  # a lost file: download again
    batch.refresh_from_db()
    assert batch.export_count == 2
    assert AuditLog.objects.filter(action="payouts.batch.export").count() == 2
    assert AuditLog.objects.filter(action="payouts.batch.status").count() == 1


def test_confirming_records_the_payment_once(world):
    batch = exported_batch(world)
    line = batch.lines.get()

    confirmed, created = services.confirm_line(world.manager, line, "MP2509.1234")
    again, created_again = services.confirm_line(world.manager, line, "MP2509.1234")

    assert created and not created_again
    payment = Payment.objects.get(pay_line=world.line(world.yao))
    assert (payment.method, payment.amount, payment.proof_type) == (MOMO, 6000, "none")
    assert payment.paid_by == world.manager
    assert payment.client_uuid == services.payment_uuid(line)
    assert confirmed.payment == payment and again.payment == payment
    assert confirmed.provider_reference == "MP2509.1234"
    batch.refresh_from_db()
    assert batch.status == PayoutBatch.Status.CONFIRMED
    assert AuditLog.objects.filter(action="payouts.line.confirm").count() == 1
    assert AuditLog.objects.filter(action="payroll.payment.create").count() == 1
    # The worker is paid: no longer a candidate, and the cash sheet counts it.
    found = services.candidates(world.manager, MON, MOMO, world.site)
    assert [c.worker for c in found] == [world.afi]
    sheet = payroll.cash_sheet(world.site, MON)
    assert next(sl for sl in sheet.lines if sl.worker == world.yao).is_paid


def test_a_transfer_without_reference_is_still_refused_without_proof(world):
    """validate_proof only accepts "no proof" inside transfer_proof()."""
    with pytest.raises(ValidationError):
        payroll.record_payment(
            world.manager, world.line(world.kofi), amount=4000, proof_type="none"
        )
    with payroll.transfer_proof("TX-1"):
        payment = payroll.record_payment(
            world.manager, world.line(world.kofi), amount=4000, proof_type="none"
        )
    assert payment.proof_type == "none"


def test_a_draft_cannot_be_confirmed(world):
    batch = services.create_batch(world.manager, MON, MOMO, world.site)
    with pytest.raises(Conflict):
        services.confirm_line(world.manager, batch.lines.get())
    assert not Payment.objects.exists()


def test_a_failed_transfer_goes_back_to_pay(world):
    batch = exported_batch(world)
    line = batch.lines.get()

    with pytest.raises(ValidationError):
        services.fail_line(world.manager, line, "  ")
    services.fail_line(world.manager, line, "Wallet blocked")
    services.fail_line(world.manager, line, "Wallet blocked")  # idempotent

    line.refresh_from_db()
    assert (line.status, line.failure_reason) == ("failed", "Wallet blocked")
    assert AuditLog.objects.filter(action="payouts.line.fail").count() == 1
    batch.refresh_from_db()
    assert batch.status == PayoutBatch.Status.CONFIRMED  # nothing pending
    with pytest.raises(Conflict):
        services.confirm_line(world.manager, line)
    state = services.transfer_state(world.line(world.yao))
    assert state.line is None and state.failed == line
    # It can go in a new batch, or be paid in cash.
    again = services.create_batch(world.manager, MON, MOMO, world.site)
    assert again.lines.get().pay_line == world.line(world.yao)


def test_partly_confirmed_then_closed(world):
    momo_account(world.afi, number="+228 90 77 88 99")
    batch = exported_batch(world)
    yao, afi = batch.lines.order_by("pay_line__worker__pk")

    services.confirm_line(world.manager, yao)
    batch.refresh_from_db()
    assert batch.status == PayoutBatch.Status.PARTLY_CONFIRMED
    with pytest.raises(Conflict):
        services.close_batch(world.manager, batch)

    services.fail_line(world.manager, afi, "Unknown number")
    services.close_batch(world.manager, batch)
    batch.refresh_from_db()
    assert batch.status == PayoutBatch.Status.CLOSED and batch.closed_at
    with pytest.raises(Locked):
        services.fail_line(world.manager, yao, "late")
    statuses = [
        (e.before["status"], e.after["status"])
        for e in AuditLog.objects.filter(action="payouts.batch.status").order_by("pk")
    ]
    assert statuses == [
        ("draft", "exported"),
        ("exported", "partly_confirmed"),
        ("partly_confirmed", "confirmed"),
        ("confirmed", "closed"),
    ]


def test_cancel_a_batch(world):
    draft = services.create_batch(world.manager, MON, MOMO, world.site)
    services.cancel_batch(world.manager, draft)
    draft.refresh_from_db()
    assert draft.status == PayoutBatch.Status.CANCELLED
    assert draft.lines.get().status == PayoutLine.Status.CANCELLED
    assert any(c.ready for c in services.candidates(world.manager, MON, MOMO, world.site))

    exported = exported_batch(world)
    with pytest.raises(ValidationError):
        services.cancel_batch(world.manager, exported)
    services.cancel_batch(world.manager, exported, "Uploaded the wrong file")
    entry = AuditLog.objects.filter(action="payouts.batch.status").latest("pk")
    assert entry.reason == "Uploaded the wrong file"

    confirmed = exported_batch(world)
    services.confirm_line(world.manager, confirmed.lines.get())
    with pytest.raises(Conflict):
        services.cancel_batch(world.manager, confirmed, "x")


def test_remove_a_line_from_a_draft(world):
    momo_account(world.afi, number="+228 90 77 88 99")
    batch = services.create_batch(world.manager, MON, MOMO, world.site)
    first, second = batch.lines.order_by("reference")
    services.remove_line(world.manager, first)
    first.refresh_from_db()
    assert first.status == PayoutLine.Status.CANCELLED
    services.remove_line(world.manager, second)
    batch.refresh_from_db()
    assert batch.status == PayoutBatch.Status.CANCELLED

    exported = exported_batch(world)
    with pytest.raises(Conflict):
        services.remove_line(world.manager, exported.lines.first())


def test_pay_changed_after_the_batch_was_prepared(world):
    batch = exported_batch(world)
    line = batch.lines.get()
    PayLine.objects.filter(pk=line.pay_line_id).update(net=5000)

    assert services.stale_lines(batch) == {line.pk}
    with pytest.raises(ValidationError) as excinfo:
        services.confirm_line(world.manager, line)
    assert "changed" in excinfo.value.messages[0]
    line.refresh_from_db()
    assert line.status == "pending" and not Payment.objects.exists()


def test_paid_in_cash_meanwhile_is_not_paid_twice(world):
    batch = exported_batch(world)
    payroll.record_payment(
        world.manager, world.line(world.yao), amount=6000, proof_type="signature"
    )
    with pytest.raises(Conflict):
        services.confirm_line(world.manager, batch.lines.get())
    assert Payment.objects.filter(pay_line=world.line(world.yao)).count() == 1


def test_a_locked_period_cannot_be_confirmed(world):
    batch = exported_batch(world)
    payroll.lock_sheet(world.manager, world.site, MON, "Closing the day")
    with pytest.raises(Locked):
        services.confirm_line(world.manager, batch.lines.get())


# --- importing a confirmation report ---------------------------------------------------------


def _csv(*rows) -> bytes:
    return ("\n".join(",".join(r) for r in rows) + "\n").encode()


@pytest.fixture
def two_lines(world):
    momo_account(world.afi, number="+228 90 77 88 99")
    batch = exported_batch(world)
    yao = batch.lines.get(pay_line__worker=world.yao)
    afi = batch.lines.get(pay_line__worker=world.afi)
    return batch, yao, afi


def test_a_good_report_confirms_every_line(world, two_lines):
    batch, yao, afi = two_lines
    report = _csv(
        ("Reference", "Amount", "Status", "Transaction ID"),
        (yao.reference, "6000", "SUCCESS", "TX-1"),
        (afi.reference, "3500", "SUCCESS", "TX-2"),
    )

    result = services.import_confirmations(world.manager, batch, "report.csv", report)

    assert [r.outcome for r in result.rows] == ["confirmed", "confirmed"]
    assert Payment.objects.filter(method=MOMO).count() == 2
    yao.refresh_from_db()
    assert yao.provider_reference == "TX-1"
    batch.refresh_from_db()
    assert batch.status == PayoutBatch.Status.CONFIRMED
    entry = AuditLog.objects.get(action="payouts.batch.import")
    assert entry.after["counts"] == {"confirmed": 2} and entry.after["filename"] == "report.csv"
    assert AuditLog.objects.filter(action="payouts.line.confirm").count() == 2

    # The same report again changes nothing.
    again = services.import_confirmations(world.manager, batch, "report.csv", report)
    assert [r.outcome for r in again.rows] == ["already_confirmed", "already_confirmed"]
    assert Payment.objects.count() == 2


def test_a_partial_report(world, two_lines):
    batch, yao, afi = two_lines
    report = _csv(
        ("Reference", "Amount", "Status", "Reason"),
        (yao.reference, "6000", "Paid", ""),
        (afi.reference, "3500", "Rejected", "Wallet not registered"),
    )

    result = services.import_confirmations(world.manager, batch, "r.csv", report)

    assert [r.outcome for r in result.rows] == ["confirmed", "failed"]
    afi.refresh_from_db()
    assert (afi.status, afi.failure_reason) == ("failed", "Wallet not registered")
    assert Payment.objects.count() == 1
    batch.refresh_from_db()
    assert batch.status == PayoutBatch.Status.CONFIRMED


def test_a_report_with_only_some_lines_leaves_the_rest_pending(world, two_lines):
    batch, yao, _afi = two_lines
    services.import_confirmations(
        world.manager, batch, "r.csv", _csv(("Reference", "Status"), (yao.reference, "OK"))
    )
    batch.refresh_from_db()
    assert batch.status == PayoutBatch.Status.PARTLY_CONFIRMED


def test_unknown_mismatched_and_unreadable_rows_change_nothing(world, two_lines):
    batch, yao, afi = two_lines
    report = _csv(
        ("Reference", "Account", "Amount", "Status"),
        ("PAY-09999-001", "22899999999", "6000", "OK"),
        (yao.reference, "", "5000", "OK"),
        (afi.reference, "", "35OO", "OK"),
        (afi.reference, "", "3500", "Queued"),
    )

    result = services.import_confirmations(world.manager, batch, "r.csv", report)

    assert [r.outcome for r in result.rows] == [
        "unknown",
        "amount_mismatch",
        "error",
        "error",
    ]
    assert result.not_applied == 4
    assert not Payment.objects.exists()
    assert set(batch.lines.values_list("status", flat=True)) == {"pending"}


def test_rows_are_matched_by_account_and_amount_without_a_reference(world, two_lines):
    batch, yao, afi = two_lines
    report = _csv(
        ("MSISDN", "Montant", "Statut"),
        ("90 11 22 33", "6000", "Succès"),  # without the country code
        ("+228 90 77 88 99", "3500", "Échec"),
    )

    result = services.import_confirmations(world.manager, batch, "r.csv", report)

    assert [(r.outcome, r.line) for r in result.rows] == [("confirmed", yao), ("failed", afi)]


def test_the_reference_is_found_inside_a_longer_text(world, two_lines):
    batch, yao, _afi = two_lines
    row = ConfirmationRow(row=2, reference=f"SALAIRE {yao.reference} W-0001")
    result = services.apply_confirmations(world.manager, batch, [row])
    assert result.rows[0].outcome == "confirmed"


def test_check_only_changes_nothing(world, two_lines):
    batch, yao, afi = two_lines
    report = _csv(("Reference", "Status"), (yao.reference, "OK"), (afi.reference, "Failed"))
    result = services.import_confirmations(world.manager, batch, "r.csv", report, check_only=True)
    assert result.check_only
    assert [r.outcome for r in result.rows] == ["confirmed", "failed"]
    assert not Payment.objects.exists()
    assert set(batch.lines.values_list("status", flat=True)) == {"pending"}
    assert not AuditLog.objects.filter(action="payouts.batch.import").exists()


def test_a_failure_reported_after_confirmation_is_flagged(world, two_lines):
    batch, yao, _afi = two_lines
    services.confirm_line(world.manager, yao)
    result = services.import_confirmations(
        world.manager, batch, "r.csv", _csv(("Reference", "Status"), (yao.reference, "Failed"))
    )
    assert result.rows[0].outcome == "error"
    yao.refresh_from_db()
    assert yao.status == "confirmed"


def test_an_xlsx_report(world, two_lines):
    batch, yao, afi = two_lines
    workbook = Workbook()
    sheet = workbook.active
    sheet.append(["Reference", "Amount", "Status"])
    sheet.append([yao.reference, 6000, "Completed"])
    sheet.append([afi.reference, 3500.0, "Completed"])
    out = io.BytesIO()
    workbook.save(out)

    result = services.import_confirmations(world.manager, batch, "report.xlsx", out.getvalue())

    assert [r.outcome for r in result.rows] == ["confirmed", "confirmed"]


def test_a_draft_batch_cannot_import(world):
    batch = services.create_batch(world.manager, MON, MOMO, world.site)
    with pytest.raises(Conflict):
        services.import_confirmations(world.manager, batch, "r.csv", _csv(("Reference",)))


# --- permissions and site scoping ------------------------------------------------------------


def test_who_may_prepare_export_and_confirm(world):
    other_site, other_manager, other_foreman = setup_site()
    accountant = UserFactory(role="accountant")
    admin = AdminUserFactory()

    with pytest.raises(PermissionDenied):
        services.create_batch(other_manager, MON, MOMO, world.site)
    with pytest.raises(PermissionDenied):
        services.create_batch(other_foreman, MON, MOMO, world.site)
    with pytest.raises(PermissionDenied):
        services.create_batch(accountant, MON, MOMO, world.site)
    with pytest.raises(PermissionDenied):
        services.create_batch(world.manager, MON, MOMO, None)  # every site: admins only

    batch = services.create_batch(world.foreman, MON, MOMO, world.site)  # PAY_OUT on own site
    for user in (other_manager, other_foreman, accountant):
        assert not services.can_act(user, batch)
        with pytest.raises(PermissionDenied):
            services.export_batch(user, batch)
    services.export_batch(world.foreman, batch)
    for user in (other_manager, accountant):
        with pytest.raises(PermissionDenied):
            services.confirm_line(user, batch.lines.get())
        with pytest.raises(PermissionDenied):
            services.fail_line(user, batch.lines.get(), "x")
        with pytest.raises(PermissionDenied):
            services.import_confirmations(user, batch, "r.csv", b"Reference\n")
    assert services.can_act(admin, batch)
    assert not Payment.objects.exists()


def test_an_all_sites_batch_takes_every_site_and_is_for_admins(world):
    other_site, other_manager, _f = setup_site()
    bola = hire(other_site, 7000, payment_method=MOMO)
    momo_account(bola, number="+228 90 12 12 12")
    roll_call(other_site, MON, {bola: "present"})
    admin = AdminUserFactory()

    batch = services.create_batch(admin, MON, MOMO)

    assert batch.site is None
    assert {line.pay_line.worker for line in batch.lines.all()} == {world.yao, bola}
    assert batch in services.batches_for(admin)
    assert batch not in services.batches_for(world.manager)
    assert not services.can_act(world.manager, batch)


def test_batches_are_scoped_to_sites(world):
    other_site, other_manager, other_foreman = setup_site()
    batch = services.create_batch(world.manager, MON, MOMO, world.site)

    assert list(services.batches_for(world.manager)) == [batch]
    assert list(services.batches_for(world.foreman)) == [batch]
    assert list(services.batches_for(other_manager)) == []
    assert list(services.batches_for(other_foreman)) == []
    assert list(services.batches_for(UserFactory(role="accountant"))) == [batch]
    # Candidates only come from the sites where the user pays out.
    assert services.candidates(other_manager, MON, MOMO) == []


def test_accounts_follow_worker_editing_rights(world):
    other_site, other_manager, _f = setup_site()
    account = PayoutAccount(
        worker=world.afi, method=MOMO, mobile_operator="T-Money", mobile_number="+228 93 00 00 01"
    )
    with pytest.raises(PermissionDenied):
        services.save_account(other_manager, account)
    with pytest.raises(PermissionDenied):
        services.save_account(world.foreman, account)
    services.save_account(world.manager, account)
    entry = AuditLog.objects.get(action="payouts.account.create")
    assert entry.after["mobile_number"] == "+228 93 00 00 01"

    account.mobile_number = "+228 93 00 00 02"
    services.save_account(world.manager, account)
    entry = AuditLog.objects.get(action="payouts.account.update")
    assert entry.before["mobile_number"] == "+228 93 00 00 01"

    assert world.afi in services.account_workers(world.foreman)
    assert world.afi not in services.account_workers(other_manager)


def test_layouts_are_saved_by_admins_only(world):
    from apps.payouts.models import ExportLayout

    layout = ExportLayout(
        name="Ecobank", method=BANK, columns=[{"field": "reference", "header": "R"}]
    )
    with pytest.raises(PermissionDenied):
        services.save_layout(world.manager, layout)
    admin = AdminUserFactory()
    services.save_layout(admin, layout)
    assert AuditLog.objects.filter(action="payouts.layout.create").exists()
    layout.columns = [{"field": "nonsense"}]
    with pytest.raises(ValidationError):
        services.save_layout(admin, layout)


# --- the cash sheet ----------------------------------------------------------------------------


def test_transfer_state_for_the_cash_sheet(world):
    assert services.transfer_state(world.line(world.kofi)) is None  # cash
    state = services.transfer_state(world.line(world.yao))
    assert state.method == MOMO and state.line is None and state.failed is None

    batch = exported_batch(world)
    state = services.transfer_state(world.line(world.yao))
    assert state.line == batch.lines.get()

    html = Template(
        "{% load payouts_tags %}{% transfer_state line as t %}"
        "{% if t %}{% transfer_cell t day site %}{% else %}CASH{% endif %}"
    )
    rendered = html.render(Context({"line": world.line(world.yao), "day": MON, "site": world.site}))
    assert "By mobile money" in rendered and batch.reference in rendered
    rendered = html.render(Context({"line": world.line(world.afi), "day": MON, "site": world.site}))
    assert "Prepare a transfer batch" in rendered and "pay_date=2025-09-01" in rendered
    cash = html.render(Context({"line": world.line(world.kofi)}))
    assert cash == "CASH"

    services.confirm_line(world.manager, batch.lines.get())
    rendered = html.render(Context({"line": world.line(world.yao)}))
    assert "Paid · mobile money" in rendered
