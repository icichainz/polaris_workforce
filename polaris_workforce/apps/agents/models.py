"""Agent tokens: how an AI assistant (Claude Desktop, claude.ai) acts for an office user.

An office user creates a token on the *AI assistant access* screen and pastes it into the
Polaris MCP server (``polaris_mcp/``). The token acts as that user: same company, same
role, same sites. It only opens the office API (``/api/v1/office/``), which reads and,
when ``can_approve`` is on, approves or rejects roll calls after a preview. It never
opens the phone endpoints, so an assistant can never record a payment.

Like a device token, the raw token is shown once; only its SHA-256 hash is stored.
"""

import hashlib

from django.conf import settings
from django.db import models
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from apps.core.models import company_field


class AgentToken(models.Model):
    company = company_field(related_name="agent_tokens")
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name=_("user"),
        on_delete=models.CASCADE,
        related_name="agent_tokens",
    )
    name = models.CharField(
        _("name"), max_length=100, help_text=_("Where it is used, e.g. “Claude on my laptop”.")
    )
    # The first characters of the raw token ("pol_ab12cd"), shown so people can tell
    # their tokens apart. Not enough to guess the rest.
    prefix = models.CharField(_("prefix"), max_length=12)
    token_hash = models.CharField(_("token hash"), max_length=64, unique=True)
    can_approve = models.BooleanField(
        _("may approve roll calls"),
        default=False,
        help_text=_("Approve or send back submitted days, always after a preview."),
    )
    expires_at = models.DateTimeField(_("expires at"))
    last_used_at = models.DateTimeField(_("last used at"), null=True, blank=True)
    revoked_at = models.DateTimeField(_("revoked at"), null=True, blank=True)
    created_at = models.DateTimeField(_("created at"), auto_now_add=True)

    class Meta:
        verbose_name = _("agent token")
        verbose_name_plural = _("agent tokens")
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"{self.name} ({self.prefix}…)"

    @staticmethod
    def hash_token(raw_token: str) -> str:
        return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()

    @property
    def is_expired(self) -> bool:
        return self.expires_at <= timezone.now()

    @property
    def is_active(self) -> bool:
        return self.revoked_at is None and not self.is_expired
