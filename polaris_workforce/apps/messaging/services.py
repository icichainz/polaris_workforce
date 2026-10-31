"""The outbox: queue a message, send what is due, retry with backoff, resend.

Rules for every message:
- Messaging is on for the company and for that message type (``MessagingSettings``).
- The person has a phone number and has not opted out (with "only message people who
  opted in", they must have opted in). Checked again just before sending.
- A ``dedupe_key`` is queued at most once per company: a payment gives one payslip text,
  a notification rule one text per person, subject and day.
- Nothing is sent while queueing: ``send_pending`` (the ``send_messages`` and
  ``send_notifications`` commands) hands due messages to the provider. Failures are
  retried ``MAX_ATTEMPTS`` times with exponential backoff, then marked failed; an admin
  can resend a failed message from the Messages page.
"""

from __future__ import annotations

import datetime
import logging
import os
import re
import uuid
from dataclasses import dataclass, field

from django.db import IntegrityError, transaction
from django.db.models import F, Q, Sum
from django.utils import timezone
from django.utils.translation import gettext as _

from apps.core.dates import company_tz
from apps.core.display import format_amount
from apps.core.models import AuditLog, Company, CompanySettings

from .models import Channel, ContactPreference, Kind, Message, MessagingSettings
from .phones import normalise_phone
from .providers import Provider, SendResult, get_provider
from .rendering import language_for, render_text

logger = logging.getLogger("polaris.messaging")

MAX_ATTEMPTS_ENV = "MESSAGING_MAX_ATTEMPTS"
DEFAULT_MAX_ATTEMPTS = 5
BACKOFF_FIRST = datetime.timedelta(minutes=1)
BACKOFF_MAX = datetime.timedelta(hours=6)
SEND_BATCH = 200

SETTINGS_ACTION = "messaging.settings.update"
PREFERENCE_ACTION = "messaging.preference.update"
RESEND_ACTION = "messaging.message.resend"


def max_attempts() -> int:
    try:
        return max(1, int(os.environ.get(MAX_ATTEMPTS_ENV, DEFAULT_MAX_ATTEMPTS)))
    except ValueError:
        return DEFAULT_MAX_ATTEMPTS


def backoff(attempts: int) -> datetime.timedelta:
    """Wait before the next try after ``attempts`` failed ones: 1, 2, 4, 8… minutes."""
    return min(BACKOFF_FIRST * (2 ** max(0, attempts - 1)), BACKOFF_MAX)


def company_id_for(company) -> int:
    if company is None:
        from apps.core.tenancy import current_company

        company = current_company()
    return getattr(company, "pk", company)


def company_label(company_settings: CompanySettings) -> str:
    """The name that starts every text (the sender is not always shown)."""
    name = company_settings.company_name
    if not name:
        name = (
            Company.objects.filter(pk=company_settings.company_id)
            .values_list("name", flat=True)
            .first()
        )
    return name or "Polaris"


# --- preferences -----------------------------------------------------------------------


def preference_for(company_id: int, *, user=None, worker=None) -> ContactPreference | None:
    if user is not None:
        return ContactPreference.objects.filter(company_id=company_id, user=user).first()
    if worker is not None:
        return ContactPreference.objects.filter(company_id=company_id, worker=worker).first()
    return None


def may_message(messaging: MessagingSettings, preference: ContactPreference | None) -> bool:
    """Opted out: never. No choice yet: unless the company requires an opt-in."""
    if preference is not None:
        return preference.opted_in
    return not messaging.require_opt_in


def channel_for(messaging: MessagingSettings, preference: ContactPreference | None) -> str | None:
    """The person's channel if the company has it on, else the company's first channel."""
    channels = messaging.channels
    if preference is not None and preference.channel in channels:
        return preference.channel
    return channels[0] if channels else None


def _preference_snapshot(preference: ContactPreference | None) -> dict:
    if preference is None:
        return {"opted_in": None, "channel": ""}
    return {"opted_in": preference.opted_in, "channel": preference.channel}


