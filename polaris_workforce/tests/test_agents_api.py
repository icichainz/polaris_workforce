"""AI assistant access: agent tokens, the office API and preview-then-confirm decisions.

What must hold:
- A token acts as its user (company, role, sites) and only opens ``/api/v1/office/``.
  It is refused everywhere else, so an assistant can never record a payment.
- Revoked, expired, disabled-user and foreman tokens get ``401``.
- Reads are scoped like the office screens and leave out personal data.
- A decision takes a preview and a confirmation; the confirmation is bound to the token,
  the decision, the note and the day as previewed. Replays answer the current state.
"""

import datetime
from unittest import mock

import pytest
from django.core.cache import cache
from django.core.exceptions import PermissionDenied
from django.urls import reverse
from rest_framework.exceptions import ValidationError
from rest_framework.test import APIClient

from apps.agents import services
from apps.agents.authentication import AgentTokenThrottle
from apps.agents.models import AgentToken
from apps.attendance.models import AttendanceDay
from apps.core.models import AuditLog
from apps.core.tenancy import use_company
from tests.factories import (
    AdminUserFactory,
    CompanyFactory,
    CompanySettingsFactory,
    ForemanDeviceFactory,
    PlatformAdminFactory,
    UserFactory,
    WorkerFactory,
)
from tests.test_payroll_services import MON, SUBMITTED, TUE, hire, roll_call, setup_site

pytestmark = pytest.mark.django_db

OFFICE = "/api/v1/office"


@pytest.fixture(autouse=True)
def _fixed_today():
    with mock.patch("apps.core.dates.company_today", return_value=TUE):
        yield


@pytest.fixture(autouse=True)
def _fresh_throttle():
    cache.clear()
    yield
    cache.clear()


@pytest.fixture
def crew(company):
    site, manager, foreman = setup_site(name="Alpha Court")
    yao = hire(site, 6000, first_name="Yao", last_name="Mensah", phone="+228 90 11 22 33")
    afi = hire(site, 4000, first_name="Afi", last_name="Dosseh")
    roll_call(site, MON, {yao: "present", afi: "half_day"})
    pending = roll_call(site, TUE, {yao: "present", afi: "absent"}, SUBMITTED)
    return {"site": site, "manager": manager, "foreman": foreman, "yao": yao, "pending": pending}


@pytest.fixture
def company(db):
    return CompanySettingsFactory()


def accountant():
    return UserFactory(role="accountant")


def agent(user, *, can_approve=False):
    """An API client holding a fresh agent token of ``user``."""
    token, raw = services.create_token(user, name="Claude", can_approve=can_approve)
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {raw}")
    client.token = token
    return client


def preview(client, day, decision="approve", note=""):
    return client.post(
        f"{OFFICE}/days/{day.pk}/decision-preview/", {"decision": decision, "note": note}
    )


def confirm(client, confirmation):
    return client.post(f"{OFFICE}/decisions/", {"confirmation": confirmation})


# --- tokens ------------------------------------------------------------------------------


def test_a_token_is_shown_once_and_stored_hashed(crew):
    token, raw = services.create_token(crew["manager"], name="Laptop")

    assert raw.startswith("pol_") and len(raw) > 40
    assert token.prefix == raw[:10]
    assert token.token_hash == AgentToken.hash_token(raw) and raw not in token.token_hash
    assert token.expires_at - token.created_at > datetime.timedelta(days=89)
    entry = AuditLog.objects.get(action="agents.token.create")
    assert entry.actor == crew["manager"] and raw not in str(entry.after)


def test_only_office_roles_of_a_company_hold_tokens(crew):
    with pytest.raises(PermissionDenied):
        services.create_token(crew["foreman"], name="x")
    services.create_token(accountant(), name="books")  # read only is fine


def test_an_accountant_cannot_make_an_approving_token(crew):
    with pytest.raises(ValidationError):
        services.create_token(accountant(), name="x", can_approve=True)


@pytest.mark.parametrize(
    "spoil",
    [
        lambda t: AgentToken.objects.filter(pk=t.pk).update(revoked_at=t.created_at),
        lambda t: AgentToken.objects.filter(pk=t.pk).update(expires_at=t.created_at),
        lambda t: type(t.user).objects.filter(pk=t.user_id).update(is_active=False),
        lambda t: type(t.user).objects.filter(pk=t.user_id).update(role="foreman"),
    ],
    ids=["revoked", "expired", "disabled user", "now a foreman"],
)
def test_spoiled_tokens_get_401(crew, spoil):
    client = agent(crew["manager"])
    assert client.get(f"{OFFICE}/me/").status_code == 200

    spoil(client.token)

    response = client.get(f"{OFFICE}/me/")
    assert response.status_code == 401
    assert response.json()["code"] == "not_authenticated"


