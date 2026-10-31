"""Messaging: the outbox, providers, phone numbers, texts in EN/FR, opt-in and the payslip text."""

import datetime
import json
import logging

import pytest
from django.core.exceptions import ImproperlyConfigured
from django.utils import timezone

from apps.core.models import AuditLog
from apps.core.tenancy import use_company
from apps.messaging import services
from apps.messaging.forms import MessagingSettingsForm
from apps.messaging.models import Channel, ContactPreference, Kind, Message, MessagingSettings
from apps.messaging.phones import normalise_phone
from apps.messaging.providers import (
    ConsoleProvider,
    FileProvider,
    LogProvider,
    SendResult,
    get_provider,
)
from apps.messaging.rendering import language_for, render_text
from tests.factories import (
    AdminUserFactory,
    AdvanceFactory,
    CompanyFactory,
    CompanySettingsFactory,
    PayLineFactory,
    PaymentFactory,
    PayPeriodFactory,
    SiteFactory,
    UserFactory,
    WorkerFactory,
)

pytestmark = pytest.mark.django_db

FRI = datetime.date(2026, 10, 30)


def enable(**fields) -> MessagingSettings:
    messaging = MessagingSettings.load()
    for name, value in {"enabled": True, **fields}.items():
        setattr(messaging, name, value)
    messaging.save()
    return messaging


class FakeProvider:
    """Records what it was asked to send; answers with ``results`` in turn."""

    name = "fake"

    def __init__(self, *results):
        self.results = list(results)
        self.sent = []

    def send(self, message):
        self.sent.append(message.pk)
        result = self.results.pop(0) if self.results else SendResult(ok=True, provider_id="x")
        if isinstance(result, Exception):
            raise result
        return result


def queue_for(person, key="k1", kind=Kind.TEST, **kwargs):
    field = "worker" if hasattr(person, "number") else "user"
    return services.queue_message(
        kind=kind, dedupe_key=key, context={}, **{field: person}, **kwargs
    )


# --- phone numbers -------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "code", "expected"),
    [
        ("+228 90 01 01 01", "", "+22890010101"),
        ("00228 90-01-01-01", "", "+22890010101"),
        ("(+228) 90.01.01.01", "", "+22890010101"),
        ("90 01 01 01", "228", "+22890010101"),
        ("090 01 01 01", "228", "+22890010101"),
        ("90 01 01 01", "", None),
        ("+228 9001", "", None),
        ("+1234567890123456", "", None),
        ("call me", "228", None),
        ("", "228", None),
        (None, "228", None),
    ],
)
def test_phone_numbers_are_normalised(raw, code, expected):
    assert normalise_phone(raw, code) == expected


# --- texts ---------------------------------------------------------------------------------


def test_payslip_text_in_english_and_french():
    context = {
        "company": "Acme",
        "amount": "4 000 XOF",
        "start": FRI,
        "end": FRI,
        "site": "Riverside & Co",
        "advance_owed": "2 000",
    }
    assert render_text("payslip", "en", context) == (
        "Acme: Paid 4 000 XOF for Fri 30 Oct · Riverside & Co · balance owed on advance 2 000"
    )
    assert render_text("payslip", "fr", context) == (
        "Acme : Payé 4 000 XOF pour ven 30 oct. · Riverside & Co · reste dû sur l’avance 2 000"
    )


def test_payslip_text_for_several_days_without_advance():
    context = {
        "company": "Acme",
        "amount": "20 000 XOF",
        "start": FRI - datetime.timedelta(days=4),
        "end": FRI,
        "site": "Riverside",
        "advance_owed": "",
    }
    assert render_text("payslip", "en", context) == (
        "Acme: Paid 20 000 XOF for 26 Oct – 30 Oct · Riverside"
    )


