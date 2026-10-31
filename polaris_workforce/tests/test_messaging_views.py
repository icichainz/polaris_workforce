"""Messaging office pages: admins only, scoped to their company."""

import pytest
from django.urls import reverse

from apps.accounts.models import User
from apps.core.models import AuditLog
from apps.core.tenancy import use_company
from apps.messaging import services
from apps.messaging.models import ContactPreference, Kind, Message, MessagingSettings
from apps.messaging.providers import SendResult
from tests.factories import (
    AdminUserFactory,
    CompanyFactory,
    CompanySettingsFactory,
    UserFactory,
    WorkerFactory,
)

pytestmark = pytest.mark.django_db

PAGES = ["messaging:messages", "messaging:settings", "messaging:recipients"]


def enable(**fields):
    messaging = MessagingSettings.load()
    for name, value in {"enabled": True, **fields}.items():
        setattr(messaging, name, value)
    messaging.save()
    return messaging


def queue(person, key, kind=Kind.TEST):
    field = "worker" if hasattr(person, "number") else "user"
    return services.queue_message(kind=kind, dedupe_key=key, context={}, **{field: person})


def failed_message(person, key="f"):
    message = queue(person, key)

    class Refuse:
        name = "refuse"

        def send(self, message):
            return SendResult(ok=False, error="no credit", permanent=True)

    services.send_pending(provider=Refuse())
    message.refresh_from_db()
    assert message.status == "failed"
    return message


@pytest.fixture
def admin_client(client):
    admin = AdminUserFactory()
    client.force_login(admin)
    client.user = admin
    return client


@pytest.fixture
def other_company():
    """A second company with a worker, a user and a failed message ("Bravo" everywhere)."""
    company = CompanyFactory(name="Bravo Co")
    with use_company(company):
        CompanySettingsFactory(company_name="Bravo Co")
        enable()
        worker = WorkerFactory(first_name="Bravoworker")
        user = UserFactory(first_name="Bravouser")
        message = failed_message(worker, "bravo")
    return {"company": company, "worker": worker, "user": user, "message": message}


@pytest.mark.parametrize("name", PAGES)
def test_admins_open_the_pages(admin_client, name):
    response = admin_client.get(reverse(name))
    assert response.status_code == 200
    assert b'href="/messages/"' in response.content  # sidebar entry


@pytest.mark.parametrize("role", [User.Role.SITE_MANAGER, User.Role.FOREMAN, User.Role.ACCOUNTANT])
def test_other_roles_are_refused(client, role):
    client.force_login(UserFactory(role=role))
    worker = WorkerFactory()
    urls = [reverse(name) for name in PAGES] + [reverse("messaging:worker", args=[worker.pk])]
    for url in urls:
        assert client.get(url).status_code == 403, url
        assert client.post(url, {"action": "send"}).status_code == 403, url
    assert b'href="/messages/"' not in client.get("/").content


def test_anonymous_people_are_sent_to_sign_in(client):
    response = client.get(reverse("messaging:messages"))
    assert response.status_code == 302
    assert "/accounts/login/" in response["Location"]


def test_messages_log_filters_by_status_and_type(admin_client):
    enable()
    user = UserFactory()
    sent = queue(user, "a")
    services.send_pending()
    failed = failed_message(UserFactory(), "b")
    url = reverse("messaging:messages")

    page = admin_client.get(url).context["page"]
    assert {m.pk for m in page} == {sent.pk, failed.pk}
    page = admin_client.get(url, {"status": "failed"}).context["page"]
    assert [m.pk for m in page] == [failed.pk]
    page = admin_client.get(url, {"kind": "payslip"}).context["page"]
    assert list(page) == []
    response = admin_client.get(url, {"date_from": "2030-01-01"})
    assert list(response.context["page"]) == []


def test_resend_a_failed_message(admin_client):
    enable()
    message = failed_message(UserFactory())
    response = admin_client.post(
        reverse("messaging:messages"),
        {"action": "resend", "message": message.pk, "query": "status=failed"},
    )
    assert response.status_code == 302
    assert response["Location"] == reverse("messaging:messages") + "?status=failed"
    message.refresh_from_db()
    assert message.status == "queued"
    assert AuditLog.objects.filter(action=services.RESEND_ACTION).count() == 1


def test_send_queued_now(admin_client):
    enable()
    message = queue(UserFactory(), "a")
    admin_client.post(reverse("messaging:messages"), {"action": "send"})
    message.refresh_from_db()
    assert message.status == "sent"


def test_bad_posts_are_404(admin_client):
    url = reverse("messaging:messages")
    assert admin_client.post(url, {"action": "resend", "message": "abc"}).status_code == 404
    assert admin_client.post(url, {"action": "nope"}).status_code == 404
    url = reverse("messaging:recipients")
    assert admin_client.post(url, {"user": "abc"}).status_code == 404
    assert admin_client.post(url, {}).status_code == 404


