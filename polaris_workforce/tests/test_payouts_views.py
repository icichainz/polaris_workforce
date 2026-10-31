"""Payout office screens, through the browser (CSRF on): the whole flow, permissions,
site scoping, layouts and payout accounts."""

import io

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse

from apps.core.models import AuditLog
from apps.payouts.models import ExportLayout, PayoutAccount, PayoutBatch, PayoutLine
from apps.payroll.models import Payment
from tests.e2e import Office, admin_office
from tests.factories import UserFactory
from tests.test_payouts_services import MOMO, World, momo_account
from tests.test_payroll_services import MON, setup_site

pytestmark = pytest.mark.django_db


@pytest.fixture
def world(db):
    from tests.factories import CompanySettingsFactory

    CompanySettingsFactory()
    return World()


def _prepare_query(world, method=MOMO):
    return {"pay_date": MON.isoformat(), "method": method, "site": world.site.pk}


def _create(office, world):
    response = office.post(reverse("payouts:prepare"), _prepare_query(world))
    assert response.status_code == 302, response
    return PayoutBatch.objects.latest("pk")


def test_the_whole_flow_in_the_office(world):
    office = Office(world.manager)

    page = office.get(reverse("payouts:batches"))
    assert page.status_code == 200
    assert reverse("payouts:batches") in page.content.decode()  # sidebar entry

    preview = office.get(reverse("payouts:prepare"), _prepare_query(world))
    text = preview.content.decode()
    assert preview.status_code == 200
    assert world.yao.full_name in text and world.afi.full_name in text
    assert "1 worker needs a payout account" in text
    assert world.kofi.full_name not in text  # paid in cash

    batch = _create(office, world)
    assert batch.lines.count() == 1
    detail = office.get(reverse("payouts:batch", args=[batch.pk]))
    assert detail.status_code == 200 and "PAY-00001-001" in detail.content.decode()

    exported = office.post(reverse("payouts:batch_export", args=[batch.pk]))
    assert exported.status_code == 200
    assert exported["Content-Disposition"] == (
        'attachment; filename="PAY-00001-2025-09-01-mobile_money.csv"'
    )
    assert b"PAY-00001-001,22890112233" in exported.content
    batch.refresh_from_db()
    assert batch.status == "exported"

    report = SimpleUploadedFile(
        "report.csv", b"Reference,Amount,Status\nPAY-00001-001,6000,OK\nPAY-00009-001,1,OK\n"
    )
    result = office.post(reverse("payouts:batch_import", args=[batch.pk]), {"file": report})
    assert result.status_code == 200
    text = result.content.decode()
    assert "Confirmed" in text and "Not found" in text
    assert Payment.objects.get().method == MOMO

    closed = office.post(reverse("payouts:batch_close", args=[batch.pk]))
    assert closed.status_code == 302
    batch.refresh_from_db()
    assert batch.status == "closed"


def test_confirm_and_fail_by_hand(world):
    momo_account(world.afi, number="+228 90 77 88 99")
    office = Office(world.manager)
    batch = _create(office, world)
    office.post(reverse("payouts:batch_export", args=[batch.pk]))
    yao = batch.lines.get(pay_line__worker=world.yao)
    afi = batch.lines.get(pay_line__worker=world.afi)

    office.post(reverse("payouts:line_confirm", args=[yao.pk]), {"provider_reference": "TX-9"})
    office.post(reverse("payouts:line_confirm", args=[yao.pk]), {"provider_reference": "TX-9"})
    office.post(reverse("payouts:line_fail", args=[afi.pk]), {"reason": "Wrong number"})

    yao.refresh_from_db()
    afi.refresh_from_db()
    assert (yao.status, yao.provider_reference) == ("confirmed", "TX-9")
    assert (afi.status, afi.failure_reason) == ("failed", "Wrong number")
    assert Payment.objects.count() == 1


