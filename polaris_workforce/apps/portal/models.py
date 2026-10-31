"""Mobile web pages where workers see their own days, pay and advances.

Workers are not users: a worker signs in to the portal with their phone number and a
4-digit PIN kept on ``WorkerAccess``, which the office gives, resets and revokes
(``apps.portal.services``). The portal session is separate from office sign-in.
"""

from django.conf import settings
from django.db import models
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from apps.core.models import TimeStampedModel, company_field


class WorkerAccess(TimeStampedModel):
    """A worker's portal sign-in: phone number, PIN hash and lockout state.

    ``phone`` is the worker's phone number, normalised (``"+228 90-11 22 33"`` →
    ``"+22890112233"``), copied from the worker profile when the office gives access or
    sets a PIN. Among enabled accesses it is unique **across companies**, so the sign-in
    page finds the worker (and so the company) from the number alone, with no company
    code to type.
    """

    company = company_field(related_name="worker_accesses")
    worker = models.OneToOneField(
        "workers.Worker",
        verbose_name=_("worker"),
        on_delete=models.CASCADE,
        related_name="portal_access",
    )
    phone = models.CharField(
        _("phone"),
        max_length=32,
        help_text=_("Normalised; the number the worker signs in with."),
    )
    pin_hash = models.CharField(_("PIN hash"), max_length=128, blank=True)
    is_enabled = models.BooleanField(_("enabled"), default=True)
    pin_chosen_by_worker = models.BooleanField(
        _("PIN chosen by the worker"),
        default=False,
        help_text=_("False while the worker still uses the PIN the office gave them."),
    )
    failed_attempts = models.PositiveSmallIntegerField(_("wrong PINs in a row"), default=0)
    locked_until = models.DateTimeField(_("locked until"), null=True, blank=True)
    last_sign_in_at = models.DateTimeField(_("last sign-in"), null=True, blank=True)
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name=_("last changed by"),
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
    )

    class Meta:
        verbose_name = _("worker portal access")
        verbose_name_plural = _("worker portal accesses")
        ordering = ["company", "worker"]
        constraints = [
            # Deliberately across companies: sign-in needs no company code.
            models.UniqueConstraint(
                fields=["phone"],
                condition=models.Q(is_enabled=True),
                name="unique_portal_phone_when_enabled",
            ),
            models.UniqueConstraint(
                fields=["company", "worker"], name="unique_portal_access_per_company_worker"
            ),
            models.CheckConstraint(
                condition=~models.Q(phone=""), name="portal_access_phone_required"
            ),
        ]

    def __str__(self) -> str:
        return f"{self.worker} · {self.phone}"

    @property
    def is_locked(self) -> bool:
        return self.locked_until is not None and self.locked_until > timezone.now()
