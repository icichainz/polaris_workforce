"""Device tokens for the foreman phone app.

A foreman signs in once per phone with phone number + PIN (``POST /auth/pin``). The
server answers with a random device token, stores only its SHA-256 hash on a
``ForemanDevice`` row, and the phone sends it on every call::

    Authorization: Device <token>

Revoking the device (``POST /auth/logout`` or the Users screen) sets ``revoked_at``;
the token then gets ``401 not_authenticated``. So does a token unused for
``DEVICE_TOKEN_IDLE_DAYS`` days (default 30, measured on ``last_seen_at``), and the token
of a user whose company is inactive. The token's user then decides the current company
of the request (``apps.core.tenancy``). A kiosk phone's token only opens the check-in
endpoints (``apps.checkin.kiosk``); every other endpoint answers ``403``.
"""

import hashlib
import secrets
from datetime import timedelta

from django.apps import apps
from django.conf import settings
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from drf_spectacular.extensions import OpenApiAuthenticationExtension
from rest_framework import exceptions
from rest_framework.authentication import BaseAuthentication, get_authorization_header

KEYWORD = "Device"
TOKEN_BYTES = 32
LAST_SEEN_INTERVAL = timedelta(minutes=1)


def hash_device_token(raw_token: str) -> str:
    return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()


def generate_device_token() -> tuple[str, str]:
    """Return ``(raw_token, token_hash)``. Give the raw token to the phone once; store the hash."""
    raw = secrets.token_urlsafe(TOKEN_BYTES)
    return raw, hash_device_token(raw)


def get_device_model():
    return apps.get_model("accounts", "ForemanDevice")


def is_idle_expired(device, now=None) -> bool:
    """True when the token has not been used for ``DEVICE_TOKEN_IDLE_DAYS`` days.

    Measured on ``last_seen_at`` (set at sign-in and on use), or ``created_at`` for a
    device that was never seen.
    """
    last_used = device.last_seen_at or device.created_at
    if last_used is None:
        return False
    idle = timedelta(days=settings.DEVICE_TOKEN_IDLE_DAYS)
    return (now or timezone.now()) - last_used > idle


class DeviceTokenAuthentication(BaseAuthentication):
    """Authenticates ``Authorization: Device <token>``; sets ``request.auth`` to the device."""

    keyword = KEYWORD

    def authenticate(self, request):
        parts = get_authorization_header(request).split()
        if not parts or parts[0].lower() != self.keyword.lower().encode():
            return None
        if len(parts) != 2:
            raise exceptions.AuthenticationFailed(
                _("Invalid Authorization header. Use: Device <token>.")
            )
        try:
            token = parts[1].decode("ascii")
        except UnicodeError as exc:
            raise exceptions.AuthenticationFailed(_("Invalid device token.")) from exc
        user, device = self.authenticate_token(token)
        # A kiosk phone (``apps.checkin.kiosk``) may only call the check-in endpoints:
        # anything else is 403 permission_denied, whatever the view's own permissions.
        from apps.checkin.kiosk import enforce_kiosk_scope

        enforce_kiosk_scope(request, device)
        return user, device

    def authenticate_token(self, token: str):
        Device = get_device_model()
        try:
            device = Device.objects.select_related("user__company").get(
                token_hash=hash_device_token(token), revoked_at__isnull=True
            )
        except Device.DoesNotExist as exc:
            raise exceptions.AuthenticationFailed(_("Invalid or revoked device token.")) from exc
        if not device.user.is_active:
            raise exceptions.AuthenticationFailed(_("This user account is disabled."))
        company = device.user.company
        if company is None or not company.is_active:
            raise exceptions.AuthenticationFailed(_("This user account is disabled."))
        if is_idle_expired(device):
            raise exceptions.AuthenticationFailed(
                _("This device token has expired. Sign in again with your PIN.")
            )
        self.touch(device)
        return device.user, device

    @staticmethod
    def touch(device) -> None:
        """Record ``last_seen_at``, writing at most once per ``LAST_SEEN_INTERVAL``."""
        now = timezone.now()
        if device.last_seen_at is not None and now - device.last_seen_at < LAST_SEEN_INTERVAL:
            return
        type(device).objects.filter(pk=device.pk).update(last_seen_at=now)
        device.last_seen_at = now

    def authenticate_header(self, request):
        # Makes DRF answer 401 (not 403) when credentials are missing.
        return self.keyword


class DeviceTokenScheme(OpenApiAuthenticationExtension):
    target_class = DeviceTokenAuthentication
    name = "DeviceToken"

    def get_security_definition(self, auto_schema):
        return {
            "type": "apiKey",
            "in": "header",
            "name": "Authorization",
            "description": (
                "Device token from `POST /api/v1/auth/pin/`, sent as "
                "`Authorization: Device <token>`."
            ),
        }