def test_remove_and_cancel(world):
    office = Office(world.manager)
    batch = _create(office, world)
    line = batch.lines.get()
    office.post(reverse("payouts:line_remove", args=[line.pk]))
    batch.refresh_from_db()
    assert batch.status == "cancelled"

    batch = _create(office, world)
    office.post(reverse("payouts:batch_export", args=[batch.pk]))
    refused = office.post(reverse("payouts:batch_cancel", args=[batch.pk]), {"reason": ""})
    assert refused.status_code == 302
    batch.refresh_from_db()
    assert batch.status == "exported"
    office.post(reverse("payouts:batch_cancel", args=[batch.pk]), {"reason": "Wrong file"})
    batch.refresh_from_db()
    assert batch.status == "cancelled"


def test_nothing_to_pay_is_reported(world):
    office = Office(world.manager)
    _create(office, world)
    response = office.post(reverse("payouts:prepare"), _prepare_query(world))
    assert response.status_code == 302
    assert PayoutBatch.objects.count() == 1


def test_other_sites_people_cannot_see_or_act(world):
    other_site, other_manager, other_foreman = setup_site()
    batch = _create(Office(world.manager), world)
    line = batch.lines.get()

    for user in (other_manager, other_foreman):
        office = Office(user)
        assert office.get(reverse("payouts:batch", args=[batch.pk])).status_code == 404
        listing = office.get(reverse("payouts:batches"), {"status": "all"})
        assert batch.reference not in listing.content.decode()
        for name, args in (
            ("payouts:batch_export", [batch.pk]),
            ("payouts:batch_cancel", [batch.pk]),
            ("payouts:batch_close", [batch.pk]),
            ("payouts:line_confirm", [line.pk]),
            ("payouts:line_fail", [line.pk]),
            ("payouts:line_remove", [line.pk]),
        ):
            assert office.post(reverse(name, args=args), {"reason": "x"}).status_code == 404
        # The site is not offered, and a forged one is refused.
        assert office.post(reverse("payouts:prepare"), _prepare_query(world)).status_code == 302
        preview = office.get(reverse("payouts:prepare"), _prepare_query(world))
        assert world.yao.full_name not in preview.content.decode()
        assert office.get(reverse("payouts:account_edit", args=[world.yao.pk])).status_code == 404
    assert PayoutBatch.objects.count() == 1
    batch.refresh_from_db()
    assert batch.status == "draft"


def test_accountants_read_batches_but_cannot_act(world):
    batch = _create(Office(world.manager), world)
    office = Office(UserFactory(role="accountant"))
    assert office.get(reverse("payouts:batches")).status_code == 200
    page = office.get(reverse("payouts:batch", args=[batch.pk]))
    assert page.status_code == 200
    assert reverse("payouts:batch_export", args=[batch.pk]) not in page.content.decode()
    assert office.post(reverse("payouts:batch_export", args=[batch.pk])).status_code == 403
    assert office.post(reverse("payouts:batch_cancel", args=[batch.pk])).status_code == 403
    batch.refresh_from_db()
    assert batch.status == "draft"