def set_preference(
    actor, *, user=None, worker=None, opted_in: bool, channel: str = ""
) -> ContactPreference:
    """Opt a user or a worker in or out (and pick their channel); audited."""
    person = user if user is not None else worker
    company_id = person.company_id
    if channel and channel not in Channel.values:
        raise ValueError(f"unknown channel {channel!r}")
    with transaction.atomic():
        preference = preference_for(company_id, user=user, worker=worker)
        before = _preference_snapshot(preference)
        if preference is None:
            preference = ContactPreference(company_id=company_id, user=user, worker=worker)
        preference.opted_in = opted_in
        preference.channel = channel
        preference.updated_by = actor if getattr(actor, "pk", None) else None
        preference.save()
        after = _preference_snapshot(preference)
        if after != before:
            AuditLog.record(
                actor=actor,
                action=PREFERENCE_ACTION,
                target=person,
                before=before,
                after=after,
                company=company_id,
            )
    return preference


# --- settings --------------------------------------------------------------------------

_SKIPPED_FIELDS = {"id", "company", "created_at", "updated_at"}


def _jsonable(value):
    if isinstance(value, datetime.time):
        return value.strftime("%H:%M")
    return value


def settings_snapshot(messaging: MessagingSettings) -> dict:
    return {
        f.name: _jsonable(getattr(messaging, f.attname))
        for f in MessagingSettings._meta.concrete_fields
        if f.name not in _SKIPPED_FIELDS
    }


def save_settings(form, *, actor, before: dict) -> tuple[str, ...]:
    """Save a valid ``MessagingSettingsForm``; audit the changed values. Returns them."""
    messaging = form.instance
    after = settings_snapshot(messaging)
    changed = tuple(name for name in after if after[name] != before.get(name))
    if not changed:
        return ()
    with transaction.atomic():
        form.save()
        AuditLog.record(
            actor=actor,
            action=SETTINGS_ACTION,
            target=messaging,
            before={name: before.get(name) for name in changed},
            after={name: after[name] for name in changed},
            company=messaging.company_id,
        )
    return changed


# --- queueing --------------------------------------------------------------------------


def queue_message(
    *,
    kind: str,
    dedupe_key: str,
    context: dict,
    user=None,
    worker=None,
    company=None,
) -> Message | None:
    """Queue a text for a user or a worker. ``None`` when nothing is queued: messaging
    or this message type is off, the key was already queued, the person opted out or
    has no phone number. A number that cannot be used gives a *skipped* message (shown
    on the Messages page so someone fixes it)."""
    company_id = company_id_for(company)
    messaging = MessagingSettings.load(company_id)
    if not messaging.is_on(kind):
        return None
    if Message.objects.filter(company_id=company_id, dedupe_key=dedupe_key).exists():
        return None
    person = user if user is not None else worker
    raw_phone = (getattr(person, "phone", "") or "").strip()
    if person is None or not raw_phone:
        return None
    preference = preference_for(company_id, user=user, worker=worker)
    if not may_message(messaging, preference):
        return None
    company_settings = CompanySettings.load(company_id)
    language = language_for(company_settings, user)
    text = render_text(kind, language, {**context, "company": company_label(company_settings)})
    to = normalise_phone(raw_phone, messaging.default_country_code)
    message = Message(
        company_id=company_id,
        channel=channel_for(messaging, preference),
        to=to or raw_phone[:20],
        user=user,
        worker=worker,
        kind=kind,
        text=text,
        language=language,
        dedupe_key=dedupe_key[:200],
    )
    if to is None:
        message.status = Message.Status.SKIPPED
        message.error = _("The phone number cannot be used (add the country code).")
    try:
        with transaction.atomic():
            message.save()
    except IntegrityError:
        return None  # queued meanwhile by another run
    return message