@pytest.mark.parametrize(
    ("kind", "context", "english", "french"),
    [
        (
            "missing_roll_call",
            {"site": "A", "due": datetime.time(8, 0)},
            "The roll call for A is not in yet (due 08:00 today). Please send it.",
            "L’appel de A n’est pas encore envoyé (attendu à 08:00 aujourd’hui). "
            "Merci de l’envoyer.",
        ),
        (
            "awaiting_approval",
            {"count": 2, "oldest_date": FRI, "oldest_site": "A"},
            "2 roll calls waiting for your approval, the oldest Fri 30 Oct · A.",
            "2 appels en attente de votre validation, le plus ancien ven 30 oct. · A.",
        ),
        (
            "unclosed_cash_sheet",
            {"count": 4, "sheets": [{"site": "A", "date": FRI}], "more": 3},
            "4 cash sheets not closed after pay day: A (Fri 30 Oct) and 3 more.",
            "4 feuilles de caisse non clôturées après le jour de paie : A (ven 30 oct.) "
            "et 3 autres.",
        ),
        (
            "advance_limit",
            {"worker": "Ama K", "owed": "9 000 XOF", "limit": "10 000 XOF", "percent": 90},
            "Ama K owes 9 000 XOF on advances, 90% of the limit (10 000 XOF).",
            "Ama K doit 9 000 XOF sur ses avances, soit 90 % du plafond (10 000 XOF).",
        ),
    ],
)
def test_every_notification_has_an_english_and_a_french_text(kind, context, english, french):
    context = {**context, "company": "Acme"}
    assert render_text(kind, "en", context) == f"Acme: {english}"
    assert render_text(kind, "fr", context) == f"Acme : {french}"


def test_every_message_type_has_both_templates():
    for kind in Kind.values:
        for language in ("en", "fr"):
            assert render_text(kind, language, {"company": "Acme", "due": None}).startswith(
                "Acme"
            ), (kind, language)


def test_language_is_the_users_then_the_companys():
    company = CompanySettingsFactory(language="fr")
    assert language_for(company, UserFactory(language="en")) == "en"
    assert language_for(company, UserFactory(language="")) == "fr"
    assert language_for(company, None) == "fr"  # workers


def test_queued_text_uses_the_recipients_language():
    CompanySettingsFactory(language="en", company_name="Acme")
    enable()
    message = queue_for(UserFactory(language="fr"))
    assert message.language == "fr"
    assert message.text.startswith("Acme : Message de test")


# --- queueing rules ----------------------------------------------------------------------


def test_nothing_is_queued_while_messaging_is_off():
    assert MessagingSettings.load().enabled is False  # off by default
    assert queue_for(UserFactory()) is None
    assert Message.objects.count() == 0


def test_a_dedupe_key_is_queued_once_per_company():
    enable()
    user = UserFactory()
    assert queue_for(user, key="same") is not None
    assert queue_for(user, key="same") is None
    assert Message.objects.count() == 1


def test_nobody_without_a_phone_gets_a_message():
    enable()
    assert queue_for(UserFactory(phone="")) is None
    assert queue_for(WorkerFactory(phone="  ")) is None


def test_opted_out_people_get_nothing():
    enable()
    user, worker = UserFactory(), WorkerFactory()
    admin = AdminUserFactory()
    services.set_preference(admin, user=user, opted_in=False)
    services.set_preference(admin, worker=worker, opted_in=False)
    assert queue_for(user, key="u") is None
    assert queue_for(worker, key="w") is None


def test_require_opt_in_only_messages_people_who_opted_in():
    enable(require_opt_in=True)
    yes, unknown = UserFactory(), UserFactory()
    services.set_preference(AdminUserFactory(), user=yes, opted_in=True)
    assert queue_for(yes, key="a") is not None
    assert queue_for(unknown, key="b") is None


def test_a_number_without_country_code_is_skipped_until_one_is_set():
    enable()
    message = queue_for(UserFactory(phone="90 01 01 01"), key="a")
    assert message.status == Message.Status.SKIPPED
    assert "country code" in message.error
    enable(default_country_code="228")
    message = queue_for(UserFactory(phone="90 01 01 02"), key="b")
    assert (message.status, message.to) == (Message.Status.QUEUED, "+22890010102")


def test_channel_is_the_persons_if_the_company_has_it_on():
    enable(whatsapp_enabled=False)
    user = UserFactory()
    services.set_preference(AdminUserFactory(), user=user, opted_in=True, channel="whatsapp")
    assert queue_for(user, key="a").channel == Channel.SMS
    enable(whatsapp_enabled=True)
    assert queue_for(user, key="b").channel == Channel.WHATSAPP
    enable(sms_enabled=True, whatsapp_enabled=True, default_channel="whatsapp")
    assert queue_for(UserFactory(), key="c").channel == Channel.WHATSAPP


