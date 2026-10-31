"""Accounts business logic: PIN sign-in with lockout, device tokens, users and roles.

Views and API views stay thin and call these functions. Every security-relevant change
(PIN set or reset, device revoke, role, site or active-status change, lockout) writes an
``AuditLog`` entry. PINs and tokens never reach the audit log or the database in clear.
"""

import hashlib
import re
from dataclasses import dataclass
from datetime import timedelta
from functools import cache

from django.conf import settings
from django.contrib.auth.hashers import check_password, make_password
from django.core.cache import cache as shared_cache
from django.db import transaction
from django.db.models import F
from django.utils import timezone
from rest_framework.throttling import BaseThrottle

from apps.api.authentication import generate_device_token
from apps.core.models import AuditLog
from apps.worksites.models import Site

from .models import ForemanDevice, User
from .permissions import has_role

Role = User.Role

PIN_MAX_ATTEMPTS = 5
PIN_LOCKOUT = timedelta(minutes=15)
PIN_ROLES = (Role.FOREMAN, Role.ADMIN)  # roles that may sign in on the phone

# Audit actions.
AUDIT_PIN_SET = "accounts.pin_set"
AUDIT_PIN_RESET = "accounts.pin_reset"
AUDIT_PIN_LOCKED = "accounts.pin_locked"
AUDIT_DEVICE_REVOKE = "accounts.device_revoke"
AUDIT_USER_INVITE = "accounts.user_invite"
AUDIT_ROLE_CHANGE = "accounts.role_change"
AUDIT_SITES_CHANGE = "accounts.sites_change"
AUDIT_ACTIVE_CHANGE = "accounts.active_change"
AUDIT_LOGIN_LOCKED = "accounts.login_locked"


@cache
def _dummy_pin_hash() -> str:
    """Checked when the phone number matches nobody, so a miss takes as long as a hit."""
    return make_password("0000")


# --- Errors ----------------------------------------------------------------------------


class SignInError(Exception):
    """Base class for PIN sign-in failures."""


class SignInRefused(SignInError):
    """Unknown phone, no PIN set, inactive user or a role that can't use the phone."""


class WrongPin(SignInError):
    def __init__(self, attempts_left: int, locked_until=None):
        super().__init__(attempts_left)
        self.attempts_left = attempts_left
        self.locked_until = locked_until


class PinLocked(SignInError):
    def __init__(self, locked_until):
        super().__init__(locked_until)
        self.locked_until = locked_until


# --- Phones and PINs -------------------------------------------------------------------


def normalize_phone(phone: str) -> str:
    """Digits only, keeping a leading ``+``: ``"+228 90-11 22 33"`` → ``"+22890112233"``."""
    phone = (phone or "").strip()
    digits = re.sub(r"\D", "", phone)
    return f"+{digits}" if phone.startswith("+") and digits else digits


def users_with_phone(phone: str, *, exclude_pk=None):
    """Users whose phone matches once both sides are normalised (spaces, dashes…)."""
    wanted = normalize_phone(phone)
    if not wanted:
        return []
    candidates = User.objects.exclude(phone="")
    if exclude_pk is not None:
        candidates = candidates.exclude(pk=exclude_pk)
    return [user for user in candidates if normalize_phone(user.phone) == wanted]


def is_weak_pin(pin: str) -> bool:
    """Four identical digits or a straight run (1234, 9876) are refused."""
    if len(set(pin)) == 1:
        return True
    steps = {int(b) - int(a) for a, b in zip(pin, pin[1:], strict=False)}
    return steps in ({1}, {-1})


def can_have_pin(user) -> bool:
    return user.is_superuser or user.role in PIN_ROLES


def set_pin(user: User, raw_pin: str, *, actor) -> User:
    """Set or reset a user's PIN. Clears any lockout. The PIN itself is never logged."""
    if not can_have_pin(user):
        raise ValueError("Only foremen and admins sign in with a PIN.")
    action = AUDIT_PIN_RESET if user.has_pin else AUDIT_PIN_SET
    user.set_pin(raw_pin)
    user.pin_failed_attempts = 0
    user.pin_locked_until = None
    user.save(update_fields=["pin_hash", "pin_failed_attempts", "pin_locked_until"])
    AuditLog.record(actor=actor, action=action, target=user)
    return user


# --- Office sign-in limits ---------------------------------------------------------------
#
# /accounts/login/ and /admin/login/ (forms in .forms). After LOGIN_MAX_ATTEMPTS wrong
# passwords for one (username, client IP) within LOGIN_LOCKOUT_MINUTES, that pair is
# refused for LOGIN_LOCKOUT_MINUTES, even with the right password. Keyed on the pair so a
# stranger cannot lock a colleague out from another address. The counters live in the
# shared cache (CACHES in config/settings.py), never in the database.