def test_an_agent_token_opens_nothing_outside_the_office_api(crew):
    client = agent(crew["manager"], can_approve=True)
    site, day = crew["site"], TUE.isoformat()
    calls = [
        ("get", "/api/v1/me/", {}),
        ("get", f"/api/v1/sites/{site.pk}/payouts/{MON.isoformat()}/", {}),
        ("post", "/api/v1/payments/", {"pay_line_id": 1, "amount": 100}),
        ("put", f"/api/v1/sites/{site.pk}/days/{day}/records/", {"records": []}),
        ("post", f"/api/v1/sites/{site.pk}/days/{day}/submit/", {}),
        ("post", "/api/v1/checkins/", {}),
    ]
    for method, url, data in calls:
        response = getattr(client, method)(url, data)
        assert response.status_code == 401, (method, url, response.status_code)


def test_the_office_api_refuses_sessions_and_phone_tokens(crew):
    session = APIClient()
    session.force_login(crew["manager"])
    assert session.get(f"{OFFICE}/me/").status_code == 401

    device = ForemanDeviceFactory(user=crew["foreman"])
    phone = APIClient()
    phone.force_authenticate(crew["foreman"], token=device)
    assert phone.get(f"{OFFICE}/me/").status_code in (401, 403)


def test_the_office_api_is_throttled_per_token(crew, monkeypatch):
    monkeypatch.setattr(AgentTokenThrottle, "THROTTLE_RATES", {"agent": "2/minute"})
    client = agent(crew["manager"])
    assert [client.get(f"{OFFICE}/me/").status_code for _ in range(3)] == [200, 200, 429]
    assert agent(crew["manager"]).get(f"{OFFICE}/me/").status_code == 200


# --- reads -------------------------------------------------------------------------------


def test_me_says_who_the_token_acts_for(crew):
    data = agent(crew["manager"], can_approve=True).get(f"{OFFICE}/me/").json()

    assert data["user"]["role"] == "site_manager"
    assert data["company"]["currency"] == {"code": "XOF", "decimals": 0}
    assert data["company"]["today"] == TUE.isoformat()
    assert data["token"]["can_approve"] is True
    assert [s["name"] for s in data["sites"]] == ["Alpha Court"]


def test_approvals_list_the_submitted_day(crew):
    data = agent(crew["manager"]).get(f"{OFFICE}/approvals/").json()

    [day] = data["days"]
    assert day["day_id"] == crew["pending"].pk
    assert day["date"] == TUE.isoformat()
    assert day["counts"]["present"] == 1 and day["counts"]["absent"] == 1
    assert day["counts"]["expected"] == 2 and day["counts"]["unmarked"] == 0


def test_a_day_shows_every_mark_and_no_phone_numbers(crew):
    client = agent(crew["manager"])
    data = client.get(f"{OFFICE}/sites/{crew['site'].pk}/days/{TUE.isoformat()}/").json()

    assert data["status"] == "submitted" and data["may_decide"] is True
    assert {r["worker"]["name"]: r["status"] for r in data["records"]} == {
        "Afi Dosseh": "absent",
        "Yao Mensah": "present",
    }
    assert "90 11 22 33" not in str(data)

    empty = client.get(f"{OFFICE}/sites/{crew['site'].pk}/days/2025-09-05/").json()
    assert empty["status"] == "not_started" and empty["day_id"] is None


def test_the_cash_sheet_gives_minor_units_and_display(crew):
    client = agent(crew["manager"])
    data = client.get(f"{OFFICE}/sites/{crew['site'].pk}/cash-sheets/{MON.isoformat()}/").json()

    nets = {line["worker"]["name"]: line["net"] for line in data["lines"]}
    assert nets["Yao Mensah"] == {"amount": 6000, "display": "6 000 XOF"}
    assert nets["Afi Dosseh"]["amount"] == 2000
    assert data["total_to_pay"]["amount"] == 8000