def test_preference_changes_are_audited():
    admin, worker = AdminUserFactory(), WorkerFactory()
    services.set_preference(admin, worker=worker, opted_in=False)
    services.set_preference(admin, worker=worker, opted_in=False)  # no change, no entry
    entries = AuditLog.objects.filter(action=services.PREFERENCE_ACTION)
    assert entries.count() == 1
    entry = entries.get()
    assert (entry.before, entry.after) == (
        {"opted_in": None, "channel": ""},
        {"opted_in": False, "channel": ""},
    )
    assert entry.target == worker
    assert ContactPreference.objects.get(worker=worker).updated_by == admin


def test_settings_changes_are_audited():
    admin = AdminUserFactory()
    messaging = MessagingSettings.load()
    before = services.settings_snapshot(messaging)
    data = {
        **{k: v for k, v in before.items() if v not in (None, False)},
        "enabled": "on",
        "quiet_hours_start": "21:00",
        "quiet_hours_end": "06:00",
    }
    form = MessagingSettingsForm(data, instance=messaging)
    assert form.is_valid(), form.errors
    changed = services.save_settings(form, actor=admin, before=before)
    assert set(changed) == {"enabled", "quiet_hours_start", "quiet_hours_end"}
    entry = AuditLog.objects.get(action=services.SETTINGS_ACTION)
    assert entry.after == {
        "enabled": True,
        "quiet_hours_start": "21:00",
        "quiet_hours_end": "06:00",
    }
    assert entry.company_id == messaging.company_id


def test_settings_form_checks_quiet_hours_and_channels():
    messaging = MessagingSettings.load()
    form = MessagingSettingsForm(
        {
            "enabled": "on",
            "quiet_hours_start": "21:00",
            "default_channel": "sms",
            "approval_reminder_hours": 2,
            "advance_limit_warn_percent": 80,
        },
        instance=messaging,
    )
    assert not form.is_valid()
    assert "quiet_hours_end" in form.errors
    assert "sms_enabled" in form.errors  # messaging on without any channel


# --- sending -------------------------------------------------------------------------------


def test_send_pending_sends_with_the_provider():
    enable()
    message = queue_for(UserFactory())
    provider = FakeProvider()
    summary = services.send_pending(provider=provider)
    message.refresh_from_db()
    assert (summary.sent, provider.sent) == (1, [message.pk])
    assert (message.status, message.provider, message.provider_id) == ("sent", "fake", "x")
    assert message.attempts == 1 and message.sent_at is not None
    assert services.send_pending(provider=provider).sent == 0  # nothing left


def test_failures_are_retried_with_backoff_then_marked_failed(monkeypatch):
    monkeypatch.setenv("MESSAGING_MAX_ATTEMPTS", "3")
    enable()
    message = queue_for(UserFactory())
    provider = FakeProvider(
        SendResult(ok=False, error="timeout"), RuntimeError("down"), SendResult(ok=False)
    )
    now = timezone.now()

    assert services.send_pending(now=now, provider=provider).retrying == 1
    message.refresh_from_db()
    assert (message.status, message.attempts, message.error) == ("queued", 1, "timeout")
    assert message.next_attempt_at == now + datetime.timedelta(minutes=1)

    # Not due yet: nothing happens.
    assert services.send_pending(now=now, provider=provider).retrying == 0
    assert len(provider.sent) == 1

    later = now + datetime.timedelta(minutes=1)
    assert services.send_pending(now=later, provider=provider).retrying == 1
    message.refresh_from_db()
    assert (message.attempts, message.error) == (2, "down")
    assert message.next_attempt_at == later + datetime.timedelta(minutes=2)

    last = later + datetime.timedelta(minutes=2)
    summary = services.send_pending(now=last, provider=provider)
    message.refresh_from_db()
    assert summary.failed == 1
    assert (message.status, message.attempts, message.next_attempt_at) == ("failed", 3, None)


def test_a_permanent_failure_is_not_retried():
    enable()
    message = queue_for(UserFactory())
    services.send_pending(provider=FakeProvider(SendResult(ok=False, error="bad", permanent=True)))
    message.refresh_from_db()
    assert (message.status, message.attempts, message.error) == ("failed", 1, "bad")


