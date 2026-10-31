"""Worker portal rules: access, PINs, sign-in with lockout, the portal session.

The office gives a worker access (``give_access``: copies the worker's phone, sets a PIN
it shows once), resets the PIN the same way, and revokes access (``revoke_access``).
The worker signs in with phone + PIN (``sign_in``) and may choose their own PIN
(``change_own_pin``).

Rules (the same as the foremen's phone PIN in ``apps.accounts.services``):

- PINs are 4 digits; four identical digits and straight runs (1234, 9876) are refused.
- 5 wrong PINs in a row lock the access for 15 minutes (even the right PIN is refused),
  with an ``AuditLog`` entry ``portal.pin_locked``. The answer to a wrong PIN says how
  many tries are left.
- Sign-in attempts are also limited per client IP (``SIGN_IN_RATE`` per minute) in the
  shared cache, so one address cannot guess across many phone numbers.
- A phone number signs in one worker only: among enabled accesses, phones are unique
  **across companies** (database constraint). Sign-in finds the access by phone first,
  then works inside ``use_company(access.company)``.
- Every access change (given, PIN reset, revoked, PIN chosen by the worker, lockout)
  writes an ``AuditLog`` entry; the PIN never reaches the audit log or the database in
  clear.

The portal session (``start_session``/``session_access``) uses its own keys in the
Django session: ``portal_worker_id`` and a keyed hash of the access's PIN hash, so a PIN
reset or a revoke ends every open portal session. It never sets ``request.user``: an
office sign-in does not open the portal and a portal sign-in does not open the office.
"""

from __future__ import annotations

import hashlib
import secrets
from datetime import timedelta
from functools import cache

from django.contrib.auth.hashers import check_password, make_password
from django.core.cache import cache as shared_cache
from django.db import IntegrityError, transaction
from django.db.models import F
from django.middleware.csrf import rotate_token
from django.utils import timezone
from django.utils.crypto import constant_time_compare, salted_hmac
from django.utils.translation import gettext as _

from apps.accounts.services import is_weak_pin, normalize_phone
from apps.core.models import AuditLog
from apps.core.tenancy import use_company

from .models import WorkerAccess

PIN_LENGTH = 4
PIN_MAX_ATTEMPTS = 5
PIN_LOCKOUT = timedelta(minutes=15)
SIGN_IN_RATE = 10  # sign-in attempts per client IP…
SIGN_IN_WINDOW = 60  # …per this many seconds
SESSION_AGE = timedelta(days=7)

SESSION_WORKER_KEY = "portal_worker_id"
SESSION_HASH_KEY = "portal_auth_hash"
_SESSION_SALT = "apps.portal.session"

# Audit actions.
AUDIT_ACCESS_GIVEN = "portal.access_given"
AUDIT_PIN_RESET = "portal.pin_reset"
AUDIT_ACCESS_REVOKED = "portal.access_revoked"
AUDIT_PIN_CHANGED = "portal.pin_changed"
AUDIT_PIN_LOCKED = "portal.pin_locked"


@cache
def _dummy_pin_hash() -> str:
    """Checked when the phone matches nobody, so a miss takes as long as a hit."""
    return make_password("0000")


# --- Errors ----------------------------------------------------------------------------


class AccessError(ValueError):
    """The office cannot give access (no phone, phone taken, inactive worker, bad PIN)."""


class SignInError(Exception):
    """Base class for portal sign-in failures."""


class SignInRefused(SignInError):
    """Unknown phone, access off, inactive worker or company."""


class WrongPin(SignInError):
    def __init__(self, attempts_left: int, locked_until=None):
        super().__init__(attempts_left)
        self.attempts_left = attempts_left
        self.locked_until = locked_until


class PinLocked(SignInError):
    def __init__(self, locked_until):
        super().__init__(locked_until)
        self.locked_until = locked_until


# --- PINs and phones -------------------------------------------------------------------


def is_valid_pin(pin: str) -> bool:
    return isinstance(pin, str) and len(pin) == PIN_LENGTH and pin.isascii() and pin.isdigit()


def generate_pin() -> str:
    """A random 4-digit PIN that is not a weak one."""
    while True:
        pin = f"{secrets.randbelow(10**PIN_LENGTH):0{PIN_LENGTH}d}"
        if not is_weak_pin(pin):
            return pin


def phone_taken(phone: str, *, worker) -> bool:
    """True if another worker's enabled access uses this phone.

    Deliberately across every company (like office users' phones): the sign-in page
    finds the worker from the number alone.
    """
    wanted = normalize_phone(phone)
    if not wanted:
        return False
    return (
        WorkerAccess.objects.filter(phone=wanted, is_enabled=True).exclude(worker=worker).exists()
    )