def test_workers_are_found_by_name_without_personal_data(crew):
    client = agent(crew["manager"])
    data = client.get(f"{OFFICE}/workers/", {"q": "mens"}).json()

    [yao] = data["workers"]
    assert yao["name"] == "Yao Mensah" and yao["site"]["name"] == "Alpha Court"
    assert set(yao) == {"id", "name", "number", "trade", "site"}

    detail = client.get(f"{OFFICE}/workers/{yao['id']}/", {"from": MON.isoformat()}).json()
    assert [(d["date"], d["status"]) for d in detail["days"]] == [
        (MON.isoformat(), "present"),
        (TUE.isoformat(), "present"),
    ]
    assert "phone" not in detail and "90 11 22 33" not in str(detail)


def test_today_overview(crew):
    data = agent(crew["manager"]).get(f"{OFFICE}/today/").json()

    assert data["waiting_for_approval"] == 1
    assert [row["site"]["name"] for row in data["sites"]] == ["Alpha Court"]


def test_reads_stay_on_the_users_sites(crew):
    other, _manager, _foreman = setup_site(name="Bravo Tower")
    stranger = hire(other, 5000, first_name="Zeb", last_name="Bravo")
    other_day = roll_call(other, TUE, {stranger: "present"}, SUBMITTED)
    client = agent(crew["manager"], can_approve=True)

    forbidden = [
        ("get", f"{OFFICE}/sites/{other.pk}/days/{TUE.isoformat()}/", {}),
        ("get", f"{OFFICE}/sites/{other.pk}/cash-sheets/{MON.isoformat()}/", {}),
        ("get", f"{OFFICE}/workers/{stranger.pk}/", {}),
        ("get", f"{OFFICE}/workers/", {"site": other.pk}),
        ("post", f"{OFFICE}/days/{other_day.pk}/decision-preview/", {"decision": "approve"}),
    ]
    for method, url, data in forbidden:
        assert getattr(client, method)(url, data).status_code == 403, url

    text = str(client.get(f"{OFFICE}/approvals/").json())
    text += str(client.get(f"{OFFICE}/workers/", {"q": "Zeb"}).json())
    text += str(client.get(f"{OFFICE}/today/").json())
    assert "Bravo" not in text
    # An admin sees both.
    assert "Bravo" in str(agent(AdminUserFactory()).get(f"{OFFICE}/approvals/").json())


# --- decisions ---------------------------------------------------------------------------


def test_a_read_only_token_cannot_preview(crew):
    response = preview(agent(crew["manager"]), crew["pending"])
    assert response.status_code == 403


def test_preview_changes_nothing_and_confirm_approves(crew):
    client = agent(crew["manager"], can_approve=True)
    day = crew["pending"]

    shown = preview(client, day)
    assert shown.status_code == 200, shown.content
    body = shown.json()
    assert body["decision"] == "approve" and body["day"]["status"] == "submitted"
    assert len(body["day"]["records"]) == 2
    day.refresh_from_db()
    assert day.status == SUBMITTED
    assert not AuditLog.objects.filter(action__startswith="attendance.day.").exists()

    done = confirm(client, body["confirmation"])

    assert done.status_code == 200, done.content
    assert done.json()["day"]["status"] == "approved"
    day.refresh_from_db()
    assert day.status == AttendanceDay.Status.APPROVED and day.approved_by == crew["manager"]
    assert AuditLog.objects.filter(action="attendance.day.approve", actor=crew["manager"]).exists()
    via = AuditLog.objects.get(action="agents.day.approve")
    assert via.after["agent_token"] == "Claude" and via.actor == crew["manager"]


def test_the_same_confirmation_twice_decides_once(crew):
    client = agent(crew["manager"], can_approve=True)
    confirmation = preview(client, crew["pending"]).json()["confirmation"]
    assert confirm(client, confirmation).status_code == 200

    again = confirm(client, confirmation)

    assert again.status_code == 200
    assert again["Idempotent-Replayed"] == "true"
    assert again.json()["day"]["status"] == "approved"
    assert AuditLog.objects.filter(action="attendance.day.approve").count() == 1


def test_a_change_after_the_preview_needs_a_new_preview(crew):
    client = agent(crew["manager"], can_approve=True)
    confirmation = preview(client, crew["pending"]).json()["confirmation"]
    record = crew["pending"].records.get(worker=crew["yao"])
    record.status = "late"
    record.save()

    response = confirm(client, confirmation)

    assert response.status_code == 409
    assert response.json()["code"] == "conflict"
    crew["pending"].refresh_from_db()
    assert crew["pending"].status == SUBMITTED


