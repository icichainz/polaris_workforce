"""Office screens: pay periods, cash sheets (htmx "Mark paid"), advances, payslip PDF."""

import datetime
import re
import uuid
import zlib

import pytest
from django.urls import reverse

from apps.core import dates
from apps.payroll import labels, pdf, services
from apps.payroll.models import Advance, PayLine, Payment, PayPeriod
from tests.factories import (
    AdvanceFactory,
    CompanySettingsFactory,
    SiteAssignmentFactory,
    UserFactory,
    WorkerFactory,
)
from tests.test_payroll_services import (
    MON,
    SAT,
    SUBMITTED,
    TUE,
    hire,
    line_of,
    photo,
    roll_call,
    setup_site,
)


@pytest.fixture
def company(db, settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path
    return CompanySettingsFactory()


@pytest.fixture
def paid_day(company):
    """A site with an approved Monday: Yao (advance) and Afi (half day)."""
    site, manager, foreman = setup_site(name="Market Hall Renovation")
    yao = hire(site, 6000, first_name="Yao", last_name="Mensah")
    afi = hire(site, 3500, first_name="Afi", last_name="Dosseh")
    AdvanceFactory(
        worker=yao,
        amount=10000,
        balance=10000,
        deduction_per_pay=2000,
        given_on=MON - datetime.timedelta(days=3),
    )
    roll_call(site, MON, {yao: "present", afi: "half_day"})
    services.cash_sheet(site, MON)
    return site, manager, foreman, yao, afi


def _table(html: str) -> str:
    return html.split("<tbody>", 1)[1].split("</tbody>", 1)[0] if "<tbody>" in html else ""


def sheet_url(site, day=MON):
    return reverse("payroll:cash_sheet", args=[site.pk, day.isoformat()])


# --- pay periods --------------------------------------------------------------------------


def test_periods_page_lists_scoped_periods(client, paid_day):
    site, manager, _foreman, _yao, _afi = paid_day
    other, _om, _of = setup_site(name="Route 4 Bridge")
    roll_call(other, MON, {hire(other): "present"})
    services.cash_sheet(other, MON)
    client.force_login(manager)

    response = client.get(reverse("payroll:periods"))

    html = response.content.decode()
    assert response.status_code == 200
    assert "Market Hall Renovation" in html
    assert "Route 4 Bridge" not in html
    assert "Ready to pay" in html
    assert sheet_url(site) in html
    assert "7 750" in html  # net of the period


@pytest.mark.parametrize(("tab", "shown"), [("open", True), ("locked", False), ("all", True)])
def test_periods_tabs(client, paid_day, tab, shown):
    client.force_login(paid_day[1])

    html = client.get(reverse("payroll:periods"), {"status": tab}).content.decode()

    assert ("Market Hall Renovation" in _table(html)) is shown


def test_periods_site_filter(client, paid_day):
    admin = UserFactory(role="admin", is_superuser=True)
    other, _om, _of = setup_site(name="Route 4 Bridge")
    roll_call(other, MON, {hire(other): "present"})
    services.cash_sheet(other, MON)
    client.force_login(admin)

    html = client.get(reverse("payroll:periods"), {"site": other.pk}).content.decode()

    table = _table(html)
    assert "Route 4 Bridge" in table
    assert "Market Hall Renovation" not in table


# --- cash sheets --------------------------------------------------------------------------


def test_cash_sheets_list(client, paid_day):
    site, manager, *_ = paid_day
    client.force_login(manager)

    html = client.get(reverse("payroll:cash_sheets")).content.decode()

    assert sheet_url(site) in html
    assert "5 750 XOF" in html


def test_cash_sheet_page(client, paid_day):
    site, manager, *_ = paid_day
    client.force_login(manager)

    response = client.get(sheet_url(site))

    html = response.content.decode()
    assert response.status_code == 200
    assert 'href="/cash-sheets/" aria-current="page"' in html
    for text in ("Cash issued", "Total to pay", "Paid so far", "Left to pay"):
        assert text in html
    assert "Cash should be in hand" in html
    assert "Yao Mensah" in html and "Afi Dosseh" in html
    assert "Mark paid · 4 000 XOF" in html
    assert "− 2 000" in html
    assert "½ day" in html
    assert reverse("payroll:payslip", args=[line_of(site, MON, paid_day[3]).pk]) in html


def test_cash_sheet_outside_scope_is_403(client, paid_day):
    site = paid_day[0]
    _other, other_manager, _of = setup_site()
    client.force_login(other_manager)

    assert client.get(sheet_url(site)).status_code == 403


def test_mark_paid_with_htmx_returns_the_row_and_the_strip(client, paid_day):
    site, manager, _foreman, yao, _afi = paid_day
    client.force_login(manager)
    line = line_of(site, MON, yao)
    key = uuid.uuid4()
    data = {"client_uuid": str(key), "proof_type": "photo", "proof_file": photo()}

    response = client.post(
        reverse("payroll:cash_sheet_pay", args=[line.pk]), data, HTTP_HX_REQUEST="true"
    )

    html = response.content.decode()
    assert response.status_code == 200
    assert f'id="line-{line.pk}"' in html
    assert "Paid " in html
    assert 'id="reconciliation" hx-swap-oob="true"' in html
    assert "4 000 XOF" in html
    payment = Payment.objects.get()
    assert (payment.client_uuid, payment.amount, payment.paid_by) == (key, 4000, manager)

    # A double click (same client_uuid) does not pay twice.
    data["proof_file"] = photo()
    client.post(reverse("payroll:cash_sheet_pay", args=[line.pk]), data, HTTP_HX_REQUEST="true")
    assert Payment.objects.count() == 1


def test_mark_paid_errors_stay_on_the_row(client, paid_day):
    site, manager, _foreman, yao, _afi = paid_day
    client.force_login(manager)
    line = line_of(site, MON, yao)

    response = client.post(
        reverse("payroll:cash_sheet_pay", args=[line.pk]),
        {"client_uuid": str(uuid.uuid4()), "proof_type": "photo"},
        HTTP_HX_REQUEST="true",
    )

    assert response.status_code == 200
    assert "Attach the photo" in response.content.decode()
    assert not Payment.objects.exists()


def test_mark_paid_without_htmx_redirects(client, paid_day):
    site, manager, _foreman, yao, _afi = paid_day
    client.force_login(manager)

    response = client.post(
        reverse("payroll:cash_sheet_pay", args=[line_of(site, MON, yao).pk]),
        {"client_uuid": str(uuid.uuid4()), "proof_type": "signature"},
    )

    assert response.status_code == 302
    assert response.url == sheet_url(site)
    assert Payment.objects.count() == 1


def test_mark_paid_with_a_boosted_form_redirects(client, paid_day):
    # hx-boost sends HX-Request too: a boosted form post is answered like a browser's.
    site, manager, _foreman, yao, _afi = paid_day
    client.force_login(manager)

    response = client.post(
        reverse("payroll:cash_sheet_pay", args=[line_of(site, MON, yao).pk]),
        {"client_uuid": str(uuid.uuid4()), "proof_type": "signature"},
        HTTP_HX_REQUEST="true",
        HTTP_HX_BOOSTED="true",
    )

    assert response.status_code == 302
    assert response.url == sheet_url(site)
    assert Payment.objects.count() == 1


def test_mark_paid_outside_scope_is_403(client, paid_day):
    site, _manager, _foreman, yao, _afi = paid_day
    _other, other_manager, _of = setup_site()
    client.force_login(other_manager)

    response = client.post(
        reverse("payroll:cash_sheet_pay", args=[line_of(site, MON, yao).pk]),
        {"client_uuid": str(uuid.uuid4()), "proof_type": "signature"},
    )

    assert response.status_code == 403
    assert not Payment.objects.exists()


def test_undo_payment(client, paid_day):
    site, manager, foreman, yao, _afi = paid_day
    line = line_of(site, MON, yao)
    payment = services.record_payment(foreman, line, amount=line.net, proof_type="signature")
    client.force_login(manager)

    response = client.post(
        reverse("payroll:payment_undo", args=[payment.pk]), {"reason": "Wrong"}, HTTP_HX_REQUEST="1"
    )

    assert response.status_code == 200
    assert "Mark paid · 4 000 XOF" in response.content.decode()
    assert not Payment.objects.exists()


def test_cash_issue_form(client, paid_day):
    site, manager, *_ = paid_day
    client.force_login(manager)

    response = client.post(
        reverse("payroll:cash_issue", args=[site.pk, MON.isoformat()]),
        {"amount": "20 000", "note": "Office safe"},
    )

    assert response.status_code == 302
    html = client.get(sheet_url(site)).content.decode()
    assert "20 000 XOF" in html
    assert "Return 14 250 XOF when done" in html


def test_close_and_lock_sheet(client, paid_day):
    site, manager, *_ = paid_day
    client.force_login(manager)
    url = reverse("payroll:cash_sheet_lock", args=[site.pk, MON.isoformat()])

    client.post(url, {"reason": ""})
    assert not PayPeriod.objects.get().is_locked  # unpaid workers need a reason

    client.post(url, {"reason": "Both workers left early"})
    assert PayPeriod.objects.get().is_locked
    html = client.get(sheet_url(site)).content.decode()
    assert "Closed · locked" in html
    assert "Mark paid" not in html


def test_preview_banner_before_approval(client, company):
    site, manager, _foreman = setup_site()
    roll_call(site, TUE, {hire(site): "present"}, status=SUBMITTED)
    client.force_login(manager)

    html = client.get(sheet_url(site, TUE)).content.decode()

    assert "Preview." in html
    assert "Waiting approval" in html
    assert "Mark paid" not in html


# --- advances -----------------------------------------------------------------------------


def test_advances_page(client, paid_day):
    _site, manager, *_ = paid_day
    client.force_login(manager)

    html = client.get(reverse("payroll:advances")).content.decode()

    assert "1 worker owes 8 000 XOF in total" in html  # 10 000 − today's 2 000 deduction
    assert "Yao Mensah" in html
    assert "Give an advance" in html


def test_give_an_advance(client, paid_day):
    _site, manager, _foreman, _yao, afi = paid_day
    client.force_login(manager)

    response = client.post(
        reverse("payroll:advances"),
        {
            "worker": afi.pk,
            "amount": "5 000",
            "deduction_per_pay": "1 000",
            "reason": "Rent",
            "proof_type": "signature",
        },
    )

    assert response.status_code == 302
    advance = Advance.objects.get(worker=afi)
    assert (advance.amount, advance.deduction_per_pay, advance.balance) == (5000, 1000, 5000)


def test_advance_over_the_limit_shows_the_error(client, paid_day):
    _site, manager, _foreman, _yao, afi = paid_day
    client.force_login(manager)

    response = client.post(
        reverse("payroll:advances"),
        {
            "worker": afi.pk,
            "amount": "20 000",
            "deduction_per_pay": "1 000",
            "proof_type": "signature",
        },
    )

    html = response.content.decode()
    assert response.status_code == 200
    assert "Over the limit" in html
    assert not Advance.objects.filter(worker=afi).exists()


def test_advance_form_only_offers_scoped_workers(client, paid_day):
    _site, manager, *_ = paid_day
    stranger = WorkerFactory(first_name="Stranger")
    client.force_login(manager)

    html = client.get(reverse("payroll:advances")).content.decode()
    response = client.post(
        reverse("payroll:advances"),
        {
            "worker": stranger.pk,
            "amount": "1 000",
            "deduction_per_pay": "500",
            "proof_type": "signature",
        },
    )

    assert "Stranger" not in html
    assert response.status_code == 200
    assert not Advance.objects.filter(worker=stranger).exists()


def test_advances_never_name_a_site_the_manager_does_not_see(client, paid_day):
    """A worker joining the manager's site next week is on another site today: their
    advance is listed (they are on the manager's crew) without naming that site."""
    site, manager, *_ = paid_day
    other, _manager, _foreman = setup_site(name="Bravo Tower")
    kofi = hire(other, 5000, first_name="Kofi", last_name="Agbo")
    next_week = dates.company_today() + datetime.timedelta(days=7)
    SiteAssignmentFactory(worker=kofi, site=site, start_date=next_week)
    AdvanceFactory(worker=kofi, amount=3000, balance=3000, deduction_per_pay=1000)
    client.force_login(manager)

    html = client.get(reverse("payroll:advances")).content.decode()

    assert "Kofi Agbo" in html
    assert "Bravo Tower" not in html


def test_advance_preview_explains_the_limit(client, paid_day):
    _site, manager, _foreman, yao, _afi = paid_day
    client.force_login(manager)

    html = client.get(
        reverse("payroll:advance_preview"),
        {"worker": yao.pk, "amount": "6 000", "deduction_per_pay": "2 000"},
    ).content.decode()

    # Owes 8 000; + 6 000 = 14 000 of 36 000 (6 000 × 6 days) = 39 %; 4 + 3 pays.
    assert "already owes <strong>8 000</strong>" in html
    assert "New total <strong>14 000</strong> = 39%" in html
    assert "limit 50%" in html
    assert "<strong>7 pays</strong>" in html


# --- payslip ------------------------------------------------------------------------------


def _pdf_text(data: bytes) -> str:
    streams = re.findall(rb"stream\n(.*?)\nendstream", data, re.S)
    return b"".join(zlib.decompress(s) for s in streams).decode("latin-1")


def test_payslip_pdf_for_a_pay_line(client, paid_day):
    site, manager, foreman, yao, _afi = paid_day
    line = line_of(site, MON, yao)
    services.record_payment(foreman, line, amount=line.net, proof_type="thumb_mark")
    client.force_login(manager)

    response = client.get(reverse("payroll:payslip", args=[line.pk]))

    assert response.status_code == 200
    assert response["Content-Type"] == "application/pdf"
    assert f"payslip-{yao.number}-2025-09-01.pdf" in response["Content-Disposition"]
    data = response.content
    assert data.startswith(b"%PDF-1.4") and data.rstrip().endswith(b"%%EOF")
    text = _pdf_text(data)
    for expected in (
        "Pay statement",
        "Yao Mensah",
        yao.number,
        "Market Hall Renovation",
        "6 000 XOF / day",
        "Mon 1 Sep",
        "6 000 XOF",
        "– 2 000 XOF".encode("cp1252").decode("latin-1"),
        "4 000 XOF",
        "Advance still owed",
        "8 000 XOF",
        "Thumb mark",
        "Worker signature or thumb mark",
    ):
        assert expected in text, expected


def test_payslip_for_a_weekly_hourly_line(client, company):
    site, manager, _foreman = setup_site(pay_frequency="weekly")
    kafui = hire(site, 900, pay_type="hourly", first_name="Kafui", last_name="Amedome")
    for offset in range(6):
        roll_call(
            site,
            MON + datetime.timedelta(days=offset),
            {kafui: ("present", datetime.time(7, 0), datetime.time(16, 0))},
        )
    services.cash_sheet(site, SAT)
    client.force_login(manager)

    response = client.get(reverse("payroll:payslip", args=[line_of(site, SAT, kafui).pk]))

    text = _pdf_text(response.content)
    assert "900 XOF / hour" in text
    assert "Sat 6 Sep" in text
    assert "43 200 XOF" in text  # 6 × 8 h × 900
    assert "Not paid yet." in text


def test_payslip_outside_scope_is_403(client, paid_day):
    site, _m, _f, yao, _afi = paid_day
    _other, other_manager, _of = setup_site()
    client.force_login(other_manager)

    assert (
        client.get(reverse("payroll:payslip", args=[line_of(site, MON, yao).pk])).status_code == 403
    )
    assert client.get(reverse("payroll:payslip", args=[999999])).status_code == 404


def test_pdf_cross_reference_table_points_at_every_object():
    doc = pdf.Document(title="Test")
    doc.text(48, 64, "Hello (world) \\ é ½ – ŵ ɔ", bold=True)
    doc.add_page()
    doc.rect(10, 10, 100, 20, fill="#F8F9FA", stroke="#E3E5E9")
    doc.line(0, 0, 10, 10)

    data = doc.to_bytes(compress=False)

    xref = int(data.rsplit(b"startxref\n", 1)[1].split(b"\n")[0])
    assert data[xref:].startswith(b"xref\n")
    offsets = re.findall(rb"(\d{10}) 00000 n", data[xref:])
    for number, offset in enumerate(offsets, start=1):
        assert data[int(offset) :].startswith(f"{number} 0 obj".encode())
    assert b"/Count 2" in data
    assert b"(Hello \\(world\\) \\\\ \xe9 \xbd \x96 w ?) Tj" in data


def test_pdf_text_width_and_alignment():
    assert pdf.text_width("0000", 10) == pytest.approx(22.24)
    assert pdf.text_width("W", 10, bold=True) == pytest.approx(9.44)
    assert pdf.text_width("é", 10) == pdf.text_width("e", 10)
    doc = pdf.Document()
    width = doc.text(100, 100, "100", align="right")
    assert f"{100 - width:.2f}".rstrip("0").rstrip(".") in doc.pages[0][0]


# --- labels -------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("days", "units", "hours", "expected"),
    [
        ([{"status": "present", "units": "1.00", "pay_type": "daily"}], "1.00", None, "1 day"),
        ([{"status": "half_day", "units": "0.50", "pay_type": "daily"}], "0.50", None, "½ day"),
        ([{"status": "half_day", "units": "0.60", "pay_type": "daily"}], "0.60", None, "0.6 day"),
        ([{"status": "late", "units": "1.00", "pay_type": "daily"}], "1.00", None, "1 day · late"),
        (
            [{"status": "present", "pay_type": "hourly", "hours": "8.00", "rate": 900}],
            "1.00",
            "8.00",
            "8 h × 900",
        ),
        ([{"pay_type": "daily"}] * 6, "5.50", None, "5.5 days"),
        ([{"pay_type": "hourly"}] * 2, "2.00", "16.50", "16.5 h"),
    ],
)
def test_worked_label(days, units, hours, expected):
    line = PayLine(days=days, units=units, hours=hours)

    assert labels.worked_label(line) == expected
