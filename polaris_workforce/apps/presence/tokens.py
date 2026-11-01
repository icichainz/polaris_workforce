"""JWT HS256 tokens shared with the tracker, with the standard library only.

Django issues every token the tracker accepts (``aud = "polaris-tracker"``) and accepts
the short service tokens the tracker sends back (``aud = "polaris-django"``). Both sides
sign with ``TRACKER_SHARED_SECRET``. The scheme, byte for byte (the Gleam side in
``polaris_tracker/src/polaris_tracker/token.gleam`` does the same):

- header ``{"alg":"HS256","typ":"JWT"}``; header and claims are compact JSON (no spaces);
- each part base64url without ``=`` padding; signature = HMAC-SHA256 over
  ``<header>.<claims>`` with the secret's UTF-8 bytes.

Claims, version 2 (``docs/implementation-plan.md`` §14), in this order::

    {"sub", "kind", "company", "sites", "travel", "aud", "exp"}

``company`` is left out of service tokens; ``sites`` is always an explicit list (a company
admin gets every site of the company, never an ``all_sites`` claim); ``travel`` (may see
off-site track points) is true for company admins only.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import re
import time
from dataclasses import dataclass

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured

TRACKER_AUDIENCE = "polaris-tracker"
DJANGO_AUDIENCE = "polaris-django"

DEVICE = "device"
GATEWAY = "gateway"
DASHBOARD = "dashboard"
SERVICE = "service"
KINDS = (DEVICE, GATEWAY, DASHBOARD, SERVICE)

DEVICE_LIFETIME = 12 * 3600
DASHBOARD_LIFETIME = 3600
GATEWAY_LIFETIME = 365 * 24 * 3600
SERVICE_LIFETIME = 300
# Accepted on top of SERVICE_LIFETIME for a service token's ``exp``: the two clocks drift.
CLOCK_SKEW = 60

TRACKER_SERVICE_SUBJECT = "service:tracker"
DJANGO_SERVICE_SUBJECT = "service:django"

HEADER = {"alg": "HS256", "typ": "JWT"}
_DEV_SECRET = "dev-only-tracker-secret-change-me"
_B64URL = re.compile(r"[A-Za-z0-9_-]+")


class TokenError(Exception):
    """A token that must be refused; ``str()`` says why (for the 401 message and logs)."""


def shared_secret() -> bytes:
    """``TRACKER_SHARED_SECRET`` as bytes. Required unless ``DEBUG`` (a dev default)."""
    secret = getattr(settings, "TRACKER_SHARED_SECRET", "")
    if not secret:
        if not settings.DEBUG:
            raise ImproperlyConfigured(
                "TRACKER_SHARED_SECRET is not set: presence tracking needs the secret shared "
                "with the tracker (see .env.example)."
            )
        secret = _DEV_SECRET
    return secret.encode("utf-8")


def b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def b64url_decode(text: str) -> bytes:
    """Base64url without padding only (``=``, ``+`` and ``/`` are refused)."""
    if not _B64URL.fullmatch(text or ""):
        raise TokenError("The token is not a valid JWT.")
    try:
        return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))
    except (binascii.Error, ValueError) as exc:
        raise TokenError("The token is not a valid JWT.") from exc


def _json(value: dict) -> bytes:
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def _sign(signing_input: bytes, secret: bytes) -> bytes:
    return hmac.new(secret, signing_input, hashlib.sha256).digest()


def encode(claims: dict, secret: bytes | None = None) -> str:
    """Sign ``claims`` (keys kept in their order) as a compact JWT."""
    secret = shared_secret() if secret is None else secret
    signing_input = f"{b64url_encode(_json(HEADER))}.{b64url_encode(_json(claims))}"
    signature = _sign(signing_input.encode("ascii"), secret)
    return f"{signing_input}.{b64url_encode(signature)}"


def decode(token: str, *, audience: str, secret: bytes | None = None, now: int | None = None):
    """The claims of a valid, unexpired HS256 token for ``audience``; else ``TokenError``."""
    secret = shared_secret() if secret is None else secret
    parts = token.split(".") if isinstance(token, str) else []
    if len(parts) != 3:
        raise TokenError("The token is not a valid JWT.")
    header_b64, claims_b64, signature_b64 = parts
    try:
        header = json.loads(b64url_decode(header_b64))
        claims = json.loads(b64url_decode(claims_b64))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise TokenError("The token is not a valid JWT.") from exc
    if not isinstance(header, dict) or not isinstance(claims, dict):
        raise TokenError("The token is not a valid JWT.")
    if header.get("alg") != "HS256":
        raise TokenError("Only HS256 tokens are accepted.")
    expected = _sign(f"{header_b64}.{claims_b64}".encode("ascii"), secret)
    if not hmac.compare_digest(expected, b64url_decode(signature_b64)):
        raise TokenError("The token signature is invalid.")
    if claims.get("aud") != audience:
        raise TokenError("The token was not issued for this service.")
    exp = claims.get("exp")
    if not isinstance(exp, int) or isinstance(exp, bool):
        raise TokenError("The token has no expiry.")
    if exp <= (int(time.time()) if now is None else now):
        raise TokenError("The token has expired.")
    return claims


# --- the tokens Django issues ------------------------------------------------------------


@dataclass(frozen=True)
class IssuedToken:
    token: str
    claims: dict
    expires_at: int  # Unix seconds


def mint(
    *,
    kind: str,
    sub: str,
    lifetime: int,
    company: int | None = None,
    sites=(),
    travel: bool = False,
    audience: str = TRACKER_AUDIENCE,
    now: int | None = None,
    secret: bytes | None = None,
) -> IssuedToken:
    """A claims-v2 token. ``company`` is required for every kind except ``service``."""
    if kind not in KINDS:
        raise ValueError(f"Unknown token kind {kind!r}")
    if kind != SERVICE and company is None:
        raise ValueError("Device, gateway and dashboard tokens belong to a company.")
    exp = (int(time.time()) if now is None else now) + lifetime
    claims: dict = {"sub": sub, "kind": kind}
    if kind != SERVICE:
        claims["company"] = int(company)
    claims["sites"] = sorted({int(site) for site in sites})
    claims["travel"] = bool(travel)
    claims["aud"] = audience
    claims["exp"] = exp
    return IssuedToken(token=encode(claims, secret), claims=claims, expires_at=exp)


def mint_service_token(*, now: int | None = None, secret: bytes | None = None) -> str:
    """For Django's calls to the tracker (``PUT /internal/sites/{id}/config``): 5 minutes."""
    return mint(
        kind=SERVICE,
        sub=DJANGO_SERVICE_SUBJECT,
        lifetime=SERVICE_LIFETIME,
        now=now,
        secret=secret,
    ).token


# --- the tokens Django accepts -----------------------------------------------------------


def verify_tracker_service_token(
    token: str, *, now: int | None = None, secret: bytes | None = None
) -> dict:
    """The claims of a service token minted by the tracker for Django, else ``TokenError``.

    Signature, ``aud = polaris-django``, ``kind = service``, ``sub = service:tracker``, not
    expired, and ``exp`` at most 5 minutes ahead (plus ``CLOCK_SKEW``): a long-lived token
    is refused even when correctly signed.
    """
    now = int(time.time()) if now is None else now
    claims = decode(token, audience=DJANGO_AUDIENCE, secret=secret, now=now)
    if claims.get("kind") != SERVICE:
        raise TokenError("Only service tokens may call this endpoint.")
    if claims.get("sub") != TRACKER_SERVICE_SUBJECT:
        raise TokenError("The token is not the tracker's.")
    if claims["exp"] > now + SERVICE_LIFETIME + CLOCK_SKEW:
        raise TokenError("The token lives too long for a service token.")
    return claims