def test_settings_page_saves_and_audits(admin_client):
    url = reverse("messaging:settings")
    data = {
        "enabled": "on",
        "sms_enabled": "on",
        "default_channel": "sms",
        "default_country_code": "+228",
        "quiet_hours_start": "21:00",
        "quiet_hours_end": "06:00",
        "notify_payslip": "on",
        "approval_reminder_hours": "3",
        "advance_limit_warn_percent": "80",
    }
    response = admin_client.post(url, data)
    assert response.status_code == 302
    messaging = MessagingSettings.load()
    assert messaging.enabled and messaging.default_country_code == "228"
    assert messaging.notify_missing_roll_call is False  # unticked
    assert AuditLog.objects.filter(action=services.SETTINGS_ACTION).count() == 1
    response = admin_client.post(url, {**data, "quiet_hours_end": ""})
    assert response.status_code == 200
    assert "quiet_hours_end" in response.context["form"].errors


def test_send_a_test_message(admin_client):
    url = reverse("messaging:settings")
    admin_client.post(url, {"action": "test"})
    assert Message.objects.count() == 0  # messaging off
    enable()
    admin_client.post(url, {"action": "test"})
    message = Message.objects.get()
    assert (message.user, message.status) == (admin_client.user, "sent")


def test_recipients_toggle_users_and_workers(admin_client):
    user, worker = UserFactory(), WorkerFactory(first_name="Zélie")
    url = reverse("messaging:recipients")
    admin_client.post(url, {"user": user.pk, "opted_in": "0"})
    admin_client.post(url, {"worker": worker.pk, "opted_in": "0"})
    assert ContactPreference.objects.get(user=user).opted_in is False
    assert ContactPreference.objects.get(worker=worker).opted_in is False
    admin_client.post(url, {"worker": worker.pk, "opted_in": "1"})
    assert ContactPreference.objects.get(worker=worker).opted_in is True
    response = admin_client.get(url, {"q": "Zélie"})
    assert [row["person"] for row in response.context["worker_rows"]] == [worker]


def test_worker_page_sets_the_choice_and_lists_texts(admin_client):
    enable()
    worker = WorkerFactory()
    queue(worker, "w1")
    url = reverse("messaging:worker", args=[worker.pk])
    response = admin_client.get(url)
    assert response.status_code == 200
    assert len(response.context["sent"]) == 1
    response = admin_client.post(url, {"channel": "whatsapp"})  # opted_in unticked
    assert response.status_code == 302
    preference = ContactPreference.objects.get(worker=worker)
    assert (preference.opted_in, preference.channel) == (False, "whatsapp")


# --- another company -------------------------------------------------------------------------


def test_pages_show_nothing_of_another_company(admin_client, other_company):
    enable()
    queue(UserFactory(first_name="Alphauser"), "a")
    for name in PAGES:
        text = admin_client.get(reverse(name)).content.decode()
        assert "Bravo" not in text, name
    page = admin_client.get(reverse("messaging:messages")).context["page"]
    assert all(m.company_id != other_company["company"].pk for m in page)


def test_another_companys_objects_answer_404_and_stay_unchanged(admin_client, other_company):
    message, worker, user = (other_company[k] for k in ("message", "worker", "user"))
    url = reverse("messaging:messages")
    assert admin_client.post(url, {"action": "resend", "message": message.pk}).status_code == 404
    message.refresh_from_db()
    assert message.status == "failed"
    url = reverse("messaging:recipients")
    assert admin_client.post(url, {"worker": worker.pk, "opted_in": "0"}).status_code == 404
    assert admin_client.post(url, {"user": user.pk, "opted_in": "0"}).status_code == 404
    worker_url = reverse("messaging:worker", args=[worker.pk])
    assert admin_client.get(worker_url).status_code == 404
    assert admin_client.post(worker_url, {"opted_in": ""}).status_code == 404
    assert ContactPreference.objects.filter(company=other_company["company"]).count() == 0


def test_send_now_only_sends_your_company(admin_client, other_company):
    enable()
    mine = queue(UserFactory(), "a")
    with use_company(other_company["company"]):
        theirs = queue(UserFactory(), "b")
    admin_client.post(reverse("messaging:messages"), {"action": "send"})
    mine.refresh_from_db()
    theirs.refresh_from_db()
    assert (mine.status, theirs.status) == ("sent", "queued")


def test_dedupe_keys_are_per_company(other_company):
    enable()
    assert queue(UserFactory(), "bravo") is not None  # "bravo" is taken in the other company
