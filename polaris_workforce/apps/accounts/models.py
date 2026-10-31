"""Users, roles and foreman devices."""

from django.contrib.auth.models import AbstractUser
from django.db import models
from django.utils.translation import gettext_lazy as _


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

    def __str__(self) -> str:
        return self.get_full_name() or self.username