def test_resend_queues_a_failed_message_again_and_audits_it():
    enable()
    admin = AdminUserFactory()
    message = queue_for(UserFactory())
    services.send_pending(provider=FakeProvider(SendResult(ok=False, permanent=True)))
    message.refresh_from_db()
    assert services.resend(admin, message) is True
    message.refresh_from_db()
    assert (message.status, message.attempts, message.error) == ("queued", 0, "")
    assert AuditLog.objects.filter(action=services.RESEND_ACTION, actor=admin).count() == 1
    assert services.send_pending(provider=FakeProvider()).sent == 1
    message.refresh_from_db()
    assert services.resend(admin, message) is False  # only failed ones


def test_someone_who_opts_out_after_queueing_is_not_messaged():
    enable()
    user = UserFactory()
    message = queue_for(user)
    services.set_preference(AdminUserFactory(), user=user, opted_in=False)
    provider = FakeProvider()
    assert services.send_pending(provider=provider).skipped == 1
    message.refresh_from_db()
    assert message.status == "skipped" and provider.sent == []


def test_turning_messaging_off_skips_what_is_queued():
    enable()
    message = queue_for(UserFactory())
    enable(enabled=False)
    provider = FakeProvider()
    services.send_pending(provider=provider)
    message.refresh_from_db()
    assert message.status == "skipped" and provider.sent == []


def test_quiet_hours_hold_messages_back():
    CompanySettingsFactory(time_zone="Africa/Lome")  # UTC+0
    enable(quiet_hours_start=datetime.time(21, 0), quiet_hours_end=datetime.time(6, 0))
    queue_for(UserFactory())
    night = datetime.datetime(2026, 10, 30, 22, 30, tzinfo=datetime.UTC)
    provider = FakeProvider()
    assert services.send_pending(now=night, provider=provider).waiting == 1
    assert provider.sent == []
    morning = datetime.datetime(2026, 10, 31, 6, 0, tzinfo=datetime.UTC)
    assert services.send_pending(now=morning, provider=provider).sent == 1


@pytest.mark.parametrize(
    ("start", "end", "at", "quiet"),
    [
        (datetime.time(21), datetime.time(6), datetime.time(23), True),
        (datetime.time(21), datetime.time(6), datetime.time(5, 59), True),
        (datetime.time(21), datetime.time(6), datetime.time(6), False),
        (datetime.time(12), datetime.time(14), datetime.time(13), True),
        (datetime.time(12), datetime.time(14), datetime.time(14), False),
        (None, None, datetime.time(3), False),
    ],
)
def test_quiet_hours(start, end, at, quiet):
    messaging = MessagingSettings(quiet_hours_start=start, quiet_hours_end=end)
    assert messaging.in_quiet_hours(at) is quiet


def test_a_message_claimed_by_another_run_is_not_sent_twice():
    enable()
    message = queue_for(UserFactory())
    stale = Message.objects.get(pk=message.pk)
    Message.objects.filter(pk=message.pk).update(attempts=1)  # another run took it
    provider = FakeProvider()
    assert services.deliver(stale, MessagingSettings.load(), provider) == "busy"
    assert provider.sent == []


# --- providers -----------------------------------------------------------------------------


def test_the_default_provider_logs_and_marks_sent(monkeypatch, caplog):
    monkeypatch.delenv("MESSAGING_PROVIDER", raising=False)
    assert isinstance(get_provider(), LogProvider)
    enable()
    message = queue_for(UserFactory())
    with caplog.at_level(logging.INFO, logger="polaris.messaging"):
        assert services.send_pending().sent == 1
    message.refresh_from_db()
    assert (message.status, message.provider, message.provider_id) == (
        "sent",
        "log",
        f"log-{message.pk}",
    )
    assert message.text in caplog.text


def test_console_and_file_providers(monkeypatch, tmp_path, capsys):
    enable()
    message = queue_for(UserFactory())
    monkeypatch.setenv("MESSAGING_PROVIDER", "console")
    assert isinstance(get_provider(), ConsoleProvider)
    assert get_provider().send(message).ok
    assert message.text in capsys.readouterr().out

    path = tmp_path / "out" / "messages.jsonl"
    monkeypatch.setenv("MESSAGING_PROVIDER", "file")
    monkeypatch.setenv("MESSAGING_FILE_PATH", str(path))
    provider = get_provider()
    assert isinstance(provider, FileProvider)
    assert provider.send(message).ok
    line = json.loads(path.read_text(encoding="utf-8").splitlines()[0])
    assert (line["id"], line["to"], line["text"]) == (message.pk, message.to, message.text)