def queue_payslip(payment) -> Message | None:
    """The short payslip text for a payment just recorded (dedupe: one per payment)."""
    from apps.payroll.models import Advance, PayLine

    # Read the line afresh: following ``payment.pay_line.period`` would cache a period on
    # the caller's payment, and payroll later saves that (stale) period.
    line = PayLine.objects.select_related("period__site", "worker").get(pk=payment.pay_line_id)
    period = line.period
    site = period.site
    worker = line.worker
    company_id = site.company_id
    messaging = MessagingSettings.load(company_id)
    if not messaging.is_on(Kind.PAYSLIP) or not (worker.phone or "").strip():
        return None
    company_settings = CompanySettings.load(company_id)
    owed = (
        Advance.objects.filter(worker=worker, balance__gt=0).aggregate(total=Sum("balance"))[
            "total"
        ]
        or 0
    )
    money = {"currency": period.currency or None, "decimals": period.currency_decimals}
    context = {
        "amount": format_amount(payment.amount, company_settings, **money),
        "start": period.start_date,
        "end": period.end_date,
        "site": site.name,
        "worker": worker.full_name,
        "advance_owed": (
            format_amount(owed, company_settings, show_currency=False, **money) if owed else ""
        ),
    }
    return queue_message(
        kind=Kind.PAYSLIP,
        dedupe_key=f"payslip:{payment.pk}",
        context=context,
        worker=worker,
        company=company_id,
    )


# The PIN in a portal PIN text, in each language's template
# (``templates/messaging/texts/portal_pin.<language>.txt``).
_PIN_IN_TEXT = re.compile(
    r"(portal PIN: |code PIN de l’espace ouvrier : |code PIN du portail : )\d{4}"
)
PIN_MASK = "••••"


def redact_pin(text: str) -> str:
    """The text with the portal PIN masked: what the outbox keeps once it is sent."""
    return _PIN_IN_TEXT.sub(lambda m: m.group(1) + PIN_MASK, text)


def queue_portal_pin(worker, pin: str, sign_in_url: str) -> Message | None:
    """Text the worker their new portal PIN and the sign-in link (after the office gave
    access or reset the PIN). ``None`` when messaging is off for the company, the worker
    opted out or has no phone. The PIN is never written to the audit log; the outbox
    keeps it only until the text is sent (``redact_pin``)."""
    return queue_message(
        kind=Kind.PORTAL_PIN,
        # Every new PIN is its own text (a reset sends again).
        dedupe_key=f"portal_pin:{worker.pk}:{uuid.uuid4().hex}",
        context={"pin": pin, "link": sign_in_url, "worker": worker.full_name},
        worker=worker,
        company=worker.company_id,
    )


def cancel_payslip(company_id: int, payment_id: int) -> None:
    """A payment undone before its text went out: the text is not sent."""
    Message.objects.filter(
        company_id=company_id, dedupe_key=f"payslip:{payment_id}", status=Message.Status.QUEUED
    ).update(
        status=Message.Status.SKIPPED,
        error=_("The payment was undone."),
        next_attempt_at=None,
        updated_at=timezone.now(),
    )


def send_test_message(actor) -> Message | None:
    """A test text to the signed-in user's own phone (checks the provider end to end)."""
    stamp = timezone.now().strftime("%Y%m%d%H%M%S%f")
    message = queue_message(
        kind=Kind.TEST,
        dedupe_key=f"test:u{actor.pk}:{stamp}",
        context={},
        user=actor,
        company=actor.company_id,
    )
    if message is not None and message.status == Message.Status.QUEUED:
        deliver(message, MessagingSettings.load(actor.company_id), get_provider())
    return message


# --- sending ---------------------------------------------------------------------------


@dataclass
class SendSummary:
    sent: int = 0
    retrying: int = 0
    failed: int = 0
    skipped: int = 0
    waiting: int = 0  # quiet hours
    errors: list[str] = field(default_factory=list)

    def __add__(self, other: SendSummary) -> SendSummary:
        return SendSummary(
            sent=self.sent + other.sent,
            retrying=self.retrying + other.retrying,
            failed=self.failed + other.failed,
            skipped=self.skipped + other.skipped,
            waiting=self.waiting + other.waiting,
            errors=self.errors + other.errors,
        )


def _skip(message: Message, reason: str) -> None:
    message.status = Message.Status.SKIPPED
    message.error = reason
    message.next_attempt_at = None
    if message.kind == Kind.PORTAL_PIN:
        message.text = redact_pin(message.text)
    message.save(update_fields=["status", "error", "next_attempt_at", "text", "updated_at"])


def _claim(message: Message) -> bool:
    """Count the attempt in the database first, so two runs never send one message twice."""
    claimed = Message.objects.filter(
        pk=message.pk, status=Message.Status.QUEUED, attempts=message.attempts
    ).update(attempts=F("attempts") + 1)
    if claimed:
        message.attempts += 1
    return bool(claimed)


