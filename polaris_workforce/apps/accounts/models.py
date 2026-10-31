"""Users, roles and foreman devices."""

import hashlib

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

    def __str__(self) -> str:
        return self.get_full_name() or self.username


class ForemanDevice(models.Model):
    """A phone a foreman has signed in on.

    The raw device token is only ever shown to the phone; the database keeps its sha256
    hex digest (``token_hash``). The PIN is stored with Django's password hashers.
    """

    user = models.ForeignKey(
        User, verbose_name=_("user"), on_delete=models.CASCADE, related_name="devices"
    )
    device_id = models.CharField(_("device ID"), max_length=128)
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