def test_a_provider_can_be_a_dotted_path(monkeypatch):
    monkeypatch.setenv("MESSAGING_PROVIDER", "tests.test_messaging_services.FakeProvider")
    assert isinstance(get_provider(), FakeProvider)
    monkeypatch.setenv("MESSAGING_PROVIDER", "nope")
    with pytest.raises(ImproperlyConfigured):
        get_provider()
    monkeypatch.setenv("MESSAGING_PROVIDER", "nowhere.Provider")
    with pytest.raises(ImproperlyConfigured):
        get_provider()


# --- payslip text --------------------------------------------------------------------------


def _payment(net=4000, **worker_kwargs):
    site = SiteFactory(name="Riverside")
    worker = WorkerFactory(**worker_kwargs)
    period = PayPeriodFactory(site=site, start_date=FRI, end_date=FRI)
    line = PayLineFactory(period=period, worker=worker, gross=net, net=net)
    return PaymentFactory(pay_line=line, amount=net)


def test_a_payment_queues_the_workers_payslip_text():
    CompanySettingsFactory(company_name="Acme", language="en")
    enable()
    worker = WorkerFactory(phone="+228 91 00 00 01")
    AdvanceFactory(worker=worker, amount=5000, balance=2000)
    site = SiteFactory(name="Riverside")
    period = PayPeriodFactory(site=site, start_date=FRI, end_date=FRI)
    line = PayLineFactory(period=period, worker=worker, gross=4000, net=4000)
    payment = PaymentFactory(pay_line=line, amount=4000)

    message = Message.objects.get()
    assert (message.kind, message.worker, message.to, message.status) == (
        "payslip",
        worker,
        "+22891000001",
        "queued",
    )
    assert message.dedupe_key == f"payslip:{payment.pk}"
    assert message.text == (
        "Acme: Paid 4 000 XOF for Fri 30 Oct · Riverside · balance owed on advance 2 000"
    )
    payment.save()  # saving again queues nothing more
    assert Message.objects.count() == 1


def test_the_payslip_text_is_in_the_company_language():
    CompanySettingsFactory(company_name="Acme", language="fr")
    enable()
    _payment()
    message = Message.objects.get()
    assert message.language == "fr"
    assert message.text == "Acme : Payé 4 000 XOF pour ven 30 oct. · Riverside"


@pytest.mark.parametrize(
    "setup",
    ["off", "payslip_off", "no_phone", "opted_out"],
)
def test_no_payslip_text_when_off_or_not_wanted(setup):
    if setup != "off":
        enable(notify_payslip=setup != "payslip_off")
    worker = WorkerFactory(phone="" if setup == "no_phone" else "+228 91 00 00 02")
    if setup == "opted_out":
        services.set_preference(AdminUserFactory(), worker=worker, opted_in=False)
    line = PayLineFactory(worker=worker, gross=3000, net=3000)
    PaymentFactory(pay_line=line)
    assert Message.objects.count() == 0


def test_undoing_a_payment_cancels_its_queued_text():
    enable()
    payment = _payment()
    message = Message.objects.get()
    payment.delete()
    message.refresh_from_db()
    assert message.status == "skipped"
    assert message.error == "The payment was undone."


def test_a_messaging_error_never_stops_a_payment(monkeypatch):
    enable()

    def broken(payment):
        raise RuntimeError("boom")

    monkeypatch.setattr(services, "queue_payslip", broken)
    payment = _payment()
    assert payment.pk is not None
    assert Message.objects.count() == 0


def test_the_payslip_text_belongs_to_the_company_of_the_payment(default_company):
    other = CompanyFactory(name="Bravo Co")
    with use_company(other):
        CompanySettingsFactory(company_name="Bravo Co")
        enable()
        line = PayLineFactory(gross=3000, net=3000)
    PaymentFactory(pay_line=line)  # recorded while another company is current
    message = Message.objects.get()
    assert message.company == other
    assert message.text.startswith("Bravo Co:")
    assert MessagingSettings.load(default_company).enabled is False


def test_the_test_message_goes_to_the_admins_own_phone():
    enable()
    admin = AdminUserFactory(phone="+228 90 99 99 99")
    message = services.send_test_message(admin)
    assert (message.kind, message.to, message.status, message.user) == (
        "test",
        "+22890999999",
        "sent",
        admin,
    )
