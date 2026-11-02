"""Phone API: GET /sites/{id}/payouts/{date}/ and POST /payments/ (to the contract)."""

import datetime
import uuid

import pytest
from django.urls import reverse
from rest_framework.test import APIClient

from apps.core.choices import PaymentMethod
from apps.payroll.api.serializers import PaymentSerializer, PayoutSerializer
from apps.payroll.models import PayLine, Payment, PayPeriod
from tests.factories import AdvanceFactory, CashIssueFactory, CompanySettingsFactory, UserFactory
from tests.test_payroll_services import MON, SUBMITTED, hire, photo, roll_call, setup_site

DAY = MON.isoformat()
EARLIER = MON - datetime.timedelta(days=3)


@pytest.fixture
def company(db, settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path
    return CompanySettingsFactory()


@pytest.fixture
def site_setup(company):
    site, manager, foreman = setup_site()
    yao, afi = hire(site, 6000, first_name="Yao"), hire(site, 3500, first_name="Afi")
    AdvanceFactory(
        worker=yao, amount=10000, balance=10000, deduction_per_pay=2000, given_on=EARLIER
    )
    return site, manager, foreman, yao, afi


def client_for(user):
    client = APIClient()
    client.force_authenticate(user)
    return client


def payout_url(site, day=DAY):
    return reverse("api:payroll:payout", kwargs={"site_id": site.pk, "date": day})


def payments_url():
    return reverse("api:payroll:payments")


def body(line, **extra):
    data = {
        "client_uuid": str(uuid.uuid4()),
        "pay_line_id": line.pk,
        "amount": line.net,
        "proof_type": "signature",
        "paid_at": "2025-09-01T16:31:00Z",
    }
    data.update(extra)
    return data


def open_sheet(site_setup, day=MON):
    site, _manager, foreman, yao, afi = site_setup
    roll_call(site, day, {yao: "present", afi: "half_day"})
    response = client_for(foreman).get(payout_url(site, day.isoformat()))
    assert response.status_code == 200
    return response.json()


# --- payouts ------------------------------------------------------------------------------


def test_payout_follows_the_contract(site_setup):
    site, manager, foreman, yao, afi = site_setup
    roll_call(site, MON, {yao: "present", afi: "half_day"})
    CashIssueFactory(site=site, date=MON, amount=20000, issued_by=manager, received_by=foreman)

    response = client_for(foreman).get(payout_url(site))

    assert response.status_code == 200
    data = response.json()
    assert PayoutSerializer(data=data).is_valid(), PayoutSerializer(data=data).errors
    assert data["status"] == "open"
    assert data["day_status"] == "approved"
    assert data["currency"] == {"code": "XOF", "decimals": 0}
    lines = {line["worker"]["id"]: line for line in data["lines"]}
    assert lines[yao.pk]["gross"] == 6000
    assert lines[yao.pk]["advance_deduction"] == 2000
    assert lines[yao.pk]["net"] == 4000
    assert lines[yao.pk]["worked"] == "1 day"
    assert lines[yao.pk]["units"] == "1.00"
    assert lines[afi.pk]["worked"] == "½ day"
    assert lines[afi.pk]["units"] == "0.50"
    assert lines[afi.pk]["payment"] is None
    # Invariants from the contract.
    assert data["total_to_pay"] == sum(line["net"] for line in data["lines"]) == 5750
    assert data["left_to_pay"] == data["total_to_pay"] - data["paid_total"]
    assert data["cash_in_hand"] == data["cash_issued"] - data["paid_total"] == 20000
    assert (data["line_count"], data["paid_count"]) == (2, 0)


def test_a_worker_paid_by_transfer_takes_no_cash(site_setup):
    """Contract 1.6: Kofi is paid by mobile money. He is on the sheet and in the totals,
    not in the cash figures; the foreman paying him anyway (a failed transfer) pays cash."""
    site, manager, foreman, yao, afi = site_setup
    kofi = hire(site, 4000, first_name="Kofi", payment_method=PaymentMethod.MOBILE_MONEY)
    roll_call(site, MON, {yao: "present", afi: "half_day", kofi: "present"})
    CashIssueFactory(site=site, date=MON, amount=8000, issued_by=manager, received_by=foreman)
    phone = client_for(foreman)

    before = phone.get(payout_url(site)).json()
    line = PayLine.objects.get(worker=kofi, period__end_date=MON)
    paid = phone.post(payments_url(), body(line), format="json")
    after = phone.get(payout_url(site)).json()

    assert (before["total_to_pay"], before["cash_to_pay"]) == (9750, 5750)
    assert before["cash_in_hand"] == 8000
    assert paid.status_code == 201 and paid.json()["method"] == "cash"
    lines = {row["worker"]["id"]: row for row in after["lines"]}
    assert lines[kofi.pk]["payment"]["method"] == "cash"
    assert (after["cash_to_pay"], after["cash_in_hand"]) == (9750, 4000)
    assert PayoutSerializer(data=after).is_valid(), PayoutSerializer(data=after).errors


def test_payout_before_approval_is_a_preview(site_setup):
    site, _manager, foreman, yao, _afi = site_setup
    roll_call(site, MON, {yao: "late"}, status=SUBMITTED)

    data = client_for(foreman).get(payout_url(site)).json()

    assert data["status"] == "not_open"
    assert data["day_status"] == "submitted"
    assert data["approved_at"] is None
    assert data["lines"][0]["worked"] == "1 day · late"


def test_payout_of_a_day_without_roll_call(site_setup):
    site, _manager, foreman, _yao, _afi = site_setup

    data = client_for(foreman).get(payout_url(site)).json()

    assert (data["status"], data["day_status"], data["lines"]) == ("not_open", "not_started", [])


def test_payout_outside_scope_is_403(site_setup):
    site, _manager, _foreman, yao, _afi = site_setup
    roll_call(site, MON, {yao: "present"})
    _other, other_manager, other_foreman = setup_site()

    for user in (other_foreman, other_manager):
        response = client_for(user).get(payout_url(site))
        assert response.status_code == 403
        assert response.json()["code"] == "permission_denied"
    assert client_for(other_foreman).get(payout_url(site.__class__(pk=99999))).status_code == 403


@pytest.mark.parametrize("name", ["payout", "payments"])
def test_payroll_api_requires_authentication(company, name):
    if name == "payout":
        response = APIClient().get(
            reverse("api:payroll:payout", kwargs={"site_id": 1, "date": DAY})
        )
    else:
        response = APIClient().post(payments_url(), {}, format="json")

    assert response.status_code == 401
    assert response.json()["code"] == "not_authenticated"


# --- payments -----------------------------------------------------------------------------


def test_payment_created_then_replayed(site_setup):
    data = open_sheet(site_setup)
    foreman = site_setup[2]
    line = PayLine.objects.get(pk=data["lines"][0]["pay_line_id"])
    request = body(line)
    client = client_for(foreman)

    first = client.post(payments_url(), request, format="json")
    second = client.post(payments_url(), request, format="json")

    assert first.status_code == 201
    assert "Idempotent-Replayed" not in first
    assert second.status_code == 200
    assert second["Idempotent-Replayed"] == "true"
    assert first.json() == second.json()
    assert Payment.objects.count() == 1
    payment = first.json()
    assert PaymentSerializer(data=payment).is_valid()
    assert payment["amount"] == line.net
    assert payment["paid_by"] == {"id": foreman.pk, "name": foreman.get_full_name()}
    assert payment["paid_at"] == "2025-09-01T16:31:00Z"
    assert payment["proof_url"] is None or payment["proof_url"].startswith("http")


def test_payment_with_a_proof_file(site_setup):
    data = open_sheet(site_setup)
    line = PayLine.objects.get(pk=data["lines"][0]["pay_line_id"])
    request = body(line, proof_file=photo())
    del request["paid_at"]

    response = client_for(site_setup[2]).post(payments_url(), request, format="multipart")

    assert response.status_code == 201
    payment_id = response.json()["id"]
    assert response.json()["proof_url"] == f"http://testserver/api/v1/files/proofs/{payment_id}/"
    sheet = client_for(site_setup[2]).get(payout_url(site_setup[0])).json()
    paid = next(x for x in sheet["lines"] if x["pay_line_id"] == line.pk)
    assert paid["payment"]["client_uuid"] == request["client_uuid"]
    assert sheet["paid_total"] == line.net
    assert sheet["paid_count"] == 1


def test_replay_ignores_a_resent_file(site_setup):
    data = open_sheet(site_setup)
    line = PayLine.objects.get(pk=data["lines"][0]["pay_line_id"])
    request = body(line, proof_file=photo())
    client = client_for(site_setup[2])
    first = client.post(payments_url(), request, format="multipart")

    request["proof_file"] = photo("again.jpg")
    second = client.post(payments_url(), request, format="multipart")

    assert (first.status_code, second.status_code) == (201, 200)
    assert second.json()["proof_url"] == first.json()["proof_url"]


def test_same_client_uuid_for_another_line_is_409(site_setup):
    data = open_sheet(site_setup)
    lines = [PayLine.objects.get(pk=x["pay_line_id"]) for x in data["lines"]]
    client = client_for(site_setup[2])
    key = str(uuid.uuid4())
    client.post(payments_url(), body(lines[0], client_uuid=key), format="json")

    response = client.post(payments_url(), body(lines[1], client_uuid=key), format="json")

    assert response.status_code == 409
    assert response.json()["details"]["client_uuid"] == key


def test_paying_a_paid_line_under_another_uuid_is_409(site_setup):
    data = open_sheet(site_setup)
    line = PayLine.objects.get(pk=data["lines"][0]["pay_line_id"])
    client = client_for(site_setup[2])
    client.post(payments_url(), body(line), format="json")

    response = client.post(payments_url(), body(line), format="json")

    assert response.status_code == 409
    assert response.json()["code"] == "conflict"


@pytest.mark.parametrize("delta", [-1, 1, -4000])
def test_amount_other_than_the_net_left_is_400(site_setup, delta):
    data = open_sheet(site_setup)
    line = PayLine.objects.get(pk=data["lines"][0]["pay_line_id"])

    response = client_for(site_setup[2]).post(
        payments_url(), body(line, amount=line.net + delta), format="json"
    )

    assert response.status_code == 400
    assert response.json()["code"] == "validation_error"
    assert "amount" in response.json()["details"]
    assert not Payment.objects.exists()


def test_payment_before_approval_is_409(site_setup):
    site, _manager, foreman, yao, _afi = site_setup
    roll_call(site, MON, {yao: "present"}, status=SUBMITTED)
    data = client_for(foreman).get(payout_url(site)).json()
    line = PayLine.objects.get(pk=data["lines"][0]["pay_line_id"])

    response = client_for(foreman).post(payments_url(), body(line), format="json")

    assert response.status_code == 409
    assert response.json()["details"] == {"status": "not_open"}


def test_payment_on_a_locked_period_is_423(site_setup):
    data = open_sheet(site_setup)
    line = PayLine.objects.get(pk=data["lines"][0]["pay_line_id"])
    PayPeriod.objects.update(status=PayPeriod.Status.LOCKED)

    response = client_for(site_setup[2]).post(payments_url(), body(line), format="json")

    assert response.status_code == 423
    assert response.json()["code"] == "locked"


def test_payment_proof_rules_are_400(site_setup):
    data = open_sheet(site_setup)
    line = PayLine.objects.get(pk=data["lines"][0]["pay_line_id"])
    client = client_for(site_setup[2])

    no_proof = client.post(payments_url(), body(line, proof_type="none"), format="json")
    big = client.post(
        payments_url(),
        body(line, proof_file=photo(size=5 * 1024 * 1024 + 1)),
        format="multipart",
    )

    assert no_proof.status_code == 400
    assert "proof_type" in no_proof.json()["details"]
    assert big.status_code == 400
    assert "proof_file" in big.json()["details"]


def test_payment_outside_scope_is_403_and_unknown_line_404(site_setup):
    data = open_sheet(site_setup)
    line = PayLine.objects.get(pk=data["lines"][0]["pay_line_id"])
    _other, other_manager, other_foreman = setup_site()

    for user in (other_foreman, other_manager, UserFactory(role="accountant")):
        response = client_for(user).post(payments_url(), body(line), format="json")
        assert response.status_code == 403
    missing = client_for(site_setup[2]).post(
        payments_url(), body(line, pay_line_id=999999), format="json"
    )
    assert missing.status_code == 404
    assert not Payment.objects.exists()


def test_bad_body_is_400(site_setup):
    response = client_for(site_setup[2]).post(payments_url(), {"amount": 1.5}, format="json")

    assert response.status_code == 400
    assert {"client_uuid", "pay_line_id", "proof_type"} <= set(response.json()["details"])