def minutes_left(until) -> int:
    """Whole minutes until ``until`` (at least 1)."""
    seconds = (until - timezone.now()).total_seconds()
    return max(1, int((seconds + 59) // 60))


# --- Office: give, reset, revoke -------------------------------------------------------


def access_of(worker) -> WorkerAccess | None:
    return WorkerAccess.objects.filter(worker=worker).first()


def access_status(access: WorkerAccess | None) -> tuple[str, str]:
    """``(status_badge status, label)`` of an access (or of no access), for the office."""
    if access is None:
        return "draft", _("No access")
    if not access.is_enabled:
        return "inactive", _("Turned off")
    if access.is_locked:
        return "rejected", _("Locked")
    return "active", _("Can sign in")


def give_access(worker, *, actor, pin: str = "") -> tuple[WorkerAccess, str]:
    """Give the worker portal access, or reset their PIN. Returns ``(access, pin)``.

    The phone is taken from the worker profile. Without ``pin`` a random one is
    generated; either way the caller shows it once and never stores it. Clears any
    lockout and ends the worker's open portal sessions (the session hash follows the
    PIN hash). Raises ``AccessError`` with a message for the office.
    """
    phone = normalize_phone(worker.phone)
    if not worker.is_active:
        raise AccessError(_("This worker is inactive. Make them active first."))
    if not phone:
        raise AccessError(_("Add the worker’s phone number on their profile first."))
    if pin and not is_valid_pin(pin):
        raise AccessError(_("Enter exactly 4 digits."))
    if pin and is_weak_pin(pin):
        raise AccessError(_("Too easy to guess: avoid 1111 or 1234."))
    if phone_taken(phone, worker=worker):
        raise AccessError(
            _(
                "Another worker already signs in to the worker portal with this phone "
                "number. Each worker needs their own number."
            )
        )
    pin = pin or generate_pin()

    with use_company(worker.company):
        access = access_of(worker)
        creating = access is None
        was_enabled = bool(access and access.is_enabled)
        old_phone = access.phone if access else None
        if creating:
            access = WorkerAccess(company=worker.company, worker=worker)
        access.phone = phone
        access.pin_hash = make_password(pin)
        access.is_enabled = True
        access.pin_chosen_by_worker = False
        access.failed_attempts = 0
        access.locked_until = None
        access.updated_by = actor if getattr(actor, "is_authenticated", False) else None
        try:
            with transaction.atomic():
                access.save()
        except IntegrityError as exc:  # another access took the phone meanwhile
            raise AccessError(
                _("Another worker already signs in to the worker portal with this phone number.")
            ) from exc
        AuditLog.record(
            actor=actor,
            action=AUDIT_PIN_RESET if was_enabled else AUDIT_ACCESS_GIVEN,
            target=access,
            before=None if creating else {"phone": old_phone, "enabled": was_enabled},
            after={"worker_id": worker.pk, "phone": phone, "enabled": True},
        )
    return access, pin


def text_pin(worker, pin: str, sign_in_url: str):
    """Queue a text with the new PIN and the sign-in link (``apps.messaging``), when the
    company has messaging on and the worker did not opt out. Returns the queued message
    or ``None``. Never raises: a text must not stop the office giving access. The PIN
    goes in the text only, never in the audit log."""
    import logging

    from apps.messaging import services as messaging

    try:
        with transaction.atomic():
            return messaging.queue_portal_pin(worker, pin, sign_in_url)
    except Exception:  # noqa: BLE001
        logging.getLogger("polaris.portal").exception(
            "could not queue the portal PIN text for worker %s", worker.pk
        )
        return None


def revoke_access(worker, *, actor, reason: str = "") -> bool:
    """Turn the worker's portal access off (open sessions end). False if it was off."""
    with use_company(worker.company):
        access = access_of(worker)
        if access is None or not access.is_enabled:
            return False
        access.is_enabled = False
        access.updated_by = actor if getattr(actor, "is_authenticated", False) else None
        access.save(update_fields=["is_enabled", "updated_by", "updated_at"])
        AuditLog.record(
            actor=actor,
            action=AUDIT_ACCESS_REVOKED,
            target=access,
            before={"enabled": True},
            after={"worker_id": worker.pk, "enabled": False},
            reason=reason,
        )
    return True


# --- Sign-in ---------------------------------------------------------------------------


def _ip_key(ip: str) -> str:
    return "portal:sign-in:" + hashlib.sha256(ip.encode()).hexdigest()


def sign_in_allowed(ip: str) -> bool:
    """Count one sign-in attempt from ``ip``; False once the per-minute limit is used."""
    key = _ip_key(ip or "unknown")
    shared_cache.add(key, 0, timeout=SIGN_IN_WINDOW)
    try:
        attempts = shared_cache.incr(key)
    except ValueError:  # expired between add() and incr()
        shared_cache.set(key, 1, timeout=SIGN_IN_WINDOW)
        attempts = 1
    return attempts <= SIGN_IN_RATE


def _find_access(phone: str) -> WorkerAccess | None:
    """The enabled access with this phone (unique across companies), if any."""
    wanted = normalize_phone(phone)
    if not wanted:
        return None
    return (
        WorkerAccess.objects.select_related("worker", "company")
        .filter(phone=wanted, is_enabled=True)
        .first()
    )


def _usable(access: WorkerAccess | None) -> bool:
    return bool(
        access is not None
        and access.is_enabled
        and access.pin_hash
        and access.worker.is_active
        and access.worker.company_id == access.company_id
        and access.company.is_active
    )


def _record_wrong_pin(access: WorkerAccess, now) -> WrongPin:
    WorkerAccess.objects.filter(pk=access.pk).update(failed_attempts=F("failed_attempts") + 1)
    access.refresh_from_db(fields=["failed_attempts"])
    attempts_left = max(PIN_MAX_ATTEMPTS - access.failed_attempts, 0)
    if attempts_left == 0:
        access.locked_until = now + PIN_LOCKOUT
        WorkerAccess.objects.filter(pk=access.pk).update(locked_until=access.locked_until)
        AuditLog.record(
            actor=None,
            action=AUDIT_PIN_LOCKED,
            target=access,
            after={"worker_id": access.worker_id, "locked_until": access.locked_until.isoformat()},
            reason=f"{PIN_MAX_ATTEMPTS} wrong PINs",
        )
    return WrongPin(attempts_left, access.locked_until)


def _check_lock(access: WorkerAccess, now) -> None:
    if access.locked_until is None:
        return
    if access.locked_until > now:
        raise PinLocked(access.locked_until)
    # The lockout is over: start counting again.
    access.failed_attempts = 0
    access.locked_until = None
    access.save(update_fields=["failed_attempts", "locked_until"])


def sign_in(*, phone: str, pin: str) -> WorkerAccess:
    """Check phone + PIN. Returns the access, or raises ``SignInRefused``, ``WrongPin``
    (``attempts_left`` tries remain; at 0 the access is locked) or ``PinLocked``."""
    access = _find_access(phone)
    if not _usable(access):
        check_password(pin, _dummy_pin_hash())
        raise SignInRefused
    # Everything below (lockout audit) belongs to the worker's company.
    with use_company(access.company):
        now = timezone.now()
        _check_lock(access, now)
        if not check_password(pin, access.pin_hash):
            raise _record_wrong_pin(access, now)
        access.failed_attempts = 0
        access.last_sign_in_at = now
        access.save(update_fields=["failed_attempts", "last_sign_in_at"])
    return access


def change_own_pin(access: WorkerAccess, *, current_pin: str, new_pin: str) -> WorkerAccess:
    """The signed-in worker chooses a new PIN. A wrong current PIN counts as a wrong PIN
    (and may lock the access). Raises ``WrongPin``, ``PinLocked`` or ``AccessError``."""
    if not is_valid_pin(new_pin):
        raise AccessError(_("Enter exactly 4 digits."))
    if is_weak_pin(new_pin):
        raise AccessError(_("Too easy to guess: avoid 1111 or 1234."))
    with use_company(access.company):
        now = timezone.now()
        _check_lock(access, now)
        if not check_password(current_pin, access.pin_hash):
            raise _record_wrong_pin(access, now)
        access.pin_hash = make_password(new_pin)
        access.pin_chosen_by_worker = True
        access.failed_attempts = 0
        access.save(
            update_fields=["pin_hash", "pin_chosen_by_worker", "failed_attempts", "updated_at"]
        )
        AuditLog.record(
            actor=None,
            action=AUDIT_PIN_CHANGED,
            target=access,
            after={"worker_id": access.worker_id},
            reason="Chosen by the worker in the portal.",
        )
    return access


# --- The portal session ----------------------------------------------------------------


def _session_hash(access: WorkerAccess) -> str:
    return salted_hmac(
        _SESSION_SALT, f"{access.pk}:{access.worker_id}:{access.pin_hash}", algorithm="sha256"
    ).hexdigest()


def start_session(request, access: WorkerAccess) -> None:
    """Sign the worker in to the portal in this browser.

    The whole session is replaced first: whoever used this browser before (an office
    user, another worker) is signed out, and the session key and CSRF token change.
    """
    request.session.flush()
    request.session[SESSION_WORKER_KEY] = access.worker_id
    request.session[SESSION_HASH_KEY] = _session_hash(access)
    request.session.set_expiry(int(SESSION_AGE.total_seconds()))
    rotate_token(request)


def refresh_session(request, access: WorkerAccess) -> None:
    """Keep the session after the worker changed their own PIN (new key too)."""
    request.session.cycle_key()
    request.session[SESSION_HASH_KEY] = _session_hash(access)


def end_session(request) -> None:
    """Sign the worker out of the portal (the whole session goes)."""
    request.session.flush()


def session_access(request) -> WorkerAccess | None:
    """The signed-in worker's access, or ``None`` (and the portal keys are dropped when
    the access was revoked, its PIN reset, or the worker or company made inactive)."""
    session = getattr(request, "session", None)
    if session is None:
        return None
    worker_id = session.get(SESSION_WORKER_KEY)
    if not worker_id:
        return None
    access = (
        WorkerAccess.objects.select_related("worker__trade", "company")
        .filter(worker_id=worker_id)
        .first()
    )
    if not _usable(access) or not constant_time_compare(
        session.get(SESSION_HASH_KEY, ""), _session_hash(access)
    ):
        session.pop(SESSION_WORKER_KEY, None)
        session.pop(SESSION_HASH_KEY, None)
        return None
    return access
