"""Agent tokens on the office API: ``Authorization: Bearer pol_…``.

Only the office API views (``apps.agents.api``) list this class. It is deliberately not
in ``REST_FRAMEWORK["DEFAULT_AUTHENTICATION_CLASSES"]``: on every other endpoint an agent
token is not a credential at all and gets ``401``, so an assistant can never take the
roll call or record a payment.

A token stops working when it is revoked, when it expires, when its user is disabled or
moved to a role without office access, and when the company is inactive.
"""

from datetime import timedelta

from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from drf_spectacular.extensions import OpenApiAuthenticationExtension
from rest_framework import exceptions
from rest_framework.authentication import BaseAuthentication, get_authorization_header
from rest_framework.permissions import BasePermission
from rest_framework.throttling import SimpleRateThrottle

from .models import AgentToken

KEYWORD = "Bearer"
LAST_USED_INTERVAL = timedelta(minutes=1)


class AgentTokenAuthentication(BaseAuthentication):
    """Authenticates ``Authorization: Bearer <token>``; sets ``request.auth`` to the token."""

    keyword = KEYWORD

    def authenticate(self, request):
        parts = get_authorization_header(request).split()
        if not parts or parts[0].lower() != self.keyword.lower().encode():
            return None
        if len(parts) != 2:
            raise exceptions.AuthenticationFailed(
                _("Invalid Authorization header. Use: Bearer <token>.")
            )
        try:
            raw = parts[1].decode("ascii")
        except UnicodeError as exc:
            raise exceptions.AuthenticationFailed(_("Invalid agent token.")) from exc
        return self.authenticate_token(raw)

    def authenticate_token(self, raw: str):
        # Imported here: services imports the permissions module, which imports models.
        from .services import may_hold_token

        token = (
            AgentToken.objects.select_related("user__company")
            .filter(token_hash=AgentToken.hash_token(raw), revoked_at__isnull=True)
            .first()
        )
        if token is None:
            raise exceptions.AuthenticationFailed(_("Invalid or revoked agent token."))
        if token.is_expired:
            raise exceptions.AuthenticationFailed(
                _("This agent token has expired. Create a new one in Polaris.")
            )
        user = token.user
        company = user.company
        if (
            not user.is_active
            or company is None
            or not company.is_active
            or company.pk != token.company_id
            or not may_hold_token(user)
        ):
            raise exceptions.AuthenticationFailed(_("This user account is disabled."))
        self.touch(token)
        return user, token

    @staticmethod
    def touch(token: AgentToken) -> None:
        """Record ``last_used_at``, writing at most once per ``LAST_USED_INTERVAL``."""
        now = timezone.now()
        if token.last_used_at is not None and now - token.last_used_at < LAST_USED_INTERVAL:
            return
        AgentToken.objects.filter(pk=token.pk).update(last_used_at=now)
        token.last_used_at = now

    def authenticate_header(self, request):
        # Makes DRF answer 401 (not 403) when credentials are missing.
        return self.keyword


class IsAgentToken(BasePermission):
    """The request was authenticated by an agent token (not a session or a phone)."""

    def has_permission(self, request, view) -> bool:
        return isinstance(request.auth, AgentToken)


class AgentTokenCanApprove(BasePermission):
    """The token was created with *may approve roll calls*."""

    message = _("This agent token may only read. Create one that may approve roll calls.")

    def has_permission(self, request, view) -> bool:
        return isinstance(request.auth, AgentToken) and request.auth.can_approve


class AgentTokenThrottle(SimpleRateThrottle):
    """Per token: an assistant in a loop must not keep SQLite busy for the office."""

    scope = "agent"

    def get_cache_key(self, request, view):
        if not isinstance(request.auth, AgentToken):
            return None
        # The hash, not the id: never reused, even if a token row is deleted.
        return self.cache_format % {"scope": self.scope, "ident": request.auth.token_hash}


class AgentTokenScheme(OpenApiAuthenticationExtension):
    target_class = AgentTokenAuthentication
    name = "AgentToken"

    def get_security_definition(self, auto_schema):
        return {
            "type": "http",
            "scheme": "bearer",
            "description": (
                "Agent token from the office screen *AI assistant access*, sent as "
                "`Authorization: Bearer pol_…`. Opens only `/api/v1/office/`."
            ),
        }
