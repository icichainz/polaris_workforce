"""The worker portal PIN by text (Wave 4 integration): giving access or resetting the PIN
queues an EN/FR text with the sign-in link and the PIN — only when the company has
messaging on and the worker did not opt out. The PIN never reaches the audit log, and
the outbox masks it once the text is sent."""

import json

import pytest
from django.core.cache import cache
from django.urls import reverse

from apps.core.models import AuditLog, CompanySettings
from apps.messaging import services as messaging
from apps.messaging.models import ContactPreference, Kind, Message, MessagingSettings
from apps.messaging.providers import LogProvider
from tests.e2e import Office
from tests.test_portal_office import _pin_from, crews  # noqa: F401

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _clear_cache():
    cache.clear()
    yield
    cache.clear()


def messaging_on(company, **extra):
    MessagingSettings.objects.update_or_create(
        company=company, defaults={"enabled": True, "default_country_code": "228", **extra}
    )


def give_pin(user, worker, pin=""):
    office = Office(user)
    data = {"pin": pin, "pin_confirm": pin} if pin else {}
    response = office.post(reverse("portal:office_set_pin", args=[worker.pk]), data)
    assert response.status_code == 200, response.content
    return response


def pin_texts():
    return list(Message.objects.filter(kind=Kind.PORTAL_PIN).order_by("pk"))


def test_no_text_while_messaging_is_off(crews):  # noqa: F811
    response = give_pin(crews["admin"], crews["ama"])

    assert pin_texts() == []
    assert "data-texted" not in response.content.decode()


def test_giving_access_texts_the_pin_and_the_link(crews):  # noqa: F811
    ama = crews["ama"]
    messaging_on(ama.company)

    response = give_pin(crews["manager_a"], ama)
    pin = _pin_from(response)

    [text] = pin_texts()
    assert text.worker == ama and text.to == "+22890000001"
    assert text.status == Message.Status.QUEUED
    assert f"portal PIN: {pin}" in text.text
    assert "http://testserver/portal/sign-in/" in text.text
    assert "data-texted" in response.content.decode()
    # The PIN never reaches the audit log.
    for row in AuditLog.objects.all():
        assert pin not in json.dumps([row.before, row.after, row.reason], default=str)


def test_a_reset_sends_a_new_text_and_the_outbox_forgets_the_pin(crews, caplog):  # noqa: F811
    ama = crews["ama"]
    messaging_on(ama.company)
    give_pin(crews["admin"], ama, pin="4826")
    give_pin(crews["admin"], ama, pin="5937")
    assert [("4826" in m.text, "5937" in m.text) for m in pin_texts()] == [
        (True, False),
        (False, True),
    ]

    with caplog.at_level("INFO", logger="polaris.messaging"):
        summary = messaging.send_pending(ama.company, provider=LogProvider())

    assert summary.sent == 2
    assert "5937" in caplog.text  # the log provider writes the text (see security review)
    for message in pin_texts():
        assert message.status == Message.Status.SENT
        assert "portal PIN: ••••" in message.text
        assert "4826" not in message.text and "5937" not in message.text
    assert messaging.resend(crews["admin"], pin_texts()[0]) is False


def test_the_text_is_in_the_company_language(crews):  # noqa: F811
    ama = crews["ama"]
    messaging_on(ama.company)
    company = CompanySettings.load()
    company.language = "fr"
    company.save()

    pin = _pin_from(give_pin(crews["admin"], ama))

    [text] = pin_texts()
    assert text.language == "fr"
    assert f"code PIN de l’espace ouvrier : {pin}" in text.text
    assert messaging.redact_pin(text.text).count("••••") == 1


def test_no_text_for_a_worker_who_opted_out(crews):  # noqa: F811
    ama = crews["ama"]
    messaging_on(ama.company)
    ContactPreference.objects.create(company=ama.company, worker=ama, opted_in=False)

    give_pin(crews["admin"], ama)

    assert pin_texts() == []


def test_no_text_without_opt_in_when_the_company_requires_one(crews):  # noqa: F811
    ama = crews["ama"]
    messaging_on(ama.company, require_opt_in=True)

    give_pin(crews["admin"], ama)
    assert pin_texts() == []

    ContactPreference.objects.create(company=ama.company, worker=ama, opted_in=True)
    give_pin(crews["admin"], ama)
    assert len(pin_texts()) == 1


def test_a_messaging_failure_does_not_stop_giving_access(crews, monkeypatch):  # noqa: F811
    ama = crews["ama"]
    messaging_on(ama.company)

    def boom(*args, **kwargs):
        raise RuntimeError("provider down")

    monkeypatch.setattr(messaging, "queue_portal_pin", boom)
    response = give_pin(crews["admin"], ama)

    assert _pin_from(response)
    assert ama.portal_access.is_enabled