def deliver(
    message: Message,
    messaging: MessagingSettings,
    provider: Provider,
    now: datetime.datetime | None = None,
) -> str:
    """Try to send one queued message; returns its new status ("retrying" if queued)."""
    now = now or timezone.now()
    if not messaging.enabled:
        _skip(message, _("Messaging was turned off."))
        return Message.Status.SKIPPED
    if message.channel not in messaging.channels:
        _skip(message, _("This channel was turned off."))
        return Message.Status.SKIPPED
    preference = preference_for(message.company_id, user=message.user, worker=message.worker)
    if (message.user_id or message.worker_id) and not may_message(messaging, preference):
        _skip(message, _("The person opted out."))
        return Message.Status.SKIPPED
    if not _claim(message):
        return "busy"
    try:
        result = provider.send(message)
    except Exception as exc:  # noqa: BLE001 - any provider error is a failed attempt
        logger.exception("messaging provider %s failed", getattr(provider, "name", "?"))
        result = SendResult(ok=False, error=str(exc) or exc.__class__.__name__)
    message.provider = getattr(provider, "name", "")[:64]
    if result.ok:
        message.status = Message.Status.SENT
        message.sent_at = now
        message.provider_id = (result.provider_id or "")[:128]
        message.error = ""
        message.next_attempt_at = None
        outcome = Message.Status.SENT
    elif result.permanent or message.attempts >= max_attempts():
        message.status = Message.Status.FAILED
        message.error = result.error or _("The provider refused the message.")
        message.next_attempt_at = None
        outcome = Message.Status.FAILED
    else:
        message.error = result.error or _("The provider refused the message.")
        message.next_attempt_at = now + backoff(message.attempts)
        outcome = "retrying"
    if message.kind == Kind.PORTAL_PIN and outcome != "retrying":
        message.text = redact_pin(message.text)  # sent or given up: keep no PIN
    message.save(
        update_fields=[
            "text",
            "status",
            "sent_at",
            "provider",
            "provider_id",
            "error",
            "next_attempt_at",
            "updated_at",
        ]
    )
    return outcome


def send_pending(
    company=None,
    *,
    now: datetime.datetime | None = None,
    provider: Provider | None = None,
    limit: int = SEND_BATCH,
) -> SendSummary:
    """Send the company's due messages (queued, and past their retry time)."""
    company_id = company_id_for(company)
    now = now or timezone.now()
    messaging = MessagingSettings.load(company_id)
    due = (
        Message.objects.filter(company_id=company_id, status=Message.Status.QUEUED)
        .filter(Q(next_attempt_at__isnull=True) | Q(next_attempt_at__lte=now))
        .select_related("user", "worker")
        .order_by("created_at", "id")
    )
    summary = SendSummary()
    if messaging.enabled and messaging.quiet_hours_start is not None:
        local = now.astimezone(company_tz(CompanySettings.load(company_id))).time()
        if messaging.in_quiet_hours(local):
            summary.waiting = due.count()
            return summary
    provider = provider or get_provider()
    for message in due[:limit]:
        outcome = deliver(message, messaging, provider, now)
        if outcome == Message.Status.SENT:
            summary.sent += 1
        elif outcome == Message.Status.FAILED:
            summary.failed += 1
            summary.errors.append(f"#{message.pk}: {message.error}")
        elif outcome == Message.Status.SKIPPED:
            summary.skipped += 1
        elif outcome == "retrying":
            summary.retrying += 1
    return summary


def resend(actor, message: Message) -> bool:
    """Queue a failed message again (fresh attempts); audited. A portal PIN text is not
    resent: its PIN was masked when it failed (set a new PIN instead)."""
    if message.status != Message.Status.FAILED or message.kind == Kind.PORTAL_PIN:
        return False
    before = {"status": message.status, "attempts": message.attempts, "error": message.error}
    with transaction.atomic():
        message.status = Message.Status.QUEUED
        message.attempts = 0
        message.error = ""
        message.next_attempt_at = None
        message.save(update_fields=["status", "attempts", "error", "next_attempt_at", "updated_at"])
        AuditLog.record(
            actor=actor,
            action=RESEND_ACTION,
            target=message,
            before=before,
            after={"status": message.status},
            company=message.company_id,
        )
    return True