def test_layouts_are_listed_for_everyone_and_edited_by_admins(world):
    manager = Office(world.manager)
    assert manager.get(reverse("payouts:layouts")).status_code == 200
    assert manager.get(reverse("payouts:layout_create")).status_code == 403
    builtin = ExportLayout.objects.get(name="Generic bank CSV")
    assert manager.get(reverse("payouts:layout_edit", args=[builtin.pk])).status_code == 403

    admin = admin_office()
    assert admin.get(reverse("payouts:layout_create")).status_code == 200
    data = {
        "name": "Ecobank bulk",
        "method": "bank_transfer",
        "file_type": "xlsx",
        "separator": ";",
        "date_format": "%d/%m/%Y",
        "amount_format": "minor",
        "encoding": "utf-8",
        "header_row": "on",
        "is_active": "on",
        "columns_text": "text:SAL = Type\nreference = Ref\naccount_number\namount = Montant",
    }
    response = admin.post(reverse("payouts:layout_create"), data)
    assert response.status_code == 302, response.context["form"].errors
    layout = ExportLayout.objects.get(name="Ecobank bulk")
    assert layout.columns[0] == {"field": "text:SAL", "header": "Type"}
    assert layout.file_type == "xlsx"
    assert AuditLog.objects.filter(action="payouts.layout.create").exists()

    bad = admin.post(reverse("payouts:layout_create"), {**data, "columns_text": "salary"})
    assert bad.status_code == 200 and "unknown field" in bad.content.decode()
    taken = admin.post(reverse("payouts:layout_create"), {**data, "name": "ecobank BULK"})
    assert taken.status_code == 200 and "already has this name" in taken.content.decode()

    edit = admin.post(
        reverse("payouts:layout_edit", args=[layout.pk]), {**data, "columns_text": "reference"}
    )
    assert edit.status_code == 302
    layout.refresh_from_db()
    assert layout.columns == [{"field": "reference", "header": "Reference"}]


def test_the_export_layout_can_be_chosen(world):
    office = Office(world.manager)
    batch = _create(office, world)
    xlsx = ExportLayout.objects.create(
        name="Flooz XLSX",
        method=MOMO,
        file_type="xlsx",
        columns=[
            {"field": "mobile_number", "header": "MSISDN"},
            {"field": "amount", "header": "Amt"},
        ],
    )
    response = office.post(reverse("payouts:batch_export", args=[batch.pk]), {"layout": xlsx.pk})
    assert response["Content-Disposition"].endswith('.xlsx"')
    from openpyxl import load_workbook

    sheet = load_workbook(io.BytesIO(response.content)).active
    assert list(sheet.iter_rows(values_only=True)) == [("MSISDN", "Amt"), ("22890112233", 6000)]
    batch.refresh_from_db()
    assert batch.layout == xlsx


def test_payout_accounts_screen(world):
    office = Office(world.manager)
    listing = office.get(reverse("payouts:accounts"))
    text = listing.content.decode()
    assert world.yao.full_name in text and world.afi.full_name in text
    assert world.kofi.full_name not in text  # cash, no account
    missing = office.get(reverse("payouts:accounts"), {"show": "missing"}).content.decode()
    assert world.afi.full_name in missing and world.yao.full_name not in missing

    url = reverse("payouts:account_edit", args=[world.afi.pk])
    assert office.get(url).status_code == 200
    bad = office.post(url, {"method": "mobile_money", "mobile_number": "12"})
    assert bad.status_code == 200
    assert not PayoutAccount.objects.filter(worker=world.afi).exists()
    back = reverse("payouts:prepare") + "?pay_date=2025-09-01&method=mobile_money"
    saved = office.post(
        url,
        {
            "method": "mobile_money",
            "mobile_operator": "T-Money",
            "mobile_number": "+228 93 12 34 56",
            "next": back,
        },
    )
    assert saved.status_code == 302 and saved["Location"] == back
    assert PayoutAccount.objects.get(worker=world.afi).mobile_operator == "T-Money"
    elsewhere = office.post(
        url,
        {
            "method": "mobile_money",
            "mobile_operator": "T-Money",
            "mobile_number": "+228 93 12 34 56",
            "next": "https://evil.example/",
        },
    )
    assert elsewhere["Location"] == reverse("payouts:accounts")

    # Foremen may look, not edit.
    foreman = Office(world.foreman)
    assert foreman.get(reverse("payouts:accounts")).status_code == 200
    assert foreman.get(url).status_code == 403


def test_a_line_of_a_cancelled_batch_is_inert(world):
    office = Office(world.manager)
    batch = _create(office, world)
    office.post(reverse("payouts:batch_cancel", args=[batch.pk]))
    line = batch.lines.get()
    office.post(reverse("payouts:line_confirm", args=[line.pk]))
    line.refresh_from_db()
    assert line.status == PayoutLine.Status.CANCELLED
    assert not Payment.objects.exists()
