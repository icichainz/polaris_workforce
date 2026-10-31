"""Users, roles and foreman devices."""

import hashlib

from django.contrib.auth.hashers import check_password, make_password
from django.contrib.auth.models import AbstractUser
from django.db import models
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from apps.core.choices import Language


class User(AbstractUser):
    """Someone who signs in: office staff or a foreman.

    Workers are not users; they are records managed in the workers app.
    """

    class Role(models.TextChoices):
        ADMIN = "admin", _("Admin")
        SITE_MANAGER = "site_manager", _("Site manager")
        FOREMAN = "foreman", _("Foreman")
        ACCOUNTANT = "accountant", _("Accountant")

    role = models.CharField(_("role"), max_length=20, choices=Role.choices, default=Role.FOREMAN)
    phone = models.CharField(_("phone"), max_length=32, blank=True)
    # Blank means "use CompanySettings.language".
    language = models.CharField(_("language"), max_length=2, choices=Language.choices, blank=True)

    # Phone sign-in. The PIN belongs to the person, not to a phone: it is set on the
    # Users screen before the first sign-in and works on any device. Stored with
    # Django's password hashers; the clear PIN is never kept.
    pin_hash = models.CharField(_("PIN hash"), max_length=128, blank=True)
    pin_failed_attempts = models.PositiveSmallIntegerField(_("failed PIN attempts"), default=0)
    pin_locked_until = models.DateTimeField(_("PIN locked until"), null=True, blank=True)

    def __str__(self) -> str:
        return self.get_full_name() or self.username

    @property
    def has_pin(self) -> bool:
        return bool(self.pin_hash)

    @property
    def is_pin_locked(self) -> bool:
        """True while a PIN lockout is in effect."""
        return self.pin_locked_until is not None and self.pin_locked_until > timezone.now()

    def set_pin(self, raw_pin: str) -> None:
        """Hash and store a PIN (not saved). Use ``accounts.services.set_pin`` from views."""
        self.pin_hash = make_password(raw_pin)

    def check_pin(self, raw_pin: str) -> bool:
        return bool(self.pin_hash) and check_password(raw_pin, self.pin_hash)

    def can(self, action: str, site=None) -> bool:
        """Shortcut for ``accounts.permissions.can(user, action, site)``."""
        from .permissions import can

        return can(self, action, site)


class ForemanDevice(models.Model):
    """A phone a foreman has signed in on.

    The raw device token is only ever shown to the phone; the database keeps its sha256
    hex digest (``token_hash``).

    ``pin_hash``, ``failed_attempts`` and ``locked_until`` are unused: the PIN and its
    lockout live on ``User`` (a device row only exists after the first sign-in). They
    are kept to avoid a destructive migration and may be dropped later.
    """

    user = models.ForeignKey(
        User, verbose_name=_("user"), on_delete=models.CASCADE, related_name="devices"
    )
    device_id = models.CharField(_("device ID"), max_length=128)
    name = models.CharField(_("device name"), max_length=100, blank=True)
    token_hash = models.CharField(_("token hash"), max_length=64, unique=True)
    pin_hash = models.CharField(_("PIN hash"), max_length=128, blank=True)
    failed_attempts = models.PositiveSmallIntegerField(_("failed attempts"), default=0)
    locked_until = models.DateTimeField(_("locked until"), null=True, blank=True)
    last_seen_at = models.DateTimeField(_("last seen at"), null=True, blank=True)
    revoked_at = models.DateTimeField(_("revoked at"), null=True, blank=True)
    created_at = models.DateTimeField(_("created at"), auto_now_add=True)

    class Meta:
        verbose_name = _("foreman device")
        verbose_name_plural = _("foreman devices")
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(fields=["user", "device_id"], name="unique_device_per_user"),
        ]

    def __str__(self) -> str:
        return f"{self.user} · {self.device_id}"

    @property
    def is_active(self) -> bool:
        """A device is active until it is revoked."""
        return self.revoked_at is None

    @property
    def is_locked(self) -> bool:
        """True while a PIN lockout is in effect."""
        return self.locked_until is not None and self.locked_until > timezone.now()

    @staticmethod
    def hash_token(raw_token: str) -> str:
        """The value stored in ``token_hash`` for a raw device token."""
        return hashlib.sha256(raw_token.encode()).hexdigest()
