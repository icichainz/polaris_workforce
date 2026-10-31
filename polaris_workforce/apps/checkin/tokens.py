"""Badge tokens: what a worker's QR badge says, and how the server checks it.

A badge carries a **random public id** (10 random bytes, not the worker id) and an
**HMAC signature** of that id (``salted_hmac`` with ``SECRET_KEY``, truncated to 10
bytes). Both are Base32 (``A–Z``, ``2–7``), so the whole QR text fits QR *alphanumeric*
mode and prints as a small, robust code::

    PW1:<public id, 16 chars>.<signature, 16 chars>
    PW1:K7Q2M4XR5TBN3WCA.D6HZP2LQ4V7NYE3S

* ``PW1`` is the format version (Polaris Workforce badge v1).
* The signature lets the server refuse a made-up or mistyped code without a database
  lookup; the public id finds the ``Badge`` row, which says whose badge it is and
  whether it was revoked. A revoked badge keeps its signature but stops counting.
* The phone can recognise a scan offline: ``GET /sites/{id}/checkin-setup/`` lists the
  SHA-256 of every active badge's QR text (``payload_sha256``); the phone hashes what it
  scanned and looks it up. It never needs the signing key.
* Rotating ``SECRET_KEY``: badges keep working while the old key is listed in
  ``SECRET_KEY_FALLBACKS``; reissue and reprint them before dropping it.
"""

from __future__ import annotations

import base64
import hashlib
import re
import secrets

from django.conf import settings
from django.utils.crypto import constant_time_compare, salted_hmac

PREFIX = "PW1:"
ID_BYTES = 10  # 80 bits → 16 Base32 characters
SIGNATURE_BYTES = 10  # 80 bits → 16 Base32 characters
SALT = "polaris.checkin.badge.v1"
MAX_SCAN_LENGTH = 64  # longer scans are not badges (stored truncated when unknown)

_TOKEN = re.compile(r"([A-Z2-7]{16})\.([A-Z2-7]{16})")


def _b32(data: bytes) -> str:
    return base64.b32encode(data).decode("ascii").rstrip("=")


def new_public_id() -> str:
    """A fresh random badge id (16 Base32 characters)."""
    return _b32(secrets.token_bytes(ID_BYTES))


def signature(public_id: str, secret: str | None = None) -> str:
    digest = salted_hmac(SALT, public_id, secret=secret, algorithm="sha256").digest()
    return _b32(digest[:SIGNATURE_BYTES])


def make_token(public_id: str) -> str:
    """``<public id>.<signature>``: what follows ``PW1:`` on the badge."""
    return f"{public_id}.{signature(public_id)}"


def make_payload(public_id: str) -> str:
    """The QR text printed on the badge."""
    return PREFIX + make_token(public_id)


def payload_sha256(payload: str) -> str:
    """Hex SHA-256 of the QR text, for the phone's offline lookup."""
    return hashlib.sha256(payload.encode("ascii")).hexdigest()


def normalise(scanned: str) -> str:
    """The scan as the server compares it: trimmed, upper case, ``PW1:`` prefix kept."""
    text = (scanned or "").strip().upper()
    if not text.startswith(PREFIX):
        text = PREFIX + text
    return text


def verify(scanned: str) -> str | None:
    """The badge's public id if ``scanned`` is a well-formed, correctly signed badge
    text (with or without the ``PW1:`` prefix), else ``None``. Whether the badge exists
    and is active is the database's answer, not this function's."""
    text = normalise(scanned)
    match = _TOKEN.fullmatch(text[len(PREFIX) :])
    if match is None:
        return None
    public_id, sent = match.groups()
    for secret in [settings.SECRET_KEY, *getattr(settings, "SECRET_KEY_FALLBACKS", [])]:
        if constant_time_compare(sent, signature(public_id, secret=secret)):
            return public_id
    return None