def login_client_ip(request) -> str:
    """The client IP, trusting ``X-Forwarded-For`` only as far as ``DJANGO_NUM_PROXIES``
    (the same rule as the API throttles)."""
    if request is None:
        return ""
    return BaseThrottle().get_ident(request) or ""


def _login_key(username: str, ip: str) -> str:
    digest = hashlib.sha256(f"{username.strip().lower()}\0{ip}".encode()).hexdigest()
    return f"accounts:login:{digest}"


def _lockout_window() -> timedelta:
    return timedelta(minutes=settings.LOGIN_LOCKOUT_MINUTES)


def login_locked_until(username: str, ip: str):
    """When the lockout of this (username, IP) ends, or ``None`` if it is not locked."""
    until = shared_cache.get(_login_key(username, ip) + ":locked")
    if until is not None and until > timezone.now():
        return until
    return None


def record_failed_login(username: str, ip: str):
    """Count a wrong password. Returns the end of the lockout if this one started it."""
    key = _login_key(username, ip)
    window = int(_lockout_window().total_seconds())
    shared_cache.add(key, 0, timeout=window)
    try:
        failures = shared_cache.incr(key)
    except ValueError:  # expired between add() and incr()
        shared_cache.set(key, 1, timeout=window)
        failures = 1
    if failures < settings.LOGIN_MAX_ATTEMPTS:
        return None
    until = timezone.now() + _lockout_window()
    shared_cache.set(key + ":locked", until, timeout=window)
    shared_cache.delete(key)
    user = User._default_manager.filter(**{User.USERNAME_FIELD: username}).first()
    # The typed username is kept only when it is a real account (it might be a password
    # typed into the wrong box).
    after = {"ip": ip, "locked_until": until.isoformat()}
    if user is not None:
        after["username"] = username
    AuditLog.record(
        actor=None,
        action=AUDIT_LOGIN_LOCKED,
        target=user,
        after=after,
        reason=f"{settings.LOGIN_MAX_ATTEMPTS} wrong passwords",
    )
    return until


def clear_failed_logins(username: str, ip: str) -> None:
    key = _login_key(username, ip)
    shared_cache.delete_many([key, key + ":locked"])


# --- Sign-in ---------------------------------------------------------------------------


@dataclass
class SignIn:
    token: str
    device: ForemanDevice
    user: User


def _find_pin_user(phone: str) -> User | None:
    matches = [user for user in users_with_phone(phone) if user.has_pin]
    return matches[0] if len(matches) == 1 else None


def _record_wrong_pin(user: User, now) -> WrongPin:
    User.objects.filter(pk=user.pk).update(pin_failed_attempts=F("pin_failed_attempts") + 1)
    user.refresh_from_db(fields=["pin_failed_attempts"])
    attempts_left = max(PIN_MAX_ATTEMPTS - user.pin_failed_attempts, 0)
    if attempts_left == 0:
        user.pin_locked_until = now + PIN_LOCKOUT
        User.objects.filter(pk=user.pk).update(pin_locked_until=user.pin_locked_until)
        AuditLog.record(
            actor=None,
            action=AUDIT_PIN_LOCKED,
            target=user,
            after={"locked_until": user.pin_locked_until.isoformat()},
            reason=f"{PIN_MAX_ATTEMPTS} wrong PINs",
        )
    return WrongPin(attempts_left, user.pin_locked_until)


def pin_sign_in(*, phone: str, pin: str, device_id: str, device_name: str = "") -> SignIn:
    """Check phone + PIN and issue a new device token.

    Raises ``SignInRefused``, ``WrongPin`` (after which ``attempts_left`` tries remain;
    at 0 the user is locked for ``PIN_LOCKOUT``) or ``PinLocked``. Signing in again from
    the same ``device_id`` replaces that device's token; the same phone signed in as
    someone else is revoked, so a phone holds one valid token.
    """
    now = timezone.now()
    user = _find_pin_user(phone)
    if user is None or not user.is_active or not has_role(user, *PIN_ROLES):
        check_password(pin, _dummy_pin_hash())
        raise SignInRefused
    if user.pin_locked_until is not None:
        if user.pin_locked_until > now:
            raise PinLocked(user.pin_locked_until)
        # The lockout is over: start counting again.
        user.pin_failed_attempts = 0
        user.pin_locked_until = None
        user.save(update_fields=["pin_failed_attempts", "pin_locked_until"])
    if not user.check_pin(pin):
        raise _record_wrong_pin(user, now)

    raw_token, token_hash = generate_device_token()
    with transaction.atomic():
        if user.pin_failed_attempts:
            user.pin_failed_attempts = 0
            user.save(update_fields=["pin_failed_attempts"])
        device, created = ForemanDevice.objects.get_or_create(
            user=user,
            device_id=device_id,
            defaults={"token_hash": token_hash, "name": device_name, "last_seen_at": now},
        )
        if not created:
            device.token_hash = token_hash
            device.revoked_at = None
            device.last_seen_at = now
            if device_name:
                device.name = device_name
            device.save(update_fields=["token_hash", "revoked_at", "last_seen_at", "name"])
        others = ForemanDevice.objects.filter(device_id=device_id, revoked_at__isnull=True)
        for other in others.exclude(user=user):
            revoke_device(other, actor=None, reason="Another user signed in on this phone.")
    return SignIn(token=raw_token, device=device, user=user)