def test_an_expired_or_foreign_confirmation_is_refused(crew, monkeypatch):
    client = agent(crew["manager"], can_approve=True)
    confirmation = preview(client, crew["pending"]).json()["confirmation"]

    other = agent(crew["manager"], can_approve=True)
    assert confirm(other, confirmation).status_code == 400
    assert confirm(client, confirmation[:-3] + "abc").status_code == 400

    monkeypatch.setattr(services, "CONFIRMATION_MAX_AGE", datetime.timedelta(seconds=-1))
    response = confirm(client, confirmation)
    assert response.status_code == 400
    assert "expired" in response.json()["details"]["confirmation"][0]
    crew["pending"].refresh_from_db()
    assert crew["pending"].status == SUBMITTED


def test_reject_needs_a_note_and_sends_the_day_back(crew):
    client = agent(crew["manager"], can_approve=True)
    assert preview(client, crew["pending"], "reject").status_code == 400

    body = preview(client, crew["pending"], "reject", "Afi was on site, check the gate log").json()
    done = confirm(client, body["confirmation"])

    assert done.status_code == 200
    crew["pending"].refresh_from_db()
    assert crew["pending"].status == AttendanceDay.Status.REJECTED
    assert crew["pending"].rejection_note == "Afi was on site, check the gate log"
    assert AuditLog.objects.get(action="agents.day.reject").reason.startswith("Afi was")


def test_only_a_submitted_day_can_be_decided(crew):
    monday = AttendanceDay.objects.get(site=crew["site"], date=MON)
    response = preview(agent(crew["manager"], can_approve=True), monday)
    assert response.status_code == 409


def test_a_manager_who_lost_the_site_cannot_confirm(crew):
    client = agent(crew["manager"], can_approve=True)
    confirmation = preview(client, crew["pending"]).json()["confirmation"]
    crew["site"].manager = AdminUserFactory()
    crew["site"].save()

    assert confirm(client, confirmation).status_code == 403
    crew["pending"].refresh_from_db()
    assert crew["pending"].status == SUBMITTED


# --- the office screen -------------------------------------------------------------------


def test_the_screen_creates_a_token_and_shows_it_once(client, crew):
    client.force_login(crew["manager"])
    page = client.get(reverse("agents:access"))
    assert page.status_code == 200
    assert b"May approve" in page.content

    response = client.post(
        reverse("agents:create"), {"name": "Claude on my laptop", "lifetime_days": "30"}
    )

    assert response.status_code == 200
    token = AgentToken.objects.get(user=crew["manager"])
    raw = response.context["raw"]
    assert raw.encode() in response.content and token.token_hash == AgentToken.hash_token(raw)
    assert "no-store" in response["Cache-Control"]
    assert raw.encode() not in client.get(reverse("agents:access")).content


def test_accountants_get_no_approval_option(client, crew):
    client.force_login(accountant())
    page = client.get(reverse("agents:access"))
    assert page.status_code == 200 and b"May approve" not in page.content


def test_foremen_cannot_open_the_screen(client, crew):
    client.force_login(crew["foreman"])
    assert client.get(reverse("agents:access")).status_code == 403


def test_revoking_from_the_screen(client, crew):
    mine, _raw = services.create_token(crew["manager"], name="mine")
    admin = AdminUserFactory()
    theirs, _raw = services.create_token(admin, name="theirs")
    client.force_login(crew["manager"])

    assert client.post(reverse("agents:revoke", args=[theirs.pk])).status_code == 404
    assert client.post(reverse("agents:revoke", args=[mine.pk])).status_code == 302
    mine.refresh_from_db()
    theirs.refresh_from_db()
    assert mine.revoked_at is not None and theirs.revoked_at is None
    assert AuditLog.objects.filter(action="agents.token.revoke").count() == 1

    # Admins see and revoke everyone's tokens of the company.
    client.force_login(admin)
    page = client.get(reverse("agents:access"))
    assert b"mine" in page.content
    other, _raw = services.create_token(crew["manager"], name="other")
    assert client.post(reverse("agents:revoke", args=[other.pk])).status_code == 302


def test_platform_admins_cannot_hold_a_token(client, crew):
    client.force_login(PlatformAdminFactory())
    assert client.get(reverse("agents:access")).status_code == 200
    assert (
        client.post(reverse("agents:create"), {"name": "x", "lifetime_days": "30"}).status_code
        == 404
    )


def test_workers_of_another_company_are_invisible(crew):
    other = CompanyFactory()
    with use_company(other):
        CompanySettingsFactory(company=other)
        WorkerFactory(first_name="Ghost", last_name="Elsewhere")
    data = agent(AdminUserFactory()).get(f"{OFFICE}/workers/", {"q": "Ghost"}).json()
    assert data["workers"] == []
