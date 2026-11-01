"""The tracker service's token on ``POST /api/v1/presence/events/``.

``Authorization: Bearer <jwt>``: a service token the tracker mints with the shared secret
(``tokens.verify_tracker_service_token``). Only the events view lists this class, so the
token opens nothing else, and device tokens, sessions and agent tokens are not accepted
there. There is no user behind it: ``request.user`` stays anonymous and ``request.auth``
is a ``TrackerService``.
"""

import logging
from dataclasses import dataclass

from django.contrib.auth.models import AnonymousUser
from django.utils.translation import gettext_lazy as _
from drf_spectacular.extensions import OpenApiAuthenticationExtension
from rest_framework import exceptions
from rest_framework.authentication import BaseAuthentication, get_authorization_header
from rest_framework.permissions import BasePermission

from . import tokens

logger = logging.getLogger(__name__)
KEYWORD = "Bearer"


@dataclass(frozen=True)
class TrackerService:
    claims: dict


class TrackerServiceAuthentication(BaseAuthentication):
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
            claims = tokens.verify_tracker_service_token(parts[1].decode("ascii"))
        except (UnicodeError, tokens.TokenError) as exc:
            logger.warning("Presence: tracker service token refused: %s", exc)
            raise exceptions.AuthenticationFailed(
                _("Invalid or expired tracker service token.")
            ) from exc
        return AnonymousUser(), TrackerService(claims)

    def authenticate_header(self, request):
        # Makes DRF answer 401 (not 403) when credentials are missing.
        return self.keyword


class IsTrackerService(BasePermission):
    def has_permission(self, request, view) -> bool:
        return isinstance(request.auth, TrackerService)


class TrackerServiceScheme(OpenApiAuthenticationExtension):
    target_class = TrackerServiceAuthentication
    name = "TrackerService"

    def get_security_definition(self, auto_schema):
        return {
            "type": "http",
            "scheme": "bearer",
            "bearerFormat": "JWT",
            "description": (
                "A JWT (HS256) the tracker service mints with `TRACKER_SHARED_SECRET`: "
                '`kind = "service"`, `sub = "service:tracker"`, `aud = "polaris-django"`, '
                "`exp` at most 5 minutes ahead. Opens only `POST /api/v1/presence/events/`."
            ),
        }