def revoke_device(device: ForemanDevice, *, actor, reason: str = "") -> bool:
    """Revoke a device token. Returns False if it was already revoked."""
    if device.revoked_at is not None:
        return False
    device.revoked_at = timezone.now()
    device.save(update_fields=["revoked_at"])
    AuditLog.record(
        actor=actor,
        action=AUDIT_DEVICE_REVOKE,
        target=device,
        after={"user_id": device.user_id, "device_id": device.device_id},
        reason=reason,
    )
    return True


def revoke_all_devices(user: User, *, actor, reason: str = "") -> int:
    return sum(
        revoke_device(device, actor=actor, reason=reason)
        for device in user.devices.filter(revoked_at__isnull=True)
    )


# --- Users and roles -------------------------------------------------------------------


def last_active(user: User):
    """The later of the last office sign-in and the last phone call, or None."""
    seen = [device.last_seen_at for device in user.devices.all() if device.last_seen_at]
    if user.last_login:
        seen.append(user.last_login)
    return max(seen, default=None)


def _site_ids(sites) -> list[int]:
    return sorted(site.pk for site in sites)


@transaction.atomic
def save_user(
    user: User,
    *,
    actor,
    managed_sites=(),
    foreman_sites=(),
    password: str = "",
    reason: str = "",
) -> User:
    """Create or update a user from the Users screen, with their site assignments.

    ``user`` is an unsaved or changed instance (e.g. ``form.save(commit=False)``).
    Managed sites are written through ``Site.manager`` and foreman sites through
    ``Site.foremen``; only the list matching the role is kept. A user who can no
    longer use the phone (disabled, or a role without PIN sign-in) is signed out of
    every phone.
    """
    creating = user.pk is None
    before = None
    if not creating:
        old = User.objects.get(pk=user.pk)
        before = {"role": old.role, "is_active": old.is_active}
        old_managed = _site_ids(Site.objects.filter(manager=old))
        old_foreman = _site_ids(old.foreman_sites.all())
    if creating:
        if password:
            user.set_password(password)
        else:
            user.set_unusable_password()
    user.save()

    managed = list(managed_sites) if user.role == Role.SITE_MANAGER else []
    foreman = list(foreman_sites) if user.role == Role.FOREMAN else []
    Site.objects.filter(manager=user).exclude(pk__in=[s.pk for s in managed]).update(manager=None)
    Site.objects.filter(pk__in=[s.pk for s in managed]).update(manager=user)
    user.foreman_sites.set(foreman)
    sites_after = {"managed_sites": _site_ids(managed), "foreman_sites": _site_ids(foreman)}

    if creating:
        AuditLog.record(
            actor=actor,
            action=AUDIT_USER_INVITE,
            target=user,
            after={"role": user.role, **sites_after},
            reason=reason,
        )
        return user

    if before["role"] != user.role:
        AuditLog.record(
            actor=actor,
            action=AUDIT_ROLE_CHANGE,
            target=user,
            before={"role": before["role"]},
            after={"role": user.role},
            reason=reason,
        )
    if before["is_active"] != user.is_active:
        AuditLog.record(
            actor=actor,
            action=AUDIT_ACTIVE_CHANGE,
            target=user,
            before={"is_active": before["is_active"]},
            after={"is_active": user.is_active},
            reason=reason,
        )
    sites_before = {"managed_sites": old_managed, "foreman_sites": old_foreman}
    if sites_before != sites_after:
        AuditLog.record(
            actor=actor,
            action=AUDIT_SITES_CHANGE,
            target=user,
            before=sites_before,
            after=sites_after,
            reason=reason,
        )
    if not user.is_active or not can_have_pin(user):
        revoke_all_devices(user, actor=actor, reason="No longer allowed to use the phone app.")
    return user
